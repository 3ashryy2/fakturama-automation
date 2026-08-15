"""
Restart Fakturama into a known-clean UI state.

Fakturama refuses to close an editor with unsaved changes (Ctrl+W raises a
"Save Parts" prompt whose Cancel just aborts the close), so the reliable way to
discard a half-built Order or Debtor between demo runs is to quit the
application and start it again. Quitting discards unsaved editors without
writing them to the database.

    python tools/reset_app.py             # quit and restart
    python tools/reset_app.py --quit      # quit only
    python tools/reset_app.py --wipe-db   # also restore the clean baseline

--wipe-db restores Database/ from Database.seed/, a snapshot holding only the
one record Fakturama needs but the brief never asks the automation to create:
the "Free of shipping costs" shipping method. Every other master record -- VAT,
payment method, Debtor, Products -- starts absent, so a run exercises all of
the creation branches. Without it, an Order editor cannot even open: Fakturama
raises a shipping error on a database with no shipping rows.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import grounding as g  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXE = Path(r"C:\Program Files\Fakturama2\Fakturama.exe")
WM_CLOSE = 0x0010


def wipe_db() -> None:
    """Restore the workspace database from the clean baseline snapshot."""
    seed, live = ROOT / "Database.seed", ROOT / "Database"
    if not seed.is_dir():
        raise FileNotFoundError(
            f"No baseline at {seed}. Create one by starting Fakturama on an "
            "empty workspace, adding a 'Free of shipping costs' shipping "
            "method, quitting, and copying Database/ to Database.seed/."
        )
    # Windows keeps the database files locked for a moment after the JVM exits,
    # so a copy attempted immediately raises WinError 32. Retry rather than
    # abort: failing here silently leaves the previous run's records in place
    # and the next run reports reusing records it was supposed to create.
    last: Exception | None = None
    for attempt in range(12):
        try:
            if live.is_dir():
                shutil.rmtree(live)
            shutil.copytree(seed, live)
            (live / "Database.lck").unlink(missing_ok=True)
            print(f"[reset] database restored from {seed.name}")
            return
        except (PermissionError, OSError) as exc:
            last = exc
            time.sleep(1.0)
    raise RuntimeError(f"Could not restore the database after 12 attempts: {last}")


def running() -> bool:
    try:
        g.main_window()
        return True
    except g.ControlNotFound:
        return False


def quit_app(timeout: float = 90.0) -> None:
    if not running():
        print("[reset] Fakturama is not running")
        return

    window = g.main_window()
    g.ensure_foreground(window)
    g.user32.PostMessageW(window.hwnd, WM_CLOSE, 0, 0)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        # Only the quit confirmation is answered. A "Save Parts" prompt is
        # deliberately left alone: its OK would persist the half-built records
        # this reset exists to discard. Quitting drops unsaved editors anyway.
        for shell in g.find_shells():
            if shell.cls != "#32770" or "quit" not in shell.caption.lower():
                continue
            for node in g.Scope(shell).nodes:
                if node.cls == "Button" and node.caption.strip().lower() in ("ok", "yes"):
                    g.click(node)
                    time.sleep(0.5)
                    break
        if not running():
            print("[reset] Fakturama closed")
            time.sleep(2.0)  # let HSQLDB finish its checkpoint
            return
    raise TimeoutError("Fakturama did not close")


def start_app(timeout: float = 90.0) -> None:
    if not EXE.exists():
        raise FileNotFoundError(f"Fakturama not found at {EXE}")
    subprocess.Popen([str(EXE)], cwd=str(EXE.parent))

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2.0)
        if running():
            time.sleep(3.0)  # let the workbench finish laying out
            print("[reset] Fakturama ready")
            return
    raise TimeoutError("Fakturama did not start")


def main() -> int:
    parser = argparse.ArgumentParser(description="Restart Fakturama cleanly")
    parser.add_argument("--quit", action="store_true", help="quit without restarting")
    parser.add_argument("--wipe-db", action="store_true",
                        help="restore Database/ from the clean baseline")
    args = parser.parse_args()

    quit_app()
    if args.wipe_db:
        wipe_db()
    if not args.quit:
        start_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
