import os
import traceback
import httpx
import datetime
from zoneinfo import ZoneInfo
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types

app = FastAPI()

# ---------------------------------------------------------
# MOUNT LOCAL IMAGES DIRECTORY
# This makes the files in your GitHub 'images' folder public
# ---------------------------------------------------------
app.mount("/images", StaticFiles(directory="images"), name="images")

# --- HOME ROUTE ---
@app.get("/")
def home():
    return {"status": "Clinic AI Receptionist is running with Supabase Cloud DB & Local Images!"}

# ---------------------------------------------------------
# CONFIGURATION & ENVIRONMENT VARIABLES
# ---------------------------------------------------------
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "Neckface@2003")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1360825553771801")
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN_HERE")
GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbwI302P_56AN4DB-kd7KLTzD31mxEFQEXzZVZA4UXw1LLlItLBfYvJCrw6XBbLt2_ctuw/exec"
DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("BASE_URL", "https://receptionista.onrender.com")

# ---------------------------------------------------------
# CLINIC OFFERS & MEDIA CATALOG (Mapped to your GitHub folder)
# ---------------------------------------------------------
OFFER_IMAGES = {
    "branches": {
        "url": f"{BASE_URL}/images/branches.jpg",
        "caption": "فروع Jothen Clinic وأماكن تواجدنا 📍"
    },
    "machines": {
        "url": f"{BASE_URL}/images/machines.jpg",
        "caption": "أحدث أجهزة إزالة الشعر بالليزر المتوفرة لدينا في العيادة ⚡"
    },
    "men_offers": {
        "url": f"{BASE_URL}/images/men_offers.jpg",
        "caption": "عروض وباقات ليزر إزالة الشعر المخصصة للرجال 🧔"
    },
    "women_areas": {
        "url": f"{BASE_URL}/images/women_areas.jpg",
        "caption": "أسعار وعروض المناطق المنفردة لليزر السيدات 🌸"
    },
    "women_packages": {
        "url": f"{BASE_URL}/images/women_packages.jpg",
        "caption": "باقات وعروض الليزر الكاملة للسيدات ✨"
    }
}

client = genai.Client()

# ---------------------------------------------------------
# SUPABASE POSTGRESQL DATABASE HELPERS
# ---------------------------------------------------------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def is_duplicate_message(message_id: str) -> bool:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,))
                if cursor.fetchone():
                    return True
                cursor.execute("INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING", (message_id,))
                conn.commit()
                return False
    except Exception as e:
        print(f"DB Deduplication Error: {e}")
        return False

def save_chat_turn(phone_number: str, role: str, content: str):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO chat_history (phone_number, role, content) VALUES (%s, %s, %s)",
                    (phone_number, role, content)
                )
                conn.commit()
    except Exception as e:
        print(f"DB Save Chat Error: {e}")

def load_chat_history(phone_number: str, limit: int = 10):
    history = []
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT role, content FROM chat_history 
                    WHERE phone_number = %s 
                    ORDER BY id DESC LIMIT %s
                """, (phone_number, limit))
                rows = cursor.fetchall()
                
        for row in reversed(rows):
            gemini_role = "user" if row["role"] == "user" else "model"
            history.append(types.Content(
                role=gemini_role,
                parts=[types.Part.from_text(text=row["content"])]
            ))
    except Exception as e:
        print(f"DB Load Chat Error: {e}")
    return history

def load_clinic_rules() -> str:
    try:
        with open("clinic_rules.txt", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"Could not load clinic_rules.txt: {e}")
        return "مواعيد العمل من السبت للخميس من 1 ظهراً لـ 10 مساءً."

# ---------------------------------------------------------
# TOOLS FOR GEMINI
# ---------------------------------------------------------
def check_schedule(date: str) -> str:
    """Fetches booked appointments for a date (YYYY-MM-DD)."""
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            response = http_client.get(f"{GOOGLE_SHEET_URL}?date={date}", timeout=10.0)
            result = response.json()
            if "booked" in result:
                booked_list = result["booked"]
                if not booked_list:
                    return f"يوم {date} متاح بالكامل، لا يوجد حجوزات."
                return f"المواعيد المحجوزة مسبقاً يوم {date} هي: {', '.join(booked_list)}"
            return "حدث خطأ أثناء قراءة الجدول."
    except Exception as e:
        return f"لا يمكن قراءة الجدول الآن: {e}"

def book_appointment(patient_name: str, phone_number: str, date: str, time: str, area: str) -> str:
    """Saves a clinic appointment when patient details and slot are confirmed."""
    payload = {
        "patient_name": patient_name,
        "phone_number": phone_number,
        "date": date,
        "time": time,
        "area": area
    }
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            response = http_client.post(GOOGLE_SHEET_URL, json=payload, timeout=10.0)
            try:
                result = response.json()
                if result.get("status") == "error":
                    return f"فشل الحجز: {result.get('message')}"
            except ValueError:
                pass
    except Exception as e:
        return f"خطأ في الاتصال بنظام الحجز: {e}"
    return f"تم تسجيل الحجز بنجاح باسم {patient_name} يوم {date} الساعة {time} لمنطقة {area}."

# ---------------------------------------------------------
# WEBHOOK ENDPOINTS
# ---------------------------------------------------------
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
        print(f"📥 RAW WEBHOOK PAYLOAD: {body}")

        entries = body.get("entry", [])
        if entries:
            value = entries[0].get("changes", [{}])[0].get("value", {})
            metadata = value.get("metadata", {})
            target_phone_id = metadata.get("phone_number_id", PHONE_NUMBER_ID)

            enable_real_clinic = os.getenv("ENABLE_REAL_CLINIC", "true").lower() == "true"
            if target_phone_id == "979476801911389" and not enable_real_clinic:
                return Response(content="REAL_NUMBER_PAUSED", status_code=200)

            messages = value.get("messages", [])
            if messages:
                incoming_msg = messages[0]
                message_id = incoming_msg.get("id")

                # Deduplication via Supabase PostgreSQL
                if is_duplicate_message(message_id):
                    return Response(content="DUPLICATE_IGNORED", status_code=200)

                if incoming_msg.get("type") == "text":
                    sender_phone = incoming_msg.get("from")
                    user_text = incoming_msg.get("text", {}).get("body", "").strip()

                    background_tasks.add_task(
                        handle_ai_conversation, sender_phone, user_text, target_phone_id
                    )

    except Exception as e:
        print(f"Webhook processing error: {e}")
        traceback.print_exc()

    return Response(content="EVENT_RECEIVED", status_code=200)

async def handle_ai_conversation(sender_phone: str, user_text: str, phone_number_id: str):
    # 1. Save inbound patient message to Supabase
    save_chat_turn(sender_phone, "user", user_text)

    # 2. Generate response with conversation history from Supabase
    ai_response, attached_image = generate_ai_reply(sender_phone, user_text)

    # 3. Send offer flyer image if requested
    if attached_image:
        await send_whatsapp_image(
            recipient_phone=sender_phone,
            image_url=attached_image["url"],
            caption=attached_image["caption"],
            phone_number_id=phone_number_id
        )

    # 4. Send text response and save to Supabase
    if ai_response:
        await send_whatsapp_message(sender_phone, ai_response, phone_number_id)
        save_chat_turn(sender_phone, "model", ai_response)

def generate_ai_reply(sender_phone: str, user_message: str):
    try:
        clinic_knowledge = load_clinic_rules()
        today_date = datetime.datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d")

        # 1. Build the image catalog text dynamically for the prompt
        image_instructions = ""
        for key, data in OFFER_IMAGES.items():
            image_instructions += f"- للسؤال عن ({key}): اطبعي هذا السطر بالضبط في بداية ردك:\nATTACH_IMAGE::{data['url']}::{data['caption']}\n"

        system_instruction = f"""
        أنتِ موظفة استقبال ذكية ومساعدة افتراضية لعيادة Jothen Clinic للتجميل والليزر.
        تاريخ اليوم: {today_date} بتوقيت القاهرة.
        رقم العميل: {sender_phone}

        تعليمات هامة للتعرف على جنس العميل:
        - استنتجي جنس العميل من اسمه (ذكر أم أنثى).
        - إذا كان العميل ذكراً (مثل: كريم، أحمد، محمد)، تحدثي معه بصيغة المذكر، وإذا سأل عن العروض اعرضي عليه عروض الرجال ('men_offers').
        - إذا كانت العميل أنثى، تحدثي معها بصيغة المؤنث.
        - إذا لم يذكر العميل اسمه بعد، تحدثي بصيغة محايدة ولبقة واطلبي منه التعرف على اسمه.

        معلومات العيادة:
        {clinic_knowledge}

        تعليمات إرسال الصور (هام جداً):
        لإرسال صورة للعميل، **يجب** أن تطبعي الكود الخاص بها في السطر الأول من رسالتك.
        الأكواد المتاحة:
        {image_instructions}

        مثال للرد الصحيح:
        ATTACH_IMAGE::{BASE_URL}/images/women_packages.jpg::باقات الليزر
        أهلاً بك يا فندم، هذه هي أفضل باقات وعروض الليزر المتوفرة لدينا...

        تعليمات الحجز:
        - جلسة الجسم الكامل: 45 دقيقة.
        - نصف الجسم: 30 دقيقة.
        - المناطق الصغيرة: 15 دقيقة.
        - افحصي الحجوزات بأداة check_schedule قبل اقتراح أي موعد.
        - لا تؤكدي الحجز بأداة book_appointment إلا بعد الموافقة الصريحة للعميل.
        """

        past_contents = load_chat_history(sender_phone, limit=10)

        chat = client.chats.create(
            model='gemini-3.6-flash',
            history=past_contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
                tools=[check_schedule, book_appointment], 
            )
        )

        response = chat.send_message(user_message)
        response_text = response.text or ""
        print(f"🤖 AI RAW TEXT: {response_text}")

        # 3. Smarter text parsing to extract the image
        attached_image = None
        if "ATTACH_IMAGE::" in response_text:
            parts = response_text.split("ATTACH_IMAGE::", 1)
            
            # Split the hidden code line from the rest of the natural conversation
            meta_and_text = parts[1].split("\n", 1)
            
            image_meta = meta_and_text[0].split("::")
            attached_image = {
                "url": image_meta[0].strip(),
                "caption": image_meta[1].strip() if len(image_meta) > 1 else ""
            }
            
            # Keep only the human-friendly text to send via WhatsApp
            response_text = meta_and_text[1].strip() if len(meta_and_text) > 1 else "إليك التفاصيل:"

        return response_text, attached_image

    except Exception as e:
        print(f"Gemini API Error: {e}")
        return "أهلاً بحضرتك يا فندم! شكراً لتواصلك مع العيادة، سيقوم أحد مسؤولي الاستقبال بالرد عليكي في أقرب وقت.", None

# ---------------------------------------------------------
# OUTBOUND MESSAGING
# ---------------------------------------------------------
async def send_whatsapp_message(recipient_phone: str, text_content: str, phone_number_id: str):
    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
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
    async with httpx.AsyncClient() as http_client:
        res = await http_client.post(url, json=payload, headers=headers)
        print(f"Meta Send Text ({phone_number_id}) -> Status: {res.status_code}")

async def send_whatsapp_image(recipient_phone: str, image_url: str, caption: str, phone_number_id: str):
    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "image",
        "image": {
            "link": image_url,
            "caption": caption
        }
    }
    async with httpx.AsyncClient() as http_client:
        res = await http_client.post(url, json=payload, headers=headers)
        print(f"Meta Send Image ({phone_number_id}) -> Status: {res.status_code}")
