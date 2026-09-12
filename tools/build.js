/* Wraps app.html (the artifact source) into:
     index.html   — standalone page + PWA hooks, for a browser or a web host
     www/         — the exact payload Capacitor ships inside the APK
   Run: node tools/build.js
*/
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const src = fs.readFileSync(path.join(ROOT, "app.html"), "utf8");

const end = src.indexOf("</style>");
if (end < 0) throw new Error("app.html: no </style> found — cannot split head from body");
const head = src.slice(0, end + "</style>".length);
const body = src.slice(end + "</style>".length);

const DESC = "Six-max preflop trainer built on the BluffTheSpot Preflop Bible: open raise, " +
             "3-bet, 4-bet, 5-bet, squeeze and blind defense, with leak tracking and range memorisation drills.";

/** @param {boolean} sw  register the service worker (web build only — Capacitor serves from a local origin) */
function page(sw) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="${DESC}">
<meta name="theme-color" content="#14514a">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Preflop">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/png" sizes="64x64" href="icons/favicon-64.png">
<link rel="apple-touch-icon" href="icons/icon-192.png">
<style>
:root{color-scheme:light dark}
html,body{margin:0}
body{
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#faf9f7;
  -webkit-tap-highlight-color:transparent;
  padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
}
img{max-width:100%}
[hidden]{display:none!important}
</style>
${head}
</head>
<body>
${body}
${sw ? `<script>
if ("serviceWorker" in navigator && location.protocol !== "file:") {
  addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}
</script>
` : ""}</body>
</html>
`;
}

fs.writeFileSync(path.join(ROOT, "index.html"), page(true));

/* www/ = what gets bundled into the APK */
const WWW = path.join(ROOT, "www");
fs.rmSync(WWW, { recursive: true, force: true });
fs.mkdirSync(path.join(WWW, "icons"), { recursive: true });
fs.writeFileSync(path.join(WWW, "index.html"), page(false));
fs.copyFileSync(path.join(ROOT, "manifest.webmanifest"), path.join(WWW, "manifest.webmanifest"));
for (const f of fs.readdirSync(path.join(ROOT, "icons"))) {
  fs.copyFileSync(path.join(ROOT, "icons", f), path.join(WWW, "icons", f));
}

const kb = f => (fs.statSync(f).size / 1024).toFixed(1) + " KB";
console.log("index.html      ", kb(path.join(ROOT, "index.html")));
console.log("www/index.html  ", kb(path.join(WWW, "index.html")));
console.log("www/icons       ", fs.readdirSync(path.join(WWW, "icons")).length + " files");
