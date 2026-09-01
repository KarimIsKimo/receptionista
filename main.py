import os
import traceback
import httpx
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import PlainTextResponse
from google import genai

app = FastAPI()

# ---------------------------------------------------------
# SECURITY BEST PRACTICE: Keys loaded securely from Render Environment
# ---------------------------------------------------------
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "Neckface@2003")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1360825553771801")
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN_HERE")
# ---------------------------------------------------------

# Track processed IDs so retries are ignored
processed_message_ids = set()

def load_clinic_rules() -> str:
    """Reads clinic rules directly from clinic_rules.txt"""
    try:
        with open("clinic_rules.txt", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"Could not load clinic_rules.txt: {e}")
        return "مواعيد العمل من السبت للخميس من 9 صباحاً لـ 9 مساءً."

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
        clinic_knowledge = load_clinic_rules()
        
        system_instruction = f"""
        أنت موظف استقبال ذكي ومساعد افتراضي للعيادة.
        مهمتك الرد على استفسارات المرضى ومساعدتهم في الحجز.

        القواعد والمعلومات الخاصة بالعيادة:
        {clinic_knowledge}

        تعليمات الأسلوب واللهجة:
        1. تحدث باللهجة المصرية العامية المهذبة والودودة (استخدم عبارات مثل: أهلاً بحضرتك، يا فندم، تمام، تحت أمرك).
        2. التزم تماماً بالمعلومات الموجودة في القواعد ولا تختلق أسعار أو مواعيد غير موجودة.
        3. اجعل الإجابات مختصرة ومفيدة (2 إلى 3 جمل كحد أقصى).
        4. لا تقدم أي استشارات طبية تشخيصية أو ترشيحات أدوية.
        """

        # Ensure GEMINI_API_KEY is saved in your Render Environment Variables
        client = genai.Client()
        response = client.models.generate_content(
            model='gemini-3.5-flash-lite', # Updated to the high-volume free model
            contents=user_message,
            config={
                'system_instruction': system_instruction,
                'temperature': 0.3,
            }
        )
        return response.text
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return "أهلاً بحضرتك! شكراً لتواصلك مع العيادة، سيقوم أحد مسؤولي الاستقبال بالرد عليك في أقرب وقت."

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
