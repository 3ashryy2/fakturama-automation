"""
State capture for diagnosing UI failures in the failing run itself.

Every hypothesis about the second-item product selector has been tested from a
*healthy* state, where the dialog behaves. This module records what is actually
there at the moment of failure: all shells, the bound dialog's complete child
tree including windows that are not visible, and screenshots.

The invisible children matter. grounding.Scope filters to visible windows, so a
canvas that exists but is hidden looks identical to a canvas that was never
created -- and those two have completely different causes.

Enable with FAKTURAMA_DIAGNOSTICS=1; output lands in diagnostics/ at the repo
root, one text file and one or more PNGs per capture.
"""

from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path
from typing import Optional

import grounding as g

ENABLED = os.getenv("FAKTURAMA_DIAGNOSTICS", "").strip() not in ("", "0", "false")
OUTPUT = Path(__file__).resolve().parent.parent / "diagnostics"


def _all_children(hwnd: int) -> list[tuple[int, int, str, str, bool, tuple]]:
    """Every descendant window, visible or not, with depth."""
    rows: list[tuple[int, int, str, str, bool, tuple]] = []
    seen: set[int] = set()

    def walk(parent: int, depth: int) -> None:
        for child in g._children(parent):
            if child in seen:
                continue
            seen.add(child)
            rect = g._rect(child)
            rows.append((
                depth, child, g._class_name(child), g._caption(child),
                bool(g.user32.IsWindowVisible(child)),
                (rect.left, rect.top, rect.right - rect.left,
                 rect.bottom - rect.top),
            ))
            walk(child, depth + 1)

    walk(hwnd, 0)
    return rows


def capture(tag: str, dialog_title: Optional[str] = None) -> Optional[Path]:
    """Write a full description of the current UI state. Returns the report path."""
    if not ENABLED:
        return None

    OUTPUT.mkdir(exist_ok=True)
    stamp = time.strftime("%H%M%S")
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in tag)
    report = OUTPUT / f"{stamp}-{slug}.txt"

    lines: list[str] = [f"=== {tag} @ {time.strftime('%H:%M:%S')} ==="]

    # Every top-level window, not just the ones find_shells accepts, so a
    # dialog with an unexpected class or a zero rect still shows up here.
    lines.append("\n--- all visible top-level windows ---")
    tops: list[int] = []

    def visit(hwnd, _lparam):
        if g.user32.IsWindowVisible(hwnd):
            tops.append(hwnd)
        return True

    g.user32.EnumWindows(g._EnumProc(visit), 0)
    for hwnd in tops:
        caption, cls = g._caption(hwnd), g._class_name(hwnd)
        if not caption.strip() and not cls.startswith(("SWT", "#32770")):
            continue
        rect = g._rect(hwnd)
        lines.append(
            f"  {cls:<18} {caption[:48]!r:<52} "
            f"({rect.left},{rect.top}) {rect.right-rect.left}x{rect.bottom-rect.top}"
        )

    # The dialog's full subtree, including hidden windows.
    if dialog_title:
        matches = [h for h in tops
                   if dialog_title.lower() in g._caption(h).lower()
                   and g._class_name(h) == "#32770"]
        lines.append(f"\n--- '{dialog_title}' shells found: {len(matches)} ---")
        for hwnd in matches:
            rect = g._rect(hwnd)
            lines.append(
                f"\n  shell {hex(hwnd)} ({rect.left},{rect.top}) "
                f"{rect.right-rect.left}x{rect.bottom-rect.top} "
                f"visible={bool(g.user32.IsWindowVisible(hwnd))} "
                f"enabled={bool(g.user32.IsWindowEnabled(hwnd))}"
            )
            children = _all_children(hwnd)
            visible = sum(1 for c in children if c[4])
            lines.append(f"  children: {len(children)} total, {visible} visible")
            for depth, child, cls, caption, vis, box in children:
                flag = " " if vis else "H"   # H = hidden
                lines.append(
                    f"   {flag}{'  ' * depth}{cls:<16} {hex(child):>10} "
                    f"({box[0]},{box[1]}) {box[2]}x{box[3]}"
                    + (f" {caption[:28]!r}" if caption.strip() else "")
                )
            try:
                image = g._print_window(hwnd)
                shot = OUTPUT / f"{stamp}-{slug}-dialog.png"
                image.save(shot)
                lines.append(f"  screenshot: {shot.name}")
            except Exception as exc:
                lines.append(f"  screenshot failed: {exc}")

    try:
        main = g.main_window()
        image = g._print_window(main.hwnd)
        shot = OUTPUT / f"{stamp}-{slug}-main.png"
        image.save(shot)
        lines.append(f"\nmain window screenshot: {shot.name}")
    except Exception as exc:
        lines.append(f"\nmain window screenshot failed: {exc}")

    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [diag] {report.name}")
    return report
