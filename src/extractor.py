"""
Image -> SalesOrder via a multimodal LLM with an enforced response schema.

The prompt asks for transcription only. Anything that can be derived -- line
totals, gross prices, payment codes -- is computed in models.py instead, so a
model that is good at reading and mediocre at arithmetic cannot corrupt the
numbers that get written into Fakturama.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from PIL import Image

from models import SalesOrder

load_dotenv()

DEFAULT_MODEL = os.getenv("EXTRACTION_MODEL", "gemini-3.6-flash")

EXTRACTION_PROMPT = """
You are a data-entry assistant transcribing a sales order document into JSON.

Transcribe only. Do not compute, infer, or correct anything.

Rules:
1. Copy every value exactly as printed. If a field is absent, use null.
2. Payment method: copy the printed text verbatim -- "Bank Transfer" stays
   "Bank Transfer". Do NOT translate it into an accounting code.
3. Paid status: copy verbatim, e.g. "PAID" or "UNPAID".
4. Addresses: split each printed block into street, zip_code, city, country.
   If a block's first line differs from the company name, put that first line
   in additional_name.
5. Contact name: split into first and last name.
6. VAT: whole number only (19% -> 19). Discount likewise (10% -> 10, none -> 0).
7. source_line_total: the line total exactly as printed in the items table.
8. Money: numeric only, no currency symbols or thousands separators.
9. Dates: ISO YYYY-MM-DD.
10. Return every item row, in the order printed.
"""


def extract_sales_order(image_path: str | Path, model: str = DEFAULT_MODEL) -> SalesOrder:
    """Extract and validate a SalesOrder from an order image."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Order image not found: {path}")

    print(f"[extract] reading {path.name} with {model}")
    image = Image.open(path)

    client = genai.Client()
    response = client.models.generate_content(
        model=model,
        contents=[image, EXTRACTION_PROMPT],
        config={
            "response_mime_type": "application/json",
            "response_schema": SalesOrder,
            "temperature": 0.0,
        },
    )

    order: SalesOrder | None = response.parsed
    if order is None:
        raise ValueError(f"Model returned unparseable output: {response.text[:400]}")

    problems = order.reconcile()
    if problems:
        raise ValueError(
            "Extraction failed arithmetic reconciliation:\n  - "
            + "\n  - ".join(problems)
        )

    print(
        f"[extract] {order.external_reference} | {order.company} | "
        f"{len(order.items)} item(s) | net {order.computed_net:.2f}"
    )
    return order


def load_cached(path: str | Path) -> SalesOrder:
    """Rehydrate a previously saved extraction, for offline UI runs."""
    return SalesOrder.model_validate(json.loads(Path(path).read_text("utf-8")))


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "../tests/test_order.PNG"
    order = extract_sales_order(target)

    print(f"\nOrder date  : {order.order_date} -> UI {order.ui_order_date!r}")
    print(f"Payment     : {order.payment_info.method!r} "
          f"-> code {order.payment_info.payment_code!r}")
    print(f"Paid        : {order.payment_info.is_paid} on {order.payment_info.paid_on}")
    print(f"Delivery==billing: {order.delivery_same_as_billing}")
    print(f"VAT rates   : {order.vat_rates}")
    for item in order.items:
        print(
            f"  {item.sku:<14} {item.vat_name:<9} "
            f"master gross {item.master_gross_price:>8.2f}  "
            f"line net {item.line_net_total:>8.2f}"
        )
    print(f"Totals      : net {order.computed_net:.2f} / "
          f"vat {order.computed_vat:.2f} / gross {order.computed_gross:.2f}")
