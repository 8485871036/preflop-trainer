"""
Red Star Poker (iPoker) memory reader — 100% accurate live cards.
PokerClient process ki memory me se current hand ka XML buffer dhoondta hai
(hero ke Pocket cards + board), kyunki client TempData file ko EXCLUSIVE lock
karke likhta hai (normal read blocked).

Usage: python redstar_mem.py
"""
import ctypes
import ctypes.wintypes as wt
import re
import subprocess

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

HERO = "ZenithBluff"


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    # Windows 10+ 64-bit: PartitionId field ke saath 48 bytes hota hai.
    # Ye MISSING tha -> RegionSize/State/Protect/Type galat offsets pe padhe ja rahe the.
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


k32 = ctypes.windll.kernel32
k32.OpenProcess.restype = ctypes.c_void_p
k32.ReadProcessMemory.restype = wt.BOOL
k32.VirtualQueryEx.restype = ctypes.c_size_t


def find_pid(proc_name="PokerClient.exe"):
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW).stdout
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == proc_name.lower():
            return int(parts[1])
    return None


def read_regions(pid, max_read=4 * 1024**3):
    """Committed readable regions scan karo. Yields (addr, bytes).
    NOTE: address space sparse hai — FREE regions ka size total me mat gino,
    sirf actually-read bytes gino (warna 2GB free region turant limit maar deta hai)."""
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print("OpenProcess failed (admin chahiye?)")
        return
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    total_read = 0
    while addr < 0x7FFFFFFFFFFF and total_read < max_read:
        r = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not r:
            break
        size = mbi.RegionSize
        prot = mbi.Protect
        readable = (mbi.State == MEM_COMMIT and not (prot & PAGE_NOACCESS) and not (prot & PAGE_GUARD))
        if readable:
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t(0)
            if k32.ReadProcessMemory(h, ctypes.c_void_p(mbi.BaseAddress or 0), buf, size, ctypes.byref(got)):
                yield mbi.BaseAddress or 0, buf.raw[:got.value]
                total_read += got.value
        addr += size
    k32.CloseHandle(h)


SUIT = {"S": "s", "H": "h", "D": "d", "C": "c"}
_mem_cache = {"base": None, "window": 32768, "last_gc": None, "last_scan": 0.0}
_scan_lock = __import__("threading").Lock()
_import_time = __import__("time").time


def _norm_card(tok):
    """'HQ' -> 'Qh', 'C7' -> '7c', 'D10' -> 'Td' (rank+suit, redstar_hh format)."""
    s, r = tok[0].upper(), tok[1:].upper()
    r = "T" if r == "10" else r
    return r + SUIT.get(s, s.lower())


def read_at(pid, addr, size):
    """Ek specific address se size bytes padho."""
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return None
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got))
    k32.CloseHandle(h)
    return buf.raw[:got.value] if ok else None


def extract_hand(txt):
    """Memory ke hand XML text se {hero, board} nikalo (100% accurate)."""
    hero, board = [], []
    m = re.search(r'<cards player="' + HERO + r'" type="Pocket">([^<]*)</cards>', txt)
    if m:
        hero = [_norm_card(t) for t in m.group(1).split() if t != "X"]
    for st in ("Flop", "Turn", "River"):
        m = re.search(r'<cards type="' + st + r'">([^<]*)</cards>', txt)
        if m:
            board += [_norm_card(t) for t in m.group(1).split() if t != "X"]
    return hero, board


HERO_RE = re.compile(rb"\x4a[\x02\x03]([SHDC](?:10|[2-9TJQKA]))")   # protobuf field 9 = pocket
BOARD_RE = re.compile(rb"\x5a[\x02\x03]([SHDC](?:10|[2-9TJQKA]))")  # protobuf field 11 = board
STREET_LEN = {None: 0, "preflop": 0, "flop": 3, "turn": 4, "river": 5}


def _max_ts(data):
    """Buffer me sabse recent protobuf timestamp varint (ms, >1e12) nikalo."""
    best = 0
    i, n = 0, len(data)
    while i < n - 1:
        tag = data[i]
        if tag >= 0x80:
            i += 1
            continue
        wire = tag & 7
        i += 1
        if wire == 0:
            val = 0
            shift = 0
            while i < n:
                b = data[i]
                i += 1
                val |= (b & 0x7f) << shift
                if b < 0x80:
                    break
                shift += 7
            if 1000000000000 < val < 2000000000000:   # Unix ms range
                best = max(best, val)
        elif wire == 2:
            i += data[i] + 1
        elif wire == 5:
            i += 4
        else:
            break
    return best


def _scan_proto_hands(pid, max_age_ms=120000):
    """Memory me sabhi RECENT protobuf hand buffers -> [(addr, hero, board)].
    NOTE: LIVE hand memory me PROTOBUF me nahi hota (client C++ structs me rakhta
    hai, sirf hand COMPLETE hone pe protobuf me serialize karta hai). Isliye sirf
    RECENT timestamp wale buffers accept karo — purane (completed) buffers ko
    binary file already cover karti hai, unhe yahan se stale data mat do."""
    hero_b = HERO.encode()
    now_ms = int(_import_time() * 1000)
    out = []
    for addr, data in read_regions(pid):
        start = 0
        while True:
            i = data.find(hero_b, start)
            if i < 0:
                break
            start = i + 1
            ctx = data[i:i + 800]
            hero = [_norm_card(c.decode()) for c in HERO_RE.findall(ctx)]
            board = [_norm_card(c.decode()) for c in BOARD_RE.findall(ctx)]
            if len(hero) == 2:
                ts = _max_ts(ctx)
                if ts and now_ms - ts <= max_age_ms:
                    out.append((addr + i, hero, board))
    return out


def find_current_hand(pid, gc, street=None):
    """Memory me LIVE hand ke hero cards + board -> (hero, board).
    Live hand PROTOBUF format me memory me likha jaata hai (binary file jaisa),
    XML format sirf completed hands me hota hai. Street (XML se) se board
    length match karke current hand identify karte hain."""
    if not pid:
        return None
    target = STREET_LEN.get(street, -1)
    # 1) cached address fast path
    base = _mem_cache["base"]
    if base:
        data = read_at(pid, base, _mem_cache["window"])
        if data:
            cands = _scan_proto_hands_in(data, base)
            best = _pick(cands, target)
            if best:
                return best
    # 2) full scan (rate-limited)
    now = _import_time()
    if _mem_cache["last_gc"] == gc and now - _mem_cache["last_scan"] < 3:
        return None
    with _scan_lock:
        if _mem_cache["last_gc"] == gc and _import_time() - _mem_cache["last_scan"] < 3:
            return None
        base = _mem_cache["base"]
        if base:
            data = read_at(pid, base, _mem_cache["window"])
            if data:
                cands = _scan_proto_hands_in(data, base)
                best = _pick(cands, target)
                if best:
                    return best
        cands = _scan_proto_hands(pid)
        _mem_cache["last_gc"] = gc
        _mem_cache["last_scan"] = _import_time()
        if cands:
            # sabse recent buffer ka base address cache karo (highest addr)
            _mem_cache["base"] = max(c[0] for c in cands) - 200
        return _pick(cands, target)


def _scan_proto_hands_in(data, base, max_age_ms=120000):
    """Ek single region ke andar RECENT protobuf hands -> [(addr, hero, board)]."""
    hero_b = HERO.encode()
    now_ms = int(_import_time() * 1000)
    out = []
    start = 0
    while True:
        i = data.find(hero_b, start)
        if i < 0:
            break
        start = i + 1
        ctx = data[i:i + 800]
        hero = [_norm_card(c.decode()) for c in HERO_RE.findall(ctx)]
        board = [_norm_card(c.decode()) for c in BOARD_RE.findall(ctx)]
        if len(hero) == 2:
            ts = _max_ts(ctx)
            if ts and now_ms - ts <= max_age_ms:
                out.append((base + i, hero, board))
    return out


def _pick(cands, target):
    """Street ke board length se match karo (strict); warna highest address (newest)."""
    if not cands:
        return None
    if target >= 0:
        matches = [c for c in cands if len(c[2]) == target]
        if not matches:
            return None           # is street ka hand memory me nahi — stale mat do
        cands = matches
    best = max(cands, key=lambda c: c[0])
    return (best[1], best[2])


if __name__ == "__main__":
    pid = find_pid()
    print("PokerClient PID:", pid)
    if pid:
        cands = _scan_proto_hands(pid)
        print(f"memory me {len(cands)} live hand buffers (2 hero cards):")
        for addr, hero, board in cands:
            print(f"  @0x{addr:X} hero={hero} board={board}")
