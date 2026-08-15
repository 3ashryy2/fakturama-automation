"""
Extraction schema and the deterministic arithmetic that sits behind it.

Two rules shape this module:

1. The LLM extracts, it does not calculate. Every derived number -- product
   master gross price, line totals, document totals -- is recomputed here in
   Python so rounding is deterministic and auditable.

2. The source string survives normalization. Fakturama needs the payment
   method under two different names: the literal image text ("Bank Transfer")
   becomes the payment record's Name/Description, while a mapped code
   ("Credit transfer") drives the payment-code dropdown. Collapsing them at
   extraction time loses information the UI still needs.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

# Step 2.10.4: image payment method -> Fakturama payment-code dropdown entry.
PAYMENT_CODE_MAP = {
    "bank transfer": "Credit transfer",
    "credit card": "Credit card",
    "sepa direct debit": "SEPA direct debit",
}

# Tolerance when reconciling recomputed money against the source document.
MONEY_TOLERANCE = 0.02


def parse_date(value: str | date) -> date:
    """Accept ISO or the common human formats an LLM might return."""
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {value!r}")


def format_ui_date(value: date) -> str:
    """
    Render a date the way the running Fakturama locale displays it.

    The installed instance shows 'Jul 14, 2026' -- abbreviated month, no zero
    padding. Used for reporting; verification parses the field instead of
    comparing strings, so a different locale does not break the run.
    """
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def date_input_candidates(value: date) -> list[str]:
    """
    Numeric renderings to type into Fakturama's date picker, likeliest first.

    The Date control is a segmented picker, not a text box: it displays
    'Jul 14, 2026' but will not accept that back. Typing the characters of the
    display form scrambles it ('Aug 20, 0026'), because each keystroke is
    routed to the segment that currently has focus. A separator character
    advances to the next segment, so a delimited numeric date fills all three
    cleanly -- but the segment *order* follows the UI locale, so the caller
    tries these in turn and keeps whichever parses back to the right day.
    """
    d, m, y = value.day, value.month, value.year
    return [
        f"{m:02d}/{d:02d}/{y:04d}",   # en-US
        f"{d:02d}/{m:02d}/{y:04d}",   # en-GB
        f"{d:02d}.{m:02d}.{y:04d}",   # de-DE
        f"{y:04d}-{m:02d}-{d:02d}",   # ISO
    ]


def money(value: float) -> float:
    return round(value + 0.0, 2)


class Address(BaseModel):
    street: str = Field(..., description="Street and building number")
    zip_code: str = Field(..., description="Postal code")
    city: str = Field(..., description="City name")
    country: str = Field(..., description="Country name")
    additional_name: Optional[str] = Field(
        None, description="Second name line, e.g. 'Northstar Office Warehouse'"
    )
    district: Optional[str] = Field(None, description="District, if supplied")

    def matches(self, other: "Address") -> bool:
        return (
            self.street.strip().lower() == other.street.strip().lower()
            and self.zip_code.strip() == other.zip_code.strip()
            and self.city.strip().lower() == other.city.strip().lower()
        )


class PaymentInfo(BaseModel):
    method: str = Field(
        ..., description="Payment method EXACTLY as printed, e.g. 'Bank Transfer'"
    )
    status: str = Field(..., description="Paid status as printed, e.g. 'PAID'")
    payment_date: Optional[str] = Field(None, description="ISO date if paid")

    @property
    def is_paid(self) -> bool:
        return self.status.strip().upper() == "PAID"

    @property
    def payment_code(self) -> str:
        """The dropdown entry for step 2.10.4."""
        try:
            return PAYMENT_CODE_MAP[self.method.strip().lower()]
        except KeyError:
            raise ValueError(
                f"No payment-code mapping for {self.method!r}; "
                "stopping for manual review"
            )

    @property
    def paid_on(self) -> Optional[date]:
        return parse_date(self.payment_date) if self.payment_date else None


class LineItem(BaseModel):
    sku: str = Field(..., description="Item SKU / item number")
    description: str = Field(..., description="Item description")
    quantity: float = Field(..., description="Quantity")
    unit: Optional[str] = Field(None, description="Unit, e.g. 'pcs'")
    unit_net_price: float = Field(..., description="Net price per unit")
    vat_percentage: float = Field(..., description="VAT rate, whole number")
    discount: float = Field(0.0, description="Line discount percent, whole number")
    source_line_total: Optional[float] = Field(
        None, description="Line net total as printed, for reconciliation"
    )

    @property
    def vat_name(self) -> str:
        """Fakturama VAT record name, e.g. 'VAT 19%' (step 3.5)."""
        pct = self.vat_percentage
        shown = int(pct) if float(pct).is_integer() else pct
        return f"VAT {shown}%"

    @property
    def master_gross_price(self) -> float:
        """
        Product master price for step 3.9.

        Deliberately excludes the transaction-line discount -- that belongs to
        the order line, not to the product record.
        """
        return money(self.unit_net_price * (1 + self.vat_percentage / 100))

    @property
    def line_net_total(self) -> float:
        """Step 3.16: quantity x unit net price x (1 - discount/100)."""
        return money(
            self.quantity * self.unit_net_price * (1 - self.discount / 100)
        )

    def reconcile(self) -> Optional[str]:
        """Return a description of any mismatch against the printed total."""
        if self.source_line_total is None:
            return None
        if abs(self.line_net_total - self.source_line_total) > MONEY_TOLERANCE:
            return (
                f"{self.sku}: computed line net {self.line_net_total:.2f} "
                f"!= printed {self.source_line_total:.2f}"
            )
        return None


class SalesOrder(BaseModel):
    external_reference: str = Field(..., description="External reference")
    order_date: str = Field(..., description="Order date, ISO YYYY-MM-DD")

    company: str = Field(..., description="Customer company name")
    customer_alias: Optional[str] = Field(None, description="Customer alias")
    contact_first_name: Optional[str] = Field(None, description="Contact first name")
    contact_last_name: Optional[str] = Field(None, description="Contact last name")
    email: Optional[str] = Field(None, description="Contact e-mail")
    phone: Optional[str] = Field(None, description="Contact telephone")

    billing_address: Address
    delivery_address: Address
    payment_info: PaymentInfo

    items: List[LineItem] = Field(..., description="Every item row, in source order")

    total_net: float = Field(..., description="Printed net total")
    total_vat: float = Field(..., description="Printed VAT total")
    total_gross: float = Field(..., description="Printed gross total")

    # -- derived ----------------------------------------------------------
    @property
    def order_date_obj(self) -> date:
        return parse_date(self.order_date)

    @property
    def ui_order_date(self) -> str:
        return format_ui_date(self.order_date_obj)

    @property
    def delivery_same_as_billing(self) -> bool:
        """Step 2.8: identical addresses get both roles, not a second record."""
        return self.billing_address.matches(self.delivery_address)

    @property
    def vat_rates(self) -> list[float]:
        return sorted({item.vat_percentage for item in self.items})

    @property
    def computed_net(self) -> float:
        return money(sum(item.line_net_total for item in self.items))

    @property
    def computed_vat(self) -> float:
        return money(
            sum(
                item.line_net_total * item.vat_percentage / 100
                for item in self.items
            )
        )

    @property
    def computed_gross(self) -> float:
        return money(self.computed_net + self.computed_vat)

    # -- validation -------------------------------------------------------
    @model_validator(mode="after")
    def _normalise(self) -> "SalesOrder":
        self.order_date = self.order_date_obj.isoformat()
        return self

    def reconcile(self) -> list[str]:
        """
        Every disagreement between recomputed and printed money.

        A non-empty result means the extraction is not trustworthy enough to
        drive the UI, so the caller routes to manual review rather than
        writing a wrong order into Fakturama.
        """
        problems = [p for p in (i.reconcile() for i in self.items) if p]
        for label, computed, printed in (
            ("net total", self.computed_net, self.total_net),
            ("VAT total", self.computed_vat, self.total_vat),
            ("gross total", self.computed_gross, self.total_gross),
        ):
            if abs(computed - printed) > MONEY_TOLERANCE:
                problems.append(
                    f"{label}: computed {computed:.2f} != printed {printed:.2f}"
                )
        if not self.items:
            problems.append("no item rows extracted")
        return problems
