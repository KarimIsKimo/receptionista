import os
import traceback
import httpx
from fastapi import FastAPI, Request, Response
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

@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token and mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge)
    return Response(content="Verification failed", status_code=403)

@app.post("/webhook")
async def receive_message(request: Request):
    try:
        body = await request.json()
        print("Incoming webhook payload received")
        
        entries = body.get("entry", [])
        if entries:
            messages = entries[0].get("changes", [{}])[0].get("value", {}).get("messages", [])
            if messages:
                incoming_msg = messages[0]
                if incoming_msg.get("type") == "text":
                    sender_phone = incoming_msg.get("from")
                    user_text = incoming_msg.get("text", {}).get("body", "").strip()
                    print(f"Message from {sender_phone}: {user_text}")
                    
                    ai_response = generate_ai_reply(user_text)
                    print(f"Reply generated: {ai_response}")
                    
                    await send_whatsapp_message(sender_phone, ai_response)
    except Exception as e:
        print(f"Webhook processing error: {e}")
        traceback.print_exc()

    return Response(content="EVENT_RECEIVED", status_code=200)

def generate_ai_reply(user_message: str) -> str:
    models_to_try = ['gemini-3.7-flash', 'gemini-2.5-flash', 'gemini-1.5-flash']
    
    for model_name in models_to_try:
        try:
            client = genai.Client()
            response = client.models.generate_content(
                model=model_name,
                contents=user_message,
                config={
                    'system_instruction': CLINIC_SYSTEM_PROMPT,
                    'temperature': 0.3,
                }
            )
            if response and response.text:
                return response.text
        except Exception as e:
            print(f"Model {model_name} failed: {e}")
            continue
            
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
        print(f"Meta Send Result -> Status: {res.status_code}, Body: {res.text}")
