export default function handler(req, res) {

  // CORS
  res.setHeader(
    "Access-Control-Allow-Origin",
    "*"
  );

  res.setHeader(
    "Access-Control-Allow-Methods",
    "GET"
  );

  res.status(200).json({
    status: "online",
    message: "Halo dari Node.js Vercel",
    creator: "Pembri"
  });

}
