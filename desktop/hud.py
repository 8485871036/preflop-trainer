"""
Preflop HUD — chhota always-on-top overlay jo poker client (Red Star) ke upar action (bet / check /
call / raise / fold) + equity + pot odds dikhata hai. Har khuli table ka apna alag HUD, usi table
ki window pe chipka hua (table hilao to HUD saath chalta hai; grip strip se HUD ki jagah badlo).
App ka apna index.html?hwnd=<table window>#hud load karta hai, frameless, transparent, draggable.

Standalone:  python desktop/hud.py
Exe se:      PreflopTrainer.exe --hud
"""
import json
import sys
import urllib.request

from PyQt6.QtCore import QObject, QPoint, Qt, QTimer, QUrl, QUrlQuery
from PyQt6.QtGui import QColor
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QFrame, QMainWindow, QVBoxLayout, QWidget

from common import remote_gto, resource_path, start_servers

try:
    import win32gui
except Exception:  # pywin32 na ho to HUD table ke saath nahi chalega (screen ke kone me rahega)
    win32gui = None

TABLES_URL = "http://127.0.0.1:8676/api/tables"
DEFAULT_OFFSET = QPoint(12, 44)   # table window ke top-left se (title bar ke neeche)


class GripBar(QFrame):
    """Frameless window ka drag handle (upar wali patli strip)."""

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._drag = None
        self.setFixedHeight(6)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setStyleSheet("background:rgba(255,255,255,.10);")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self._win.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self._win.dragged()

    @property
    def dragging(self):
        return self._drag is not None


class HudWindow(QMainWindow):
    def __init__(self, table=None):
        super().__init__()
        self.setWindowTitle("Preflop HUD")
        self.resize(200, 54)    # action + equity + pot odds
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.offset = QPoint(DEFAULT_OFFSET)   # table window ke top-left se HUD kitna door
        self.anchor = None                     # table window ka aakhri top-left

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.grip = GripBar(self)
        lay.addWidget(self.grip)

        self.view = QWebEngineView()
        self.view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        lay.addWidget(self.view, 1)

        url = QUrl.fromLocalFile(str(resource_path("index.html")))
        q = QUrlQuery()
        if remote_gto():
            q.addQueryItem("gto", remote_gto())
        if table:                                # is HUD ki apni table (window handle)
            q.addQueryItem("hwnd", str(int(table)))
        if not q.isEmpty():
            url.setQuery(q)
        url.setFragment("hud")
        self.view.load(url)

        # default position: screen ke top-right corner (table mile to manager wahan le jaata hai)
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.width() - 24, scr.top() + 24)

    def dragged(self):
        """User ne HUD khiskaya — table ke hisaab se nayi jagah yaad rakho."""
        if self.anchor is not None:
            self.offset = self.pos() - self.anchor

    def follow(self, top_left):
        self.anchor = top_left
        if not self.grip.dragging:
            self.move(top_left + self.offset)


class HudManager(QObject):
    """Har khuli poker table ke liye ek HUD: banata hai, table ke saath chalata hai, table band to hatata hai."""

    def __init__(self):
        super().__init__()
        self.huds = {}                      # hwnd -> HudWindow
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync)
        self.timer.start(500)

    def show(self):                         # main.py `make_hud()` ke baad `.show()` bulata hai
        self.sync()

    def _tables(self):
        try:
            with urllib.request.urlopen(TABLES_URL, timeout=0.4) as r:
                return json.load(r).get("tables") or []
        except Exception:
            return None                     # server abhi nahi utha / busy — jo hai waisa rehne do

    def sync(self):
        tables = self._tables()
        if tables is None:
            return
        scale = QApplication.primaryScreen().devicePixelRatio() or 1.0
        live = set()
        for t in tables:
            hwnd, title = t.get("hwnd"), t.get("title")
            if not hwnd or not title:
                continue
            live.add(hwnd)
            hud = self.huds.get(hwnd)
            if hud is None:
                hud = self.huds[hwnd] = HudWindow(hwnd)
            if win32gui:
                try:
                    shown = win32gui.IsWindow(hwnd) and not win32gui.IsIconic(hwnd)
                    rect = win32gui.GetWindowRect(hwnd) if shown else None
                except Exception:
                    rect = None
                if rect is None:
                    hud.hide()              # table minimize hai — HUD bhi chhupa do
                    continue
                hud.follow(QPoint(int(rect[0] / scale), int(rect[1] / scale)))
            if not hud.isVisible():
                hud.show()
        for hwnd in [h for h in self.huds if h not in live]:
            self.huds.pop(hwnd).close()


def make_hud():
    return HudManager()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    start_servers()
    w = make_hud()
    w.show()
    sys.exit(app.exec())
