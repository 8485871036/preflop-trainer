"""
Shared helpers for the desktop wrapper (main.py) and the HUD overlay (hud.py).
Alag module isliye — frozen exe me entry script `__main__` hota hai, `main` module
import nahi hota, to hud.py `from main import ...` nahi kar sakta.
"""
import os
import socket
import sys
import traceback
from pathlib import Path


def resource_path(relative: str) -> Path:
    # Frozen (PyInstaller) build bundles assets next to the exe; in dev mode
    # they're the repo's own root files (one level up from this script).
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def repo_root() -> Path:
    # Solver + GTO cache repo me hi rehte hain (exe me bundle nahi, 200MB+).
    # Exe desktop/dist/ me hota hai, isliye teen level upar repo root.
    if os.environ.get("PREFLOP_ROOT"):
        return Path(os.environ["PREFLOP_ROOT"])
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent.parent
    return Path(__file__).resolve().parent.parent


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def remote_gto() -> str:
    """PREFLOP_GTO_URL env — remote solver ka base URL (jaise http://192.168.1.5:8675), ya "".
    Set ho to local gto_server nahi chalta (stealth: solver dusri machine pe)."""
    return os.environ.get("PREFLOP_GTO_URL", "").strip()


def start_servers():
    """Mirror (8676) + GTO (8675) server isi process me background threads me chalao.
    Koi pehle se chal raha ho to use hi use karo. Fail ho to app phir bhi khule.
    PREFLOP_GTO_URL set ho to local GTO server skip (solver remote pe chal raha hai)."""
    root = repo_root()
    os.environ.setdefault("PREFLOP_ROOT", str(root))
    sys.path[:0] = [str(root), str(root / "tools"), str(root / "tools" / "postflop")]
    servers = ["mirror_server"]
    if not remote_gto():
        servers.append("gto_server")
    started = []
    for name in servers:
        try:
            mod = __import__(name)
            if port_busy(mod.PORT):
                continue
            if mod.start_background() is not None:
                started.append(name)
        except Exception:
            log = Path(sys.executable if getattr(sys, "frozen", False) else __file__).with_name("PreflopTrainer.log")
            try:
                with open(log, "a", encoding="utf-8") as f:
                    f.write(f"--- {name} start failed ---\n{traceback.format_exc()}\n")
            except OSError:
                pass
    return started
