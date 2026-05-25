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
        Rewrite semua URL segment dalam MPD (DASH) supaya lewat proxy ini.
        MPD pakai format XML — kita rewrite atribut media= dan initialization=
        di dalam SegmentTemplate, serta BaseURL.
        """
        text     = body.decode('utf-8', errors='replace')
        base     = base_url[:base_url.rfind('/') + 1]
        ref_enc  = urllib.parse.quote(referer, safe='') if referer else ''
        ua_enc   = urllib.parse.quote(ua, safe='') if ua else ''

        def make_proxy(url):
            # Jadikan absolute dulu
            if url.startswith('http://') or url.startswith('https://'):
                abs_url = url
            else:
                abs_url = base + url
            enc = urllib.parse.quote(abs_url, safe='')
            p   = f'/api/proxy?url={enc}'
            if ref_enc:
                p += f'&ref={ref_enc}'
            if ua_enc:
                p += f'&ua={ua_enc}'
            return p

        # Rewrite BaseURL tag
        def rewrite_baseurl(m):
            url = m.group(1).strip()
            if url.startswith('http://') or url.startswith('https://'):
                abs_url = url
            else:
                abs_url = base + url
            return f'<BaseURL>{make_proxy(abs_url)}</BaseURL>'

        text = re.sub(r'<BaseURL>(.*?)<\/BaseURL>', rewrite_baseurl, text, flags=re.DOTALL)

        # Rewrite media= dan initialization= di SegmentTemplate
        def rewrite_attr(m):
            attr = m.group(1)   # 'media' atau 'initialization'
            val  = m.group(2)   # nilai atributnya
            # Kalau sudah absolute, proxy langsung
            # Kalau relative (tidak ada http), jadikan absolute dulu
            if val.startswith('http://') or val.startswith('https://'):
                proxied = make_proxy(val)
            else:
                # Relative — base + val, tapi jangan encode $Time$ dan $RepresentationID$
                abs_url = base + val
                proxied = make_proxy(abs_url)
            return f'{attr}="{proxied}"'

        text = re.sub(r'(media|initialization)="([^"]+)"', rewrite_attr, text)

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
