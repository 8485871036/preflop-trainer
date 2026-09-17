"""
Preflop Trainer desktop wrapper.

Loads the app's own index.html (the same file served on the web / bundled
into the Android app) inside a native Qt window via QWebEngineView, instead
of re-implementing the trainer's HTML/CSS/JS logic in PyQt6 widgets.
"""
import sys
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QIcon
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMainWindow


def resource_path(relative: str) -> Path:
    # Frozen (PyInstaller) build bundles assets next to the exe; in dev mode
    # they're the repo's own root files (one level up from this script).
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Preflop Trainer")
        self.resize(1280, 900)

        icon_path = resource_path("icons/icon-192.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.view = QWebEngineView()
        self.view.load(QUrl.fromLocalFile(str(resource_path("index.html"))))
        self.setCentralWidget(self.view)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Preflop Trainer")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
