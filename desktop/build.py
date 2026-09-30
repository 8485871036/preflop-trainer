"""
Builds a standalone PreflopTrainer.exe (PyQt6 + QWebEngineView wrapper around
the app's own index.html) with PyInstaller.

Requires: pip install PyQt6 PyQt6-WebEngine pyinstaller pywin32 opencv-python numpy Pillow pytesseract
Run: python desktop/build.py
Output: desktop/dist/PreflopTrainer.exe
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"


def main():
    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--name", "PreflopTrainer",
        "--onefile", "--windowed", "--noconfirm",
        "--icon", str(DESKTOP / "app.ico"),
        "--add-data", f"{ROOT / 'index.html'};.",
        "--add-data", f"{ROOT / 'icons'};icons",
        "--add-data", f"{ROOT / 'manifest.webmanifest'};.",
        # verified card-rank templates (repo root wali file pehle padhi jaati hai, ye fallback)
        "--add-data", f"{ROOT / 'rank_templates.json'};.",
        # bet / button amount fonts (OpenHoldem-style, hand history se verified)
        "--add-data", f"{ROOT / 'bet_font.json'};.",
        "--add-data", f"{ROOT / 'btn_font.json'};.",
        # Mirror + GTO servers exe ke andar chalte hain (solver repo se)
        "--paths", str(ROOT),
        "--paths", str(ROOT / "tools"),
        "--paths", str(ROOT / "tools" / "postflop"),
        "--hidden-import", "mirror_server",
        "--hidden-import", "gto_server",
        "--hidden-import", "hh_review",           # /api/review (lazy import)
        "--hidden-import", "rangeutil",
        "--hidden-import", "redstar_hh",          # Red Star live card reader (mirror_server lazy import)
        "--hidden-import", "redstar_mem",         # Red Star memory reader (redstar_hh lazy import)
        "--hidden-import", "hud",                 # always-on-top HUD overlay (main.py --hud se)
        "--distpath", str(DESKTOP / "dist"),
        "--workpath", str(DESKTOP / "build"),
        "--specpath", str(DESKTOP),
        str(DESKTOP / "main.py"),
    ], check=True, cwd=str(ROOT))


if __name__ == "__main__":
    main()
