"""
Preflop Trainer desktop wrapper.

Loads the app's own index.html (the same file served on the web / bundled
into the Android app) inside a native Qt window via QWebEngineView, instead
of re-implementing the trainer's HTML/CSS/JS logic in PyQt6 widgets.
"""
import sys

from common import remote_gto, resource_path, start_servers
from PyQt6.QtCore import QUrl, QUrlQuery
from PyQt6.QtGui import QIcon
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMainWindow


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Preflop Trainer")
        self.resize(920, 600)
        self.setMinimumSize(420, 320)

        icon_path = resource_path("icons/icon-192.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.view = QWebEngineView()
        # Allow the local (file://) app to call the local GTO/AI server at http://127.0.0.1.
        # Without this, Qt WebEngine blocks fetch() from local content to remote URLs.
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        url = QUrl.fromLocalFile(str(resource_path("index.html")))
        if remote_gto():                 # solver remote pe hai to app ko wahi point karo
            q = QUrlQuery()
            q.addQueryItem("gto", remote_gto())
            url.setQuery(q)
        self.view.load(url)
        self.setCentralWidget(self.view)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Preflop Trainer")
    start_servers()          # QApplication ke baad — Qt ka DPI mode pehle set ho
    window = MainWindow()
    window.show()
    if "--no-hud" not in sys.argv:  # har table pe chhota always-on-top overlay — default on (double-click se bhi)
        import hud
        hud_win = hud.make_hud()   # reference rakho — warna window turant garbage-collect ho jaati hai
        hud_win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
