"""
Preflop HUD — chhota always-on-top overlay jo poker client (Red Star) ke upar
preflop action (bada text) dikhata hai. App ka apna index.html#hud load karta
hai (wahi mirror + auto spot-detect + recommendation logic), frameless,
transparent, draggable.

Standalone:  python desktop/hud.py
Exe se:      PreflopTrainer.exe --hud
"""
import sys

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QColor
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QFrame, QMainWindow, QVBoxLayout, QWidget

from common import resource_path, start_servers


class GripBar(QFrame):
    """Frameless window ka drag handle (upar wali patli strip)."""

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._drag = None
        self.setFixedHeight(12)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setStyleSheet("background:rgba(255,255,255,.10);")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self._win.move(e.globalPosition().toPoint() - self._drag)


class HudWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Preflop HUD")
        self.resize(380, 150)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(GripBar(self))

        self.view = QWebEngineView()
        self.view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        lay.addWidget(self.view, 1)

        url = QUrl.fromLocalFile(str(resource_path("index.html")))
        url.setFragment("hud")
        self.view.load(url)

        # default position: screen ke top-right corner (drag se kahin bhi le jao)
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.width() - 24, scr.top() + 24)


def make_hud():
    return HudWindow()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    start_servers()
    w = HudWindow()
    w.show()
    sys.exit(app.exec())
