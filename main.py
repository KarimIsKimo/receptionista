import os
import traceback
import httpx
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import PlainTextResponse
from google import genai

app = FastAPI()

VERIFY_TOKEN = "Neckface@2003"
PHONE_NUMBER_ID = "1360825553771801"
ACCESS_TOKEN = "EAAZAnVi3cKTQBSXjsDyBzG4KDhV9pQATPG5hAYcIdb3ottZBdpQZBNyf71MavZCkytvi5KXX6CHdimYgX0yfE7RqoUXl0NIC83skQXDGRxZB7ffxZCXqC8DSZBNfw2LP4cBjvu4PRkPksRXgmFkXFSoFZCmuS5VZA1w8PJBv29y1yPOEjVVhB1QSYheooWTJ3fx49iwZDZD"

CLINIC_SYSTEM_PROMPT = """
You are a friendly, professional AI receptionist and medical assistant for a clinic. 
Your job is to answer patient inquiries politely, provide general working hours (Saturday-Thursday, 9 AM to 9 PM), 
and guide them on how to book appointments. Keep your responses concise and empathetic (maximum 2-3 sentences).
"""

# Track processed IDs so retries are ignored
processed_message_ids = set()

@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token and mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge)
    return Response(content="Verification failed", status_code=403)

@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        entries = body.get("entry", [])
        if entries:
            value = entries[0].get("changes", [{}])[0].get("value", {})
            messages = value.get("messages", [])
            
            if messages:
                incoming_msg = messages[0]
                message_id = incoming_msg.get("id")

                # Deduplication: Ignore if we already processed this message
                if message_id in processed_message_ids:
                    return Response(content="DUPLICATE_IGNORED", status_code=200)

                if incoming_msg.get("type") == "text":
                    processed_message_ids.add(message_id)
                    sender_phone = incoming_msg.get("from")
                    user_text = incoming_msg.get("text", {}).get("body", "").strip()
                    
                    # Process AI reply in the background so webhook responds instantly
                    background_tasks.add_task(handle_ai_conversation, sender_phone, user_text)

    except Exception as e:
        print(f"Webhook processing error: {e}")
        traceback.print_exc()

    # Always return 200 OK immediately
    return Response(content="EVENT_RECEIVED", status_code=200)

async def handle_ai_conversation(sender_phone: str, user_text: str):
    ai_response = generate_ai_reply(user_text)
    await send_whatsapp_message(sender_phone, ai_response)

def generate_ai_reply(user_message: str) -> str:
    try:
        client = genai.Client()
        response = client.models.generate_content(
            model='gemini-2.5-flash',
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
        res = await client.post(url, json=payload, headers=headers)
        print(f"Meta Send Result -> Status: {res.status_code}")
