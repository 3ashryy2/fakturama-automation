import sys
from extractor import extract_sales_order
from state_machine import FakturamaAutomation
from ui_driver import FakturamaUIDriver

def run_automation(image_path: str):
    """
    Executes the continuous Image-to-Cash automation flow.
    """
    # Initialize the Finite State Machine
    fsm = FakturamaAutomation()
    print(f"[System] Initial State: extracting_image")

    # ---------------------------------------------------------
    # PHASE 1: Data Extraction & Validation
    # ---------------------------------------------------------
    print("\n--- PHASE 1: IMAGE EXTRACTION ---")
    try:
        order_data = extract_sales_order(image_path)
        fsm.validate_extraction()
    except Exception as e:
        print(f"[Error] Extraction failed: {e}")
        fsm.trigger_manual_review()
        sys.exit(1)

    # ---------------------------------------------------------
    # PHASE 2: UI Initialization
    # ---------------------------------------------------------
    print("\n--- PHASE 2: UI EXECUTION ---")
    try:
        ui = FakturamaUIDriver()
    except RuntimeError as e:
        print(f"[Error] {e}")
        sys.exit(1)
        
    # Start the "Order-first" flow
    ui.open_new_order(str(order_data.order_date), order_data.external_reference)
    fsm.start_debtor_search()

    # ---------------------------------------------------------
    # PHASE 3: Debtor Resolution
    # ---------------------------------------------------------
    debtor_match = ui.search_debtor_in_order(order_data.company)
    
    if debtor_match:
        fsm.debtor_found()
    else:
        fsm.debtor_missing()
        ui.create_new_debtor(order_data)
        fsm.debtor_created()
        
        # Reselect newly created Debtor from the Order
        ui.search_debtor_in_order(order_data.company)
        fsm.debtor_found()
        
    # ---------------------------------------------------------
    # PHASE 4: Product Resolution (Loop)
    # ---------------------------------------------------------
    for item in order_data.items:
        print(f"\n[System] Processing Line Item: {item.sku}")
        product_match = ui.search_product_in_order(item.sku)
        
        if product_match:
            fsm.product_found()
        else:
            fsm.product_missing()
            
            # Resolve Master Data prerequisites
            ui.verify_or_create_vat(item.vat_percentage)
            fsm.vat_verified()
            
            ui.create_new_product(item)
            fsm.product_created()
            
            # Reselect newly created Product from the Order
            ui.search_product_in_order(item.sku)
            fsm.product_found()
            
        # Complete the active grid row
        ui.complete_order_line_item(item)
        fsm.line_item_added()

    # Finalize item loop
    fsm.all_items_added()

    # ---------------------------------------------------------
    # PHASE 5: Save and Invoice Generation
    # ---------------------------------------------------------
    print("\n--- PHASE 3: VERIFICATION & INVOICING ---")
    ui.save_and_verify_order(order_data.total_net)
    fsm.order_verified()
    
    ui.generate_linked_invoice(order_data.payment_info)
    fsm.invoice_verified()
    
    print("\n[System] PROCESS COMPLETED SUCCESSFULLY.")

if __name__ == "__main__":
    # Point this to your local sample order image
    target_image = "tests/test_order.png"
    run_automation(target_image)