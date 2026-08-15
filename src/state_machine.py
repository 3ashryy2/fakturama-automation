"""
The Order-first flow as an explicit state machine.

Its job is not to perform the work -- ui_driver.py does that -- but to make the
legal orderings explicit and to refuse illegal ones. The valuable property here
is that resolving missing master data is a *detour*: creating a Debtor, a
payment method, a VAT rate or a Product suspends the Order and must return to
it. Encoding that as transitions means a detour which never comes back raises
immediately rather than quietly leaving the run in a half-built Order.
"""

from __future__ import annotations

from statemachine import State, StateMachine


class FakturamaFlow(StateMachine):
    # -- states -----------------------------------------------------------
    extracting = State(initial=True)
    order_open = State()
    debtor_pending = State()
    debtor_creating = State()
    debtor_ready = State()
    items_pending = State()
    order_complete = State()
    invoice_open = State()
    done = State(final=True)
    halted = State(final=True)

    # -- 1: extraction to an open Order -----------------------------------
    extracted = extracting.to(order_open)
    order_opened = order_open.to(debtor_pending)

    # -- 2: Debtor, with a creation detour --------------------------------
    debtor_missing = debtor_pending.to(debtor_creating)
    debtor_created = debtor_creating.to(debtor_pending)
    debtor_resolved = debtor_pending.to(debtor_ready) | debtor_ready.to(items_pending)

    # -- 3: items ---------------------------------------------------------
    items_complete = (
        items_pending.to(order_complete)
        | debtor_ready.to(order_complete)
    )

    # -- 4 and 5 ----------------------------------------------------------
    order_saved = order_complete.to(invoice_open)
    invoice_saved = invoice_open.to(done)

    # -- stop-for-manual-review, legal from any working state -------------
    halt = (
        extracting.to(halted)
        | order_open.to(halted)
        | debtor_pending.to(halted)
        | debtor_creating.to(halted)
        | debtor_ready.to(halted)
        | items_pending.to(halted)
        | order_complete.to(halted)
        | invoice_open.to(halted)
    )
