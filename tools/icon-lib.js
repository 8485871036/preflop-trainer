/* Dependency-free PNG writer plus the spade artwork the app is branded with. */
const zlib = require("zlib");

/* ---------- minimal PNG encoder ---------- */
const CRC_T = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();
function crc32(buf) {
  let c = -1;
  for (let i = 0; i < buf.length; i++) c = CRC_T[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}
function chunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type, "latin1"), data]);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}
function png(width, height, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0); ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 6;                    // 8-bit RGBA
  const raw = Buffer.alloc((width * 4 + 1) * height);
  let o = 0;
  for (let y = 0; y < height; y++) {
    raw[o++] = 0;
    rgba.copy(raw, o, y * width * 4, (y + 1) * width * 4);
    o += width * 4;
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0))
  ]);
}

/* ---------- palette ---------- */
const FELT_A  = [0x1a, 0x60, 0x57];
const FELT_B  = [0x0a, 0x2d, 0x29];
const BRASS   = [0xd8, 0xb1, 0x35];
const BRASS_D = [0xa8, 0x85, 0x1c];

/* ---------- shapes, all in 0..1 space ---------- */
function inCircle(x, y, cx, cy, r) { const dx = x - cx, dy = y - cy; return dx * dx + dy * dy <= r * r; }
function inTri(x, y, a, b, c) {
  const s = (p, q, r) => (p[0] - r[0]) * (q[1] - r[1]) - (q[0] - r[0]) * (p[1] - r[1]);
  const d1 = s([x, y], a, b), d2 = s([x, y], b, c), d3 = s([x, y], c, a);
  return !((d1 < 0 || d2 < 0 || d3 < 0) && (d1 > 0 || d2 > 0 || d3 > 0));
}
/** Spade: triangle pointing up, two lobes below it, a flared stem. `s` scales the glyph. */
function inSpade(x, y, s) {
  const X = 0.5 + (x - 0.5) / s, Y = 0.5 + (y - 0.5) / s;
  if (X < 0 || X > 1 || Y < 0 || Y > 1) return false;
  if (inTri(X, Y, [0.5, 0.11], [0.11, 0.63], [0.89, 0.63])) return true;
  if (inCircle(X, Y, 0.315, 0.575, 0.215)) return true;
  if (inCircle(X, Y, 0.685, 0.575, 0.215)) return true;
  if (Y >= 0.63 && Y <= 0.91) {
    const t = (Y - 0.63) / 0.28;
    if (Math.abs(X - 0.5) <= 0.05 + 0.165 * t * t) return true;
  }
  return false;
}
function inRounded(x, y, r) {
  const cx = Math.min(x, 1 - x), cy = Math.min(y, 1 - y);
  if (cx >= r || cy >= r) return true;
  return inCircle(x, y, x < 0.5 ? r : 1 - r, y < 0.5 ? r : 1 - r, r);
}
const feltAt = (fx, fy) => {
  const g = Math.min(1, Math.max(0, fx * 0.55 + fy * 0.75));
  return [0, 1, 2].map(i => Math.round(FELT_A[i] + (FELT_B[i] - FELT_A[i]) * g));
};
const brassAt = fy => {
  const g = Math.min(1, Math.max(0, (fy - 0.2) / 0.6));
  return [0, 1, 2].map(i => Math.round(BRASS[i] + (BRASS_D[i] - BRASS[i]) * g));
};

const SS = 4;   // supersampling

/** Full icon: spade on a rounded felt tile. */
function draw(size, { radius = 0.22, glyph = 0.62, bleed = false } = {}) {
  const buf = Buffer.alloc(size * size * 4);
  for (let py = 0; py < size; py++) for (let px = 0; px < size; px++) {
    let bg = 0, fg = 0;
    for (let sy = 0; sy < SS; sy++) for (let sx = 0; sx < SS; sx++) {
      const x = (px + (sx + 0.5) / SS) / size, y = (py + (sy + 0.5) / SS) / size;
      if (bleed || inRounded(x, y, radius)) { bg++; if (inSpade(x, y, glyph)) fg++; }
    }
    const n = SS * SS, aBg = bg / n, aFg = fg / n;
    const felt = feltAt(px / size, py / size), brass = brassAt(py / size);
    const o = (py * size + px) * 4;
    for (let i = 0; i < 3; i++) buf[o + i] = Math.round(felt[i] * (1 - aFg) + brass[i] * aFg);
    buf[o + 3] = Math.round(aBg * 255);
  }
  return png(size, size, buf);
}

/** Adaptive-icon foreground: the spade alone on transparency. */
function drawGlyphOnly(size, glyph = 0.44) {
  const buf = Buffer.alloc(size * size * 4);
  for (let py = 0; py < size; py++) for (let px = 0; px < size; px++) {
    let fg = 0;
    for (let sy = 0; sy < SS; sy++) for (let sx = 0; sx < SS; sx++) {
      const x = (px + (sx + 0.5) / SS) / size, y = (py + (sy + 0.5) / SS) / size;
      if (inSpade(x, y, glyph)) fg++;
    }
    const a = fg / (SS * SS);
    const brass = brassAt(py / size);
    const o = (py * size + px) * 4;
    for (let i = 0; i < 3; i++) buf[o + i] = brass[i];
    buf[o + 3] = Math.round(a * 255);
  }
  return png(size, size, buf);
}

/** Splash: felt field with a centred spade sized to the shorter edge. */
function drawSplash(width, height) {
  const buf = Buffer.alloc(width * height * 4);
  const min = Math.min(width, height), gs = min * 0.30;
  const gx = (width - gs) / 2, gy = (height - gs) / 2;
  for (let py = 0; py < height; py++) for (let px = 0; px < width; px++) {
    let fg = 0;
    for (let sy = 0; sy < SS; sy++) for (let sx = 0; sx < SS; sx++) {
      const x = (px + (sx + 0.5) / SS - gx) / gs, y = (py + (sy + 0.5) / SS - gy) / gs;
      if (x >= 0 && x <= 1 && y >= 0 && y <= 1 && inSpade(x, y, 1)) fg++;
    }
    const a = fg / (SS * SS);
    const felt = feltAt(px / width, py / height), brass = brassAt(0.5);
    const o = (py * width + px) * 4;
    for (let i = 0; i < 3; i++) buf[o + i] = Math.round(felt[i] * (1 - a) + brass[i] * a);
    buf[o + 3] = 255;
  }
  return png(width, height, buf);
}

module.exports = { png, draw, drawGlyphOnly, drawSplash };
