"""Unfolded-card tracker — NEW, fully separate module (existing mirror_server code untouched).

Red Star flicker ka asli kaaran: fold ke baad opponent ke cards kabhi-kabhi DIM (washed-out)
dikhte rehte hain, jinhe OCR dobara "in hand" samajh leta hai. Ye module har opponent seat ka
status alag karta hai:
    "in_hand"  — blue card back (abhi hand me, fold nahi hua)
    "dim"      — dim/washed-out card (fold ho chuka, Red Star ghost dikha raha hai)
    "none"     — card gayab (fold ho chuka, kuch nahi dikh raha)

Yeh sirf READ-ONLY detector hai — koi global state nahi badalta, koi existing function edit
nahi karta. Agar output sahi lage to ise baad me tracker me plug kar sakte ho.

Standalone test:  python unfolded_tracker.py <frame.jpg>
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image

import mirror_server as ms   # sirf SEAT_ANCHORS / BOARD_REGION / cv2 alias / _is_blue_back reuse


def _dim_body(crop):
    """Card body washed-out (dim) hai? (_is_dim_card jaisa heuristic — is module ki apni copy,
    mirror_server ki definition ko chhuna nahi hai)."""
    a = np.asarray(crop.convert("RGB"), dtype=np.int32)
    a = a[:max(1, int(a.shape[0] * 0.55))]
    r, g, b = (int(v) for v in np.median(a.reshape(-1, 3), axis=0))
    return 140 <= min(r, g, b) <= 190 and max(r, g, b) - min(r, g, b) < 70


def detect_unfolded(img):
    """{screen_seat: status} — har opponent seat ka unfolded/folded state (hero seat 1 chhodo)."""
    a = np.asarray(img.convert("RGB"), dtype=np.int32)
    h, w = a.shape[:2]
    R, G, B = a[..., 0], a[..., 1], a[..., 2]

    # 1) blue card back = in-hand (unfolded)
    blue = ((B > R + 40) & (B > 110) & (G < B)).astype(np.uint8)
    # 2) dim card = washed-out body (folded ghost) — high RGB + low saturation
    mx = np.maximum(np.maximum(R, G), B)
    mn = np.minimum(np.minimum(R, G), B)
    dim = ((mn > 130) & (mx - mn < 60)).astype(np.uint8)

    seats_blue = set()
    seats_dim = set()
    bx0, by0, bx1, by1 = ms.BOARD_REGION

    for mask, want_blue in ((blue, True), (dim, False)):
        n, _, st, cen = ms._cv2.connectedComponentsWithStats(mask, 8)
        for i in range(1, n):
            x, y, bw, bh, ar = (int(v) for v in st[i])
            if ar < w * h * 0.0008 or bh < h * 0.03:
                continue
            cx, cy = cen[i][0] / w, cen[i][1] / h
            if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                continue                                   # board region ignore
            s = min(range(len(ms.SEAT_ANCHORS)),
                    key=lambda k: (cx - ms.SEAT_ANCHORS[k][0]) ** 2
                                  + (cy - ms.SEAT_ANCHORS[k][1]) ** 2)
            if s == ms.HERO_SEAT:
                continue
            crop = img.crop((x, y, x + bw, y + bh))
            if want_blue and ms._is_blue_back(crop):
                seats_blue.add(s)
            elif not want_blue and _dim_body(crop):
                seats_dim.add(s)

    out = {}
    for s in range(len(ms.SEAT_ANCHORS)):
        if s == ms.HERO_SEAT:
            continue
        if s in seats_blue:
            out[s] = "in_hand"
        elif s in seats_dim:
            out[s] = "dim"
        else:
            out[s] = "none"
    return out


def unfolded_positions(img, posmap):
    """{position: True/False} — unfolded (in-hand) opponents ko position ke naam se.
    posmap = {screen_seat: "BTN"/"SB"/...} (mirror_server ka xml_pos/posmap)."""
    st = detect_unfolded(img)
    return {posmap.get(s): (v == "in_hand") for s, v in st.items() if posmap.get(s)}


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "debug_frame.jpg"
    img = Image.open(p)
    print("frame:", img.size)
    print("detect_unfolded:", detect_unfolded(img))
