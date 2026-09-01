import os
import traceback
import httpx
import datetime
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

# 1. Initialize the Gemini client ONCE globally so it stays open
# Ensure GEMINI_API_KEY is set in your Render environment variables!
client = genai.Client()

# Track processed IDs so retries are ignored
processed_message_ids = set()

# Dictionary to store active chat sessions for each phone number
active_chats = {}

def load_clinic_rules() -> str:
    """Reads clinic rules directly from clinic_rules.txt"""
    try:
        with open("clinic_rules.txt", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"Could not load clinic_rules.txt: {e}")
        return "مواعيد العمل من السبت للخميس من 1 ظهراً لـ 10 مساءً."

# --- UPDATED TOOL FUNCTION ---
def book_appointment(patient_name: str, phone_number: str, date: str, time: str, area: str) -> str:
    """
    Saves a clinic appointment. 
    Use this tool ONLY when the patient has confirmed the date, time, AND the laser area.
    IMPORTANT: The 'date' parameter MUST be formatted as YYYY-MM-DD.
    """
    print(f"🟢 SENDING TO GOOGLE SHEETS: {patient_name} ({phone_number}) on {date} at {time} for {area}")
    
    GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbwI302P_56AN4DB-kd7KLTzD31mxEFQEXzZVZA4UXw1LLlItLBfYvJCrw6XBbLt2_ctuw/exec"
    
    payload = {
        "patient_name": patient_name,
        "phone_number": phone_number,
        "date": date,
        "time": time,
        "area": area
    }
    
    try:
        # follow_redirects=True is required for Google Apps Script Web Apps
        with httpx.Client(follow_redirects=True) as http_client:
            response = http_client.post(GOOGLE_SHEET_URL, json=payload, timeout=10.0)
            print(f"Google Sheets HTTP Status: {response.status_code}")
            print(f"Google Sheets Raw Response: {response.text}")
            
            # Parse the response to check if it was rejected due to double-booking
            try:
                result = response.json()
                if result.get("status") == "error":
                    if "Slot already taken" in result.get("message", ""):
                        return f"فشل الحجز: الموعد يوم {date} الساعة {time} محجوز مسبقاً. اعتذر للمريضة واطلب منها اختيار موعد آخر."
                    return f"حدث خطأ أثناء حفظ الحجز: {result.get('message')}"
            except ValueError:
                print(f"Google Sheets returned non-JSON response: {response.text}")
                
    except Exception as e:
        print(f"Failed to send to Google Sheets: {e}")
        return "حدث خطأ في الاتصال بنظام الحجز، يرجى المحاولة لاحقاً."
    
    return f"تم تسجيل الحجز بنجاح باسم {patient_name} يوم {date} الساعة {time} لمنطقة {area}."
# -------------------------

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
    # Pass the sender's phone number into the AI function so it knows who is talking
    ai_response = generate_ai_reply(sender_phone, user_text)
    await send_whatsapp_message(sender_phone, ai_response)

def generate_ai_reply(sender_phone: str, user_message: str) -> str:
    try:
        clinic_knowledge = load_clinic_rules()
        today_date = datetime.date.today().strftime("%Y-%m-%d")
        
        system_instruction = f"""
        أنت موظف استقبال ذكي ومساعد افتراضي للعيادة.
        تاريخ اليوم هو: {today_date}
        رقم هاتف المريضة الحالي هو: {sender_phone}

        مهمتك الرد على استفسارات المرضى ومساعدتهم في الحجز.

        القواعد والمعلومات الخاصة بالعيادة:
        {clinic_knowledge}
        
        تعليمات هامة جداً قبل الحجز:
        يجب أن تسألي المريضة عن المنطقة التي تريد عمل ليزر لها (مثل: الوجه، البكيني، الجسم كامل، إلخ) قبل تأكيد الحجز.
        لا تقومي بتأكيد الحجز باستخدام الأداة إلا بعد معرفة كل التفاصيل: (الاسم، اليوم، الساعة، والمنطقة).
        استخدمي رقم هاتف المريضة الحالي المرفق أعلاه عند استخدام أداة الحجز.
        """
        
        # Check if this patient already has an active conversation going
        if sender_phone not in active_chats:
            # If not, create a new chat session with the clinic rules and tools
            active_chats[sender_phone] = client.chats.create(
                model='gemini-3.5-flash-lite', 
                config={
                    'system_instruction': system_instruction,
                    'temperature': 0.3,
                    'tools': [book_appointment],
                }
            )
        
        # Send the user's message to their specific chat history
        chat = active_chats[sender_phone]
        response = chat.send_message(user_message)
        
        return response.text
        
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return "أهلاً بحضرتك يا فندم! شكراً لتواصلك مع العيادة، سيقوم أحد مسؤولي الاستقبال بالرد عليكي في أقرب وقت."

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
    
    # Using httpx.AsyncClient here for web requests (unrelated to the genai client)
    async with httpx.AsyncClient() as http_client:
        res = await http_client.post(url, json=payload, headers=headers)
        print(f"Meta Send Result -> Status: {res.status_code}")
