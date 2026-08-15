# Fakturama Image-to-Cash Automation

Turns a single order image into a saved, verified Order and linked Invoice in
[Fakturama 2.2](https://www.fakturama.info/download/), resolving or creating the
Debtor, payment method, VAT rate and Product master records along the way.

Extraction is done by a multimodal LLM under an enforced schema; the desktop
side is driven through Microsoft UI Automation and the native Win32 layer
beneath SWT. No screen coordinates are hardcoded anywhere.

---

## The problem this design exists to solve

Fakturama 2.2 is Eclipse RCP on SWT 3.124 with NatTable 2.4. That combination
defeats a straightforward UIA approach in two independent ways, and both were
measured against the running application rather than assumed:

**1. The form fields are invisible to UIA.** A full UIA crawl of the New Order
editor returns 1107 unnamed `PaneControl`s and only a handful of named
`EditControl`s. `Date` and `No.` have no UIA edit node at all — only a sibling
`TextControl` label. So the common recipe of "find the label, take the next
sibling" cannot work: there is no sibling to take.

**2. Every grid is a canvas.** The Items table, both selector dialogs, and the
VATs / terms-of-payment / Documents lists are all NatTable, which paints cells
onto a bare `SWT_Window0`. UIA reports **zero** `Table`, `DataGrid`, `DataItem`
or `ListItem` nodes anywhere in the process. Grid contents cannot be read
through the accessibility tree at all.

What *is* still available is the native layer. SWT builds `Text`, `Combo` and
`Label` on real Win32 controls — the process exposes 145 `Edit`, 23 `ComboBox`
and 240 `Static` handles. So the automation recovers fields by pairing a
labelled `Static` with the nearest field on the same visual row, and reads them
with an explicit `WM_GETTEXT`.

That last detail is the crux: `GetWindowText` deliberately does **not** cross
the process boundary for caption-less controls — it returns `""` rather than
sending the message. Sending `WM_GETTEXT` explicitly is the difference between
empty strings and real data, and it is what makes per-step verification
possible.

### Three layers

| Layer | Covers | Mechanism |
|---|---|---|
| Toolbar, menus, navigation | `Create: New Order`, `Save`, `New product`, follow-up `Invoice` | UIA — this part is exposed cleanly |
| Form fields | Date, Cust.Ref., Company, Street, ZIP, prices, combos | `grounding.py` — Win32 label→field by geometry |
| Grids | Items, both selectors, VATs, Documents | Act blind, verify by consequence (below) |

### Verification by consequence, not by appearance

Because grid contents are unreadable, the automation avoids needing to read
them. Row *geometry* is measured deterministically from the rendered pixels
(separator rules give an exact row count and exact row rectangles), and
correctness is confirmed from the document's own native fields — if every line
landed correctly then `Total Net` must equal the recomputed source total, and if
any line is wrong it cannot.

Concretely, selecting a Debtor or Product works like this: type the exact search
term into the dialog's search box, then count the rows that carry ink. Empty
rows still have their separator rules drawn, so "has ink" is what distinguishes
one record from an empty list — and that count alone answers the brief's rule.
One row selects it, several stop for manual review, none takes the creation
branch. Exactness is then confirmed by consequence: a wrongly selected Debtor
fails the address check, a wrongly selected Product fails the totals check.

**A normal run therefore makes zero vision API calls.** This was a deliberate
correction after an earlier version transcribed each grid with the vision model:
that costs a request per selection, is nondeterministic, and on the Gemini free
tier's 20 requests/day it failed a run partway through with `RESOURCE_EXHAUSTED`.
`Grid.select_unique` keeps the stricter cell-matching path for cases where a
search filter genuinely cannot establish exactness; nothing in the default flow
calls it.

### On not hardcoding coordinates

The *procedure* is hardcoded — the order of operations is fixed and belongs in
readable linear code. What is resolved at runtime is only *where each control
currently is*. That distinction matters concretely here:

- The display runs at 150% scaling: the same window measures 1000×600 logical
  and 1500×900 physical. A coordinate captured on one machine is off by 50% on
  another. `grounding.py` opts into per-monitor DPI awareness so window rects,
  screenshots and the cursor all share one coordinate space.
- The main window's rectangle changed between two probes in a single session.
- Names are not stable either: the product-selector control has **no name** at
  all (it is an unnamed `ImageControl`), so it is grounded structurally as
  "the first icon below the `Items` anchor".

Runtime resolution is not the expensive part — a full Win32 sweep of 97 controls
takes ~2.5 ms and a label→field lookup ~0.02 ms. The one slow path is the UIA
walk behind the selector icons at ~600 ms, so that result is cached per editor
and invalidated whenever an editor opens or closes.

---

## Setup

Requires **Windows** (UIA + Win32), **Python 3.11+**, and Fakturama 2.2
installed at `C:\Program Files\Fakturama2`.

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt

copy .env.example .env      # then add your key
```

`.env`:

```
GOOGLE_API_KEY=your_key_here
EXTRACTION_MODEL=gemini-3.6-flash    # optional
```

### Fakturama workspace

Point Fakturama at this repo directory as its workspace. The application writes
its database, logs and templates here; none of that is committed.

The automation needs exactly one record it is never asked to create: a shipping
method. Fakturama seeds this during its own first-run setup, and **an Order
editor will not open on a database with no shipping rows** — it raises a
shipping error instead. Everything else (VAT rate, payment method, Debtor,
Products) is created by the automation during a run.

**Creating the clean baseline once**, so runs are reproducible:

1. Start Fakturama on this workspace and let it initialise.
2. Open **Data → Shippings**, add a method named `Free of shipping costs`
   with value `0.00`, and save.
3. Quit Fakturama so HSQLDB checkpoints, then copy `Database/` to
   `Database.seed/`.

`Database.seed/` is deliberately not committed — it is generated local state,
not source. Once it exists:

```bash
venv\Scripts\python tools\reset_app.py --wipe-db
```

That quits Fakturama, restores `Database/` from the baseline, and restarts. Use
it between demo runs: Fakturama refuses to close an editor with unsaved changes
(`Ctrl+W` raises a "Save Parts" prompt whose Cancel merely aborts the close), so
restarting is the reliable way to discard a half-built Order.

Run `tools\reset_app.py` without `--wipe-db` to restart the application while
keeping the current data.

---

## Running

```bash
# full run: extract the image, then drive Fakturama
venv\Scripts\python src\main.py

# a different order image
venv\Scripts\python src\main.py --image path\to\order.png

# reuse a saved extraction (no API call) -- what the demos below use
venv\Scripts\python src\main.py --cached tests\test_order.extracted.json

# extraction only, no UI
venv\Scripts\python src\main.py --extract-only --save-extraction out.json

# show the planned UI sequence without touching the app
venv\Scripts\python src\main.py --dry-run
```

Leave the machine alone while a run is in progress: it drives the real mouse and
keyboard.

Each stage is guarded. A step that cannot be completed is recorded and the run
stops with a readable summary of what was done, created and skipped, rather than
a traceback — which is also how the "known gaps" below are produced.

---

## Repository layout

```
src/
  models.py        Pydantic schema + all deterministic arithmetic
  extractor.py     image -> SalesOrder via multimodal LLM, schema-enforced
  grounding.py     Win32/UIA control discovery, input, capture   <- the core
  grid.py          NatTable geometry, selector dialogs, pluggable cell reader
  ui_driver.py     one method per step of the brief
  state_machine.py legal orderings, incl. master-data detours
  diagnostics.py   state capture for debugging a failing run (opt-in)
  main.py          CLI entry point and stage sequencing
tools/
  reset_app.py     restart Fakturama / restore the clean database baseline
tests/
  test_order.PNG              the supplied order image
  test_order.extracted.json   cached extraction, for offline UI runs
docs/
  design_document.md          Part 1 deliverable
  screenshots/                annotated run evidence
```

---

## Design notes worth calling out

**The LLM transcribes; Python calculates.** The prompt asks for verbatim
transcription only. Every derived number — product master gross price, line
totals, document totals — is recomputed in `models.py`, and an extraction whose
arithmetic does not reconcile against the printed totals is rejected before any
UI action happens.

**The source string survives normalization.** Fakturama needs the payment method
under two different names: the literal image text (`Bank Transfer`) becomes the
payment record's Name/Description, while a mapped code (`Credit transfer`)
drives the payment-code dropdown. Collapsing them at extraction time loses
information the UI still needs.

**Dates are a picker, not a text box.** The `Date` control is segmented: typing
its own displayed format back into it (`Jul 14, 2026`) scrambles to
`Aug 20, 0026`, because each keystroke goes to whichever segment has focus. A
delimited numeric date (`07/14/2026`) fills all three segments cleanly. Since
segment order is locale-dependent, the driver tries several orderings and
verifies by *parsing the field back to a date* rather than string-matching.

**Fakturama reformats on commit,** so verification is semantic: `0.00` comes back
as `$0.00`, `0%` as `0.00%`. Comparing raw strings would flag every money field
as a failed write and retype it repeatedly.

**Editors must be allowed to settle.** Eclipse creates an editor's controls and
then moves them as layout resolves. A snapshot taken too early pairs a label
with whichever field is momentarily beside it — during development this put an
order date into the address box. The driver waits for two identical consecutive
layout signatures before binding any handle.

---

## Status against the brief

| Brief steps | Status |
|---|---|
| 1.1–1.2 Extract and normalize the image | Done — reconciles against printed totals |
| 1.3–1.8 Open New Order; No., Date, Cust.Ref., Net, With VAT | Done, verified by read-back |
| 2.1–2.3 Select Debtor from the Order; exact-match / ambiguity / none | Done |
| 2.4 Verify populated Invoice + Delivery addresses | Done |
| 2.5–2.9 Create Debtor, main address, roles, Miscellaneous | Done |
| 2.10 Payment method: select, else create with mapped code | Done |
| 2.11–2.13 Save Debtor, re-select from the Order | Implemented |
| 3.1–3.3 Product selector as the existence check | Done |
| 3.4–3.6 VAT verification / creation | Done — creates `VAT 19%` |
| 3.7–3.12 Product creation with calculated gross price | Implemented |
| 3.13–3.17 Line qty / price / discount, per item | Done for the first item — qty, price and discount write, confirmed by the Order's Total Net moving to the expected value |
| 4.1–4.7 Verify defaults and totals, save, follow-up Invoice | Wired |
| 5.1–5.7 Invoice payment method, paid status, save, verify | Wired |

"Wired" means written, connected into `main.py`'s stage sequence, and reviewed
against the brief — but not yet reached by a live run, because the flow stops
earlier (see below). "Done" means observed working against the running
application.

## Tests

The pure layer runs offline in under five seconds, with Fakturama closed:

```bash
venv\Scripts\python -m pytest tests\test_offline.py -q     # 27 passed
```

It covers the money arithmetic and totals reconciliation, the payment-method /
payment-code split, date candidate ordering, the semantic read-back comparison,
and NatTable row detection against the captured screenshots in
`docs/screenshots/` — including the two cases that broke naive detection: a list
whose empty rows still draw separator rules, and a grid containing a selected
(highlighted) row.

## Known gaps

These are real and deliberately not papered over:

- **The end-to-end run is not green in one unattended pass.** Stages 1, 2 and
  the first item of stage 3 complete reliably from a clean database: the
  payment method, Debtor with both addresses, VAT rate and both Products are
  all created, and line 1 is filled and confirmed. The run then stops
  reopening the product selector for the **second** item, so stages 4 and 5 —
  though wired and reviewed — have not been reached by a live run. `main.py`
  always reports the exact stopping point.

  The failure is specific and reproducible: after the second Product is saved
  and its editor closed, reopening `Select a product` yields a dialog whose
  list canvas never appears, and it survives retries, a settle-wait on the
  Order editor, and re-resolving both the shell and the canvas. Clicking the
  same icon by hand at that exact moment works, which points at workbench
  state rather than at the locator. This is the one thing I would finish first.

- **Runs are disrupted by any mouse or keyboard use.** The automation drives
  real input, so touching the machine mid-run corrupts it — one such
  interruption produced a scrambled date before the priming click was added.
- **Step 2.4 cannot verify the Delivery address on the Order.** Fakturama
  renders only an `Invoice address` tab there, so there is no Delivery tab to
  read, even though the delivery address is correctly created on the Debtor and
  persisted (confirmed in the database). The run reports this rather than
  failing on a tab the application never draws.
- **Debtor selection disambiguates by billing ZIP, not by full cell matching.**
  The address selector lists addresses rather than debtors, so a Debtor with
  separate invoice and delivery addresses returns two rows for one company;
  narrowing by ZIP picks the invoice address without an OCR call. A different
  debtor that happened to share both company name and ZIP would not be
  distinguished — `Grid.select_unique` is the stricter path for that.
- **`VAT code (E-Invoice)` is not asserted** when reusing an existing VAT row
  (step 3.5 wants `S (Standard rate)` confirmed). It is left at its default on
  creation, which is correct, but a pre-existing row with a conflicting code
  would not be caught.
- **Only tested against Fakturama 2.2.0, en-US, at 150% DPI.** The locale
  fallbacks for date segment order exist but are untested on another locale.

---

## If I had 3 more hours

1. **Unblock the second-item product selector, then run stages 4–5 for real.**
   Everything downstream is written and connected; it is one reproducible
   workbench-state bug standing between here and a full run. My next step would
   be to stop inferring and instrument it directly — dump the dialog's child
   tree and a screenshot at the instant it is bound, in the failing run rather
   than from a healthy state — since every hypothesis so far (stale handle,
   stale canvas, unsettled Order tab, lingering dialog) has been cheap to test
   and wrong. Each earlier bug turned out to be a small, specific UI behaviour
   rather than a design problem — a segmented date picker, a combo that keeps
   keyboard focus, an editor that caches its dropdown, a highlighted row that
   reads as a separator — and I would expect the same character of fix here.

2. **Use the HSQLDB file as an independent verification oracle.** The workspace
   database is plain-text SQL containing `FKT_DOCUMENT`, `FKT_DOCUMENTITEM`,
   `FKT_PRODUCT`, `FKT_CONTACT` and `FKT_ADDRESS`. Asserting the saved records
   against it after a run would confirm what was actually *persisted* rather
   than what the UI displayed — genuinely independent evidence, which is the one
   thing UI-level verification structurally cannot give you. It already paid off
   during development: it is how I confirmed the delivery address really had
   been saved when the Order refused to show a Delivery tab.

3. **Replace the optional vision path with local OCR.** Windows ships
   `Windows.Media.Ocr`, and the reader in `grid.py` is already behind an
   interface, so this is a swap rather than a rewrite. The default flow makes no
   vision calls today, but the strict cell-matching path still would; local OCR
   would make strict matching free and deterministic, which in turn would let
   Debtor selection match on full cell values instead of narrowing by ZIP.

4. **Widen the extraction tests.** The arithmetic is well covered offline, but
   every test uses one order image. I would add fixtures for the cases that
   break real invoices — multi-page item tables, mixed VAT rates in one order,
   an unpaid order, a missing contact name — and assert that reconciliation
   rejects the malformed ones *before* any UI action happens, since that
   boundary is what stops a bad extraction from writing a wrong order.
