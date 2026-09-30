"""Verify turn (4-card) and river (5-card) solves: time + dump size."""
import subprocess, json, time
from pathlib import Path
from rangeutil import weighted_range_string as wrs

ROOT = Path(__file__).resolve().parent.parent.parent
SOLVER_DIR = ROOT / "tools" / "postflop" / "solver" / "extracted" / "TexasSolver-v0.2.0-Windows"
SOLVER_EXE = SOLVER_DIR / "console_solver.exe"
WORK = ROOT / "tools" / "postflop" / "work"

OPEN_BTN = "22+, A2s+, K2s+, Q2s+, J4s+, T6s+, 95s, 96s, 97s, 98s, 84s, 85s, 86s, 87s, 74s, 75s, 76s, 63s, 64s, 65s, 53s, 54s, 43s, A2o+, K9o+, QTo+, JTo"
OPEN_BTN_B = "J2s, J3s, T5s, 94s, 83s, 73s, 62s, 52s, 42s, 32s, K7o, K8o, Q8o, Q9o, J8o, J9o, T8o, T9o, 98o, 87o"
BB_CALL = "22-66, A6s-A8s, K2s-K9s, Q2s-Q9s, J2s-J8s, T6s-T8s, 95s, 96s, 97s, 84s, 85s, 86s, 74s, 75s, 63s, 64s, 53s, A2o-A9o, K9o-KQo, QTo, QJo, JTo"
BB_CALL_B = "T5s, 94s, 83s, 73s, 62s, 52s, 43s, K8o, K7o, Q9o, J9o, T9o, 98o"
IP = wrs(OPEN_BTN, OPEN_BTN_B)
OOP = wrs(BB_CALL, BB_CALL_B)

TEMPLATE = """set_pot {pot}
set_effective_stack 97
set_board {board}
set_range_ip {ip}
set_range_oop {oop}
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
set_thread_num 4
set_accuracy 0.01
set_max_iteration {max_iter}
set_print_interval 10
set_use_isomorphism 1
start_solve
set_dump_rounds 1
dump_result {out}
"""

def run(board, pot, max_iter, tag):
    out_name = f"verify_{tag}.json"
    inp = WORK / f"verify_{tag}_in.txt"
    inp.write_text(TEMPLATE.format(pot=pot, board=board, ip=IP, oop=OOP, max_iter=max_iter, out=out_name), encoding="utf-8")
    out_path = SOLVER_DIR / out_name
    out_path.unlink(missing_ok=True)
    t0 = time.time()
    with open(WORK / f"verify_{tag}.log", "w") as lf:
        subprocess.run([str(SOLVER_EXE), "-i", str(inp)], cwd=str(SOLVER_DIR), stdout=lf, stderr=subprocess.STDOUT,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    dt = time.time() - t0
    if not out_path.exists():
        print(f"[{tag}] FAILED in {dt:.0f}s"); return None
    size = out_path.stat().st_size
    print(f"[{tag}] board={board} iter={max_iter} -> {size/1e6:.2f} MB in {dt:.0f}s")
    return out_path

if __name__ == "__main__":
    p = run("As,7d,2c,3c", 12, 40, "turn")
    if p:
        d = json.loads(p.read_text(encoding="utf-8"))
        print("  turn root player:", d.get("player"), "actions:", d.get("actions"))
        print("  root childrens:", list(d.get("childrens", {}).keys()))
    p2 = run("As,7d,2c,3c,Kd", 24, 40, "river")
    if p2:
        d = json.loads(p2.read_text(encoding="utf-8"))
        print("  river root player:", d.get("player"), "actions:", d.get("actions"))
