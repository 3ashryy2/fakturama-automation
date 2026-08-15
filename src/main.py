"""
Entry point: one continuous Order-first run from an order image.

Usage:
    python src/main.py                       # extract, then drive Fakturama
    python src/main.py --image path/to.png   # a different order image
    python src/main.py --cached f.json       # reuse a saved extraction
    python src/main.py --extract-only        # no UI, just print the extraction
    python src/main.py --dry-run             # extract + report the planned steps

The flow deliberately does not abort the process on the first UI failure. Each
stage is guarded so that a step which cannot be completed is recorded and the
run stops with a readable summary of what was done, what was created, and what
was left -- which is more useful than a traceback both for a demo and for the
"note anything you skipped" requirement.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python src/main.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extractor import extract_sales_order, load_cached  # noqa: E402
from models import SalesOrder  # noqa: E402
from state_machine import FakturamaFlow  # noqa: E402
from ui_driver import FakturamaDriver, ManualReview, RunReport  # noqa: E402

DEFAULT_IMAGE = Path(__file__).resolve().parent.parent / "tests" / "test_order.PNG"


def describe(order: SalesOrder) -> None:
    print("\n--- extracted order -------------------------------------------")
    print(f"  reference   : {order.external_reference}")
    print(f"  order date  : {order.order_date}  (UI: {order.ui_order_date})")
    print(f"  debtor      : {order.company} / "
          f"{order.contact_first_name} {order.contact_last_name}")
    print(f"  billing     : {order.billing_address.street}, "
          f"{order.billing_address.zip_code} {order.billing_address.city}")
    print(f"  delivery    : {order.delivery_address.street}, "
          f"{order.delivery_address.zip_code} {order.delivery_address.city}"
          f"  (same as billing: {order.delivery_same_as_billing})")
    print(f"  payment     : {order.payment_info.method} "
          f"-> code {order.payment_info.payment_code}; "
          f"paid={order.payment_info.is_paid} on {order.payment_info.paid_on}")
    for item in order.items:
        print(f"  item        : {item.sku:<14} qty {item.quantity:g} "
              f"@ {item.unit_net_price:.2f} -{item.discount:g}% "
              f"{item.vat_name}  master gross {item.master_gross_price:.2f}  "
              f"line net {item.line_net_total:.2f}")
    print(f"  totals      : net {order.computed_net:.2f} / "
          f"vat {order.computed_vat:.2f} / gross {order.computed_gross:.2f}")
    print("---------------------------------------------------------------\n")


class _Halt(Exception):
    """Internal signal: a stage failed and the run should unwind cleanly."""


def run(order: SalesOrder) -> RunReport:
    report = RunReport()
    flow = FakturamaFlow()
    driver = FakturamaDriver(report)

    def stage(name: str, fn, *args, **kwargs):
        """Run one stage, recording a stop instead of raising outward."""
        print(f"\n--- {name} ---")
        try:
            return fn(*args, **kwargs)
        except _Halt:
            raise
        except Exception as exc:
            report.stop(name, exc)
            raise _Halt from exc

    try:
        # 1. Extract the image and open a New Order
        stage("1. open New Order", driver.open_new_order, order)
        flow.extracted()
        flow.order_opened()

        # 2. Select or create the Debtor
        found = stage("2. resolve Debtor", driver.try_select_debtor, order)
        if not found:
            flow.debtor_missing()
            stage("2b. create Debtor", driver.create_debtor, order)
            flow.debtor_created()
            found = stage("2c. re-select Debtor",
                          driver.try_select_debtor, order)
            if not found:
                report.stop("2c. re-select Debtor", ManualReview(
                    "Newly created Debtor is still not selectable from the Order"
                ))
                raise _Halt
        stage("2d. verify Debtor addresses",
              driver.verify_debtor_addresses, order)
        flow.debtor_resolved()

        # 3. Select or create each Product, in source order
        for index, item in enumerate(order.items):
            label = f"3. resolve Product {item.sku}"
            if not stage(label, driver.try_select_product, item):
                stage(f"3a. ensure {item.vat_name}", driver.ensure_vat, item)
                stage(f"3b. create Product {item.sku}",
                      driver.create_product, item)
                if not stage(f"3c. re-select Product {item.sku}",
                             driver.try_select_product, item):
                    report.stop(f"3c. re-select Product {item.sku}", ManualReview(
                        f"Newly created Product {item.sku} is not selectable"
                    ))
                    raise _Halt
            stage(f"3d. complete line {index + 1}",
                  driver.complete_line, item, index)
        flow.items_complete()

        # 4. Complete and save the Order
        stage("4. verify Order defaults", driver.verify_order_defaults, order)
        stage("4b. verify Order totals", driver.verify_order_totals, order)
        stage("4c. save Order", driver.save_order)
        flow.order_saved()
        stage("4d. create linked Invoice", driver.create_followup_invoice)

        # 5. Complete and verify the linked Invoice
        stage("5. complete Invoice", driver.complete_invoice, order)
        stage("5b. verify Documents", driver.verify_documents, order)
        flow.invoice_saved()

    except _Halt:
        pass

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Fakturama Image-to-Cash automation")
    parser.add_argument("--image", default=str(DEFAULT_IMAGE),
                        help="order image to process")
    parser.add_argument("--cached", help="reuse a saved extraction JSON")
    parser.add_argument("--extract-only", action="store_true",
                        help="extract and print, do not touch the UI")
    parser.add_argument("--dry-run", action="store_true",
                        help="extract and show the plan without driving the UI")
    parser.add_argument("--save-extraction", metavar="PATH",
                        help="write the extraction to a JSON file")
    args = parser.parse_args()

    try:
        order = load_cached(args.cached) if args.cached \
            else extract_sales_order(args.image)
    except Exception as exc:
        print(f"[error] extraction failed: {exc}")
        return 1

    describe(order)

    if args.save_extraction:
        Path(args.save_extraction).write_text(
            order.model_dump_json(indent=2), encoding="utf-8"
        )
        print(f"[extract] saved to {args.save_extraction}")

    if args.extract_only:
        return 0

    if args.dry_run:
        print("Planned UI sequence:")
        print("  1. New Order; set Date, Cust.Ref., Net, With VAT")
        print("  2. Address selector -> exact Debtor, else create then re-select")
        for item in order.items:
            print(f"  3. Product selector -> {item.sku}, else ensure "
                  f"{item.vat_name} then create, then set qty/price/discount")
        print("  4. Verify totals, Save, follow-up Invoice")
        print("  5. Apply paid status, Save, verify in Documents")
        return 0

    try:
        report = run(order)
    except Exception as exc:
        print(f"[error] could not start: {exc}")
        return 1

    print(report.summary())
    return 0 if report.stopped_at is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
