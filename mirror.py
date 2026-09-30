import tkinter as tk
from PIL import Image, ImageTk
import win32gui
import win32ui
import win32con
import win32process
import subprocess
import threading
import time
import ctypes

# ============================================
#  YAHAN APNI EXE KA PATH DAALO 👇
# ============================================
APP_PATH = r"C:\Windows\System32\notepad.exe"
# ============================================

# High-DPI screen ke liye zaroori
ctypes.windll.shcore.SetProcessDpiAwareness(1)


class AppMirror:
    def __init__(self, root, exe_path):
        self.root = root
        self.exe_path = exe_path
        self.hwnd = None
        self.running = True

        self.root.title("App Mirror")
        self.root.geometry("900x650")
        self.root.configure(bg="black")

        self.canvas = tk.Label(root, bg="black")
        self.canvas.pack(fill="both", expand=True)

        self.process = subprocess.Popen(exe_path)
        threading.Thread(target=self.find_window, daemon=True).start()

        self.root.after(100, self.update_frame)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def find_window(self):
        """EXE ka main window dhundho"""
        for _ in range(100):  # 10 sec tak try karega
            time.sleep(0.1)
            try:
                def callback(hwnd, hwnds):
                    if win32gui.IsWindowVisible(hwnd):
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        if pid == self.process.pid:
                            hwnds.append(hwnd)
                    return True

                hwnds = []
                win32gui.EnumWindows(callback, hwnds)
                if hwnds:
                    # Sabse bada visible window lo (dialog na pakda jaye)
                    self.hwnd = max(
                        hwnds,
                        key=lambda h: win32gui.GetWindowRect(h)[2] * win32gui.GetWindowRect(h)[3]
                    )
                    return
            except Exception:
                pass

    def capture_window(self):
        """Window ka screenshot lo (poori window, title bar ke saath)"""
        if not self.hwnd:
            return None
        try:
            # Poori window ka rect (title bar samet)
            left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
            w, h = right - left, bottom - top
            if w <= 0 or h <= 0:
                return None

            hwndDC = win32gui.GetWindowDC(self.hwnd)
            mfcDC = win32ui.CreateDCFromHandle(hwndDC)
            saveDC = mfcDC.CreateCompatibleDC()
            saveBitMap = win32ui.CreateBitmap()
            saveBitMap.CreateCompatibleBitmap(mfcDC, w, h)
            saveDC.SelectObject(saveBitMap)

            # flag 2 = PW_RENDERFULLCONTENT (poori window)
            ctypes.windll.user32.PrintWindow(self.hwnd, saveDC.GetSafeHdc(), 2)

            bmpinfo = saveBitMap.GetInfo()
            bmpstr = saveBitMap.GetBitmapBits(True)
            img = Image.frombuffer(
                'RGB',
                (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                bmpstr, 'raw', 'BGRX', 0, 1
            )

            win32gui.DeleteObject(saveBitMap.GetHandle())
            saveDC.DeleteDC()
            mfcDC.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hwndDC)
            return img
        except Exception:
            return None

    def update_frame(self):
        """Har 100ms mein naya frame dikhao"""
        if not self.running:
            return
        img = self.capture_window()
        if img:
            cw = self.root.winfo_width()
            ch = self.root.winfo_height()
            if cw > 1 and ch > 1:
                # Aspect ratio maintain karte hue fit karo
                iw, ih = img.size
                scale = min(cw / iw, ch / ih)
                nw, nh = int(iw * scale), int(ih * scale)
                img = img.resize((nw, nh), Image.LANCZOS)
                self.tk_img = ImageTk.PhotoImage(img)
                self.canvas.config(image=self.tk_img)

        self.root.after(100, self.update_frame)

    def on_close(self):
        self.running = False
        try:
            self.process.terminate()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    AppMirror(root, APP_PATH)
    root.mainloop()
