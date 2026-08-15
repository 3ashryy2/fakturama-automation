from pydantic import BaseModel, Field, model_validator
from typing import List, Optional

class Address(BaseModel):
    street: str = Field(..., description="Street and building number")
    zip_code: str = Field(..., description="Postal code")
    city: str = Field(..., description="City name")
    country: str = Field(..., description="Country name")
    district: Optional[str] = Field(None, description="Optional address district or specification")

class PaymentInfo(BaseModel):
    method: str = Field(..., description="Exact payment method (e.g., 'Bank Transfer', 'Credit Card')")
    status: str = Field(..., description="Current payment status (e.g., 'PAID', 'UNPAID')")
    payment_date: Optional[str] = Field(None, description="Payment date if PAID, format YYYY-MM-DD")

class LineItem(BaseModel):
    sku: str = Field(..., description="Item SKU/Number")
    description: str = Field(..., description="Full item description")
    quantity: float = Field(..., description="Item quantity")
    unit: str = Field(..., description="Measurement unit (e.g., 'pcs')")
    unit_net_price: float = Field(..., description="Net price per single unit")
    vat_percentage: float = Field(..., description="VAT rate as a whole number (e.g., 19 for 19%)")
    discount: float = Field(..., description="Line discount percentage as a whole number")
    
    # Calculated field for the Master Product creation
    calculated_gross_price: Optional[float] = None

    @model_validator(mode='after')
    def calculate_gross(self) -> 'LineItem':
        # Calculate the base product gross price for Master Data creation
        gross = self.unit_net_price * (1 + (self.vat_percentage / 100))
        self.calculated_gross_price = round(gross, 2)
        return self

class SalesOrder(BaseModel):
    external_reference: str = Field(..., description="Order reference ID")
    order_date: str = Field(..., description="Order date, format YYYY-MM-DD")
    
    # Debtor / Contact Details
    company: str = Field(..., description="Customer company name")
    customer_alias: Optional[str] = Field(None, description="Customer Alias if present")
    contact_first_name: str = Field(..., description="Contact's first name")
    contact_last_name: str = Field(..., description="Contact's last name")
    email: str = Field(..., description="Contact email address")
    phone: str = Field(..., description="Contact phone number")
    
    billing_address: Address
    delivery_address: Address
    payment_info: PaymentInfo
    
    items: List[LineItem] = Field(..., description="List of all purchased items")
    
    # Validation Totals
    total_net: float = Field(..., description="Extracted Net Total")
    total_vat: float = Field(..., description="Extracted VAT Total")
    total_gross: float = Field(..., description="Extracted Gross Total")