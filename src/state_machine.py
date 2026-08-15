from statemachine import StateMachine, State

class FakturamaAutomation(StateMachine):
    """
    State machine controlling the 'Order-first' flow through Fakturama.
    """
    # Define States
    extracting_image = State(initial=True)
    initializing_order = State()
    searching_debtor = State()
    creating_debtor = State()
    searching_product = State()
    verifying_vat = State()
    creating_product = State()
    adding_line_items = State()
    verifying_order = State()
    generating_invoice = State()
    completed = State(final=True)
    
    # Error / Manual Intervention State
    manual_review_required = State(final=True)

    # 1. Extraction -> Order Start
    validate_extraction = extracting_image.to(initializing_order)
    
    # 2. Debtor Handling
    start_debtor_search = initializing_order.to(searching_debtor)
    debtor_found = searching_debtor.to(searching_product)
    debtor_missing = searching_debtor.to(creating_debtor)
    debtor_created = creating_debtor.to(searching_debtor)  # Loop back to search after creation
    
    # 3. Product Handling (Loops per item)
    product_found = searching_product.to(adding_line_items)
    product_missing = searching_product.to(verifying_vat)
    
    vat_verified = verifying_vat.to(creating_product)
    vat_missing = verifying_vat.to(verifying_vat) # Internal logic will create VAT then self-transition
    
    product_created = creating_product.to(searching_product) # Loop back to search after creation
    
    # 4. Item Completion
    line_item_added = adding_line_items.to(searching_product) # Next item loop
    all_items_added = adding_line_items.to(verifying_order)
    
    # 5. Order & Invoice Completion
    order_verified = verifying_order.to(generating_invoice)
    invoice_verified = generating_invoice.to(completed)

    # 6. Global Error Fallback
    # All non-final states can trigger a manual review on UI failure or ambiguity
    trigger_manual_review = (
        extracting_image.to(manual_review_required) |
        searching_debtor.to(manual_review_required) |
        creating_debtor.to(manual_review_required) |
        searching_product.to(manual_review_required) |
        verifying_vat.to(manual_review_required) |
        generating_invoice.to(manual_review_required)
    )

    # Example callback hooking into UI logic
    def on_enter_searching_debtor(self):
        print("[System] Entering Debtor Search Context in UIA...")
        # UI logic to focus the address search window goes here.