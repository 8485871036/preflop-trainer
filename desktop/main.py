"""
Preflop Trainer desktop wrapper.

Loads the app's own index.html (the same file served on the web / bundled
into the Android app) inside a native Qt window via QWebEngineView, instead
of re-implementing the trainer's HTML/CSS/JS logic in PyQt6 widgets.
"""
import os
import socket
import sys
import traceback
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QIcon
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMainWindow


def resource_path(relative: str) -> Path:
    # Frozen (PyInstaller) build bundles assets next to the exe; in dev mode
    # they're the repo's own root files (one level up from this script).
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def repo_root() -> Path:
    # Solver + GTO cache repo me hi rehte hain (exe me bundle nahi, 200MB+).
    # Exe desktop/dist/ me hota hai, isliye do level upar repo root.
    if os.environ.get("PREFLOP_ROOT"):
        return Path(os.environ["PREFLOP_ROOT"])
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent.parent
    return Path(__file__).resolve().parent.parent


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_servers():
    """Mirror (8676) + GTO (8675) server isi process me background threads me chalao.
    Koi pehle se chal raha ho to use hi use karo. Fail ho to app phir bhi khule."""
    root = repo_root()
    os.environ.setdefault("PREFLOP_ROOT", str(root))
    sys.path[:0] = [str(root), str(root / "tools" / "postflop")]
    started = []
    for name in ("mirror_server", "gto_server"):
        try:
            mod = __import__(name)
            if port_busy(mod.PORT):
                continue
            if mod.start_background() is not None:
                started.append(name)
        except Exception:
            log = Path(sys.executable if getattr(sys, "frozen", False) else __file__).with_name("PreflopTrainer.log")
            try:
                with open(log, "a", encoding="utf-8") as f:
                    f.write(f"--- {name} start failed ---\n{traceback.format_exc()}\n")
            except OSError:
                pass
    return started


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Preflop Trainer")
        self.resize(1280, 900)

        icon_path = resource_path("icons/icon-192.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.view = QWebEngineView()
        # Allow the local (file://) app to call the local GTO/AI server at http://127.0.0.1.
        # Without this, Qt WebEngine blocks fetch() from local content to remote URLs.
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.view.load(QUrl.fromLocalFile(str(resource_path("index.html"))))
        self.setCentralWidget(self.view)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Preflop Trainer")
    start_servers()          # QApplication ke baad — Qt ka DPI mode pehle set ho
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
