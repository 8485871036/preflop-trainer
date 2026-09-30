"""
Postflop spot library — har preflop spot (postflop_spots.json) x har canonical flop ka
pehle se solve kiya hua flop strategy. Live me sirf lookup (in-memory cache ke baad ~microseconds).

Canonical flop: 24 suit-permutations me se lexicographically sabse chhota form — isomorphic flops
(As Kh 7d == Ah Ks 7c ...) ek hi file share karte hain; hero ke cards pe wahi suit mapping lagti hai.

Har file me sirf decision nodes (combo -> action freq, 0..1000):
  "r"        OOP pehla action (root)
  "c"        IP, OOP ne check kiya
  "b<amt>"   IP, OOP ne <amt> bet kiya
  "cb<amt>"  OOP, check ke baad IP ne <amt> bet kiya
"""
import gzip
import itertools
import json
import os
import re
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
# exe (PyInstaller) me __file__ temp folder me hota hai — repo root (PREFLOP_ROOT) ka folder pehle
_root_pf = Path(os.environ["PREFLOP_ROOT"]) / "tools" / "postflop" if os.environ.get("PREFLOP_ROOT") else None
if _root_pf and (_root_pf / "postflop_spots.json").exists():
    HERE = _root_pf
LIB_DIR = Path(os.environ.get("POSTFLOP_LIB", HERE / "work" / "lib"))
RANKS = "23456789TJQKA"
SUITS = "shdc"
_SPOTS = None
_cache = {}
_cache_lock = threading.Lock()


def spots():
    global _SPOTS
    if _SPOTS is None:
        _SPOTS = json.loads((HERE / "postflop_spots.json").read_text(encoding="utf-8"))
    return _SPOTS


def _key(card):
    return (RANKS.index(card[0]), SUITS.index(card[1]))


def canonical(board):
    """board (3 cards) -> (canonical board list, suit map dict orig->canon)."""
    best = None
    for perm in itertools.permutations(SUITS):
        m = dict(zip(SUITS, perm))
        cards = sorted((c[0] + m[c[1]] for c in board), key=lambda c: (-RANKS.index(c[0]), SUITS.index(c[1])))
        k = tuple(cards)
        if best is None or k < best[0]:
            best = (k, m)
    return list(best[0]), best[1]


def all_canonical_flops():
    seen = set()
    out = []
    deck = [r + s for r in RANKS for s in SUITS]
    for f in itertools.combinations(deck, 3):
        c = tuple(canonical(list(f))[0])
        if c not in seen:
            seen.add(c)
            out.append(list(c))
    return out


def map_cards(cards, m):
    return [c[0] + m[c[1]] for c in cards]


def lib_path(spot, canon):
    return LIB_DIR / spot / ("".join(canon) + ".json.gz")


def extract_nodes(tree):
    """Solver ka flop tree -> sirf decision nodes (quantized)."""
    def strat(node):
        st = node.get("strategy", {})
        acts = st.get("actions") or node.get("actions", [])
        probs = st.get("strategy", {})
        return {"a": acts, "s": {cb: [int(round(p * 1000)) for p in v] for cb, v in probs.items()}}

    out = {}
    if not tree or tree.get("node_type") != "action_node":
        return out
    out["r"] = strat(tree)
    ch = tree.get("childrens", {})
    for a, node in ch.items():
        if node.get("node_type") != "action_node":
            continue
        if a == "CHECK":
            out["c"] = strat(node)
            for a2, n2 in node.get("childrens", {}).items():
                m = re.match(r"BET ([\d.]+)", a2)
                if m and n2.get("node_type") == "action_node":
                    out["cb" + str(round(float(m.group(1)), 2))] = strat(n2)
        else:
            m = re.match(r"BET ([\d.]+)", a)
            if m:
                out["b" + str(round(float(m.group(1)), 2))] = strat(node)
    return out


def save(spot, canon, nodes):
    p = lib_path(spot, canon)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(nodes, f, separators=(",", ":"))
    os.replace(tmp, p)
    with _cache_lock:
        _cache[(spot, tuple(canon))] = nodes


def load(spot, canon):
    k = (spot, tuple(canon))
    with _cache_lock:
        if k in _cache:
            return _cache[k]
    p = lib_path(spot, canon)
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        nodes = json.load(f)
    with _cache_lock:
        if len(_cache) > 3000:
            _cache.clear()
        _cache[k] = nodes
    return nodes


def pick_node(nodes, hero_ip, facing):
    """Hero ke current decision ka node. facing = bet amount (BB) jo hero ke saamne hai."""
    if facing <= 0:
        return nodes.get("c" if hero_ip else "r"), None
    prefix = "b" if hero_ip else "cb"
    cands = [(abs(float(k[len(prefix):]) - facing), k) for k in nodes
             if k.startswith(prefix) and re.fullmatch(r"[\d.]+", k[len(prefix):])]
    if not cands:
        return None, None
    d, k = min(cands)
    return nodes[k], float(k[len(prefix):])


def combo_key(c1, c2):
    a, b = sorted([c1, c2], key=lambda c: (RANKS.index(c[0]), SUITS.index(c[1])), reverse=True)
    return a + b


def hero_strategy(node, hero_cards):
    """Node + hero ke (mapped) cards -> {action: freq}."""
    if not node:
        return None
    for key in (combo_key(*hero_cards), hero_cards[0] + hero_cards[1], hero_cards[1] + hero_cards[0]):
        v = node["s"].get(key)
        if v is not None:
            tot = sum(v) or 1
            return {a: round(x / tot, 4) for a, x in zip(node["a"], v)}
    return None


# ---------------------------------------------------------------------------
# Subset library: 1755 flops -> ~236 texture buckets; har bucket ka ek representative flop
# poore tree se solve hota hai. Koi bhi flop apne bucket ke representative pe map, aur hero ka
# hand representative board pe SAME FEATURES (pair kaunsa, kicker, draws) wale hands pe.
# ---------------------------------------------------------------------------
_LEVEL = dict(zip(RANKS, "2 2 4 4 4 7 7 7 T T Q K A".split()))
_subset = None


def bucket_key(flop):
    rs = sorted((RANKS.index(c[0]) for c in flop), reverse=True)
    pair = "T" if rs[0] == rs[2] else ("P" if len(set(rs)) == 2 else "U")
    n = len({c[1] for c in flop})
    sp = {3: "r", 2: "t", 1: "m"}[n] if pair == "U" else "x"
    return "|".join(_LEVEL[RANKS[x]] for x in rs) + "|" + pair + "|" + sp


def subset():
    """{bucket_key: representative canonical flop} — deterministic."""
    global _subset
    if _subset is not None:
        return _subset
    groups = {}
    for f in all_canonical_flops():
        groups.setdefault(bucket_key(f), []).append(f)
    out = {}
    for k, fl in groups.items():
        # bucket ke average rank ke sabse paas; two-tone me high+mid suited, paired me rainbow pasand
        avg = [sum(sorted((RANKS.index(c[0]) for c in f), reverse=True)[i] for f in fl) / len(fl) for i in range(3)]

        def score(f):
            rs = sorted((RANKS.index(c[0]) for c in f), reverse=True)
            d = sum(abs(a - b) for a, b in zip(rs, avg))
            cs = sorted(f, key=lambda c: -RANKS.index(c[0]))
            pref = 0
            if k.endswith("|t"):
                pref = 0 if cs[0][1] == cs[1][1] else 0.5
            if k.endswith("|x"):
                pref = 0 if len({c[1] for c in f}) == 3 else 0.5
            return (d + pref, "".join(f))
        out[k] = min(fl, key=score)
    _subset = out
    return out


def representative(canon):
    return subset()[bucket_key(canon)]


def _straight_outs(ranks):
    """Kitne alag ranks (turn/river) straight complete karte hain — 2 = OESD, 1 = gutshot."""
    have = set(ranks)
    if 12 in have:
        have.add(-1)
    outs = 0
    for r in range(-1, 13):
        if r in have:
            continue
        hs = have | {r}
        if any(all(x in hs for x in range(lo, lo + 5)) for lo in range(-1, 9)):
            outs += 1
    return outs


def hand_features(hole, board):
    """Board ke relative hand ki pehchaan: (category, sub, kicker, flush_draw, straight_draw, overcards)."""
    from gto_server import seven_rank, parse_card   # lazy (circular import se bachne ke liye)
    hr = sorted((RANKS.index(c[0]) for c in hole), reverse=True)
    br = sorted((RANKS.index(c[0]) for c in board), reverse=True)
    cat = seven_rank([parse_card(c) for c in list(hole) + list(board)])[0]
    ub = sorted(set(br), reverse=True)
    sub = kick = 0
    if cat == 1:                                          # one pair — kaunsa?
        if hr[0] == hr[1]:
            sub = "over" if hr[0] > br[0] else ("under" if hr[0] < br[-1] else "mid_pp")
        else:
            hit = [r for r in hr if r in br]
            if hit:
                pos = ub.index(hit[0])
                sub = ("top", "second", "third")[min(pos, 2)]
                k = [r for r in hr if r != hit[0]][0]
                kick = 2 if k >= 10 else (1 if k >= 7 else 0)
            else:
                sub = "board"                              # pair board pe, hero ka kuch nahi
    elif cat == 2:
        sub = "both" if all(r in br for r in hr) and hr[0] != hr[1] else "one"
    suits = [c[1] for c in list(hole) + list(board)]
    fd = 0
    if len(board) < 5:
        for s in set(c[1] for c in hole):
            if suits.count(s) == 4:
                fd = 1
    sd = min(2, _straight_outs(set(hr + br))) if len(board) < 5 and cat < 4 else 0
    overs = sum(1 for r in hr if r > br[0]) if cat == 0 else 0
    return (cat, sub, kick, fd, sd, overs)


_feat_groups = {}


def _groups(node_key, node, rep_board):
    k = (node_key, "".join(rep_board))
    g = _feat_groups.get(k)
    if g is None:
        g = {}
        for cb, st in node["s"].items():
            f = hand_features([cb[:2], cb[2:]], rep_board)
            for lvl in range(6):
                g.setdefault((lvl,) + f[:6 - lvl], []).append(st)
        if len(_feat_groups) > 500:
            _feat_groups.clear()
        _feat_groups[k] = g
    return g


def mapped_strategy(node_key, node, rep_board, hero, real_board):
    """Hero (real board) -> rep board pe same-feature hands ki average strategy. (mix, match_level)"""
    if not node:
        return None, None
    f = hand_features(hero, real_board)
    g = _groups(node_key, node, rep_board)
    for lvl in range(6):
        lst = g.get((lvl,) + f[:6 - lvl])
        if lst:
            tot = [0.0] * len(node["a"])
            for st in lst:
                s = sum(st) or 1
                for j, v in enumerate(st):
                    tot[j] += v / s
            return {a: round(t / len(lst), 4) for a, t in zip(node["a"], tot)}, lvl
    return None, None
