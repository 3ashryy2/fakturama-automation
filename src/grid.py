"""
Reading and driving NatTable grids.

Fakturama renders every table -- the Items list, both selector dialogs, VATs,
terms of payment, Documents -- with NatTable, which paints cells onto a bare
canvas. UIA reports no Table, DataGrid or DataItem node anywhere in the
process, so there is nothing to query and nothing to click by name.

The workaround splits the problem in two:

* Geometry is measured, not guessed. Row separators are solid horizontal rules,
  so they are found by scanning the captured pixels. That yields an exact row
  count and exact row rectangles without hardcoding a row height.

* Text is read by a vision model, and only when a decision depends on it. The
  brief's matching rules ("exactly one exact row", "stop if ambiguous") need
  cell contents, and this is the one place where OCR/LLM is genuinely the
  right tool rather than a shortcut.
"""

from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv
from PIL import Image
from pydantic import BaseModel, Field

import grounding as g

load_dotenv()

VISION_MODEL = os.getenv("VISION_MODEL", "gemini-3.6-flash")

# A separator counts when this share of the sampled row is darker than the
# page background. Grid rules are solid; antialiased text never comes close.
_LINE_COVERAGE = 0.70
_DARK_THRESHOLD = 210


# ---------------------------------------------------------------------------
# Vision schema
# ---------------------------------------------------------------------------
class GridCells(BaseModel):
    """One rendered table row, transcribed left to right."""
    cells: List[str] = Field(..., description="Cell values in column order")


class GridContents(BaseModel):
    columns: List[str] = Field(..., description="Column headers, left to right")
    rows: List[GridCells] = Field(..., description="Data rows, top to bottom")


READ_PROMPT = """
Transcribe this table screenshot.

Return the column headers, then every data row in top-to-bottom order with its
cells in left-to-right order. Copy text exactly as shown, including any
trailing ellipsis where a value is visually truncated. Do not include the
header row among the data rows. If there are no data rows, return an empty list.
"""


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
@dataclass
class GridRow:
    index: int
    top: int          # relative to the canvas
    bottom: int
    cells: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)

    @property
    def center_y(self) -> int:
        return (self.top + self.bottom) // 2

    def cell(self, column: str) -> str:
        """
        Value under a named column.

        Addressing by header rather than by position matters because NatTable
        grids carry a leading unnamed row-number column and users can reorder
        columns, so a fixed index silently reads the wrong field.
        """
        wanted = column.strip().lower()
        for position, header in enumerate(self.columns):
            if header.strip().lower() == wanted:
                # Right-align cells against headers when the transcription
                # includes the unnamed row-number column but the headers do not.
                offset = len(self.cells) - len(self.columns)
                index = position + max(0, offset)
                return self.cells[index] if index < len(self.cells) else ""
        raise KeyError(f"No column named {column!r} in {self.columns}")

    @staticmethod
    def _equal(got: str, want: str) -> bool:
        got, want = got.strip(), (want or "").strip()
        if got.endswith("...") or got.endswith("…"):
            # NatTable truncates wide cells; compare against the visible stem.
            stem = got.rstrip(".…").strip()
            return want.lower().startswith(stem.lower())
        return got.lower() == want.lower()

    def matches(self, expected: dict[str, str]) -> bool:
        """True when every named column equals its expected value."""
        try:
            return all(self._equal(self.cell(col), val)
                       for col, val in expected.items())
        except KeyError:
            return False


class AmbiguousMatch(RuntimeError):
    """More than one candidate row matched -- the brief says stop here."""


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------
class Grid:
    """A NatTable canvas, addressed by pixels."""

    def __init__(self, canvas: g.Node):
        self.canvas = canvas
        self.columns: List[str] = []

    # -- capture ----------------------------------------------------------
    def snapshot(self) -> Image.Image:
        return g.capture(self.canvas)

    def wait_until_stable(self, timeout: float = 6.0, settle: float = 0.4) -> Image.Image:
        """
        Step 2.2's "wait for the list to stabilize".

        Compares consecutive captures instead of sleeping a fixed interval, so
        a slow filter is tolerated and a fast one is not paid for.
        """
        previous = self.snapshot().tobytes()
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(settle)
            current_image = self.snapshot()
            current = current_image.tobytes()
            if current == previous:
                return current_image
            previous = current
        return self.snapshot()

    # -- geometry ---------------------------------------------------------
    @staticmethod
    def _separators(image: Image.Image) -> List[int]:
        grey = image.convert("L")
        width, height = grey.size
        pixels = grey.load()
        sample = max(1, width // 400)
        span = len(range(0, width, sample))

        hits: List[int] = []
        for y in range(height):
            dark = sum(
                1 for x in range(0, width, sample)
                if pixels[x, y] < _DARK_THRESHOLD
            )
            if dark / span >= _LINE_COVERAGE:
                hits.append(y)

        # Collapse 2-3px rules into a single boundary.
        merged: List[int] = []
        for y in hits:
            if merged and y - merged[-1] <= 2:
                merged[-1] = y
            else:
                merged.append(y)

        # The canvas edge and the horizontal scrollbar both read as full-width
        # dark rules. Left in, the top edge shifts every band by the header
        # height and silently misaligns which row gets clicked.
        return [y for y in merged if 2 < y < height - 3]

    def row_bands(self, image: Optional[Image.Image] = None) -> List[GridRow]:
        """
        Row rectangles, derived from the rendered rules and then regularised.

        Reading the rules alone is not enough: a selected row is painted a
        solid highlight colour, so every one of its scanlines looks as dark as
        a separator and the row merges into its neighbour. NatTable lays rows
        out at a constant pitch, so the reliable signal is the *spacing*
        between rules rather than each individual rule. Taking the most common
        gap and stepping by it recovers boundaries that the highlight hid.
        """
        from collections import Counter

        image = image if image is not None else self.snapshot()
        lines = self._separators(image)
        if len(lines) < 2:
            return []

        gaps = [b - a for a, b in zip(lines, lines[1:]) if b - a >= 12]
        if not gaps:
            return []
        top_count = max(Counter(gaps).values())
        pitch = min(g for g, c in Counter(gaps).items() if c == top_count)

        # Start from the measured header edge, not from the first detected
        # rule. When the very first data row is the selected one, its solid
        # highlight merges into the header rule and that row disappears
        # entirely -- the run then reports an empty grid while a line sits
        # plainly visible in it.
        start = self._header_bottom(image)
        if start <= 0 or start >= lines[-1]:
            start = lines[0]
        else:
            # Re-phase onto the detected rules so bands land on real boundaries.
            while start + pitch <= lines[0] - pitch // 2:
                start += pitch

        boundaries = list(range(start, lines[-1] + 1, pitch))
        return [
            GridRow(index=i, top=boundaries[i], bottom=boundaries[i + 1])
            for i in range(len(boundaries) - 1)
        ]

    def _clean_sample_window(self, image: Image.Image) -> tuple[int, int]:
        """
        A band of rows containing column rules and nothing else.

        Prefers the first empty row; falls back to the header strip when the
        grid is full.
        """
        bands = self.row_bands(image)
        for band in bands:
            if not self._has_ink(image, band):
                return (band.top + 4, band.bottom - 3)

        bottom = self._header_bottom(image)
        if bottom > 8:
            return (2, bottom - 2)
        lines = self._separators(image)
        return (max(0, lines[0] - 26), lines[0] - 2) if lines else (0, 1)

    @staticmethod
    def _header_bottom(image: Image.Image, limit: int = 70) -> int:
        """
        The y of the header strip's lower edge.

        NatTable paints its header on a grey background while data rows are
        white (or the selection colour). Finding where that grey ends locates
        the first data row directly, instead of inferring it from separator
        rules that a highlighted row can swallow.
        """
        grey = image.convert("L")
        width, height = grey.size
        pixels = grey.load()
        step = max(1, width // 200)

        # Contiguous from the top only. Scanning the whole strip for grey rows
        # would also catch shaded cells further down and report a header three
        # times too tall.
        last = 0
        for y in range(min(height, limit)):
            row = sorted(pixels[x, y] for x in range(0, width, step))
            median = row[len(row) // 2]
            if not 150 <= median <= 235:  # left the header grey
                break
            last = y
        return last if last >= 8 else 0

    def row_count(self) -> int:
        return len(self.row_bands())

    @staticmethod
    def _has_ink(image: Image.Image, row: GridRow,
                 threshold: int = 140, minimum: int = 12) -> bool:
        """Does this band contain drawn text rather than just empty cells?"""
        grey = image.convert("L")
        width, _ = grey.size
        pixels = grey.load()
        dark = 0
        for y in range(row.top + 3, max(row.top + 4, row.bottom - 2)):
            for x in range(0, width, 2):
                if pixels[x, y] < threshold:
                    dark += 1
                    if dark >= minimum:
                        return True
        return False

    def populated_rows(self, image: Optional[Image.Image] = None) -> List[GridRow]:
        """
        Row bands that actually contain data.

        A NatTable draws its rules for empty rows too, so counting bands would
        report a fresh, empty list as having a dozen entries. Checking each band
        for ink separates "this list has one record" from "this list is empty"
        without reading any text, which keeps existence checks free of OCR.

        Scanning stops at the first blank band rather than filtering the whole
        set: rows are filled contiguously from the top, and anything with ink
        below a gap is furniture -- in practice the horizontal scrollbar, which
        otherwise counts as an extra record.
        """
        image = image if image is not None else self.snapshot()
        rows: List[GridRow] = []
        for band in self.row_bands(image):
            if not self._has_ink(image, band):
                break
            rows.append(band)
        return rows

    @staticmethod
    def _verticals(image: Image.Image, header_band: tuple[int, int]) -> List[int]:
        """Column rules, sampled across the header strip where they are solid."""
        grey = image.convert("L")
        width, _ = grey.size
        pixels = grey.load()
        top, bottom = header_band
        rows = list(range(top + 2, bottom - 1))
        if not rows:
            return []

        hits: List[int] = []
        for x in range(width):
            dark = sum(1 for y in rows if pixels[x, y] < _DARK_THRESHOLD)
            if dark / len(rows) >= _LINE_COVERAGE:
                hits.append(x)

        merged: List[int] = []
        for x in hits:
            if merged and x - merged[-1] <= 2:
                merged[-1] = x
            else:
                merged.append(x)
        return merged

    def column_bands(self, image: Optional[Image.Image] = None) -> List[tuple[int, int]]:
        """
        Column rectangles, measured from the header's vertical rules.

        Needed to edit a specific cell: NatTable exposes no cell objects, so
        reaching "the Discount cell of row 2" means computing where it is
        drawn rather than asking for it.
        """
        image = image if image is not None else self.snapshot()
        lines = self._separators(image)
        if len(lines) < 2:
            return []

        # Sample the column rules inside an *empty* row wherever one exists.
        # The header strip looks like the obvious choice but its own text
        # produces vertical strokes that read as rules, and a populated row
        # does the same; a selected row is worse still, being solid colour. An
        # empty row is clean white cells separated by exactly the rules wanted.
        window = self._clean_sample_window(image)
        verticals = self._verticals(image, window)

        # The first column's left edge is the canvas border, which is not drawn
        # as a rule and so never gets detected. Without it every band shifts by
        # one column and 'Qty.' silently addresses 'Item No.'.
        if verticals and verticals[0] > 20:
            verticals = [0] + verticals

        return [
            (verticals[i], verticals[i + 1])
            for i in range(len(verticals) - 1)
            if verticals[i + 1] - verticals[i] >= 12
        ]

    def scroll_columns(self, direction: str = "right", clicks: int = 14) -> None:
        """
        Scroll the grid horizontally using its own scrollbar buttons.

        The Items grid is wider than its viewport, so Discount and Price sit
        off-screen and cannot be clicked until it is scrolled. The scrollbar's
        'Column right' / 'Column left' buttons are named in UIA, which makes
        them the one reliable handle on this.
        """
        import uiautomation as auto

        name = "Column right" if direction == "right" else "Column left"
        g.ensure_foreground(self.canvas)
        win = g.uia_window()
        rect = None
        for control, _ in auto.WalkControl(win, maxDepth=20):
            if control.ControlTypeName == "ButtonControl" and control.Name == name:
                box = control.BoundingRectangle
                if box.width() > 0:
                    rect = box
                    break
        if rect is None:
            raise g.ControlNotFound(f"No {name!r} scroll control on the grid")

        x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        for _ in range(clicks):
            auto.Click(x, y, waitTime=0.12)
        time.sleep(0.5)

    def cell_point(self, row: GridRow, column: str,
                   image: Optional[Image.Image] = None,
                   anchor: str = "left") -> tuple[int, int]:
        """
        Screen coordinates of a named cell in a given row.

        `anchor` says which end of the header list the visible columns line up
        with. Unscrolled, the leftmost visible band is column 0. Scrolled fully
        right, the rightmost visible band is the *last* column, so positions
        are counted from the end instead.
        """
        if not self.columns:
            self.read(image)
        bands = self.column_bands(image)
        headers = list(self.columns)

        wanted = column.strip().lower()
        for index, header in enumerate(headers):
            if header.strip().lower() != wanted:
                continue
            band = index if anchor == "left" \
                else len(bands) - (len(headers) - index)
            if not 0 <= band < len(bands):
                raise KeyError(
                    f"Column {column!r} (index {index}) is not among the "
                    f"{len(bands)} columns visible with anchor {anchor!r}; "
                    "the grid needs scrolling"
                )
            left, right = bands[band]
            rect = g._rect(self.canvas.hwnd)
            return (rect.left + (left + right) // 2, rect.top + row.center_y)
        raise KeyError(f"Column {column!r} not locatable among {headers}")

    def edit_cell(self, row: GridRow, column: str, value: str,
                  commit: str = "{Enter}", anchor: str = "left") -> None:
        """
        Type a value into a grid cell.

        NatTable opens its inline editor on double-click, so the sequence is
        select, open, replace, commit. Verification is left to the caller and
        is done on the document totals rather than by re-reading the grid.
        """
        import uiautomation as auto

        g.ensure_foreground(self.canvas)
        x, y = self.cell_point(row, column, anchor=anchor)
        # Two clicks rather than a double-click helper: the first selects the
        # cell, the second opens NatTable's inline editor.
        auto.Click(x, y, waitTime=0.15)
        auto.Click(x, y, waitTime=0.25)
        auto.SendKeys("{Ctrl}a", waitTime=0.05)
        auto.SendKeys(str(value).replace("{", "{{").replace("}", "}}"), waitTime=0.02)
        if commit:
            auto.SendKeys(commit, waitTime=0.15)

    # -- contents ---------------------------------------------------------
    def read(self, image: Optional[Image.Image] = None) -> List[GridRow]:
        """Row geometry plus transcribed cells."""
        image = image if image is not None else self.wait_until_stable()
        rows = self.row_bands(image)
        if not rows:
            return []

        contents = self._transcribe(image)
        self.columns = contents.columns

        populated: List[GridRow] = []
        for row, transcribed in zip(rows, contents.rows):
            if not any(cell.strip() for cell in transcribed.cells):
                continue
            row.cells = transcribed.cells
            row.columns = contents.columns
            populated.append(row)
        # A grid draws rules for its empty rows too; only rows that actually
        # carry data are candidates for matching.
        return populated

    @staticmethod
    def _transcribe(image: Image.Image) -> GridContents:
        from google import genai

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        client = genai.Client()
        response = client.models.generate_content(
            model=VISION_MODEL,
            contents=[
                {"inline_data": {"mime_type": "image/png", "data": buffer.getvalue()}},
                READ_PROMPT,
            ],
            config={
                "response_mime_type": "application/json",
                "response_schema": GridContents,
                "temperature": 0.0,
            },
        )
        parsed: GridContents | None = response.parsed
        if parsed is None:
            raise ValueError("Vision model returned unreadable grid output")
        return parsed

    # -- interaction ------------------------------------------------------
    def click_row(self, row: GridRow, column_x: int = 60) -> None:
        """Click a row at a point derived from its measured band."""
        import uiautomation as auto

        rect = g._rect(self.canvas.hwnd)
        auto.Click(rect.left + column_x, rect.top + row.center_y, waitTime=0.15)

    def select_only_row(self, what: str = "row") -> GridRow:
        """
        Select the single remaining row after a search, or refuse.

        This is the existence check the whole flow turns on, and it runs purely
        on measured geometry: the caller has already filtered the list by the
        exact search term, so the number of rows carrying ink answers the
        question. Exactly one selects, several stop for manual review, none
        tells the caller to take the creation branch.

        Deliberately no OCR. Transcribing the cells to compare them costs a
        vision call per selection, which is what turns a run into a quota
        failure halfway through on a metered key. Exactness is instead
        confirmed by consequence: a wrongly selected Debtor fails the address
        check, a wrongly selected Product fails the totals check. Use
        select_unique when cell-level matching is genuinely required.
        """
        rows = self.populated_rows()
        if len(rows) > 1:
            raise AmbiguousMatch(
                f"{len(rows)} {what} rows matched the search; "
                "stopping for manual review"
            )
        if not rows:
            raise LookupError(f"No {what} row matched the search")

        self.click_row(rows[0])
        return rows[0]

    def select_unique(self, expected: dict[str, str]) -> GridRow:
        """
        Select the one row whose cells match `expected`, or refuse.

        Stricter than select_only_row and the closest match to the brief's
        wording, but it transcribes the grid with the vision model, so every
        call costs an API request. Reserved for cases where the search filter
        alone cannot establish exactness.
        """
        rows = self.read()
        matches = [r for r in rows if r.matches(expected)]

        if len(matches) > 1:
            raise AmbiguousMatch(
                f"{len(matches)} rows matched {expected}; stopping for manual review"
            )
        if not matches:
            raise LookupError(f"No row matched {expected} among {len(rows)} row(s)")

        self.click_row(matches[0])
        return matches[0]


# ---------------------------------------------------------------------------
# Selector dialogs
# ---------------------------------------------------------------------------
class SelectorDialog:
    """
    A modal "Select the ..." dialog.

    SWT builds these as native #32770 dialogs, so the search box and the
    OK/Cancel buttons are real Win32 controls even though the list between
    them is not.
    """

    def __init__(self, title: str, timeout: float = 10.0):
        self.title = title
        self._shell = g.find_dialog(title, timeout=timeout)
        self._grid: Optional[Grid] = None

    @property
    def shell(self) -> g.Node:
        """
        The dialog window, re-resolved by title on every access.

        Two of these open per item and SWT does not reuse the handle, so a
        shell captured once is dead the next time round. Holding a stale one
        fails silently -- its child enumeration simply comes back empty, and
        the problem surfaces much later as "no list canvas". Re-resolving is a
        single window enumeration, cheap enough to do unconditionally, and it
        removes a whole class of stale-handle bugs rather than patching them.
        """
        for shell in g.find_shells(self.title):
            if shell.cls == "#32770":
                self._shell = shell
                return shell
        return self._shell

    @property
    def scope(self) -> g.Scope:
        """Re-read the dialog's controls on every access."""
        return g.Scope(self.shell)

    @property
    def grid(self) -> Grid:
        """
        The dialog's list, re-resolved whenever its handle goes stale.

        SWT recycles child handles when the dialog re-lays out -- which it does
        while filtering -- so a canvas captured at construction can be a dead
        window by the time it is screenshotted, reporting a zero-size rect. Two
        of these dialogs open per item, so caching the handle fails on the
        second product every time. Re-resolving costs about 2ms.
        """
        if self._grid is None or not g.user32.IsWindow(self._grid.canvas.hwnd):
            # The shell exists before its contents are laid out, so on a
            # freshly reopened dialog the canvas is briefly absent entirely.
            try:
                canvas = g.wait_for(
                    lambda: next(iter(self.scope.canvases()), None),
                    timeout=8.0, interval=0.2,
                    what=f"list canvas in {self.title!r}",
                )
            except TimeoutError:
                # Report what was actually enumerated rather than just "not
                # found" -- the distinction between an empty enumeration and a
                # populated one that fails the size filter is the whole answer.
                from collections import Counter

                scope = self.scope
                kinds = Counter(n.cls for n in scope.nodes)
                swt = [(hex(n.hwnd), n.width, n.height)
                       for n in scope.nodes if n.cls == g.CANVAS_CLASS]
                raise TimeoutError(
                    f"list canvas in {self.title!r} not found. "
                    f"shell={hex(scope.root.hwnd)} "
                    f"nodes={len(scope.nodes)} kinds={dict(kinds)} "
                    f"swt_windows={swt}"
                ) from None
            self._grid = Grid(canvas)
        return self._grid

    def search(self, term: str) -> None:
        g.set_text(self.scope.field_for("Search:"), term, commit=False)
        self.grid.wait_until_stable()

    def _button(self, name: str) -> g.Node:
        """
        The dialog's native OK/Cancel button.

        These are real Win32 Buttons with captions, so they are resolved the
        same way as every other control here rather than through UIA.
        """
        wanted = name.strip().lower()
        for node in g.Scope(self.shell).nodes:
            if node.cls == "Button" and node.caption.strip().lower() == wanted:
                return node
        raise g.ControlNotFound(f"{name!r} button not found in {self.title!r}")

    def _press(self, name: str) -> None:
        # Resolve first, then raise the dialog, then click -- activation has to
        # be the last thing before the click or focus can drift in between.
        button = self._button(name)
        g.click(button)

    def ok(self) -> None:
        self._press("OK")
        self.wait_closed()

    def cancel(self) -> None:
        self._press("Cancel")
        self.wait_closed()

    def wait_closed(self, timeout: float = 8.0) -> None:
        g.wait_for(
            lambda: not g.dialog_open(self.title),
            timeout=timeout,
            what=f"{self.title!r} to close",
        )

    def __enter__(self) -> "SelectorDialog":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if g.dialog_open(self.title):
            try:
                self.cancel()
            except Exception:
                pass
