# System Design Document: Fakturama Image-to-Cash Automation

**Candidate:** Mohanad Emad
**Email:** mohanad200130@gmail.com
**Phone:** +201144111251

**Revision 2** — the original document was written before implementation. This
version keeps that design where it held up and revises it where contact with
Fakturama 2.2.0 proved it wrong. Sections marked **Revised** changed as a direct
result of what the application turned out to do; every number quoted here was
measured against the running program rather than estimated.

---

## 1. Executive Summary

The system converts a single order image into a fully persisted Order and linked
Invoice inside the Fakturama desktop application. To avoid brittle,
coordinate-based automation it uses a decoupled, state-driven architecture,
separating unstructured data processing (an LLM) from desktop UI interaction,
and sequencing them through an explicit state machine.

The original design assumed Microsoft UI Automation would be sufficient for the
UI half. It is not. Fakturama 2.2.0 is Eclipse RCP on SWT 3.124 with NatTable
2.4.0, and that combination hides both its form fields and its grids from the
accessibility tree. The central design decision in this revision is therefore a
**three-layer grounding strategy**: UIA where it works, the native Win32 layer
underneath SWT where it does not, and measured pixels for the grids that have no
programmatic representation at all.

The guiding principle throughout is that **the procedure is fixed and belongs in
readable code, while the location of every control is resolved at run time.**
Hardcoding the sequence of steps is correct — that sequence is the business
process. Hardcoding where a control sits is what the brief rules out, and what
breaks the moment DPI, window size, or Eclipse's layout pass changes.

---

## 2. Image-Extraction & Data Normalization Strategy

The pipeline bypasses OCR templates, which fail under minor layout variation, in
favour of a multimodal LLM (Google Gemini) constrained by a strict response
schema.

### 2.1 Multimodal Parsing with Enforced Schema

- **Direct image consumption.** The raw order image goes to the vision model, so
  spatial reasoning over nested tables needs no intermediate bounding-box work.
- **Pydantic validation.** The extraction layer defines a schema mapping exactly
  onto the Fakturama entities (Order, Debtor, Address, Product, Line Item), and
  passes it to the SDK's `response_schema` parameter.

### 2.2 Deterministic Calculation Boundaries

Floating-point arithmetic is deliberately moved out of the model and into the
Python validation layer. The product master gross price
(`unit_net_price × (1 + vat/100)`) and every line and document total are
recomputed in `models.py`, giving deterministic rounding before any UI action.

### 2.3 The prompt transcribes; it does not normalise — **Revised**

The original design had the LLM perform in-flight normalisation, including
mapping `"Bank Transfer"` to Fakturama's terminology. Implementation showed this
to be actively wrong: Fakturama needs the payment method under **two different
names simultaneously**. The payment record's Name and Description must be the
literal printed text (`Bank Transfer`), while a separate payment-code dropdown
must be set to the mapped accounting term (`Credit transfer`). Normalising at
extraction time destroyed the original string, leaving the record impossible to
create correctly.

The prompt now asks for verbatim transcription only, and the mapping lives in
`PAYMENT_CODE_MAP` as a derived property. The general rule this produced:
**the LLM reports what the document says; the application layer decides what
that means.**

### 2.4 Extraction is rejected before it can do damage — **New**

`SalesOrder.reconcile()` recomputes every line total and all three document
totals and compares them against the printed values. A mismatch beyond two cents
aborts the run *before* the UI is touched. This boundary matters more than any
UI safeguard: a plausible-looking but wrong extraction is the one failure mode
that would otherwise write a confidently incorrect order into the accounts.

---

## 3. Control-Discovery & Grounding Strategy — **Substantially revised**

### 3.1 What UIA actually exposes

A full UIA crawl of the New Order editor returns:

| Control type | Count |
|---|---|
| `PaneControl` (unnamed) | 1107 |
| `EditControl` | 18 |
| `CheckBoxControl` | 2 |
| `Table` / `DataGrid` / `DataItem` / `ListItem` | **0** |

Two consequences follow, and they drove the rest of the design.

**Form fields are unreachable.** `Date` and `No.` have no UIA edit node at all —
only a sibling `TextControl` label. The common idiom of finding a label and
taking its next sibling cannot work, because there is no sibling to take. The
original design's "locate an Edit field by finding the Text label preceding it"
was exactly this idiom, and it fails here.

**Grids have no contents.** The Items table, both selector dialogs, and the
VATs / terms-of-payment / Documents lists are all NatTable, which paints cells
onto a bare canvas. There is nothing to query and nothing to click by name.

### 3.2 The native layer beneath SWT

SWT builds its `Text`, `Combo` and `Label` widgets on **real Win32 controls**.
The process exposes 145 `Edit`, 23 `ComboBox` and 240 `Static` handles. Fields
are therefore recovered by pairing a labelled `Static` with the nearest field on
the same visual row, bounded by the next label to the right — which is what
lets one label own several inputs, as `First Name Last Name` and `ZIP - City` do.

One detail makes this work at all: `GetWindowText` deliberately does **not**
cross the process boundary for caption-less controls, returning `""` rather than
sending the message. `WM_GETTEXT` must be sent explicitly. That single
distinction is the difference between empty strings and real data, and it is
what makes per-step verification possible.

Rectangles come from each control's own `HWND` via `GetWindowRect` at call time.
Nothing is hardcoded: if Fakturama moves a field, its handle moves with it.

### 3.3 DPI awareness is a correctness requirement, not a nicety

On the 150 %-scaled test display, a DPI-unaware process sees the main window as
1000 × 600 while the screen holds 1500 × 900 physical pixels. Window rectangles,
screenshots and the mouse would disagree by 50 %, and every click would land in
the wrong place. `grounding.py` opts into per-monitor DPI awareness before any
rectangle is read, putting all three in one coordinate space.

### 3.4 Structural grounding for unnamed controls

The address and product selector icons carry **no name** — they are unnamed
`ImageControl`s, so no name-based locator can ever find them. (This is why the
original `ButtonControl(Name="Select a product")` silently matched nothing, and
execution fell through to a blind pixel-offset click that typed a SKU into the
Invoice address box.) They are instead grounded *structurally*: find the section
header, then take the icons below it in document order. Index 0 is "select an
existing record"; index 1 is the green "create new" the brief forbids here.

### 3.5 Asynchronous state polling — extended

Explicit polling replaced fixed sleeps as originally planned, but two additional
forms of waiting proved necessary:

- **Layout settling.** Eclipse creates an editor's controls and then *moves*
  them as layout resolves. A snapshot taken too early pairs a label with
  whichever field is momentarily beside it. During development this put an order
  date into the address textarea. The driver now waits for two identical
  consecutive layout signatures before binding any handle.
- **Focus verification.** `SetForegroundWindow` is advisory; Windows refuses it
  when the caller does not own the foreground, and returns without raising. The
  switch is confirmed, with `AttachThreadInput` as a fallback. Without this,
  clicking Cancel on an unfocused modal dialog does nothing at all.

### 3.6 Cost of grounding

| Operation | Cost | Frequency |
|---|---|---|
| Full Win32 sweep (97 controls) | 2.5 ms | per editor |
| Label → field lookup | 0.02 ms | per field |
| UIA walk for selector icons | 608 ms | cached per editor |

Runtime resolution is effectively free; only the UIA walk is expensive, so that
one result is cached and invalidated whenever an editor opens or closes. This is
the empirical answer to the objection that runtime resolution is too slow to
prefer over a precomputed coordinate table.

---

## 4. Grid Interaction Without Accessibility — **New section**

No part of the original design anticipated that grids would be entirely opaque.
This is the single largest addition.

### 4.1 Geometry is measured, not guessed

Row separators are solid horizontal rules, found by scanning captured pixels.
Three refinements were forced by real rendering behaviour:

- **A selected row is painted a solid highlight**, so every one of its scanlines
  reads as dark as a separator and the row merges into its neighbour. NatTable
  lays rows out at a constant pitch, so the reliable signal is the *spacing*
  between rules, not each rule.
- **When the selected row is the first row** it merges into the header and
  disappears completely — reported as an empty grid while a line sits plainly
  visible in it. The header's grey background is therefore detected directly,
  contiguously from the top, and used as the first boundary.
- **Empty rows still have their rules drawn**, so counting bands reports a fresh
  list as full. Rows are counted by whether they carry *ink*, scanning from the
  top and stopping at the first blank — anything with ink below a gap is
  furniture, in practice the horizontal scrollbar.

Column boundaries are sampled inside an **empty** row, because header text and
populated cells both produce vertical strokes that read as column rules. The
canvas's left edge is added explicitly, since it is not drawn and its absence
shifts every column by one — silently addressing `Item No.` when asked for
`Qty.`.

### 4.2 Screenshots come from the window, not the screen

Captures use `PrintWindow` with `PW_RENDERFULLCONTENT` rather than a screen
grab, so verification keeps working when Fakturama is partly obscured. A screen
grab makes every check hostage to whatever else is on the desktop.

### 4.3 Verification by consequence — the key idea

Because grid contents cannot be read, the design avoids needing to read them.
Existence checks run on the filtered row count, which is deterministic and free.
Correctness is then confirmed from the document's **own native fields**: if
every line landed correctly, `Total Net` must equal the recomputed source total,
and if any line is wrong it cannot. Setting a quantity to 2 on a €250 line was
confirmed by `Total Net` moving to `$500.00` — without reading a single cell.

This replaced an earlier design in which the vision model transcribed each grid.
That version cost an API call per selection, was nondeterministic, and failed a
run partway through with `RESOURCE_EXHAUSTED` against the Gemini free tier's
limit of 20 requests per day. **A normal run now makes zero vision calls.** The
stricter cell-matching path is retained behind an interface for cases where a
search filter genuinely cannot establish exactness.

---

## 5. Orchestration & State Management

Procedural logic is governed by an explicit state machine. Its value is not
performing the work but making the legal orderings explicit and refusing illegal
ones. The property worth encoding is that resolving missing master data is a
**detour**: creating a Debtor, payment method, VAT rate or Product suspends the
Order and must return to it. A detour that never returns raises immediately,
rather than quietly leaving a half-built Order.

| Current state | Trigger | Target state |
|---|---|---|
| `extracting` | reconciliation succeeds | `order_open` |
| `order_open` | New Order editor ready | `debtor_pending` |
| `debtor_pending` | exact match selected | `debtor_ready` |
| `debtor_pending` | no match; context switch | `debtor_creating` |
| `debtor_creating` | Debtor saved | `debtor_pending` (re-select) |
| `items_pending` | SKU missing | VAT check → Product creation |
| `order_complete` | totals verified, saved | `invoice_open` |
| any working state | ambiguity or failed verification | `halted` |

### 5.1 Ordering constraints discovered in implementation — **New**

Two dependencies are not obvious from the UI and were found only by running it:

- **The Debtor editor caches its Payment combo.** A payment method created while
  that editor is open saves correctly but remains unselectable — the combo
  reports no items at all. The payment method must therefore be resolved
  *before* the Debtor editor opens. The brief already imposes the same
  constraint at step 3.7 for VAT before New product; it applies here too.
- **Navigation-panel links are single-click actions.** Clicking twice opens two
  editors, and the automation then fills one while the other sits behind it —
  so the record that gets saved is the empty one.

---

## 6. Architectural Tradeoffs & Mitigations

- **LLM latency vs OCR speed.** Passing images to an external API costs 5–15 s,
  accepted because it guarantees normalised structured text and removes the
  overhead of maintaining regex rules for dynamic invoices. Revised in one
  respect: this tradeoff is worth making for the *order image*, which has
  arbitrary layout, and not for *grid screenshots*, which are crisp,
  high-contrast tables where a metered, nondeterministic call buys nothing.
- **State machine vs procedural scripting.** More upfront setup than nested
  conditionals, repaid by a self-documenting orchestration layer that cannot
  loop indefinitely during context switching.
- **Runtime grounding vs a precomputed coordinate map.** Higher complexity than
  a table of offsets, and the measurements in §3.6 show the runtime cost is
  negligible. The alternative fails on DPI scaling, window resizing, and any
  control without a stable name — which here includes the two selector icons the
  flow depends on most.
- **Stopping vs guessing.** Every ambiguity halts with a report rather than
  choosing. This is what the brief asks for, and it means a database containing
  duplicate SKUs stops the run — correct, but it makes clean master data a
  precondition for an unattended run.
- **Real keystrokes vs `WM_SETTEXT`.** Synthesising text directly is faster but
  risks updating the visible control without updating JFace's bound model — a
  silent corruption surfacing only after save. Real input is slower and correct.

### 6.1 Application behaviours that shaped the implementation

Recorded because they are not discoverable from documentation:

| Behaviour | Consequence |
|---|---|
| `Date` is a segmented picker | Typing its own displayed format (`Jul 14, 2026`) scrambles it to `Aug 20, 0026`; a delimited numeric (`07/14/2026`) fills all segments. Verification parses the field back to a date rather than comparing strings, so segment order can vary by locale. |
| Combos keep keyboard focus after selection | The next field's text leaks into the combo as type-ahead. Typing "Invoice add**ress**" set the country to *San Marino*. Focus is now moved off with `Tab`. |
| Fakturama reformats on commit | `0.00` → `$0.00`, `0%` → `0.00%`. Literal comparison flags every money field as a failed write and retypes it three times; comparison is semantic. |
| First click after a context switch is consumed | It activates the editor rather than hitting the target, so the first write silently goes nowhere. Writes retry when a field reads back empty. |
| The Order shows only an `Invoice address` tab | The Delivery address cannot be verified there even when correctly saved on the Debtor — confirmed present in the database. |
| Payment-code label ships untranslated | It appears as the literal key `!editorPaymentPaymentcode!`, so it is addressed as the editor's only combo instead. |
| A database with no shipping rows | Blocks the Order editor entirely; a baseline snapshot supplies the one seeded record the automation is never asked to create. |

---

## 7. Implementation Status

Honest position at hand-in. "Done" means observed working against the running
application; "wired" means written, connected and reviewed but not yet reached
by a live run.

| Stage | Status |
|---|---|
| 1. Extract, open Order, Date / Cust.Ref. / Net / With VAT | **Done**, every value verified by read-back |
| 2. Debtor: select, create, addresses, roles, payment method, re-select | **Done** from a clean database |
| 3. Product: selector check, VAT create/reuse, Product create, line 1 | **Done** for the first item |
| 3. Second item | **Blocked** — see below |
| 4. Verify defaults and totals, save, follow-up Invoice | **Wired** |
| 5. Invoice payment method, paid status, save, verify in Documents | **Wired** |

A clean run creates the payment method, the Debtor with both addresses, the VAT
rate and both Products, and completes line 1 — then stops reopening the product
selector for the second item. Instrumentation captured the dialog's full child
tree at the moment of failure and compared it against a successful bind: the two
are **structurally identical** — same 18 children, same 16 visible, same
rectangles. The dialog is healthy, which rules out the stale-handle,
unsettled-workbench and lingering-dialog hypotheses and points at the
enumeration in the automation's own scope layer. That is where I would resume.

Offline, 28 tests cover the arithmetic, the payment-code split, date candidate
ordering, semantic read-back comparison, and NatTable row detection against
captured screenshots — including the two cases that defeated naive detection.

---

## 8. Written Questionnaire: Future Scope

**If I had 3 more hours, what would I do for this task?**

1. **Finish the second-item selector and run stages 4–5 for real.** The
   instrumentation has already narrowed this from "the dialog is broken" to "the
   dialog is fine and my enumeration disagrees", which is a much smaller
   problem. Everything downstream is written and connected.
2. **Use the HSQLDB file as an independent verification oracle.** The workspace
   database is plain-text SQL containing `FKT_DOCUMENT`, `FKT_DOCUMENTITEM`,
   `FKT_PRODUCT`, `FKT_CONTACT` and `FKT_ADDRESS`. Asserting saved records
   against it would confirm what was actually *persisted* rather than what the
   UI displayed — evidence UI-level checking structurally cannot provide. It
   already proved its worth during development: it is how I confirmed the
   delivery address really had been saved when the Order refused to show a
   Delivery tab.
3. **Replace the optional vision path with local OCR.** Windows ships
   `Windows.Media.Ocr`, and the reader sits behind an interface, so this is a
   swap rather than a rewrite. It would make strict cell matching free and
   deterministic, which in turn would let Debtor selection match on full cell
   values instead of narrowing by ZIP.
4. **Widen the extraction fixtures.** The arithmetic is well covered, but every
   test uses one image. Multi-page item tables, mixed VAT rates, an unpaid
   order, and a missing contact name are the cases that break real invoices, and
   the assertion that matters is that reconciliation rejects the malformed ones
   *before* any UI action happens.
5. **Multi-page table stitching**, carried over from the original plan: extend
   the schema and prompt to handle carry-over totals so no line items are lost
   to pagination.
