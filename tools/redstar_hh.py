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


def _table_title(txt):
    """XML header ka <tablename> -> 'Bewdley 817748117' (window title isi se shuru hota hai)."""
    m = re.search(r"<tablename>(.*?)</tablename>", txt)
    return m.group(1).replace(",", "").strip() if m else ""


def _live_table_xml(table_name):
    """Hero wale table ka live XML text (aur table dir). table_name (window title) diya ho to
    SIRF usi table ki XML — multi-table me har window ki apni hand history."""
    tables = _tables()
    for tbl in tables:
        xml_path = tbl.parent / (tbl.name + ".xml")
        if xml_path.exists():
            try:
                txt = xml_path.read_text(encoding="utf-8", errors="replace")
                if HERO not in txt:
                    continue
                if table_name:
                    t = _table_title(txt)
                    if not t or not table_name.replace(",", "").startswith(t):
                        continue
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
    broke = set()
    for ptag in re.findall(r'<player\s+([^>]+)/>', body):
        nm = re.search(r'name="([^"]+)"', ptag)
        st = re.search(r'seat="(\d+)"', ptag)
        if not (nm and st):
            continue
        names.append(nm.group(1))
        seats.append(int(st.group(1)))
        ch = re.search(r'chips="[^"\d]*([\d.]+)', ptag)
        if ch and float(ch.group(1)) == 0:
            broke.add(nm.group(1))            # stack 0 = bust ho chuka, is hand me deal nahi hoga
        if re.search(r'dealer="1"', ptag):
            dealer_idx = len(names) - 1
    if not names:
        return None
    # rounds: body ko <round no=...> se tod do (last round incomplete ho sakta hai)
    rounds = {}
    for rm in re.finditer(r'<round no="(\d+)">(.*?)(?=<round no=|\Z)', body, re.S):
        rno = int(rm.group(1))
        acts = []
        for am in re.finditer(r'<action\s+([^>]+)/?>', rm.group(2)):
            p = re.search(r'player="([^"]+)"', am.group(1))
            t = re.search(r'type="(\d+)"', am.group(1))
            s = re.search(r'sum="([^"]*)"', am.group(1))
            if p and t:
                acts.append((p.group(1), t.group(1), s.group(1) if s else "0"))
        rounds[rno] = acts
    prev_gc = m[-2].group(1) if len(m) > 1 else None
    return {"gamecode": gc, "prev_gamecode": prev_gc, "names": names, "seats": seats, "broke": broke,
            "dealer_idx": dealer_idx, "rounds": rounds}


_last_dealt_cache = {}


def last_dealt(tbl_name, prev_gc):
    """Pichle hand (prev_gc) me jo players DEAL hue the unke naam — DONE_DATA XML se (wahan
    sirf dealt players likhe hote hain). Live XML me sit-out players bhi baithe dikhte hain;
    jo pichle hand me deal nahi hua aur is hand me blind bhi nahi diya, woh abhi bhi bahar hai.
    None = pata nahi (file/hand nahi mila) — tab koi filter mat lagao."""
    if not tbl_name or not prev_gc:
        return None
    try:
        f = DONE_DATA / (str(tbl_name) + ".xml")
        s = f.stat()
        key = (str(f), prev_gc)
        hit = _last_dealt_cache.get(key)
        if hit and hit[0] == (s.st_mtime, s.st_size):
            return hit[1]
        txt = f.read_text(encoding="utf-8", errors="replace")
        i = txt.find('<game gamecode="%s">' % prev_gc)
        out = None
        if i >= 0:
            j = txt.find("</players>", i)
            out = set(re.findall(r'<player\s[^>]*name="([^"]+)"', txt[i:j])) if j > i else None
        if len(_last_dealt_cache) > 50:
            _last_dealt_cache.clear()
        _last_dealt_cache[key] = ((s.st_mtime, s.st_size), out or None)
        return out or None
    except Exception:
        return None


def _position(names, dealer_idx, hero=HERO):
    if hero not in names:
        return None
    n = len(names)
    off = (names.index(hero) - dealer_idx) % n
    tbl = {2: POS_2, 3: POS_3, 4: POS_4, 5: POS_5, 6: POS_6}.get(n, POS_6)
    return tbl.get(off, "BTN")


def _refit(names, d, kept, acts0):
    """names ko kept tak chhota karo -> (names, dealer_idx) ya None (filter mat lagao).
    Dealer button kabhi aise player pe hota hai jo deal nahi hua (uth gaya / sit-out = dead
    button): tab asli button = SB poster se theek pehle wala dealt player (heads-up me SB hi)."""
    if len(kept) < 2:
        return None
    if names[d] in kept:
        return kept, kept.index(names[d])
    sb = next((nm for nm, t, _ in acts0 if t == "1"), None)
    if sb in kept:
        i = kept.index(sb)
        return kept, (i if len(kept) == 2 else (i - 1) % len(kept))
    bb = next((nm for nm, t, _ in acts0 if t == "2"), None)
    if bb in kept and len(kept) > 2:            # dead small blind: button BB se do pehle
        return kept, (kept.index(bb) - 2) % len(kept)
    return None


def dealt_positions(st, prev_dealt=None, extra_keep=(), hero_absent=False, drop=()):
    """Asli dealt positions (sit-out / wait-for-BB players hata ke): {name: pos}.
    XML me wait-for-BB / sit-out players bhi baithe hote hain jo is hand me DEAL nahi hote —
    blinds ke beech wale (dealer->SB, SB->BB) unhe hata ke hi positions sahi aati hain.
    Yeh hatao to pura table ek seat khisak jaata hai (BB ko MP padh leta hai).
    prev_dealt: pichle hand ke dealt naam (last_dealt) — jo usme nahi tha, blind nahi diya aur
    extra_keep (card backs dikhe) me bhi nahi, woh sit-out hai (blinds ke bahar baitha ho tab bhi)."""
    names = list(st.get("names") or [])
    if not names:
        return {}
    d = st["dealer_idx"]
    acts0 = (st.get("rounds") or {}).get(0, [])
    posters = {nm for nm, _, _ in acts0}
    drop = set(drop) | set(st.get("broke") or ())
    if drop:
        # screen pe "SIT OUT" badge wale / stack 0 wale — pakka deal nahi hue (blind diya ho to rakho)
        fit = _refit(names, d, [nm for nm in names if nm not in drop or nm in posters], acts0)
        if fit:
            names, d = fit
    if hero_absent and not prev_dealt and HERO in names and HERO not in posters \
            and HERO not in extra_keep:
        # pichla hand log nahi hua (hero usme deal nahi tha / table pe pehla hand) aur hero ke
        # cards bhi nahi dikh rahe — hero abhi bhi bahar baitha hai, positions me mat gino
        fit = _refit(names, d, [nm for nm in names if nm != HERO], acts0)
        if fit:
            names, d = fit
    if prev_dealt:
        kept = [nm for nm in names
                if nm in prev_dealt or nm in posters or nm in extra_keep]
        fit = _refit(names, d, kept, acts0)
        if fit:
            names, d = fit
    n = len(names)
    # dead blind (naya player BB post karta hai) bhi type 2 hota hai — asli SB/BB PEHLA poster hai
    blinds = {}
    for nm, t, _ in acts0:
        if t in ("1", "2"):
            blinds.setdefault(t, nm)
    sb, bb = blinds.get("1"), blinds.get("2")
    if not sb or not bb:
        sb, bb = names[d], names[(d + 1) % n]
    # Heads-up: dealer hi SB hota hai (BB doosra). General order logic HU me BB ko hata deta
    # tha (dealer ke 'baad' SB nahi hota) — isliye seedha assign karo.
    # Dealer ne khud SB post kiya = hand heads-up hai, chahe table pe 3-4 log baithe hon
    # (baaki sit-out). 3+ handed me dealer kabhi SB nahi deta.
    if n == 2 or (blinds.get("1") == names[d] and blinds.get("2")):
        return {sb: "SB", bb: "BB"}
    order = [names[(d + i) % n] for i in range(1, n + 1)]   # dealer ke baad clockwise
    keep = set(names)
    if bb in order:
        stop = order.index(sb) if sb in order else order.index(bb)
        keep -= set(order[:stop])                            # dealer -> SB ke beech (wait)
        if sb in order:
            keep -= set(order[order.index(sb) + 1:order.index(bb)])  # SB -> BB ke beech (wait)
    dealt = [nm for nm in names if nm in keep]
    if names[d] not in keep:
        return {}
    dd = dealt.index(names[d])
    return {nm: _position(dealt, dd, nm) for nm in dealt}


def _money(v):
    m = re.search(r"[\d.]+", v)
    return float(m.group()) if m else 0.0


def _next_to_act(names, dealer_idx, acts, preflop, hero=HERO):
    """acts: [(name, type)] — current street ke actions. Returns True agar hero to act."""
    n = len(names)
    if not n:
        return False
    folded, acted = set(), set()
    last_idx = None
    for nm, t, _ in acts:
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


def read_live_state(use_mem=True, table_name=None):
    """Cards + position + hero_to_act — XML gamecode se anchored (100% sync).
    use_mem=False: PokerClient memory scan mat karo (seconds lagte hain, live hand wahan milta nahi).
    table_name: window title — multi-table me usi table ka state."""
    xml, tbl = _live_table_xml(table_name)
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
    if HAS_MEM and use_mem:
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
    # sit-out/wait players hata ke (sahi positions)
    pos_map = dealt_positions(st, last_dealt(tbl.name, st.get("prev_gamecode")), {HERO})
    hand["pos"] = pos_map.get(HERO)
    hand["dealer"] = st["names"][st["dealer_idx"]] if st["names"] else None
    hand["seats"] = {nm: i for i, nm in enumerate(st["names"])}
    # position -> naam (opponents ke liye, villain auto-select)
    hand["names"] = {p: nm for nm, p in pos_map.items() if nm != HERO and p}
    # PREFLOP bets (100% accurate XML se): {pos: total bb invested} — limp + raise exact.
    # raise (type 23) ka sum = street TOTAL ("to"); blind/call/bet/all-in ka sum = increment.
    bbv = next((_money(v) for _, t, v in st["rounds"].get(0, []) if t == "2"), 0.0) or 0.02
    put = {}
    for no in (0, 1):
        for nm, t, v in st["rounds"].get(no, []):
            amt = _money(v)
            if t == "23":
                put[nm] = amt                     # raise: sum = total "to"
            elif t in ("0", "4"):
                continue                          # fold/check — koi chips nahi
            else:
                put[nm] = put.get(nm, 0.0) + amt  # blind/call/bet/all-in: increment
    pf = {}
    for nm, amt in put.items():
        p = pos_map.get(nm)
        if p and amt > 0:
            pf[p] = round(amt / bbv, 2)
    hand["pf_bets"] = pf
    # Live XML hand ke DAURAAN sirf blinds (round 0) likhti hai — preflop actions hand khatam hone
    # pe aate hain. Round 1 me actions hon tabhi pf_bets poore hain; warna frontend OCR bets le.
    hand["pf_live"] = bool(st["rounds"].get(1))
    hero_pos = hand.get("pos")
    hand["pf_to_call"] = round(max(pf.values(), default=0.0) - pf.get(hero_pos, 0.0), 2) if pf else 0.0
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


# ---------------- rake + profit (completed hands XML se) ----------------
GAME_BY_CARDS = {2: "nlh", 4: "plo4", 5: "plo5", 6: "plo6"}   # hero ke pocket cards ki ginti = game
_pnl_cache = {}


def _hand_pnl(body, hero=HERO):
    """Ek <game> body -> hero ka result: {"game", "net", "rake", "cashout", "ts"} ya None.
    net = rake katne ke BAAD ka profit. `win` me rake pehle se kata hota hai (cashout / run-it-twice
    ka total bhi isi me), lekin `bet` me UNCALLED bet bhi gina hota hai jo wapas aa jaata hai —
    hero ka jitna paisa kisi ne match hi nahi kiya (doosre sabse bade bet se upar) woh ghata do."""
    hero_tag, others = None, []
    for ptag in re.findall(r'<player\s+([^>]+)/>', body):
        nm = re.search(r'name="([^"]+)"', ptag)
        bet = re.search(r'\bbet="([^"]*)"', ptag)
        if not nm:
            continue
        if nm.group(1) == hero:
            hero_tag = ptag
        else:
            others.append(_money(bet.group(1)) if bet else 0.0)
    if hero_tag is None:
        return None                                   # hero is hand me deal nahi hua
    pocket = re.search(r'type="Pocket">(.*?)</cards>', body)
    game = GAME_BY_CARDS.get(len(pocket.group(1).split()) if pocket else 0)
    if not game:
        return None

    def val(key):
        m = re.search(r'\b%s="([^"]*)"' % key, hero_tag)
        return _money(m.group(1)) if m else 0.0

    put = min(val("bet"), max(others, default=0.0))   # uncalled hissa hata ke
    ts = re.search(r"<startdate>(.*?)</startdate>", body)
    return {"game": game, "net": val("win") - put, "rake": val("rakeamount"),
            "cashout": 'cashout="1"' in hero_tag, "ts": ts.group(1) if ts else ""}


def _epoch(ts):
    """'2026-10-02 05:34:46' -> epoch seconds (string ko UTC maan ke) ya None."""
    try:
        import calendar
        return calendar.timegm(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def _file_pnl(f):
    """Ek table XML ke saare hero hands (mtime+size pe cached) -> (currency symbol, [hand, ...])."""
    s = f.stat()
    sig = (s.st_mtime, s.st_size)
    hit = _pnl_cache.get(str(f))
    if hit and hit[0] == sig:
        return hit[1]
    txt = f.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"<bigblind>\s*([^\d\s<]*)", txt)
    hands = [h for h in (_hand_pnl(g) for g in re.findall(r"<game gamecode.*?</game>", txt, re.S)) if h]
    # startdate server ke timezone me hoti hai (UTC nahi). File ka mtime = last hand ka asli waqt,
    # isliye dono ka fark (aadhe ghante pe round) = is file ka timezone offset.
    stamps = re.findall(r"<startdate>(.*?)</startdate>", txt)
    last = _epoch(stamps[-1]) if stamps else None
    off = round((last - s.st_mtime) / 1800) * 1800 if last else 0
    for h in hands:
        e = _epoch(h["ts"])
        h["day"] = time.strftime("%Y-%m-%d", time.localtime(e - off)) if e else h["ts"][:10]
    out = ((m.group(1) if m else ""), hands)
    _pnl_cache[str(f)] = (sig, out)
    return out


def pnl_stats():
    """Game-wise rake + profit: {"cur": "€", "games": {"plo4": {"today": {...}, "all": {...}}, ...}}.
    Har bucket: hands, rake (hero ke jeete pots se kata), net (rake ke baad), gross (rake se pehle),
    cashouts. "today" = aaj ki local date wale hands."""
    def blank():
        return {"hands": 0, "rake": 0.0, "net": 0.0, "gross": 0.0, "cashouts": 0}

    games = {g: {"today": blank(), "all": blank()} for g in GAME_BY_CARDS.values()}
    today = time.strftime("%Y-%m-%d")
    cur, since = "", ""
    try:
        files = sorted(DONE_DATA.glob("*.xml"))
    except Exception:
        files = []
    for f in files:
        try:
            sym, hands = _file_pnl(f)
        except Exception:
            continue
        cur = sym or cur
        for h in hands:
            if h["game"] != "nlh" and (not since or h["day"] < since):
                since = h["day"]                       # sabse purana PLO hand (Total kab se hai)
            buckets = [games[h["game"]]["all"]]
            if h["day"] == today:
                buckets.append(games[h["game"]]["today"])
            for b in buckets:
                b["hands"] += 1
                b["rake"] += h["rake"]
                b["net"] += h["net"]
                b["gross"] += h["net"] + h["rake"]
                b["cashouts"] += h["cashout"]
    for g in games.values():
        for b in g.values():
            for k in ("rake", "net", "gross"):
                b[k] = round(b[k], 2)
    return {"cur": cur, "hero": HERO, "today": today, "since": since, "games": games}


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
