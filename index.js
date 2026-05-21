const express = require('express');
const cors = require('cors');
const app = express();
app.use(cors());

app.get('/api/tes', (req, res) => {
    res.json({ pesan: "Halo dari Node.js!" });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Server Node.js jalan di port ${PORT}`));
