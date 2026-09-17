"""
Generates the v1 postflop c-bet trainer data set.

Spot: BTN opens, BB calls (100bb effective), flop is dealt, BB checks,
BTN (hero, in position) decides. Solves 16 representative flops with the
TexasSolver console binary and extracts BTN's decision node (the node right
after BB's check) into a compact JS data block that gets spliced into
app.html between the POSTFLOP_DATA markers.

Requires: TexasSolver console build extracted at tools/postflop/solver/
  (download TexasSolver-v0.2.0-Windows.zip from
   https://github.com/bupticybee/TexasSolver/releases and extract there;
   the folder is gitignored, not committed).

Usage: python tools/postflop/generate.py
"""
import concurrent.futures
import json
import re
import subprocess
from pathlib import Path

from rangeutil import weighted_range_string

ROOT = Path(__file__).resolve().parent.parent.parent
SOLVER_DIR = ROOT / "tools" / "postflop" / "solver" / "extracted" / "TexasSolver-v0.2.0-Windows"
SOLVER_EXE = SOLVER_DIR / "console_solver.exe"
WORK_DIR = ROOT / "tools" / "postflop" / "work"
APP_HTML = ROOT / "app.html"

POT = 6
EFF_STACK = 97
THREADS_PER_SOLVE = 4    # kept low deliberately -- this machine has 15GB RAM total; each solver process's
                         # regret tables already run 1-2GB, so boards are solved one at a time (MAX_WORKERS=1)
MAX_WORKERS = 1
MAX_ITERATION = 100
ACCURACY = 0.01  # low target -> solver always uses the full iteration budget, so runtime per board is predictable

OPEN_BTN = "22+, A2s+, K2s+, Q2s+, J4s+, T6s+, 95s, 96s, 97s, 98s, 84s, 85s, 86s, 87s, 74s, 75s, 76s, 63s, 64s, 65s, 53s, 54s, 43s, A2o+, K9o+, QTo+, JTo"
OPEN_BTN_B = "J2s, J3s, T5s, 94s, 83s, 73s, 62s, 52s, 42s, 32s, K7o, K8o, Q8o, Q9o, J8o, J9o, T8o, T9o, 98o, 87o"
BB_CALL = "22-66, A6s-A8s, K2s-K9s, Q2s-Q9s, J2s-J8s, T6s-T8s, 95s, 96s, 97s, 84s, 85s, 86s, 74s, 75s, 63s, 64s, 53s, A2o-A9o, K9o-KQo, QTo, QJo, JTo"
BB_CALL_B = "T5s, 94s, 83s, 73s, 62s, 52s, 43s, K8o, K7o, Q9o, J9o, T9o, 98o"

IP_RANGE = weighted_range_string(OPEN_BTN, OPEN_BTN_B)
OOP_RANGE = weighted_range_string(BB_CALL, BB_CALL_B)

# id, board (TexasSolver format), texture label shown to the user
BOARDS = [
    ("a72r",  "As,7d,2c", "dry, ace-high, rainbow"),
    ("a83r",  "Ad,8h,3s", "dry, ace-high, rainbow"),
    ("k83r",  "Kc,8d,3h", "dry, king-high, rainbow"),
    ("k62r",  "Kh,6s,2d", "dry, king-high, rainbow"),
    ("q94r",  "Qs,9d,4c", "dry, queen-high, rainbow"),
    ("jt6tt", "Jh,Th,6c", "wet, two-tone, connected"),
    ("98stt", "9s,8s,5d", "wet, two-tone, connected"),
    ("876tt", "8h,7h,6c", "wet, two-tone, connected"),
    ("t98tt", "Th,9h,8d", "wet, two-tone, connected"),
    ("kk2r",  "Kd,Kc,2s", "paired, high"),
    ("883r",  "8d,8c,3h", "paired, mid"),
    ("552r",  "5h,5s,2c", "paired, low"),
    ("a72mono","Ah,7h,2h", "monotone"),
    ("963mono","9s,6s,3s", "monotone"),
    ("654r",  "6h,5d,4c", "low, connected, rainbow"),
    ("764r",  "7s,6d,4h", "low, one-gap, rainbow"),
]

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
set_accuracy {accuracy}
set_max_iteration {max_iter}
set_print_interval 20
set_use_isomorphism 1
start_solve
set_dump_rounds 1
dump_result {out_json}
"""


def action_key_label(action_str, pot):
    """'CHECK' / 'BET 2.000000' -> (key, {big, sub, cls})"""
    if action_str == "CHECK":
        return "check", {"big": "Check", "sub": "", "cls": "k-call"}
    m = re.match(r"BET ([\d.]+)", action_str)
    amt = float(m.group(1))
    pct = round(amt / pot * 100)
    if pct >= 130 or amt >= EFF_STACK - 1:
        return "allin", {"big": "All-in", "sub": str(round(amt)) + "bb", "cls": "k-allin"}
    if pct <= 50:
        return "bet33", {"big": "Bet", "sub": str(pct) + "% pot", "cls": "k-raise"}
    return "bet75", {"big": "Bet", "sub": str(pct) + "% pot", "cls": "k-3bet"}


def solve_one(board_id, board_str, texture):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    in_path = WORK_DIR / f"{board_id}_input.txt"
    # dump_result's argument is space-split by TexasSolver's own line parser, and this repo's
    # path contains a space ("PREFLOP TRAINER") -- so we must pass a bare relative filename
    # here (resolved against cwd=SOLVER_DIR below), not the absolute WORK_DIR path.
    out_name = f"{board_id}_output.json"
    solver_out_path = SOLVER_DIR / out_name
    final_out_path = WORK_DIR / out_name
    script = INPUT_TEMPLATE.format(
        pot=POT, eff=EFF_STACK, board=board_str,
        ip_range=IP_RANGE, oop_range=OOP_RANGE,
        threads=THREADS_PER_SOLVE, accuracy=ACCURACY, max_iter=MAX_ITERATION,
        out_json=out_name,
    )
    in_path.write_text(script, encoding="utf-8")
    log_path = WORK_DIR / f"{board_id}_run.log"
    solver_out_path.unlink(missing_ok=True)
    with open(log_path, "w") as log_f:
        subprocess.run([str(SOLVER_EXE), "-i", str(in_path)], cwd=str(SOLVER_DIR),
                        stdout=log_f, stderr=subprocess.STDOUT, check=True)
    if not solver_out_path.exists():
        raise RuntimeError(f"{board_id}: solver did not produce {solver_out_path} -- check {log_path}")
    solver_out_path.replace(final_out_path)
    print(f"[done] {board_id}")
    return board_id, board_str, texture, final_out_path


def extract_spot(board_id, board_str, texture, out_path):
    data = json.loads(out_path.read_text(encoding="utf-8"))
    node = data["childrens"]["CHECK"]  # BB checks, BTN (hero) to act
    actions = node["actions"]
    labeled = [action_key_label(a, POT) for a in actions]
    acts = []
    seen = set()
    for key, meta in labeled:
        if key in seen:
            continue
        seen.add(key)
        acts.append({"key": key, **meta})
    strat = {}
    for combo, probs in node["strategy"]["strategy"].items():
        by_key = {}
        for (key, _meta), p in zip(labeled, probs):
            by_key[key] = round(by_key.get(key, 0) + p, 4)
        strat[combo] = by_key
    board_cards = board_str.split(",")
    spot = {
        "id": board_id,
        "board": board_cards,
        "texture": texture,
        "pot": POT,
        "eff": EFF_STACK,
        "heroPos": "BTN",
        "villPos": "BB",
        "acts": acts,
    }
    return spot, strat


def main():
    if not SOLVER_EXE.exists():
        raise SystemExit(f"console_solver.exe not found at {SOLVER_EXE} — download TexasSolver first.")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(solve_one, bid, board, tex) for bid, board, tex in BOARDS]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    by_id = {r[0]: r for r in results}
    ordered = [by_id[bid] for bid, _, _ in BOARDS]

    pf_spots = []
    pf_strat = {}
    for board_id, board_str, texture, out_path in ordered:
        spot, strat = extract_spot(board_id, board_str, texture, out_path)
        pf_spots.append(spot)
        pf_strat[board_id] = strat

    js = "const PF_SPOTS = " + json.dumps(pf_spots, indent=2) + ";\n"
    js += "const PF_STRAT = " + json.dumps(pf_strat, separators=(",", ":")) + ";\n"

    html = APP_HTML.read_text(encoding="utf-8")
    start_marker = "/* POSTFLOP_DATA_START */"
    end_marker = "/* POSTFLOP_DATA_END */"
    pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
    block = start_marker + "\n" + js + end_marker
    if not pattern.search(html):
        raise SystemExit("POSTFLOP_DATA markers not found in app.html")
    html = pattern.sub(lambda _m: block, html)
    APP_HTML.write_text(html, encoding="utf-8")
    print(f"Spliced {len(pf_spots)} postflop spots into app.html")


if __name__ == "__main__":
    main()
