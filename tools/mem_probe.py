import sys, re, ctypes, ctypes.wintypes as wt, subprocess, time
sys.path.insert(0, "tools")
from redstar_mem import find_pid, read_regions

pid = find_pid()
print("PID", pid, flush=True)
if not pid:
    sys.exit(0)

needles = {
    "hero_u8": b"ZenithBluff",
    "hero_u16": "ZenithBluff".encode("utf-16-le"),
    "pocket_u8": b"Pocket",
    "pocket_u16": "Pocket".encode("utf-16-le"),
    "flop_u16": "Flop".encode("utf-16-le"),
}
counts = {k: 0 for k in needles}
samples = {}
t0 = time.time()
for addr, data in read_regions(pid):
    if len(data) > 64 * 1024 * 1024:   # bade regions skip (XML buffer chhota hoga)
        continue
    for k, n in needles.items():
        c = data.count(n)
        if c:
            counts[k] += c
            if k not in samples:
                i = data.find(n)
                samples[k] = data[max(0, i - 30): i + 90]
print("elapsed", round(time.time() - t0, 1), "s", flush=True)
print(counts, flush=True)
for k, s in samples.items():
    print("---", k, "---", flush=True)
    print(s[:160], flush=True)
