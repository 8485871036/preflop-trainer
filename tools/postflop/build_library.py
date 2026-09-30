"""
Postflop spot library builder — har spot x ~236 representative flops (texture buckets), POORE tree
(flop+turn+river bet sizes) se solve — flop-only tree all-in bahut zyaada deta tha (galat).
Resume-able: jo file ban chuki hai use skip. Solver low priority pe (khelte waqt PC slow na ho).

Run:   python tools/postflop/build_library.py                 (saare spots, priority order)
       python tools/postflop/build_library.py srp_BTN_BB      (sirf ek spot)
Env:   LIB_THREADS (default 20), LIB_ITER (default 80)
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gto_server as g
import rangeutil
import spotlib

PRIORITY = ["srp_BTN_BB", "srp_CO_BB", "srp_SB_BB", "srp_MP_BB", "srp_UTG_BB",
            "3bp_BTN_BB", "3bp_BTN_SB", "3bp_CO_BTN", "3bp_SB_BB", "3bp_CO_BB", "3bp_CO_SB"]
THREADS = int(os.environ.get("LIB_THREADS", "6"))   # gentle default — 20 pe system hil jata tha
ITER = int(os.environ.get("LIB_ITER", "80"))


def ranges(spot):
    sp = spotlib.spots()[spot]
    ip = rangeutil.weighted_range_string(sp["ip_range"]["main"], sp["ip_range"].get("border", ""))
    oop = rangeutil.weighted_range_string(sp["oop_range"]["main"], sp["oop_range"].get("border", ""))
    return sp, ip, oop


def solve_flop(spot, canon, low_priority=True, threads=THREADS, lock=False):
    """Ek spot + canonical flop solve karke library me save. Returns nodes."""
    sp, ip, oop = ranges(spot)
    name = f"lib_{spot}_{''.join(canon)}"
    tree = g.solve(",".join(canon), sp["pot"], sp["eff"], ip, oop, name, ITER,
                   template=g.INPUT_TEMPLATE, threads=threads, low_priority=low_priority, lock=lock)
    nodes = spotlib.extract_nodes(tree)
    if nodes:
        spotlib.save(spot, canon, nodes)
    for f in (g.SOLVER_DIR / f"{name}.json", g.CACHE_DIR / f"{name}_input.txt", g.CACHE_DIR / f"{name}.log"):
        try:
            f.unlink()
        except OSError:
            pass
    return nodes


def main():
    todo = sys.argv[1:] or PRIORITY + [s for s in spotlib.spots() if s not in PRIORITY]
    flops = sorted(spotlib.subset().values())
    for spot in todo:
        done = sum(1 for f in flops if spotlib.lib_path(spot, f).exists())
        print(f"[lib] {spot}: {done}/{len(flops)} pehle se", flush=True)
        t0, n = time.time(), 0
        for canon in flops:
            if spotlib.lib_path(spot, canon).exists():
                continue
            try:
                solve_flop(spot, canon)
            except Exception as e:
                print(f"[lib] {spot} {''.join(canon)} fail: {e}", flush=True)
                continue
            n += 1
            done += 1
            if n % 10 == 0:
                per = (time.time() - t0) / n
                print(f"[lib] {spot}: {done}/{len(flops)}  {per:.1f}s/flop  "
                      f"~{(len(flops) - done) * per / 3600:.1f}h baaki", flush=True)


if __name__ == "__main__":
    main()
