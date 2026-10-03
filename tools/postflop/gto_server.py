"""
Postflop GTO full-hand simulator server (GTO Wizard style).

Runs the whole hand for the existing preflop-trainer app:
  - Flop strategy comes from the pre-solved TexasSolver dumps in work/*_output.json.
  - Turn / river strategies are solved ON DEMAND (console_solver.exe, ~5s each) and
    cached on disk, using reach-weighted ranges (street re-solving).
  - Optional DeepSeek AI coach (reads DEEPSEEK_API_KEY from environment).

Stdlib only. Run:
    python tools/postflop/gto_server.py            # port 8675
    $env:DEEPSEEK_API_KEY="sk-..."                 # optional, enables AI coach

API (JSON):
  GET  /api/health            -> {"ok":true,"ai":bool,"spots":N}
  GET  /api/spots             -> [{id,board,texture,pot,eff,heroPos,villPos}]
  POST /api/new     {spotId?} -> state (villain auto-played up to hero's turn)
  POST /api/act     {handId,key} -> {state,lastDecision}
  POST /api/coach   {handId,focus?} -> {text}
"""
import json
import os
import random
import re
import subprocess
import threading
import hashlib
import time
import queue as _queue
from itertools import combinations
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rangeutil

# PREFLOP_ROOT: desktop exe repo ka path deta hai (frozen build me __file__ temp dir hota hai)
ROOT = Path(os.environ.get("PREFLOP_ROOT") or Path(__file__).resolve().parent.parent.parent)
SOLVER_DIR = ROOT / "tools" / "postflop" / "solver" / "extracted" / "TexasSolver-v0.2.0-Windows"
SOLVER_EXE = SOLVER_DIR / "console_solver.exe"
# SOLVER_OFF marker file hone pe console_solver.exe bilkul spawn nahi hota (memory overload
# se bachne ke liye). File hatao to solver wapas on. Env PREFLOP_SOLVER_OFF=1 bhi kaam karta hai.
SOLVER_OFF_MARKER = SOLVER_DIR / "SOLVER_OFF"


def solver_off():
    return SOLVER_OFF_MARKER.exists() or os.environ.get("PREFLOP_SOLVER_OFF") == "1"
WORK_DIR = ROOT / "tools" / "postflop" / "work"
CACHE_DIR = WORK_DIR / "gto_cache"

POT = 6.0
EFF_STACK = 97.0
PORT = int(os.environ.get("GTO_PORT", "8675"))
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

TURN_ITER = int(os.environ.get("GTO_TURN_ITER", "50"))
RIVER_ITER = int(os.environ.get("GTO_RIVER_ITER", "50"))
THREADS = int(os.environ.get("GTO_THREADS", "6"))
BG_THREADS = int(os.environ.get("GTO_BG_THREADS", "4"))   # background solve (gentle) ke liye kam threads

OPEN_BTN = "22+, A2s+, K2s+, Q2s+, J4s+, T6s+, 95s, 96s, 97s, 98s, 84s, 85s, 86s, 87s, 74s, 75s, 76s, 63s, 64s, 65s, 53s, 54s, 43s, A2o+, K9o+, QTo+, JTo"
OPEN_BTN_B = "J2s, J3s, T5s, 94s, 83s, 73s, 62s, 52s, 42s, 32s, K7o, K8o, Q8o, Q9o, J8o, J9o, T8o, T9o, 98o, 87o"
BB_CALL = "22-66, A6s-A8s, K2s-K9s, Q2s-Q9s, J2s-J8s, T6s-T8s, 95s, 96s, 97s, 84s, 85s, 86s, 74s, 75s, 63s, 64s, 53s, A2o-A9o, K9o-KQo, QTo, QJo, JTo"
BB_CALL_B = "T5s, 94s, 83s, 73s, 62s, 52s, 43s, K8o, K7o, Q9o, J9o, T9o, 98o"

# Solver ko shorthand nahi, expanded class list chahiye ("22+"/"A2s+" reject hota hai)
IP_RANGE = rangeutil.weighted_range_string(OPEN_BTN, OPEN_BTN_B)
OOP_RANGE = rangeutil.weighted_range_string(BB_CALL, BB_CALL_B)

# id, board (TexasSolver format), texture label
BOARDS = [
    ("a72r",   "As,7d,2c", "dry, ace-high, rainbow"),
    ("a83r",   "Ad,8h,3s", "dry, ace-high, rainbow"),
    ("k83r",   "Kc,8d,3h", "dry, king-high, rainbow"),
    ("k62r",   "Kh,6s,2d", "dry, king-high, rainbow"),
    ("q94r",   "Qs,9d,4c", "dry, queen-high, rainbow"),
    ("jt6tt",  "Jh,Th,6c", "wet, two-tone, connected"),
    ("98stt",  "9s,8s,5d", "wet, two-tone, connected"),
    ("876tt",  "8h,7h,6c", "wet, two-tone, connected"),
    ("t98tt",  "Th,9h,8d", "wet, two-tone, connected"),
    ("kk2r",   "Kd,Kc,2s", "paired, high"),
    ("883r",   "8d,8c,3h", "paired, mid"),
    ("552r",   "5h,5s,2c", "paired, low"),
    ("a72mono","Ah,7h,2h", "monotone"),
    ("963mono","9s,6s,3s", "monotone"),
    ("654r",   "6h,5d,4c", "low, connected, rainbow"),
    ("764r",   "7s,6d,4h", "low, one-gap, rainbow"),
]
SPOTS = []
for _i, (_bid, _board, _tex) in enumerate(BOARDS):
    SPOTS.append({
        "id": _i,
        "board": [c for c in _board.split(",")],
        "texture": _tex,
        "pot": POT,
        "eff": EFF_STACK,
        "heroPos": "BTN",
        "villPos": "BB",
    })

INPUT_TEMPLATE = """set_pot {pot}
set_effective_stack {eff}
set_board {board}
set_range_ip {ip_range}
set_range_oop {oop_range}
set_bet_sizes oop,flop,bet,33
set_bet_sizes oop,flop,allin
set_bet_sizes ip,flop,bet,33,75
set_bet_sizes ip,flop,raise,75
set_bet_sizes ip,flop,allin
set_bet_sizes oop,turn,bet,66
set_bet_sizes oop,turn,raise,75
set_bet_sizes oop,turn,allin
set_bet_sizes ip,turn,bet,66
set_bet_sizes ip,turn,raise,75
set_bet_sizes ip,turn,allin
set_bet_sizes oop,river,bet,66
set_bet_sizes oop,river,raise,75
set_bet_sizes oop,river,allin
set_bet_sizes ip,river,bet,66
set_bet_sizes ip,river,raise,75
set_bet_sizes ip,river,allin
set_allin_threshold 0.67
build_tree
set_thread_num {threads}
set_accuracy 0.01
set_max_iteration {max_iter}
set_print_interval 20
set_use_isomorphism 1
start_solve
set_dump_rounds 1
dump_result {out_json}
"""

# Flop-only template — turn/river bet sizes nahi (tree terminal = ~10x faster).
# Flop C-bet advice ke liye kaafi accurate; exact pre-solved boards pe ye use nahi hota.
FLOP_TEMPLATE = """set_pot {pot}
set_effective_stack {eff}
set_board {board}
set_range_ip {ip_range}
set_range_oop {oop_range}
set_bet_sizes oop,flop,bet,33
set_bet_sizes oop,flop,allin
set_bet_sizes ip,flop,bet,33,75
set_bet_sizes ip,flop,raise,75
set_bet_sizes ip,flop,allin
set_allin_threshold 0.67
build_tree
set_thread_num {threads}
set_accuracy 0.01
set_max_iteration {max_iter}
set_print_interval 20
set_use_isomorphism 1
start_solve
set_dump_rounds 1
dump_result {out_json}
"""

_solve_lock = threading.Lock()
_nolock = __import__("contextlib").nullcontext()

# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------
RANKS = "23456789TJQKA"
SUITS = "shdc"


def rank_i(c):
    return RANKS.index(c.upper())


def card_key(c):
    return c[0].upper() + c[1].lower()


def parse_card(c):
    return (RANKS.index(c[0].upper()), c[1].lower())


def combo_key(c1, c2):
    a, b = card_key(c1), card_key(c2)
    if RANKS.index(a[0]) >= RANKS.index(b[0]):
        return a + b
    return b + a


def card_in(card, cards):
    return card_key(card) in {card_key(x) for x in cards}


def fresh_deck():
    return [r + s for r in RANKS for s in SUITS]


# --------------------------------------------------------------------------
# Hand evaluation (best 5 of 7)
# --------------------------------------------------------------------------
def five_rank(cards):
    ranks = sorted((c[0] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    flush = len(set(suits)) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight = False
    high = 0
    if len(uniq) == 5 and uniq[0] - uniq[-1] == 4:
        straight, high = True, uniq[0]
    elif set(uniq) == {12, 3, 2, 1, 0}:
        straight, high = True, 3
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    if straight and flush:
        return (8, [high])
    if groups[0][1] == 4:
        return (7, [groups[0][0], groups[1][0]])
    if groups[0][1] == 3 and groups[1][1] == 2:
        return (6, [groups[0][0], groups[1][0]])
    if flush:
        return (5, ranks)
    if straight:
        return (4, [high])
    if groups[0][1] == 3:
        return (3, [groups[0][0]] + [g[0] for g in groups[1:]])
    if groups[0][1] == 2 and groups[1][1] == 2:
        pr = sorted([groups[0][0], groups[1][0]], reverse=True)
        return (2, pr + [g[0] for g in groups[2:]])
    if groups[0][1] == 2:
        return (1, [groups[0][0]] + [g[0] for g in groups[1:]])
    return (0, ranks)


def seven_rank(cards):
    best = None
    for combo in combinations(cards, 5):
        r = five_rank(list(combo))
        if best is None or r > best:
            best = r
    return best


HAND_NAMES = {8: "Straight Flush", 7: "Quads", 6: "Full House", 5: "Flush",
              4: "Straight", 3: "Trips", 2: "Two Pair", 1: "Pair", 0: "High Card"}


# --------------------------------------------------------------------------
# Solver interface
# --------------------------------------------------------------------------
def solve(board_str, pot, stack, ip_range, oop_range, out_name, max_iter, template=None, threads=None,
          low_priority=False, lock=True):
    """Run console_solver.exe. Returns parsed JSON (dump_rounds=1)."""
    if solver_off():
        raise RuntimeError("solver OFF (SOLVER_OFF marker present)")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    in_path = CACHE_DIR / f"{out_name}_input.txt"
    out_path = SOLVER_DIR / f"{out_name}.json"
    script = (template or INPUT_TEMPLATE).format(
        pot=pot, eff=stack, board=board_str,
        ip_range=ip_range, oop_range=oop_range,
        threads=threads or THREADS, max_iter=max_iter, out_json=f"{out_name}.json",
    )
    in_path.write_text(script, encoding="utf-8")
    out_path.unlink(missing_ok=True)
    log_path = CACHE_DIR / f"{out_name}.log"
    # Windows pe solver HAMESHA gentle priority pe chale — warna 6-20 CPU threads
    # poora system hila dete hain (UI freeze / mouse lag).
    #   live solve -> BELOW_NORMAL_PRIORITY_CLASS (0x4000)
    #   background -> IDLE_PRIORITY_CLASS      (0x40)   (library builder, sirf free CPU pe)
    flags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW: windowed app se console_solver spawn karne par har solve pe
        # kala console window flash hota tha — ab chupchaap (no console) chalega.
        flags = subprocess.CREATE_NO_WINDOW | (0x00000040 if low_priority else 0x00004000)
    with (_solve_lock if lock else _nolock):
        with open(log_path, "w") as lf:
            subprocess.run([str(SOLVER_EXE), "-i", str(in_path)],
                           cwd=str(SOLVER_DIR), stdout=lf, stderr=subprocess.STDOUT,
                           check=True, creationflags=flags)
    if not out_path.exists():
        raise RuntimeError(f"solve failed for {out_name}; see {log_path}")
    return json.loads(out_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Range helpers
# --------------------------------------------------------------------------
def node_strategy(node):
    return node["strategy"]["strategy"]


def range_combos_from_node(node):
    return list(node_strategy(node).keys())


def action_amount(key):
    m = re.match(r"(?:BET|RAISE) ([\d.]+)", key)
    return float(m.group(1)) if m else None


def action_label(key, pot, stack=EFF_STACK):
    if key == "CHECK":
        return {"label": "Check", "cls": "k-call", "kind": "check"}
    if key == "CALL":
        return {"label": "Call", "cls": "k-call", "kind": "call"}
    if key == "FOLD":
        return {"label": "Fold", "cls": "k-fold", "kind": "fold"}
    m = re.match(r"(BET|RAISE) ([\d.]+)", key)
    if m:
        amt = float(m.group(2))
        if amt >= stack - 1:
            return {"label": "All-in", "cls": "k-allin", "kind": "allin"}
        if m.group(1) == "BET":
            pct = round(amt / pot * 100)
            return {"label": f"Bet {pct}% pot", "cls": "k-raise", "kind": "bet"}
        return {"label": f"Raise to {amt:.0f}bb", "cls": "k-3bet", "kind": "raise"}
    return {"label": key, "cls": "k-call", "kind": "?"}


def reach_weights(line, tree, prior=None):
    """Walk `tree` along `line` (solver action strings). Returns (ip_w, oop_w).
    `prior` = (ip_w, oop_w) reach weights the tree's ranges were built from, so the
    river ranges still reflect the flop action and not only the turn action."""
    ip_w, oop_w = {}, {}
    p_ip, p_oop = prior if prior else ({}, {})

    def collect(n):
        if n.get("node_type") == "action_node":
            ip = n.get("player") == 0
            for combo in node_strategy(n):
                (ip_w if ip else oop_w).setdefault(combo, (p_ip if ip else p_oop).get(combo, 1.0))
            for v in n.get("childrens", {}).values():
                collect(v)

    collect(tree)
    node = tree
    for act in line:
        if node is None or node.get("node_type") != "action_node":
            break
        st = node_strategy(node)
        player = node.get("player")
        actions = node.get("actions", [])
        if act in actions:
            idx = actions.index(act)
            weights = ip_w if player == 0 else oop_w
            for combo, probs in st.items():
                if combo in weights:
                    weights[combo] = max(weights[combo] * probs[idx], 0.01)
        node = node.get("childrens", {}).get(act) if node.get("childrens") else None
    return ip_w, oop_w


def combo_to_class(combo):
    """'AhKh' -> 'AKs', 'AhKd' -> 'AKo', 'AhAd' -> 'AA' (solver range shorthand)."""
    r1, s1 = combo[0], combo[1]
    r2, s2 = combo[2], combo[3]
    if r1 == r2:
        return r1 + r2
    hi, lo = (r1, r2) if RANKS.index(r1) >= RANKS.index(r2) else (r2, r1)
    return hi + lo + ("s" if s1 == s2 else "o")


def weighted_range_string_from(weights, board_cards, min_w=0.005):
    """Aggregate per-combo reach weights into solver shorthand classes (mean per class)."""
    from collections import defaultdict
    cls_sum = defaultdict(float)
    cls_cnt = defaultdict(int)
    for combo, w in weights.items():
        if w < min_w:
            continue
        if card_in(combo[0:2], board_cards) or card_in(combo[2:4], board_cards):
            continue
        cls = combo_to_class(combo)
        cls_sum[cls] += w
        cls_cnt[cls] += 1
    parts = []
    for cls in sorted(cls_sum):
        w = cls_sum[cls] / cls_cnt[cls]
        if w < min_w:
            continue
        parts.append(f"{cls}:{round(w, 4)}")
    return ",".join(parts)


# --------------------------------------------------------------------------
# Live Mirror postflop recommendation (board + hero hand -> GTO mix)
# --------------------------------------------------------------------------
FLOP_ITER = int(os.environ.get("GTO_FLOP_ITER", "30"))


def _pre_solved_flop(board_list):
    """16 pre-solved flop boards me se match — card set se (order matter nahi).
    Mil gaya to INSTANT result (koi solve nahi)."""
    target = {card_key(c) for c in board_list}
    for _bid, bstr, _tex in BOARDS:
        if {card_key(c) for c in bstr.split(",")} == target:
            path = WORK_DIR / f"{_bid}_output.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
    return None


def _live_cache_path(key):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def _flop_key(board_list, pot, stack):
    return hashlib.md5(f"flop|{','.join(board_list)}|{round(pot,1)}|{round(stack,1)}".encode()).hexdigest()[:16]


def _flop_tree(board_list, pot=POT, stack=EFF_STACK, threads=None):
    """Flop tree — exact pre-solved (instant) ya flop-only on-demand solve (~5s). Returns (tree, approx)."""
    if pot == POT and stack == EFF_STACK:
        pre = _pre_solved_flop(board_list)
        if pre is not None:
            return pre, False
    board_str = ",".join(board_list)
    key = _flop_key(board_list, pot, stack)
    path = _live_cache_path(key)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), False
    tree = solve(board_str, pot, stack, IP_RANGE, OOP_RANGE, key, FLOP_ITER, template=FLOP_TEMPLATE, threads=threads)
    path.write_text(json.dumps(tree), encoding="utf-8")
    return tree, False


# --------------------------------------------------------------------------
# Background gentle solver — live me naye boards queue me daal ke ek-ek solve
# karo (kam threads + beech me saans) taaki PC chude nahi aur pre-solved library
# dheere-dheere badhti rahe. /api/live?bg=1 is queue ka use karta hai.
# --------------------------------------------------------------------------
_bg_jobs = _queue.Queue()
_bg_queued = set()
_bg_lock = threading.Lock()


def enqueue_flop(board_list, pot, stack):
    key = _flop_key(board_list, pot, stack)
    with _bg_lock:
        if key in _bg_queued:
            return
        _bg_queued.add(key)
    _bg_jobs.put(("flop", board_list, pot, stack))


def _bg_worker():
    while True:
        job = _bg_jobs.get()
        if job is None:
            return
        kind, board_list, pot, stack = job
        try:
            if kind == "flop":
                _flop_tree(board_list, pot, stack, threads=BG_THREADS)
        except Exception:
            pass
        time.sleep(1.0)   # solve ke beech pause — CPU ko saans


def solve_street_tree(board_list, pot, stack, ip_w, oop_w, max_iter):
    """Turn/river board ka tree — reach-weighted ranges se, disk cache ke saath."""
    board_str = ",".join(board_list)
    ip_r = weighted_range_string_from(ip_w, board_list)
    oop_r = weighted_range_string_from(oop_w, board_list)
    key = hashlib.md5(
        (board_str + "|" + str(round(pot, 1)) + "|s" + str(round(stack, 1))
         + "|" + ip_r + "|" + oop_r + "|i" + str(max_iter)).encode()).hexdigest()[:16]
    path = _live_cache_path(key)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    tree = solve(board_str, pot, stack, ip_r, oop_r, key, max_iter)
    path.write_text(json.dumps(tree), encoding="utf-8")
    return tree


def _hero_ip_node(tree, facing):
    """IP (hero=BTN) node — OOP ne check kiya ho to CHECK child, bet kiya ho to closest BET child."""
    if not tree or tree.get("node_type") != "action_node":
        return None
    children = tree.get("childrens", {})
    if facing <= 0:
        return children.get("CHECK")
    best, best_key = 1e18, None
    for a in tree.get("actions", []):
        m = re.match(r"BET ([\d.]+)", a)
        if m:
            amt = float(m.group(1))
            if abs(amt - facing) < best:
                best, best_key = abs(amt - facing), a
    return children.get(best_key) if best_key else None


def _hero_oop_node(tree, facing):
    """OOP (hero=BB) node — pehla action ho to root; IP ne bet kiya ho (hero ne check kiya tha)
    to CHECK -> closest BET child (call/fold/raise wala node). Pehle BB ko hamesha root milta tha,
    bet face karte waqt bhi bet/check strategy dikhti thi."""
    if not tree or tree.get("node_type") != "action_node":
        return None
    if facing <= 0:
        return tree
    return _hero_ip_node(tree.get("childrens", {}).get("CHECK"), facing)


def _hero_node(tree, facing, hero_pos):
    return _hero_oop_node(tree, facing) if hero_pos == "BB" else _hero_ip_node(tree, facing)


def live_recommend(board, hero, street="flop", pot=POT, stack=EFF_STACK, facing=0.0, hero_pos="BTN"):
    """Hero ke exact hand ki GTO strategy current street ke node pe.

    board: ["As","7d","2c",...] (3-5 cards)
    hero:  "AhKh" (4-char) ya ["Ah","Kh"]
    Assumes: hero BTN (IP) vs BB (OOP); prior streets check-check.
    """
    if not board or len(board) < 3 or len(board) > 5:
        return {"error": "invalid board"}
    if isinstance(hero, str):
        if len(hero) != 4:
            return {"error": "invalid hero"}
        hero = [hero[0:2], hero[2:4]]
    combo = combo_key(hero[0], hero[1])

    flop = board[:3]
    approx = False
    try:
        flop_tree, approx = _flop_tree(flop, pot, stack)
    except Exception as e:
        return {"error": f"flop solve failed: {e}"}

    if street == "flop":
        node = _hero_node(flop_tree, facing, hero_pos)
    elif street == "turn":
        flop_line = ["CHECK", "CHECK"]
        ip_w, oop_w = reach_weights(flop_line, flop_tree)
        turn_tree = solve_street_tree(board[:4], pot, stack, ip_w, oop_w, TURN_ITER)
        node = _hero_node(turn_tree, facing, hero_pos)
    elif street == "river":
        flop_line = ["CHECK", "CHECK"]
        ip_w, oop_w = reach_weights(flop_line, flop_tree)
        turn_tree = solve_street_tree(board[:4], pot, stack, ip_w, oop_w, TURN_ITER)
        turn_line = ["CHECK", "CHECK"]
        ip_w, oop_w = reach_weights(turn_line, turn_tree, (ip_w, oop_w))
        river_tree = solve_street_tree(board, pot, stack, ip_w, oop_w, RIVER_ITER)
        node = _hero_node(river_tree, facing, hero_pos)
    else:
        return {"error": "unknown street"}

    if not node or node.get("node_type") != "action_node":
        return {"error": "no decision node"}

    actions = node.get("actions", [])
    st = node_strategy(node).get(combo)
    if st is None:
        return {"error": "hand not in range", "actions": actions}
    mix = {a: round(float(st[i]), 4) for i, a in enumerate(actions)}
    labels = {a: action_label(a, pot, stack)["label"] for a in actions}
    best = max(mix, key=mix.get)
    return {
        "board": board, "street": street, "combo": combo,
        "strategy": mix, "best": best,
        "bestLabel": labels[best], "labels": labels,
        "actions": [{"key": a, **action_label(a, pot, stack)} for a in actions],
        "approx": bool(approx),
    }


# --------------------------------------------------------------------------
# Spot library — asli preflop spot (SRP / 3-bet pot, positions) ka pre-solved flop.
# Library me ho to lookup (memory cache ke baad microseconds). Na ho to usi spot ki ranges se
# live solve (library builder se upar priority) aur library me save — agli baar instant.
# --------------------------------------------------------------------------
import spotlib  # noqa: E402

_PRE_ORDER = ["UTG", "MP", "CO", "BTN", "SB", "BB"]
LIB_ITER = int(os.environ.get("LIB_ITER", "80"))
_POST_ORDER = ["SB", "BB", "UTG", "MP", "CO", "BTN"]
_live_jobs = _queue.Queue()
_live_pending = set()
_live_lock = threading.Lock()


def resolve_spot(hero_pos, villain_pos, pot_type):
    """-> (spot_id, hero_is_ip, exact). Preflop order me pehle wala = opener."""
    spots = spotlib.spots()
    if hero_pos not in _PRE_ORDER or villain_pos not in _PRE_ORDER or hero_pos == villain_pos:
        sid = "3bp_BTN_BB" if pot_type == "3bp" else "srp_BTN_BB"
        return sid, hero_pos == "BTN", False
    a, b = sorted([hero_pos, villain_pos], key=_PRE_ORDER.index)
    sid = f"3bp_{a}_{b}" if pot_type == "3bp" else f"srp_{a}_BB"
    exact = sid in spots and (pot_type == "3bp" or b == "BB")
    if sid not in spots:
        sid = "3bp_BTN_BB" if pot_type == "3bp" else "srp_BTN_BB"
    if exact:
        hero_ip = spots[sid]["ip"] == hero_pos
    else:
        hero_ip = _POST_ORDER.index(hero_pos) > _POST_ORDER.index(villain_pos)
    return sid, hero_ip, exact


def villain_range(hero_pos, villain_pos, pot_type="srp", board=None, action=None):
    """Villain ki range; board+action diya ho to postflop narrow (flop library se)."""
    if not hero_pos or not villain_pos:
        return {"error": "heroPos/villainPos chahiye"}
    sid, _hero_ip, exact = resolve_spot(hero_pos, villain_pos, pot_type)
    sp = spotlib.spots().get(sid)
    if not sp:
        return {"error": "spot nahi mila: " + sid}
    vill_ip = sp["ip"] == villain_pos
    rng = sp["ip_range"] if vill_ip else sp["oop_range"]
    out = {"spot": sid, "villain": villain_pos, "range": rng.get("main", ""),
           "border": rng.get("border", ""), "ip": vill_ip, "exact": bool(exact)}
    # postflop narrowing: sirf flop (3 cards) + villain ka action (check/bet) — turn/river ke liye
    # poora line track karna padta hai (reach-weighted), abhi flop hi reliable hai.
    if not (board and action and len(board) == 3):
        return out
    try:
        canon, _m = spotlib.canonical(board[:3])
        rep = canon if spotlib.lib_path(sid, canon).exists() else spotlib.representative(canon)
        nodes = spotlib.load(sid, rep)
        if not nodes:
            out["narrowed"] = "flop library nahi bani (build_library.py chalao) — sirf preflop range"
            return out
        node = nodes.get("c" if vill_ip else "r")
        if not node:
            return out
        acts = node.get("a", [])
        probs = node.get("s", {})
        bet_idxs = [i for i, a in enumerate(acts) if a.startswith("BET") or a.startswith("RAISE")]
        check_idx = acts.index("CHECK") if "CHECK" in acts else -1
        agg = {}
        for combo, ps in probs.items():
            if action in ("check", "call"):
                if check_idx < 0 or check_idx >= len(ps):
                    continue
                w = float(ps[check_idx]) / 1000.0
            else:
                if not bet_idxs:
                    break
                w = sum(float(ps[i]) for i in bet_idxs if i < len(ps)) / 1000.0
            if w >= 0.5:
                cls = combo_to_class(combo)
                agg[cls] = max(agg.get(cls, 0.0), w)
        if not agg:
            out["narrowed"] = ("flop " + ("check" if action in ("check", "call") else "bet") + " → koi strong combo nahi (thin/bluff)")
            return out
        top = sorted(agg.items(), key=lambda x: (-x[1], x[0]))[:18]
        out["narrowed"] = ("flop " + ("check" if action in ("check", "call") else "bet") + " → "
                           + ", ".join(f"{c}({int(round(w * 100))}%)" for c, w in top))
    except Exception as e:
        out["narrowed"] = "narrow fail: " + str(e)
    return out


def _spot_ranges(spot):
    sp = spotlib.spots()[spot]
    ip = rangeutil.weighted_range_string(sp["ip_range"]["main"], sp["ip_range"].get("border", ""))
    oop = rangeutil.weighted_range_string(sp["oop_range"]["main"], sp["oop_range"].get("border", ""))
    return sp, ip, oop


def _solve_spot_flop(spot, canon):
    """Representative flop POORE tree se (library builder jaisa) — flop-only tree galat tha."""
    canon = list(canon)
    sp, ip, oop = _spot_ranges(spot)
    name = f"live_{spot}_{''.join(canon)}"
    tree = solve(",".join(canon), sp["pot"], sp["eff"], ip, oop, name, LIB_ITER, template=INPUT_TEMPLATE)
    nodes = spotlib.extract_nodes(tree)
    if nodes:
        spotlib.save(spot, canon, nodes)
    (SOLVER_DIR / f"{name}.json").unlink(missing_ok=True)
    return tree


def _live_worker():
    while True:
        job = _live_jobs.get()
        try:
            job[0](*job[1:])
        except Exception:
            pass
        with _live_lock:
            _live_pending.discard(job[1:])


def _enqueue_live(fn, *args):
    with _live_lock:
        if args in _live_pending:
            return
        _live_pending.add(args)
    _live_jobs.put((fn, *args))


def _hand_combos(hand):
    r1, r2 = hand[0], hand[1]
    suited = hand[2:] == "s"
    out = []
    for s1 in "shdc":
        for s2 in "shdc":
            c1, c2 = r1 + s1, r2 + s2
            if c1 == c2:
                continue
            if r1 == r2 and "shdc".index(s1) >= "shdc".index(s2):
                continue
            if r1 != r2 and suited != (s1 == s2):
                continue
            out.append((c1, c2))
    return out


def _range_weights(range_str, board):
    """'AA,KQs:0.5,...' -> {combo: weight} (board cards hata ke)."""
    w = {}
    for tok in range_str.split(","):
        tok = tok.strip()
        if not tok:
            continue
        hand, _, wt = tok.partition(":")
        wt = float(wt) if wt else 1.0
        for c1, c2 in _hand_combos(hand):
            if c1 in board or c2 in board:
                continue
            w[combo_key(c1, c2)] = wt
    return w


def _checkcheck_reach(spot, canon, nodes, rep):
    """Flop check-check line ke baad dono ki ranges (turn solve ke liye) — rep board ki strategy
    same-feature mapping se har asli combo pe."""
    sp, ip, oop = _spot_ranges(spot)
    wi, wo = _range_weights(ip, canon), _range_weights(oop, canon)

    def p_check(key, cb):
        node = nodes.get(key)
        if not node or "CHECK" not in node["a"]:
            return 0.0
        if rep == canon and cb in node["s"]:
            v = node["s"][cb]
            return v[node["a"].index("CHECK")] / (sum(v) or 1)
        mix, _ = spotlib.mapped_strategy(key, node, rep, [cb[:2], cb[2:]], canon)
        return (mix or {}).get("CHECK", 0.0)
    oop_w = {cb: w * p_check("r", cb) for cb, w in wo.items()}
    ip_w = {cb: w * p_check("c", cb) for cb, w in wi.items()}
    return ip_w, oop_w


def _spot_turn_path(spot, board4):
    return _live_cache_path(hashlib.md5(("spot_turn|" + spot + "|" + ",".join(board4)).encode()).hexdigest()[:16])


def _solve_spot_turn(spot, board4):
    sp = spotlib.spots()[spot]
    canon = list(board4[:3])
    rep = spotlib.load(spot, canon) and canon or spotlib.representative(canon)
    nodes = spotlib.load(spot, rep)
    ip_w, oop_w = _checkcheck_reach(spot, canon, nodes, rep)
    tree = solve_street_tree(list(board4), sp["pot"], sp["eff"], ip_w, oop_w, TURN_ITER)
    _spot_turn_path(spot, board4).write_text(json.dumps(tree), encoding="utf-8")
    return tree


def _spot_full_flop_tree(spot, canon):
    """Spot ka POORA flop tree (canon board pe, disk cached) — asli line ke reach weights
    ke liye. Library sirf 2-level nodes rakhti hai (check-check / check-bet), isliye bet-call /
    bet-raise jaisi lines ke liye poora tree chahiye (reach_weights walk karta hai)."""
    canon = list(canon)
    key = hashlib.md5(f"ftree|{spot}|{''.join(canon)}".encode()).hexdigest()[:16]
    path = _live_cache_path(key)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    sp, ip, oop = _spot_ranges(spot)
    name = f"live_ftree_{spot}_{''.join(canon)}"
    tree = solve(",".join(canon), sp["pot"], sp["eff"], ip, oop, name, LIB_ITER)
    path.write_text(json.dumps(tree), encoding="utf-8")
    (SOLVER_DIR / f"{name}.json").unlink(missing_ok=True)
    return tree


def _match_tree_action(node, kind, amt=0.0):
    """Real action (check/call/fold/bet/raise + bb) -> is node ke tree ka nearest action string."""
    if node is None or node.get("node_type") != "action_node":
        return None
    acts = node.get("actions", [])
    if kind == "check":
        return "CHECK" if "CHECK" in acts else None
    if kind == "call":
        return "CALL" if "CALL" in acts else None
    if kind == "fold":
        return "FOLD" if "FOLD" in acts else None
    if kind in ("bet", "raise", "open", "3bet", "4bet", "5bet", "6bet"):
        best, best_key = 1e18, None
        for a in acts:
            m = re.match(r"(BET|RAISE) ([\d.]+)", a)
            if m:
                d = abs(float(m.group(2)) - amt)
                if d < best:
                    best, best_key = d, a
        return best_key
    return None


def _walk_line(events, tree):
    """Client ke events [{pos, act, amt}] (chronological, hero+villain) -> actual tree action
    strings. Har step pe child node me jaate hue real amount ko nearest tree size se match karo."""
    out = []
    node = tree
    for e in events:
        if node is None or node.get("node_type") != "action_node":
            break
        a = _match_tree_action(node, e.get("act"), float(e.get("amt", 0.0) or 0.0))
        if a is None:
            break
        out.append(a)
        node = node.get("childrens", {}).get(a) if node.get("childrens") else None
    return out


def _similar_mix(strat_by_combo, actions, hero, board, k=5):
    """Hero ka hand range me nahi (chart se alag khela) -> board pe usi taaqat ke range hands
    (made-hand rank + flush draw) me se k sabse kareeb ki average strategy."""
    def key(c1, c2):
        cards = [parse_card(c) for c in (c1, c2, *board)]
        suits = [c[1] for c in (c1, c2, *board)]
        fd = max(suits.count(x) for x in set(suits)) == 4 and len(board) < 5
        return seven_rank(cards), fd
    hk = key(*hero)
    pool = []
    for cb, st in strat_by_combo.items():
        c1, c2 = cb[:2], cb[2:]
        if c1 in hero or c2 in hero:
            continue
        rk = key(c1, c2)
        if rk[1] != hk[1]:
            continue
        pool.append((rk[0], st))
    if not pool:
        return None
    pool.sort(key=lambda x: x[0])
    ranks = [r for r, _ in pool]
    import bisect
    i = bisect.bisect_left(ranks, hk[0])
    near = pool[max(0, i - k // 2): max(0, i - k // 2) + k]
    tot = [0.0] * len(actions)
    for _, st in near:
        ssum = sum(st) or 1
        for j, v in enumerate(st):
            tot[j] += v / ssum
    n = len(near)
    return {a: round(tot[j] / n, 4) for j, a in enumerate(actions)}


def spot_label(key, pot, stack, facing=0.0):
    """Seedha karne layak label, BB amount ke saath: 'Call 2bb', 'Bet 1.8bb (33% pot)', 'Raise to 10bb'."""
    fmt = lambda v: (f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}") + "bb"
    if key == "CALL":
        return "Call " + fmt(facing) if facing > 0 else "Call"
    m = re.match(r"(BET|RAISE) ([\d.]+)", key)
    if m:
        amt = float(m.group(2))
        if amt >= stack - 1:
            return f"All-in ({fmt(stack)})"
        if m.group(1) == "BET":
            return f"Bet {fmt(amt)} ({round(amt / pot * 100)}% pot)"
        return f"Raise to {fmt(amt)}"
    return action_label(key, pot, stack)["label"]


def spot_recommend(board, hero, street, facing, hero_pos, villain_pos, pot_type, bg=True, line=None):
    if not board or len(board) < 3 or len(board) > 5:
        return {"error": "invalid board"}
    if isinstance(hero, str):
        hero = [hero[0:2], hero[2:4]]
    spot, hero_ip, exact = resolve_spot(hero_pos, villain_pos, pot_type)
    sp = spotlib.spots()[spot]
    canon, m = spotlib.canonical(board[:3])
    cboard = canon + spotlib.map_cards(board[3:], m)
    chero = spotlib.map_cards(hero, m)
    # exact flop library me ho to wahi, warna bucket ka representative (same-feature mapping)
    rep = canon if spotlib.lib_path(spot, canon).exists() else spotlib.representative(canon)
    nodes = spotlib.load(spot, rep)
    if nodes is None:
        if bg:
            _enqueue_live(_solve_spot_flop, spot, tuple(rep))
            return {"queued": True, "spot": spot, "eta": 170}
        _solve_spot_flop(spot, rep)
        nodes = spotlib.load(spot, rep)
    approx = not exact
    source = "library" if rep == canon else "library~" + "".join(rep)
    similar = False
    if street == "flop":
        node, nkey = spotlib.pick_node(nodes, hero_ip, facing)
        nkey = ("c" if hero_ip else "r") if facing <= 0 else ("b" if hero_ip else "cb") + str(nkey)
        actions = node["a"] if node else []
        mix = spotlib.hero_strategy(node, chero) if rep == canon else None
        if mix is None and node:
            mix, lvl = spotlib.mapped_strategy(nkey, node, rep, chero, canon)
            similar = rep == canon and mix is not None
    else:
        # turn/river — asli LINE se reach weights (pehle flop+turn check-check maana jaata tha,
        # isliye bet/raise wali lines ka jawab galat hota tha). line = {"flop":[events], "turn":[events]}
        # events chronological hero+villain actions: {pos, act: check/call/fold/bet/raise, amt}.
        approx, source = True, "solved"
        line = line or {}
        flop_events = line.get("flop") or []
        turn_events = line.get("turn") or []
        b4 = tuple(cboard[:4])
        if not flop_events or all((e.get("act") == "check") for e in flop_events):
            # check-check flop — library nodes se turant (pehle wala fast path)
            ip_w, oop_w = _checkcheck_reach(spot, canon, nodes, rep)
        else:
            # flop pe bet/raise hua — poora tree chahiye (reach_weights walk)
            flop_tree = _spot_full_flop_tree(spot, canon)
            flop_actual = _walk_line(flop_events, flop_tree)
            ip_w, oop_w = reach_weights(flop_actual, flop_tree)
        tree = solve_street_tree(b4, sp["pot"], sp["eff"], ip_w, oop_w, TURN_ITER)
        if street == "river":
            turn_actual = _walk_line(turn_events, tree) if turn_events else ["CHECK", "CHECK"]
            ip_w, oop_w = reach_weights(turn_actual, tree, (ip_w, oop_w))
            tree = solve_street_tree(cboard, sp["pot"], sp["eff"], ip_w, oop_w, RIVER_ITER)
        node = _hero_node(tree, facing, "BTN" if hero_ip else "BB")
        if not node or node.get("node_type") != "action_node":
            return {"error": "no decision node", "spot": spot}
        actions = node.get("actions", [])
        strat = node_strategy(node)
        st = strat.get(combo_key(*chero))
        mix = {a: round(float(st[i]), 4) for i, a in enumerate(actions)} if st is not None else None
        if mix is None:
            mix = _similar_mix(strat, actions, chero, cboard)
            similar = mix is not None
    if not mix:
        return {"error": "hand not in range", "spot": spot, "actions": actions}
    labels = {a: spot_label(a, sp["pot"], sp["eff"], facing) for a in mix}
    best = max(mix, key=mix.get)
    return {"board": board, "street": street, "spot": spot, "hero_ip": hero_ip,
            "strategy": mix, "best": best, "bestLabel": labels[best], "labels": labels,
            "approx": approx or similar, "similar": similar, "source": source,
            "pot": sp["pot"], "eff": sp["eff"]}


# --------------------------------------------------------------------------
# DeepSeek
# --------------------------------------------------------------------------
def deepseek_chat(system, user, max_tokens=800):
    import urllib.request
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


# --------------------------------------------------------------------------
# Game engine
# --------------------------------------------------------------------------
HANDS = {}
HANDS_LOCK = threading.Lock()
_PLURIBUS_TEXT = None    # /api/pluribus ka 7MB text ek baar padho, cache rakho


class Hand:
    def __init__(self, spot):
        self.spot = spot
        self.id = hashlib.md5(f"{spot['id']}-{random.random()}-{os.getpid()}".encode()).hexdigest()[:12]
        self.board = list(spot["board"])
        self.street = 0
        self.pot = float(spot["pot"])
        self.committed = {"hero": 0.0, "villain": 0.0}
        self.stack = EFF_STACK      # effective stack left at the start of the current street
        self._weights = None        # reach weights the current street's tree was solved with
        self.history = []
        self.hand_over = False
        self.result = None
        self.last_decision = None
        self.hero = None
        self.villain = None
        self.tree = None
        self.node = None
        self.to_act = "villain"
        self._flop_tree = get_flop_tree(spot["id"])
        self._deal()
        self.tree = self._flop_tree
        self.node = self.tree
        self.to_act = "villain"
        self._settle()

    # ---- dealing -------------------------------------------------------
    def _deal(self):
        btn_node = self._flop_tree["childrens"]["CHECK"]
        btn_combos = range_combos_from_node(btn_node)
        def free(c, dead):
            return not card_in(c[0:2], dead) and not card_in(c[2:4], dead)
        hero_pick = random.choice([c for c in btn_combos if free(c, self.board)])
        self.hero = [hero_pick[0:2], hero_pick[2:4]]
        bb_combos = range_combos_from_node(self._flop_tree)
        vill_pick = random.choice([c for c in bb_combos if free(c, self.board + self.hero)])
        self.villain = [vill_pick[0:2], vill_pick[2:4]]

    # ---- helpers -------------------------------------------------------
    def _combo(self, actor):
        cards = self.hero if actor == "hero" else self.villain
        return combo_key(cards[0], cards[1])

    def _node_strategy(self, actor):
        if self.node is None or self.node.get("node_type") != "action_node":
            return None
        return node_strategy(self.node).get(self._combo(actor))

    def _sample_villain(self):
        st = node_strategy(self.node)
        combo = self._combo("villain")
        probs = st.get(combo)
        actions = self.node["actions"]
        if not probs:
            return "CHECK" if "CHECK" in actions else (actions[0] if actions else "CHECK")
        return random.choices(actions, weights=[max(p, 0.0) for p in probs])[0]

    # ---- street transitions -------------------------------------------
    def _deal_next_card(self):
        used = {card_key(c) for c in self.board + self.hero + self.villain}
        deck = [c for c in fresh_deck() if c not in used]
        self.board.append(random.choice(deck))

    def _solve_street(self):
        board_str = ",".join(self.board)
        if self.street == 1:
            line = [h["action"] for h in self.history if h["street"] == "flop"]
            ip_w, oop_w = reach_weights(line, self._flop_tree)
            max_iter = TURN_ITER
        else:
            line = [h["action"] for h in self.history if h["street"] == "turn"]
            ip_w, oop_w = reach_weights(line, self._turn_tree, self._weights)
            max_iter = RIVER_ITER
        ip_r = weighted_range_string_from(ip_w, self.board)
        oop_r = weighted_range_string_from(oop_w, self.board)
        self._weights = (ip_w, oop_w)
        cache_key = hashlib.md5(
            (board_str + "|" + str(round(self.pot, 1)) + "|s" + str(round(self.stack, 1))
             + "|" + ip_r + "|" + oop_r + "|i" + str(max_iter)).encode()
        ).hexdigest()[:16]
        cache_path = CACHE_DIR / f"{cache_key}.json"
        if cache_path.exists():
            self.tree = json.loads(cache_path.read_text(encoding="utf-8"))
        else:
            self.tree = solve(board_str, self.pot, self.stack, ip_r, oop_r, cache_key, max_iter)
            cache_path.write_text(json.dumps(self.tree), encoding="utf-8")
        if self.street == 1:
            self._turn_tree = self.tree
        self.node = self.tree
        self.to_act = "villain" if self.node.get("player") == 1 else "hero"

    def _end_street(self):
        if self.street == 2:
            self._showdown()
            return
        self._deal_next_card()
        self.street += 1
        self.stack -= self.committed["hero"]   # betting closed, so both put in the same amount
        self.committed = {"hero": 0.0, "villain": 0.0}
        self._solve_street()

    def _runout(self):
        used = {card_key(c) for c in self.board + self.hero + self.villain}
        deck = [c for c in fresh_deck() if c not in used]
        while len(self.board) < 5:
            card = random.choice(deck)
            deck.remove(card)
            self.board.append(card)
        self.street = 2
        self._showdown()

    def _showdown(self):
        board_p = [parse_card(c) for c in self.board]
        h = seven_rank(board_p + [parse_card(c) for c in self.hero])
        v = seven_rank(board_p + [parse_card(c) for c in self.villain])
        winner = "hero" if h > v else ("villain" if v > h else "tie")
        self.hand_over = True
        self.result = {
            "winner": winner, "reason": "showdown", "pot": round(self.pot, 1),
            "heroHand": HAND_NAMES[h[0]], "villainHand": HAND_NAMES[v[0]],
            "villain": self.villain,
        }

    # ---- applying actions ---------------------------------------------
    def _apply(self, actor, key):
        self.history.append({
            "street": ["flop", "turn", "river"][self.street],
            "actor": "BB" if actor == "villain" else "BTN",
            "action": key,
            "label": action_label(key, self.pot, self.stack)["label"],
        })
        if key == "FOLD":
            self.hand_over = True
            self.result = {"winner": "hero" if actor == "villain" else "villain",
                           "reason": "fold", "pot": round(self.pot, 1),
                           "villain": self.villain, "heroHand": "", "villainHand": ""}
            self.node = None
            return
        if key == "CHECK":
            self._advance(key)
            if self.node is None or self.node.get("node_type") == "chance_node":
                self._end_street()
            else:
                self.to_act = "villain" if actor == "hero" else "hero"
            return
        if key == "CALL":
            other = "villain" if actor == "hero" else "hero"
            amt = self.committed[other] - self.committed[actor]
            self.committed[actor] += amt
            self.pot += amt
            self._advance(key)
            self._close_betting()
            return
        amt = action_amount(key)
        if amt is not None:
            diff = amt - self.committed[actor]
            self.committed[actor] = amt
            self.pot += diff
            self._advance(key)
            other = "villain" if actor == "hero" else "hero"
            if self.committed[other] >= self.stack - 0.01:
                self._close_betting()
            else:
                self.to_act = other
            return
        self.to_act = "villain" if actor == "hero" else "hero"

    def _advance(self, key):
        if self.node and self.node.get("childrens"):
            self.node = self.node["childrens"].get(key)
        else:
            self.node = None

    def _close_betting(self):
        someone_allin = max(self.committed.values()) >= self.stack - 0.01
        if someone_allin:
            self._runout()
        elif self.street == 2:
            self._showdown()
        else:
            self._end_street()

    def _settle(self):
        guard = 0
        while not self.hand_over and guard < 20:
            guard += 1
            if self.to_act != "villain":
                return
            if self.node is None or self.node.get("node_type") != "action_node":
                return
            if self.node.get("player") != 1:
                self.to_act = "hero"
                return
            self._apply("villain", self._sample_villain())

    # ---- public --------------------------------------------------------
    def hero_actions(self):
        if self.node is None or self.node.get("node_type") != "action_node":
            return []
        return [{"key": a, **action_label(a, self.pot, self.stack)} for a in self.node["actions"]]

    def hero_strategy(self):
        st = self._node_strategy("hero")
        if st is None:
            return None
        return {a: round(float(p), 4) for a, p in zip(self.node["actions"], st)}

    def hero_act(self, key):
        if self.hand_over or self.to_act != "hero":
            return None
        strat = self.hero_strategy()
        if strat is None or key not in strat:
            return None
        correct_key = max(strat, key=strat.get)
        chosen_f = strat.get(key, 0.0)
        self.last_decision = {
            "key": key,
            "label": action_label(key, self.pot, self.stack)["label"],
            "correct": key == correct_key,
            "acceptable": chosen_f >= 0.20,
            "mix": strat,
            "correctKey": correct_key,
            "correctLabel": action_label(correct_key, self.pot, self.stack)["label"],
            "labels": {a: action_label(a, self.pot, self.stack)["label"] for a in strat},
            "street": ["flop", "turn", "river"][self.street],
        }
        self._apply("hero", key)
        self._settle()
        return self.state()

    def state(self):
        return {
            "handId": self.id,
            "spotId": self.spot["id"],
            "texture": self.spot["texture"],
            "board": self.board,
            "hero": self.hero,
            "street": ["flop", "turn", "river"][self.street],
            "pot": round(self.pot, 1),
            "stack": round(self.stack - max(self.committed.values()), 1),
            "toAct": self.to_act,
            "handOver": self.hand_over,
            "result": self.result,
            "history": self.history,
            "actions": self.hero_actions() if (self.to_act == "hero" and not self.hand_over) else [],
        }


# --------------------------------------------------------------------------
# Flop tree loading
# --------------------------------------------------------------------------
_flop_trees = {}


def get_flop_tree(spot_id):
    if spot_id not in _flop_trees:
        path = WORK_DIR / f"{BOARDS[spot_id][0]}_output.json"
        _flop_trees[spot_id] = json.loads(path.read_text(encoding="utf-8"))
    return _flop_trees[spot_id]


# --------------------------------------------------------------------------
# Coach
# --------------------------------------------------------------------------
def coach(hand, focus="", lang="hinglish"):
    if not DEEPSEEK_KEY:
        return ("AI coach offline. Set DEEPSEEK_API_KEY then restart the server, e.g.\n"
                "$env:DEEPSEEK_API_KEY=\"sk-...\"")
    if hand is None:
        return "Unknown hand."
    if lang == "hinglish":
        system = ("You are a world-class poker GTO coach and teacher. Explain in Hinglish — Roman "
                  "Hindi mixed with English, like a friendly Indian poker coach (use 'bhai', 'aap', "
                  "'matlab', 'kyunki', 'isliye' naturally). Keep poker terms in English (bet, check, "
                  "call, range, top pair, bluff, pot, sizing). Teach with DEEP REASONING: not just "
                  "WHAT the solver does but WHY — explain the underlying concepts. Treat the solver "
                  "frequencies as the source of truth for GTO numbers (never invent different ones). "
                  "Structure your answer as a clear lesson with markdown headings. Be specific and encouraging.")
        tail = "\n\nRespond in Hinglish (Roman Hindi + English mix)."
    else:
        system = ("You are a world-class poker GTO coach and teacher. Explain in clear English with "
                  "DEEP REASONING: not just WHAT the solver does but WHY — explain the underlying "
                  "concepts. Treat the solver frequencies as the source of truth for GTO numbers "
                  "(never invent different ones). Reference bb and %-pot. Structure your answer as "
                  "a clear lesson with markdown headings. Be specific and encouraging.")
        tail = "\n\nRespond in English."
    user = hand_summary(hand) + tail
    if focus:
        user += "\n\nFocus question: " + focus
    return deepseek_chat(system, user)


def hand_summary(hand):
    if hand.hand_over and hand.result:
        vill = f"Villain (BB) had: {hand.villain[0]} {hand.villain[1]}."
    else:
        vill = "Villain (BB) hole cards are unknown to hero (still in BB's calling range)."
    lines = [
        f"Spot: BTN opens, BB calls. Board texture: {hand.spot['texture']}. "
        f"Pot {hand.spot['pot']}bb, eff {hand.spot['eff']}bb.",
        f"Current pot {round(hand.pot, 1)}bb, {round(hand.stack, 1)}bb behind at the start of this street.",
        f"Hero (BTN): {hand.hero[0]} {hand.hero[1]}.  {vill}",
        "Board: " + " ".join(hand.board),
        "Action log:",
    ]
    for h in hand.history:
        lines.append(f"  {h['street'].title()}: {h['actor']} -> {h['label']}")
    if hand.last_decision:
        d = hand.last_decision
        lines.append(f"Hero chose '{d['label']}' on the {d['street']}.")
        mix = ", ".join(f"{k} {int(v*100)}%" for k, v in sorted(d["mix"].items(), key=lambda x: -x[1]))
        lines.append(f"GTO strategy for hero's exact hand: {mix}.")
        lines.append(f"Best GTO action: '{d['correctLabel']}'. "
                     f"Verdict: {'CORRECT' if d['correct'] else ('acceptable mix' if d['acceptable'] else 'MISTAKE')}.")
    if hand.hand_over and hand.result:
        r = hand.result
        lines.append(f"Hand over: {r['reason']}, winner={r['winner']}, pot={r['pot']}bb.")
        if r.get("villainHand"):
            lines.append(f"Showdown: hero {r['heroHand']} vs villain {r['villainHand']}.")
    lines.append("\nTeach this spot in depth with reasoning, covering ALL of:")
    lines.append("1) Board & range analysis — who has range advantage and nut advantage here, and WHY.")
    lines.append("2) Hero's exact hand class (value / protection / bluff / showdown value / pot-control) and its strategic role.")
    lines.append("3) The solver's GTO frequencies for hero's hand — WHY each action gets that frequency, including bet-sizing logic.")
    lines.append("4) Verdict on hero's actual choice with deeper reasoning (equity realization, fold equity, protection, etc.).")
    lines.append("5) The key poker concepts at play — explain EACH with reasoning (range advantage, nut advantage, board texture, sizing, equity denial, polarization).")
    lines.append("6) 2-3 concrete takeaways.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Lightweight access log for debugging connectivity.
        try:
            with open(WORK_DIR / "gto_access.log", "a", encoding="utf-8") as f:
                f.write(f"{self.command} {self.path}\n")
        except Exception:
            pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_OPTIONS(self):
        self._send({})

    def do_GET(self):
        if self.path == "/api/health":
            self._send({"ok": True, "ai": bool(DEEPSEEK_KEY), "spots": len(SPOTS)})
        elif self.path == "/api/spots":
            self._send(SPOTS)
        elif self.path == "/api/player_notes":
            import hh_review
            self._send(hh_review.notes_get())
        elif self.path == "/api/pluribus":
            # Pluribus ke 10k hands (pluribus_convert.py se bane) — Hand Review ka reference (cached)
            global _PLURIBUS_TEXT
            if _PLURIBUS_TEXT is None:
                f = WORK_DIR / "pluribus_gg.txt"
                _PLURIBUS_TEXT = f.read_text(encoding="utf-8") if f.exists() else ""
            self._send({"text": _PLURIBUS_TEXT} if _PLURIBUS_TEXT else {"error": "pluribus_gg.txt nahi mila — tools/postflop/pluribus_convert.py chalao"})
        elif self.path == "/api/hh_files":
            import hh_review
            self._send(hh_review.load_hh_files())
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        try:
            if self.path == "/api/new":
                b = self._body()
                sid = b.get("spotId")
                if not isinstance(sid, int) or not 0 <= sid < len(SPOTS):
                    sid = random.randrange(len(SPOTS))
                h = Hand(SPOTS[sid])
                with HANDS_LOCK:
                    HANDS[h.id] = h
                    while len(HANDS) > 200:          # dicts keep insertion order: drop the oldest
                        HANDS.pop(next(iter(HANDS)))
                self._send(h.state())
            elif self.path == "/api/act":
                b = self._body()
                with HANDS_LOCK:
                    h = HANDS.get(b.get("handId"))
                if not h:
                    self._send({"error": "unknown hand"}, 404)
                    return
                st = h.hero_act(b.get("key"))
                if st is None:
                    self._send({"error": "invalid action"}, 400)
                    return
                self._send({"state": st, "lastDecision": h.last_decision})
            elif self.path == "/api/coach":
                b = self._body()
                with HANDS_LOCK:
                    h = HANDS.get(b.get("handId"))
                self._send({"text": coach(h, b.get("focus"), b.get("lang", "hinglish"))})
            elif self.path == "/api/review":
                # Session ke baad: poora (khatam hua) hand history text + preflop ranges
                import hh_review
                b = self._body()
                try:
                    self._send(hh_review.review(b.get("hh", ""), b.get("ranges") or {}))
                except ValueError as e:
                    self._send({"error": str(e)}, 400)
            elif self.path == "/api/player_exploits":
                import hh_review
                self._send(hh_review.exploits_update(self._body().get("players") or {}))
            elif self.path == "/api/player_note_mine":
                import hh_review
                b = self._body()
                self._send(hh_review.notes_update(b.get("name", ""), mine=b.get("text", "")))
            elif self.path == "/api/player_note":
                import hh_review
                self._send(hh_review.player_note(self._body()))
            elif self.path == "/api/spot":
                if solver_off():
                    self._send({"error": "solver off (memory saver)", "off": True})
                    return
                b = self._body()
                self._send(spot_recommend(b.get("board"), b.get("hero"), b.get("street", "flop"),
                                          float(b.get("facing", 0.0)), b.get("heroPos"), b.get("villainPos"),
                                          b.get("potType", "srp"), bool(b.get("bg", True)), b.get("line")))
            elif self.path == "/api/villain_range":
                b = self._body()
                self._send(villain_range(b.get("heroPos"), b.get("villainPos"), b.get("potType", "srp"),
                                         b.get("board"), b.get("action")))
            elif self.path == "/api/live":
                if solver_off():
                    self._send({"error": "solver off (memory saver)", "off": True})
                    return
                b = self._body()
                board = b.get("board")
                hero = b.get("hero")
                street = b.get("street", "flop")
                pot = float(b.get("pot", POT))
                stack = float(b.get("stack", EFF_STACK))
                facing = float(b.get("facing", 0.0))
                hero_pos = b.get("heroPos", "BTN")
                if b.get("bg") and street == "flop" and board and len(board) >= 3:
                    flop = board[:3]
                    if _pre_solved_flop(flop) is None and not _live_cache_path(_flop_key(flop, pot, stack)).exists():
                        enqueue_flop(flop, pot, stack)
                        self._send({"queued": True, "board": board, "street": street})
                        return
                self._send(live_recommend(board, hero, street, pot, stack, facing, hero_pos))
            else:
                self._send({"error": "not found"}, 404)
        except Exception as e:  # noqa
            self._send({"error": f"{type(e).__name__}: {e}"}, 500)


def start_background():
    """HTTP server ko daemon thread me chalao (desktop exe ke liye). Solver na mile to None."""
    if not SOLVER_EXE.exists():
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_bg_worker, daemon=True).start()
    threading.Thread(target=_live_worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    if not SOLVER_EXE.exists():
        raise SystemExit(f"console_solver.exe not found at {SOLVER_EXE}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_bg_worker, daemon=True).start()
    threading.Thread(target=_live_worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"GTO server on http://127.0.0.1:{PORT}  (AI coach: {'ON' if DEEPSEEK_KEY else 'OFF'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
