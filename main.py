import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

app = FastAPI()

# 1. Configuration
VERIFY_TOKEN = "Neckface@2003"

# Replace these with the values from your Meta API Setup page
PHONE_NUMBER_ID = "1360825553771801"
ACCESS_TOKEN = "EAAZAnVi3cKTQBSXjsDyBzG4KDhV9pQATPG5hAYcIdb3ottZBdpQZBNyf71MavZCkytvi5KXX6CHdimYgX0yfE7RqoUXl0NIC83skQXDGRxZB7ffxZCXqC8DSZBNfw2LP4cBjvu4PRkPksRXgmFkXFSoFZCmuS5VZA1w8PJBv29y1yPOEjVVhB1QSYheooWTJ3fx49iwZDZD"

# 2. Webhook Verification (GET)
@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return PlainTextResponse(content=challenge)
        else:
            return Response(content="Verification failed", status_code=403)
            
    return Response(content="Bad Request", status_code=400)

# 3. Message Receiver & Automated Response (POST)
@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()

    try:
        # Extract the message details safely
        entries = body.get("entry", [])
        if entries:
            changes = entries[0].get("changes", [])
            if changes:
                value = changes[0].get("value", {})
                messages = value.get("messages", [])

                if messages:
                    incoming_msg = messages[0]
                    sender_phone = incoming_msg.get("from")  # Patient's phone number
                    msg_type = incoming_msg.get("type")

                    # Handle standard text messages
                    if msg_type == "text":
                        user_text = incoming_msg.get("text", {}).get("body", "").strip().lower()

                        # Basic Chatbot Response Logic
                        if user_text in ["hi", "hello", "hey", "مرحبا", "السلام عليكم"]:
                            reply = "Welcome! How can we assist you with your clinic inquiry today?"
                        elif "hours" in user_text or "مواعيد" in user_text:
                            reply = "Our working hours are Saturday through Thursday, 9:00 AM to 9:00 PM."
                        elif "appointment" in user_text or "حجز" in user_text:
                            reply = "To book an appointment, please reply with your preferred day and doctor."
                        else:
                            reply = f"Thank you for contacting us. We received: '{user_text}'. A representative will reply shortly!"

                        # Send the auto-reply back to WhatsApp
                        await send_whatsapp_message(sender_phone, reply)

    except Exception as e:
        print(f"Error parsing message payload: {e}")

    # Meta requires a fast 200 OK response to confirm delivery
    return Response(content="EVENT_RECEIVED", status_code=200)

# 4. Helper function to call Meta API
async def send_whatsapp_message(recipient_phone: str, text_content: str):
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "text",
        "text": {"body": text_content},
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        print("Meta API Response:", response.status_code, response.text)
