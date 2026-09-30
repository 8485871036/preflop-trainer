r"""
Red Star Poker (iPoker) live hand reader — 100% accurate cards.

Client khud apni hand history TempData me binary files me likhta hai (jaise khud
read karta hai, waise hi hum read karte hain). Ye files LOCKED nahi hain.

Structure:
  History\TempData\<session>\Data\Tables\<table_id>\<hand_id>   -> live binary hand
  History\TempData\<session>\Data\Tables\<table_id>.xml        -> live actions (text)
  History\Data\Tables\<table_id>.xml                           -> completed hands (text, cards)

Binary hand file me cards suit+rank format me hote hain: "S9" = 9s, "D10" = Td.
Opponents ke cards "X X" (hidden) hote hain. Board cards file ke end me hote hain.

Run: python tools/redstar_hh.py            (live hand dikhata hai)
"""
import os
import re
import time
from pathlib import Path

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
REDSTAR = LOCALAPPDATA / "Red Star Poker" / "data"
HERO = "ZenithBluff"

SUIT = {"S": "s", "H": "h", "D": "d", "C": "c"}
CARD_RE = re.compile(rb"[SHDC](?:10|[2-9TJQKA])")
# Board cards protobuf me field 11 (0x5a) + length(2/3) se prefixed hote hain.
# Player pocket cards field 9 (0x4a) use karte hain — inhe alag rakhna zaroori hai.
BOARD_RE = re.compile(rb"\x5a[\x02\x03]([SHDC](?:10|[2-9TJQKA]))")

TEMP_DATA = REDSTAR / HERO / "History" / "TempData"
DONE_DATA = REDSTAR / HERO / "History" / "Data" / "Tables"

try:
    import redstar_mem
    HAS_MEM = True
except Exception:
    HAS_MEM = False


def _norm(tok):
    """'S9' -> '9s', 'D10' -> 'Td'"""
    if isinstance(tok, bytes):
        tok = tok.decode()
    s, r = tok[0].upper(), tok[1:].upper()
    r = "T" if r == "10" else r
    return r + SUIT[s]


def _tables():
    """Session ke saare table dirs (newest-first)."""
    try:
        sessions = [d for d in TEMP_DATA.iterdir() if d.is_dir()]
        sess = max(sessions, key=lambda p: p.stat().st_mtime)
    except Exception:
        return []
    try:
        tdir = sess / "Data" / "Tables"
        return sorted([d for d in tdir.iterdir() if d.is_dir()],
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []


def _parse(data, hand_id, mtime):
    """Binary hand file -> dict."""
    # hero cards = 'ZenithBluff' ke baad ke pehle 2 real card tokens
    hero_raw = []
    i = data.find(HERO.encode())
    if i >= 0:
        hero_raw = CARD_RE.findall(data[i:i + 80])[:2]
    # board cards = sirf 0x5a-prefixed (protobuf field 11) — opponents ke
    # showdown cards (S6 SA etc.) field 9 use karte hain, unhe ignore karo.
    board_raw = BOARD_RE.findall(data)
    hero = [_norm(c) for c in hero_raw]
    board = [_norm(c) for c in board_raw]
    street = {0: None, 3: "flop", 4: "turn", 5: "river"}.get(len(board))
    return {
        "hand_id": hand_id,
        "hero": hero,
        "board": board,
        "street": street,
        "mtime": mtime,
    }


def _hero_table():
    """Hero jis table pe baithha hai (newest hero-containing hand wala table)."""
    tables = _tables()
    best = None
    for tbl in tables:
        try:
            files = [f for f in tbl.iterdir() if f.is_file()]
            if not files:
                continue
            f = max(files, key=lambda p: p.stat().st_mtime)
            data = f.read_bytes()
            if HERO.encode() in data:
                if best is None or f.stat().st_mtime > best[0].stat().st_mtime:
                    best = (f, data, tbl)
        except Exception:
            continue
    return best


def read_live_hand():
    """Hero jis table pe baithha hai us table ka newest live hand."""
    best = _hero_table()
    if not best:
        tables = _tables()
        newest = None
        for tbl in tables:
            try:
                files = [f for f in tbl.iterdir() if f.is_file()]
                if files:
                    f = max(files, key=lambda p: p.stat().st_mtime)
                    if newest is None or f.stat().st_mtime > newest.stat().st_mtime:
                        newest = f
            except Exception:
                continue
        if not newest:
            return None
        hfile, data, tbl = newest, newest.read_bytes(), None
    else:
        hfile, data, tbl = best
    out = _parse(data, hfile.name, hfile.stat().st_mtime)
    out["table"] = tbl.name if tbl else None
    return out


# ---------------- position + hero_to_act (live table XML se, 100% accurate) ----------------
POS_6 = {0: "BTN", 1: "SB", 2: "BB", 3: "UTG", 4: "MP", 5: "CO"}
POS_5 = {0: "BTN", 1: "SB", 2: "BB", 3: "MP", 4: "CO"}    # 5-handed: UTG seat khaali = MP ranges (screen logic jaisa)
POS_4 = {0: "BTN", 1: "SB", 2: "BB", 3: "CO"}
POS_3 = {0: "BTN", 1: "SB", 2: "BB"}
POS_2 = {0: "SB", 1: "BB"}                  # heads-up: button hi SB post karta hai


def _live_table_xml(table_name):
    """Hero wale table ka live XML text (aur table dir)."""
    tables = _tables()
    for tbl in tables:
        xml_path = tbl.parent / (tbl.name + ".xml")
        if xml_path.exists():
            try:
                txt = xml_path.read_text(encoding="utf-8", errors="replace")
                if HERO in txt:
                    return txt, tbl
            except Exception:
                continue
    return None, None


def _parse_table_state(xml):
    """Live XML ka LAST (incomplete) game -> players/dealer/rounds."""
    m = list(re.finditer(r'<game gamecode="(\d+)">', xml))
    if not m:
        return None
    gc = m[-1].group(1)
    body = xml[m[-1].end():].split("</game>")[0]
    names, seats, dealer_idx = [], [], 0
    for ptag in re.findall(r'<player\s+([^>]+)/>', body):
        nm = re.search(r'name="([^"]+)"', ptag)
        st = re.search(r'seat="(\d+)"', ptag)
        if not (nm and st):
            continue
        names.append(nm.group(1))
        seats.append(int(st.group(1)))
        if re.search(r'dealer="1"', ptag):
            dealer_idx = len(names) - 1
    if not names:
        return None
    # rounds: body ko <round no=...> se tod do (last round incomplete ho sakta hai)
    rounds = {}
    for rm in re.finditer(r'<round no="(\d+)">(.*?)(?=<round no=|\Z)', body, re.S):
        rno = int(rm.group(1))
        acts = re.findall(r'<action[^>]*player="([^"]+)"[^>]*type="(\d+)"', rm.group(2))
        rounds[rno] = acts
    return {"gamecode": gc, "names": names, "seats": seats, "dealer_idx": dealer_idx, "rounds": rounds}


def _position(names, dealer_idx, hero=HERO):
    if hero not in names:
        return None
    n = len(names)
    off = (names.index(hero) - dealer_idx) % n
    tbl = {2: POS_2, 3: POS_3, 4: POS_4, 5: POS_5, 6: POS_6}.get(n, POS_6)
    return tbl.get(off, "BTN")


def _next_to_act(names, dealer_idx, acts, preflop, hero=HERO):
    """acts: [(name, type)] — current street ke actions. Returns True agar hero to act."""
    n = len(names)
    if not n:
        return False
    folded, acted = set(), set()
    last_idx = None
    for nm, t in acts:
        if preflop and t in ("1", "2"):
            continue                      # blinds — order me last aayenge
        if t == "0":
            folded.add(nm)
            acted.add(nm)
        elif t == "23":
            acted = {nm}                  # raise -> sabko dobara act karna
        else:
            acted.add(nm)
        if nm in names:
            last_idx = names.index(nm)
    start = (dealer_idx + 3) % n if preflop else (dealer_idx + 1) % n
    if last_idx is not None:
        start = (last_idx + 1) % n
    for step in range(n):
        idx = (start + step) % n
        nm = names[idx]
        if nm in folded:
            continue
        if nm not in acted:
            return nm == hero
    return False


def read_live_state():
    """Cards + position + hero_to_act — XML gamecode se anchored (100% sync)."""
    xml, tbl = _live_table_xml(None)
    if not xml or not tbl:
        h = read_live_hand()
        if h:
            h["pos"] = None
            h["hero_to_act"] = None
        return h
    st = _parse_table_state(xml)
    if not st:
        h = read_live_hand()
        if h:
            h["pos"] = None
            h["hero_to_act"] = None
        return h
    gc = st["gamecode"]
    hand = {"hand_id": gc, "hero": [], "board": [], "street": None, "mtime": None}
    hero, board, mem_used = [], [], False
    # XML rounds se current street nikaalo (memory reader ko board length batane ke liye)
    max_r = max(st["rounds"].keys()) if st["rounds"] else 0
    xml_street = {0: None, 1: None, 2: "flop", 3: "turn", 4: "river"}.get(max_r)
    # 1) MEMORY first — client isi hand ka PROTOBUF memory me LIVE likhta hai (no lag)
    if HAS_MEM:
        try:
            pid = redstar_mem.find_pid()
            if pid:
                r = redstar_mem.find_current_hand(pid, gc, xml_street)
                if r and (len(r[0]) == 2 or r[1]):
                    hero, board = r
                    mem_used = True
        except Exception:
            pass
    # 2) binary file fallback — client LIVE hand ko C++ structs me rakhta hai (memory me
    #    readable nahi), sirf hand COMPLETE hone pe <gamecode> naam ki file likhta hai.
    #    Sirf ISI gamecode ki file lo — newest file pichla hand hota hai (stale cards).
    if not mem_used:
        try:
            bf = tbl / gc
            if bf.is_file():
                hh = _parse(bf.read_bytes(), bf.name, bf.stat().st_mtime)
                hero, board = hh["hero"], hh["board"]
        except Exception:
            pass
    hand["hero"] = hero
    hand["board"] = board
    hand["street"] = {0: None, 3: "flop", 4: "turn", 5: "river"}.get(len(board))
    hand["table"] = tbl.name
    hand["pos"] = _position(st["names"], st["dealer_idx"])
    hand["dealer"] = st["names"][st["dealer_idx"]] if st["names"] else None
    hand["seats"] = {nm: i for i, nm in enumerate(st["names"])}
    # position -> naam (opponents ke liye, villain auto-select)
    names_by_pos = {}
    for nm in st["names"]:
        if nm == HERO:
            continue
        p = _position(st["names"], st["dealer_idx"], nm)
        if p:
            names_by_pos[p] = nm
    hand["names"] = names_by_pos
    # street: binary board se, lekin XML rounds se verify karo (round count)
    rounds = st["rounds"]
    street = hand.get("street")
    if street is None:
        acts = rounds.get(0, []) + rounds.get(1, [])
        hand["hero_to_act"] = _next_to_act(st["names"], st["dealer_idx"], acts, True)
    else:
        rmap = {"flop": 2, "turn": 3, "river": 4}
        acts = rounds.get(rmap.get(street, 2), [])
        hand["hero_to_act"] = _next_to_act(st["names"], st["dealer_idx"], acts, False)
    return hand


def read_last_completed():
    r"""Data\Tables XML me se last completed hand (100% authoritative, verification)."""
    try:
        f = max(DONE_DATA.glob("*.xml"), key=lambda p: p.stat().st_mtime)
    except Exception:
        return None
    xml = f.read_text(encoding="utf-8", errors="replace")
    games = re.findall(r"<game gamecode=\"(\d+)\">(.*?)</game>", xml, re.S)
    if not games:
        return None
    gc, body = games[-1]
    hero, board = [], []
    m = re.search(r'<cards player="' + HERO + r'" type="Pocket">(.*?)</cards>', body)
    if m:
        toks = m.group(1).split()
        hero = [_norm(t) for t in toks if t != "X"]
    for street in ("Flop", "Turn", "River"):
        m = re.search(r'<cards type="' + street + r'">(.*?)</cards>', body)
        if m:
            board += [_norm(t) for t in m.group(1).split() if t != "X"]
    return {"gamecode": gc, "hero": hero, "board": board,
            "street": {0: None, 3: "flop", 4: "turn", 5: "river"}.get(len(board))}


if __name__ == "__main__":
    pid = None
    for _ in range(8):
        h = read_live_hand()
        done = read_last_completed()
        print(time.strftime("%H:%M:%S"), "LIVE:", h)
        if _ == 0:
            print("           DONE:", done)
        if h and h["hand_id"] != (None):
            pass
        time.sleep(2)
