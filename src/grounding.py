"""
Win32-level control grounding for Fakturama (Eclipse RCP / SWT).

Why this layer exists
---------------------
Fakturama 2.2.0 ships SWT 3.124 + NatTable 2.4.0. That combination breaks the
naive UIA approach in two specific ways:

1. Form fields are invisible to UIA. A full UIA crawl of the New Order editor
   returns 1107 unnamed PaneControls and only a handful of named EditControls.
   The Date and No. fields have *no* UIA edit node at all -- only a sibling
   TextControl label. This is why `TextControl(Name="Date").GetNextSiblingControl()`
   times out: there is no sibling to get.

2. Every grid is a canvas. The Items table, the "Select the address" and
   "Select a product" dialogs, and the VATs / terms-of-payment / Documents lists
   are all NatTable, which paints cells onto a plain SWT_Window0 canvas. UIA
   reports zero Table/DataGrid/DataItem/ListItem nodes anywhere in the process,
   so grid contents cannot be read through the accessibility tree at all.

What is still available
-----------------------
SWT builds its Text, Combo and Label widgets on *native* Win32 controls. The
process exposes 145 `Edit`, 23 `ComboBox` and 240 `Static` handles. So fields
are recovered by pairing a labelled `Static` with the nearest field handle on
the same visual row, and read with an explicit WM_GETTEXT.

Note on coordinates: rectangles here are queried from each control's own HWND at
runtime via GetWindowRect. Nothing is hardcoded and nothing assumes a fixed
layout, resolution or DPI -- if Fakturama moves a field, the handle moves with it.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import uiautomation as auto

user32 = ctypes.windll.user32

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C

FIELD_CLASSES = ("Edit", "ComboBox")
CANVAS_CLASS = "SWT_Window0"

# SWT shells use SWT_Window0; its modal dialogs ("Select the address",
# "Select a product") are native #32770 dialog boxes instead.
SHELL_CLASSES = ("SWT_Window0", "#32770")

_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _ensure_dpi_aware() -> None:
    """
    Opt into per-monitor DPI awareness before any rectangle is read.

    This matters more than it looks. On a 150%-scaled display a DPI-unaware
    process sees the main window as 1000x600 while the screen really holds
    1500x900 physical pixels, so GetWindowRect and the mouse would disagree by
    50% and every click would land in the wrong place. Making the process
    DPI-aware puts window rects, screenshots and cursor coordinates in one
    space.
    """
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


_ensure_dpi_aware()


# ---------------------------------------------------------------------------
# Raw Win32 helpers
# ---------------------------------------------------------------------------
def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _caption(hwnd: int) -> str:
    """Window caption. Works cross-process for Static labels and shell titles."""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(hwnd, buf, length + 2)
    return buf.value


def _control_text(hwnd: int) -> str:
    """
    Read an Edit/ComboBox value from another process.

    GetWindowText deliberately does not cross the process boundary for controls
    without a caption -- it returns "" rather than sending WM_GETTEXT. The
    message has to be sent explicitly, which is what makes field verification
    possible here.
    """
    length = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.SendMessageW(hwnd, WM_GETTEXT, length + 2, ctypes.byref(buf))
    return buf.value


def _rect(hwnd: int) -> wintypes.RECT:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def _children(hwnd: int) -> list[int]:
    out: list[int] = []

    def collect(child, _lparam):
        out.append(child)
        return True

    user32.EnumChildWindows(hwnd, _EnumProc(collect), 0)
    return out


# ---------------------------------------------------------------------------
# Node model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    hwnd: int
    cls: str
    rect: wintypes.RECT
    caption: str

    @property
    def center(self) -> tuple[int, int]:
        return (
            (self.rect.left + self.rect.right) // 2,
            (self.rect.top + self.rect.bottom) // 2,
        )

    @property
    def mid_y(self) -> int:
        return (self.rect.top + self.rect.bottom) // 2

    @property
    def width(self) -> int:
        return self.rect.right - self.rect.left

    @property
    def height(self) -> int:
        return self.rect.bottom - self.rect.top

    def text(self) -> str:
        """Current value, read live from the control."""
        return _control_text(self.hwnd)

    def __repr__(self) -> str:
        return f"<Node {self.cls} {hex(self.hwnd)} {self.caption[:24]!r}>"


class ControlNotFound(LookupError):
    """Raised when a label or field cannot be grounded."""


# ---------------------------------------------------------------------------
# Shell discovery
# ---------------------------------------------------------------------------
def find_shells(title_contains: str = "") -> list[Node]:
    """
    Visible top-level SWT shells, outermost first.

    Fakturama's modal dialogs ("Select the address", "Select a product") are
    separate top-level shells, not children of the main window, so dialog
    handling starts here rather than by descending the main window.
    """
    shells: list[Node] = []

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = _class_name(hwnd)
        if cls not in SHELL_CLASSES:
            return True
        cap = _caption(hwnd)
        if title_contains and title_contains.lower() not in cap.lower():
            return True
        shells.append(Node(hwnd, cls, _rect(hwnd), cap))
        return True

    user32.EnumWindows(_EnumProc(visit), 0)
    return shells


def main_window() -> Node:
    shells = [s for s in find_shells("Fakturama") if s.cls == "SWT_Window0"]
    if not shells:
        raise ControlNotFound("Fakturama main window not found. Is the app running?")
    # The main window is the largest Fakturama-titled shell; dialogs are smaller.
    return max(shells, key=lambda n: n.width * n.height)


def find_dialog(title: str, timeout: float = 10.0) -> Node:
    """Wait for a modal dialog with the given title and return its shell."""
    def look():
        for shell in find_shells(title):
            if shell.cls == "#32770":
                return shell
        return None

    return wait_for(look, timeout=timeout, what=f"dialog {title!r}")


def dialog_open(title: str) -> bool:
    return any(s.cls == "#32770" for s in find_shells(title))


# ---------------------------------------------------------------------------
# Scope: a snapshot of one shell's live control tree
# ---------------------------------------------------------------------------
class Scope:
    """
    A point-in-time snapshot of the native controls inside one shell.

    Re-snapshot after any action that changes the UI -- handles are recycled
    aggressively by SWT when editors open and close.
    """

    def __init__(self, root: Node):
        self.root = root
        self.nodes: list[Node] = []
        self.refresh()

    def refresh(self) -> "Scope":
        seen: set[int] = {self.root.hwnd}
        nodes: list[Node] = []

        stack = _children(self.root.hwnd)
        for hwnd in stack:
            if hwnd in seen:
                continue
            seen.add(hwnd)
            if not user32.IsWindowVisible(hwnd):
                continue
            nodes.append(Node(hwnd, _class_name(hwnd), _rect(hwnd), _caption(hwnd)))

        self.nodes = nodes
        return self

    # -- accessors ---------------------------------------------------------
    @property
    def labels(self) -> list[Node]:
        return [n for n in self.nodes if n.cls == "Static" and n.caption.strip()]

    @property
    def fields(self) -> list[Node]:
        return [n for n in self.nodes if n.cls in FIELD_CLASSES]

    def label(self, text: str, exact: bool = True) -> Node:
        want = text.strip().lower()
        matches = [
            n for n in self.labels
            if (n.caption.strip().lower() == want if exact
                else want in n.caption.strip().lower())
        ]
        if not matches:
            raise ControlNotFound(f"No Static label matching {text!r}")
        # Topmost, then leftmost -- stable when a label repeats in a form.
        return sorted(matches, key=lambda n: (n.rect.top, n.rect.left))[0]

    def _anchor(self, label_text: str, occurrence: int = 0) -> Node:
        want = label_text.strip().lower()
        candidates = sorted(
            (n for n in self.labels if n.caption.strip().lower() == want),
            key=lambda n: (n.rect.top, n.rect.left),
        )
        if not candidates:
            raise ControlNotFound(f"No Static label matching {label_text!r}")
        if occurrence >= len(candidates):
            raise ControlNotFound(
                f"Label {label_text!r} has {len(candidates)} occurrence(s); "
                f"index {occurrence} requested"
            )
        return candidates[occurrence]

    def fields_for(
        self,
        label_text: str,
        occurrence: int = 0,
        row_tolerance: int = 12,
    ) -> list[Node]:
        """
        Every field belonging to a label, left to right.

        One label can own several inputs -- the Debtor editor pairs
        "First Name Last Name" with two boxes and "ZIP - City" with two more.
        The run is terminated by the next label on the same row, which is what
        stops "ZIP - City" from swallowing the Telefax box further right.
        """
        anchor = self._anchor(label_text, occurrence)

        def on_row(node: Node) -> bool:
            return abs(node.mid_y - anchor.mid_y) < row_tolerance

        later_labels = [
            n for n in self.labels
            if on_row(n) and n.rect.left > anchor.rect.left and n is not anchor
        ]
        boundary = min(
            (n.rect.left for n in later_labels), default=None
        )

        run = [
            f for f in self.fields
            if on_row(f)
            and f.rect.left >= anchor.rect.right - 4
            and (boundary is None or f.rect.left < boundary)
        ]
        return sorted(run, key=lambda f: f.rect.left)

    def field_for(
        self,
        label_text: str,
        occurrence: int = 0,
        nth: int = 0,
        max_gap: int = 260,
        row_tolerance: int = 12,
    ) -> Node:
        """
        The input control belonging to `label_text`.

        `occurrence` picks between repeated labels (the Order editor has two
        "VAT" labels: the mode combo and the total). `nth` picks within a
        single label's run of fields.
        """
        run = self.fields_for(label_text, occurrence, row_tolerance)
        if not run:
            raise ControlNotFound(f"No field on the same row as {label_text!r}")
        if nth >= len(run):
            raise ControlNotFound(
                f"Label {label_text!r} owns {len(run)} field(s); "
                f"index {nth} requested"
            )

        anchor = self._anchor(label_text, occurrence)
        chosen = run[nth]
        if nth == 0 and chosen.rect.left - anchor.rect.right > max_gap:
            raise ControlNotFound(
                f"Nearest field to {label_text!r} is "
                f"{chosen.rect.left - anchor.rect.right}px away (>{max_gap}px); "
                "refusing to guess"
            )
        return chosen

    def field_with_value(self, values: Iterable[str]) -> Node:
        """
        A field identified by what it currently contains.

        The Order editor's price-mode combo carries no label of its own, so the
        only stable handle on it is that it holds 'Net' or 'Gross'.
        """
        wanted = {v.strip().lower() for v in values}
        for node in sorted(self.fields, key=lambda n: (n.rect.top, n.rect.left)):
            if node.text().strip().lower() in wanted:
                return node
        raise ControlNotFound(f"No field currently holding one of {sorted(wanted)}")

    def canvases(self, min_width: int = 200, min_height: int = 60) -> list[Node]:
        """
        Candidate NatTable regions, largest first.

        These have no readable contents -- the rectangles are handed to the
        screenshot/OCR path so grid state can still be verified.
        """
        found = [
            n for n in self.nodes
            if n.cls == CANVAS_CLASS
            and n.width >= min_width
            and n.height >= min_height
        ]
        return sorted(found, key=lambda n: n.width * n.height, reverse=True)

    def signature(self) -> tuple:
        """
        A fingerprint of the current layout.

        Eclipse builds an editor progressively: controls are created, then
        moved as the layout resolves. A snapshot taken mid-render pairs labels
        with whichever field happens to be beside them at that instant, which
        silently binds the wrong handle. Comparing signatures detects when the
        layout has stopped moving.
        """
        return tuple(
            (n.hwnd, n.rect.left, n.rect.top, n.rect.right, n.rect.bottom)
            for n in sorted(self.labels + self.fields,
                            key=lambda x: (x.rect.top, x.rect.left, x.hwnd))
        )

    def dump(self, kinds: Iterable[str] = ("Static", "Edit", "ComboBox")) -> str:
        lines = []
        for n in sorted(self.nodes, key=lambda x: (x.rect.top, x.rect.left)):
            if n.cls not in kinds:
                continue
            value = n.text() if n.cls in FIELD_CLASSES else n.caption
            if not value.strip():
                continue
            lines.append(
                f"{n.cls:<9} {hex(n.hwnd):>9}  "
                f"({n.rect.left:>5},{n.rect.top:>5})  {value[:48]!r}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Input primitives
# ---------------------------------------------------------------------------
def activate(window: Node) -> None:
    """Bring a shell to the foreground so keystrokes land in it."""
    if user32.IsIconic(window.hwnd):
        user32.ShowWindow(window.hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(window.hwnd)
    time.sleep(0.15)


def ensure_foreground(node: Node) -> None:
    """
    Raise the shell that owns `node` before interacting with it.

    Synthesised mouse and keyboard input goes to whatever is focused, not to
    whatever we aimed at, so a background window silently swallows every
    action. Modal dialogs make this acute: clicking Cancel on an unfocused
    dialog does nothing at all.
    """
    root = _top_level(node.hwnd)
    if user32.IsIconic(root):
        user32.ShowWindow(root, 9)  # SW_RESTORE

    # SetForegroundWindow is advisory: Windows refuses it when the caller does
    # not own the current foreground, and it returns without raising. Confirm
    # the switch actually happened, and fall back to AttachThreadInput -- which
    # borrows the foreground thread's input queue -- before giving up.
    for attempt in range(4):
        if user32.GetForegroundWindow() == root:
            return
        user32.SetForegroundWindow(root)
        time.sleep(0.12)
        if user32.GetForegroundWindow() == root:
            return
        if attempt == 1:
            current = user32.GetForegroundWindow()
            ours = ctypes.windll.kernel32.GetCurrentThreadId()
            theirs = user32.GetWindowThreadProcessId(current, None)
            if theirs and theirs != ours:
                user32.AttachThreadInput(ours, theirs, True)
                try:
                    user32.SetForegroundWindow(root)
                    user32.BringWindowToTop(root)
                finally:
                    user32.AttachThreadInput(ours, theirs, False)
                time.sleep(0.12)
    time.sleep(0.1)


def click(node: Node, dx: int = 0, dy: int = 0) -> None:
    """
    Click a control at a point derived from its own live rectangle.

    Every coordinate here is queried from the HWND at call time, so this
    survives window moves, resizes and DPI changes -- unlike a fixed offset
    measured against a screenshot.
    """
    ensure_foreground(node)
    x, y = node.center
    auto.Click(x + dx, y + dy, waitTime=0.05)


def set_text(node: Node, value: str, commit: bool = True, retries: int = 1) -> str:
    """
    Type `value` into a field and return what the control actually holds.

    Real keystrokes are used rather than WM_SETTEXT. SWT wires its widgets to
    JFace data binding through native change notifications, and synthesising
    the text directly risks updating the visible control without updating the
    bound model -- a silent corruption that would only surface after save.

    A retry fires only when the field came back *empty* after writing
    something, which is the signature of a keystroke that never landed.
    Retrying on any difference would be wrong: Fakturama reformats on commit
    ('0.00' becomes '$0.00', '0%' becomes '0.00%'), so a value-based check here
    would retype every money field several times. Semantic verification is the
    caller's job -- see ui_driver.values_match.
    """
    text = "" if value is None else str(value)
    for attempt in range(retries + 1):
        click(node)
        auto.SendKeys("{Ctrl}a", waitTime=0.03)
        auto.SendKeys("{Delete}", waitTime=0.03)
        if text:
            # '{' and '}' are SendKeys metacharacters and need escaping.
            auto.SendKeys(text.replace("{", "{{").replace("}", "}}"), waitTime=0.01)
        if commit:
            auto.SendKeys("{Tab}", waitTime=0.05)
        time.sleep(0.15)

        actual = _control_text(node.hwnd)
        if actual.strip() or not text.strip():
            return actual
        if attempt < retries:
            time.sleep(0.3)
    return _control_text(node.hwnd)


def _commit_combo(node: Node) -> None:
    """
    Commit a combo selection and move focus off it.

    A native Combo keeps keyboard focus after selection and keeps interpreting
    keystrokes as type-ahead. Leaving focus there is actively dangerous: the
    next field's text leaks into the combo one character at a time and silently
    rewrites it -- typing "Invoice address" into the following field ended with
    the country set to San Marino, because the trailing 's' jumped the list.

    Tab commits and moves on. Enter does not reliably commit here, and Escape
    reverts the selection outright.
    """
    auto.SendKeys("{Tab}", waitTime=0.08)
    time.sleep(0.15)


def select_combo(node: Node, value: str, retries: int = 2) -> str:
    """
    Choose `value` in a combo by typing it and confirming the read-back.

    SWT combos are native, so type-ahead selects the matching entry. Verifying
    afterwards distinguishes a real selection from a partial type-ahead match,
    and the popup is always collapsed before returning.
    """
    for attempt in range(retries + 1):
        click(node)
        auto.SendKeys("{Ctrl}a", waitTime=0.03)
        auto.SendKeys(value, waitTime=0.02)
        time.sleep(0.2)
        actual = _control_text(node.hwnd)
        if actual.strip().lower() == value.strip().lower():
            _commit_combo(node)
            return _control_text(node.hwnd)

        # Fall back to walking the list with the keyboard.
        click(node)
        auto.SendKeys("{Home}", waitTime=0.05)
        for _ in range(40):
            if _control_text(node.hwnd).strip().lower() == value.strip().lower():
                _commit_combo(node)
                return _control_text(node.hwnd)
            auto.SendKeys("{Down}", waitTime=0.03)
        _commit_combo(node)
        if attempt < retries:
            time.sleep(0.25)
    return _control_text(node.hwnd)


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.25, what: str = ""):
    """
    Poll `predicate` until it returns something truthy.

    Used instead of fixed sleeps so the flow tracks Fakturama's actual
    rendering latency rather than a guessed worst case.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"Timed out after {timeout}s waiting for {what or predicate!r}")


# ---------------------------------------------------------------------------
# UIA bridge: anchors and icon strips
# ---------------------------------------------------------------------------
# Section headers ("Addresses", "Items") are SWT CLabels drawn on a canvas, so
# they have no native HWND -- but UIA does surface them, along with the small
# selector icons beside them. Those icons carry no name, so they are grounded
# structurally: find the header, then take the icons that sit below it in
# document order. Step 2.1 wants the *upper* icon; the lower one creates a new
# record, which the brief explicitly forbids here.
def uia_window(timeout: float = 5.0):
    auto.SetGlobalSearchTimeout(timeout)
    win = auto.WindowControl(searchDepth=1, RegexName=".*Fakturama.*")
    if not win.Exists(timeout, 1):
        raise ControlNotFound("Fakturama window not visible to UIA")
    return win


def _uia_rects(max_depth: int = 40):
    """One pass over the UIA tree, collecting anchors and icons with rects."""
    win = uia_window()
    anchors: dict[str, tuple[int, int, int, int]] = {}
    icons: list[tuple[int, int, int, int]] = []
    for control, _depth in auto.WalkControl(win, maxDepth=max_depth):
        kind = control.ControlTypeName
        rect = control.BoundingRectangle
        if rect.width() <= 0:
            continue
        box = (rect.left, rect.top, rect.right, rect.bottom)
        if kind == "ImageControl":
            icons.append(box)
        elif kind == "TextControl" and control.Name:
            anchors.setdefault(control.Name.strip(), box)
    return anchors, icons


def selector_icon(anchor_name: str, index: int = 0, max_dx: int = 200):
    """
    The `index`-th selector icon beneath a section header, top to bottom.

    index=0 is the "select an existing record" icon; index=1 is the green
    "create new" icon that steps 2.1 and 3.2 tell us to avoid.
    """
    anchors, icons = _uia_rects()
    if anchor_name not in anchors:
        raise ControlNotFound(f"No UIA anchor label named {anchor_name!r}")
    left, top, right, bottom = anchors[anchor_name]

    below = [
        box for box in icons
        if box[1] >= top - 4 and box[0] >= left - 40 and box[0] - left <= max_dx
    ]
    below.sort(key=lambda b: (b[1], b[0]))
    if index >= len(below):
        raise ControlNotFound(
            f"Anchor {anchor_name!r} has {len(below)} icon(s); index {index} requested"
        )
    box = below[index]
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


def click_selector_icon(anchor_name: str, index: int = 0) -> None:
    ensure_foreground(main_window())
    x, y = selector_icon(anchor_name, index)
    auto.Click(x, y, waitTime=0.1)


def toolbar_button(name: str, timeout: float = 5.0):
    """
    A named toolbar control.

    The global toolbar is the one part of Fakturama that UIA exposes cleanly,
    so it stays on UIA rather than being reimplemented on Win32.
    """
    win = uia_window(timeout)
    for ctl in (win.ButtonControl(searchDepth=6, Name=name),
                win.SplitButtonControl(searchDepth=6, Name=name)):
        if ctl.Exists(timeout, 0.2):
            return ctl
    raise ControlNotFound(f"No toolbar control named {name!r}")


def click_toolbar(name: str) -> None:
    button = toolbar_button(name)
    ensure_foreground(main_window())
    button.Click(waitTime=0.1)


def click_navigation(name: str, timeout: float = 5.0) -> None:
    """
    Open an entry in the left Navigation View, e.g. 'VATs', 'New Contact'.

    Single click, deliberately. These entries are action links, not tree nodes:
    clicking twice opens the editor twice. That is harmless for list views,
    which are singletons, but 'New Contact' and 'New product' each yield a
    second editor -- and the automation then fills one while the other sits
    behind it, so the record that gets saved is the empty one.
    """
    ensure_foreground(main_window())
    win = uia_window(timeout)
    item = win.TextControl(searchDepth=12, Name=name)
    if not item.Exists(timeout, 0.2):
        raise ControlNotFound(f"No navigation entry named {name!r}")
    rect = item.BoundingRectangle
    auto.Click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2,
               waitTime=0.2)


# ---------------------------------------------------------------------------
# Canvas capture (NatTable verification path)
# ---------------------------------------------------------------------------
def _print_window(hwnd: int):
    """
    Render a window's own pixels via PrintWindow.

    Grabbing the screen would work only while Fakturama is unobscured, which
    makes every capture hostage to whatever else is on the desktop. PrintWindow
    asks the window to redraw itself into an off-screen bitmap instead, so
    verification keeps working when the app is partly covered.
    PW_RENDERFULLCONTENT (0x2) is required for the GDI-drawn NatTable canvases.
    """
    from PIL import Image

    gdi32 = ctypes.windll.gdi32
    # Handles are pointer-sized; without explicit prototypes ctypes truncates
    # them to a C int and 64-bit DCs overflow.
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
    ]

    rect = _rect(hwnd)
    width, height = rect.right - rect.left, rect.bottom - rect.top

    # A window caught mid-open or mid-close briefly reports no size. Give it a
    # moment rather than failing the whole run on a transient rect.
    deadline = time.time() + 2.0
    while (width <= 0 or height <= 0) and time.time() < deadline:
        time.sleep(0.15)
        rect = _rect(hwnd)
        width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise ValueError(f"Window {hex(hwnd)} has no drawable area")

    window_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, mem_dc, 0x2):
            raise OSError("PrintWindow failed")

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # negative => top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0

        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer,
                        ctypes.byref(header), 0)
        return Image.frombuffer(
            "RGB", (width, height), buffer, "raw", "BGRX", 0, 1
        )
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)


def _top_level(hwnd: int) -> int:
    GA_ROOT = 2
    return user32.GetAncestor(hwnd, GA_ROOT) or hwnd


def capture(node: Node, path: str | None = None):
    """
    Image of a control region, taken from the window rather than the screen.

    NatTable grids have no accessible contents, so looking at them is the only
    way to verify what they show. The result feeds the reader in grid.py.
    """
    root = _top_level(node.hwnd)
    shot = _print_window(root)

    root_rect = _rect(root)
    node_rect = _rect(node.hwnd)
    box = (
        max(0, node_rect.left - root_rect.left),
        max(0, node_rect.top - root_rect.top),
        min(shot.width, node_rect.right - root_rect.left),
        min(shot.height, node_rect.bottom - root_rect.top),
    )
    image = shot.crop(box) if box[2] > box[0] and box[3] > box[1] else shot
    if path:
        image.save(path)
    return image


# ---------------------------------------------------------------------------
# Read-only self test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    win = main_window()
    print(f"Main window: {win.caption}\n")

    scope = Scope(win)
    print(f"{len(scope.nodes)} visible native controls "
          f"({len(scope.labels)} labels, {len(scope.fields)} fields)\n")

    print("--- grounded fields ---")
    for name in ("No.", "Date", "Cust.Ref.", "Consultant",
                 "VAT", "Discount", "Shipping", "Total Gross", "Total"):
        try:
            node = scope.field_for(name)
            print(f"  {name:<14} {node.cls:<9} {hex(node.hwnd):>9}  {node.text()!r}")
        except ControlNotFound as exc:
            print(f"  {name:<14} -- {exc}")

    print("\n--- NatTable canvases (OCR targets) ---")
    for c in scope.canvases()[:5]:
        print(f"  {hex(c.hwnd):>9}  {c.width:>4}x{c.height:<4} "
              f"at ({c.rect.left},{c.rect.top})")

    print("\n--- open dialogs ---")
    for shell in find_shells():
        if shell.hwnd != win.hwnd:
            print(f"  {shell.caption!r}")
