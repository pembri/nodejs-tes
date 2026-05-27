"""
Vidira - Vercel Proxy (Python)
Fetch URL video/HLS/DASH apapun dan forward ke browser dengan CORS header.
Supports: m3u8, ts, mp4, mpd, dan format lain.
"""

import urllib.request
import urllib.parse
import urllib.error
import re
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        # Parse query string
        parsed   = urllib.parse.urlparse(self.path)
        params   = urllib.parse.parse_qs(parsed.query)

        target   = params.get('url',  [None])[0]
        ref      = params.get('ref',  [None])[0]
        origin   = params.get('origin', [None])[0]
        ua       = params.get('ua',   [None])[0]

        if not target:
            self._error(400, 'Missing ?url= parameter')
            return

        # Decode kalau masih encoded ganda
        target = urllib.parse.unquote(target)

        # Tentukan Referer & Origin — spoof sesuai domain target
        try:
            parsed_target = urllib.parse.urlparse(target)
            target_origin = f"{parsed_target.scheme}://{parsed_target.netloc}"
            referer       = ref or origin or (target_origin + '/')
        except Exception:
            target_origin = ''
            referer       = ref or ''

        req = urllib.request.Request(target)

        # Header yang bikin server kira request dari browser asli
        user_agent = ua or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        req.add_header('User-Agent',      user_agent)
        req.add_header('Referer',         referer)
        req.add_header('Origin',          target_origin)
        req.add_header('Accept',          '*/*')
        req.add_header('Accept-Language', 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7')
        req.add_header('Cache-Control',   'no-cache')
        req.add_header('Pragma',          'no-cache')
        req.add_header('Connection',      'keep-alive')

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                content_type = resp.headers.get('Content-Type', 'application/octet-stream')
                body         = resp.read()

            # Kalau m3u8 — rewrite semua segment URL lewat proxy ini juga
            is_m3u8 = (
                '.m3u8' in target.lower() or
                'mpegurl' in content_type.lower() or
                'x-mpegurl' in content_type.lower()
            )

            # Kalau mpd — rewrite semua URL segment lewat proxy
            is_mpd = (
                '.mpd' in target.lower() or
                'dash+xml' in content_type.lower() or
                'application/dash' in content_type.lower()
            )

            if is_m3u8:
                body = self._rewrite_m3u8(body, target, referer, ua)
                content_type = 'application/vnd.apple.mpegurl'
            elif is_mpd:
                body = self._rewrite_mpd(body, target, referer, ua)
                content_type = 'application/dash+xml'

            self.send_response(200)
            self.send_header('Content-Type',                content_type)
            self.send_header('Content-Length',              str(len(body)))
            self.send_header('Cache-Control',               'no-cache, no-store')
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        except urllib.error.HTTPError as e:
            self._error(e.code, f'Upstream HTTP error: {e.code} {e.reason}')
        except urllib.error.URLError as e:
            self._error(502, f'URL error: {e.reason}')
        except Exception as e:
            self._error(500, f'Proxy error: {str(e)}')

    def _rewrite_m3u8(self, body: bytes, base_url: str, referer: str, ua: str = None) -> bytes:
        """Rewrite semua URL segmen dalam m3u8 supaya lewat proxy ini."""
        text     = body.decode('utf-8', errors='replace')
        base     = base_url[:base_url.rfind('/') + 1]
        out      = []
        ref_part = urllib.parse.quote(referer, safe='') if referer else ''
        ua_part  = urllib.parse.quote(ua, safe='') if ua else ''

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                out.append(line)
                continue

            # Jadikan absolute URL
            if stripped.startswith('http://') or stripped.startswith('https://'):
                abs_url = stripped
            else:
                abs_url = base + stripped

            # Rewrite lewat proxy
            encoded = urllib.parse.quote(abs_url, safe='')
            proxied = f'/api/proxy?url={encoded}'
            if ref_part:
                proxied += f'&ref={ref_part}'
            if ua_part:
                proxied += f'&ua={ua_part}'
            out.append(proxied)

        return '\n'.join(out).encode('utf-8')

    def _rewrite_mpd(self, body: bytes, base_url: str, referer: str, ua: str = None) -> bytes:
        """
        Rewrite URL segment dalam MPD (DASH) supaya semua segmen lewat proxy.

        Strategi:
        1. <BaseURL> absolut/relatif  → proxy langsung
        2. SegmentTemplate media= / initialization= yang mengandung template
           variable ($Number$, $Time$, $RepresentationID$, dll) → TIDAK bisa
           di-proxy di sini karena variabelnya belum di-resolve.
           Solusi: inject <BaseURL> per-Representation supaya dash.js memakai
           base proxy, sehingga segmen hasil expand template otomatis lewat proxy.
        3. sourceURL= (SegmentBase/SegmentList) tanpa template var → proxy langsung.
        4. Inject xml:base ke elemen <MPD> agar base URL diketahui dash.js.
        """
        text    = body.decode('utf-8', errors='replace')
        base    = base_url[:base_url.rfind('/') + 1]
        ref_enc = urllib.parse.quote(referer, safe='') if referer else ''
        ua_enc  = urllib.parse.quote(ua, safe='') if ua else ''
        PROXY_BASE = 'https://proxy-server.vidiraplay.biz.id'

        def make_proxy(url):
            enc = urllib.parse.quote(url, safe='')
            p   = f'{PROXY_BASE}/api/proxy?url={enc}'
            if ref_enc:
                p += f'&ref={ref_enc}'
            if ua_enc:
                p += f'&ua={ua_enc}'
            return p

        def to_absolute(url):
            if url.startswith('http://') or url.startswith('https://'):
                return url
            if url.startswith('//'):
                scheme = base_url.split('://')[0]
                return scheme + ':' + url
            # Relative path — gabung dengan base direktori MPD
            if url.startswith('/'):
                parsed_base = urllib.parse.urlparse(base_url)
                return f"{parsed_base.scheme}://{parsed_base.netloc}{url}"
            return base + url

        # ── 1. Rewrite <BaseURL> ────────────────────────────────────────────────
        def rewrite_baseurl(m):
            url = m.group(1).strip()
            if not url:
                return m.group(0)
            abs_url = to_absolute(url)
            if abs_url.startswith('http://') or abs_url.startswith('https://'):
                proxied = make_proxy(abs_url)
                # Pastikan trailing slash ada agar template $Number$ bisa digabung
                if not proxied.endswith('/') and '?' not in proxied.split('/')[-1]:
                    proxied += '/'
                return f'<BaseURL>{proxied}</BaseURL>'
            return m.group(0)

        text = re.sub(r'<BaseURL>(.*?)<\/BaseURL>', rewrite_baseurl, text, flags=re.DOTALL)

        # ── 2. Inject <BaseURL> proxy ke setiap <Representation> yang punya
        #        SegmentTemplate dengan template variable ─────────────────────────
        #   Pendekatan: kalau MPD tidak punya <BaseURL> di level Period/AdaptationSet
        #   (yaitu semua segmen pakai URL absolut dari template), kita perlu
        #   intercept di level berbeda.
        #   Trik terbaik: rewrite atribut media= dan initialization= dengan
        #   mengganti bagian base URL-nya menjadi proxy, tapi biarkan template var.
        #
        #   Contoh:
        #     media="https://cdn.example.com/seg$Number$.m4s"
        #   →  media="https://proxy-server.../api/proxy?url=https%3A%2F%2Fcdn.example.com%2Fseg$Number$.m4s"
        #
        #   dash.js akan expand $Number$ SETELAH substituting template, sehingga
        #   URL akhirnya: https://proxy.../api/proxy?url=https%3A%2F%2Fcdn...seg123.m4s
        #   yang benar.

        def rewrite_template_attr(m):
            attr = m.group(1)   # "media" atau "initialization"
            val  = m.group(2)
            # Jadikan absolut dulu
            abs_val = to_absolute(val)
            if not (abs_val.startswith('http://') or abs_val.startswith('https://')):
                return m.group(0)
            # Pisahkan bagian template variable ($Number$, $Time$, $RepresentationID$, dll)
            # dari bagian URL biasa. Bagian URL di-encode, template var dibiarkan literal
            # sehingga dash.js bisa expand sebelum fetch.
            parts = re.split(r'(\$[^$]+\$)', abs_val)
            encoded_parts = []
            for part in parts:
                if part.startswith('$') and part.endswith('$'):
                    # Template variable — biarkan literal agar dash.js expand
                    encoded_parts.append(part)
                else:
                    # URL biasa — encode
                    encoded_parts.append(urllib.parse.quote(part, safe=''))
            encoded_val = ''.join(encoded_parts)
            proxy_url = f'{PROXY_BASE}/api/proxy?url={encoded_val}'
            if ref_enc:
                proxy_url += f'&ref={ref_enc}'
            if ua_enc:
                proxy_url += f'&ua={ua_enc}'
            return f'{attr}="{proxy_url}"'

        # Rewrite media= dan initialization= di SegmentTemplate
        text = re.sub(
            r'((?:media|initialization))="([^"]+)"',
            rewrite_template_attr,
            text
        )

        # ── 3. sourceURL= (SegmentBase/SegmentList tanpa template var) ──────────
        def rewrite_segment_url_attr(m):
            attr = m.group(1)
            val  = m.group(2)
            abs_url = to_absolute(val)
            if abs_url.startswith('http://') or abs_url.startswith('https://'):
                return f'{attr}="{make_proxy(abs_url)}"'
            return m.group(0)

        text = re.sub(r'(sourceURL)="([^"]+)"', rewrite_segment_url_attr, text)

        return text.encode('utf-8')

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def _error(self, code: int, msg: str):
        body = msg.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type',   'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # Suppress default logging
