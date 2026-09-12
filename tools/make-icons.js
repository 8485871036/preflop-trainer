/* Web / PWA icons. Run: node tools/make-icons.js */
const fs = require("fs");
const path = require("path");
const { draw } = require("./icon-lib.js");

const out = path.join(__dirname, "..", "icons");
fs.mkdirSync(out, { recursive: true });

const jobs = [
  ["icon-192.png",          192, { radius: 0.22, glyph: 0.62 }],
  ["icon-512.png",          512, { radius: 0.22, glyph: 0.62 }],
  ["icon-maskable-512.png", 512, { radius: 0.50, glyph: 0.46, bleed: true }],
  ["icon-1024.png",        1024, { radius: 0.22, glyph: 0.62 }],
  ["favicon-64.png",         64, { radius: 0.20, glyph: 0.70 }]
];
for (const [name, size, opt] of jobs) {
  const b = draw(size, opt);
  fs.writeFileSync(path.join(out, name), b);
  console.log(name.padEnd(24), size + "x" + size, (b.length / 1024).toFixed(1) + " KB");
}
