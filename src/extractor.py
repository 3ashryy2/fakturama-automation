import os
from PIL import Image
from google import genai
from models import SalesOrder
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

# Initialize the modern Google GenAI Client
# Ensure your GOOGLE_API_KEY environment variable is set.
client = genai.Client()

def extract_sales_order(image_path: str) -> SalesOrder:
    """
    Passes the order image to Gemini and returns a validated SalesOrder Pydantic object.
    """
    print(f"[Extractor] Processing image: {image_path}")
    
    # 1. Load the multimodal input
    order_image = Image.open(image_path)

    # 2. Define the strict instruction prompt
    extraction_prompt = """
    You are an expert data extraction assistant for an ERP system. 
    Analyze the provided invoice/order image and extract all transactional data exactly as it appears, but format it to match the requested JSON schema.
    
    Apply the following strict business logic during extraction:
    
    1. Addresses: Split the single-line address blocks into their respective components (Street, ZIP, City, Country). 
    2. Names: Split the Contact Name into first and last name variables.
    3. Discount: If no line-item discount is explicitly shown, set the discount to 0.
    4. VAT: Extract the VAT percentage as a whole number (e.g., for 19%, output 19).
    5. Payment Method: Standardize the extracted payment method if it matches these exact phrases[cite: 1, 2]:
        - If the image says "Bank Transfer", output "Credit transfer"
        - If the image says "Credit Card", output "Credit card"
        - If the image says "SEPA Direct Debit", output "SEPA direct debit"
    6. Currency: Disregard currency symbols; output numerical values as floats.
    """

    # 3. Call the Gemini API with structured outputs
    chat = client.chats.create(model='gemini-3.6-flash')
    response = chat.send_message(
        message=[order_image, extraction_prompt],
        config={
            "response_mime_type": "application/json",
            "response_schema": SalesOrder,
            "temperature": 0.0,
        }
    )

    # 4. The response.parsed field automatically returns the validated Pydantic object!
    extracted_order: SalesOrder = response.parsed
    
    print(f"[Extractor] Successfully extracted Order Ref: {extracted_order.external_reference}")
    return extracted_order

if __name__ == "__main__":
    # Test the extraction layer in isolation
    try:
        sample_order = extract_sales_order("tests/test_order.png")
        print(f"Company: {sample_order.company}")
        print(f"Items found: {len(sample_order.items)}")
        
        # Verify our Pydantic validator calculated the Gross Master Price successfully
        for item in sample_order.items:
            print(f"SKU: {item.sku} | Calculated Master Gross Price: {item.calculated_gross_price}")
            
    except Exception as e:
        print(f"Extraction failed: {e}")