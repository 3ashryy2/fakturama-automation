# Fakturama Image-to-Cash Automation

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
