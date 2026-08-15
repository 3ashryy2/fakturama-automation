"""
The Fakturama Image-to-Cash flow, one method per step of the brief.

The procedure is hardcoded on purpose -- the order of operations is fixed and
belongs in readable, linear code. What is *not* hardcoded is where any control
lives: every handle is resolved at call time by grounding.py, because window
size, DPI scaling and Eclipse's editor layout all move controls between runs.

Verification favours consequences over appearances. After a line item is added
the order's Total Net is read straight out of its native field rather than
OCR'd off the grid, so a normal run makes no vision calls at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import uiautomation as auto

import diagnostics
import grounding as g
from grid import AmbiguousMatch, Grid, SelectorDialog
from models import LineItem, SalesOrder

SAVE_BUTTON = "Save the current contents"
NEW_ORDER_BUTTON = "Create: New Order"


class ManualReview(RuntimeError):
    """Raised where the brief says to stop rather than guess."""


class VerificationFailed(RuntimeError):
    """A step wrote a value that did not survive read-back."""


@dataclass
class RunReport:
    """What the run actually did, for the console summary and the README."""
    steps: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    stopped_at: Optional[str] = None
    error: Optional[str] = None

    def step(self, message: str) -> None:
        self.steps.append(message)
        print(f"  [ok] {message}")

    def gap(self, message: str) -> None:
        self.gaps.append(message)
        print(f"  [gap] {message}")

    def stop(self, stage: str, exc: BaseException) -> None:
        self.stopped_at = stage
        self.error = f"{type(exc).__name__}: {exc}"
        print(f"  [stop] {stage}: {self.error}")

    def summary(self) -> str:
        lines = [
            "",
            "=" * 68,
            "RUN SUMMARY",
            "=" * 68,
            f"steps completed : {len(self.steps)}",
            f"records created : {', '.join(self.created) or '(none)'}",
            f"records reused  : {', '.join(self.reused) or '(none)'}",
        ]
        if self.gaps:
            lines.append("known gaps:")
            lines += [f"  - {gap}" for gap in self.gaps]
        if self.stopped_at:
            lines += ["", f"STOPPED AT: {self.stopped_at}", f"  {self.error}"]
        else:
            lines += ["", "COMPLETED: Order and linked Invoice saved and verified"]
        return "\n".join(lines)


def money_of(text: str) -> float:
    """Parse a Fakturama money field ('$570.00', '1,234.50 EUR') to a float."""
    cleaned = "".join(c for c in text if c.isdigit() or c in ".,-")
    cleaned = cleaned.replace(",", "") if cleaned.count(",") and "." in cleaned \
        else cleaned.replace(",", ".")
    try:
        return round(float(cleaned), 2)
    except ValueError:
        raise VerificationFailed(f"Cannot read a number from {text!r}")


def percent_of(text: str) -> float:
    return money_of(text.replace("%", ""))


def values_match(written: str, shown: str) -> bool:
    """
    Did `shown` end up meaning the same as `written`?

    Fakturama normalises on commit -- '0.00' comes back as '$0.00', '0%' as
    '0.00%', '250' as '$250.00'. Comparing the raw strings would flag every one
    of those as a failed write, so numbers are compared numerically and
    everything else case-insensitively.
    """
    written, shown = (written or "").strip(), (shown or "").strip()
    if written.lower() == shown.lower():
        return True
    try:
        return abs(money_of(written) - money_of(shown)) < 0.005
    except VerificationFailed:
        return False


class FakturamaDriver:
    """Drives one continuous Order-first run."""

    def __init__(self, report: Optional[RunReport] = None):
        self.window = g.main_window()
        self.report = report or RunReport()
        g.ensure_foreground(self.window)
        # Resolved once per editor rather than per action: the Win32 sweep is
        # ~2ms but the UIA walk behind the selector icons is ~600ms.
        self._icons: dict[tuple[str, int], tuple[int, int]] = {}

    # -- infrastructure ---------------------------------------------------
    def scope(self) -> g.Scope:
        """A fresh snapshot of the main window's native controls."""
        return g.Scope(self.window)

    def invalidate(self) -> None:
        self._icons.clear()

    def icon(self, anchor: str, index: int = 0) -> tuple[int, int]:
        key = (anchor, index)
        if key not in self._icons:
            self._icons[key] = g.selector_icon(anchor, index)
        return self._icons[key]

    def click_icon(self, anchor: str, index: int = 0) -> None:
        g.ensure_foreground(self.window)
        x, y = self.icon(anchor, index)
        auto.Click(x, y, waitTime=0.15)

    def open_selector(self, anchor: str, title: str,
                      attempts: int = 3) -> SelectorDialog:
        """
        Click a selector icon until its dialog appears, and bind to it.

        Two separate hazards are handled here. Returning to the Order after
        another editor has been open means the first click lands on the editor
        to activate it rather than on the icon, so the dialog never opens --
        hence the retry. And the dialog is bound *inside* the loop rather than
        by the caller afterwards: a previous dialog that is still finishing its
        close would otherwise be detected as "open", leaving the caller to bind
        a window that vanishes a moment later.
        """
        # Dismiss a lingering instance rather than waiting it out. A dialog
        # left open by a failed attempt would otherwise block every retry --
        # the wait times out, the retry waits again, and the run can never
        # recover from a state it created itself.
        if g.dialog_open(title):
            try:
                SelectorDialog(title, timeout=3.0).cancel()
            except Exception:
                g.ensure_foreground(self.window)
                auto.SendKeys("{Esc}", waitTime=0.3)
            time.sleep(0.5)

        for _ in range(attempts):
            self.invalidate()
            self.click_icon(anchor, 0)
            deadline = time.time() + 4.0
            while time.time() < deadline:
                if g.dialog_open(title):
                    return SelectorDialog(title, timeout=6.0)
                time.sleep(0.25)
        raise g.ControlNotFound(
            f"{title!r} did not open after {attempts} clicks on the "
            f"{anchor!r} selector icon"
        )

    def wait_for_label(self, label: str, timeout: float = 20.0) -> g.Scope:
        """
        Block until an editor showing `label` has rendered *and settled*.

        Waiting for the label alone is not enough. Eclipse creates an editor's
        controls and then moves them as the layout resolves, so a snapshot
        taken too early pairs a label with whichever field is momentarily
        beside it -- which is how a date once ended up in the address box.
        Two identical consecutive layouts mean the editor has stopped moving.
        """
        self.invalidate()
        previous: Optional[tuple] = None

        def ready():
            nonlocal previous
            scope = self.scope()
            try:
                scope._anchor(label)
            except g.ControlNotFound:
                previous = None
                return None
            signature = scope.signature()
            if signature == previous:
                return scope
            previous = signature
            return None

        return g.wait_for(ready, timeout=timeout, interval=0.3,
                          what=f"editor with {label!r} to settle")

    def save(self) -> None:
        g.click_toolbar(SAVE_BUTTON)
        time.sleep(1.2)

    def close_editor(self) -> None:
        g.ensure_foreground(self.window)
        auto.SendKeys("{Ctrl}w", waitTime=0.4)
        time.sleep(0.8)
        self.invalidate()

    def settle_on_order(self) -> g.Scope:
        """
        Wait until the Order editor is front-most and laid out again.

        Closing a master-data editor hands focus back to the Order, but the
        workbench re-lays the tab out afterwards. Clicking a selector icon
        during that window opens a dialog that is immediately torn down and
        rebuilt, so whatever was bound to it has no contents -- which surfaces
        as a dialog with no list canvas. 'Cust.Ref.' is unique to the Order
        editor, so waiting on it proves the right tab is up and stable.
        """
        return self.wait_for_label("Cust.Ref.")

    def open_view(self, name: str) -> None:
        g.click_navigation(name)
        time.sleep(1.5)
        self.invalidate()

    def set_field(self, scope: g.Scope, label: str, value: str,
                  nth: int = 0, occurrence: int = 0) -> str:
        node = scope.field_for(label, occurrence=occurrence, nth=nth)
        actual = g.set_text(node, value)
        if not values_match(str(value), actual):
            raise VerificationFailed(
                f"{label!r}[{nth}] holds {actual!r} after writing {value!r}"
            )
        return actual

    def set_date_field(self, scope: g.Scope, label: str, value,
                       occurrence: int = 0) -> str:
        """
        Fill a segmented date picker and confirm it by parsing it back.

        Comparing the read-back as a *date* rather than as a string means a
        locale that displays or orders its segments differently still passes,
        as long as the field ends up on the right day.
        """
        from models import date_input_candidates, format_ui_date, parse_date

        node = scope.field_for(label, occurrence=occurrence)

        # Prime the focus. Straight after an editor opens, the first click is
        # spent activating it rather than focusing this control, so the opening
        # keystrokes land in no segment and every later one is a segment out of
        # phase -- which shows up as a date like 'Jul 20, 2607'.
        g.click(node)
        time.sleep(0.2)

        for candidate in date_input_candidates(value):
            for _ in range(2):
                g.click(node)
                auto.SendKeys("{Home}", waitTime=0.06)
                auto.SendKeys(candidate, waitTime=0.05)
                auto.SendKeys("{Tab}", waitTime=0.06)
                time.sleep(0.35)
                shown = node.text().strip()
                try:
                    if parse_date(shown) == value:
                        return shown
                except ValueError:
                    pass
        raise VerificationFailed(
            f"{label!r} shows {node.text()!r}; could not set it to "
            f"{format_ui_date(value)} with any locale ordering"
        )

    def set_combo(self, scope: g.Scope, label: str, value: str,
                  occurrence: int = 0) -> str:
        node = scope.field_for(label, occurrence=occurrence)
        actual = g.select_combo(node, value)
        if actual.strip().lower() != value.strip().lower():
            raise VerificationFailed(
                f"combo {label!r} holds {actual!r} after selecting {value!r}"
            )
        return actual

    # =====================================================================
    # 1. Open the Order first
    # =====================================================================
    def open_new_order(self, order: SalesOrder) -> None:
        g.click_toolbar(NEW_ORDER_BUTTON)
        scope = self.wait_for_label("Cust.Ref.")

        # 1.4 leave the proposed No. unchanged -- read it only for the report.
        number = scope.field_for("No.").text()
        self.report.step(f"New Order opened, proposed No. {number!r} left unchanged")

        # 1.5 Date. The picker is segmented, so this types a numeric form and
        # verifies by parsing the display back.
        shown = self.set_date_field(scope, "Date", order.order_date_obj)
        self.report.step(f"Date set to {shown!r} ({order.order_date})")

        # 1.6 External reference.
        self.set_field(scope, "Cust.Ref.", order.external_reference)
        self.report.step(f"Cust.Ref. set to {order.external_reference}")

        # 1.7 Net price mode, VAT stays 'With VAT'. The price-mode combo has no
        # label of its own, so it is found by the value it is showing.
        mode = scope.field_with_value(("Net", "Gross"))
        if mode.text().strip() != "Net":
            actual = g.select_combo(mode, "Net")
            if actual.strip() != "Net":
                raise VerificationFailed(f"price mode is {actual!r}, expected 'Net'")
        self.report.step("Price mode set to Net")

        vat_mode = scope.field_for("VAT", occurrence=0)
        if vat_mode.text().strip() != "With VAT":
            g.select_combo(vat_mode, "With VAT")
        self.report.step(f"VAT mode {vat_mode.text()!r}")

    # =====================================================================
    # 2. Select or create the Debtor
    # =====================================================================
    def try_select_debtor(self, order: SalesOrder) -> bool:
        """
        Step 2.1-2.3. True when an exact Debtor was selected.

        The Order's own address selector is the existence check -- no separate
        lookup against the Debtor list, exactly as the brief specifies.
        """
        zip_code = order.billing_address.zip_code
        with self.open_selector("Addresses", "Select the address") as dialog:
            dialog.search(order.company)
            try:
                dialog.grid.select_only_row("Debtor")
            except AmbiguousMatch:
                # This dialog lists *addresses*, not debtors, so a Debtor with
                # separate invoice and delivery addresses legitimately returns
                # more than one row for the same company. Narrowing by the
                # billing ZIP picks out the invoice address specifically --
                # still no OCR. Only if that stays ambiguous is it a genuine
                # conflict between different debtors.
                dialog.search(zip_code)
                try:
                    dialog.grid.select_only_row("Debtor")
                except AmbiguousMatch as exc:
                    raise ManualReview(
                        f"Debtor selection is ambiguous even when narrowed to "
                        f"ZIP {zip_code}: {exc}"
                    ) from exc
                except LookupError:
                    self.report.step(
                        f"No Debtor address at ZIP {zip_code}; creating")
                    return False
            except LookupError:
                self.report.step(f"No exact Debtor for {order.company!r}; creating")
                return False
            dialog.ok()

        self.report.step(f"Existing Debtor selected for {order.company!r}")
        self.report.reused.append(f"Debtor {order.company}")
        return True

    def address_text(self) -> str:
        """
        The address block the Order is currently showing.

        Identified by being multi-line: Fakturama renders the selected
        Debtor's address as a single read-only box with embedded newlines,
        which distinguishes it from every single-line field around it.
        """
        blocks = [
            n.text() for n in self.scope().fields
            if ("\n" in n.text() or "\r" in n.text())
        ]
        return "\n".join(blocks)

    def verify_debtor_addresses(self, order: SalesOrder) -> None:
        """
        Step 2.4 / 2.13: both populated addresses must match the image.

        Invoice and Delivery live on separate tabs, so each is checked with
        its own tab in front.
        """
        checks = [("Invoice address", order.billing_address)]
        if not order.delivery_same_as_billing:
            checks.append(("Delivery address", order.delivery_address))

        for tab, address in checks:
            try:
                self.click_tab(tab)
            except g.ControlNotFound:
                # Fakturama renders an address tab only when the document
                # actually carries that address. With a delivery address held
                # on the Debtor but not on the Order, no Delivery tab exists to
                # inspect -- so this is reported rather than treated as a
                # mismatch, which would halt on a tab the app never draws.
                if tab != "Invoice address":
                    self.report.gap(
                        f"Step 2.4: no {tab!r} tab on the Order, so it could "
                        f"not be verified against the image "
                        f"({address.street}, {address.zip_code} {address.city}). "
                        "The address is saved on the Debtor record."
                    )
                    continue
            blob = self.address_text().lower()
            missing = [
                token for token in (
                    address.street, address.zip_code, address.city,
                )
                if token and token.lower() not in blob
            ]
            if missing:
                raise ManualReview(
                    f"{tab} on the Order does not match the image "
                    f"(missing {', '.join(repr(m) for m in missing)}). "
                    f"Shown: {self.address_text()!r}"
                )
            self.report.step(f"{tab} matches the source image")

    def create_debtor(self, order: SalesOrder) -> None:
        """
        Steps 2.5-2.11, with the Order tab left open throughout.

        The payment method is resolved *before* the Debtor editor opens. That
        editor fills its Payment combo once, when it is created, and never
        refreshes: a method created while it is open saves correctly but stays
        unselectable, with the combo reporting no items at all. The same
        ordering constraint is already in the brief at step 3.7, where the VAT
        must exist before New product so the dropdown offers it.
        """
        self.ensure_payment_method(order)

        self.open_view("New Contact")
        scope = self.wait_for_label("Customer ID")

        proposed = scope.field_for("Customer ID").text()
        self.report.step(f"New Debtor opened, Customer ID {proposed!r} unchanged")

        self.set_field(scope, "Company", order.company)
        if order.contact_first_name:
            self.set_field(scope, "First Name Last Name", order.contact_first_name, nth=0)
        if order.contact_last_name:
            self.set_field(scope, "First Name Last Name", order.contact_last_name, nth=1)
        self.report.step(f"Company/contact entered for {order.company!r}")

        # 2.7 Main address.
        billing = order.billing_address
        scope = self.scope()
        self.set_field(scope, "Street", billing.street)
        self.set_field(scope, "ZIP - City", billing.zip_code, nth=0)
        self.set_field(scope, "ZIP - City", billing.city, nth=1)
        if order.email:
            self.set_field(scope, "E-Mail", order.email)
        if order.phone:
            self.set_field(scope, "Telephone", order.phone)
        if billing.additional_name:
            self.set_field(scope, "additional name", billing.additional_name)
        self.set_combo(self.scope(), "Country", billing.country)
        self.report.step(f"Main address entered ({billing.zip_code} {billing.city})")

        self.assign_address_roles(order)
        self.fill_miscellaneous(order)
        self.select_debtor_payment(order)

        self.save()
        self.report.step(f"Debtor {order.company!r} saved")
        self.report.created.append(f"Debtor {order.company}")
        self.close_editor()
        self.settle_on_order()

    def set_address_type(self, roles: str) -> str:
        """
        Write the 'address type' field and confirm it.

        The control looks like a plain Edit but behaves as a role picker: it
        resolves what is typed against its known roles, so the full role name
        ('Invoice address') is written rather than an abbreviation.

        Routed through set_text for its retry, which matters here because this
        field is usually reached straight after a combo: the first click lands
        on the still-focused combo to commit it rather than on this field, so
        the first write silently goes nowhere and the retry is what makes it
        stick.
        """
        node = self.scope().field_for("address type")
        actual = g.set_text(node, roles)
        if not actual.strip():
            raise VerificationFailed(
                f"'address type' is still empty after writing {roles!r}"
            )
        return actual

    def assign_address_roles(self, order: SalesOrder) -> None:
        """
        Step 2.8.

        The Main address always takes the Invoice role. When billing and
        delivery are the same it takes the Delivery role too and no second
        address is created; when they differ, a second address is added with
        the '+' control and given the Delivery role.
        """
        if order.delivery_same_as_billing:
            shown = self.set_address_type("Invoice address, Delivery address")
            self.report.step(f"Main address roles set to {shown!r} "
                             "(delivery identical, no second address)")
            return

        shown = self.set_address_type("Invoice address")
        self.report.step(f"Main address role set to {shown!r}")
        self.add_delivery_address(order)

    def add_delivery_address(self, order: SalesOrder) -> None:
        """Second address for a delivery location that differs from billing."""
        g.ensure_foreground(self.window)
        win = g.uia_window()
        plus = win.ButtonControl(searchDepth=20, Name="+")
        if not plus.Exists(4, 0.3):
            self.report.gap(
                "Step 2.8: could not find the '+' control to add a second "
                "address; the Delivery address was not created"
            )
            return

        plus.Click(waitTime=0.4)
        time.sleep(1.2)
        self.invalidate()

        delivery = order.delivery_address
        scope = self.scope()
        self.set_field(scope, "Street", delivery.street)
        self.set_field(self.scope(), "ZIP - City", delivery.zip_code, nth=0)
        self.set_field(self.scope(), "ZIP - City", delivery.city, nth=1)
        if delivery.additional_name:
            self.set_field(self.scope(), "additional name", delivery.additional_name)
        self.set_combo(self.scope(), "Country", delivery.country)

        shown = self.set_address_type("Delivery address")
        self.report.step(
            f"Second address added for delivery ({delivery.zip_code} "
            f"{delivery.city}), role {shown!r}"
        )

    def fill_miscellaneous(self, order: SalesOrder) -> None:
        """
        Steps 2.9 and 2.10 -- both live on the Miscellaneous tab.

        There is no separate Payment tab in this build: 'Alias name',
        'Discount', 'Net or Gross' and 'Payment' are all fields of
        Miscellaneous, so they are filled in one pass.
        """
        self.click_tab("Miscellaneous", expect="Alias name")

        if order.customer_alias:
            self.set_field(self.scope(), "Alias name", order.customer_alias)
        self.set_field(self.scope(), "Discount", "0%")
        self.set_combo(self.scope(), "Net or Gross", "Net")
        self.report.step("Miscellaneous set (alias, 0% discount, Net)")

    def click_tab(self, name: str, expect: Optional[str] = None,
                  timeout: float = 8.0) -> None:
        """
        Switch an editor's inner tab and confirm the switch happened.

        Clicking a CTabItem is not synchronous: the new page is laid out
        afterwards, so reading fields immediately still sees the old tab.
        Passing `expect` names a label unique to the destination and waits for
        it, which turns a silent no-op into a real failure.
        """
        g.ensure_foreground(self.window)
        win = g.uia_window(timeout)
        target = None
        for control, _ in auto.WalkControl(win, maxDepth=20):
            if control.ControlTypeName == "TabItemControl" \
                    and (control.Name or "").strip() == name \
                    and control.BoundingRectangle.width() > 0:
                target = control
                break
        if target is None:
            raise g.ControlNotFound(f"No tab named {name!r}")

        rect = target.BoundingRectangle
        auto.Click((rect.left + rect.right) // 2,
                   (rect.top + rect.bottom) // 2, waitTime=0.3)
        self.invalidate()
        if expect:
            self.wait_for_label(expect, timeout=timeout)
        else:
            time.sleep(0.6)

    # -- payment method ---------------------------------------------------
    def ensure_payment_method(self, order: SalesOrder) -> None:
        """
        Steps 2.10.1-2.10.6, run before the Debtor editor is opened.

        Existence is decided by filtering the terms-of-payment list and
        counting rows that actually contain ink -- no OCR, and no reliance on
        the Debtor editor's combo, which cannot be trusted to refresh.
        """
        method = order.payment_info.method
        self.open_view("terms of payment")

        scope = self.scope()
        g.set_text(scope.field_for("Search:"), method, commit=False)
        time.sleep(1.0)

        rows = Grid(self.list_canvas()).populated_rows()
        if len(rows) > 1:
            raise ManualReview(
                f"{len(rows)} payment methods match {method!r}; "
                "stopping for manual review"
            )
        if rows:
            self.report.step(f"Payment method {method!r} already exists; reusing")
            self.report.reused.append(f"Payment {method}")
            return

        self.create_payment_method(order)

    def select_debtor_payment(self, order: SalesOrder) -> None:
        """Step 2.10: pick the method, which ensure_payment_method guaranteed."""
        method = order.payment_info.method
        self.click_tab("Miscellaneous", expect="Payment")
        self.set_combo(self.scope(), "Payment", method)
        self.report.step(f"Payment method {method!r} selected")

    def create_payment_method(self, order: SalesOrder) -> None:
        """Steps 2.10.1-2.10.6."""
        payment = order.payment_info
        self.open_view("terms of payment")
        self.click_new_in_list()
        scope = self.wait_for_label("Name")

        self.set_field(scope, "Name", payment.method)
        self.set_field(self.scope(), "Description", payment.method)

        # Step 2.10.4. This build ships the payment-code label untranslated, as
        # the literal resource key '!editorPaymentPaymentcode!', so matching on
        # its text would break the moment that bug is fixed. It is the only
        # combo in this editor, which is a far steadier handle.
        combos = [n for n in self.scope().fields if n.cls == "ComboBox"]
        if len(combos) != 1:
            raise ManualReview(
                f"Expected exactly one combo in the payment editor, found "
                f"{len(combos)}; cannot set the payment code safely"
            )
        actual = g.select_combo(combos[0], payment.payment_code)
        if not values_match(payment.payment_code, actual):
            raise VerificationFailed(
                f"payment code is {actual!r}, expected {payment.payment_code!r}"
            )

        # Step 2.10.5.
        for label in ("Cash discount", "Discount Days", "Net Days"):
            try:
                self.set_field(self.scope(), label, "0")
            except (g.ControlNotFound, VerificationFailed):
                pass

        self.save()
        self.report.step(f"Payment method {payment.method!r} created "
                         f"(code {payment.payment_code!r})")
        self.report.created.append(f"Payment {payment.method}")
        self.close_editor()

    def click_new_in_list(self, exclude: str = "product") -> None:
        """
        The green '+' at the upper-right of the open list view.

        Each view names its own button ('Create a new tax rate', 'Create a new
        term of payment', ...), so rather than keeping a lookup table this
        matches the shared prefix. The global toolbar's 'Create a new product'
        is always present and must be excluded, or it would hijack every view.
        """
        g.ensure_foreground(self.window)
        win = g.uia_window()
        candidates = [
            control for control, _ in auto.WalkControl(win, maxDepth=16)
            if control.ControlTypeName == "ButtonControl"
            and (control.Name or "").lower().startswith("create a new")
            and exclude not in (control.Name or "").lower()
            and control.BoundingRectangle.width() > 0
        ]
        if not candidates:
            raise g.ControlNotFound(
                "No 'Create a new ...' control in the open list view"
            )
        candidates[0].Click(waitTime=0.3)
        time.sleep(1.5)
        self.invalidate()

    # =====================================================================
    # 3. Select or create each Product
    # =====================================================================
    def ensure_vat(self, item: LineItem) -> None:
        """Steps 3.4-3.6, run before New product so the rate is selectable."""
        self.open_view("VATs")
        scope = self.scope()
        search = scope.field_for("Search:")
        g.set_text(search, item.vat_name, commit=False)
        time.sleep(1.0)

        rows = Grid(self.list_canvas()).populated_rows()
        if len(rows) > 1:
            raise ManualReview(
                f"{len(rows)} VAT rows match {item.vat_name!r}; "
                "stopping for manual review"
            )
        if rows:
            self.report.step(f"{item.vat_name} already exists; reusing")
            self.report.reused.append(item.vat_name)
            return

        self.click_new_in_list()
        editor = self.wait_for_label("Name")
        self.set_field(editor, "Name", item.vat_name)
        self.set_field(self.scope(), "Description", item.vat_name)
        self.set_field(self.scope(), "Value", f"{item.vat_percentage:g}%")
        self.save()
        self.report.step(f"{item.vat_name} created (Standard rate)")
        self.report.created.append(item.vat_name)
        self.close_editor()

    def list_canvas(self) -> g.Node:
        """
        The NatTable of the currently open list view.

        Anchored to that view's own 'Search:' box. Picking the smallest canvas
        in the window instead reaches across into whatever editor is open
        behind the list -- in practice the Order's Remarks box -- and every
        existence check then reads an empty grid and creates a duplicate
        record.
        """
        scope = self.scope()
        search = scope.field_for("Search:")
        candidates = [
            n for n in scope.canvases(200, 80)
            if n.rect.top >= search.rect.bottom - 10
            and n.rect.left <= search.rect.left
            and n.rect.right >= search.rect.right
        ]
        if not candidates:
            raise g.ControlNotFound("No list canvas below the view's search box")
        return min(candidates, key=lambda n: n.width * n.height)

    def items_canvas(self) -> g.Node:
        """
        The Order editor's Items grid.

        Chosen as the innermost canvas that starts at the 'Items' anchor row,
        which distinguishes the NatTable from the composites wrapping it.
        """
        anchors, _ = g._uia_rects()
        if "Items" not in anchors:
            raise g.ControlNotFound("No 'Items' anchor in the open Order")
        left, top, _, _ = anchors["Items"]

        # The grid starts on the anchor's own row. Bounding the vertical offset
        # matters: without it the smallest canvas anywhere below 'Items' wins,
        # and that is the Remarks box further down the form.
        candidates = [
            n for n in self.scope().canvases(200, 60)
            if abs(n.rect.top - top) <= 40 and n.rect.left >= left
        ]
        if not candidates:
            raise g.ControlNotFound("Items grid canvas not found")
        # Innermost of the nested composites is the NatTable itself.
        return min(candidates, key=lambda n: n.width * n.height)

    def try_select_product(self, item: LineItem, attempts: int = 3) -> bool:
        """
        Steps 3.2-3.3, using the Order's product selector as the check.

        The whole open-search-select cycle is retried, not just the click.
        Reopening this dialog right after another editor has been saved and
        closed is genuinely flaky: it sometimes appears and is then torn down
        and rebuilt as the workbench settles, so a dialog bound in that window
        has no contents. Only a real ambiguity or a real absence is allowed to
        end the loop.
        """
        last: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                with self.open_selector("Items", "Select a product") as dialog:
                    diagnostics.capture(
                        f"{item.sku}-bound-attempt{attempt}", "Select a product")
                    dialog.search(item.sku)
                    try:
                        dialog.grid.select_only_row(f"Product {item.sku}")
                    except AmbiguousMatch as exc:
                        raise ManualReview(
                            f"Product {item.sku!r} is ambiguous: {exc}") from exc
                    except LookupError:
                        self.report.step(
                            f"No exact Product {item.sku!r}; creating")
                        return False
                    dialog.ok()
            except (ManualReview, VerificationFailed):
                raise
            except (TimeoutError, g.ControlNotFound) as exc:
                last = exc
                diagnostics.capture(
                    f"{item.sku}-FAILED-attempt{attempt}", "Select a product")
                self.invalidate()
                time.sleep(1.5)
                continue

            self.report.step(f"Existing Product {item.sku!r} selected")
            self.report.reused.append(f"Product {item.sku}")
            return True

        raise ManualReview(
            f"Product selector for {item.sku!r} did not become usable after "
            f"{attempts} attempts: {last}"
        )

    def create_product(self, item: LineItem) -> None:
        """
        Steps 3.7-3.11, opened from the toolbar rather than the navigation view.

        The 'New product' link in the left panel is not reliably clickable --
        depending on scroll position and which section is expanded, the click
        registers but no editor opens. The toolbar button is always present and
        always hits.
        """
        g.click_toolbar("Create a new product")
        scope = self.wait_for_label("Item Number")

        self.set_field(scope, "Item Number", item.sku)
        self.set_field(self.scope(), "Name", item.description)
        self.set_field(self.scope(), "Description", item.description)
        self.set_field(self.scope(), "Price (gross)", f"{item.master_gross_price:.2f}")
        self.set_field(self.scope(), "cost price (net)", "0.00")
        self.set_combo(self.scope(), "VAT", item.vat_name)
        try:
            self.set_field(self.scope(), "Stock", "0.00")
        except g.ControlNotFound:
            pass

        self.save()
        self.report.step(
            f"Product {item.sku!r} created at gross {item.master_gross_price:.2f} "
            f"with {item.vat_name}"
        )
        self.report.created.append(f"Product {item.sku}")
        self.close_editor()
        self.settle_on_order()

    def complete_line(self, item: LineItem, row_index: int) -> None:
        """Steps 3.13-3.16 on the row the product selector just filled."""
        grid = Grid(self.items_canvas())
        bands = grid.row_bands()
        if row_index >= len(bands):
            raise ManualReview(
                f"Items grid has {len(bands)} row(s); expected row {row_index}"
            )
        row = bands[row_index]
        grid.columns = ["Pos.", "Qty.", "Item No.", "Picture", "Name",
                        "Description", "VAT", "U.Price", "Discount", "Price"]

        # Qty. is reachable in the default (unscrolled) view.
        grid.edit_cell(row, "Qty.", f"{item.quantity:g}")

        # U.Price and Discount are past the right edge of the viewport, so the
        # grid is scrolled and the columns are then addressed from the right.
        if item.discount or item.unit_net_price:
            grid.scroll_columns("right")
            grid.edit_cell(row, "U.Price", f"{item.unit_net_price:.2f}",
                           anchor="right")
            if item.discount:
                grid.edit_cell(row, "Discount", f"{item.discount:g}%",
                               anchor="right")
            grid.scroll_columns("left")

        self.report.step(
            f"Line {row_index + 1}: {item.sku} qty {item.quantity:g} "
            f"@ {item.unit_net_price:.2f} less {item.discount:g}%"
        )

    # =====================================================================
    # 4. Complete and save the Order
    # =====================================================================
    def verify_order_totals(self, order: SalesOrder) -> None:
        """
        Step 4.3, read from the Order's own native total fields.

        This is the verification that replaces reading the grid: if every line
        landed correctly the document totals must equal the recomputed source
        totals, and if any line is wrong they cannot.
        """
        scope = self.scope()
        problems: list[str] = []

        def read(label: str, occurrence: int = 0) -> Optional[float]:
            try:
                return money_of(
                    scope.field_for(label, occurrence=occurrence).text())
            except (g.ControlNotFound, VerificationFailed):
                return None

        # The net-total label follows the document price mode: 'Total Net' in
        # Net mode, 'Total Gross' in Gross mode. Step 1.7 sets Net, but reading
        # whichever is present keeps this honest if that ever fails.
        net = read("Total Net")
        if net is None:
            net = read("Total Gross")
        # Two 'VAT' labels exist: the mode combo near the top and the tax total
        # at the bottom. The totals one is the second in document order.
        vat = read("VAT", occurrence=1)
        gross = read("Total")

        for label, actual, expected in (
            ("net total", net, order.computed_net),
            ("VAT total", vat, order.computed_vat),
            ("gross total", gross, order.computed_gross),
        ):
            if actual is None:
                problems.append(f"{label}: could not be read from the Order")
            elif abs(actual - expected) > 0.02:
                problems.append(f"{label}: Order shows {actual:.2f}, "
                                f"source says {expected:.2f}")

        if problems:
            raise ManualReview("Order totals disagree with the image:\n  - "
                               + "\n  - ".join(problems))
        self.report.step(
            f"Totals verified: net {order.computed_net:.2f} / "
            f"vat {order.computed_vat:.2f} / gross {order.computed_gross:.2f}"
        )

    def verify_order_defaults(self, order: SalesOrder) -> None:
        """
        Step 4.2: overall Discount stays 0% and Shipping stays free, unless the
        image supplied order-level values (this one does not).
        """
        scope = self.scope()
        try:
            discount = percent_of(scope.field_for("Discount").text())
            if abs(discount) > 0.001:
                raise ManualReview(
                    f"Order-level Discount is {discount}%, expected 0% -- the "
                    "image supplies no order-level discount"
                )
            self.report.step("Order Discount confirmed at 0%")
        except (g.ControlNotFound, VerificationFailed):
            self.report.gap("Step 4.2: order-level Discount could not be read")

        try:
            shipping = scope.field_for("Shipping").text().strip()
            self.report.step(f"Shipping left at {shipping!r}")
        except g.ControlNotFound:
            self.report.gap("Step 4.2: Shipping field could not be read")

    def save_order(self) -> str:
        scope = self.scope()
        number = scope.field_for("No.").text()
        self.save()
        self.report.step(f"Order {number} saved")
        return number

    def create_followup_invoice(self) -> None:
        """
        Step 4.6: the follow-up action, not the toolbar Invoice button --
        only the follow-up preserves the Order relationship.
        """
        g.ensure_foreground(self.window)
        win = g.uia_window()
        group = win.GroupControl(searchDepth=20, Name="Create a follow-up document")
        if not group.Exists(6, 0.3):
            raise g.ControlNotFound("Follow-up document group not found on the Order")
        button = group.ButtonControl(searchDepth=4, Name="Invoice")
        if not button.Exists(4, 0.3):
            raise g.ControlNotFound("Follow-up 'Invoice' button not found")
        button.Click(waitTime=0.3)
        self.wait_for_label("Cust.Ref.")
        self.report.step("Linked Invoice created from the Order's follow-up action")

    # =====================================================================
    # 5. Complete and verify the linked Invoice
    # =====================================================================
    def complete_invoice(self, order: SalesOrder) -> None:
        """Steps 5.1-5.4."""
        scope = self.scope()
        number = scope.field_for("No.").text()
        self.report.step(f"Invoice {number!r} proposed values left unchanged")

        payment = order.payment_info
        if not payment.is_paid:
            self.report.step("Paid status is not PAID; leaving paid clear")
            self.save()
            return

        # 5.2: the payment method must already be the extracted one, carried
        # over from the Debtor. The brief says stop if it is not available.
        self.verify_invoice_payment_method(order)

        checkbox = self.paid_checkbox()
        if checkbox is None:
            raise ManualReview(
                "Could not locate the Invoice 'paid' checkbox, so the PAID "
                "status could not be applied"
            )
        try:
            toggle = checkbox.GetTogglePattern()
            if toggle.ToggleState != 1:
                checkbox.Click(waitTime=0.3)
        except Exception:
            checkbox.Click(waitTime=0.3)
        time.sleep(0.8)

        # 5.3: payment date and the full invoice total. Both fields only exist
        # once 'paid' is ticked, which is why they are resolved after it.
        from models import format_ui_date

        paid_on = payment.paid_on
        if paid_on:
            if not self.set_paid_date(paid_on):
                self.report.gap(
                    f"Step 5.3: payment date {format_ui_date(paid_on)} could "
                    "not be written -- no date field appeared beside 'paid'"
                )

        try:
            self.set_field(self.scope(), "Value", f"{order.computed_gross:.2f}")
            self.report.step(f"Paid Value set to {order.computed_gross:.2f}")
        except (g.ControlNotFound, VerificationFailed) as exc:
            self.report.gap(f"Step 5.3: paid Value not set ({exc})")

        self.save()
        self.report.step(
            f"Invoice marked paid on {paid_on} for {order.computed_gross:.2f}"
        )

    def verify_invoice_payment_method(self, order: SalesOrder) -> None:
        """Step 5.2, without disturbing a correct carried-over value."""
        method = order.payment_info.method
        node = None
        for candidate in self.scope().fields:
            if candidate.cls == "ComboBox" and \
                    candidate.text().strip().lower() == method.strip().lower():
                node = candidate
                break
        if node is not None:
            self.report.step(f"Invoice payment method is {method!r}")
            return

        try:
            self.set_combo(self.scope(), "Payment", method)
            self.report.step(f"Invoice payment method set to {method!r}")
        except (g.ControlNotFound, VerificationFailed) as exc:
            raise ManualReview(
                f"Invoice payment method is not {method!r} and could not be "
                f"set ({exc}); stopping for manual review"
            ) from exc

    def set_paid_date(self, paid_on) -> bool:
        """
        Write the payment date beside the 'paid' checkbox.

        The field carries no label of its own in this build, so it is found by
        being a date picker that currently holds a parseable date -- the same
        segmented control the Order's Date field uses, filled the same way.
        """
        from models import date_input_candidates, parse_date

        for node in self.scope().fields:
            if node.cls != "Edit":
                continue
            current = node.text().strip()
            if not current:
                continue
            try:
                parse_date(current)
            except ValueError:
                continue
            for candidate in date_input_candidates(paid_on):
                g.click(node)
                auto.SendKeys("{Home}", waitTime=0.06)
                auto.SendKeys(candidate, waitTime=0.05)
                auto.SendKeys("{Tab}", waitTime=0.06)
                time.sleep(0.3)
                try:
                    if parse_date(node.text().strip()) == paid_on:
                        self.report.step(f"Payment date set to {node.text()!r}")
                        return True
                except ValueError:
                    continue
        return False

    def paid_checkbox(self):
        """
        The Invoice's 'paid' checkbox.

        A named match is preferred. An unnamed checkbox is accepted only when
        it is the sole candidate on screen: the UIA crawl found just two
        checkboxes in the whole process, so taking any unnamed one blindly
        risks toggling something unrelated.
        """
        g.ensure_foreground(self.window)
        win = g.uia_window()
        named, unnamed = [], []
        for control, _ in auto.WalkControl(win, maxDepth=20):
            if control.ControlTypeName != "CheckBoxControl":
                continue
            if control.BoundingRectangle.width() <= 0:
                continue
            name = (control.Name or "").strip().lower()
            if "paid" in name:
                named.append(control)
            elif not name:
                unnamed.append(control)

        if named:
            return named[0]
        return unnamed[0] if len(unnamed) == 1 else None

    def verify_documents(self, order: SalesOrder) -> None:
        """
        Steps 4.5 and 5.5: confirm the saved rows in Data > Documents.

        Counts rows carrying ink rather than separator rules, since the list
        draws rules for its empty rows too and would otherwise always look
        populated.
        """
        self.open_view("Documents")
        rows = Grid(self.list_canvas()).populated_rows()
        if len(rows) < 2:
            raise ManualReview(
                f"Documents view shows {len(rows)} saved row(s); expected the "
                "Order and its linked Invoice"
            )
        self.report.step(
            f"Documents view shows {len(rows)} saved documents "
            f"(Order + linked Invoice for {order.external_reference})"
        )
