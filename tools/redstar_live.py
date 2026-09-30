"""
Red Star Poker (iPoker) live hand reader — 100% accurate cards (memory/file se).

Red Star client (PokerClient.exe) current hand ka XML TempData file me EXCLUSIVE
lock ke saath likhta hai — normal open "Permission denied" deta hai. Isliye hum
handle-duplication se client ka file handle apne process me le aakar padhte hain.

Run: python tools/redstar_live.py   (PokerClient chal raha ho to current hand dikhata hai)
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import struct
import time
from pathlib import Path

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
TEMP_DIR = LOCALAPPDATA / "Red Star Poker" / "data" / "ZenithBluff" / "History" / "TempData"
HERO = "ZenithBluff"

k32 = ctypes.windll.kernel32
ntdll = ctypes.windll.ntdll

ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong
ntdll.NtQuerySystemInformation.argtypes = [
    ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
    ctypes.POINTER(ctypes.c_ulong),
]

# --- structs ---
class SYSTEM_HANDLE_TABLE_ENTRY_INFO(ctypes.Structure):
    _fields_ = [
        ("UniqueProcessId", wt.USHORT),
        ("CreatorBackTraceIndex", wt.USHORT),
        ("ObjectTypeIndex", ctypes.c_ubyte),
        ("HandleAttributes", ctypes.c_ubyte),
        ("HandleValue", wt.USHORT),
        ("Object", ctypes.c_void_p),
        ("GrantedAccess", wt.ULONG),
    ]

class SYSTEM_HANDLE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NumberOfHandles", wt.ULONG),
        ("Handles", SYSTEM_HANDLE_TABLE_ENTRY_INFO * 1),
    ]


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


def _query_handles():
    """System ke saare handles nikal (NtQuerySystemInformation, class 16)."""
    info_class = 16
    size = ctypes.c_ulong(0)
    ntdll.NtQuerySystemInformation(info_class, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(max(size.value * 2, 1 << 20))
    while True:
        r = ntdll.NtQuerySystemInformation(info_class, buf, len(buf), ctypes.byref(size))
        if r == 0:
            break
        if r == 0xC0000004:   # STATUS_INFO_LENGTH_MISMATCH
            buf = ctypes.create_string_buffer(size.value)
            continue
        return []
    n = struct.unpack_from("<I", buf.raw, 0)[0]
    entries = []
    # 64-bit: NumberOfHandles(4) ke baad entries pointer-size aligned (offset 8) hoti hain
    off = ctypes.sizeof(ctypes.c_void_p)
    for _ in range(n):
        e = SYSTEM_HANDLE_TABLE_ENTRY_INFO.from_buffer_copy(buf.raw, off)
        entries.append(e)
        off += ctypes.sizeof(SYSTEM_HANDLE_TABLE_ENTRY_INFO)
        if off >= len(buf.raw):
            break
    return entries


def _handle_path(dup):
    """Duplicated handle ka full file path nikalo."""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = k32.GetFinalPathNameByHandleW(dup, buf, 1024, 0)
        if n:
            return buf.value
    except Exception:
        pass
    return ""


def _dup_handle(pid, handle_value):
    """Client ka handle duplicate karo. Returns dup handle ya None."""
    PROCESS_DUP_HANDLE = 0x0040
    src = k32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
    if not src:
        return None
    cur = k32.GetCurrentProcess()
    dup = ctypes.c_void_p()
    ok = k32.DuplicateHandle(src, handle_value, cur, ctypes.byref(dup), 0, False, 0x2)
    k32.CloseHandle(src)
    return dup if ok else None


def _read_dup(dup):
    """Dup handle se poora content padho."""
    out = bytearray()
    chunk = ctypes.create_string_buffer(1 << 16)
    read = ctypes.c_ulong(0)
    k32.SetFilePointer(dup, 0, None, 0)   # FILE_BEGIN
    while k32.ReadFile(dup, chunk, len(chunk), ctypes.byref(read), None) and read.value:
        out += chunk.raw[:read.value]
    k32.CloseHandle(dup)
    return bytes(out)


def read_live_hand():
    """Locked TempData file ka current content padho (handle dup se)."""
    pid = find_pid()
    if not pid:
        return None
    target = ""
    try:
        newest = max(TEMP_DIR.glob("*"), key=lambda p: p.stat().st_mtime)
        target = str(newest).lower()
    except Exception:
        return None
    for e in _query_handles():
        if e.UniqueProcessId != pid:
            continue
        dup = _dup_handle(pid, e.HandleValue)
        if not dup:
            continue
        path = _handle_path(dup).lower()
        if path and path.endswith(target) or (path and target and path.split("\\")[-1] == target.split("\\")[-1]):
            data = _read_dup(dup)
            return data
        k32.CloseHandle(dup)
    return None


SUIT = {"S": "s", "H": "h", "D": "d", "C": "c"}
RANKS = "23456789TJQKA"


def parse_card(tok):
    tok = tok.strip()
    if len(tok) == 2:
        s, r = tok[0].upper(), tok[1].upper()
        if s in SUIT and r in RANKS:
            return r + SUIT[s]
    return None


def parse_hand(xml):
    """XML se hero cards + board + street nikalo. {hand, board, street} ya None."""
    if not isinstance(xml, str):
        try:
            xml = xml.decode("utf-8", errors="replace")
        except Exception:
            return None
    hand, board = None, []
    # hero pocket
    m = re.search(r'<cards player="' + re.escape(HERO) + r'" type="Pocket">(.*?)</cards>', xml)
    if m:
        cards = [c for c in (parse_card(t) for t in m.group(1).split()) if c]
        if len(cards) == 2:
            hand = cards[0] + cards[1]
    for street in ("Flop", "Turn", "River"):
        m = re.search(r'<cards type="' + street + r'">(.*?)</cards>', xml)
        if m:
            cs = [c for c in (parse_card(t) for t in m.group(1).split()) if c]
            board.extend(cs)
    street = {0: None, 3: "flop", 4: "turn", 5: "river"}.get(len(board))
    return {"hand": hand, "board": board, "street": street}


if __name__ == "__main__":
    pid = find_pid()
    print("PokerClient PID:", pid)
    if not pid:
        print("client nahi chala")
    else:
        data = read_live_hand()
        if data:
            print("live TempData:", len(data), "bytes")
            print(parse_hand(data))
        else:
            print("TempData handle nahi mila (ya koi hand chal raha nahi hai)")
