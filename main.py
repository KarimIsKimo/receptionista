import os
import traceback
import httpx
import datetime
import secrets
from zoneinfo import ZoneInfo
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, Response, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from google import genai
from google.genai import types

app = FastAPI()

# ---------------------------------------------------------
# MOUNT LOCAL IMAGES DIRECTORY
# ---------------------------------------------------------
app.mount("/images", StaticFiles(directory="images"), name="images")

# --- HOME ROUTE ---
@app.get("/")
def home():
    return {"status": "Clinic AI Receptionist is running with Supabase Cloud DB & Patient CRM!"}

# ---------------------------------------------------------
# CONFIGURATION & ENVIRONMENT VARIABLES
# ---------------------------------------------------------
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "Neckface@2003")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1360825553771801")
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN_HERE")
GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbwI302P_56AN4DB-kd7KLTzD31mxEFQEXzZVZA4UXw1LLlItLBfYvJCrw6XBbLt2_ctuw/exec"
DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("BASE_URL", "https://receptionista.onrender.com")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "jothen123")

# Numbers the bot will completely ignore
BLOCKED_NUMBERS = [
    "01142286600",
    "201142286600"
]

# ---------------------------------------------------------
# CLINIC OFFERS & MEDIA CATALOG
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

def load_patient_profile(phone_number: str) -> dict:
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT name, preferences FROM patients WHERE phone_number = %s", (phone_number,))
                row = cursor.fetchone()
                if row:
                    return {"name": row.get("name") or "", "preferences": row.get("preferences") or ""}
    except Exception as e:
        print(f"DB Load Patient Error: {e}")
    return {"name": "", "preferences": ""}

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
            response = http_client.get(f"{GOOGLE_SHEET_URL}?date={date}", timeout=15.0)
            response.raise_for_status()
            result = response.json()
            if "booked" in result:
                booked_list = result["booked"]
                if not booked_list:
                    return f"يوم {date} متاح بالكامل، لا يوجد حجوزات."
                return f"المواعيد المحجوزة مسبقاً يوم {date} هي: {', '.join(booked_list)}"
            return "حدث خطأ أثناء قراءة الجدول، يرجى المحاولة بعد قليل."
    except httpx.TimeoutException:
        return "النظام يستغرق وقتاً أطول من المعتاد، سأحاول التحقق مرة أخرى."
    except Exception as e:
        print(f"Check Schedule Error: {e}")
        return "لا يمكن قراءة الجدول الآن."

def normalize_to_ampm(time_str: str) -> str:
    """Converts 24h or fuzzy time strings (e.g., '20:00', '8pm') to '8:00 PM'."""
    time_str = time_str.strip().upper()
    try:
        t = datetime.datetime.strptime(time_str, "%H:%M")
        return t.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        pass
    try:
        t = datetime.datetime.strptime(time_str, "%I:%M %p")
        return t.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        pass
    return time_str

def book_appointment(patient_name: str, phone_number: str, date: str, time: str, area: str) -> str:
    """Saves a clinic appointment when patient details and slot are confirmed."""
    standard_time = normalize_to_ampm(time)
    payload = {
        "patient_name": patient_name,
        "phone_number": phone_number,
        "date": date,
        "time": standard_time,
        "area": area
    }
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            response = http_client.post(GOOGLE_SHEET_URL, json=payload, timeout=15.0)
            response.raise_for_status()
            try:
                result = response.json()
                if result.get("status") == "error":
                    return f"فشل الحجز: {result.get('message')}"
            except ValueError:
                pass
    except httpx.TimeoutException:
        return "جاري تأكيد الحجز، يرجى الانتظار قليلاً."
    except Exception as e:
        print(f"Book Appointment Error: {e}")
        return f"خطأ في الاتصال بنظام الحجز: {e}"
    return f"تم تسجيل الحجز بنجاح باسم {patient_name} يوم {date} الساعة {standard_time} لمنطقة {area}."

def update_patient_file(phone_number: str, name: str, preferences: str) -> str:
    """
    Saves or updates long-term patient records: their name, customary areas, and preferences/quirks.
    Call this whenever the user shares their name, preferred areas, or medical/service quirks.
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO patients (phone_number, name, preferences)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (phone_number)
                    DO UPDATE SET
                        name = COALESCE(NULLIF(EXCLUDED.name, ''), patients.name),
                        preferences = CASE 
                            WHEN patients.preferences IS NULL OR patients.preferences = '' THEN EXCLUDED.preferences
                            WHEN EXCLUDED.preferences IS NULL OR EXCLUDED.preferences = '' THEN patients.preferences
                            ELSE patients.preferences || ' | ' || EXCLUDED.preferences
                        END;
                """, (phone_number, name.strip(), preferences.strip()))
                conn.commit()
        return "تم تحديث الملف الدائم للعميل بنجاح."
    except Exception as e:
        print(f"DB Update Patient Error: {e}")
        return f"حدث خطأ أثناء حفظ الملف: {e}"

# ---------------------------------------------------------
# PRIVATE ADMIN DASHBOARD (WEB)
# ---------------------------------------------------------
security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

@app.get("/admin/api/data")
def get_admin_data(admin: str = Depends(verify_admin)):
    patients = []
    chats = []
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT * FROM patients ORDER BY name")
                patients = cursor.fetchall()
                
                cursor.execute("SELECT phone_number, role, content FROM chat_history ORDER BY id ASC")
                chats = cursor.fetchall()
    except Exception as e:
        print(f"Admin API DB Error: {e}")
        
    return {"patients": patients, "chats": chats}

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(admin: str = Depends(verify_admin)):
    html_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>لوحة تحكم Jothen Clinic</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #e5ddd5; margin: 0; display: flex; height: 100vh; }
            .sidebar { width: 350px; background: #ffffff; border-left: 1px solid #ddd; display: flex; flex-direction: column; }
            .sidebar-header { background: #f0f2f5; padding: 20px; font-weight: bold; font-size: 18px; border-bottom: 1px solid #ddd; }
            .patient-list { overflow-y: auto; flex-grow: 1; }
            .patient-card { padding: 15px; border-bottom: 1px solid #f2f2f2; cursor: pointer; display: flex; flex-direction: column; }
            .patient-card:hover { background: #f5f6f6; }
            .p-name { font-weight: bold; color: #111b21; font-size: 16px; margin-bottom: 5px; }
            .p-phone { color: #667781; font-size: 13px; margin-bottom: 5px; direction: ltr; text-align: right; }
            .p-prefs { color: #008069; font-size: 12px; }
            
            .chat-area { flex-grow: 1; display: flex; flex-direction: column; background: #efeae2; }
            .chat-header { background: #f0f2f5; padding: 15px 20px; font-size: 18px; border-bottom: 1px solid #ddd; display: flex; align-items: center; }
            .messages { flex-grow: 1; padding: 30px 50px; overflow-y: auto; display: flex; flex-direction: column; }
            
            .msg { max-width: 65%; padding: 10px 15px; border-radius: 8px; margin-bottom: 12px; font-size: 14px; line-height: 1.5; position: relative; box-shadow: 0 1px 1px rgba(0,0,0,0.1); }
            .msg.user { background: #ffffff; align-self: flex-start; border-top-right-radius: 0; }
            .msg.model { background: #d9fdd3; align-self: flex-end; border-top-left-radius: 0; }
            
            .empty-state { margin: auto; text-align: center; color: #888; font-size: 18px; }
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="sidebar-header">ملفات المرضى 📁</div>
            <div class="patient-list" id="patient-list">
                <div style="padding: 20px; text-align: center; color: #888;">جاري تحميل البيانات...</div>
            </div>
        </div>
        
        <div class="chat-area">
            <div class="chat-header" id="chat-header">
                <strong style="color: #54656f;">اختر مريضاً من القائمة الجانبية لعرض المحادثة</strong>
            </div>
            <div class="messages" id="messages">
                <div class="empty-state">المحادثات الحية ستظهر هنا...</div>
            </div>
        </div>

        <script>
            let allChats = [];
            let currentPhone = null;
            let autoScroll = true;

            const messagesDiv = document.getElementById('messages');
            
            // Detect if user scrolls up to stop auto-scrolling
            messagesDiv.addEventListener('scroll', () => {
                const isAtBottom = messagesDiv.scrollHeight - messagesDiv.scrollTop <= messagesDiv.clientHeight + 50;
                autoScroll = isAtBottom;
            });

            async function loadData() {
                try {
                    const res = await fetch('/admin/api/data');
                    const data = await res.json();
                    allChats = data.chats;
                    
                    const pList = document.getElementById('patient-list');
                    pList.innerHTML = '';
                    
                    if (data.patients.length === 0) {
                        pList.innerHTML = '<div style="padding: 20px; text-align: center; color: #888;">لا يوجد مرضى مسجلين بعد.</div>';
                    }

                    data.patients.forEach(p => {
                        const div = document.createElement('div');
                        div.className = 'patient-card';
                        
                        const name = p.name || 'مريض غير معروف';
                        const prefs = p.preferences ? p.preferences.replace(/ \| /g, ' • ') : 'لا توجد ملاحظات مسجلة';
                        
                        div.innerHTML = `
                            <div class="p-name">${name}</div>
                            <div class="p-phone">${p.phone_number}</div>
                            <div class="p-prefs">${prefs}</div>
                        `;
                        div.onclick = () => showChat(p.phone_number, name);
                        
                        // Highlight active chat
                        if (currentPhone === p.phone_number) {
                            div.style.background = '#ebebeb';
                        }
                        
                        pList.appendChild(div);
                    });
                    
                    // Refresh active chat window if one is open
                    if (currentPhone) {
                        renderActiveChat();
                    }
                } catch (err) {
                    console.error("Failed to load dashboard data", err);
                }
            }

            function showChat(phone, name) {
                currentPhone = phone;
                document.getElementById('chat-header').innerHTML = `<strong>${name}</strong>&nbsp; &nbsp; <span style="font-size: 14px; color: #667781;" dir="ltr">${phone}</span>`;
                autoScroll = true; // Force scroll to bottom on new selection
                renderActiveChat();
            }
            
            function renderActiveChat() {
                if (!currentPhone) return;
                
                messagesDiv.innerHTML = '';
                const patientChats = allChats.filter(c => c.phone_number === currentPhone);
                
                if (patientChats.length === 0) {
                    messagesDiv.innerHTML = '<div class="empty-state">لا توجد رسائل سابقة.</div>';
                    return;
                }

                patientChats.forEach(c => {
                    const div = document.createElement('div');
                    div.className = `msg ${c.role}`;
                    div.innerHTML = c.content.replace(/\\n/g, '<br>');
                    messagesDiv.appendChild(div);
                });
                
                if (autoScroll) {
                    messagesDiv.scrollTop = messagesDiv.scrollHeight;
                }
            }

            // Fetch immediately, then every 5 seconds
            loadData();
            setInterval(loadData, 5000);
        </script>
    </body>
    </html>
    """
    return html_content

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
                    sender_phone = incoming_msg.get("from", "").strip()
                    user_text = incoming_msg.get("text", {}).get("body", "").strip()

                    # ---------------------------------------------------------
                    # BLACKLIST CHECK: Ignore blocked phone numbers completely
                    # ---------------------------------------------------------
                    if sender_phone in BLOCKED_NUMBERS or sender_phone.endswith("1142286600"):
                        print(f"🚫 Ignored blocked number: {sender_phone}")
                        return Response(content="BLOCKED_NUMBER_IGNORED", status_code=200)

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

    # 2. Generate response with conversation history and patient profile
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

        # Load long-term profile from Supabase
        profile = load_patient_profile(sender_phone)
        known_name = profile["name"] or "غير معروف بعد"
        known_preferences = profile["preferences"] or "لا توجد تفضيلات مسجلة بعد"

        # Build image catalog instructions
        image_instructions = ""
        for key, data in OFFER_IMAGES.items():
            image_instructions += f"- للسؤال عن ({key}): اطبعي هذا السطر بالضبط في بداية ردك:\nATTACH_IMAGE::{data['url']}::{data['caption']}\n"

        system_instruction = f"""
        أنتِ موظفة استقبال ذكية ومساعدة افتراضية لعيادة Jothen Clinic للتجميل والليزر.
        تاريخ اليوم: {today_date} بتوقيت القاهرة.
        رقم هاتف العميل: {sender_phone}

        الملف الدائم للعميل (ذاكرة العيادة):
        - اسم العميل المسجل: {known_name}
        - التفضيلات والملاحظات المحفوظة: {known_preferences}

        تعليمات هامة للتعرف على جنس وهوية العميل:
        - استنتجي جنس العميل من اسمه المسجل أو الاسم الجديد الذي يذكره.
        - إذا كان العميل ذكراً (مثل: كريم، أحمد، محمد)، خاطبيه بالمذكر، وإذا طلب العروض اعرضي عروض الرجال ('men_offers').
        - إذا كانت العميل أنثى، خاطبيها بالمؤنث.
        - إذا لم يُذكر الاسم حتى الآن، تحدثي بلباقة واطلبي منه التعرف على اسمه.

        تعليمات حفظ الملف الدائم (أداة update_patient_file):
        - استخدمي أداة update_patient_file فور معرفتك لاسم العميل، أو المناطق التي يفضل عمل جلسات لها دائماً، أو أي ملاحظات هامة (مثل: بشرة حساسة، مواعيد مفضلة، تفضيل أخصائي معين).
        - مرري رقم الهاتف: {sender_phone} عند استدعاء الأداة.

        معلومات العيادة:
        {clinic_knowledge}

        تعليمات إرسال الصور (هام جداً):
        لإرسال صورة للعميل، **يجب** أن تطبعي الكود الخاص بها في السطر الأول من رسالتك:
        {image_instructions}

        تعليمات الحجز:
        - جلسة الجسم الكامل: 45 دقيقة.
        - نصف الجسم: 30 دقيقة.
        - المناطق الصغيرة: 15 دقيقة.
        - افحصي الحجوزات بأداة check_schedule قبل اقتراح أي موعد.
        - لا تؤكدي الحجز بأداة book_appointment إلا بعد الموافقة الصريحة للعميل على الاسم والتاريخ والوقت والمنطقة.
        
        تعليمات فحص وحجز المواعيد لمنع التضارب:
        - كل المواعيد في الجدول مسجلة بصيغة (AM / PM) مثل: "8:00 PM".
        - استدعي أداة check_schedule قبل اقتراح أو تأكيد أي موعد.
        - إذا وجدت موعداً محجوزاً في نفس التوقيت (مثلاً 8:00 PM أو 20:00 متطابقان تماماً):
          * اقترحي موعداً بديلاً متاحاً (مثلاً 9:00 PM).
        - جلسة الجسم الكامل = 45 دقيقة، نصف الجسم = 30 دقيقة، المناطق الصغيرة = 15 دقيقة. لا تحجزي موعدين متعارضين في نفس الوقت نهائياً.
        """

        past_contents = load_chat_history(sender_phone, limit=10)

        chat = client.chats.create(
            model='gemini-3.6-flash',
            history=past_contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
                tools=[check_schedule, book_appointment, update_patient_file], 
            )
        )

        response = chat.send_message(user_message)
        response_text = response.text or ""
        print(f"🤖 AI RAW TEXT: {response_text}")

        # Extract image if present
        attached_image = None
        if "ATTACH_IMAGE::" in response_text:
            parts = response_text.split("ATTACH_IMAGE::", 1)
            meta_and_text = parts[1].split("\n", 1)
            image_meta = meta_and_text[0].split("::")
            attached_image = {
                "url": image_meta[0].strip(),
                "caption": image_meta[1].strip() if len(image_meta) > 1 else ""
            }
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
