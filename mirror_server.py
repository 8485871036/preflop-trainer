
import json
import os
import re
import sys
import threading
import time
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



WINDOW_TITLE = ["NL Hold'em", "NLHP", "Natural8"]
# Process ke naam se bhi match karo (title empty/badle to bhi pakde).
PROCESS_NAMES = ["pokerclient", "ggnet"]
# Lobby windows ke exact titles — inke card-preview / banners ko hero cards padh leta tha.
LOBBY_TITLES = {"natural8", "redstar poker", "red star poker", "ggpoker"}
PORT = int(os.environ.get("MIRROR_PORT", "8676"))
MAX_TABLES = int(os.environ.get("MIRROR_MAX_TABLES", "4"))   # multi-table: ek saath kitni tables

HERO_REGION = (0.43, 0.675, 0.14, 0.085)
USE_OCR = True             # OCR ke liye:  pip install pytesseract Pillow  (+ Tesseract binary)

SEAT_ANCHORS = [
    (0.79, 0.73),  # 0 bottom-right
    (0.50, 0.82),  # 1 bottom-center (HERO)
    (0.20, 0.73),  # 2 bottom-left
    (0.12, 0.35),  # 3 top-left
    (0.50, 0.20),  # 4 top-center
    (0.88, 0.35),  # 5 top-right
]
HERO_SEAT = 1

BET_BOXES = [
    (0.61, 0.62, 0.74, 0.665),   # 0 bottom-right
    (0.44, 0.62, 0.62, 0.665),   # 1 HERO
    (0.23, 0.62, 0.40, 0.665),   # 2 bottom-left
    (0.16, 0.36, 0.33, 0.405),   # 3 top-left
    (0.40, 0.295, 0.56, 0.335),  # 4 top-center ("Pot:" line se upar)
    (0.68, 0.36, 0.84, 0.405),   # 5 top-right
]
# Har seat ka naam/stack plaque — khaali seat pe yahan text nahi hota.
PLAQUE_BOXES = [
    (0.72, 0.71, 0.86, 0.78),    # 0 bottom-right
    (0.43, 0.785, 0.57, 0.855),  # 1 HERO
    (0.14, 0.68, 0.27, 0.75),    # 2 bottom-left
    (0.06, 0.29, 0.19, 0.36),    # 3 top-left
    (0.43, 0.17, 0.57, 0.24),    # 4 top-center
    (0.80, 0.27, 0.94, 0.33),    # 5 top-right
]
# Plaque ke upar ka badge ("SIT OUT" / "FOLD") — sit-out wale position me nahi gine jaate.
BADGE_BOXES = [
    (0.73, 0.675, 0.85, 0.71),
    None,                        # hero hamesha khel raha hai
    (0.15, 0.645, 0.27, 0.68),
    (0.07, 0.235, 0.19, 0.275),
    (0.44, 0.115, 0.56, 0.15),
    (0.81, 0.23, 0.93, 0.265),
]
# Board cards ka area — yahan rang dikhe to flop aa chuka hai (preflop khatam)
BOARD_REGION = (0.30, 0.37, 0.58, 0.50)
# Pot ka text ("Pot: X BB") — board ke UPAR, top-center seat ke bet ke NEECHE.
POT_REGION = (0.30, 0.315, 0.70, 0.375)
# Har seat ka NAAM — plaque ke upar ka hissa (stack neeche hota hai). HERO skip.
# Best-effort: OCR fail ho to names nahi milte (villain auto-select ka fallback dropdown hai).
NAME_REGIONS = [
    (0.72, 0.705, 0.86, 0.745),   # 0 bottom-right
    None,                          # 1 HERO
    (0.14, 0.675, 0.27, 0.715),    # 2 bottom-left
    (0.06, 0.285, 0.19, 0.325),    # 3 top-left
    (0.43, 0.165, 0.57, 0.205),    # 4 top-center
    (0.80, 0.265, 0.94, 0.30),     # 5 top-right
]
# ============================================================

RANKS = "AKQJT98765432"
SUIT_MAP = {
    "s": "s", "h": "h", "d": "d", "c": "c",
    "♠": "s", "♥": "h", "♦": "d", "♣": "c",
    "♤": "s", "♡": "h", "♢": "d", "♧": "c",
}

state = {
    "frame_jpeg": None,
    "window_hwnd": None,
    "window_title": None,
    "manual_hand": None,
    "last_ocr": None,
    "ocr_available": False,
    "windows": [],
    "selected_title": None,
    "pinned": False,
    "blocked": False,
    "position": None,
    "last_img": None,
    "action": None,
    "tables": {},   # title -> {hwnd, frame_jpeg, last_img, blocked, last_ocr, raw, position, action, manual_hand}
    "accuracy": {"total": 0, "ok": 0},   # reading accuracy — OCR to_call vs XML truth (hand khatam hone pe)
}

# ---------------- accuracy stats persistence (restart ke baad bhi 200-hand progress na toote) ----------------
ACC_KEYS = ["accuracy", "card_accuracy", "line_ok_accuracy", "pos_ok_accuracy", "pot_ok_accuracy"]


def _acc_path():
    base = os.environ.get("PREFLOP_ROOT") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "accuracy_stats.json")


def _acc_load():
    try:
        with open(_acc_path(), encoding="utf-8") as f:
            data = json.load(f)
        for k in ACC_KEYS:
            if isinstance(data.get(k), dict):
                state[k] = {"total": int(data[k].get("total", 0)),
                            "ok": int(data[k].get("ok", 0))}
    except Exception:
        pass


def _acc_save():
    try:
        with open(_acc_path(), "w", encoding="utf-8") as f:
            json.dump({k: state.get(k, {}) for k in ACC_KEYS}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _acc_bump(name, ok, total=1):
    acc = state.setdefault(name, {"total": 0, "ok": 0})
    acc["total"] += total
    acc["ok"] += int(ok)
    _acc_save()


_acc_load()

# ---------------- optional deps ----------------
try:
    from PIL import Image, ImageDraw, ImageOps
    HAS_PIL = True
except Exception:
    HAS_PIL = False

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    HAS_TESS = True
except Exception:
    HAS_TESS = False

try:
    import win32gui
    import win32ui
    import win32process
    import win32con
    import win32api
    HAS_WIN32 = True
except Exception:
    HAS_WIN32 = False

try:
    import numpy as _np
    import cv2 as _cv2
    HAS_CV = True
except Exception:
    HAS_CV = False

try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
    import redstar_hh
    HAS_REDSTAR = True
except Exception:
    HAS_REDSTAR = False

# High-DPI screens pe coordinates sahi rakhne ke liye
if HAS_WIN32:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _proc_name(pid, cache):
    """pid -> process name (lowercase, no .exe), cached."""
    if pid in cache:
        return cache[pid]
    name = ""
    try:
        h = win32api.OpenProcess(0x1000 | 0x0400, False, pid)
        path = win32process.GetModuleFileNameEx(h, 0)
        win32api.CloseHandle(h)
        name = path.split("\\")[-1].lower()
    except Exception:
        pass
    cache[pid] = name
    return name


def find_windows(title, proc_names=None):
    """Saari visible windows jinka TITLE ya PROCESS NAME match karta hai.
    Sabse badi pehle; minimized/off-screen skip."""
    if not HAS_WIN32:
        return []
    titles = title if isinstance(title, (list, tuple)) else [title]
    procs = [p.lower() for p in (proc_names or [])]
    found = []
    pcache = {}
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.IsIconic(hwnd):          # minimized window skip karo
            return True
        t = win32gui.GetWindowText(hwnd)
        if t.strip().lower() in LOBBY_TITLES:  # lobby window — table nahi
            return True
        title_ok = bool(t) and any(s.lower() in t.lower() for s in titles)
        proc_ok = False
        if not title_ok and procs:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc_ok = any(p in _proc_name(pid, pcache) for p in procs)
            except Exception:
                pass
        if not title_ok and not proc_ok:
            return True
        try:
            l, tp, r, b = win32gui.GetWindowRect(hwnd)
            if l < -10000 or tp < -10000:    # off-screen / minimized
                return True
        except Exception:
            return True
        found.append((hwnd, t, title_ok))
        return True
    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    # Title se table windows mili hain to sirf process se mili windows (lobby/settings) hatao —
    # warna lobby ke card-preview ko hero cards padh lete hain
    if any(ok for _, _, ok in found):
        found = [f for f in found if f[2]]
    found = [(hwnd, t) for hwnd, t, _ in found]
    found.sort(key=lambda x: win32gui.GetWindowRect(x[0])[2] * win32gui.GetWindowRect(x[0])[3], reverse=True)
    return found


def pick_window(windows):
    """User ne table select kiya ho to wahi, warna sabse badi window."""
    if not windows:
        return None
    if state["selected_title"]:
        for hwnd, t in windows:
            if t == state["selected_title"]:
                return hwnd
    return windows[0][0]


def bring_to_front(hwnd):
    """Table ko ek baar aage lao (ALT trick se Windows ko dhoka dekar)."""
    if not hwnd:
        return
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # ALT press/release — background process ko foreground lene ki ijaazat milti hai
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)   # VK_MENU (Alt) down
        win32gui.SetForegroundWindow(hwnd)
        ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)   # Alt up
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
    except Exception:
        pass


def pin_top(hwnd, on):
    """Table ko hamesha aage rakho (on=True) ya chhodo (on=False)."""
    if not hwnd:
        return
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        pos = win32con.HWND_TOPMOST if on else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(hwnd, pos, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        if on:
            win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _capture_via(hwnd, use_screen):
    """Window ka screenshot. use_screen=True -> screen se BitBlt (GPU apps ke liye)."""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    if use_screen:
        srcDC = win32gui.GetDC(0)      # poori screen ka DC
        srcHwnd = 0
        sx, sy = left, top
    else:
        srcDC = win32gui.GetWindowDC(hwnd)
        srcHwnd = hwnd
        sx, sy = 0, 0
    mfcDC = win32ui.CreateDCFromHandle(srcDC)
    saveDC = mfcDC.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfcDC, w, h)
    saveDC.SelectObject(bmp)
    try:
        if use_screen:
            # SRCCOPY
            ctypes.windll.gdi32.BitBlt(saveDC.GetSafeHdc(), 0, 0, w, h, mfcDC.GetSafeHdc(), sx, sy, 0x00CC0020)
        else:
            ctypes.windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(srcHwnd, srcDC)


def is_capture_blocked(hwnd):
    """App ne window pe WDA_EXCLUDEFROMCAPTURE / WDA_MONITOR lagaya hai to capture
    black ya peeche wali window deta hai — us case me capture mat karo."""
    if not HAS_WIN32 or not hwnd:
        return False
    try:
        aff = ctypes.c_uint(0)
        if ctypes.windll.user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(aff)):
            return aff.value != 0
    except Exception:
        pass
    return False


def _is_black(img):
    try:
        extrema = img.convert("L").getextrema()
        return extrema[1] <= 8   # max brightness 8 ya kam -> effectively black
    except Exception:
        return False


def capture(hwnd):
    """Window ka screenshot. Pehle PrintWindow; black aaye to screen se BitBlt."""
    if not HAS_WIN32 or not hwnd:
        return None
    try:
        img = _capture_via(hwnd, use_screen=False)
        if img and _is_black(img):
            img = _capture_via(hwnd, use_screen=True)
        return img
    except Exception:
        return None


def parse_hand(raw):
    """OCR text -> 'AsKd' style hand string, or None."""
    if not raw:
        return None
    txt = raw
    for k, v in SUIT_MAP.items():
        txt = txt.replace(k, v)
    txt = txt.replace("10", "T").replace("1O", "T").replace("I0", "T")
    txt = txt.lower()
    ranks = [c for c in txt if c in "akqjt98765432"]
    suits = [c for c in txt if c in "shdc"]
    # positional pairing: 1st rank + 1st suit, 2nd rank + 2nd suit
    if len(ranks) >= 2 and len(suits) >= 2:
        c1 = ranks[0].upper() + suits[0]
        c2 = ranks[1].upper() + suits[1]
        if c1 != c2 and c1[0] != c2[0]:
            return c1 + c2
    return None


def crop_hero(img):
    """Hero cards ka region crop karo."""
    if not img:
        return None, None
    w, h = img.size
    x, y, rw, rh = HERO_REGION
    box = (int(x * w), int(y * h), int((x + rw) * w), int((y + rh) * h))
    return img.crop(box), box


def _ocr(text_corner, whitelist, psm="10"):
    """Kisi corner image se ek token OCR karo."""
    if not HAS_TESS:
        return ""
    g = text_corner.convert("L")
    for im in (g, ImageOps.invert(g)):
        for th in (128, 160, 96):
            bw2 = im.point(lambda p: 255 if p > th else 0)
            try:
                t = pytesseract.image_to_string(
                    bw2, config=f"--psm {psm} -c tessedit_char_whitelist={whitelist}")
                t = t.strip()
                if t:
                    return t
            except Exception:
                pass
    return ""


def _suit_from_ocr(text):
    """OCR text se suit letter nikalo."""
    for ch in text:
        low = ch.lower()
        if low == "s" or ch in ("\u2660", "\u2664"):
            return "s"
        if low == "h" or ch in ("\u2665", "\u2661"):
            return "h"
        if low == "d" or ch in ("\u2666", "\u2662"):
            return "d"
        if low == "c" or ch in ("\u2663", "\u2667"):
            return "c"
    return None


def _largest_component(mask):
    """Binary mask me se sabse bada white blob rakho — chhote noise hat jate hain."""
    w, h = mask.size
    mp = mask.load()
    seen = set()
    best = []
    for y in range(h):
        for x in range(w):
            if mp[x, y] and (x, y) not in seen:
                comp = []
                stack = [(x, y)]
                seen.add((x, y))
                while stack:
                    cx, cy = stack.pop()
                    comp.append((cx, cy))
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < w and 0 <= ny < h and mp[nx, ny] and (nx, ny) not in seen:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
                if len(comp) > len(best):
                    best = comp
    out = Image.new("L", (w, h), 0)
    op = out.load()
    for (x, y) in best:
        op[x, y] = 255
    return out


def _card_bbox(half):
    """Hero region ke half me card ka bounding box (colored card body se)."""
    w, h = half.size
    px = half.convert("RGB").load()
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            mx = max(r, g, b)
            mn = min(r, g, b)
            # saturated (red/blue/green body) ya light-gray body = card; dark felt excluded
            if (mx - mn) > 25 or mx > 80:
                mp[x, y] = 255
    mask = _largest_component(mask)
    return mask.getbbox()


def _suit_from_card(card):
  
    w, h = card.size
    px = card.convert("RGB").load()
    counts = {"h": 0, "d": 0, "c": 0, "s": 0}
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            mx = max(r, g, b)
            mn = min(r, g, b)
            if mn > 195:                     # white body
                continue
            if mx < 60:                      # black text (spade)
                counts["s"] += 1
                continue
            if (mx - mn) < 35:               # gray border (not a suit color)
                continue
            if r > g and r > b and (r - g) > 25:
                counts["h"] += 1
            elif g > r and g >= b and (g - r) > 20:
                counts["c"] += 1
            elif b > r and b >= g and (b - r) > 20:
                counts["d"] += 1
            else:
                counts["s"] += 1
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else None


def _card_body_rgb(card):
    """Card ke upar wale hisse ka median color (neeche Rabbit Hunt ka brown hissa ho sakta hai)."""
    a = _np.asarray(card.convert("RGB"), dtype=_np.int32)
    a = a[:max(1, int(a.shape[0] * 0.55))]
    return tuple(int(v) for v in _np.median(a.reshape(-1, 3), axis=0))


def _suit(card):
    """Card ka suit. Colored-body deck (Red Star/Natural8 4-color: gray=s, red=h, blue=d,
    green=c) -> body color se. White-body deck -> rank/suit text ke color se."""
    r, g, b = _card_body_rgb(card)
    if min(r, g, b) > 190:
        return _suit_from_card(card)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 30:
        return "s"
    if r == mx:
        return "h"
    if g == mx and g - b > 15:
        return "c"
    return "d"                                # blue / cyan


def _is_dim_card(card):
    """Hero ke FOLD kiye hue cards: Red Star unhe dhundhla (washed-out) karke dikhata rehta hai
    (hand ke end pe / hover pe). Body ka rang safed ki taraf khisak jaata hai — asli card
    (red 167,53,52 / gray 112,108,115) vs dim (183,182,185 / 185,210,186). Inhe hand padhna
    galat hai: app me cards gayab -> wapas -> naya hand jaisa flicker hota tha."""
    r, g, b = _card_body_rgb(card)
    return 140 <= min(r, g, b) <= 190 and max(r, g, b) - min(r, g, b) < 70


def _is_card_back(card):
    """Opponent ka face-down card. Bot-style pixel-hash library match (100%, scale-invariant);
    library khaali ho to blue+logo heuristic fallback."""
    _load_back_sigs()
    if _BACK_SIGS:
        sig = _back_sig(card)
        return max(float(_np.dot(sig, b)) for b in _BACK_SIGS) >= BACK_SIM_THRESHOLD
    a = _np.asarray(card.convert("RGB"), dtype=_np.int32)
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    r, g, b = _card_body_rgb(card)
    return b > r + 40 and ((R > 170) & (G < 90) & (B < 90)).mean() > 0.02


# ---------------- Card back hash library (OpenHoldem h$ hash jaisa) ----------------
_BACK_SIGS = []
_back_sigs_loaded = False
BACK_SIM_THRESHOLD = 0.90     # cosine similarity — back vs face clear farak


def _load_back_sigs():
    """card_back_library.json se back ke RGB signatures (unit vectors) load karo."""
    global _BACK_SIGS, _back_sigs_loaded
    if _back_sigs_loaded:
        return
    _back_sigs_loaded = True
    for base in (os.environ.get("PREFLOP_ROOT"), getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(__file__))):
        if not base:
            continue
        try:
            with open(os.path.join(base, "card_back_library.json"), encoding="utf-8") as f:
                data = json.load(f)
            _BACK_SIGS[:] = [_np.array(v, dtype=_np.float32) for v in data.get("backs", [])]
            return
        except Exception:
            continue


def _back_sig(card):
    """Card ka scale-invariant RGB signature (12x16 downscale + normalize) — cosine match ke liye."""
    a = _np.asarray(card.convert("RGB").resize((12, 16), Image.BILINEAR), dtype=_np.float32)
    a = a.reshape(-1, 3)
    a = a - a.mean(0)
    n = float(_np.linalg.norm(a)) or 1.0
    return (a / n).reshape(-1)


def add_card_back(card):
    """Live table se capture ki gayi card back ki signature library me jodo (persist)."""
    _load_back_sigs()
    sig = _back_sig(card)
    if any(float(_np.dot(sig, b)) >= 0.97 for b in _BACK_SIGS):
        return len(_BACK_SIGS)
    _BACK_SIGS.append(sig)
    base = os.environ.get("PREFLOP_ROOT") or os.path.dirname(os.path.abspath(__file__))
    try:
        json.dump({"backs": [v.tolist() for v in _BACK_SIGS]},
                  open(os.path.join(base, "card_back_library.json"), "w", encoding="utf-8"))
    except Exception:
        pass
    return len(_BACK_SIGS)


def _is_rabbit_card(card):
    """Rabbit Hunt card (hand khatam hone ke baad dikhaya 'jo aata') — neeche brown patti.
    Ye asli board nahi hai, hand file me bhi nahi hota."""
    a = _np.asarray(card.convert("RGB"), dtype=_np.int32)
    a = a[int(a.shape[0] * 0.6):]
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    brown = (R > 50) & (R < 160) & (G * 100 > R * 45) & (G * 100 < R * 80) & (B * 10 < G * 6)
    return brown.mean() > 0.15


def _ocr_binary(bw_img, whitelist, psm):
    """Already-binarized image se OCR karo (white glyph on black)."""
    try:
        return pytesseract.image_to_string(
            bw_img, config=f"--psm {psm} -c tessedit_char_whitelist={whitelist}").strip()
    except Exception:
        return ""



FEAT_W, FEAT_H = 12, 16
# Board cards (chhote, ~44px) ke glyphs hero cards se thode alag crop hote hain — isliye
# score 0.90+ nahi, ~0.80-0.84 tak gir jaata hai (correct read bhi). 0.84 pe turn/river ka
# aakhri card 'None' ho jaata tha aur board flop pe atka rehta tha. 0.80 + 0.04 margin se
# ye pakda jaata hai, galat-rank cross-match (~0.75-0.81) abhi bhi bahar rehta hai.
TEMPLATE_MIN_SCORE = 0.80    # isse kam correlation = anjaan glyph
TEMPLATE_SURE_SCORE = 0.92   # saare 13 ranks ke templates na hon tab itna chahiye (cross-rank max ~0.88)
TEMPLATE_MIN_MARGIN = 0.04   # best rank doosre rank se kam se kam itna aage ho
TEMPLATE_DUP_SCORE = 0.985   # same rank ka itna milta template pehle se hai to naya mat jodo
TEMPLATE_CONFLICT_SCORE = 0.97   # doosre rank ka itna milta template = galat label tha, hata do
_SHIFTS = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]


def _templates_path(base):
    return os.path.join(base, "rank_templates.json")


def _load_templates():
    # Pehle repo root (PREFLOP_ROOT) — exe me bhi yahi writable/persistent hai,
    # taaki naye verified glyphs bina rebuild ke add ho sakein.
    bases = [os.environ.get("PREFLOP_ROOT"),
             getattr(sys, "_MEIPASS", None),
             os.path.dirname(os.path.abspath(__file__))]
    for base in bases:
        if not base:
            continue
        try:
            with open(_templates_path(base), encoding="utf-8") as f:
                data = json.load(f)
            if data.get("format") != f"ncc{FEAT_W}x{FEAT_H}":
                continue                     # purana binary format — dobara seekh lenge
            return [(rank, bytes.fromhex(h)) for rank, hs in data["templates"].items() for h in hs]
        except Exception:
            continue
    return []


RANK_TEMPLATES = _load_templates()
_tmpl_arr = {"id": None, "ranks": [], "arr": None}
_rank_cache = {}             # sirf memory me — galat OCR disk pe pakka nahi hota


# ---------------- suit templates (suit symbol pixels -> exact suit) ----------------
# Hero cards chhote hote hain isliye suit symbol faint hota hai — rank ki tarah hand file se
# seekhta hai. Board cards 4-color body wale hote hain (unka suit body color se hi sahi aata hai).
SUIT_FEAT_W, SUIT_FEAT_H = 8, 10
SUIT_MATCH_SCORE = 0.82


def _suit_templates_path(base):
    return os.path.join(base, "suit_templates.json")


def _load_suit_templates():
    for base in (os.environ.get("PREFLOP_ROOT"), getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(__file__))):
        if not base:
            continue
        try:
            with open(_suit_templates_path(base), encoding="utf-8") as f:
                data = json.load(f)
            return [(s, bytes.fromhex(h)) for s, hs in data["templates"].items() for h in hs]
        except Exception:
            continue
    return []


SUIT_TEMPLATES = _load_suit_templates()


def _suit_feat(card):
    """Card ke right-center (suit symbol) ka contrast map -> key — ya None (symbol nahi dikha)."""
    if not HAS_CV:
        return None
    cw, ch = card.size
    region = card.crop((int(cw * 0.45), 0, cw, ch))
    a = _np.asarray(region.convert("RGB"), dtype=_np.float32)
    body = _np.median(a.reshape(-1, 3), axis=0)
    d = _np.clip(_np.abs(a - body).sum(2), 0, 255)
    if d.max() < 40:
        return None
    g = _cv2.resize(d, (SUIT_FEAT_W, SUIT_FEAT_H), interpolation=_cv2.INTER_AREA)
    return _np.clip(g, 0, 255).astype(_np.uint8).tobytes()


def _suit_scores(key):
    q = _np.frombuffer(key, _np.uint8).astype(_np.float32).reshape(SUIT_FEAT_H, SUIT_FEAT_W)
    qs = q - q.mean()
    out = {}
    for s, k in SUIT_TEMPLATES:
        t = _np.frombuffer(k, _np.uint8).astype(_np.float32).reshape(SUIT_FEAT_H, SUIT_FEAT_W)
        ts = t - t.mean()
        den = _np.sqrt((qs * qs).sum() * (ts * ts).sum()) + 1e-6
        sc = float((qs * ts).sum() / den)
        if sc > out.get(s, -1.0):
            out[s] = sc
    return out


def _suit_match(card):
    """Learned suit template se suit padho — ya None (tab body color wala _suit use hota hai)."""
    key = _suit_feat(card)
    if not key or not SUIT_TEMPLATES:
        return None
    sc = sorted(_suit_scores(key).items(), key=lambda kv: -kv[1])
    if sc and sc[0][1] >= SUIT_MATCH_SCORE and (len(sc) < 2 or sc[0][1] - sc[1][1] >= 0.05):
        return sc[0][0]
    return None


def _learn_suit(key, suit):
    global SUIT_TEMPLATES
    if not key or suit not in "shdc":
        return False
    if any(s == suit and k == key for s, k in SUIT_TEMPLATES):
        return False
    SUIT_TEMPLATES.append((suit, key))
    base = os.environ.get("PREFLOP_ROOT") or os.path.dirname(os.path.abspath(__file__))
    try:
        data = {}
        for s, k in SUIT_TEMPLATES:
            data.setdefault(s, []).append(k.hex())
        with open(_suit_templates_path(base), "w", encoding="utf-8") as f:
            json.dump({"format": f"suit{SUIT_FEAT_W}x{SUIT_FEAT_H}", "templates": data}, f)
    except Exception:
        pass
    return True


def _rank_feat(card):
    """Card ke top-left rank glyph ka contrast map -> FEAT_W*FEAT_H bytes (glyph key).
    Corner = card WIDTH se naapa (board card ka blob height badalta rehta hai); usme body
    color se sabse bada contrast blob = glyph, uska tight crop (edges/suit symbol bahar)."""
    if not HAS_CV:
        return None
    a = _np.asarray(card.convert("RGB"), dtype=_np.float32)
    ch, cw = a.shape[:2]
    c = a[:min(ch, max(6, round(cw * 0.65))), :max(6, round(cw * 0.55))]
    body = _np.median(c.reshape(-1, 3), axis=0)
    d = _np.clip(_np.abs(c - body).sum(2), 0, 255)
    if d.max() < 60:                     # corner me kuch nahi (card back / khaali)
        return None
    # 0.45*d.max() se thin/anti-aliased glyph (jaise "3") TOOT jaata tha — chhote cards pe
    # white text + dark outline dono milke glyph banate hain, unche threshold pe gaps aa jaate
    # the aur NCC 0.4 pe fail ho jaata tha. Kam threshold poori shape rakhta hai (0.96 match).
    m = d > max(40, 0.30 * d.max())
    m[:2, :] = False                     # card ka top/left edge glyph nahi
    m[:, :2] = False
    n, _, st, _ = _cv2.connectedComponentsWithStats(m.astype(_np.uint8), 8)
    if n < 2:
        return None
    i = 1 + int(_np.argmax(st[1:, _cv2.CC_STAT_AREA]))
    x, y, w, h = (int(v) for v in st[i, :4])
    if h < 5:
        return None
    g = d[max(0, y - 1):y + h + 1, max(0, x - 1):x + w + 1]
    g = _cv2.resize(g, (FEAT_W, FEAT_H), interpolation=_cv2.INTER_AREA)
    return _np.clip(g, 0, 255).astype(_np.uint8).tobytes()


def _tmpl_matrix():
    """Templates ka (N, H, W) float array — templates badle tabhi dobara banta hai."""
    tl = RANK_TEMPLATES
    if _tmpl_arr["id"] is not tl:
        arr = _np.array([_np.frombuffer(k, _np.uint8) for _, k in tl], dtype=_np.float32)
        _tmpl_arr.update(id=tl, ranks=[r for r, _ in tl],
                         arr=arr.reshape(-1, FEAT_H, FEAT_W) if len(tl) else None)
    return _tmpl_arr["ranks"], _tmpl_arr["arr"]


def _ncc_scores(key, arr):
    """Har template se best correlation (+-1 px shift me)."""
    q = _np.frombuffer(key, _np.uint8).astype(_np.float32).reshape(FEAT_H, FEAT_W)
    best = _np.full(len(arr), -1.0, dtype=_np.float32)
    for dy, dx in _SHIFTS:
        qs = q[max(0, dy):FEAT_H + min(0, dy), max(0, dx):FEAT_W + min(0, dx)]
        ts = arr[:, max(0, -dy):FEAT_H + min(0, -dy), max(0, -dx):FEAT_W + min(0, -dx)]
        qs = qs - qs.mean()
        ts = ts - ts.mean(axis=(1, 2), keepdims=True)
        den = _np.sqrt((qs * qs).sum() * (ts * ts).sum(axis=(1, 2))) + 1e-6
        best = _np.maximum(best, (ts * qs).sum(axis=(1, 2)) / den)
    return best


def _match_scores(key):
    """{rank: best score} — har rank ka sabse milta template."""
    ranks, arr = _tmpl_matrix()
    if not key or arr is None:
        return {}
    out = {}
    for r, sc in zip(ranks, _ncc_scores(key, arr).tolist()):
        if sc > out.get(r, -1.0):
            out[r] = sc
    return out


def _match_template(key):
    """Confident template match ka rank, ya None (anjaan / do ranks me confusion).
    Jab tak kisi rank ka template bana hi nahi, uska glyph kisi aur rank se ~0.88 tak
    mil sakta hai — isliye tab sirf bahut pakka (>= TEMPLATE_SURE_SCORE) match maanya."""
    sc = sorted(_match_scores(key).items(), key=lambda kv: -kv[1])
    if not sc or sc[0][1] < TEMPLATE_MIN_SCORE:
        return None
    if len(sc) > 1 and sc[0][1] - sc[1][1] < TEMPLATE_MIN_MARGIN:
        return None
    if sc[0][1] < TEMPLATE_SURE_SCORE and len(sc) < len(RANKS):
        return None
    return sc[0][0]


def _rank_glyph(card):
    """Card ke top-left corner se rank glyph nikalo (tesseract fallback ke liye) — ya None.
    Red Star cards WHITE body + colored text hote hain; Natural8 colored body +
    white text. Isliye body color se CONTRAST wale pixels hi rank glyph hain."""
    cw, ch = card.size
    corner = card.crop((0, 0, int(cw * 0.65), int(ch * 0.65)))
    w, h = corner.size
    px = corner.convert("RGB").load()
    vals = [px[x, y] for y in range(h) for x in range(w)]
    vals.sort()
    br, bg, bb = vals[len(vals) // 2]          # body color (median)
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if abs(r - br) + abs(g - bg) + abs(b - bb) > 150:
                mp[x, y] = 255
    mask = _largest_component(mask)
    bbox = mask.getbbox()
    return mask.crop(bbox) if bbox else None


def _read_rank(card):
    """Card ke top-left me rank padho."""
    return _read_rank_key(card)[0]


# True = sirf verified templates se padho (anjaan glyph = None, kabhi galat nahi).
# False = jab tak saare 13 ranks ke templates nahi bane, anjaan glyph pe tesseract ka
# andaza (galat ho sakta hai); 13 ranks ho jaane ke baad tesseract band.
TEMPLATES_ONLY = False


def _templates_complete():
    return len({r for r, _ in RANK_TEMPLATES}) == len(RANKS)


def _read_rank_key(card):
    """(rank, glyph_key) — key se baad me hand file ke exact cards se template banta hai."""
    key = _rank_feat(card)
    if not key:
        return None, None
    if key in _rank_cache:
        return _rank_cache[key], key
    rank = _match_template(key)
    # Tesseract fallback: templates complete hone ke BAAD bhi chalao — chhote board card ka
    # glyph threshold se niche (0.81) ho to template 'None' deta hai aur card ginti se chhoot
    # jaata hai (turn/river kabhi nahi padha jaata). Tesseract is case me '6' sahi padh leta
    # hai. Ye sirf FALLBACK hai — confident template match hamesha pehle aata hai.
    if rank is None and not TEMPLATES_ONLY:
        rank = _tesseract_rank(card)
    if len(_rank_cache) > 2000:
        _rank_cache.clear()
    _rank_cache[key] = rank          # same pixels = same jawab (None bhi)
    return rank, key


def _tesseract_rank(card):
    glyph = _rank_glyph(card)
    if not glyph or not HAS_TESS:
        return None
    s = max(glyph.size) + 12
    canvas = Image.new("L", (s, s), 0)
    canvas.paste(glyph, ((s - glyph.width) // 2, (s - glyph.height) // 2))
    # NEAREST (blocky font) — LANCZOS blur karke 5->9, 9->5 jaisa galat padh deta hai
    canvas = canvas.resize((s * 8, s * 8), Image.NEAREST)

    def try_psm(psm):
        for ch in _ocr_binary(canvas, "AKQJT98765432", psm):
            if ch.upper() in "AKQJT98765432":
                return ch.upper()
        return None
    return _ocr_rank(try_psm)


def _ocr_rank(try_psm):
    # psm 13 + 8 digits/letters dono ke liye reliable (5 vs 9 confusion nahi)
    r13, r8 = try_psm("13"), try_psm("8")
    if r13 and r8 and r13 == r8:
        return r13
    for psm in ("6", "10", "7"):
        r = try_psm(psm)
        if r:
            return r
    return r13 or r8


_tmpl_lock = threading.Lock()
_tmpl_state = {"gen": 0}


def _tmpl_gen():
    return _tmpl_state["gen"]


def add_rank_template(rank, key):
    """Verified glyph key ko templates me add karo (memory + repo disk).
    Isi glyph jaisa doosre rank ka template (purani galat training) hata deta hai."""
    global RANK_TEMPLATES
    if not key or rank not in RANKS:
        return False
    with _tmpl_lock:
        ranks, arr = _tmpl_matrix()
        sc = _ncc_scores(key, arr).tolist() if arr is not None else []
        if any(s >= TEMPLATE_DUP_SCORE and r == rank for r, s in zip(ranks, sc)):
            return False
        bad = {i for i, (r, s) in enumerate(zip(ranks, sc)) if s >= TEMPLATE_CONFLICT_SCORE and r != rank}
        RANK_TEMPLATES = [t for i, t in enumerate(RANK_TEMPLATES) if i not in bad] + [(rank, key)]
        _rank_cache.clear()      # purane (tesseract) jawab ab galat ho sakte hain
        _tmpl_state["gen"] += 1  # action_loop current cards dobara padhega
        data = {}
        for r, k in RANK_TEMPLATES:
            data.setdefault(r, []).append(k.hex())
        base = os.environ.get("PREFLOP_ROOT") or os.path.dirname(os.path.abspath(__file__))
        try:
            with open(_templates_path(base), "w", encoding="utf-8") as fh:
                json.dump({"format": f"ncc{FEAT_W}x{FEAT_H}", "templates": data}, fh)
        except Exception:
            pass
    return True


def learn_from_hand(hand, img=None):
    """User ne cards manually correct kiye — hero ke dono glyphs is label se save karo.
    hand = "4c8d" (4-char), left card pehle."""
    img = img or state.get("last_img")
    if not img or not isinstance(hand, str) or len(hand) != 4:
        return {"ok": False, "error": "no frame / invalid hand"}
    obs = []
    detect(img, obs)
    if len(obs) != 2:
        return {"ok": False, "error": "hero cards not found"}
    added = [r for r, (k, _) in zip((hand[0].upper(), hand[2].upper()), obs) if add_rank_template(r, k)]
    return {"ok": True, "added": added}


def read_card(card_img, obs=None):
    """Ek card se (rank, suit) nikalo — rank OCR se, suit learned template (ya body color).
    obs list di ho to (rank_key, suit, suit_feat) usme append hota hai (auto-labelling ke liye)."""
    if not card_img:
        return None, None
    cw, ch = card_img.size
    if cw < 8 or ch < 8:
        return None, None
    if _is_card_back(card_img):
        return None, None
    rank, key = _read_rank_key(card_img)
    suit = _suit_match(card_img) or _suit(card_img)
    if obs is not None:
        obs.append((key, suit, _suit_feat(card_img)))
    return rank, suit


def isolate_cards(crop):
    """Hero region me se do cards nikalo (left + right half, card body ke bbox se)."""
    if not crop:
        return []
    w = crop.width
    cards = []
    for x0, x1 in ((0, w // 2), (w // 2, w)):
        half = crop.crop((x0, 0, x1, crop.height))
        bbox = _card_bbox(half)
        if bbox:
            cards.append(half.crop(bbox))
    return cards


def _board_card_boxes(img):
    """Board ke cards ke (x, y, w, h) boxes, left->right.
    Board ke saare cards ek hi size ke, ek row me, fixed pitch pe hote hain. Jis card pe pot ke
    chips chadhe hain uska blob tedha aata hai (zyada chauda / kam lamba / kam fill) — usse
    rank/suit ka crop khisak jaata tha (Ks ko Kd padha, ya card mila hi nahi). Saaf cards se
    size + grid nikaal ke aise blobs ko usi grid pe baitha do."""
    blobs = [b for b in _find_card_blobs(img, y0f=0.30, y1f=0.58, loose=True) if b[3] >= 8]
    clean = [b for b in blobs if 0.68 <= b[2] / b[3] <= 0.82 and b[4] / (b[2] * b[3]) >= 0.6]
    if clean:
        ch = max(b[3] for b in clean)
        clean = [b for b in clean if b[3] >= ch - 2]
        cw = sorted(b[2] for b in clean)[len(clean) // 2]
        y0, xref = min(b[1] for b in clean), clean[0][0]
        pitch = cw + 0.07 * ch
    out = []
    for x, y, bw, bh, area in blobs:
        if clean and (x, y, bw, bh, area) not in clean and bw < 1.6 * cw:
            gx = xref + round((x - xref) / pitch) * pitch
            if abs(gx - x) <= 0.25 * cw and abs(y - y0) <= 3 and bh >= 0.5 * ch:
                out.append((int(round(gx)), y0, cw, ch))
                continue
        if area / (bw * bh) < 0.6:
            continue                                # tedha blob jo grid pe nahi baitha — card nahi
        gap = 0.07 * bh
        n = max(1, round((bw + gap) / (0.81 * bh)))
        pitch_b = (bw + gap) / n
        for k in range(n):
            out.append((x + int(round(k * pitch_b)), y, max(8, int(round(pitch_b - gap))), bh))
    return sorted(set(out))


def _find_card_blobs(img, y0f=0.40, y1f=1.0, loose=False):
    """Window ke ek horizontal band me face-up card body ke bbox dhoondo.

    Natural8/GG 4-color deck: card body RED/BLUE/GREEN/GRAY hota hai —
    isi se cards table felt, white text aur HUD se alag hote hain.
    """
    if not HAS_CV:
        return []
    arr = _np.asarray(img.convert("RGB"), dtype=_np.int32)
    h, w = arr.shape[:2]
    R, G, B = arr[..., 0], arr[..., 1], arr[..., 2]
    red = (R > 110) & (G < 85) & (B < 85)
    green = (G > 95) & (R < 85) & (B < 85)
    # diamond = CYAN (blue+green dono high, red low) — Natural8 me diamond cyan hota hai
    cyan = (B > 100) & (R < 90) & (G > 70) & (B >= G)
    gray = ((R > 75) & (R < 150) & (G > 75) & (G < 150) & (B > 75) & (B < 150)
            & (_np.maximum(_np.maximum(R, G), B) - _np.minimum(_np.minimum(R, G), B) < 25))
    # WHITE card body — Red Star ke hero cards white hote hain (colored text ke saath)
    white = (R > 165) & (G > 165) & (B > 165)
    # LIGHT BLUE card body — Red Star chhote cards pe hero + board cards light blue bante hain
    lightblue = (B > 140) & (G > 130) & (R > 100) & (B > R + 20)
    min_area = int(w * h * 0.0004)
    min_w, min_h = int(w * 0.03), int(h * 0.05)
    blobs = []
    # Har body color ke blobs ALAG — ek combined mask me chips / "48 BB" jaisa white text
    # card se jud jaata tha aur card ka rectangle test fail ho jaata tha.
    for mask in (red, green, cyan, gray, white, lightblue):
        mask = mask.copy()
        mask[:int(h * y0f), :] = False
        mask[int(h * y1f):, :] = False
        n, labels, stats, _ = _cv2.connectedComponentsWithStats(mask.astype(_np.uint8) * 255, 8)
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if area < min_area or bw < min_w or bh < min_h:
                continue
            if area / (bw * bh) < 0.6:      # solid rectangle hi card hai (text/strips nahi)
                # Card pe chips / bet label chadhe hon (board ka gray card + pot chips) to same
                # rang ke chip pixels bbox ko phaila dete hain aur fill 0.6 se neeche gir jaata
                # hai — poora card gayab ho jaata tha (flop "preflop" hi dikhta raha).
                # loose=True: aise blob bhi lautao, caller (detect_board) grid se size theek karega.
                if not loose or area / (bw * bh) < 0.4:
                    continue
            blobs.append((int(x), int(y), int(bw), int(bh), int(area)))
    return sorted(blobs)


def _split_wide(x, y, bw, bh):
    """Overlapping cards ek wide blob me ho sakte hain — beech se tod do."""
    if bw > 1.55 * bh:
        hw = bw // 2
        return [(x, y, hw, bh), (x + hw, y, bw - hw, bh)]
    return [(x, y, bw, bh)]


def _pick_pair(cards):
    """Valid cards me se hero ka side-by-side pair chuno — bottom-most row.
    Hero cards sabse neeche hote hain (board center, opponents side me)."""
    if len(cards) == 2:
        a, b = sorted(cards, key=lambda c: c[0])
        if abs((a[1] + a[3] / 2) - (b[1] + b[3] / 2)) <= 0.6 * max(a[3], b[3]):
            return [a, b]
        return None
    if len(cards) < 2:
        return None
    # bottom-most card se shuru karke uski same-row pair dhoondo
    cards = sorted(cards, key=lambda c: -(c[1] + c[3]))   # y descending
    for i, a in enumerate(cards):
        for b in cards[i + 1:]:
            if abs((a[1] + a[3] / 2) - (b[1] + b[3] / 2)) <= 0.6 * max(a[3], b[3]):
                return sorted([a, b], key=lambda c: c[0])
    return None


def detect(img, obs=None):
    """Hero ke dono cards padho — position auto-detect, rank + suit.
    obs list di ho to hero ke 2 cards ke (glyph_key, suit) left->right usme aate hain."""
    if not img or not USE_OCR or not HAS_PIL or not HAS_TESS:
        return None, None
    # 1) card-body blobs dhoondo (sirf bottom band — board center me hota hai),
    # 2) har blob se card padho (sirf valid rakhna)
    cards = []
    for x, y, bw, bh, _area in _find_card_blobs(img, y0f=0.55, y1f=0.95):
        for sx, sy, sw, sh in _split_wide(x, y, bw, bh):
            # hero hamesha bottom-CENTER — side wali seats ke cards/backs hero nahi
            if not 0.35 <= (sx + sw / 2) / img.width <= 0.65:
                continue
            o = []
            card = img.crop((sx, sy, sx + sw, sy + sh))
            if _is_dim_card(card):
                continue                      # fold kiye hue (dhundhle) cards — hand nahi
            r, s = read_card(card, o)
            if o and o[0][0] and s:
                cards.append((sx, sy, sw, sh, (r or "?") + s, o[0]))
    # 3) side-by-side pair (bottom-most = hero)
    pair = _pick_pair(cards)
    if pair and len(pair) == 2:
        if obs is not None:
            obs.extend([pair[0][5], pair[1][5]])
        if "?" not in pair[0][4] + pair[1][4]:
            return pair[0][4] + pair[1][4], pair[0][4] + " " + pair[1][4]
        return None, pair[0][4] + " " + pair[1][4]
    # fallback: fixed HERO_REGION (agar dynamic detect fail ho)
    crop, _ = crop_hero(img)
    fixed = [c for c in (isolate_cards(crop) if crop else []) if not _is_dim_card(c)]
    parts, o = [], []
    for c in fixed[:2]:
        r, s = read_card(c, o)
        if r and s:
            parts.append(r + s)
    if obs is not None and len(o) == 2 and all(k and s for k, s, _ in o):
        obs.extend(o)
    if len(parts) == 2:
        return parts[0] + parts[1], " ".join(parts)
    return None, None


_board_cache = {}
_board_prev = []     # last sahi detected board — transient misread pe street stable rakhta hai
_board_votes = {}    # board index (left->right) -> {read_str: count}  (multi-frame voting)
_board_cards = []    # confirmed board cards ("Rs" strings, left->right)
BOARD_CONFIRM_FRAMES = 3   # ek card itne consistent frames pe padhe tabhi confirm


_board_tables = {}   # table title -> (votes, cards, prev) — har table ki apni board state
_board_cur = [None]


def _board_use(key):
    """Board ki voting state (votes / confirmed cards / last board) HAR TABLE ki alag.
    Yeh module-level thi: do tables khuli hon to ek table ka board doosri ke votes me mil
    jaata tha — turn/river confirm nahi hota tha, ya doosri table ka board 'purana board'
    ban ke aa jaata tha (flop ke bets preflop me, walk pe phantom check). Har table ke
    frame se pehle uski state load karo."""
    global _board_votes, _board_cards, _board_cache
    if key == _board_cur[0]:
        return
    if _board_cur[0] is not None:
        _board_tables[_board_cur[0]] = (_board_votes, _board_cards, list(_board_prev), _board_cache)
    # cache bhi per-table: usme confirmed board (state) rakha hota hai
    _board_votes, _board_cards, pv, _board_cache = _board_tables.pop(key, ({}, [], [], {}))
    _board_prev[:] = pv
    _board_cur[0] = key


def detect_board(img, obs=None):
    """Board (flop/turn/river) — multi-frame voting se PAKKA. Har card alag confirm hota hai
    (OpenHoldem multi-sample jaisa): 3 frames me same read aaye tabhi board me jaata hai.
    Ek frame ka misread board ko kabhi nahi todta. Flop 3 -> turn 4 -> river 5 (left->right
    cards badhte hain, isliye index stable rehta hai)."""
    global _board_votes, _board_cards
    if not img or not HAS_CV or not HAS_TESS or not board_present(img):
        _board_votes = {}
        _board_cards = []
        return None
    w, h = img.size
    x0, y0, x1, y1 = BOARD_REGION
    # key me POORI board row (0.30-0.70): river card x~0.61-0.67 pe hota hai jo BOARD_REGION
    # (0.58 tak) ke bahar hai — check-through river pe key badalti hi nahi thi aur cache
    # purana 4-card board lautata rehta tha (river kabhi detect nahi hota tha).
    bkey = img.crop((int(x0 * w), int(y0 * h), int(0.70 * w), int(y1 * h))).resize((64, 20), Image.BILINEAR).tobytes()
    hit = _board_cache.get(bkey)
    if hit is not None:
        board, saved_obs = hit
        if obs is not None and saved_obs:
            obs.extend(saved_obs)
        return board
    # 1) individual cards padho (all-or-nothing NAHI — har card apna read)
    cands = []   # [(x, "Rs"|None, [obs 3-tuple])]
    for sx, y, sw, bh in _board_card_boxes(img):
        card = img.crop((sx, y, sx + sw, y + bh))
        if _is_card_back(card) or _is_rabbit_card(card):
            continue
        o = []
        r, s = read_card(card, o)
        if o:
            # rank None bhi rakho — glyph key hai, hand khatam pe truth se label hoke seekhega
            cands.append((sx, (r + s) if r and s else None, o))
    cands.sort(key=lambda c: c[0])
    # 1b) obs: SAARE candidate glyph keys (left->right) — misread (galat rank) ka bhi glyph
    #     record hota hai, XML truth use sahi rank se label kar deta hai (misread kam hota jaata)
    if obs is not None:
        for _, _, o in cands:
            if o:
                obs.append(o[0])
    # 2) vote (sirf rank padhe cards; index = left->right)
    for i, (_, read, _o) in enumerate(cands):
        if read:
            d = _board_votes.setdefault(i, {})
            d[read] = d.get(read, 0) + 1
    # 2b) FAST ADVANCE: candidate count badha (turn/river deal hua) aur SAARE cards clean
    #     padhe (rank+suit, koi None nahi) — turant confirm. 3-frame vote ka wait turn/river
    #     bet ko galat street (flop) pe daal deta tha.
    reads = [r for _, r, _ in cands if r]
    if reads and len(reads) == len(cands) and len(reads) == len(_board_cards) + 1 \
            and 3 <= len(reads) <= 5 \
            and all(a[0] == b[0] for a, b in zip(reads, _board_cards)):
        _board_cards = list(reads)
        saved = [o[0] for _, _, o in cands if o]
        _board_cache[bkey] = (list(_board_cards), saved)
        return list(_board_cards)
    # 3) confirm: top vote >= BOARD_CONFIRM_FRAMES
    confirmed = []
    for i in sorted(_board_votes):
        read, cnt = max(_board_votes[i].items(), key=lambda kv: kv[1])
        if cnt >= BOARD_CONFIRM_FRAMES:
            confirmed.append(read)
    # duplicate ya galat split hua to purana board rakho
    if len(confirmed) != len(set(confirmed)) or len(confirmed) > 5:
        confirmed = list(_board_cards)
    if 3 <= len(confirmed) <= 5 and len(set(confirmed)) == len(confirmed):
        # naya confirm hua board — purana se 1 zyada ho to hi advance (galat 4th card na aaye).
        # Prefix RANK-only match: suit chhote cards pe flicker karta hai (9s <-> 9c), isliye
        # suit ki exact match maangne se turn/river kabhi confirm nahi hota tha.
        ranks_ok = all(a[0] == b[0] for a, b in zip(confirmed, _board_cards))
        if len(_board_cards) == 0 or len(confirmed) == len(_board_cards) \
                or (len(confirmed) == len(_board_cards) + 1 and ranks_ok):
            _board_cards = list(confirmed)
        # cache me SAARE candidate glyph keys (cache-hit frames bhi wahi obs return karein)
        saved = [o[0] for _, _, o in cands if o]
        _board_cache[bkey] = (list(_board_cards), saved)
        if len(_board_cache) > 40:
            _board_cache.clear()
        return list(_board_cards)
    # 4) abhi confirm nahi — last known board (street stable)
    if 3 <= len(_board_cards) <= 5:
        _board_cache[bkey] = (list(_board_cards), [])
        return list(_board_cards)
    return None


_btn_detect_cache = {}


def _detect_button(img):
    """Dealer button (yellow 'D' circle) ka center dhoondo — window fraction.
    Button sirf hand change pe hilta hai — frame hash se cache (cv2 full-frame scan se bachao)."""
    if not HAS_CV:
        return None
    key = img.resize((120, 85), Image.BILINEAR).tobytes()
    v = _btn_detect_cache.get(key)
    if v is not None:
        return v
    arr = _np.asarray(img.convert("RGB"), dtype=_np.int32)
    h, w = arr.shape[:2]
    R, G, B = arr[..., 0], arr[..., 1], arr[..., 2]
    gold = (R > 150) & (G > 110) & (B < 110)
    n, labels, stats, _ = _cv2.connectedComponentsWithStats((gold.astype(_np.uint8)) * 255, 8)
    best = None
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 80 or bw < 8 or bh < 8:
            continue
        if not (0.5 <= bw / bh <= 2.0):      # roundish hi button hai (text nahi)
            continue
        # button chamkeela peela (~229,201,51) hai; bet chips dull gold (~195,158,55)
        if G[labels == i].mean() < 185:
            continue
        if best is None or area > best[2]:
            best = (x + bw / 2.0, y + bh / 2.0, area)
    if best:
        pos = (best[0] / w, best[1] / h)
        _btn_detect_cache[key] = pos
        if len(_btn_detect_cache) > 60:
            _btn_detect_cache.clear()
        return pos
    return None


def _button_seat(img):
    """Dealer button kis seat ke paas hai (SEAT_ANCHORS index), ya None."""
    btn = _detect_button(img)
    if not btn:
        return None
    bx, by = btn
    return min(range(len(SEAT_ANCHORS)),
               key=lambda i: (bx - SEAT_ANCHORS[i][0]) ** 2 + (by - SEAT_ANCHORS[i][1]) ** 2)


def _crop_frac(img, box):
    w, h = img.size
    return img.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))


_badge_cache = {}


def _sitting_out(img, seat):
    """Plaque ke upar "SIT OUT" badge hai? (OCR cached — same pixels = same jawab)"""
    box = BADGE_BOXES[seat]
    if not box or not HAS_TESS:
        return False
    c = _crop_frac(img, box)
    c = c.resize((c.width * 4, c.height * 4), Image.LANCZOS)
    a = _np.asarray(c.convert("RGB"), dtype=_np.int32)
    white = (a.min(2) > 150) & ((a.max(2) - a.min(2)) < 40)
    if white.sum() < 40:
        return False
    key = white.tobytes()
    if key not in _badge_cache:
        try:
            t = pytesseract.image_to_string(
                Image.fromarray(_np.where(white, 0, 255).astype(_np.uint8)),
                config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ").strip()
        except Exception:
            t = ""
        if len(_badge_cache) > 300:
            _badge_cache.clear()
        _badge_cache[key] = ("OUT" in t) or ("SIT" in t)      # "SITOUT", "SOTOUT" dono aate hain
    return _badge_cache[key]


def active_seats(img):
    """Kaun si seats hand me hain: player baitha hai aur sit-out nahi. Hero hamesha active."""
    img = img.convert("RGB")
    out = []
    for i, box in enumerate(PLAQUE_BOXES):
        if i == HERO_SEAT:
            out.append(True)
            continue
        a = _np.asarray(_crop_frac(img, box), dtype=_np.int32)
        text = ((a.min(2) > 85) & ((a.max(2) - a.min(2)) < 40)).mean()   # naam/stack ka text
        out.append(text > 0.01 and not _sitting_out(img, i))
    return out


def seat_positions(img, btn):
    """{seat: "BTN"/"SB"/...} — sirf active seats. Kam players ho to UTG/MP pehle hat-te hain
    (4-handed = CO, BTN, SB, BB). Khaali/sit-out seat ko gina to poori table ek seat khisak jaati."""
    act = active_seats(img)
    n = len(SEAT_ANCHORS)
    after = [(btn + k) % n for k in range(1, n) if act[(btn + k) % n]]   # button ke baad clockwise
    pos = {}
    if act[btn]:
        pos[btn] = "BTN"
        if len(after) == 1:                                               # heads-up: button = SB
            pos[btn], pos[after[0]] = "SB", "BB"
            return pos
    for seat, name in zip(after, ("SB", "BB")):
        pos[seat] = name
    rest = after[2:]
    if rest:
        pos.update(zip(rest, ["UTG", "MP", "CO"][-len(rest):]))
    return pos


def detect_position(img):
    """Dealer button + active seats se hero ki table position nikalo (UTG/MP/CO/BTN/SB/BB)."""
    b = _button_seat(img)
    return None if b is None else seat_positions(img, b).get(HERO_SEAT)



FONT_RADIUS = 150
_font_lock = threading.Lock()


def _font_load(name):
    for base in (os.environ.get("PREFLOP_ROOT"), getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(__file__))):
        if not base:
            continue
        try:
            with open(os.path.join(base, name), encoding="utf-8") as f:
                data = json.load(f)
            return {tuple(int(v, 16) for v in k.split(",")): c for k, c in data["chars"].items()}
        except Exception:
            continue
    return {}


FONTS = {"bet": _font_load("bet_font.json"), "btn": _font_load("btn_font.json")}


def _font_mask(crop):
    a = _np.asarray(crop.convert("RGB"), dtype=_np.int32)
    return _np.abs(a - 255).sum(2) <= FONT_RADIUS


def _font_lines(mask):
    """Khaali rows pe text lines (button: 'CALL' / '1.50 BB'). Chhote dhabbe (chips ki chamak) nahi."""
    rows = list(mask.any(1)) + [False]
    out, start = [], None
    for y, r in enumerate(rows):
        if r and start is None:
            start = y
        elif not r and start is not None:
            ln = mask[start:y]
            if ln.sum() >= 6 and ln.shape[0] >= 5:
                out.append(ln)
            start = None
    return out


def _font_chars(line):
    """Line -> chars; har char = columns ke ints ka tuple (neeche ke khaali bits trim)."""
    weights = 1 << _np.arange(line.shape[0] - 1, -1, -1, dtype=_np.int64)
    cols = [int(v) for v in (line.astype(_np.int64) * weights[:, None]).sum(0)]
    chars, cur = [], []
    for c in cols + [0]:
        if c:
            cur.append(c)
        elif cur:
            chars.append(cur)
            cur = []
    out = []
    for ch in chars:
        low = min((c & -c).bit_length() - 1 for c in ch)
        out.append(tuple(c >> low for c in ch))
    return out


def _font_read(chars, kind):
    f = FONTS[kind]
    return "".join(f.get(ch, "?") for ch in chars)


def _amount_text(v):
    """Client ka format: 1 -> '1BB', 2.5 -> '2.50BB'."""
    return (str(int(round(v))) if abs(v - round(v)) < 1e-6 else f"{v:.2f}") + "BB"


def _font_learn(kind, chars, text):
    """Verified (chars, text) ko font me jodo. Kisi char ka pehle se alag label ho to kuch nahi."""
    if len(chars) != len(text):
        return 0
    f = FONTS[kind]
    if any(f.get(ch, c) != c for ch, c in zip(chars, text)):
        return 0
    new = {ch: c for ch, c in zip(chars, text) if ch not in f}
    if not new:
        return 0
    with _font_lock:
        f.update(new)
        base = os.environ.get("PREFLOP_ROOT") or os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(base, f"{kind}_font.json"), "w", encoding="utf-8") as fh:
                json.dump({"format": "ohfont", "radius": FONT_RADIUS,
                           "chars": {",".join(format(v, "x") for v in ch): c for ch, c in f.items()}},
                          fh, indent=1, sort_keys=True)
        except Exception:
            pass
    return len(new)


FONT_MAX_CHAR_BITS = 11      # text chars ~9 rows ke; isse lamba = chips / kachra
_last_bet_chars = []         # read_bets ne har seat ke jo chars dekhe (SEAT_ANCHORS order)
_font_frames = {}            # gamecode -> {"t", "frames": [(street, to_call, {pos: chars})]}


def _font_record(to_call, pos_by_seat, gc=None, street="preflop", tbl=None):
    """Hero decision frame: button ka exact to_call + har position ke bet chars. Hand khatam hone pe
    hand history se exact bets milte hain -> tab label (sirf yahi strict truth; 'amount kahin bhi
    daala gaya' jaisi kamzor verification ne 3 ko 2 label kar diya tha).
    gc = isi table ki live XML ka gamecode (multi-table me har table ka apna).
    tbl = isi table ka dir (hand XML real names wali — isi se truth aata hai)."""
    if not gc or not pos_by_seat:
        return
    chars = {pos_by_seat[i]: ch for i, ch in enumerate(_last_bet_chars) if ch and i in pos_by_seat}
    p = _font_frames.setdefault(gc, {"t": time.time(), "frames": [], "tbl": tbl})
    if tbl is not None:
        p["tbl"] = tbl
    if len(p["frames"]) < 60 and (street, to_call, chars) not in p["frames"]:
        p["frames"].append((street, to_call, chars))


# gamecode -> DONE_DATA XML file (har hand unique hota hai; index banane se 5-files limit nahi)
_done_index = {}
_done_index_time = 0.0


def _done_index_refresh(force=False):
    global _done_index, _done_index_time
    now = time.time()
    if _done_index and not force and now - _done_index_time < 60:
        return
    _done_index_time = now
    try:
        for f in redstar_hh.DONE_DATA.glob("*.xml"):
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for m in re.finditer(r'<game gamecode="(\d+)">', txt):
                _done_index[m.group(1)] = f
    except Exception:
        pass


def _xml_game_body(tbl, gc):
    """Table dir ki XML se game body (REAL names) — ya DONE_DATA fallback (anonymized 'Player N').
    TempData XML hand complete hote hi clear ho jaata hai, isliye DONE_DATA index bhi try karo."""
    if not gc:
        return None
    if tbl:
        try:
            p = tbl / (tbl.name + ".xml")
            if p.exists():
                xml = p.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'<game gamecode="%s">(.*?)</game>' % gc, xml, re.S)
                if m:
                    return m.group(1)
        except Exception:
            pass
    # DONE_DATA fallback (anonymized: name="Player N", seat=N)
    if gc not in _done_index:
        _done_index_refresh(force=True)
    f = _done_index.get(gc)
    if not f:
        return None
    try:
        xml = f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r'<game gamecode="%s">(.*?)</game>' % gc, xml, re.S)
    return m.group(1) if m else None


def _completed_decisions(gc, tbl=None):
    """Completed hand XML se hero ke har decision ka (street, to_call, {pos: bet_bb}) — ya None.
    Saari streets (preflop/flop/turn/river) — postflop bet fonts bhi isi se seekhte hain.
    tbl (table dir) ki XML se real names milte hain — DONE_DATA anonymized hai isliye usme
    hero match nahi hota tha."""
    body = _xml_game_body(tbl, gc)
    if not body:
        return None
    money = lambda v: float(re.search(r"[\d.]+", v).group()) if re.search(r"[\d.]+", v) else 0.0
    names, dealer = _xml_players(body)
    def act(no):
        r = re.search(r'<round no="%d">(.*?)</round>' % no, body, re.S)
        return re.findall(r'player="([^"]+)" sum="([^"]*)" type="(\d+)"', r.group(1)) if r else []
    acts0 = act(0)
    pos = redstar_hh.dealt_positions({"names": names, "dealer_idx": dealer,
                                      "rounds": {0: [(nm, t, v) for nm, v, t in acts0]}})
    bbv = next((money(v) for _, v, t in acts0 if t == "2"), 0) or 0.02

    def street_decs(acts, put0):
        put = dict(put0)
        decs = []
        for n, v, t in acts:
            if n == redstar_hh.HERO:
                decs.append((round(max(put.values(), default=0) - put.get(n, 0), 2),
                             {pos[k]: round(x, 2) for k, x in put.items() if x > 0 and k in pos}))
            if t == "23":
                put[n] = money(v) / bbv                   # raise ka sum = TOTAL "raise to"
            elif t not in ("0", "4"):
                put[n] = put.get(n, 0) + money(v) / bbv   # call/bet/all-in = increment
        return decs
    put0 = {}
    for n, v, _ in acts0:
        put0[n] = put0.get(n, 0) + money(v) / bbv
    decs = [("preflop", tc, b) for tc, b in street_decs(act(1), put0)]
    for rno, sname in ((2, "flop"), (3, "turn"), (4, "river")):
        decs += [(sname, tc, b) for tc, b in street_decs(act(rno), {})]
    return decs


def _learn_fonts_from_history(current_gc):
    """Khatam hue hands: har recorded decision frame ko hand history ke exact bets se label karo."""
    for gc in list(_font_frames):
        if gc == current_gc:
            continue
        p = _font_frames[gc]
        decs = _completed_decisions(gc, p.get("tbl"))
        if decs is None and time.time() - p["t"] < 300:
            continue                                       # XML abhi likhi nahi gayi
        _font_frames.pop(gc, None)
        n = matched = total = 0
        for street, to_call, chars in p["frames"] if decs else []:
            total += 1
            d = [b for st, tc, b in decs if st == street and abs(tc - to_call) < 0.01]
            if len(d) != 1:
                continue
            matched += 1
            for ps, ch in chars.items():
                if d[0].get(ps, 0) > 0:
                    n += _font_learn("bet", ch, _amount_text(d[0][ps]))
        if total:
            _acc_bump("accuracy", matched, total)
        if n:
            print(f"[font] hand {gc}: {n} naye verified bet chars")


_BET_RE = re.compile(r"(\d+(?:\.\d+)?)\s*B")


def _parse_bet(text):
    m = _BET_RE.findall(text)
    if m:
        return float(m[-1])
    m = re.search(r"(\d+(?:\.\d+)?)88$", text.replace(" ", ""))   # "BB" kabhi "88" padh jaata hai
    return float(m.group(1)) if m else 0.0


_bet_cache = {}


_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?BB")


def read_bets(img):
    """Har seat ke saamne ka bet (BB me) — SEAT_ANCHORS order me list.
    Pehle font scan (exact, ~1 ms); koi char anjaan ho tabhi tesseract."""
    img = img.convert("RGB")
    w, h = img.size
    out, chars_by_seat = [], []
    for x0, y0, x1, y1 in BET_BOXES:
        c = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
        lines = _font_lines(_font_mask(c))
        chars = _font_chars(max(lines, key=lambda l: l.sum())) if lines else []
        chars = [ch for ch in chars if max(ch).bit_length() <= FONT_MAX_CHAR_BITS]   # chips ka kachra
        chars_by_seat.append(tuple(chars))
        s = _font_read(chars, "bet")
        # Dealer button "D" bet label ke bilkul paas baithta hai aur box me aa jaata hai
        # ('3BB.' jaisa) — fullmatch fail hota tha aur BTN ka poora bet 0 padha jaata tha.
        # Amount shuru se match karo; "BB" ke baad ka (aur shuru ke '.') kachra chhod do.
        m = _AMOUNT_RE.match(s.lstrip("."))
        if m:
            out.append(float(m.group()[:-2]))
            continue
        # Font ne kuch chars pehchane par label "…BB" pe khatam nahi hota = label KATA hua hai
        # (popup / stats box ne dhak diya: '1 BB' ka sirf '?B' bacha). Tesseract aise tukde ko
        # '3 BB' jaisa kuch bhi padh deta tha (phantom 3-bet). Ise unreadable maano.
        # Label poora hai agar "BB" pe khatam hota hai ('?BB' = anjaan digit) ya koi digit
        # pehchana gaya ('20?B' = anjaan B) — tab tesseract theek hai.
        if s and any(ch != "?" for ch in s) and not any(ch.isdigit() for ch in s)                 and not s.rstrip(".?").endswith("BB"):
            out.append(0.0)
            continue
        out.append(_tess_bet(c))
    _last_bet_chars[:] = chars_by_seat
    return out


def read_pot(img):
    """Table ke beech ka 'Pot: X BB' padho (font pehle, phir tesseract)."""
    if not img:
        return 0.0
    w, h = img.size
    x0, y0, x1, y1 = POT_REGION
    c = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    # font: pot text bet se chhota font hai — bet font se match nahi hota, isliye
    # seedha tesseract hi use karo (white text 'X BB' / 'X').
    lines = _font_lines(_font_mask(c))
    for ln in lines:
        s = _font_read(_font_chars(ln), "bet")
        m = _BET_RE.search(s)
        if m and "?" not in s:
            return float(m.group(1))
    return _tess_bet(c)


def _tess_bet(c):
    """Purana tesseract path — sirf jab font me koi char anjaan ho."""
    if not HAS_TESS:
        return 0.0
    c = c.resize((c.width * 4, c.height * 4), Image.LANCZOS)
    a = _np.asarray(c, dtype=_np.int32)
    white = (a.min(2) > 160) & ((a.max(2) - a.min(2)) < 50)   # safed text, rangeen chips nahi
    if white.sum() < 40:                                       # khaali — tesseract mat chalao
        return 0.0
    key = white.tobytes()                                      # exact — alag amount collide na ho
    if key not in _bet_cache:                                  # same label = same pixels
        bw = Image.fromarray(_np.where(white, 0, 255).astype(_np.uint8))
        try:
            t = pytesseract.image_to_string(
                bw, config="--psm 7 -c tessedit_char_whitelist=0123456789.B").strip()
        except Exception:
            t = ""
        if len(_bet_cache) > 500:
            _bet_cache.clear()
        _bet_cache[key] = _parse_bet(t)
    return _bet_cache[key]


def board_present(img):
    """Flop/turn/river cards table ke beech me hain?"""
    w, h = img.size
    x0, y0, x1, y1 = BOARD_REGION
    a = _np.asarray(img.convert("RGB").crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))),
                    dtype=_np.int32)
    colored = (a.max(2) > 90) | ((a.max(2) - a.min(2)) > 60)
    return colored.mean() > 0.1


TABLE_POINTS = [
    ((0.03, 0.55), (12, 69, 24)),     # bahar ka green background
    ((0.13, 0.50), (43, 37, 37)),     # table ka rim
    ((0.70, 0.45), (46, 46, 46)),     # dark felt
]
TABLE_POINT_TOL = 30                  # |dR|+|dG|+|dB|


def is_table_frame(img):
    """Frame sach me (default theme wali) Red Star table hai?"""
    a = _np.asarray(img.convert("RGB"), dtype=_np.int32)
    h, w = a.shape[:2]
    for (fx, fy), rgb in TABLE_POINTS:
        x, y = int(fx * w), int(fy * h)
        c = a[max(0, y - 2):y + 3, max(0, x - 2):x + 3].reshape(-1, 3).mean(0)
        if abs(c - _np.array(rgb)).sum() > TABLE_POINT_TOL:
            return False
    return True


def _action_buttons(img):
    """Neeche-right FOLD / CALL / RAISE panel (red glow) dikh raha hai = hero ki baari.
    Baaki time wahan sirf felt + "Check/Fold" checkboxes hote hain (red ~0%, panel 7-23%)."""
    a = _np.asarray(img.convert("RGB"), dtype=_np.int32)
    h, w = a.shape[:2]
    r = a[int(h * 0.86):int(h * 0.99), int(w * 0.60):int(w * 0.99)]
    R, G, B = r[..., 0], r[..., 1], r[..., 2]
    return bool(((R > 70) & (R > 2 * G) & (R > 2 * B)).mean() > 0.04)


RS_SEATS = [1, 3, 5, 6, 8, 10]   # Red Star 6-max XML seat numbers, clockwise (action order)


_cb_cache = {}


def _is_blue_back(crop):
    """Red Star (iPoker) card back = solid BLUE body + white/red star logo."""
    a = _np.asarray(crop.convert("RGB"), dtype=_np.int32)
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    blue = (B > R + 30) & (B > 100) & (G < B)
    return blue.mean() >= 0.3


def card_back_seats(img):
    """Jin opponent seats ke saamne face-down cards (blue back + star logo) hain — screen seat set.
    Fold hone pe Red Star cards HIDE kar deta hai (card back gayab) — isi se fold pakda jaata hai.
    Full-frame blue blob scan (fixed regions galat seat pe miss karte the — card ka exact
    position window size ke saath thoda khisakta hai)."""
    key = img.resize((120, 85), Image.BILINEAR).tobytes()
    v = _cb_cache.get(key)
    if v is not None:
        return v
    a = _np.asarray(img.convert("RGB"), dtype=_np.int32)
    h, w = a.shape[:2]
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    blue = ((B > R + 40) & (B > 110) & (G < B)).astype(_np.uint8)
    n, _, st, cen = _cv2.connectedComponentsWithStats(blue, 8)
    bx0, by0, bx1, by1 = BOARD_REGION
    seats = set()
    for i in range(1, n):
        x, y, bw, bh, ar = (int(v) for v in st[i])
        if ar < w * h * 0.0008 or bh < h * 0.03:      # chhota card back (~40px) bhi pakdo
            continue
        cx, cy = cen[i][0] / w, cen[i][1] / h
        if bx0 <= cx <= bx1 and by0 <= cy <= by1:
            continue                                   # board ke cards (blue diamond etc.)
        crop = img.crop((x, y, x + bw, y + bh))
        if not _is_blue_back(crop):
            continue
        s = min(range(len(SEAT_ANCHORS)),
                key=lambda k: (cx - SEAT_ANCHORS[k][0]) ** 2 + (cy - SEAT_ANCHORS[k][1]) ** 2)
        # Colour-tag wale player ka NAAM-PLAQUE bhi neela hota hai. Use card back maan lene se us
        # seat ka fold kabhi nahi pakda jaata tha (phir phantom call). Plaque = naam wali jagah
        # pe + logo ka laal bilkul nahi (asli back me ~9% laal; action bubble dhake tab bhi back
        # plaque ke UPAR hota hai, isliye sirf laal pe bharosa nahi).
        px0, py0, px1, py1 = PLAQUE_BOXES[s]
        if px0 - 0.02 <= cx <= px1 + 0.02 and py0 - 0.02 <= cy <= py1 + 0.03:
            c = a[y:y + bh, x:x + bw]
            if ((c[..., 0] > 170) & (c[..., 1] < 90) & (c[..., 2] < 90)).mean() < 0.02:
                continue
        if s != HERO_SEAT:
            seats.add(s)
    _cb_cache[key] = seats
    if len(_cb_cache) > 60:
        _cb_cache.clear()
    return seats


def xml_seat_positions(st, hero=None, dealt_seats=None, prev_dealt=None, backs_only=False,
                       sitout_seats=()):
    """Live XML (players + seat + dealer) -> ({screen_seat: "BTN"/...}, dealer_screen_seat).
    Screen pe hero hamesha HERO_SEAT (bottom-center), seats clockwise — isliye XML seat ka
    order-offset hi screen index hai.
    dealt_seats: is hand me jin opponent screen-seats pe card backs dikhe (hand ki SHURUAAT se dekha
    ho tabhi do) — live XML me baithe-par-deal-na-hue players bhi hote hain, woh hata do."""
    hero = hero or redstar_hh.HERO
    names, seats = st.get("names") or [], st.get("seats") or []
    if hero not in names or len(seats) != len(names) or any(s not in RS_SEATS for s in seats):
        return None, None
    k = RS_SEATS.index(seats[names.index(hero)])
    scr = lambda s: (HERO_SEAT + RS_SEATS.index(s) - k) % len(SEAT_ANCHORS)
    dealer_scr = scr(seats[st["dealer_idx"]])
    # Positions truth wale hi function se (dealt_positions) — dono SAME logic, isliye exact match.
    # Live XML me sit-out / "Wait for BB" players bhi list me hote hain (deal nahi hue):
    #   - blinds ke beech wale dealt_positions khud hata deta hai
    #   - baaki sit-out: pichle hand me deal nahi hua + is hand me blind nahi diya => bahar.
    # dealt_seats (card backs) sirf RAKHNE ka saboot hai (sit-out se wapas aaya player) — kabhi
    # hatane ka nahi, kyunki card-back detection flaky hai (dealt player hat jaata tha).
    back = {nm for nm, s in zip(names, seats) if scr(s) in (dealt_seats or ())}
    if not prev_dealt and backs_only and back:
        # pichla hand log nahi hua (hero usme deal nahi tha) — tab sirf card backs hi bata sakte
        # hain kaun deal hua. Hand shuru se dekha ho (backs_only) tabhi bharosa karo.
        prev_dealt = back
    by_name = redstar_hh.dealt_positions(st, prev_dealt, back,
                                         hero_absent=dealt_seats is not None and hero not in back,
                                         drop={nm for nm, s in zip(names, seats)
                                               if scr(s) in sitout_seats and nm != hero})
    if not by_name:
        return None, dealer_scr
    pos = {scr(s): by_name[nm] for nm, s in zip(names, seats) if nm in by_name}
    return pos, dealer_scr


_btn_cache = {}


def _button_text(img, x0, x1):
    """Button ka tesseract text (fallback). Same pixels = cache."""
    w, h = img.size
    c = img.convert("RGB").crop((int(x0 * w), int(0.905 * h), int(x1 * w), int(0.995 * h)))
    c = c.resize((c.width * 4, c.height * 4), Image.LANCZOS)
    a = _np.asarray(c, dtype=_np.int32)
    white = (a.min(2) > 150) & ((a.max(2) - a.min(2)) < 50)
    if white.sum() < 40:
        return ""
    key = (x0, white.tobytes())
    if key not in _btn_cache:
        try:
            t = pytesseract.image_to_string(
                Image.fromarray(_np.where(white, 0, 255).astype(_np.uint8)),
                config="--psm 6 -c tessedit_char_whitelist=CALHEKIN-B0123456789.").strip()
        except Exception:
            t = ""
        if len(_btn_cache) > 300:
            _btn_cache.clear()
        _btn_cache[key] = t
    return _btn_cache[key]


BTN_MID, BTN_RIGHT = (0.755, 0.877), (0.878, 0.995)


def _button_lines(img, xs):
    w, h = img.size
    return _font_lines(_font_mask(img.crop((int(xs[0] * w), int(0.905 * h), int(xs[1] * w), int(0.995 * h)))))


def read_call_button(img, chips_to_call=None):
    """Hero ki baari pe exact call amount (BB) buttons se: CHECK -> (0, False),
    CALL 1.50 BB -> (1.5, False); chhota stack ho to beech wala button hota hi nahi, sirf
    ALL-IN 65.25 BB -> (65.25, True). Na padh paaye to None.
    CHECK vs CALL: CHECK button pe koi NUMBER nahi hota — amount mile to CALL, na mile to CHECK.
    (Line ginna galat tha: bade table size pe halka "CALL" text mask me aata hi nahi.)
    Amount font scan se (exact); anjaan char ho tabhi tesseract, jiska jawab chips wale to_call
    se mile to font me jod dete hain."""
    mid = _button_lines(img, BTN_MID)
    allin = False
    if not mid:
        # CHECK ka text dim hai — font mask me line nahi banti. Beech ka button sach me hai ya nahi,
        # tesseract se dekho; warna right wala "BET 1 BB" ALL-IN call padh liya jaata tha.
        t = _button_text(img, *BTN_MID)
        if re.search(r"[A-Z]{3}", t) and not re.search(r"\d", t):
            return 0.0, False
        mid, allin = _button_lines(img, BTN_RIGHT), True     # beech ka button nahi = sirf ALL-IN
        if not mid:
            return None
    chars = _font_chars(mid[-1])
    s = _font_read(chars, "btn")
    if _AMOUNT_RE.fullmatch(s):
        return float(s[:-2]), allin
    t = _button_text(img, *(BTN_RIGHT if allin else BTN_MID))
    m = re.search(r"(\d+(?:\.\d+)?)\s*B", t)
    if not m:
        if not allin and t and not re.search(r"\d", t):
            return 0.0, False                                 # number hi nahi = CHECK
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    if chips_to_call is not None and abs(v - chips_to_call) < 0.01:
        _font_learn("btn", chars, _amount_text(v))       # do alag tareeke same bole = verified
    return v, allin


_allin_cache = {}
_allin_state = {}      # gamecode -> {"street": aakhri padhi street, "set": all-in positions}


def _allin_seats(img, seats):
    """Jin seats ke plaque pe "ALL-IN" (ya stack "0 BB") likha hai. All-in ke baad us player ka
    koi action nahi hota — aage ki streets pe sirf cards khulte hain. Yeh na pata ho to runout
    ki har street pe phantom 'check' lagte the. Same pixels = cache (tesseract ek hi baar)."""
    out = set()
    if not HAS_TESS:
        return out
    for i in seats:
        if i >= len(PLAQUE_BOXES):
            continue
        c = _crop_frac(img, PLAQUE_BOXES[i])
        a = _np.asarray(c.convert("RGB"), dtype=_np.int32)
        white = (a.min(2) > 140) & ((a.max(2) - a.min(2)) < 50)
        if white.sum() < 15:
            continue                                  # dim plaque (fold) / khaali seat
        key = (i, white.tobytes())
        v = _allin_cache.get(key)
        if v is None:
            txt = ""
            try:
                for y0 in (0.45, 0.0):               # pehle stack wali line, phir poora plaque
                    part = c.crop((0, int(c.height * y0), c.width, c.height))
                    part = part.resize((part.width * 3, part.height * 3), Image.LANCZOS)
                    b = _np.asarray(part.convert("RGB"), dtype=_np.int32)
                    m = (b.min(2) > 140) & ((b.max(2) - b.min(2)) < 50)
                    if m.sum() < 40:
                        continue
                    txt += "\n" + pytesseract.image_to_string(
                        Image.fromarray(_np.where(m, 0, 255).astype(_np.uint8)),
                        config="--psm 7" if y0 else "--psm 6").upper()
            except Exception:
                pass
            v = bool(re.search(r"ALL.?IN", txt) or re.search(r"(^|\s)[0O]\s*BB", txt))
            if len(_allin_cache) > 400:
                _allin_cache.clear()
            _allin_cache[key] = v
        if v:
            out.add(i)
    return out


def detect_action(img, board_obs=None, xml_pos=None, gc=None, tbl=None):
    """Preflop/postflop action: position, bets, board, hero_to_act.
    hero_to_act = abhi hero ki baari hai (call/check/raise ka faisla).
    Positions OCR se (dealer button + active seats + sit-out badges) — screen hi asli source."""
    b = _button_seat(img)
    if b is None:
        return None
    # Positions: XML exact (already dealt_seats se filtered) > OCR fallback.
    pos = xml_pos or seat_positions(img, b)
    hero = pos.get(HERO_SEAT)
    bets = read_bets(img)
    bets_by_pos = {pos[i]: amt for i, amt in enumerate(bets) if amt > 0 and i in pos}
    mx = max(bets_by_pos.values()) if bets_by_pos else 0.0
    hero_bet = bets_by_pos.get(hero, 0.0)
    to_call = mx - hero_bet
    # Hero ki baari = FOLD/CALL/RAISE buttons dikh rahe hain. Pehle to_call > 0 se andaza tha —
    # hero fold karke bahar ho ya uske baad kisi aur ki baari ho, tab bhi "aapki baari" bolta tha.
    hero_to_act = _action_buttons(img)
    call_allin = False
    if hero_to_act:
        cb = read_call_button(img, to_call)
        if cb is not None:
            bt, ballin = cb
            # Button misread guard: bets ka hisaab (mx - hero_bet) call > 0 bol raha hai par
            # button ne CHECK (0) padha -> math pe bharosa karo. CALL 9BB ka '9' font fail
            # hone pe "CHECK" samajh leta tha, isliye solver ko facing=0 milta tha.
            if bt > 0 or to_call <= 0.02:
                to_call, call_allin = bt, ballin
    board = None
    if board_present(img):
        board = detect_board(img, board_obs)
        if board:
            _board_prev[:] = board          # sahi board yaad rakho
        elif _board_prev:
            board = list(_board_prev)       # board abhi confirm nahi — purana board, street na toote
    else:
        _board_prev[:] = []                 # board screen se gayab = naya hand
        _board_votes.clear()                # voting bhi reset (naya hand, naya board)
        _board_cards[:] = []
    street = _street_name(board) if board else "preflop"
    pot = read_pot(img)
    # hero ke cards abhi screen pe hain? (fold ho gaye to gayab ho jaate hain)
    hc_crop, _ = crop_hero(img)
    # Card jaisa size zaroori: hero BTN pe ho to peela dealer button "D" is area ke kinare me aa
    # jaata hai (1x7 px ka tukda) — use card maan lene se hero ka fold kabhi pakda nahi jaata tha
    # (phir har street pe phantom call/check lagte the).
    hero_cards = bool(hc_crop and any(
        c.width >= 0.3 * hc_crop.width / 2 and c.height >= 0.5 * hc_crop.height
        and not _is_dim_card(c) for c in isolate_cards(hc_crop)))
    if hero_to_act and not call_allin:
        _font_record(round(to_call, 2), pos, gc, street, tbl)  # hand khatam pe exact bets se font seekhega
    # ALL-IN plaque padhna mehenga hai (tesseract) — sirf tab padho jab nayi street khule
    # (board ke cards badhe). All-in ka label runout bhar plaque pe rehta hai, isliye itna kaafi.
    ai = _allin_state.get(gc)
    if ai is None or ai["gc"] != gc:
        if len(_allin_state) > 12:
            _allin_state.clear()
        ai = _allin_state[gc] = {"gc": gc, "street": None, "set": set()}
    if (board and street != ai["street"]) or ai["street"] is None and ai.get("asked"):
        ai["street"] = street
        ai["set"] |= {pos[i] for i in _allin_seats(img, list(pos)) if pos.get(i)}
    allin = sorted(ai["set"])
    if board:
        return {"street": "postflop", "hero": hero, "bets": bets_by_pos, "pot": pot, "allin": allin,
                "board": board, "streetName": street, "hero_cards": hero_cards,
                "hero_to_act": hero_to_act, "to_call": round(to_call, 2), "call_allin": call_allin}
    return {"street": "preflop", "hero": hero, "bets": bets_by_pos, "pot": pot, "allin": allin,
            "hero_cards": hero_cards,
            "hero_to_act": hero_to_act, "to_call": round(to_call, 2), "call_allin": call_allin}


def _street_name(board):
    """Board card count se street — 3=flop, 4=turn, 5=river."""
    if not board:
        return None
    return {3: "flop", 4: "turn", 5: "river"}.get(len(board))


# ---------------- Action tracker — kis player ne kya kiya (pooori line) -------------
POS_ORDER = ["SB", "BB", "UTG", "MP", "CO", "BTN"]   # preflop + postflop (SB first) action order
BLIND_LABEL = {"1": "sb", "2": "bb"}
RAISE_ACTS = ("open", "3bet", "4bet", "5bet", "6bet")
RAISE_NAMES = ["open", "3bet", "4bet", "5bet", "6bet"]
STREETS = ["preflop", "flop", "turn", "river"]
ALLIN_JUMP = 40.0       # ek hi jump me itne BB+ = all-in (stack data OCR path me nahi hota)


def _tracker_seed(t, gc, st, posmap):
    """Naya hand — tracker reset. Blinds ab XML se nahi, OCR bet-delta se aate hain (screen hi
    asli source hai — XML me sit-out/wait players position shift kar dete the)."""
    trk = {"gc": gc, "street": "preflop", "line": {"preflop": []}, "prev_bets": {},
           "prev_inhand": set(), "prev_hta": None, "prev_to_call": 0.0, "check_pending": None,
           "pos": posmap, "t0": time.time()}
    t["tracker"] = trk
    return trk


def _dead_posts(st, posmap):
    """Jin positions ne is hand me dead blind post kiya (live XML round 0 me asli SB/BB ke
    BAAD wale type-1/2 posts) — screen pe yeh bet limp/open jaisa dikhta hai."""
    names, seats = (st or {}).get("names") or [], (st or {}).get("seats") or []
    hero = redstar_hh.HERO if HAS_REDSTAR else None
    if hero not in names or len(seats) != len(names) or any(s not in RS_SEATS for s in seats):
        return set()
    k = RS_SEATS.index(seats[names.index(hero)])
    seen, out = set(), set()
    for nm, t, _ in (st.get("rounds") or {}).get(0, []):
        if t not in ("1", "2"):
            continue
        if t in seen and nm in names:
            scr = (HERO_SEAT + RS_SEATS.index(seats[names.index(nm)]) - k) % len(SEAT_ANCHORS)
            if posmap.get(scr):
                out.add(posmap[scr])
        seen.add(t)
    return out


PREFLOP_ORDER = ["UTG", "MP", "CO", "BTN", "SB", "BB"]


def _poster_checks(line, before=None, alive=None):
    """Dead blind post karne wale ko preflop me option milta hai: uski baari tak koi raise na
    ho to woh CHECK karta hai (chips nahi hilte — screen pe kuch nahi dikhta).
    before=raiser: jo poster raiser se PEHLE act karta hai usne check kiya.
    alive=set: koi raise hua hi nahi — jo poster flop tak hand me hai usne check kiya."""
    for q in [e["pos"] for e in line if e.get("act") == "post"]:
        if any(e["pos"] == q and e.get("act") in ("check", "fold") + RAISE_ACTS for e in line):
            continue
        if before is not None:
            if q not in PREFLOP_ORDER or before not in PREFLOP_ORDER                     or PREFLOP_ORDER.index(q) >= PREFLOP_ORDER.index(before):
                continue
        elif alive is not None and q not in alive:
            continue
        line.append({"pos": q, "act": "check"})


def _check_pot(trk, act):
    """Screen ka pot (tesseract — chhota font) kabhi galat padha jaata hai (15.0 -> 45.0,
    81.50 -> 8150) aur naye hand ke shuru me pichle hand ka pot dikhta rehta hai. Line se pot
    ka hisaab exact hai (har event ka amt = table pe aaye chips): padha hua pot usse bahut
    upar ho hi nahi sakta. Had se bahar ho to line wala pot lo.
    act["pot_raw"] = jo padha gaya, act["pot"] = jaancha hua (yahi app aur accuracy me jaata hai)."""
    raw = act.get("pot_raw")
    if raw is None:
        raw = act["pot_raw"] = act.get("pot") or 0.0
    calc = sum(e.get("amt", 0.0) for evs in trk["line"].values() for e in evs)
    bets, prev, hwm = act.get("bets") or {}, trk.get("prev_bets") or {}, trk.get("hw") or {}
    # is frame ke naye chips (street ke high-water se — label gayab hoke lautne pe dobara na gino)
    fresh = sum(max(0.0, v - max(prev.get(p, 0.0), hwm.get(p, 0.0))) for p, v in bets.items())
    top = max([0.0] + list(bets.values()) + list(hwm.values()))
    who = set(trk.get("alive") or trk.get("prev_inhand") or ()) | set(bets) | set(hwm)
    room = sum(max(0.0, top - max(bets.get(q, 0.0), hwm.get(q, 0.0))) for q in who)
    hi = calc + fresh + room + 0.05                 # baaki sab call kar dein to bhi isse upar nahi
    ok = raw > 0 and 0.5 * (calc + fresh) - 0.05 <= raw <= hi
    # padha hua pot line se KAM bhi nahi ho sakta (chips table pe aa chuke) — bada wala lo
    act["pot"] = round(max(raw, calc + fresh), 2) if ok else round(calc + fresh, 2)


def _finish_hand(trk):
    """Hand khatam (board hat gaya): river check-through ka aakhri check jodo."""
    if trk.get("street") != "river" or trk.get("showdown") or trk.get("runout"):
        return
    hw = trk.get("hw") or {}
    if hw and max(hw.values()) > 0.02:
        return                                  # bet baaki tha — fold/call alag se tay hota hai
    line = trk["line"].setdefault("river", [])
    if any(e.get("act") in ("bet", "raise", "call") for e in line):
        return
    gone = {e["pos"] for evs in trk["line"].values() for e in evs if e.get("act") == "fold"}
    alive = set(trk.get("alive") or ()) - gone
    for q in sorted(alive, key=lambda x: POS_ORDER.index(x) if x in POS_ORDER else 99):
        if not any(e["pos"] == q for e in line):
            line.append({"pos": q, "act": "check"})


def _remap_positions(trk, posmap):
    """Hand ke shuru me positions kabhi galat hoti hain (pichle hand ki file der se aati hai, to
    sit-out player gina jaata hai aur poori table ek seat khiski padhi jaati hai: asli SB ka 0.5
    'BB' pe, asli BB ka 1.0 'MP limp' ban ke likha jaata tha). Seat wahi rehti hai, sirf label
    badalta hai — isliye jab label badle to ab tak ke saare events/bets naye label pe le jao."""
    old = trk.get("posmap")
    trk["posmap"] = dict(posmap)
    if not old or old == posmap:
        return
    mp = {old[k]: posmap[k] for k in old if k in posmap and old[k] and posmap[k] and old[k] != posmap[k]}
    if not mp:
        return
    f = lambda q: mp.get(q, q)
    for evs in trk["line"].values():
        for e in evs:
            e["pos"] = f(e["pos"])
            a = e.get("act")
            if a in ("sb", "bb", "limp", "call") and e.get("amt", 9) <= 1.05 \
                    and not any(x.get("act") in RAISE_ACTS for x in trk["line"].get("preflop", [])):
                if e["pos"] == "SB" and a == "bb":
                    e["act"] = "sb"
                elif e["pos"] == "BB" and a in ("sb", "limp", "call"):
                    e["act"] = "bb"
    for key in ("prev_bets", "hw", "pend_out"):
        d = trk.get(key)
        if isinstance(d, dict):
            trk[key] = {f(k): v for k, v in d.items()}
    for key in ("prev_inhand", "alive", "allin"):
        v = trk.get(key)
        if isinstance(v, set):
            trk[key] = {f(k) for k in v}
    cp = trk.get("check_pending")
    if cp:
        trk["check_pending"] = [f(k) for k in cp]


def _pend_final(trk, p):
    """Pending fold pakka: nishaan hatao."""
    for evs in trk["line"].values():
        for e in evs:
            if e.get("pend") and e["pos"] == p:
                e.pop("pend")
    (trk.get("pend_out") or {}).pop(p, None)


def _pend_drop(trk, p):
    """Pending fold galat tha (card backs wapas dikhe): event hi hata do."""
    for evs in trk["line"].values():
        evs[:] = [e for e in evs if not (e.get("pend") and e["pos"] == p)]
    (trk.get("pend_out") or {}).pop(p, None)


def _pick_callers(shorts, remaining):
    """Pending players me se kaun call kar gaya: jinke shorts ka jod pot ki badhat ke barabar ho.
    shorts = {pos: short}. Returns {pos: amt}. Exact subset na mile (all-in for less) to
    position order me jitna pot badha utna baanto."""
    import itertools
    names = sorted(shorts, key=lambda x: POS_ORDER.index(x) if x in POS_ORDER else 99)
    if remaining <= 0.05 or not names:
        return {}
    for k in range(1, len(names) + 1):
        for combo in itertools.combinations(names, k):
            if abs(sum(shorts[q] for q in combo) - remaining) <= 0.06:
                return {q: shorts[q] for q in combo}
    out = {}
    for q in names:
        amt = min(shorts[q], remaining)
        if amt > 0.05:
            out[q] = amt
            remaining -= amt
    return out


def _close_street(trk, card_backs, posmap, act, gain=None):
    """Street badalne pe pichli street poori karo. Aakhri call ke chips agle hi frame me pot me
    chale jaate hain (bet-delta kabhi dikhta hi nahi), aur opponents ke checks sirf hero ki baari
    se pakde jaate the — hero hand me na ho to poori check-through street khaali rehti thi.
    Jo player agli street pe abhi bhi hand me hai usne zaroor call/check kiya hai."""
    line = trk["line"].get(trk["street"])
    if line is None:
        return
    folded = {e["pos"] for evs in trk["line"].values() for e in evs if e.get("act") == "fold"}
    alive = {posmap.get(s) for s in card_backs} - {None}
    hero = act.get("hero")
    if hero and act.get("hero_cards"):
        alive.add(hero)
    else:
        alive.discard(hero)
    pend = trk.get("pend_out") or {}
    alive -= folded
    bets = trk.get("hw") or {}       # street ka high-water (chips pot me jaane ke baad bhi yaad)
    mx = max(bets.values()) if bets else 0.0
    order = sorted(alive, key=lambda x: POS_ORDER.index(x) if x in POS_ORDER else 99)
    # gain = chips collect hote waqt pot kitna badha (None = pata nahi: street aage badhi).
    # Calls ka jod isse zyada nahi ho sakta: jo abhi bhi hand me hai usne call kiya (all-in
    # for less ho to utna hi jitna pot badha); pending walon me se wahi jinka hisaab baithta hai.
    remaining = gain
    if mx > 0.02 and (gain is None or gain > 0.02):
        for p in order:
            short = mx - bets.get(p, 0.0)
            if short > 0.02:
                amt = short if remaining is None else min(short, remaining)
                if amt > 0.02:
                    line.append({"pos": p, "act": "call", "amt": round(amt, 2)})
                    if remaining is not None:
                        remaining -= amt
    if pend:
        callers = {}
        if mx > 0.02 and remaining is not None:
            callers = _pick_callers({q: mx - bets.get(q, 0.0) for q in pend}, remaining)
        for q in list(pend):
            if q in callers:
                for evs in trk["line"].values():
                    for e in evs:
                        if e.get("pend") and e["pos"] == q:
                            e.pop("pend")
                            e["act"], e["amt"] = "call", round(callers[q], 2)
                pend.pop(q, None)
                trk["showdown"] = True            # cards khule the, fold nahi
            else:
                _pend_final(trk, q)
    if gain is not None and gain <= 0.02:
        return                       # pot nahi badha = haath yahin khatam (walk / sab fold)
    if trk.get("runout") or trk.get("showdown"):
        return                       # all-in runout: aage ki streets pe koi action nahi
    if mx > 0.02:
        pass
    elif trk["street"] != "preflop" and any(posmap.get(s) for s in card_backs):
        # opponent ke card backs abhi bhi hain = asli check-through street. Backs na hon to
        # yeh all-in ka runout hai (cards khule) — us street pe kisi ne kuch nahi kiya.
        for p in order:
            if not any(e["pos"] == p for e in line):
                line.append({"pos": p, "act": "check"})
    if trk["street"] == "preflop" and not any(e.get("act") in RAISE_ACTS for e in line):
        _poster_checks(line, alive=alive)
    # limped pot (koi raise nahi): BB ko option milta hai aur flop aaya = BB ne check kiya.
    # Chips nahi hilte, isliye yeh check sirf yahin se pata chalta hai.
    if (trk["street"] == "preflop" and "BB" in alive
            and not any(e.get("act") in RAISE_ACTS for e in line)
            and not any(e["pos"] == "BB" and e.get("act") == "check" for e in line)):
        line.append({"pos": "BB", "act": "check"})


def update_tracker(t, act, card_backs, posmap, gc, st):
    """Har STABLE frame pe action line update: limp/open/raise/call/fold/check/bet.
    Preflop bets = total invested (labels), postflop = current street — street change pe
    prev_bets reset. Bets DELTA + card backs (fold) + hero_to_act transitions se line banti hai."""
    trk = t.get("tracker")
    if not act:
        if trk and trk.get("gc") == gc:
            for q in list(trk.get("pend_out") or {}):   # table hat gayi / hand khatam — fold hi tha
                _pend_final(trk, q)
        return
    if (not trk or trk.get("gc") != gc) and st is not None:
        # Naya gamecode XML me aa gaya par blinds (round 0) abhi likhe nahi gaye: positions
        # blinds se hi tay hoti hain, tab tak poori table ek-do seat khisak ke padhi jaati hai
        # (asli SB ka 0.5 'BB' pe, asli BB ka 1.0 'MP' pe = phantom "MP limp"). Blinds aane tak ruko.
        posted = {tp for _, tp, _ in (st.get("rounds") or {}).get(0, [])}
        if not {"1", "2"} <= posted and len(st.get("names") or []) > 2:
            return
    if not trk or trk.get("gc") != gc:
        trk = _tracker_seed(t, gc, st, posmap)
        # seed frame — baseline set karo. prev_inhand = SAARE dealt positions (XML posmap se),
        # taaki jo player 1.5s settle window ke andar hi fold kar dein unke folds bhi baad me
        # pakde jaayen (pehle card backs se seed hota tha aur early folds chhoot jaate the).
        trk["prev_inhand"] = {p for p in posmap.values() if p}
        trk["prev_hta"] = act.get("hero_to_act")
        trk["prev_to_call"] = act.get("to_call", 0.0)
        _check_pot(trk, act)
        return
    _check_pot(trk, act)
    if trk.get("done"):
        return
    street = "preflop" if act.get("street") == "preflop" else (act.get("streetName") or None)
    if street is None:
        return                       # postflop frame jisme board padha nahi — skip (galat street mat banao)
    age = time.time() - trk.get("t0", 0)
    if age < 2.0 and street != "preflop":
        return                       # naye hand me pichle hand ka board abhi screen pe hai — stale ignore
    # sit-out player baad me posmap se hat gaya (pichle hand ki file der se aayi) — uska
    # phantom fold mat gino
    _remap_positions(trk, posmap)
    live_pos = {p for p in posmap.values() if p}
    if live_pos:
        trk["prev_inhand"] &= live_pos
    trk.setdefault("allin", set()).update(act.get("allin") or ())
    if street != trk["street"]:
        still = set(trk.get("alive") or ())
        if len(still) >= 2 and len(still - trk["allin"]) <= 1:
            trk["runout"] = True      # sab (ya ek ko chhod ke sab) all-in: aage koi action nahi
        pend = trk.get("hero_pending")
        if pend and pend["to_call"] <= 0.02 and act.get("hero") \
                and not (trk.get("runout") or trk.get("showdown")):
            trk["line"].setdefault(trk["street"], []).append({"pos": act["hero"], "act": "check"})
        trk["hero_pending"] = None
        if STREETS.index(street) < STREETS.index(trk["street"]):
            # board gayab = hand khatam. River pe koi bet baaki nahi tha to jo bhi abhi tak hand
            # me tha aur river pe kuch nahi kiya, usne check kiya (aakhri player ka check-behind:
            # cards aur board ek saath hat-te hain, koi aur signal nahi milta). Iske baad is
            # hand ki line band — agle hand ke shuruaati frames isme kuch na jodein.
            # 3 frame lagataar ho tabhi (ek frame ka popup / board flicker hand band na kare),
            # aur hand sach me shuru hua ho (preflop events hain) — naye hand ke shuru me
            # pichle hand ka board der tak dikhe to use hand-end mat samjho.
            if trk["line"].get("preflop"):
                trk["back_n"] = trk.get("back_n", 0) + 1
                if trk["back_n"] >= 3:
                    for q in list(trk.get("pend_out") or {}):
                        _pend_final(trk, q)
                    _finish_hand(trk)
                    trk["done"] = True
                return
        trk["back_n"] = 0
        if STREETS.index(street) > STREETS.index(trk["street"]):   # aage badhe tabhi (hand-end pe nahi)
            _close_street(trk, card_backs, posmap, act)
        trk["hw"] = {}
        trk["street"] = street
        trk["prev_bets"] = {}
        trk["prev_inhand"] = set()
        trk["prev_hta"] = None
        trk["prev_to_call"] = 0.0
        trk["check_pending"] = None
        trk["line"].setdefault(street, [])
    trk["back_n"] = 0
    line = trk["line"].setdefault(street, [])
    bets = act.get("bets") or {}
    if not bets and trk["prev_bets"] and (act.get("pot_raw") or 0.0) <= 0:
        # bets + pot dono ek saath gayab: table amounts BB ki jagah currency (EUR) me dikha raha
        # hai (ya popup) — yeh frame padha nahi ja sakta. State mat chhedo, warna wahi bet
        # wapas dikhne pe dobara "bet" lag jaata tha.
        return
    hw = trk.setdefault("hw", {})
    # Baseline = is street me ab tak ka sabse bada bet (street ke andar bet kabhi ghatta nahi).
    # Sirf pichle frame se compare karne par, label ek frame gayab hoke wapas aaye (popup ne
    # dhaka) to wahi bet dobara gin liya jaata tha ("BB:bb" do baar).
    old_bets = dict(trk["prev_bets"])
    for p, v in hw.items():
        if v > old_bets.get(p, 0.0):
            old_bets[p] = v
    for p, amt in bets.items():
        hw[p] = max(hw.get(p, 0.0), amt)
    hero = act.get("hero")
    # folds — jinke card backs gayab ho gaye
    inhand = set()
    for seat in card_backs:
        p = posmap.get(seat)
        if p:
            inhand.add(p)
    if hero:
        inhand.add(hero)
    gone = {e["pos"] for evs in trk["line"].values() for e in evs if e.get("act") == "fold"}
    facing = max(hw.values()) if hw else 0.0
    opp = inhand - {hero}
    if opp:                           # aakhri baar jab opponents ke backs dikhe — kaun hand me tha
        trk["alive"] = (opp | ({hero} if hero and trk.get("hero_had") else set())) - gone
    # pend_out: jinke card backs bet ke saamne gayab hue — FOLD ya ALL-IN CALL (cards khul gaye)?
    # Screen pe dono ek jaise dikhte hain. Faisla pot se: chips collect hone pe pot badha to
    # call, nahi badha to fold. Tab tak pending (backs wapas dikhen = flicker tha, cancel).
    # Line me turant FOLD likho ("pend" nishaan ke saath) — pot badha to wahi event call ban jaata hai.
    pend = trk.setdefault("pend_out", {})
    for p in [q for q in pend if q in trk.get("allin", ())]:
        _pend_drop(trk, p)                           # all-in tha: cards khule hain, fold nahi
        gone.discard(p)
        trk["showdown"] = True
    for p in [q for q, t0 in pend.items() if time.time() - t0 >= 2.5]:
        _pend_final(trk, p)                          # pot se kuch nahi aaya = fold pakka
    if age >= 1.5:                    # deal ka animation settle hone se pehle folds mat gino
        vanished = sorted(trk["prev_inhand"] - inhand)
        for p in vanished:
            if p in gone:
                continue              # ek player ek hi baar fold karta hai (card-back flicker)
            if facing > 0.02:
                if facing - hw.get(p, 0.0) > 0.02:
                    line.append({"pos": p, "act": "fold", "pend": True})
                    pend[p] = time.time()
                    gone.add(p)
                    if gc in _allin_state:
                        _allin_state[gc]["street"] = None    # ALL-IN plaque abhi dobara padho
                        _allin_state[gc]["asked"] = True
                elif any(e["pos"] == p and e.get("act") in ("bet", "raise") + RAISE_ACTS
                         for e in line):
                    trk["showdown"] = True       # sabse bade bet wale ke cards khule / haath khatam
                elif (inhand - {p}) - gone:
                    # sirf blind lagaya tha aur koi aur abhi hand me hai: raise ka label agle frame
                    # me aayega (card backs ek frame aage chalte hain) — yeh fold hi hai
                    line.append({"pos": p, "act": "fold", "pend": True})
                    pend[p] = time.time()
                    gone.add(p)
                continue
            if street != "preflop" and facing <= 0.02:
                # saamne koi bet nahi = fold nahi. River pe iska matlab showdown hai (cards
                # khul gaye): jis-jis ne river pe abhi tak kuch nahi kiya usne check kiya
                # (aakhri player ka check-behind kisi aur signal se nahi dikhta).
                if street == "river":
                    for q in sorted((trk["prev_inhand"] | {hero}) - gone - {None},
                                    key=lambda x: POS_ORDER.index(x) if x in POS_ORDER else 99):
                        if q == hero and not trk.get("hero_had"):
                            continue
                        if not any(e["pos"] == q for e in line):
                            line.append({"pos": q, "act": "check"})
                continue
            # (preflop + koi bet nahi = chips collect ho chuke, haath khatam: jeetne wale ke
            #  cards hat rahe hain — yeh fold nahi hai)
        trk["prev_inhand"] = inhand    # folds gin liye — ab current in-hand update karo
    # HERO ka fold: cards 2 frame se gayab + saamne bet hai. Buttons ka transition zaroori nahi
    # (pre-select "Fold" checkbox se hero ki baari ka frame aata hi nahi).
    if act.get("hero_cards"):
        trk["hero_had"], trk["hero_gone"] = True, 0
    elif hero:
        trk["hero_gone"] = trk.get("hero_gone", 0) + 1
        if (trk["hero_gone"] >= 2 and trk.get("hero_had") and hero not in gone
                and facing - hw.get(hero, 0.0) > 0.02):
            line.append({"pos": hero, "act": "fold"})
            gone.add(hero)
            trk["hero_pending"] = None
    # opponents ne hero ko action de diya (hta False->True) bina bet ke => check.
    # 2-frame stable chahiye — bet amount der se dikhta hai (pehle frame pe to_call=0 hota hai
    # jabki villain ne bet kiya hota hai), isliye pehle frame pe check add karne se
    # "BB check, BB bet" jaisa galat line banta tha.
    hta = act.get("hero_to_act")
    quiet = bool(trk.get("runout") or trk.get("showdown") or trk.get("river_closed"))
    if quiet:
        trk["check_pending"] = None
    elif hta and street != "preflop" and act.get("to_call", 0.0) <= 0.02:
        if trk["prev_hta"] is False:
            # sirf wahi jo hero se PEHLE bolte hain — hero ke baad wale ne abhi kuch nahi kiya
            # (3-way pot me hero ke baad wale player pe phantom check lagta tha)
            po = ["BB", "SB"] if set(posmap.values()) <= {"SB", "BB"} else POS_ORDER
            hi = po.index(hero) if hero in po else len(po)
            trk["check_pending"] = [x for x in po[:hi] if x in inhand and x != hero and x not in gone]
        elif trk.get("check_pending"):
            for p in trk["check_pending"]:
                if not any(e["pos"] == p for e in line):
                    line.append({"pos": p, "act": "check"})
            trk["check_pending"] = None
    else:
        trk["check_pending"] = None
    # bet deltas — position order me classify karo (old_bets snapshot se, taaki same frame
    # ke andar ki updates ek dusre ko corrupt na karein)
    allin_call = bool(act.get("call_allin"))
    for p in sorted(bets, key=lambda x: POS_ORDER.index(x) if x in POS_ORDER else 99):
        amt = bets[p]
        prev = old_bets.get(p, 0.0)
        d = amt - prev
        if quiet:
            continue          # runout / river band: yeh jeetne wale ko jaate chips hain, bet nahi
        if d > 0.02:
            others = max([v for q, v in old_bets.items() if q != p] or [0.0])
            ai = d >= ALLIN_JUMP            # itna bada ek-jump = all-in
            if street == "preflop":
                if prev <= 0.01 and p in _dead_posts(st, posmap) \
                        and not any(e["pos"] == p and e["act"] == "post" for e in line):
                    # dead blind (naya / wapas aaya player) — limp ya open nahi
                    evt = {"pos": p, "act": "post", "amt": round(d, 2)}
                elif p in ("SB", "BB") and amt <= 1.05 and prev <= 0.01:
                    evt = {"pos": p, "act": "sb" if p == "SB" else "bb", "amt": round(d, 2)}
                elif amt <= 1.05 and others <= 1.05:
                    evt = {"pos": p, "act": "limp" if p not in ("SB", "BB") else "call",
                           "amt": round(d, 2)}
                elif amt > others + 0.02:
                    # raise level: pehla=open, doosra=3bet, teesra=4bet...
                    nraises = sum(1 for e in line if e.get("act") in RAISE_ACTS)
                    evt = {"pos": p, "act": RAISE_NAMES[min(nraises, len(RAISE_NAMES) - 1)],
                           "amt": round(d, 2)}
                else:
                    evt = {"pos": p, "act": "call", "amt": round(d, 2)}
            else:
                if others <= 0.02:
                    evt = {"pos": p, "act": "bet", "amt": round(d, 2)}
                    # bettor se PEHLE act karne wale (abhi bhi hand me) ne check kiya hoga —
                    # hero hand me na ho to yeh checks kisi aur signal se nahi milte
                    # heads-up me postflop BB pehle bolta hai, SB (button) baad me
                    order = ["BB", "SB"] if set(posmap.values()) <= {"SB", "BB"} else POS_ORDER
                    if p in order and not any(e.get("act") == "bet" for e in line):
                        gone = {e["pos"] for evs in trk["line"].values() for e in evs
                                if e.get("act") == "fold"}
                        for q in order[:order.index(p)]:
                            if (q in inhand and q != hero and q not in gone
                                    and not any(e["pos"] == q for e in line)):
                                line.append({"pos": q, "act": "check"})
                elif amt > others + 0.02:
                    evt = {"pos": p, "act": "raise", "amt": round(d, 2)}
                else:
                    evt = {"pos": p, "act": "call", "amt": round(d, 2)}
            if evt["act"] == "open":
                _poster_checks(line, before=p)
            if ai or (p == hero and allin_call and evt["act"] == "call"):
                evt["allin"] = True
            for evs in trk["line"].values():      # fold ke baad bet = woh fold flicker tha
                evs[:] = [e for e in evs if not (e["pos"] == p and e.get("act") == "fold")]
            (trk.get("pend_out") or {}).pop(p, None)
            line.append(evt)
    # hero ne act kiya (hta True->False) bina bet badhaye => check ya fold.
    # Fold ka pakka signal: hero ke cards screen se GAYAB ho gaye (cards wapas dikhe to
    # 3-bet/call hai, fold nahi — isliye pehle yahan galat 'fold' lagta tha).
    # Faisla usi frame pe mat karo: buttons gayab hone ke EK FRAME BAAD bet ka label aata hai
    # (pehle "check" + phir "bet" dono lag jaate the) aur fold pe cards bhi ek frame baad
    # gayab hote hain (fold chhoot jaata tha). Isliye pending rakho aur agle frames me tay karo.
    now = time.time()
    if trk["prev_hta"] is True and hta is False and hero:
        trk["hero_pending"] = {"t": now, "bet": old_bets.get(hero, 0.0),
                               "to_call": trk["prev_to_call"], "gone": 0}
    pend = trk.get("hero_pending")
    if pend and hero:
        if bets.get(hero, 0.0) > pend["bet"] + 0.02:
            trk["hero_pending"] = None            # bet/call/raise — upar delta se line me aa gaya
        elif pend["to_call"] > 0.02:
            if now - pend["t"] > 2.5:
                trk["hero_pending"] = None        # fold/call upar ya street close pe tay hota hai
        elif now - pend["t"] >= 0.7 or hta:
            if not quiet and not any(e["pos"] == hero and e.get("act") == "check" for e in line):
                line.append({"pos": hero, "act": "check"})
            trk["hero_pending"] = None
    # chips pot me chale gaye (saare bet labels gayab) — street ke adhoore calls abhi jodo.
    # Pot badha ho tabhi: sab fold hue to uncalled bet wapas jaata hai aur pot nahi badhta
    # (card back ek frame der se gayab ho to phantom call na lage).
    # jaancha hua pot (act["pot"]): galat padha pot (131.75 -> 431.78) "pot nahi badha" dikha
    # ke hero ke all-in call ko fold bana deta tha
    pot = (act.get("pot") or 0.0) if (act.get("pot_raw") or 0.0) > 0 else 0.0
    if not bets and hw and pot > 0 and trk.get("prev_pot", 0) > 0:
        gain = pot - trk["prev_pot"]
        _close_street(trk, card_backs, posmap, act, gain=gain)
        if street == "river":
            trk["river_closed"] = True
        if (gain <= 0.02 and hero and trk.get("hero_had") and hero not in gone
                and max(hw.values()) - hw.get(hero, 0.0) > 0.02):
            # pot nahi badha = bet uncalled wapas gaya. Hero ke cards fold ke baad bhi screen
            # pe (dim) rehte hain, isliye hero ka fold yahin se pakdo.
            line.append({"pos": hero, "act": "fold"})
        trk["hw"] = {}
    if bets and (act.get("pot") or 0.0) > 0:
        trk["prev_pot"] = act["pot"]   # aakhri (jaancha hua) pot jab bets table pe the
    if not bets and not trk.get("pend_out") and trk["line"].get("preflop"):
        dealt = {q for q in posmap.values() if q}
        out = {e["pos"] for evs in trk["line"].values() for e in evs if e.get("act") == "fold"}
        if len(dealt) >= 2 and len(dealt - out) <= 1:
            trk["done"] = True
    trk["prev_hta"] = hta
    trk["prev_to_call"] = act.get("to_call", 0.0)
    trk["prev_bets"] = bets
    # prev_inhand upar age>=1.5 block me update hota hai (early folds miss na hon isliye)


def _ocr_name(crop):
    """Plaque se naam OCR karo (best-effort). Empty matlab naam nahi mila."""
    if not HAS_TESS:
        return ""
    g = crop.convert("L")
    for im in (g, ImageOps.invert(g)):
        bw = im.point(lambda p: 255 if p > 150 else 0)
        try:
            t = pytesseract.image_to_string(bw, config="--psm 7").strip()
            t = "".join(c for c in t if c.isalnum() or c in "._-")
            if 2 <= len(t) <= 15 and not t.isdigit():
                return t
        except Exception:
            pass
    return ""


def detect_names(img):
    """{seat_index: naam} — sirf jahan OCR kuch padh paya."""
    if not HAS_TESS or not img:
        return {}
    w, h = img.size
    out = {}
    for i, box in enumerate(NAME_REGIONS):
        if box is None:
            continue
        x0, y0, x1, y1 = box
        try:
            nm = _ocr_name(img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))))
            if nm:
                out[i] = nm
        except Exception:
            pass
    return out


def _hero_region_key(img):
    """Hero cards wale area ka chhota hash — cards same rahe to dobara OCR skip karo."""
    try:
        w, h = img.size
        x, y, rw, rh = (0.35, 0.62, 0.30, 0.20)
        crop = img.crop((int(x * w), int(y * h), int((x + rw) * w), int((y + rh) * h)))
        crop = crop.resize((64, 32), Image.BILINEAR)
        return crop.tobytes()
    except Exception:
        return None


# ---------------- ground-truth auto-labelling (Red Star) ----------------
# Live hand ke cards client kahin readable nahi rakhta — sirf hand KHATAM hone pe
# TempData\...\Tables\<table>\<gamecode> binary file likhta hai (exact cards).
# Isliye har frame ke rank glyphs us waqt ke gamecode ke naam yaad rakhte hain, aur
# file aate hi unhe EXACT rank se template bana dete hain. Har hand ke baad OCR aur
# pakka hota jaata hai — galat padhe glyph (3->A) bhi sahi ho jaate hain.
TRUTH_SETTLE_SECS = 1.5    # naya gamecode aane ke baad itni der purane cards screen pe reh sakte hain
TRUTH_MIN_FRAMES = 5       # itne frames me same glyphs dikhe tabhi label (animation/transition nahi)
# pending[gc] = {"tbl": Path, "t": time, "hero": {obs: n}, "board": {obs: n}}
# since[gc] = gamecode pehli baar kab dekha (settle guard ke liye)
# gc_by_table[title] = current gamecode (multi-table truth)
# verified[gc] = kab verify hua — usi hand ke late frames pending dobara na banayein (double count)
_truth = {"pending": {}, "since": {}, "gc_by_table": {}, "verified": {}}
_truth_lock = threading.Lock()


def _record_obs(kind, obs, gc, tbl):
    """Current frame ke (glyph_key, suit) tuples ko is table ke gamecode ke saath gino.
    gc/tbl action_loop se aate hain (per-table) — multi-table me bhi learning chalti rahegi."""
    if not HAS_REDSTAR or not gc:
        return
    with _truth_lock:
        if gc in _truth["verified"]:
            return
        if time.time() - _truth["since"].get(gc, 0) < TRUTH_SETTLE_SECS:
            return
        p = _truth["pending"].setdefault(gc, {"tbl": tbl, "t": time.time(),
                                              "hero": {}, "board": {},
                                              "line": None, "pos": None, "pot": None})
        p[kind][obs] = p[kind].get(obs, 0) + 1


def _hero_seat(st):
    """Live XML state se hero ka SEAT number (DONE_DATA anonymized me hero pehchanne ke liye)."""
    if not st:
        return None
    names = st.get("names") or []
    seats = st.get("seats") or []
    if redstar_hh.HERO in names:
        i = names.index(redstar_hh.HERO)
        return seats[i] if i < len(seats) else None
    return None


def _record_hand(gc, tbl, line, hero, pot, hero_seat=None):
    """Hand ke OCR meta (action line + position + pot) record karo — hand khatam pe XML truth se
    compare hoga. Latest frame hi jeet-ta hai (line aage badhti rehti hai)."""
    if not gc:
        return
    with _truth_lock:
        if gc in _truth["verified"]:
            return                    # hand verify ho chuka — der se aaya frame dobara na ginwaye
        p = _truth["pending"].setdefault(gc, {"tbl": tbl, "t": time.time(),
                                              "hero": {}, "board": {},
                                              "line": None, "pos": None, "pot": None})
        if line:
            p["line"] = {k: [dict(e) for e in v] for k, v in line.items() if v}
        if hero:
            p["pos"] = hero
        if pot:
            # MAX pot rakho (hand ke beech me pot badhta rehta hai; last frame ka pot
            # final pot se kam ho sakta hai — max hi final ke kareeb hota hai)
            p["pot"] = max(p.get("pot") or 0, pot)
        if hero_seat is not None:
            p["hero_seat"] = hero_seat


def _label_cards(obs, truth):
    """obs [(key, suit, suit_feat)] ko truth ['Td', '3c'] se label karo (rank)."""
    if len(obs) != len(truth):
        return 0
    return sum(1 for (k, _, _), c in zip(obs, truth) if add_rank_template(c[0], k))


def _label_suits(obs, truth):
    """obs ke suit features ko exact suits se label karo (hero cards ke faint symbols seekhte hain)."""
    if len(obs) != len(truth):
        return 0
    return sum(1 for (_, _, sk), c in zip(obs, truth) if _learn_suit(sk, c[1]))


def _learn_truth(p, hh):
    """Hand file ke exact cards se recorded glyphs ko label karo (rank + suit dono).
    Order decide: suit match pehle, warna rank-template match se (suit galat hone par bhi sahi order)."""
    added = 0
    hero = hh.get("hero") or []
    votes = sorted(p["hero"].items(), key=lambda kv: -kv[1])
    if len(hero) == 2 and votes and votes[0][1] >= TRUTH_MIN_FRAMES:
        obs = votes[0][0]
        if len(obs) == 2:
            suit_fwd = [s == c[1] for (_, s, _), c in zip(obs, hero)]
            rank_fwd = sum(1 for (k, _, _), c in zip(obs, hero) if _match_template(k) == c[0])
            rank_rev = sum(1 for (k, _, _), c in zip(reversed(obs), hero) if _match_template(k) == c[0])
            if all(suit_fwd):
                added += _label_cards(obs, hero) + _label_suits(obs, hero)
            elif suit_fwd[0] and not suit_fwd[1]:
                added += _label_cards(obs, hero) + _label_suits(obs, hero)
            elif suit_fwd[1] and not suit_fwd[0]:
                added += _label_cards(list(reversed(obs)), hero) + _label_suits(list(reversed(obs)), hero)
            elif rank_rev > rank_fwd:
                added += _label_cards(list(reversed(obs)), hero) + _label_suits(list(reversed(obs)), hero)
            else:
                added += _label_cards(obs, hero) + _label_suits(obs, hero)
    board = hh.get("board") or []
    for obs, n in p["board"].items():
        if n >= TRUTH_MIN_FRAMES and board:
            added += _label_cards(obs, board)
    return added


def verify_hand_accuracy(p, hh):
    """Hand khatam hone pe OCR padha hua vs XML truth — mismatch count + card accuracy update.
    _learn_truth ne abhi galat glyph ke sahi template bana diye hain, isliye agla hand sahi padhega."""
    hero = hh.get("hero") or []
    board = hh.get("board") or []
    res = {"hero": "".join(hero) if len(hero) == 2 else None, "board": board,
           "hero_ocr": None, "hero_ok": None, "board_ok": None, "mismatch": 0}
    votes = sorted(p["hero"].items(), key=lambda kv: -kv[1])
    if len(hero) == 2 and votes and votes[0][1] >= TRUTH_MIN_FRAMES:
        obs = votes[0][0]
        pred = "".join((_match_template(k) or "?") + s for (k, s, _) in obs)
        res["hero_ocr"] = pred
        truth = "".join(hero)
        res["hero_ok"] = pred == truth
        if pred != truth:
            res["mismatch"] += 1
    if board:
        # board ke recorded glyphs me se sabse zyada frame wala use karo
        bvotes = sorted(p["board"].items(), key=lambda kv: -kv[1])
        if bvotes and bvotes[0][1] >= TRUTH_MIN_FRAMES:
            pred = "".join((_match_template(k) or "?") + s for (k, s, _) in bvotes[0][0])
            res["board_ocr"] = pred
            # Sabse zyada frames wala observation aksar FLOP hota hai (sabse der wahi dikhta
            # hai) jabki truth poora 5-card board hai — pehle har aisa hand "galat" gina jaata
            # tha. Jitne cards padhe utne hi truth ke shuru se milao.
            truth = "".join(board)[:len(pred)] if len(pred) >= 6 else "".join(board)
            res["board_ok"] = pred == truth
            if pred != truth:
                res["mismatch"] += 1
    if res["hero_ok"] is not None:
        _acc_bump("card_accuracy", res["hero_ok"])
    if res.get("board_ok") is not None:
        _acc_bump("card_accuracy", res["board_ok"])
    return res


# ---------------- line / position / pot verification (XML truth) ----------------
_mismatch_log = []
_mismatch_lock = threading.Lock()


def _log_mismatch(gc, kind, ocr, truth):
    """Mismatch persist karo — isi se pata chalta hai code bug hai ya training gap."""
    rec = {"t": round(time.time(), 1), "gc": gc, "kind": kind, "ocr": ocr, "truth": truth}
    with _mismatch_lock:
        _mismatch_log.append(rec)
        if len(_mismatch_log) > 200:
            _mismatch_log[:] = _mismatch_log[-200:]
        try:
            base = os.environ.get("PREFLOP_ROOT") or os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(base, "mismatch_log.json"), "w", encoding="utf-8") as f:
                json.dump(_mismatch_log, f, ensure_ascii=False, indent=1)
        except Exception:
            pass


def _xml_players(body):
    # DONE_DATA me players hamesha seat order me nahi hote (kabhi ek player list ke END me
    # likha hota hai) — positions clockwise order se banti hain, isliye seat se sort karo.
    rows = []
    for ptag in re.findall(r"<player\s+([^>]+)/>", body):
        nm = re.search(r'name="([^"]+)"', ptag)
        if nm:
            st = re.search(r'seat="(\d+)"', ptag)
            rows.append((int(st.group(1)) if st else 99, nm.group(1), 'dealer="1"' in ptag))
    rows.sort(key=lambda r: r[0])
    names = [r[1] for r in rows]
    dealer = next((i for i, r in enumerate(rows) if r[2]), 0)
    return names, dealer


def _xml_round(body, no):
    r = re.search(r'<round no="%d">(.*?)</round>' % no, body, re.S)
    return re.findall(r'player="([^"]+)" sum="([^"]*)" type="(\d+)"', r.group(1)) if r else []


def _xml_positions(body):
    names, dealer = _xml_players(body)
    acts0 = _xml_round(body, 0)
    return redstar_hh.dealt_positions({"names": names, "dealer_idx": dealer,
                                       "rounds": {0: [(nm, t, v) for nm, v, t in acts0]}})


def _xml_action_line(tbl, gc):
    """XML se poora action line (real names wali session XML) — tracker line jaisa format."""
    body = _xml_game_body(tbl, gc)
    if not body:
        return None
    money = lambda v: float(re.search(r"[\d.]+", v).group()) if re.search(r"[\d.]+", v) else 0.0
    pos = _xml_positions(body)
    acts0 = _xml_round(body, 0)
    bbv = next((money(v) for _, v, t in acts0 if t == "2"), 0) or 0.02
    line = {"preflop": [], "flop": [], "turn": [], "river": []}
    # round 0 = blinds (pehla type-1/2 hi asli SB/BB; baaki dead blind post hain)
    seen_blind = set()
    for n, v, t in acts0:
        if t in ("1", "2"):
            act = ("sb" if t == "1" else "bb") if t not in seen_blind else "post"
            seen_blind.add(t)
            line["preflop"].append({"pos": pos.get(n, "?"), "act": act, "amt": round(money(v) / bbv, 2)})
    # preflop (round 1)
    put = {n: money(v) / bbv for n, v, t in acts0}
    for n, v, t in _xml_round(body, 1):
        p = pos.get(n, "?")
        if t == "0":
            line["preflop"].append({"pos": p, "act": "fold"})
        elif t == "4":
            line["preflop"].append({"pos": p, "act": "check"})
        elif t == "23":
            amt = money(v) / bbv - put.get(n, 0)
            nraises = sum(1 for e in line["preflop"] if e.get("act") in RAISE_ACTS)
            line["preflop"].append({"pos": p, "act": RAISE_NAMES[min(nraises, len(RAISE_NAMES) - 1)],
                                    "amt": round(amt, 2)})
            put[n] = money(v) / bbv
        else:  # 3 = call, 7 = all-in increment
            amt = money(v) / bbv
            top = max(put.values()) if put else 0.0
            if put.get(n, 0) + amt > top + 0.02 and t != "3":      # all-in shove = raise
                nraises = sum(1 for e in line["preflop"] if e.get("act") in RAISE_ACTS)
                line["preflop"].append({"pos": p, "act": RAISE_NAMES[min(nraises, len(RAISE_NAMES) - 1)],
                                        "amt": round(amt, 2)})
            else:
                line["preflop"].append({"pos": p, "act": "call", "amt": round(amt, 2)})
            put[n] = put.get(n, 0) + amt
    # postflop rounds
    for rno, sname in ((2, "flop"), (3, "turn"), (4, "river")):
        acts = _xml_round(body, rno)
        if not acts:
            continue
        put = {}
        for n, v, t in acts:
            p = pos.get(n, "?")
            if t == "0":
                line[sname].append({"pos": p, "act": "fold"})
            elif t == "4":
                line[sname].append({"pos": p, "act": "check"})
            elif t == "23":
                amt = money(v) / bbv - put.get(n, 0)
                line[sname].append({"pos": p, "act": "raise", "amt": round(amt, 2)})
                put[n] = money(v) / bbv
            elif t == "5":
                amt = money(v) / bbv
                line[sname].append({"pos": p, "act": "bet", "amt": round(amt, 2)})
                put[n] = put.get(n, 0) + amt
            else:  # 3 = call, 7 = all-in
                amt = money(v) / bbv
                top = max(put.values()) if put else 0.0
                if put.get(n, 0) + amt > top + 0.02 and t != "3":  # all-in shove = bet / raise
                    line[sname].append({"pos": p, "act": "raise" if top > 0.02 else "bet",
                                        "amt": round(amt, 2)})
                else:
                    line[sname].append({"pos": p, "act": "call", "amt": round(amt, 2)})
                put[n] = put.get(n, 0) + amt
    return line


def _line_from_st(st):
    """LIVE XML state (rounds) se poora action line — folds/checks/calls 100% accurate.
    Red Star opponents ke card backs render nahi karta, isliye OCR se folds kabhi padh nahi
    paate the (line hamesha adhoori, isliye line_accuracy 0% hi rehta tha). XML me har action
    (fold/check/call/bet/raise) live likha hota hai — yahi asli source hai."""
    if not st or not st.get("rounds"):
        return None
    try:
        pos = redstar_hh.dealt_positions(st)
    except Exception:
        pos = {}
    if not pos:
        return None
    money = lambda v: float(re.search(r"[\d.]+", v).group()) if re.search(r"[\d.]+", v) else 0.0
    acts0 = st["rounds"].get(0, [])
    bbv = next((money(v) for _, t, v in acts0 if t == "2"), 0) or 0.02
    line = {"preflop": []}
    for nm, t, v in acts0:
        p = pos.get(nm, "?")
        if t == "1":
            line["preflop"].append({"pos": p, "act": "sb", "amt": round(money(v) / bbv, 2)})
        elif t == "2":
            line["preflop"].append({"pos": p, "act": "bb", "amt": round(money(v) / bbv, 2)})
    put = {nm: money(v) / bbv for nm, _, v in acts0}
    for rno, sname in ((1, "preflop"), (2, "flop"), (3, "turn"), (4, "river")):
        acts = st["rounds"].get(rno, [])
        if not acts:
            continue
        if sname != "preflop":
            line.setdefault(sname, [])
            put = {}
        for nm, t, v in acts:
            p = pos.get(nm, "?")
            amt = money(v) / bbv
            if t == "0":
                line[sname].append({"pos": p, "act": "fold"})
            elif t == "4":
                line[sname].append({"pos": p, "act": "check"})
            elif t == "23":
                if sname == "preflop":
                    nraises = sum(1 for e in line["preflop"] if e.get("act") in RAISE_ACTS)
                    line["preflop"].append({"pos": p,
                                            "act": RAISE_NAMES[min(nraises, len(RAISE_NAMES) - 1)],
                                            "amt": round(amt - put.get(nm, 0), 2)})
                else:
                    line[sname].append({"pos": p, "act": "raise", "amt": round(amt - put.get(nm, 0), 2)})
                put[nm] = amt
            elif t == "5":
                line[sname].append({"pos": p, "act": "bet", "amt": round(amt, 2)})
                put[nm] = put.get(nm, 0) + amt
            else:  # 3 = call / all-in increment
                line[sname].append({"pos": p, "act": "call", "amt": round(amt, 2)})
                put[nm] = put.get(nm, 0) + amt
    return {k: v for k, v in line.items() if v}


def _xml_facing(st):
    """LIVE XML se hero ka current-street to_call (BB) — flop/turn/river pe exact.
    OCR button misread (CALL 9BB ko CHECK padh lena) se solver ko facing=0 mil jaata tha."""
    if not st or not st.get("rounds"):
        return None
    rnos = sorted(st["rounds"].keys())
    if not rnos or rnos[-1] < 2:
        return None
    try:
        pos = redstar_hh.dealt_positions(st)
    except Exception:
        pos = {}
    hero_pos = pos.get(redstar_hh.HERO)
    if not hero_pos:
        return None
    money = lambda v: float(re.search(r"[\d.]+", v).group()) if re.search(r"[\d.]+", v) else 0.0
    acts0 = st["rounds"].get(0, [])
    bbv = next((money(v) for _, t, v in acts0 if t == "2"), 0) or 0.02
    put = {}
    for nm, t, v in st["rounds"][rnos[-1]]:
        p = pos.get(nm)
        if not p:
            continue
        amt = money(v) / bbv
        if t == "23":
            put[p] = amt                        # raise ka sum = street TOTAL
        elif t in ("5", "3"):
            put[p] = put.get(p, 0.0) + amt      # bet/call ka sum = increment
    mx = max(put.values()) if put else 0.0
    return round(mx - put.get(hero_pos, 0.0), 2)


def _xml_pot(tbl, gc):
    """XML se final pot (BB) — players ke bet attributes ka sum."""
    body = _xml_game_body(tbl, gc)
    if not body:
        return None
    money = lambda v: float(re.search(r"[\d.]+", v).group()) if re.search(r"[\d.]+", v) else 0.0
    total = 0.0
    for ptag in re.findall(r"<player\s+([^>]+)/>", body):
        bm = re.search(r'bet="([^"]*)"', ptag)
        if bm:
            total += money(bm.group(1))
    acts0 = _xml_round(body, 0)
    bbv = next((money(v) for _, v, t in acts0 if t == "2"), 0) or 0.02
    return round(total / bbv, 2)


def _xml_hero_pos(tbl, gc, hero_seat=None):
    body = _xml_game_body(tbl, gc)
    if not body:
        return None
    pos = _xml_positions(body)
    if redstar_hh.HERO in pos:
        return pos[redstar_hh.HERO]
    if hero_seat is not None:
        return pos.get(f"Player {hero_seat}")
    return None


def _line_signature(line):
    """Line ko (pos, class) multiset me badlo — order/amt ke chhote farak se bachne ke liye.
    open/3bet/4bet sab 'raise' class me (naming fark se false mismatch na ho)."""
    sig = {}
    for street, evs in (line or {}).items():
        for e in evs:
            cls = e.get("act")
            if cls in RAISE_ACTS:
                cls = "raise"
            elif cls == "limp":
                cls = "call"          # XML limp ko bhi 'call' hi likhta hai
            key = (e.get("pos"), cls)
            sig[key] = sig.get(key, 0) + 1
    return sig


def verify_hand_full(gc, p, hh, tbl):
    """Hand khatam: OCR (cards+line+pos+pot) vs XML truth — mismatch log + accuracy meters."""
    res = verify_hand_accuracy(p, hh)
    if res.get("hero_ok") is False:
        _log_mismatch(gc, "hero_cards", res.get("hero_ocr"), res.get("hero"))
    if res.get("board_ok") is False:
        _log_mismatch(gc, "board", res.get("board_ocr"), "".join(res.get("board") or []))
    xml_line = _xml_action_line(tbl, gc)
    ocr_line = p.get("line")
    if xml_line and ocr_line:
        ok = _line_signature(ocr_line) == _line_signature(xml_line)
        res["line_ok"] = ok
        if not ok:
            res["mismatch"] += 1
            _log_mismatch(gc, "line", ocr_line, xml_line)
    xml_pos = _xml_hero_pos(tbl, gc, p.get("hero_seat"))
    if xml_pos and p.get("pos"):
        ok = p["pos"] == xml_pos
        res["pos_ok"] = ok
        if not ok:
            res["mismatch"] += 1
            _log_mismatch(gc, "pos", p["pos"], xml_pos)
    xml_pot = _xml_pot(tbl, gc)
    if xml_pot and p.get("pot"):
        ok = abs(p["pot"] - xml_pot) <= max(0.5, xml_pot * 0.1)
        res["pot_ok"] = ok
        if not ok:
            res["mismatch"] += 1
            _log_mismatch(gc, "pot", p["pot"], xml_pot)
    for key in ("line_ok", "pos_ok", "pot_ok"):
        v = res.get(key)
        if v is not None:
            _acc_bump(key + "_accuracy", v)
    return res


HAND_SETTLE_SECS = 1.0     # naye gamecode ke baad itni der pichle hand ke cards screen pe reh sakte hain
# cards itni der lagataar gayab rahen tabhi "koi hand nahi" dikhao. Showdown / jeet ke overlay
# cards ko 1-4 sec dhak dete hain (hand -> khaali -> wahi hand ka flick); fold ke baad hand
# bas itni der aur dikhta hai, phir saaf.
HAND_NONE_SECS = 5.0


def _stable_hand(t, raw, gc):
    """Hero hand ko flicker se bachao. Ek gamecode (hand) ke andar hero ke cards badal nahi
    sakte — isliye is hand me sabse zyada frames me padha gaya hand hi dikhao. Jeet ka
    '+2.50 BB' overlay cards pe chadh ke rank galat padhwa deta tha (Td9d -> Td4d -> Td9d) aur
    ek-do frame ke liye cards 'gayab' ho jaate the (hand -> khaali -> wahi hand).
    Gamecode na ho (Red Star nahi) to raw read jaisa ka taisa."""
    now = time.time()
    if not gc:
        return raw
    hs = t.get("hand_state")
    if not hs or hs["gc"] != gc:
        hs = t["hand_state"] = {"gc": gc, "t0": now, "votes": {}, "none_since": None, "shown": None}
    if raw:
        hs["none_since"] = None
        if now - hs["t0"] < HAND_SETTLE_SECS:
            hs["shown"] = raw                    # settle: abhi vote nahi (pichle hand ke cards ho sakte hain)
        else:
            hs["votes"][raw] = hs["votes"].get(raw, 0) + 1
            hs["shown"] = max(hs["votes"], key=hs["votes"].get)
    else:
        if hs["none_since"] is None:
            hs["none_since"] = now
        if now - hs["none_since"] >= HAND_NONE_SECS:
            hs["shown"] = None
    return hs["shown"]


# ---------------- mismatch debug frames ----------------
# Har hand ke frames (2 fps, PNG) memory me; hand XML se match na ho to disk pe
# debug_frames/<gamecode>/ me likh do — isi se pata chalta hai screen pe kya tha.
DBG_FPS = 2.0
DBG_KEEP_HANDS = 8          # memory me itne hands
DBG_KEEP_DUMPS = 40         # disk pe itne mismatch hands
_dbg = {}
_dbg_lock = threading.Lock()


def _dbg_frame(gc, title, img, st):
    if not gc or img is None:
        return
    now = time.time()
    with _dbg_lock:
        h = _dbg.setdefault(gc, {"t0": now, "last": 0.0, "frames": [], "title": title})
        if now - h["last"] < 1.0 / DBG_FPS or len(h["frames"]) > 600:
            return
        h["last"] = now
        if len(_dbg) > DBG_KEEP_HANDS:
            for g in sorted(_dbg, key=lambda k: _dbg[k]["t0"])[:len(_dbg) - DBG_KEEP_HANDS]:
                if g != gc:
                    _dbg.pop(g, None)
    try:
        from io import BytesIO
        buf = BytesIO()
        img.save(buf, "PNG", compress_level=1)
        meta = {"t": now, "st": dict(st, broke=sorted(st.get("broke") or [])) if st else None}
        with _dbg_lock:
            h["frames"].append((buf.getvalue(), meta))
    except Exception:
        pass


def _dbg_finish(gc, keep, info=None):
    """Hand verify ho gaya: mismatch (keep) ho to frames disk pe, warna memory se hatao."""
    with _dbg_lock:
        h = _dbg.pop(gc, None)
    if not h or not keep:
        return
    try:
        base = os.environ.get("PREFLOP_ROOT") or os.path.dirname(os.path.abspath(__file__))
        root = os.path.join(base, "debug_frames")
        d = os.path.join(root, str(gc))
        os.makedirs(d, exist_ok=True)
        metas = []
        for i, (png, meta) in enumerate(h["frames"]):
            with open(os.path.join(d, f"{i:04d}.png"), "wb") as f:
                f.write(png)
            metas.append(meta)
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"gc": gc, "title": h.get("title"), "info": info, "frames": metas}, f)
        import shutil
        old = sorted((x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x))),
                     key=lambda x: os.path.getmtime(os.path.join(root, x)))
        for x in old[:-DBG_KEEP_DUMPS]:
            shutil.rmtree(os.path.join(root, x), ignore_errors=True)
    except Exception:
        pass


def truth_loop():
    """Background: har table ka gamecode track karo; purane hands ki file aate hi glyphs label
    karo aur OCR vs XML accuracy verify karo (multi-table bhi chalta hai)."""
    while True:
        try:
            with _truth_lock:
                now_gc = {}
                for title, t in list(state["tables"].items()):
                    st = t.get("xml_st")
                    gc = st["gamecode"] if st else None
                    if gc:
                        now_gc[title] = gc
                        if _truth["gc_by_table"].get(title) != gc:
                            _truth["since"][gc] = time.time()   # naya hand — settle guard
                _truth["gc_by_table"] = now_gc
                done = []
                for g, p in list(_truth["pending"].items()):
                    f = p["tbl"] / g if p.get("tbl") else None
                    if f is not None and f.is_file():
                        if time.time() - f.stat().st_mtime >= 3.0:   # aakhri frames (hand-end) aane do
                            done.append((g, p, f))
                    elif time.time() - p["t"] > 900:
                        done.append((g, None, None))      # file kabhi nahi aayi — chhod do
                for g, _, _ in done:
                    _truth["pending"].pop(g, None)
                    _truth["verified"][g] = time.time()
                if len(_truth["verified"]) > 500:
                    for g in sorted(_truth["verified"], key=_truth["verified"].get)[:250]:
                        _truth["verified"].pop(g, None)
            try:
                for gc in set(_truth["gc_by_table"].values()):
                    _learn_fonts_from_history(gc)
            except Exception:
                pass
            for g, p, f in done:
                if p is None:
                    continue
                try:
                    hh = redstar_hh._parse(f.read_bytes(), g, 0)
                    n = _learn_truth(p, hh)
                    acc = verify_hand_full(g, p, hh, p.get("tbl"))
                    _dbg_finish(g, bool(acc.get("mismatch")),
                                {k: acc.get(k) for k in ("line_ok", "pos_ok", "pot_ok", "hero_ok", "board_ok")})
                    if n or acc.get("mismatch"):
                        print(f"[ocr] hand {g}: {n} naye templates | {acc}")
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(0.5)


def livecards_loop():
    """/api/livecards ke liye Red Star XML/file state background me — HTTP request kabhi wait nahi karti.
    Memory scan band: har call pe seconds lagte the aur live hand memory me milta hi nahi."""
    while True:
        try:
            tables = list(state["tables"].items())
            multi = len(tables) > 1
            for title, t in tables:
                # multi-table: har window ki apni XML (<tablename> se match) — warna ek hi table ki
                # positions saari windows pe lag jaati thi
                name = title if multi else None
                t["livecards"] = redstar_hh.read_live_state(use_mem=False, table_name=name)
                xml, tbl = redstar_hh._live_table_xml(name)
                t["xml_st"] = redstar_hh._parse_table_state(xml) if xml else None
                if tbl is not None:
                    t["tbl_dir"] = tbl
            sel = state["tables"].get(state.get("selected_title")) or (tables[0][1] if tables else None)
            state["livecards"] = sel.get("livecards") if sel else redstar_hh.read_live_state(use_mem=False)
        except Exception:
            pass
        time.sleep(0.25)


def action_loop():
    """Background: har TABLE ke naye frame pe cards + bets/action padho.
    Action OCR flicker se bachne ke liye 2 frame same aaye tabhi publish."""
    prev = {}
    seen = {}
    while True:
        for title, t in list(state["tables"].items()):
            img = t.get("last_img")
            if img is None or img is seen.get(title) or t.get("blocked"):
                continue
            seen[title] = img
            # Sirf Hold'em table ka saaf frame padho — Omaha (4 cards), lobby, popup ya badla
            # theme ho to galat cards/bets padhne se behtar kuch na padhna.
            if "omaha" in title.lower():
                t["layout"] = "omaha"
            elif HAS_CV and not is_table_frame(img):
                t["layout"] = "unknown"
            else:
                t["layout"] = "ok"
            if t["layout"] != "ok":
                t["last_ocr"] = None
                t["action"] = None
                t["position"] = None
                continue
            if USE_OCR and HAS_TESS and t.get("manual_hand") is None:
                try:
                    hkey = _hero_region_key(img)
                    if hkey != t.get("hero_hash") or t.get("tmpl_gen") != _tmpl_gen():
                        obs = []                        # cards badle (ya naye templates) tabhi OCR
                        hand, raw = detect(img, obs)
                        t["raw"] = raw
                        t["hero_hash"] = hkey
                        t["tmpl_gen"] = _tmpl_gen()
                        t["hero_obs"] = tuple(obs) if len(obs) == 2 else None
                        t["ocr_raw"] = hand or None    # cards gayab (fold/na hain) = None
                    t["last_ocr"] = _stable_hand(t, t.get("ocr_raw"),
                                                 (t.get("xml_st") or {}).get("gamecode"))
                    # Har frame ke glyphs current gamecode ke naam — hand file aate hi exact label
                    if t.get("hero_obs"):
                        _record_obs("hero", t["hero_obs"],
                                    (t.get("xml_st") or {}).get("gamecode"), t.get("tbl_dir"))
                except Exception:
                    pass
            if HAS_CV:
                try:
                    bobs = []
                    xml_pos = None
                    cbacks = card_back_seats(img)          # ek baar hi — dl + tracker dono use karein
                    if HAS_REDSTAR and t.get("xml_st"):          # isi table ki XML (multi-table bhi)
                        gc = t["xml_st"].get("gamecode")
                        dl = t.get("dealt")
                        if not dl or dl["gc"] != gc:
                            # naya hand: shuru se dekh rahe hain tabhi (pichla gc pata ho) card-back
                            # union bharosemand — beech se join kiya to fold wale miss honge
                            dl = t["dealt"] = {"gc": gc, "seats": set(), "n": 0,
                                               "from_start": bool(dl), "t0": time.time()}
                        # "SIT OUT" badge wali seats (hand bhar yaad — badge har frame nahi padhta)
                        dl.setdefault("sitout", set()).update(
                            i for i in range(len(SEAT_ANCHORS))
                            if i != HERO_SEAT and i not in dl["seats"] and _sitting_out(img, i))
                        # NOTE: "is seat pe kabhi card backs nahi dikhe = deal nahi hua" wala rule
                        # hata diya: jo player deal hote hi fold kar deta hai (pre-select fold)
                        # uske backs ek frame bhi nahi dikhte, aur woh galti se hat jaata tha
                        # (UTG ka fold line se gayab). Sit-out ke liye badge + pichla hand hi.
                        if dl["from_start"] and time.time() - dl["t0"] < 8:
                            dl["seats"] |= cbacks   # deal ke baad, fold hone se pehle
                            dl["n"] += 1
                        tdir = t.get("tbl_dir")
                        prev_dealt = redstar_hh.last_dealt(tdir.name if tdir else None,
                                                           t["xml_st"].get("prev_gamecode"))
                        # hero ke cards dikh rahe hain = hero deal hua (sit-out se wapas aaya ho tab bhi)
                        hero_in = bool(t.get("last_ocr") or (prev.get(title) or {}).get("hero_cards"))
                        # pichle hand ki file (kaun deal hua) abhi likhi nahi gayi: tab tak sit-out
                        # players bhi gine jaate hain aur positions khiski hoti hain (BB ka 1 BB
                        # "BTN limp" ban jaata tha). 4 sec tak ruko, phir jo hai usi se chalo.
                        t["pos_wait"] = (bool(t["xml_st"].get("prev_gamecode")) and prev_dealt is None
                                         and time.time() - dl["t0"] < 4.0)
                        _xp = xml_seat_positions(t["xml_st"],
                                                 dealt_seats=dl["seats"] | ({HERO_SEAT} if hero_in else set()),
                                                 prev_dealt=prev_dealt,
                                                 backs_only=dl["from_start"] and dl["n"] >= 3,
                                                 sitout_seats=dl["sitout"] - dl["seats"])
                        xml_pos = _xp[0] if _xp else None
                    _dbg_frame((t.get("xml_st") or {}).get("gamecode"), title, img, t.get("xml_st"))
                    _board_use(title)
                    act = detect_action(img, bobs, xml_pos, (t.get("xml_st") or {}).get("gamecode"),
                                        t.get("tbl_dir"))
                    if bobs:
                        _record_obs("board", tuple(bobs),
                                    (t.get("xml_st") or {}).get("gamecode"), t.get("tbl_dir"))
                    t["position"] = act and act["hero"]
                    # 1-frame delayed publish: pichle frame ka action publish karo (fast action bhi
                    # miss na ho — pehle 2 frame same maangta tha, jo quick sequence chhod deta tha).
                    # None bhi publish hota hai taaki purana action (pichle hand ka board) na atke.
                    prev_act = prev.get(title)
                    if prev_act is not None:
                        t["action"] = prev_act
                        btn = _button_seat(img)
                        # Positions: XML exact (sit-out/wait players hata ke) — OCR sirf fallback.
                        # Screen se 'baitha hai par deal nahi hua' vs 'deal hua' ka farak nahi
                        # dikhta (yehi 22% pos error ka source tha). Action khud OCR se hi.
                        posmap = xml_pos or (seat_positions(img, btn) if btn is not None else {})
                        gc = (t.get("xml_st") or {}).get("gamecode")
                        if not t.get("pos_wait"):
                            update_tracker(t, prev_act, cbacks, posmap, gc, t.get("xml_st"))
                        # hand meta record (line/pos/pot) — hand khatam pe XML truth se compare
                        trk = t.get("tracker")
                        # hand beech se dekhna shuru kiya (server abhi chala / table abhi khuli) to
                        # line adhoori hi hogi — use accuracy me mat gino
                        dl = t.get("dealt")
                        whole = not dl or dl.get("gc") != gc or dl.get("from_start")
                        if trk and trk.get("gc") == gc and trk.get("line") and whole:
                            _record_hand(gc, t.get("tbl_dir"), trk["line"],
                                         prev_act.get("hero"), prev_act.get("pot"),
                                         _hero_seat(t.get("xml_st")))
                    prev[title] = act
                except Exception:
                    pass
            # Seat names (throttled — har 3 sec) villain auto-select ke liye. Red Star pe naam XML se
            # exact aate hain (/api/livecards) — yeh ~400 ms ka OCR loop rok deta tha, isliye skip.
            if USE_OCR and HAS_TESS and not HAS_REDSTAR and time.time() - t.get("names_time", 0) > 3:
                try:
                    raw_names = detect_names(img)
                    btn = _button_seat(img)
                    posmap = seat_positions(img, btn) if btn is not None else {}
                    t["names"] = {posmap[i]: nm for i, nm in raw_names.items() if i in posmap and i != HERO_SEAT}
                    t["names_time"] = time.time()
                except Exception:
                    pass
        # Selected (ya pehli) table ke results flat state me sync karo — purane endpoints ke liye
        sel = state["tables"].get(state.get("selected_title")) or next(iter(state["tables"].values()), None)
        if sel is not None:
            state["manual_hand"] = sel.get("manual_hand")
            state["last_ocr"] = sel.get("last_ocr")
            state["raw"] = sel.get("raw", "")
            state["position"] = sel.get("position")
            state["action"] = sel.get("action")
            state["frame_jpeg"] = sel.get("frame_jpeg")
            state["last_img"] = sel.get("last_img")
            state["blocked"] = sel.get("blocked", False)
            state["layout"] = sel.get("layout")
            state["tracker"] = sel.get("tracker")
        time.sleep(0.03)


def loop():
    """Background: SAARI table windows capture karo (har 0.1s) + detect action_loop se."""
    last_find = 0.0
    last_pin = 0.0
    while True:
        try:
            now = time.time()
            # Har 2 sec dobara dhoondo: nayi tables aayi, band hui hat jayen
            if now - last_find > 2:
                wins = find_windows(WINDOW_TITLE, PROCESS_NAMES)
                state["windows"] = wins
                sel = state.get("selected_title")
                ordered = sorted(wins, key=lambda wt: (0 if wt[1] == sel else 1, wt[1]))[:MAX_TABLES]
                keep = {}
                for hwnd, title in ordered:
                    t = state["tables"].get(title)
                    if t is None:
                        t = {"hwnd": hwnd, "title": title, "frame_jpeg": None, "last_img": None,
                             "blocked": False, "last_ocr": None, "raw": "", "position": None,
                             "action": None, "manual_hand": None, "names": {}, "names_time": 0.0,
                             "hero_hash": None}
                    t["hwnd"] = hwnd
                    keep[title] = t
                state["tables"] = keep
                sel_t = keep.get(sel) if sel and sel in keep else (next(iter(keep.values()), None) if keep else None)
                state["window_hwnd"] = sel_t["hwnd"] if sel_t else None
                state["window_title"] = sel_t["title"] if sel_t else None
                last_find = now

            # Pin on top — selected table ko aage rakho
            if state["pinned"] and state["window_hwnd"] and now - last_pin > 1.5:
                pin_top(state["window_hwnd"], True)
                last_pin = now

            # Saari tables capture karo
            for t in state["tables"].values():
                hwnd = t["hwnd"]
                t["blocked"] = is_capture_blocked(hwnd)
                if t["blocked"]:
                    t["frame_jpeg"] = None
                    t["last_ocr"] = None
                    t["last_img"] = None
                    continue
                img = capture(hwnd)
                if img:
                    from io import BytesIO
                    buf = BytesIO()
                    img.save(buf, "JPEG", quality=80)
                    t["frame_jpeg"] = buf.getvalue()
                    t["last_img"] = img
                else:
                    t["last_img"] = None
        except Exception:
            pass
        time.sleep(0.1)


class Handler(BaseHTTPRequestHandler):
    server_version = "MirrorServer/1.0"

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            ctype = "application/json"
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass                                     # browser ne beech me request chhod di (reload) — server nahi girna chahiye

    def _qparam(self, name):
        from urllib.parse import urlparse, parse_qs
        v = parse_qs(urlparse(self.path).query).get(name)
        return v[0] if v else None

    def _table(self):
        title = self._qparam("table")
        return state["tables"].get(title) if title else None

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self):
        if self.path.startswith("/api/health"):
            def meter(name):
                a = state.get(name, {})
                t, o = a.get("total", 0), a.get("ok", 0)
                return {"total": t, "ok": o, "pct": round(o / t * 100, 1) if t else None}
            self._send(200, {
                "ok": True,
                "window": state["window_title"],
                "ocr": bool(HAS_TESS and USE_OCR),
                "redstar": HAS_REDSTAR,
                "ranks_learned": "".join(r for r in RANKS if any(t[0] == r for t in RANK_TEMPLATES)),
                "suits_learned": "".join(sorted({s for s, _ in SUIT_TEMPLATES})),
                "connected": bool(state["window_hwnd"]),
                "table_count": len(state.get("windows", [])),
                "pinned": state.get("pinned", False),
                "blocked": state.get("blocked", False),
                "layout": state.get("layout"),
                "accuracy": meter("accuracy"),
                "card_accuracy": meter("card_accuracy"),
                "line_accuracy": meter("line_ok_accuracy"),
                "pos_accuracy": meter("pos_ok_accuracy"),
                "pot_accuracy": meter("pot_ok_accuracy"),
                "mismatches": len(_mismatch_log),
            })
        elif self.path.startswith("/api/accuracy"):
            def meter(name):
                a = state.get(name, {})
                t, o = a.get("total", 0), a.get("ok", 0)
                return {"total": t, "ok": o, "pct": round(o / t * 100, 1) if t else None}
            self._send(200, {
                "cards": meter("card_accuracy"),
                "line": meter("line_ok_accuracy"),
                "pos": meter("pos_ok_accuracy"),
                "pot": meter("pot_ok_accuracy"),
                "bets": meter("accuracy"),
                "ranks_learned": "".join(r for r in RANKS if any(t[0] == r for t in RANK_TEMPLATES)),
                "suits_learned": "".join(sorted({s for s, _ in SUIT_TEMPLATES})),
                "mismatches": _mismatch_log[-30:],
            })
        elif self.path.startswith("/api/livecards"):
            out = {"available": HAS_REDSTAR}
            if HAS_REDSTAR:
                try:
                    tq = self._table()                    # ?table=<title> — multi-table
                    h = (tq or {}).get("livecards") or state.get("livecards")   # livecards_loop har 0.25s
                    out["hand_id"] = h["hand_id"] if h else None
                    out["hand"] = h["hero"] if h else []
                    out["hand_str"] = "".join(h["hero"]) if h else None
                    out["board"] = h["board"] if h else []
                    out["street"] = h["street"] if h else None
                    out["pos"] = h.get("pos") if h else None
                    out["hero_to_act"] = h.get("hero_to_act") if h else None
                    out["dealer"] = h.get("dealer") if h else None
                    out["names"] = h.get("names", {}) if h else {}
                    out["pf_bets"] = h.get("pf_bets", {}) if h else {}
                    out["pf_to_call"] = h.get("pf_to_call") if h else None
                    out["pf_live"] = bool(h and h.get("pf_live"))
                except Exception as e:
                    out["error"] = str(e)
            self._send(200, out)
        elif self.path.startswith("/api/pnl"):
            # game-wise (PLO4/5/6) rake + profit after rake — completed hands XML se
            out = {"available": HAS_REDSTAR}
            if HAS_REDSTAR:
                try:
                    out.update(redstar_hh.pnl_stats())
                except Exception as e:
                    out["error"] = str(e)
            self._send(200, out)
        elif self.path.startswith("/api/windows"):
            self._send(200, {
                "windows": [{"title": t, "hwnd": int(h)} for h, t in state.get("windows", [])],
                "selected": state.get("selected_title"),
            })
        elif self.path.startswith("/api/tables"):
            tables = []
            for t in state["tables"].values():
                lc = t.get("livecards") or {}
                hero = lc.get("hero") or []
                acc_hand = "".join(hero) if len(hero) == 2 else None
                acc_pos = lc.get("pos")
                acc_street = lc.get("street")          # None = preflop, else flop/turn/river
                action = t.get("action")
                # OCR action na mile to accurate street (postflop) bata do — tile khaali na rahe
                if not action and acc_street:
                    action = {"street": acc_street, "hero": acc_pos, "bets": {},
                              "streetName": acc_street}
                hta = ((action or {}).get("hero_to_act")
                       if isinstance(action, dict) and action.get("hero_to_act") is not None
                       else lc.get("hero_to_act"))
                ocr = t.get("action") if isinstance(t.get("action"), dict) else None
                # OCR board PEHLE — ye LIVE screen hai. lc.board binary file se aata hai jo sirf
                # hand COMPLETE hone pe likhi jaati hai (pichle hand ka STALE board hota hai).
                board = (ocr.get("board") if ocr else None) or lc.get("board") or []
                tables.append({
                    "title": t["title"], "hwnd": int(t["hwnd"]),
                    "hand": t.get("manual_hand") or t.get("last_ocr") or acc_hand,
                    "raw": t.get("raw", ""),
                    "pos": t.get("position") or acc_pos,
                    "action": action,
                    "board": board,
                    "pot": (action or {}).get("pot"),
                    "hero_to_act": hta,
                    "names": t.get("names", {}),
                    "blocked": t.get("blocked", False),
                    "layout": t.get("layout"),
                    "line": (t.get("tracker") or {}).get("line"),
                    "has_frame": bool(t.get("frame_jpeg")),
                })
            self._send(200, {"max_tables": MAX_TABLES, "selected": state.get("selected_title"),
                             "tables": tables})
        elif self.path.startswith("/api/frame"):
            t = self._table()
            jpeg = (t and t.get("frame_jpeg")) or state.get("frame_jpeg")
            if jpeg:
                self._send(200, jpeg, "image/jpeg")
            else:
                self._send(404, {"error": "no frame"})
        elif self.path.startswith("/api/region"):
            # Hero region ka crop (tuning ke liye)
            from io import BytesIO
            t = self._table()
            jpeg = (t and t.get("frame_jpeg")) or state.get("frame_jpeg")
            if jpeg and HAS_PIL:
                img = Image.open(BytesIO(jpeg))
                crop, _ = crop_hero(img)
                if crop:
                    buf = BytesIO()
                    crop.save(buf, "JPEG", quality=90)
                    self._send(200, buf.getvalue(), "image/jpeg")
                    return
            self._send(404, {"error": "no frame"})
        elif self.path.startswith("/api/detect"):
            t = self._table()
            src = t if t else state
            hand = src.get("manual_hand") or src.get("last_ocr")
            self._send(200, {"hand": hand, "raw": src.get("raw", ""),
                             "pos": src.get("position"),
                             "action": src.get("action"),
                             "line": (src.get("tracker") or {}).get("line")})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/api/sethand"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                state["manual_hand"] = data.get("hand")
                self._send(200, {"ok": True, "hand": state["manual_hand"]})
            except Exception as e:
                self._send(400, {"error": str(e)})
        elif self.path.startswith("/api/learn"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, learn_from_hand(data.get("hand", "")))
            except Exception as e:
                self._send(400, {"error": str(e)})
        elif self.path.startswith("/api/select"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                state["selected_title"] = data.get("title")
                t = state["tables"].get(state["selected_title"])
                if t:
                    state["window_hwnd"] = t["hwnd"]
                    state["window_title"] = t["title"]
                self._send(200, {"ok": True, "selected": state["selected_title"]})
            except Exception as e:
                self._send(400, {"error": str(e)})
        elif self.path.startswith("/api/focus"):
            t = self._table()
            bring_to_front(t["hwnd"] if t else state["window_hwnd"])
            self._send(200, {"ok": True})
        elif self.path.startswith("/api/pin"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                state["pinned"] = bool(data.get("on"))
                t = self._table()
                pin_top(t["hwnd"] if t else state["window_hwnd"], state["pinned"])
                self._send(200, {"ok": True, "pinned": state["pinned"]})
            except Exception as e:
                self._send(400, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def main():
    if not HAS_WIN32:
        print("ERROR: pywin32 nahi mila.  ->  pip install pywin32")
        return
    if USE_OCR and not HAS_TESS:
        print("NOTE: OCR off hai (pytesseract nahi mila).  ->  pip install pytesseract Pillow")
        print("      Cards manually select karo trainer me — Detect kaam nahi karega.")
    if USE_OCR and not HAS_PIL:
        print("NOTE: Pillow nahi mila.  ->  pip install Pillow")

    srv = start_background()
    print(f"Mirror server running on http://127.0.0.1:{PORT}")
    print(f"Target window title: {WINDOW_TITLE!r}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


def start_background():
    """Capture/detect threads + HTTP server — sab background me (desktop exe isse chalata hai)."""
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=action_loop, daemon=True).start()
    if HAS_REDSTAR:
        threading.Thread(target=truth_loop, daemon=True).start()
        threading.Thread(target=livecards_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    main()
