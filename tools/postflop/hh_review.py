"""
Post-session hand review — ek KHATAM hua GG/Natural8 hand history ("Poker Hand #...")
lo aur flop/turn/river pe har decision ko TexasSolver se check karo.

- Sirf poora hand chalta hai (SUMMARY section zaroori) — chalte hand ke liye nahi hai.
- Heads-up flop, NLHE. Preflop ranges app bhejta hai (uske spots se).
- Har street alag solve hoti hai; agli street ki ranges pichli street ki line se narrow hoti hain.
- Tree me hand ke ASLI bet sizes daale jaate hain taaki line tree me mil jaaye.

Output har decision ke liye: hero ne kya kiya vs solver frequencies; villain ne bet/raise
kiya to us waqt uski range kaisi thi (value / medium / draw / bluff).
"""
import hashlib
import json
import re
from collections import defaultdict
from itertools import combinations

import gto_server as g

POST_ORDER = ["SB", "BB", "UTG", "MP", "CO", "BTN"]      # postflop pehle kaun bolta hai
STREET_ITER = {"flop": 40, "turn": 150, "river": 200}
BOARD_N = {"flop": 3, "turn": 4, "river": 5}


# --------------------------------------------------------------------------
# Hand history parse (GG / Natural8 "Poker Hand #")
# --------------------------------------------------------------------------
def _positions(seats, btn_seat, sb, bb):
    s = sorted(seats, key=lambda x: x[0])
    bi = next((i for i, x in enumerate(s) if x[0] == btn_seat), None)
    if bi is None:
        bi = next((i for i, x in enumerate(s) if x[0] > btn_seat), 0)
    names = [x[1] for x in s[bi:] + s[:bi]]
    pos = {}
    if names[0] != sb:
        pos[names[0]] = "BTN"
    if sb:
        pos[sb] = "SB"
    if bb:
        pos[bb] = "BB"
    rest = []
    if bb in names:
        k = names.index(bb)
        for j in range(1, len(names)):
            nm = names[(k + j) % len(names)]
            if nm == names[0]:
                break
            rest.append(nm)
    for nm, p in zip(rest, ["UTG", "MP", "CO"][-len(rest):] if rest else []):
        pos[nm] = p
    return pos


def parse_hand(text):
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    head = re.match(r"^Poker Hand #(\S+): Hold'em No Limit \([^\d]*([\d.]+)/[^\d]*([\d.]+)\)", lines[0])
    if not head:
        raise ValueError("Sirf NLHE 'Poker Hand #' hand history chalti hai")
    if not any(l.startswith("*** SUMMARY ***") for l in lines):
        raise ValueError("Hand poora nahi hai (SUMMARY nahi mila) — sirf khatam hue hands review hote hain")
    bb = float(head.group(3))
    btn = int(re.search(r"Seat #(\d+) is the button", lines[1]).group(1))
    seats, stacks = [], {}
    hero = hero_cards = sb_name = bb_name = None
    for l in lines:
        m = re.match(r"^Seat (\d+): (.+?) \([^\d]*([\d.]+) in chips\)", l)
        if m:
            seats.append((int(m.group(1)), m.group(2)))
            stacks[m.group(2)] = float(m.group(3)) / bb
        m = re.match(r"^Dealt to (.+?) \[(\S\S) (\S\S)\]$", l)
        if m:
            hero, hero_cards = m.group(1), [m.group(2), m.group(3)]
        m = re.match(r"^(.+?): posts small blind", l)
        if m:
            sb_name = m.group(1)
        m = re.match(r"^(.+?): posts big blind", l)
        if m:
            bb_name = m.group(1)
    if not hero:
        raise ValueError("Hero ke cards nahi mile")
    pos = _positions(seats, btn, sb_name, bb_name)

    acts, board, invested, street_put = [], [], defaultdict(float), defaultdict(float)
    street = "pre"
    street_pot, street_inv = {}, {}
    for l in lines:
        m = re.match(r"^\*\*\* (FLOP|TURN|RIVER) \*\*\* (.*)$", l)
        if m:
            street = m.group(1).lower()
            board = re.findall(r"\b[2-9TJQKA][shdc]\b", m.group(2))
            street_pot[street] = sum(invested.values())
            street_inv[street] = dict(invested)
            street_put = defaultdict(float)
            continue
        if re.match(r"^\*\*\* (SHOWDOWN|SUMMARY) \*\*\*", l):
            street = "done"
        m = re.match(r"^(.+?): posts (?:small|big) blind [^\d]*([\d.]+)", l)
        if m:
            amt = float(m.group(2)) / bb
            invested[m.group(1)] += amt
            street_put[m.group(1)] += amt
            continue
        m = re.match(r"^Uncalled bet \([^\d]*([\d.]+)\) returned to (.+)$", l)
        if m:
            invested[m.group(2)] -= float(m.group(1)) / bb
            continue
        if street == "done":
            continue
        m = re.match(r"^(.+?): (folds|checks|calls|bets|raises)(?: [^\d]*([\d.]+))?(?: to [^\d]*([\d.]+))?", l)
        if not m:
            continue
        nm, verb = m.group(1), m.group(2)
        a = {"street": street, "name": nm, "pos": pos.get(nm, "?"), "verb": verb,
             "pot": sum(invested.values()), "allin": "all-in" in l}
        if verb in ("calls", "bets"):
            a["amt"] = float(m.group(3)) / bb
            invested[nm] += a["amt"]
            street_put[nm] += a["amt"]
        elif verb == "raises":
            a["to"] = float(m.group(4)) / bb
            add = a["to"] - street_put[nm]
            invested[nm] += add
            street_put[nm] = a["to"]
        a["pot_after"] = sum(invested.values())
        a["street_put"] = dict(street_put)
        acts.append(a)
    return {"bb": bb, "hero": hero, "hero_cards": hero_cards, "pos": pos, "stacks": stacks,
            "acts": acts, "board": board, "street_pot": street_pot, "street_inv": street_inv}


# --------------------------------------------------------------------------
# Solver
# --------------------------------------------------------------------------
def _script(board, pot, stack, ip_r, oop_r, sizes, max_iter, out_json):
    lines = [f"set_pot {pot:.2f}", f"set_effective_stack {stack:.2f}", f"set_board {','.join(board)}",
             f"set_range_ip {ip_r}", f"set_range_oop {oop_r}"]
    st = "flop" if len(board) == 3 else "turn" if len(board) == 4 else "river"
    for pl in ("oop", "ip"):
        bets = sorted(set([33, 75] + sizes.get((pl, "bet"), [])))
        lines.append(f"set_bet_sizes {pl},{st},bet,{','.join(str(b) for b in bets)}")
        lines.append(f"set_bet_sizes {pl},{st},raise,{','.join(str(r) for r in sorted(set([75] + sizes.get((pl, 'raise'), []))))}")
        lines.append(f"set_bet_sizes {pl},{st},allin")
    lines += ["set_allin_threshold 0.67", "build_tree", f"set_thread_num {g.THREADS}",
              "set_accuracy 0.01", f"set_max_iteration {max_iter}", "set_print_interval 20",
              "set_use_isomorphism 1", "start_solve", "set_dump_rounds 1", f"dump_result {out_json}"]
    return "\n".join(lines) + "\n"


def _solve(board, pot, stack, ip_r, oop_r, sizes, max_iter):
    key_src = json.dumps([board, round(pot, 2), round(stack, 2), ip_r, oop_r,
                          sorted((k[0] + k[1], v) for k, v in sizes.items()), max_iter])
    key = "rv_" + hashlib.md5(key_src.encode()).hexdigest()[:16]
    cache = g.CACHE_DIR / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    g.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    in_path = g.CACHE_DIR / f"{key}_input.txt"
    out_path = g.SOLVER_DIR / f"{key}.json"
    in_path.write_text(_script(board, pot, stack, ip_r, oop_r, sizes, max_iter, f"{key}.json"), encoding="utf-8")
    out_path.unlink(missing_ok=True)
    with g._solve_lock:
        with open(g.CACHE_DIR / f"{key}.log", "w") as lf:
            g.subprocess.run([str(g.SOLVER_EXE), "-i", str(in_path)], cwd=str(g.SOLVER_DIR),
                             stdout=lf, stderr=g.subprocess.STDOUT, check=True,
                             creationflags=(g.subprocess.CREATE_NO_WINDOW | 0x00004000) if g.os.name == "nt" else 0)
    tree = json.loads(out_path.read_text(encoding="utf-8"))
    cache.write_text(json.dumps(tree), encoding="utf-8")
    out_path.unlink(missing_ok=True)
    return tree


def _match_action(node, a):
    """HH ka action -> tree ka sabse kareebi action key."""
    keys = node.get("actions", [])
    want = {"folds": "FOLD", "checks": "CHECK", "calls": "CALL"}.get(a["verb"])
    if want:
        return want if want in keys else None
    amt = a.get("to") if a["verb"] == "raises" else a.get("amt")
    kind = "RAISE" if a["verb"] == "raises" else "BET"
    cands = [(abs(g.action_amount(k) - amt), k) for k in keys if k.startswith(kind)]
    return min(cands)[1] if cands else None


# --------------------------------------------------------------------------
# Villain range: value / medium / draw / bluff
# --------------------------------------------------------------------------
def _board_cat(board):
    counts = defaultdict(int)
    for c in board:
        counts[c[0]] += 1
    top = sorted(counts.values(), reverse=True)
    if len(board) == 5:
        return g.five_rank([g.parse_card(c) for c in board])[0]
    if top[0] == 3:
        return 3
    if top[0] == 2 and len(top) > 1 and top[1] == 2:
        return 2
    return 1 if top[0] == 2 else 0


def _has_draw(hole, board):
    cards = hole + board
    suits = defaultdict(int)
    for c in cards:
        suits[c[1]] += 1
    if any(n == 4 for n in suits.values()) and any(suits[c[1]] == 4 for c in hole):
        return True
    rs = {g.rank_i(c[0]) for c in cards}
    if 12 in rs:
        rs.add(-1)
    for lo in range(-1, 9):
        win = set(range(lo, lo + 5))
        if len(win & rs) == 4 and any(g.rank_i(c[0]) in win for c in hole):
            return True
    return False


def classify(combo, board):
    """Villain combo kitna strong hai — sirf wahi gino jo HOLE cards se bana (board ka pair sabka hai)."""
    hole = [combo[0:2], combo[2:4]]
    hr = [g.rank_i(c[0]) for c in hole]
    br = [g.rank_i(c[0]) for c in board]
    cat = g.seven_rank([g.parse_card(c) for c in hole + board])[0]
    if cat > _board_cat(board) and cat >= 3:
        return "value"                                   # trips / straight / flush / full house+
    pairs = ([hr[0]] if hr[0] == hr[1] else []) + [r for r in hr if r in br]
    if pairs:
        if hr[0] != hr[1] and len(set(pairs)) >= 2:
            return "value"                               # dono hole cards se two pair
        return "value" if max(pairs) >= max(br) else "medium"   # top pair / overpair vs neeche wala pair
    if len(board) < 5 and _has_draw(hole, board):
        return "draw"
    return "bluff"


def range_breakdown(weights, board, dead):
    tot, parts, top = 0.0, defaultdict(float), []
    for combo, w in weights.items():
        if w <= 0.001 or any(g.card_in(c, dead) for c in (combo[0:2], combo[2:4])):
            continue
        k = classify(combo, board)
        parts[k] += w
        tot += w
        top.append((w, combo, k))
    if tot <= 0:
        return None
    top.sort(reverse=True)
    cls = defaultdict(float)
    for w, combo, k in top:
        cls[(g.combo_to_class(combo), k)] += w
    best = sorted(cls.items(), key=lambda kv: -kv[1])[:10]
    return {"pct": {k: round(v / tot * 100) for k, v in parts.items()},
            "combos": round(tot, 1),
            "top": [{"hand": h, "kind": k, "w": round(v, 2)} for (h, k), v in best]}


def _reach_at(tree, line, prior):
    """Line ke is point tak dono players ki reach weights (pichli street ki ranges se shuru)."""
    return g.reach_weights(line, tree, prior)


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------
def review(hh_text, ranges):
    """ranges: {"UTG": "AA,KK,...", ...} — app ke spots se preflop ranges (hand classes)."""
    h = parse_hand(hh_text)
    post = [a for a in h["acts"] if a["street"] in ("flop", "turn", "river")]
    if not post:
        return {"error": "Flop tak nahi pahuncha — sirf preflop review"}
    players = []
    for a in post:
        if a["name"] not in players:
            players.append(a["name"])
    if len(players) != 2 or h["hero"] not in players:
        return {"error": "Multiway ya hero ke bina pot — solver sirf heads-up review karta hai"}
    vil = next(p for p in players if p != h["hero"])
    hp, vp = h["pos"][h["hero"]], h["pos"][vil]
    ip_name = max(players, key=lambda n: POST_ORDER.index(h["pos"][n]))
    oop_name = next(p for p in players if p != ip_name)
    rng = {n: ranges.get(h["pos"][n]) for n in players}
    if not all(rng.values()):
        return {"error": "Preflop ranges nahi mili is line ke liye"}
    from rangeutil import weighted_range_string
    hero_combo = g.combo_key(*h["hero_cards"])
    # hero ka asli hand range me na ho (range ka andaza, ya off-range play) to jod do —
    # warna solver us hand ke liye koi strategy nahi deta
    hero_cls = g.combo_to_class(hero_combo)
    notes = []
    if hero_cls not in rng[h["hero"]].split(","):
        rng[h["hero"]] += "," + hero_cls
        notes.append(f"{hero_cls} aapki preflop range me nahi tha — review ke liye range me joda")
    ip_r, oop_r = weighted_range_string(rng[ip_name]), weighted_range_string(rng[oop_name])

    out = {"hero": hp, "villain": vp, "heroIP": ip_name == h["hero"], "streets": [], "notes": notes}
    prior = None
    eff0 = min(h["stacks"][h["hero"]], h["stacks"][vil])
    for st in ("flop", "turn", "river"):
        sacts = [a for a in post if a["street"] == st]
        if not sacts:
            break
        board = h["board"][:BOARD_N[st]]
        pot = h["street_pot"][st]
        inv = h["street_inv"][st]
        stack = max(1.0, min(h["stacks"][n] - inv.get(n, 0) for n in players))
        sizes = defaultdict(list)
        for a in sacts:                                   # asli bet sizes tree me daalo (% pot)
            pl = "ip" if a["name"] == ip_name else "oop"
            if a["verb"] == "bets" and a["pot"] > 0:
                sizes[(pl, "bet")].append(max(10, min(200, round(a["amt"] / a["pot"] * 100))))
        if prior is None:
            ip_rs, oop_rs = ip_r, oop_r
        else:
            ip_rs = g.weighted_range_string_from(prior[0], board)
            oop_rs = g.weighted_range_string_from(prior[1], board)
        try:
            tree = _solve(board, pot, stack, ip_rs, oop_rs, dict(sizes), STREET_ITER[st])
        except Exception as e:
            out["streets"].append({"street": st, "board": board, "error": f"solve fail: {e}"})
            break
        line, node, steps = [], tree, []
        for a in sacts:
            if not node or node.get("node_type") != "action_node":
                break
            key = _match_action(node, a)
            if key is None:
                steps.append({"who": "hero" if a["name"] == h["hero"] else "villain", "note": "Ye action solver tree me nahi mila"})
                break
            labels = {k: g.action_label(k, pot, stack)["label"] for k in node.get("actions", [])}
            if a["name"] == h["hero"]:
                strat = g.node_strategy(node).get(hero_combo)
                step = {"who": "hero", "did": labels.get(key, key)}
                if strat:
                    mix = {labels[k]: round(float(strat[i]) * 100) for i, k in enumerate(node["actions"])}
                    f = mix.get(labels[key], 0)
                    step.update(mix=mix, best=max(mix, key=mix.get), freq=f,
                                verdict="good" if f >= 50 else "mixed" if f >= 15 else "bad")
                else:
                    step["note"] = "Aapka hand solver ki range me nahi (preflop line range ke bahar)"
                steps.append(step)
            elif a["verb"] in ("bets", "raises"):
                ip_w, oop_w = _reach_at(tree, line, prior)
                vw = ip_w if a["name"] == ip_name else oop_w
                st_v = g.node_strategy(node)
                idx = node["actions"].index(key)
                after = {c: w * st_v[c][idx] for c, w in vw.items() if c in st_v}
                br = range_breakdown(after, board, h["hero_cards"] + board)
                sp_ = a["street_put"]                     # pot odds: call / (pot after bet + call)
                call_amt = max(sp_.values()) - sp_.get(h["hero"], 0)
                need = round(call_amt / (a["pot_after"] + call_amt) * 100) if call_amt > 0 else None
                steps.append({"who": "villain", "did": labels.get(key, key), "range": br, "needEq": need})
            else:
                steps.append({"who": "villain", "did": labels.get(key, key)})
            line.append(key)
            node = (node.get("childrens") or {}).get(key)
        out["streets"].append({"street": st, "board": board, "pot": round(pot, 1), "steps": steps})
        prior = _reach_at(tree, line, prior)
    return out


# --------------------------------------------------------------------------
# Hand history files — app khulte hi auto-load (sirf KHATAM hue sessions)
# --------------------------------------------------------------------------
def hh_dirs():
    """Red Star/iPoker: data/<nick>/History/Data/Tables (table band hone pe likhi file).
    TempData (chalta session) jaan-boojh ke nahi padhte. PREFLOP_HH_DIRS se aur folders (';' se alag)."""
    import os
    from pathlib import Path
    dirs = []
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for nick in (local / "Red Star Poker" / "data").glob("*/History/Data/Tables"):
        dirs.append(nick)
    for extra in [Path.home() / "Documents" / "DriveHUD2" / "HandHistory_Import"] + \
                 [Path(p) for p in os.environ.get("PREFLOP_HH_DIRS", "").split(";") if p]:
        if extra.is_dir():
            dirs.append(extra)
    return dirs


def load_hh_files(max_mb=40):
    out, total = [], 0
    for d in hh_dirs():
        for f in sorted(d.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.suffix.lower() not in (".xml", ".txt") or not f.is_file():
                continue
            size = f.stat().st_size
            if total + size > max_mb * 1024 * 1024:
                break
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "Holdem NL" not in text and "Hold'em No Limit" not in text:
                continue                                  # PLO wagairah — review nahi hota
            out.append({"name": f.name, "dir": str(d), "text": text})
    return {"dirs": [str(d) for d in hh_dirs()], "files": out}


# --------------------------------------------------------------------------
# Player notes — disk pe save, naya data aane pe update
# --------------------------------------------------------------------------
import threading as _th
_notes_lock = _th.Lock()


def _notes_path():
    return g.WORK_DIR / "player_notes.json"


def notes_get():
    try:
        return json.loads(_notes_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def notes_update(name, **fields):
    with _notes_lock:
        data = notes_get()
        entry = data.get(name, {})
        entry.update(fields)
        data[name] = entry
        _notes_path().parent.mkdir(parents=True, exist_ok=True)
        _notes_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return entry


def exploits_update(players):
    """{name: {"ex": [[spot, text], ...], "hands": n}} — jinke exploits badle unka naya snapshot save.
    Pichla snapshot "ex_prev" me rehta hai taaki app dikha sake kya naya aaya / kya hata."""
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M")
    with _notes_lock:
        data = notes_get()
        for name, v in players.items():
            e = data.get(name, {})
            if e.get("ex") != v.get("ex"):
                e["ex_prev"], e["ex"] = e.get("ex"), v.get("ex")
                e["ex_hands"], e["ex_updated"] = v.get("hands"), now
                data[name] = e
        _notes_path().parent.mkdir(parents=True, exist_ok=True)
        _notes_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data


def player_note(p):
    """Player ke stats + showdowns se AI profile (Hinglish), aur disk pe save. Sirf review ke liye."""
    if not g.DEEPSEEK_KEY:
        return {"error": "AI off hai (DEEPSEEK_API_KEY set nahi)"}
    system = ("Tum ek data-driven poker analyst ho. Ek opponent ka 6-max NLHE data diya hai: overall stats, "
              "aur 'streets' me har street (flop/turn/river) ka: bet_or_raise/actions, avg bet size (% pot), "
              "showdown_when_betting (jab usne us street pe bet/raise karke showdown dikhaya: value/medium/draw/bluff counts), "
              "aur faced_bet (us pe bet hua to fold/call/raise counts). "
              "SIRF in numbers se Hinglish me likho — koi emotional/generic advice nahi, har claim ke saath number aur sample (n). "
              "Format:\n1) Type: 1 line, numbers ke saath.\n"
              "2) Har street (Flop, Turn, River) ke liye: 'Uska bet: X% value / Y% bluff (n=..)', 'Call ke liye equity chahiye: s/(1+2s) se', "
              "'Uska fold vs bet: Z% (n=..), bluff breakeven 33%=25%, 50%=33%, 75%=43%', aur 'MERA ACTION:' — facing his bet (call/fold/raise kis hand strength se) "
              "aur jab main bet karu (bluff haan/nahi, value kitna thin, sizing).\n"
              "3) Preflop: 'preflop_showdowns_by_raise_level' se open / 3-bet / 4-bet me premium (AA/KK/QQ/AK), strong (JJ/TT/AQ), "
              "speculative (A5s, suited connectors, small pairs) aur garbage ka % (n ke saath), fourbet_vs_3bet frequency, "
              "aur MERA ACTION: uske 3-bet/4-bet ke khilaf kya continue/4-bet/fold karna hai.\n"
              "n<5 ho to us line me likho 'data kam — GTO default'. 180-260 words. Pichla note diya ho to sirf numbers ke basis pe update karo.")
    user = json.dumps(p, ensure_ascii=False)
    prev = notes_get().get(p.get("name"), {})
    if prev.get("ai"):                                   # purana note do, taaki AI batae kya badla
        user += ("\n\nPichla note (" + str(prev.get("hands")) + " hands pe): " + prev["ai"] +
                 "\nNaye data se ise UPDATE karo; jo badla hai wo saaf batao.")
    try:
        text = g.deepseek_chat(system, user, max_tokens=700)
    except Exception as e:
        return {"error": f"AI call fail: {e}"}
    import time as _t
    saved = notes_update(p.get("name"), ai=text, hands=p.get("hands"), stats=p.get("stats"),
                         category=p.get("category"), updated=_t.strftime("%Y-%m-%d %H:%M"))
    return {"text": text, "saved": saved}
