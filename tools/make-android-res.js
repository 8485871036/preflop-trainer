/* Writes the Android launcher icons and splash art straight into the native project.
   Reuses the drawing code from make-icons.js. Run: node tools/make-android-res.js */
const fs = require("fs");
const path = require("path");
const { draw, drawGlyphOnly, drawSplash } = require("./icon-lib.js");

const RES = path.join(__dirname, "..", "android", "app", "src", "main", "res");
if (!fs.existsSync(RES)) {
  console.error("No android project yet — run `npx cap add android` first.");
  process.exit(1);
}
const w = (rel, buf) => {
  const p = path.join(RES, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, buf);
};

/* Legacy launcher icons: 48dp at each density */
const DENS = { mdpi: 1, hdpi: 1.5, xhdpi: 2, xxhdpi: 3, xxxhdpi: 4 };
for (const [d, s] of Object.entries(DENS)) {
  const px = Math.round(48 * s);
  w(`mipmap-${d}/ic_launcher.png`,       draw(px, { radius: 0.22, glyph: 0.62 }));
  w(`mipmap-${d}/ic_launcher_round.png`, draw(px, { radius: 0.50, glyph: 0.60 }));
  /* Adaptive foreground: 108dp canvas, glyph must sit inside the 72dp safe zone */
  w(`mipmap-${d}/ic_launcher_foreground.png`, drawGlyphOnly(Math.round(108 * s), 0.44));
}

/* Adaptive icon background is a flat colour behind that foreground */
w("values/ic_launcher_background.xml",
`<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="ic_launcher_background">#0F433D</color>
</resources>
`);

/* Splash: Capacitor's template expects drawable/splash.png plus per-orientation copies */
const SPLASH = {
  "drawable":                 [480, 320],
  "drawable-port-mdpi":       [320, 480],
  "drawable-port-hdpi":       [480, 800],
  "drawable-port-xhdpi":      [720, 1280],
  "drawable-port-xxhdpi":     [960, 1600],
  "drawable-port-xxxhdpi":    [1280, 1920],
  "drawable-land-mdpi":       [480, 320],
  "drawable-land-hdpi":       [800, 480],
  "drawable-land-xhdpi":      [1280, 720],
  "drawable-land-xxhdpi":     [1600, 960],
  "drawable-land-xxxhdpi":    [1920, 1280]
};
for (const [dir, [pw, ph]] of Object.entries(SPLASH)) {
  w(`${dir}/splash.png`, drawSplash(pw, ph));
}

console.log("launcher icons  ", Object.keys(DENS).length + " densities × 3 variants");
console.log("splash screens  ", Object.keys(SPLASH).length + " sizes");
console.log("adaptive bg      #0F433D");
