import uiautomation as auto
import time
from models import SalesOrder, LineItem, PaymentInfo, Address

class FakturamaUIDriver:
    """
    Low-level UI Automation wrapper for interacting with Fakturama.
    Uses Microsoft UIA via the 'uiautomation' library.
    """

    def __init__(self, window_title_regex=".*Fakturama.*"):
        # Set a global timeout for UI elements to appear
        auto.SetGlobalSearchTimeout(10.0)
        
        # Connect to the main Fakturama window (SearchDepth=1 prevents hanging)
        print("[UI] Connecting to Fakturama...")
        self.main_window = auto.WindowControl(searchDepth=1, RegexName=window_title_regex)
        
        if not self.main_window.Exists(3, 1):
            raise RuntimeError("Fakturama window not found. Is it running?")
        self.main_window.SetActive()
        print("[UI] Successfully connected.")

    # ---------------------------------------------------------
    # 1. Order Initialization
    # ---------------------------------------------------------
    def open_new_order(self, order_date: str, cust_ref: str):
        print(f"[UI] Opening New Order...")
        # Locate the 'Order' button in the top toolbar
        order_btn = self.main_window.ButtonControl(searchDepth=5, Name="Create: New Order")
        order_btn.Click()

        # Wait for the New Order editor panel to appear
        self.order_editor = self.main_window.PaneControl(RegexName=".*New Order.*")
        if not self.order_editor.Exists(10, 1):
            raise RuntimeError("New Order pane did not appear.")

        # 1. Find the "Date" label, then get the input box next to it
        date_label = self.order_editor.TextControl(searchDepth=5, Name="Date")
        date_edit = date_label.GetNextSiblingControl()
        date_edit.Click()
        date_edit.SendKeys('{Ctrl}a{Delete}')
        date_edit.SendKeys(order_date)

        # 2. Find the "Cust.Ref." label, then get the input box next to it
        ref_label = self.order_editor.TextControl(searchDepth=5, Name="Cust.Ref.")
        ref_edit = ref_label.GetNextSiblingControl()
        ref_edit.Click() # Focus the box
        ref_edit.SendKeys('{Ctrl}a{Delete}') # Clear any default text just in case
        ref_edit.SendKeys(cust_ref)

    # ---------------------------------------------------------
    # 2. Debtor Interaction
    # ---------------------------------------------------------
    def search_debtor_in_order(self, search_term: str) -> bool:
        """Attempts to find the debtor, returns False if missing to trigger FSM fallback."""
        print(f"[UI] Searching for debtor: {search_term}")
        
        # We use .Exists() with a short 2-second timeout. 
        # If it doesn't exist, it safely moves on instead of crashing with a LookupError.
        address_icon = self.order_editor.ButtonControl(Name="Select the address", searchDepth=4)
        if address_icon.Exists(2, 1):
            address_icon.Click()
            # Modal handling would go here...
            
        print("[UI] Debtor not found in current view. FSM should trigger creation.")
        return False 

    def create_new_debtor(self, order_data: SalesOrder):
        """Navigates to New Contact and fills the Debtor Master Data."""
        print("[UI] Creating New Debtor Master Record...")
        new_contact_btn = self.main_window.SplitButtonControl(searchDepth=5, Name="Create a new contact")
        new_contact_btn.Click()

        debtor_editor = self.main_window.PaneControl(RegexName=".*New Contact.*")
        if not debtor_editor.Exists(10, 1):
            raise RuntimeError("New Contact pane did not appear.")

        # Fill Company, First/Last Name
        debtor_editor.EditControl(Name="Company").SendKeys(order_data.company)
        debtor_editor.EditControl(Name="First Name").SendKeys(order_data.contact_first_name)
        debtor_editor.EditControl(Name="Name").SendKeys(order_data.contact_last_name)

        # Fill Billing Address
        debtor_editor.EditControl(Name="Street").SendKeys(order_data.billing_address.street)
        debtor_editor.EditControl(Name="ZIP").SendKeys(order_data.billing_address.zip_code)
        debtor_editor.EditControl(Name="City").SendKeys(order_data.billing_address.city)
        # ... logic to assign Invoice/Delivery roles and save ...

    # ---------------------------------------------------------
    # 3. Product & VAT Interaction
    # ---------------------------------------------------------
    def search_product_in_order(self, sku: str) -> bool:
        """Clicks the product selection icon in the order line and searches by SKU."""
        product_icon = self.order_editor.ButtonControl(Name="Select a product")
        product_icon.Click()
        # Similar modal handling as the Debtor search...
        return False

    def verify_or_create_vat(self, vat_percentage: float):
        """Opens Data > VATs, searches for 'VAT XX%', and creates if missing."""
        print(f"[UI] Verifying VAT {vat_percentage}% exists...")
        # ... logic to navigate the Data tree and check VAT table ...
        pass

    def create_new_product(self, item: LineItem):
        """Navigates to New Product and fills Master Data using the calculated gross price."""
        print(f"[UI] Creating Product Master for SKU: {item.sku}")
        # Note: We use the calculated_gross_price from the Pydantic model here
        # ... logic to set item.sku, description, calculated_gross_price, and map VAT ...
        pass

    def complete_order_line_item(self, item: LineItem):
        """Fills the currently active row in the Order Items table."""
        print(f"[UI] Filling order line for {item.sku}...")
        # ... logic to input item.quantity, item.unit_net_price, and item.discount into the grid ...
        pass

    # ---------------------------------------------------------
    # 4. Verification and Invoicing
    # ---------------------------------------------------------
    def save_and_verify_order(self, expected_total: float):
        """Clicks save and verifies the document state in the Data > Documents tree."""
        save_btn = self.main_window.ButtonControl(searchDepth=3, Name="Save")
        save_btn.Click()
        time.sleep(1) # Allow database to persist
        # ... logic to check Data > Documents for open order ...
        pass

    def generate_linked_invoice(self, payment_info: PaymentInfo):
        """Clicks Invoice from the follow-up menu of the saved Order and applies payment."""
        print("[UI] Generating follow-up Invoice...")
        invoice_btn = self.order_editor.ButtonControl(Name="Invoice")
        invoice_btn.Click()
        
        invoice_editor = self.main_window.PaneControl(RegexName=".*Invoice.*")
        if not invoice_editor.Exists(10, 1):
            raise RuntimeError("Invoice pane did not appear.")
        
        # Apply payment status if PAID
        if payment_info.status.upper() == "PAID":
            paid_checkbox = invoice_editor.CheckBoxControl(Name="paid")
            paid_checkbox.Click()
            # ... fill date and value ...
        
        # Save invoice
        invoice_editor.ButtonControl(searchDepth=3, Name="Save").Click()