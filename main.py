import os
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from google import genai

app = FastAPI()

# 1. Configuration
VERIFY_TOKEN = "Neckface@2003"
PHONE_NUMBER_ID = "1360825553771801"
ACCESS_TOKEN = "EAAZAnVi3cKTQBSXjsDyBzG4KDhV9pQATPG5hAYcIdb3ottZBdpQZBNyf71MavZCkytvi5KXX6CHdimYgX0yfE7RqoUXl0NIC83skQXDGRxZB7ffxZCXqC8DSZBNfw2LP4cBjvu4PRkPksRXgmFkXFSoFZCmuS5VZA1w8PJBv29y1yPOEjVVhB1QSYheooWTJ3fx49iwZDZD"

# Initialize the Google GenAI client (it reads GEMINI_API_KEY from your Render environment variables)
ai_client = genai.Client()

# System instructions to shape the AI's persona for your clinic
CLINIC_SYSTEM_PROMPT = """
You are a friendly, professional AI receptionist and medical assistant for a clinic. 
Your job is to answer patient inquiries politely, provide general working hours (Saturday-Thursday, 9 AM to 9 PM), 
and guide them on how to book appointments. 
Keep your responses concise, empathetic, and clear (maximum 2-3 sentences), as you are chatting via WhatsApp. 
If a patient asks a specific medical diagnosis question, gently remind them that a doctor must evaluate them in person.
"""

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

# 3. Message Receiver & AI Automated Response (POST)
@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()

    try:
        entries = body.get("entry", [])
        if entries:
            changes = entries[0].get("changes", [])
            if changes:
                value = changes[0].get("value", {})
                messages = value.get("messages", [])

                if messages:
                    incoming_msg = messages[0]
                    sender_phone = incoming_msg.get("from")
                    msg_type = incoming_msg.get("type")

                    if msg_type == "text":
                        user_text = incoming_msg.get("text", {}).get("body", "").strip()

                        # Generate AI response using Gemini
                        ai_response = generate_ai_reply(user_text)

                        # Send the AI's reply back to WhatsApp
                        await send_whatsapp_message(sender_phone, ai_response)

    except Exception as e:
        print(f"Error parsing message payload: {e}")

    return Response(content="EVENT_RECEIVED", status_code=200)

# AI Generation Function
def generate_ai_reply(user_message: str) -> str:
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.7-flash',
            contents=user_message,
            config={
                'system_instruction': CLINIC_SYSTEM_PROMPT,
                'temperature': 0.3,
            }
        )
        return response.text
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return "Thank you for contacting our clinic. A representative will get back to you shortly."

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
