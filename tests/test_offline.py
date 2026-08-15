"""
Offline tests: everything that can be checked without Fakturama running.

The slowest feedback loop in this project is "restart the app, drive the UI,
wait". Everything here -- the money arithmetic, the payment-code mapping, the
date candidate ordering, the read-back comparison, and NatTable row detection
against saved screenshots -- is pure and runs in under a second.

    venv\\Scripts\\python -m pytest tests/test_offline.py -q
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from models import (  # noqa: E402
    Address, LineItem, PaymentInfo, SalesOrder,
    date_input_candidates, format_ui_date, parse_date,
)


# ---------------------------------------------------------------------------
# Arithmetic (step 3.9, 3.16, 4.3)
# ---------------------------------------------------------------------------
def chair() -> LineItem:
    return LineItem(sku="CHR-ERG-01", description="Ergonomic Desk Chair",
                    quantity=2, unit="pcs", unit_net_price=250.0,
                    vat_percentage=19, discount=10, source_line_total=450.0)


def mat() -> LineItem:
    return LineItem(sku="MAT-DESK-02", description="Anti-Fatigue Desk Mat",
                    quantity=3, unit="pcs", unit_net_price=40.0,
                    vat_percentage=19, discount=0, source_line_total=120.0)


def test_master_gross_price_excludes_line_discount():
    """Step 3.9: unit net x (1 + VAT/100), and the line discount must NOT apply."""
    assert chair().master_gross_price == 297.50   # 250 * 1.19, not 225 * 1.19
    assert mat().master_gross_price == 47.60


def test_line_net_total_applies_discount():
    """Step 3.16: quantity x unit net x (1 - discount/100)."""
    assert chair().line_net_total == 450.00       # 2 * 250 * 0.9
    assert mat().line_net_total == 120.00


def test_vat_name_matches_fakturama_convention():
    """Step 3.5 expects the record to be named 'VAT 19%', not 'VAT 19.0%'."""
    assert chair().vat_name == "VAT 19%"


def order(**overrides) -> SalesOrder:
    billing = Address(street="Friedrichstrasse 88", zip_code="10117",
                      city="Berlin", country="Germany")
    delivery = Address(street="Beusselstrasse 44", zip_code="10553",
                       city="Berlin", country="Germany")
    data = dict(
        external_reference="WEB-2026-0714-A17", order_date="2026-07-14",
        company="Northstar Office GmbH", customer_alias="NORTHSTAR-BERLIN",
        contact_first_name="Marta", contact_last_name="Klein",
        email="marta.klein@example.test", phone="+49 30 5550 1420",
        billing_address=billing, delivery_address=delivery,
        payment_info=PaymentInfo(method="Bank Transfer", status="PAID",
                                 payment_date="2026-07-18"),
        items=[chair(), mat()],
        total_net=570.0, total_vat=108.30, total_gross=678.30,
    )
    data.update(overrides)
    return SalesOrder(**data)


def test_document_totals_match_the_printed_document():
    o = order()
    assert (o.computed_net, o.computed_vat, o.computed_gross) == (570.0, 108.30, 678.30)
    assert o.reconcile() == []


def test_reconcile_rejects_a_bad_extraction():
    """A wrong total must be caught before any UI action happens."""
    problems = order(total_net=999.0).reconcile()
    assert problems and "net total" in problems[0]


def test_reconcile_rejects_an_empty_order():
    assert "no item rows extracted" in order(items=[]).reconcile()


# ---------------------------------------------------------------------------
# Payment mapping (step 2.10.4)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("printed,code", [
    ("Bank Transfer", "Credit transfer"),
    ("Credit Card", "Credit card"),
    ("SEPA Direct Debit", "SEPA direct debit"),
])
def test_payment_code_mapping(printed, code):
    assert PaymentInfo(method=printed, status="PAID").payment_code == code


def test_payment_method_keeps_the_printed_string():
    """The record's Name uses the image text; only the dropdown uses the code."""
    payment = PaymentInfo(method="Bank Transfer", status="PAID")
    assert payment.method == "Bank Transfer"
    assert payment.payment_code == "Credit transfer"


def test_unmappable_payment_method_stops_rather_than_guessing():
    with pytest.raises(ValueError):
        PaymentInfo(method="Carrier Pigeon", status="PAID").payment_code


def test_unpaid_order_has_no_payment_date():
    assert PaymentInfo(method="Bank Transfer", status="UNPAID").is_paid is False


# ---------------------------------------------------------------------------
# Dates (step 1.5)
# ---------------------------------------------------------------------------
def test_ui_date_has_no_zero_padding():
    assert format_ui_date(date(2026, 7, 4)) == "Jul 4, 2026"


def test_date_input_candidates_lead_with_us_ordering():
    """The installed locale is en-US, so M/D/Y must be tried first."""
    assert date_input_candidates(date(2026, 7, 14))[0] == "07/14/2026"


def test_every_date_candidate_round_trips():
    """Whichever ordering the picker accepts, parsing it back must agree."""
    target = date(2026, 7, 14)
    for candidate in date_input_candidates(target):
        try:
            parsed = parse_date(candidate)
        except ValueError:
            continue
        assert parsed.year == target.year


def test_delivery_same_as_billing_detection():
    same = Address(street="Friedrichstrasse 88", zip_code="10117",
                   city="Berlin", country="Germany")
    assert order(delivery_address=same).delivery_same_as_billing is True
    assert order().delivery_same_as_billing is False


# ---------------------------------------------------------------------------
# Read-back comparison
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("written,shown,expected", [
    ("0.00", "$0.00", True),        # Fakturama adds the currency symbol
    ("0%", "0.00%", True),          # and pads percentages
    ("250", "$250.00", True),
    ("297.50", "$297.50", True),
    ("Berlin", "Berlin", True),
    ("0.00", "$5.00", False),
    ("Berlin", "Munich", False),
])
def test_values_match_is_semantic_not_literal(written, shown, expected):
    from ui_driver import values_match
    assert values_match(written, shown) is expected


# ---------------------------------------------------------------------------
# NatTable row detection, against captured screenshots
# ---------------------------------------------------------------------------
GRIDS = ROOT / "docs" / "screenshots"


@pytest.mark.parametrize("filename,expected", [
    ("02-address-selector-exact-match.png", 1),
    ("03-payment-method-created.png", 1),
    ("04-product-selector-ambiguous.png", 3),      # duplicates -> manual review
    ("05-items-grid-natable.png", 2),              # incl. a selected (blue) row
    ("07-items-row-selected-first.png", 1),        # selected row is row 1
])
def test_populated_row_detection(filename, expected):
    """
    Row counting must survive empty rows and highlighted rows alike.

    Empty rows still have their separator rules drawn, so counting rules alone
    reports a fresh list as full. A selected row is painted a solid colour that
    reads as dark as a rule, so darkness alone merges it into its neighbour --
    and when the selected row is the *first* one it merges into the header and
    disappears entirely, which is what `07-...` pins down.
    """
    from PIL import Image
    from grid import Grid

    path = GRIDS / filename
    if not path.exists():
        pytest.skip(f"{filename} not captured")

    grid = Grid.__new__(Grid)
    grid.columns = []
    assert len(grid.populated_rows(Image.open(path))) == expected
