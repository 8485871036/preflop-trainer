"""
Mirror Server — captures a target app window and serves it to the
Preflop Trainer's "Live Mirror" mode, plus optional hole-card OCR.

Run:   python mirror_server.py
Then open the trainer app -> "Live Mirror".

Endpoints (all CORS-enabled):
  GET  /api/health   -> {"ok": true, "window": "<found title>", "ocr": true|false}
  GET  /api/frame    -> latest screenshot (JPEG)
  GET  /api/detect   -> {"hand": "AsKd"|null, "raw": "<ocr text>", "pos": "CO"|null,
                          "action": {"street", "hero", "bets": {"UTG": 2.2, ...}}|null}
  POST /api/sethand  -> {"hand": "AsKd"}  (manual override, used until detect changes)
"""
import json
import os
import re
import sys
import threading
import time
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============================================================
#  CONFIG — apne poker app ke hisaab se badlo 👇
# ============================================================
# Ek ya kai naam de sakte ho — koi bhi match hoga (partial).
# Poker client ka naam + table windows ka common prefix dono rakho.
WINDOW_TITLE = ["NL Hold'em", "NLHP", "Natural8"]
# Process ke naam se bhi match karo (title empty/badle to bhi pakde).
PROCESS_NAMES = ["pokerclient", "ggnet"]
# Lobby windows ke exact titles — inke card-preview / banners ko hero cards padh leta tha.
LOBBY_TITLES = {"natural8", "redstar poker", "red star poker", "ggpoker"}
PORT = int(os.environ.get("MIRROR_PORT", "8676"))
MAX_TABLES = int(os.environ.get("MIRROR_MAX_TABLES", "4"))   # multi-table: ek saath kitni tables
# Hero ke 2 cards ka region — window ka FRACTION (x, y, width, height).
# (0,0) = top-left, (1,1) = bottom-right. Apne poker client ke liye tune karo.
# Natural8 (5-seat layout): hero cards bottom-center, naam/stack ke UPAR hote hain.
HERO_REGION = (0.43, 0.675, 0.14, 0.085)
USE_OCR = True             # OCR ke liye:  pip install pytesseract Pillow  (+ Tesseract binary)
# Natural8/GG 4-color deck: ♥=red, ♦=blue, ♣=green, ♠=black/gray.
# Is client me card ka suit SYMBOL bahut faint hota hai — suit CARD BODY COLOR se padhte hain.

# GG/Natural8 6-max seat anchors (window FRACTION, clockwise order).
# Index 0 = bottom-right, phir clockwise: bottom-center (HERO), bottom-left,
# top-left, top-center, top-right. HERO hamesha bottom-center hota hai (index 1).
# Agar aapka layout alag ho to in fractions ko tune karo.
SEAT_ANCHORS = [
    (0.79, 0.73),  # 0 bottom-right
    (0.50, 0.82),  # 1 bottom-center (HERO)
    (0.20, 0.73),  # 2 bottom-left
    (0.12, 0.35),  # 3 top-left
    (0.50, 0.20),  # 4 top-center
    (0.88, 0.35),  # 5 top-right
]
HERO_SEAT = 1
# Har seat ke bet label ("2.50 BB") ka box (x0, y0, x1, y1) — SEAT_ANCHORS wale order me.
# Chip icon aur card backs se bachne ke liye tight rakhe hain.
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
}

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
    """Red Star (white card + colored text) ke liye suit: card ke colored
    rank+suit text ka dominant hue. Red=heart, green=club, blue=diamond, black=spade."""
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


def _is_card_back(card):
    """Opponent ka face-down card: blue body + red/white chip logo."""
    a = _np.asarray(card.convert("RGB"), dtype=_np.int32)
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    r, g, b = _card_body_rgb(card)
    return b > r + 40 and ((R > 170) & (G < 90) & (B < 90)).mean() > 0.02


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


# Rank glyph templates — tesseract itne chhote (~7x12 px) glyph pe 3->A, J->T, 8->6 padhta hai.
# rank_templates.json me VERIFIED glyphs hain (hand file ke exact cards se auto-label).
# Glyph = card ke top-left corner ka "body color se contrast" map (FEAT_W x FEAT_H grayscale).
# Match = normalized cross-correlation (+-1 px shift). Koi confident match na ho tabhi tesseract.
FEAT_W, FEAT_H = 12, 16
TEMPLATE_MIN_SCORE = 0.84    # isse kam correlation = anjaan glyph
TEMPLATE_SURE_SCORE = 0.92   # saare 13 ranks ke templates na hon tab itna chahiye (cross-rank max ~0.88)
TEMPLATE_MIN_MARGIN = 0.06   # best rank doosre rank se kam se kam itna aage ho
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
    m = d > max(60, 0.45 * d.max())
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
    if rank is None and not TEMPLATES_ONLY and not _templates_complete():
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
    """Ek card se (rank, suit) nikalo — rank OCR se, suit text color se.
    obs list di ho to (glyph_key, suit) usme append hota hai (auto-labelling ke liye)."""
    if not card_img:
        return None, None
    cw, ch = card_img.size
    if cw < 8 or ch < 8:
        return None, None
    if _is_card_back(card_img):
        return None, None
    rank, key = _read_rank_key(card_img)
    suit = _suit(card_img)
    if obs is not None:
        obs.append((key, suit))
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


def _find_card_blobs(img, y0f=0.40, y1f=1.0):
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
    min_area = int(w * h * 0.0004)
    min_w, min_h = int(w * 0.03), int(h * 0.05)
    blobs = []
    # Har body color ke blobs ALAG — ek combined mask me chips / "48 BB" jaisa white text
    # card se jud jaata tha aur card ka rectangle test fail ho jaata tha.
    for mask in (red, green, cyan, gray, white):
        mask = mask.copy()
        mask[:int(h * y0f), :] = False
        mask[int(h * y1f):, :] = False
        n, labels, stats, _ = _cv2.connectedComponentsWithStats(mask.astype(_np.uint8) * 255, 8)
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if area < min_area or bw < min_w or bh < min_h:
                continue
            if area / (bw * bh) < 0.6:      # solid rectangle hi card hai (text/strips nahi)
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
            r, s = read_card(img.crop((sx, sy, sx + sw, sy + sh)), o)
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
    fixed = isolate_cards(crop) if crop else []
    parts, o = [], []
    for c in fixed[:2]:
        r, s = read_card(c, o)
        if r and s:
            parts.append(r + s)
    if obs is not None and len(o) == 2 and all(k and s for k, s in o):
        obs.extend(o)
    if len(parts) == 2:
        return parts[0] + parts[1], " ".join(parts)
    return None, None


def detect_board(img, obs=None):
    """Board (flop/turn/river) cards — table ke center band se, left->right order me.
    obs list di ho to saare board cards ke (glyph_key, suit) left->right usme aate hain."""
    if not img or not HAS_CV or not HAS_TESS or not board_present(img):
        return None
    blobs = _find_card_blobs(img, y0f=0.30, y1f=0.58)
    cards, seen_obs = [], []
    for x, y, bw, bh, _area in blobs:
        if bh < 8:
            continue
        # paas-paas ke board cards ek wide blob me — card width ~0.74*h, beech me ~0.07*h gap.
        # (round(bw/bh) 3 cards ko 2 gin leta tha aur card beech se kat jaata tha)
        gap = 0.07 * bh
        n = max(1, round((bw + gap) / (0.81 * bh)))
        pitch = (bw + gap) / n
        for k in range(n):
            sx = x + int(round(k * pitch))
            sw = max(8, int(round(pitch - gap)))
            card = img.crop((sx, y, sx + sw, y + bh))
            if _is_card_back(card) or _is_rabbit_card(card):
                continue
            o = []
            r, s = read_card(card, o)
            if o and o[0][0] and s:
                seen_obs.append((sx, o[0]))
                cards.append((sx, (r + s) if r else None))
    cards.sort(key=lambda c: c[0])
    seen_obs.sort(key=lambda c: c[0])
    if obs is not None and 3 <= len(seen_obs) <= 5:
        obs.extend(o for _, o in seen_obs)
    board = [c for _, c in cards]
    # ek bhi card anjaan / duplicate = board pe bharosa nahi (chhota board galat street deta hai)
    if not 3 <= len(board) <= 5 or None in board or len(set(board)) != len(board):
        return None
    return board


def _detect_button(img):
    """Dealer button (yellow 'D' circle) ka center dhoondo — window fraction."""
    if not HAS_CV:
        return None
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
        return (best[0] / w, best[1] / h)
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


_BET_RE = re.compile(r"(\d+(?:\.\d+)?)\s*B")


def _parse_bet(text):
    m = _BET_RE.findall(text)
    if m:
        return float(m[-1])
    m = re.search(r"(\d+(?:\.\d+)?)88$", text.replace(" ", ""))   # "BB" kabhi "88" padh jaata hai
    return float(m.group(1)) if m else 0.0


_bet_cache = {}


def read_bets(img):
    """Har seat ke saamne ka bet (BB me) — SEAT_ANCHORS order me list."""
    img = img.convert("RGB")
    w, h = img.size
    out = []
    for x0, y0, x1, y1 in BET_BOXES:
        c = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
        c = c.resize((c.width * 4, c.height * 4), Image.LANCZOS)
        a = _np.asarray(c, dtype=_np.int32)
        white = (a.min(2) > 160) & ((a.max(2) - a.min(2)) < 50)   # safed text, rangeen chips nahi
        if white.sum() < 40:                                       # khaali — tesseract mat chalao
            out.append(0.0)
            continue
        key = white.tobytes()
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
        out.append(_bet_cache[key])
    return out


def board_present(img):
    """Flop/turn/river cards table ke beech me hain?"""
    w, h = img.size
    x0, y0, x1, y1 = BOARD_REGION
    a = _np.asarray(img.convert("RGB").crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))),
                    dtype=_np.int32)
    colored = (a.max(2) > 90) | ((a.max(2) - a.min(2)) > 60)
    return colored.mean() > 0.1


def _action_buttons(img):
    """Neeche-right FOLD / CALL / RAISE panel (red glow) dikh raha hai = hero ki baari.
    Baaki time wahan sirf felt + "Check/Fold" checkboxes hote hain (red ~0%, panel 7-23%)."""
    a = _np.asarray(img.convert("RGB"), dtype=_np.int32)
    h, w = a.shape[:2]
    r = a[int(h * 0.86):int(h * 0.99), int(w * 0.60):int(w * 0.99)]
    R, G, B = r[..., 0], r[..., 1], r[..., 2]
    return bool(((R > 70) & (R > 2 * G) & (R > 2 * B)).mean() > 0.04)


RS_SEATS = [1, 3, 5, 6, 8, 10]   # Red Star 6-max XML seat numbers, clockwise (action order)


def xml_seat_positions(st, hero=None):
    """Live XML (dealt players + seat + dealer) -> ({screen_seat: "BTN"/...}, dealer_screen_seat).
    Screen pe hero hamesha HERO_SEAT (bottom-center), seats clockwise — isliye XML seat ka
    order-offset hi screen index hai. Sit-out / wait-for-BB wale XML me hote hi nahi."""
    hero = hero or redstar_hh.HERO
    names, seats = st.get("names") or [], st.get("seats") or []
    if hero not in names or len(seats) != len(names) or any(s not in RS_SEATS for s in seats):
        return None, None
    k = RS_SEATS.index(seats[names.index(hero)])
    scr = lambda s: (HERO_SEAT + RS_SEATS.index(s) - k) % len(SEAT_ANCHORS)
    pos = {scr(s): redstar_hh._position(names, st["dealer_idx"], nm) for nm, s in zip(names, seats)}
    return pos, scr(seats[st["dealer_idx"]])


def detect_action(img, board_obs=None, xml_pos=None):
    """Preflop/postflop action: position, bets, board, hero_to_act.
    hero_to_act = abhi hero ki baari hai (call/check/raise ka faisla).
    xml_pos = xml_seat_positions() ka jawab — ho to positions usi se (100% sahi); screen se
    active seats ginne me dim plaque (wait-for-BB) wale bhi gine jaate the aur sab khisak jaata tha."""
    b = _button_seat(img)
    if b is None:
        return None
    if xml_pos and xml_pos[0] and xml_pos[1] == b:     # screen ka button = XML ka dealer (same hand)
        pos = xml_pos[0]
    else:
        pos = seat_positions(img, b)
    hero = pos.get(HERO_SEAT)
    bets = read_bets(img)
    bets_by_pos = {pos[i]: amt for i, amt in enumerate(bets) if amt > 0 and i in pos}
    mx = max(bets_by_pos.values()) if bets_by_pos else 0.0
    hero_bet = bets_by_pos.get(hero, 0.0)
    to_call = mx - hero_bet
    # Hero ki baari = FOLD/CALL/RAISE buttons dikh rahe hain. Pehle to_call > 0 se andaza tha —
    # hero fold karke bahar ho ya uske baad kisi aur ki baari ho, tab bhi "aapki baari" bolta tha.
    hero_to_act = _action_buttons(img)
    if board_present(img):
        board = detect_board(img, board_obs)
        return {"street": "postflop", "hero": hero, "bets": bets_by_pos,
                "board": board or [], "streetName": _street_name(board),
                "hero_to_act": hero_to_act, "to_call": round(to_call, 1)}
    return {"street": "preflop", "hero": hero, "bets": bets_by_pos,
            "hero_to_act": hero_to_act, "to_call": round(to_call, 1)}


def _street_name(board):
    """Board card count se street — 3=flop, 4=turn, 5=river."""
    if not board:
        return None
    return {3: "flop", 4: "turn", 5: "river"}.get(len(board))


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
_truth = {"gc": None, "tbl": None, "since": 0.0, "pending": {}, "st": None}
_truth_lock = threading.Lock()


def _record_obs(kind, obs):
    """Current frame ke (glyph_key, suit) tuples ko current gamecode ke saath gino."""
    if not HAS_REDSTAR or len(state["tables"]) != 1:     # multi-table: gamecode kis table ka, pata nahi
        return
    with _truth_lock:
        gc = _truth["gc"]
        if not gc or time.time() - _truth["since"] < TRUTH_SETTLE_SECS:
            return
        p = _truth["pending"].setdefault(gc, {"tbl": _truth["tbl"], "t": time.time(),
                                              "hero": {}, "board": {}})
        p[kind][obs] = p[kind].get(obs, 0) + 1


def _label_cards(obs, truth):
    """obs [(key, suit)] ko truth ['Td', '3c'] se label karo — sirf jab har suit match kare."""
    if len(obs) > len(truth) or any(s != c[1] for (_, s), c in zip(obs, truth)):
        return 0
    return sum(1 for (k, _), c in zip(obs, truth) if add_rank_template(c[0], k))


def _learn_truth(p, hh):
    added = 0
    hero = hh.get("hero") or []
    votes = sorted(p["hero"].items(), key=lambda kv: -kv[1])
    if len(hero) == 2 and votes and votes[0][1] >= TRUTH_MIN_FRAMES:
        obs = votes[0][0]
        same_suit = hero[0][1] == hero[1][1] and hero[0][0] != hero[1][0]
        if not same_suit:
            # suit order se hi pata chalta hai kaunsa glyph kaunsa card hai
            added += _label_cards(obs, hero) or _label_cards(obs[::-1], hero[::-1])
        else:
            # suited hand: order suit se tay nahi hota — maujooda template se decide karo
            m = [_match_template(k) for k, _ in obs]
            if m[0] == hero[0][0] or m[1] == hero[1][0]:
                added += _label_cards(obs, hero)
            elif m[0] == hero[1][0] or m[1] == hero[0][0]:
                added += _label_cards(obs[::-1], hero[::-1])
    board = hh.get("board") or []
    for obs, n in p["board"].items():
        if n >= TRUTH_MIN_FRAMES and board:
            added += _label_cards(obs, board)
    return added


def truth_loop():
    """Background: current gamecode track karo; purane hands ki file aate hi label karo."""
    while True:
        try:
            xml, tbl = redstar_hh._live_table_xml(None)
            st = redstar_hh._parse_table_state(xml) if xml else None
            gc = st["gamecode"] if st else None
            with _truth_lock:
                if gc != _truth["gc"]:
                    _truth.update(gc=gc, tbl=tbl, since=time.time())
                _truth["st"] = st
                done = []
                for g, p in _truth["pending"].items():
                    f = p["tbl"] / g if p["tbl"] else None
                    if f is not None and f.is_file():
                        done.append((g, p, f))
                    elif g != gc and time.time() - p["t"] > 900:
                        done.append((g, None, None))      # file kabhi nahi aayi — chhod do
                for g, _, _ in done:
                    _truth["pending"].pop(g, None)
            for g, p, f in done:
                if p is None:
                    continue
                try:
                    n = _learn_truth(p, redstar_hh._parse(f.read_bytes(), g, 0))
                    if n:
                        print(f"[ocr] hand {g}: {n} naye verified rank templates")
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(0.5)


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
                        if hand:
                            t["last_ocr"] = hand
                        elif raw:
                            t["last_ocr"] = None
                    # Har frame ke glyphs current gamecode ke naam — hand file aate hi exact label
                    if t.get("hero_obs"):
                        _record_obs("hero", t["hero_obs"])
                except Exception:
                    pass
            if HAS_CV:
                try:
                    bobs = []
                    xml_pos = None
                    if HAS_REDSTAR and _truth.get("st") and len(state["tables"]) == 1:
                        xml_pos = xml_seat_positions(_truth["st"])
                    act = detect_action(img, bobs, xml_pos)
                    if bobs:
                        _record_obs("board", tuple(bobs))
                    t["position"] = act and act["hero"]
                    if act is not None and act == prev.get(title):
                        t["action"] = act
                    prev[title] = act
                except Exception:
                    pass
            # Seat names (throttled — har 3 sec) villain auto-select ke liye
            if USE_OCR and HAS_TESS and time.time() - t.get("names_time", 0) > 3:
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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _qparam(self, name):
        from urllib.parse import urlparse, parse_qs
        v = parse_qs(urlparse(self.path).query).get(name)
        return v[0] if v else None

    def _table(self):
        title = self._qparam("table")
        return state["tables"].get(title) if title else None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/health"):
            self._send(200, {
                "ok": True,
                "window": state["window_title"],
                "ocr": bool(HAS_TESS and USE_OCR),
                "redstar": HAS_REDSTAR,
                "ranks_learned": "".join(r for r in RANKS if any(t[0] == r for t in RANK_TEMPLATES)),
                "connected": bool(state["window_hwnd"]),
                "table_count": len(state.get("windows", [])),
                "pinned": state.get("pinned", False),
                "blocked": state.get("blocked", False),
            })
        elif self.path.startswith("/api/livecards"):
            out = {"available": HAS_REDSTAR}
            if HAS_REDSTAR:
                try:
                    h = redstar_hh.read_live_state()
                    out["hand_id"] = h["hand_id"] if h else None
                    out["hand"] = h["hero"] if h else []
                    out["hand_str"] = "".join(h["hero"]) if h else None
                    out["board"] = h["board"] if h else []
                    out["street"] = h["street"] if h else None
                    out["pos"] = h.get("pos") if h else None
                    out["hero_to_act"] = h.get("hero_to_act") if h else None
                    out["dealer"] = h.get("dealer") if h else None
                    out["names"] = h.get("names", {}) if h else {}
                except Exception as e:
                    out["error"] = str(e)
            self._send(200, out)
        elif self.path.startswith("/api/windows"):
            self._send(200, {
                "windows": [{"title": t, "hwnd": int(h)} for h, t in state.get("windows", [])],
                "selected": state.get("selected_title"),
            })
        elif self.path.startswith("/api/tables"):
            self._send(200, {
                "max_tables": MAX_TABLES,
                "selected": state.get("selected_title"),
                "tables": [
                    {"title": t["title"], "hwnd": int(t["hwnd"]),
                     "hand": t.get("manual_hand") or t.get("last_ocr"),
                     "raw": t.get("raw", ""),
                     "pos": t.get("position"),
                     "action": t.get("action"),
                     "names": t.get("names", {}),
                     "blocked": t.get("blocked", False),
                     "has_frame": bool(t.get("frame_jpeg"))}
                    for t in state["tables"].values()
                ],
            })
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
                             "action": src.get("action")})
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
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    main()
