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
PORT = int(os.environ.get("MIRROR_PORT", "8676"))
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
        found.append((hwnd, t))
        return True
    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
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


def _body_color(card):
    """Card body ka median color (white rank + dark text hata kar)."""
    w, h = card.size
    px = card.convert("RGB").load()
    rs, gs, bs = [], [], []
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            mx = max(r, g, b)
            mn = min(r, g, b)
            if mx <= 60:                      # dark text / felt
                continue
            if (mx - mn) < 30 and mx > 200:   # white rank letter
                continue
            rs.append(r)
            gs.append(g)
            bs.append(b)
    if not rs:
        return None
    rs.sort()
    gs.sort()
    bs.sort()
    m = len(rs) // 2
    return (rs[m], gs[m], bs[m])


def _suit_from_color(rgb):
    """Card body color -> suit. Natural8 4-color deck ke hisaab se:
    ♥=red, ♦=cyan(blue), ♣=green, ♠=black/gray."""
    if not rgb:
        return None
    r, g, b = rgb
    mx = max(r, g, b)
    mn = min(r, g, b)
    if mx - mn < 25:
        return "s"                            # neutral gray/black -> spade
    if r >= g and r >= b and (r - g) > 40 and (r - b) > 40:
        return "h"                            # red -> heart
    if g >= r and g >= b and (g - r) > 40 and (g - b) > 30:
        return "c"                            # green -> club
    if b >= g and b > r and (b - r) > 60 and g > 70:
        return "d"                            # cyan -> diamond
    return "s"


def _ocr_binary(bw_img, whitelist, psm):
    """Already-binarized image se OCR karo (white glyph on black)."""
    try:
        return pytesseract.image_to_string(
            bw_img, config=f"--psm {psm} -c tessedit_char_whitelist={whitelist}").strip()
    except Exception:
        return ""


# Rank glyph templates — tesseract is blocky font pe Q ko A, 5 ko 9 jaisa padh deta hai.
# rank_templates.json me live table se liye VERIFIED glyphs hain (rank -> fingerprints).
# Naya glyph sabse kareebi template se match hota hai; koi paas na ho tabhi tesseract.
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
            with open(os.path.join(base, "rank_templates.json"), encoding="utf-8") as f:
                data = json.load(f)
            return [(rank, int(fp, 2)) for rank, fps in data.items() for fp in fps]
        except Exception:
            continue
    return []


RANK_TEMPLATES = _load_templates()
TEMPLATE_MAX_DIST = 50       # 320 bits me se itne tak farak chalega (same-rank ~45, cross-rank 116+)
_rank_cache = {}             # sirf memory me — galat OCR disk pe pakka nahi hota


def _glyph_key(glyph):
    """Glyph ka chhota (16x20) binary fingerprint — halke anti-alias farak ko ignore karta hai."""
    g = glyph.resize((16, 20), Image.BILINEAR)
    return "".join("1" if v > 127 else "0" for v in g.tobytes())


def _match_template(key):
    """Sabse kareebi verified template ka rank, ya None agar koi kaafi paas nahi."""
    if not RANK_TEMPLATES:
        return None
    v = int(key, 2)
    dist, rank = min((bin(v ^ fp).count("1"), r) for r, fp in RANK_TEMPLATES)
    return rank if dist <= TEMPLATE_MAX_DIST else None


def _rank_glyph(card):
    """Card ke top-left se white rank glyph (cropped) nikalo — ya None."""
    cw, ch = card.size
    corner = card.crop((0, 0, int(cw * 0.65), int(ch * 0.65)))
    w, h = corner.size
    px = corner.convert("RGB").load()
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            # white rank letter — colored body/suit tint ka min channel low hota hai
            if min(r, g, b) > 115:
                mp[x, y] = 255
    mask = _largest_component(mask)
    bbox = mask.getbbox()
    return mask.crop(bbox) if bbox else None


def _read_rank(card):
    """Card ke top-left me white rank letter padho."""
    glyph = _rank_glyph(card)
    if not glyph:
        return None
    key = _glyph_key(glyph)
    if key in _rank_cache:
        return _rank_cache[key]
    tm = _match_template(key)
    if tm:
        _rank_cache[key] = tm
        return tm
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

    # Tesseract same image pe hamesha same jawab deta hai — isliye result (None bhi) cache
    if len(_rank_cache) > 2000:
        _rank_cache.clear()
    _rank_cache[key] = _ocr_rank(try_psm)
    return _rank_cache[key]


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


def add_rank_template(rank, fp):
    """Verified glyph fingerprint ko templates me add karo (memory + repo disk)."""
    global RANK_TEMPLATES
    v = int(fp, 2)
    if any(r == rank and f == v for r, f in RANK_TEMPLATES):
        return False
    RANK_TEMPLATES.append((rank, v))
    base = os.environ.get("PREFLOP_ROOT") or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "rank_templates.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault(rank, []).append(fp)
    try:
        json.dump(data, open(path, "w", encoding="utf-8"))
    except Exception:
        pass
    return True


def learn_from_hand(hand):
    """User ne cards manually correct kiye — unka glyph template bana lo.
    hand = "4c8d" (4-char). Hero ke left/right glyphs ko is label se save karta hai."""
    img = state.get("last_img")
    if not img or not isinstance(hand, str) or len(hand) != 4:
        return {"ok": False, "error": "no frame / invalid hand"}
    glyphs = []
    for x, y, bw, bh, _area in _find_card_blobs(img):
        for sx, sy, sw, sh in _split_wide(x, y, bw, bh):
            g = _rank_glyph(img.crop((sx, sy, sx + sw, sy + sh)))
            if g is not None:
                glyphs.append((sx, g))
    if len(glyphs) < 2:
        return {"ok": False, "error": "hero cards not found"}
    glyphs.sort(key=lambda g: g[0])       # left -> right
    added = []
    for rank, (_, g) in zip((hand[0], hand[2]), glyphs[:2]):
        rank = rank.upper()
        if rank in "AKQJT98765432" and add_rank_template(rank, _glyph_key(g)):
            added.append(rank)
    return {"ok": True, "added": added}


def read_card(card_img):
    """Ek card se (rank, suit) nikalo — rank OCR se, suit body color se."""
    if not card_img:
        return None, None
    cw, ch = card_img.size
    if cw < 8 or ch < 8:
        return None, None
    rank = _read_rank(card_img)
    suit = _suit_from_color(_body_color(card_img))
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
    mask = (red | green | cyan | gray)
    mask[:int(h * y0f), :] = False
    mask[int(h * y1f):, :] = False
    mask = (mask.astype(_np.uint8)) * 255
    n, labels, stats, _ = _cv2.connectedComponentsWithStats(mask, 8)
    min_area = int(w * h * 0.0004)
    min_w, min_h = int(w * 0.03), int(h * 0.05)
    blobs = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < min_area or bw < min_w or bh < min_h:
            continue
        if area / (bw * bh) < 0.6:      # solid rectangle hi card hai (text/strips nahi)
            continue
        blobs.append((int(x), int(y), int(bw), int(bh), int(area)))
    return blobs


def _split_wide(x, y, bw, bh):
    """Overlapping cards ek wide blob me ho sakte hain — beech se tod do."""
    if bw > 1.55 * bh:
        hw = bw // 2
        return [(x, y, hw, bh), (x + hw, y, bw - hw, bh)]
    return [(x, y, bw, bh)]


def _pick_pair(cards):
    """Valid cards me se hero ka side-by-side pair chuno."""
    if len(cards) == 2:
        a, b = sorted(cards, key=lambda c: c[0])
        if abs((a[1] + a[3] / 2) - (b[1] + b[3] / 2)) <= 0.6 * max(a[3], b[3]):
            return [a, b]
        return None
    if len(cards) < 2:
        return None
    best, best_dist = None, 1e18
    for i in range(len(cards)):
        for j in range(i + 1, len(cards)):
            a, b = cards[i], cards[j]
            if abs((a[1] + a[3] / 2) - (b[1] + b[3] / 2)) > 0.6 * max(a[3], b[3]):
                continue
            dist = abs((a[0] + a[2] / 2) - (b[0] + b[2] / 2))
            if dist < best_dist:
                best_dist, best = dist, (a, b)
    return [best[0], best[1]] if best else None


def detect(img):
    """Hero ke dono cards padho — position auto-detect, rank + suit."""
    if not img or not USE_OCR or not HAS_PIL or not HAS_TESS:
        return None, None
    # 1) card-body blobs dhoondo, 2) har blob se card padho (sirf valid rakhna)
    cards = []
    for x, y, bw, bh, _area in _find_card_blobs(img):
        for sx, sy, sw, sh in _split_wide(x, y, bw, bh):
            r, s = read_card(img.crop((sx, sy, sx + sw, sy + sh)))
            if r and s:
                cards.append((sx, sy, sw, sh, r + s))
    # 3) side-by-side pair
    pair = _pick_pair(cards)
    if pair and len(pair) == 2:
        return pair[0][4] + pair[1][4], pair[0][4] + " " + pair[1][4]
    # fallback: fixed HERO_REGION (agar dynamic detect fail ho)
    crop, _ = crop_hero(img)
    fixed = isolate_cards(crop) if crop else []
    parts = []
    for c in fixed[:2]:
        r, s = read_card(c)
        if r and s:
            parts.append(r + s)
    if len(parts) == 2:
        return parts[0] + parts[1], " ".join(parts)
    return None, None


def detect_board(img):
    """Board (flop/turn/river) cards — table ke center band se, left->right order me."""
    if not img or not HAS_CV or not HAS_TESS or not board_present(img):
        return None
    blobs = _find_card_blobs(img, y0f=0.30, y1f=0.58)
    cards = []
    for x, y, bw, bh, _area in blobs:
        if bh < 8:
            continue
        n = max(1, round(bw / bh))          # overlapping board cards ek wide blob me
        for k in range(n):
            sx = x + int(k * bw / n)
            sw = max(8, int(bw / n))
            r, s = read_card(img.crop((sx, y, sx + sw, y + bh)))
            if r and s:
                cards.append((sx, y, sw, bh, r + s))
    if len(cards) < 3:
        return None
    # same row wale cards: y-center ke paas, sabse bada cluster
    cards.sort(key=lambda c: c[0])
    board = [c[4] for c in cards[:5]]
    # duplicate remove (overlap se same card do baar aa sakta hai)
    seen, out = set(), []
    for c in board:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out if 3 <= len(out) <= 5 else None


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


def detect_action(img):
    """Preflop/postflop action: position, bets, aur board. {"street", "hero", "bets", "board"}."""
    b = _button_seat(img)
    if b is None:
        return None
    pos = seat_positions(img, b)
    hero = pos.get(HERO_SEAT)
    if board_present(img):
        board = detect_board(img)
        return {"street": "postflop", "hero": hero, "bets": {},
                "board": board or [], "streetName": _street_name(board)}
    bets = read_bets(img)
    return {"street": "preflop", "hero": hero,
            "bets": {pos[i]: amt for i, amt in enumerate(bets) if amt > 0 and i in pos}}


def _street_name(board):
    """Board card count se street — 3=flop, 4=turn, 5=river."""
    if not board:
        return None
    return {3: "flop", 4: "turn", 5: "river"}.get(len(board))


def action_loop():
    """Background: har naye frame pe cards + bets/action padho (capture loop ko rokta nahi).
    Action OCR flicker se bachne ke liye 2 frame same aaye tabhi publish."""
    prev = None
    seen = None
    while True:
        img = state.get("last_img")
        if img is None or img is seen or state["blocked"] or not HAS_TESS:
            time.sleep(0.03)
            continue
        seen = img
        if USE_OCR and state["manual_hand"] is None:
            try:
                hand, raw = detect(img)
                state["raw"] = raw
                if hand:
                    state["last_ocr"] = hand
                elif raw:
                    state["last_ocr"] = None
            except Exception:
                pass
        if HAS_CV:
            try:
                act = detect_action(img)
                state["position"] = act and act["hero"]
                if act is not None and act == prev:
                    state["action"] = act
                prev = act
            except Exception:
                pass


def loop():
    """Background: capture frames + detect cards. Window dobara dhoondta rehta hai."""
    last_find = 0.0
    last_pin = 0.0
    fail_count = 0
    while True:
        try:
            now = time.time()
            # Har 2 sec (ya jab window nahi hai / capture fail ho raha hai) dobara dhoondo
            if not state["window_hwnd"] or now - last_find > 2 or fail_count >= 3:
                wins = find_windows(WINDOW_TITLE, PROCESS_NAMES)
                state["windows"] = wins
                hwnd = pick_window(wins)
                state["window_hwnd"] = hwnd
                state["window_title"] = win32gui.GetWindowText(hwnd) if hwnd else None
                last_find = now
                fail_count = 0

            # Pin on top — table ko hamesha aage rakho
            if state["pinned"] and state["window_hwnd"] and now - last_pin > 1.5:
                pin_top(state["window_hwnd"], True)
                last_pin = now

            state["blocked"] = is_capture_blocked(state["window_hwnd"])
            if state["blocked"]:
                # Purana / galat frame mat dikhao
                state["frame_jpeg"] = None
                state["last_ocr"] = None
                time.sleep(0.5)
                continue

            img = capture(state["window_hwnd"]) if state["window_hwnd"] else None
            if img:
                fail_count = 0
                from io import BytesIO
                buf = BytesIO()
                img.save(buf, "JPEG", quality=80)
                state["frame_jpeg"] = buf.getvalue()
                state["last_img"] = img            # action_loop isse cards + bets padhta hai
            else:
                fail_count += 1
                if fail_count >= 6:
                    state["window_hwnd"] = None   # window band ho gayi — dobara dhoondo
        except Exception:
            pass
        time.sleep(0.08)


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
                "connected": bool(state["window_hwnd"]),
                "table_count": len(state.get("windows", [])),
                "pinned": state.get("pinned", False),
                "blocked": state.get("blocked", False),
            })
        elif self.path.startswith("/api/windows"):
            self._send(200, {
                "windows": [{"title": t, "hwnd": int(h)} for h, t in state.get("windows", [])],
                "selected": state.get("selected_title"),
            })
        elif self.path.startswith("/api/frame"):
            if state["frame_jpeg"]:
                self._send(200, state["frame_jpeg"], "image/jpeg")
            else:
                self._send(404, {"error": "no frame"})
        elif self.path.startswith("/api/region"):
            # Hero region ka crop (tuning ke liye)
            from io import BytesIO
            if state["frame_jpeg"] and HAS_PIL:
                img = Image.open(BytesIO(state["frame_jpeg"]))
                crop, _ = crop_hero(img)
                if crop:
                    buf = BytesIO()
                    crop.save(buf, "JPEG", quality=90)
                    self._send(200, buf.getvalue(), "image/jpeg")
                    return
            self._send(404, {"error": "no frame"})
        elif self.path.startswith("/api/detect"):
            hand = state["manual_hand"] or state["last_ocr"]
            self._send(200, {"hand": hand, "raw": state.get("raw", ""),
                             "pos": state.get("position"),
                             "action": state.get("action")})
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
                state["window_hwnd"] = None   # force re-pick on next loop
                self._send(200, {"ok": True, "selected": state["selected_title"]})
            except Exception as e:
                self._send(400, {"error": str(e)})
        elif self.path.startswith("/api/focus"):
            bring_to_front(state["window_hwnd"])
            self._send(200, {"ok": True})
        elif self.path.startswith("/api/pin"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                state["pinned"] = bool(data.get("on"))
                pin_top(state["window_hwnd"], state["pinned"])
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
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    main()
