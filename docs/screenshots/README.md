# Run evidence

Annotated captures from live runs against Fakturama 2.2.0.

All images are taken with `PrintWindow` (window-owned pixels), not a screen grab, so they are correct even when the application is partly covered — which is also how the automation verifies grids while other windows are in front.

| Image | What it shows |
|---|---|
| `02-address-selector-exact-match.png` | Step 2.2–2.3. The Order's address selector filtered to a single exact Debtor. Row geometry here is measured from the rendered separator rules — the two rules at y=75 and y=105 give exactly one row of height 30 — so the row count is deterministic and needs no OCR. |
| `03-payment-method-created.png` | Step 2.10. `Bank Transfer` created by the automation with Name and Description both set to the literal image text, while the payment *code* dropdown was set to the mapped `Credit transfer`. Keeping those two distinct is why the extractor no longer collapses them. |
| `04-product-selector-ambiguous.png` | Step 3.3's ambiguity rule firing. Three identical `CHR-ERG-01` rows from earlier manual testing; the run stops for manual review rather than guessing which to select. |
| `05-items-grid-natable.png` | The Order's Items grid — a NatTable. UIA reports no `Table`, `DataGrid` or `DataItem` node for any of this. The selected (blue) row is why row detection cannot rely on darkness alone: every scanline in a highlighted row reads as dark as a separator, so boundaries are recovered from the constant row pitch instead. |
| `06-debtor-editor.png` | The New Debtor editor. Labels sit left of their fields, and one label can own several inputs (`First Name Last Name`, `ZIP - City`) — resolved by taking the run of fields between a label and the next label on the same row. |