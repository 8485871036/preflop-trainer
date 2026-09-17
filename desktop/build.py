"""
Builds a standalone PreflopTrainer.exe (PyQt6 + QWebEngineView wrapper around
the app's own index.html) with PyInstaller.

Requires: pip install PyQt6 PyQt6-WebEngine pyinstaller
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
        "--distpath", str(DESKTOP / "dist"),
        "--workpath", str(DESKTOP / "build"),
        "--specpath", str(DESKTOP),
        str(DESKTOP / "main.py"),
    ], check=True, cwd=str(ROOT))


if __name__ == "__main__":
    main()
