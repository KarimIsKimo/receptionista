import os
import requests
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import PlainTextResponse

app = FastAPI()

# --- Configuration ---
# In production, set these as Environment Variables on your hosting platform
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "your_custom_verify_token")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL", "https://script.google.com/macros/s/YOUR_SCRIPT_ID/exec")

@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Meta pings this route once when you set up the webhook in their developer portal.
    It expects the exact challenge string returned as plain text.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        # Must return the challenge as an integer/string exactly as received
        return PlainTextResponse(content=challenge, status_code=200)
    
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed")


@app.post("/webhook")
async def receive_message(request: Request):
    """
    Meta sends all incoming WhatsApp messages to this route.
    """
    body = await request.json()

    # WhatsApp sends a deeply nested JSON payload. 
    # We parse it safely to extract the actual text message.
    try:
        entry = body.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        
        # Check if this payload contains a user message (and not a delivery receipt)
        if "messages" in value:
            message = value["messages"][0]
            sender_phone = message["from"]
            text_content = message["text"]["body"]

            # ---------------------------------------------------------
            # AI PROCESSING GOES HERE
            # 1. Pass `text_content` to your LLM (e.g., OpenAI, Gemini, Groq)
            # 2. Instruct the LLM to extract booking intent and parameters
            # ---------------------------------------------------------
            
            # Example: Assume the AI parsed the text and returned this structured data
            extracted_booking_data = {
                "phone": sender_phone,
                "intent": "new_booking",
                "procedure": "Laser Hair Removal", 
                "date": "2026-09-02",            
                "time": "14:00"                  
            }

            # Forward the structured data to your Google Apps Script backend
            if extracted_booking_data["intent"] == "new_booking":
                response = requests.post(APPS_SCRIPT_URL, json=extracted_booking_data)
                
                if response.status_code != 200:
                    print(f"Error connecting to Apps Script: {response.text}")

    except (KeyError, IndexError):
        # The payload didn't match the expected message structure, which is normal 
        # for status updates (sent, delivered, read). We can ignore these safely.
        pass

    # You MUST return a 200 OK within 20 seconds, or Meta will assume your server is down 
    # and aggressively retry sending the exact same message.
    return {"status": "ok"}