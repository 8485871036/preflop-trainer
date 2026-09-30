// Trainer (app.html) ke preflop charts se postflop spots banao -> postflop_spots.json
// Har spot: kaun IP/OOP, dono ki range (main + borderline), pot aur effective stack (BB me).
// Run: node tools/postflop/export_spots.js
const fs = require("fs");
const path = require("path");
const ROOT = path.join(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app.html"), "utf8");
const a = src.indexOf("const RG = {");
const b = src.indexOf("const SPOTS = [];");
eval(src.slice(a, b) + ";globalThis.__D = {RG, OPEN_SIZE, TB_SIZE};");
const {RG, OPEN_SIZE, TB_SIZE} = globalThis.__D;

const POSTFLOP_ORDER = ["SB", "BB", "UTG", "MP", "CO", "BTN"];
const grp = p => (p === "UTG" || p === "MP") ? "EP" : p;       // BB/SB charts EP ko ek saath rakhte hain
const spots = {};
const r = (key) => ({main: RG[key] || "", border: RG[key + "_b"] || RG[key + "_c"] ? (RG[key + "_b"] || "") : ""});

function add(id, p1, range1, p2, range2, pot, eff) {
  if (!range1.main || !range2.main) { console.warn("skip (range missing)", id); return; }
  const ip = POSTFLOP_ORDER.indexOf(p1) > POSTFLOP_ORDER.indexOf(p2);
  spots[id] = {
    ip: ip ? p1 : p2, oop: ip ? p2 : p1,
    ip_range: ip ? range1 : range2, oop_range: ip ? range2 : range1,
    pot: +pot.toFixed(2), eff: +eff.toFixed(2),
  };
}

// --- Single-raised pots: opener vs BB call ---
for (const op of ["UTG", "MP", "CO", "BTN", "SB"]) {
  const o = OPEN_SIZE[op];
  const dead = op === "SB" ? 0 : 0.5;                         // SB fold kiya to uska 0.5 pot me
  add(`srp_${op}_BB`, op, r("open_" + op), "BB",
      {main: RG[`bb_${grp(op)}_call`] || "", border: RG[`bb_${grp(op)}_call_b`] || ""},
      2 * o + dead, 100 - o);
}

// --- 3-bet pots: opener call karta hai 3-bettor ke khilaf ---
const LATER = {UTG: ["MP", "CO", "BTN"], MP: ["CO", "BTN"], CO: ["BTN"], BTN: []};
for (const op of ["UTG", "MP", "CO", "BTN", "SB"]) {
  const threeBettors = (LATER[op] || []).concat(op === "SB" ? ["BB"] : ["SB", "BB"]);
  for (const tb of threeBettors) {
    const size = (TB_SIZE[op] || {})[tb];
    if (!size) continue;
    let tbKey;
    if (tb === "BB") tbKey = op === "SB" ? "bb_SB_3b" : `bb_${grp(op)}_3b`;
    else if (tb === "SB") tbKey = `tb_SB_${grp(op)}`;
    else tbKey = `tb_${tb}_${op}`;
    const blind = tb === "SB" || tb === "BB";
    const callKey = op === "SB" ? "fb_SB_bb_c" : `fb_${op}_${blind ? "bl" : "ip"}_c`;
    const opCall = {main: RG[callKey] || "", border: ""};
    const tbRange = {main: RG[tbKey] || "", border: RG[tbKey + "_b"] || ""};
    const dead = (tb !== "SB" && op !== "SB") ? 0.5 : 0;       // SB fold kiya to 0.5
    const deadBB = (tb !== "BB" && op !== "BB") ? 1 : 0;       // BB fold kiya to 1
    add(`3bp_${op}_${tb}`, op, opCall, tb, tbRange, 2 * size + dead + deadBB, 100 - size);
  }
}
fs.writeFileSync(path.join(__dirname, "postflop_spots.json"), JSON.stringify(spots, null, 1));
console.log(Object.keys(spots).length, "spots:", Object.keys(spots).join(" "));
