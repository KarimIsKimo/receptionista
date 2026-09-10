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
from pydantic import BaseModel
from google import genai
from google.genai import types

app = FastAPI()

# ---------------------------------------------------------
# MOUNT LOCAL IMAGES DIRECTORY
# ---------------------------------------------------------
app.mount("/images", StaticFiles(directory="images"), name="images")

@app.get("/")
def home():
    return {"status": "Clinic AI Receptionist v2.0 - Human Handoff Active!"}

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "Neckface@2003")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1360825553771801")
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN_HERE")
GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbwI302P_56AN4DB-kd7KLTzD31mxEFQEXzZVZA4UXw1LLlItLBfYvJCrw6XBbLt2_ctuw/exec"
DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("BASE_URL", "https://receptionista.onrender.com")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "jothen123")

BLOCKED_NUMBERS = ["01142286600", "201142286600"]

OFFER_IMAGES = {
    "branches": {"url": f"{BASE_URL}/images/branches.jpg", "caption": "فروعنا وأماكن تواجدنا 📍"},
    "machines": {"url": f"{BASE_URL}/images/machines.jpg", "caption": "أحدث أجهزة إزالة الشعر بالليزر المتوفرة لدينا ⚡"},
    "men_offers": {"url": f"{BASE_URL}/images/men_offers.jpg", "caption": "عروض وباقات ليزر إزالة الشعر المخصصة للرجال 🧔"},
    "women_areas": {"url": f"{BASE_URL}/images/women_areas.jpg", "caption": "أسعار وعروض المناطق المنفردة لليزر السيدات 🌸"},
    "women_packages": {"url": f"{BASE_URL}/images/women_packages.jpg", "caption": "باقات وعروض الليزر الكاملة للسيدات ✨"}
}

client = genai.Client()

# ---------------------------------------------------------
# DB HELPERS
# ---------------------------------------------------------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def is_duplicate_message(message_id: str) -> bool:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,))
                if cursor.fetchone(): return True
                cursor.execute("INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING", (message_id,))
                conn.commit()
                return False
    except Exception:
        return False

def save_chat_turn(phone_number: str, role: str, content: str):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                try:
                    cursor.execute("INSERT INTO chat_history (phone_number, role, content, created_at) VALUES (%s, %s, %s, NOW())", (phone_number, role, content))
                except Exception:
                    conn.rollback()
                    cursor.execute("INSERT INTO chat_history (phone_number, role, content) VALUES (%s, %s, %s)", (phone_number, role, content))
                conn.commit()
    except Exception as e:
        print(f"DB Save Chat Error: {e}")

def load_chat_history(phone_number: str, limit: int = 10):
    history = []
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT role, content FROM chat_history WHERE phone_number = %s ORDER BY id DESC LIMIT %s", (phone_number, limit))
                rows = cursor.fetchall()
        for row in reversed(rows):
            gemini_role = "user" if row["role"] == "user" else "model"
            history.append(types.Content(role=gemini_role, parts=[types.Part.from_text(text=row["content"])]))
    except Exception:
        pass
    return history

def load_patient_profile(phone_number: str) -> dict:
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT name, preferences, is_paused FROM patients WHERE phone_number = %s", (phone_number,))
                row = cursor.fetchone()
                if row:
                    return {"name": row.get("name") or "", "preferences": row.get("preferences") or "", "is_paused": row.get("is_paused", False)}
    except Exception:
        pass
    return {"name": "", "preferences": "", "is_paused": False}

def update_patient_file(phone_number: str, name: str, preferences: str) -> str:
    """Saves or updates long-term patient records."""
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
        return f"حدث خطأ أثناء حفظ الملف: {e}"

# ---------------------------------------------------------
# GEMINI TOOLS (APPOINTMENTS)
# ---------------------------------------------------------
def normalize_to_ampm(time_str: str) -> str:
    time_str = time_str.strip().upper()
    try:
        t = datetime.datetime.strptime(time_str, "%H:%M")
        return t.strftime("%I:%M %p").lstrip("0")
    except ValueError: pass
    try:
        t = datetime.datetime.strptime(time_str, "%I:%M %p")
        return t.strftime("%I:%M %p").lstrip("0")
    except ValueError: pass
    return time_str

def check_schedule(date: str) -> str:
    """Fetches booked appointments for a date (YYYY-MM-DD)."""
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            res = http_client.get(f"{GOOGLE_SHEET_URL}?date={date}", timeout=15.0).json()
            if "booked" in res and not res["booked"]: return f"يوم {date} متاح بالكامل."
            if "booked" in res: return f"المواعيد المحجوزة مسبقاً يوم {date} هي: {', '.join(res['booked'])}"
            return "حدث خطأ."
    except Exception: return "لا يمكن قراءة الجدول الآن."

def check_patient_appointments(phone_number: str) -> str:
    """Checks if the patient already has an upcoming appointment in the system."""
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            res = http_client.get(f"{GOOGLE_SHEET_URL}?phone={phone_number}", timeout=15.0).json()
            if "appointments" in res and res["appointments"]:
                return f"حجوزات العميل الحالية: {', '.join(res['appointments'])}"
            return "لا يوجد حجوزات سابقة أو قادمة لهذا العميل."
    except Exception: return "فشل في قراءة حجوزات العميل."

def cancel_appointment(phone_number: str, date: str) -> str:
    """Cancels a patient's appointment for a specific date (YYYY-MM-DD)."""
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            res = http_client.post(GOOGLE_SHEET_URL, json={"action": "cancel", "phone_number": phone_number, "date": date}, timeout=15.0).json()
            if res.get("deleted"): return f"تم إلغاء الحجز القديم يوم {date} بنجاح."
            return f"لم يتم العثور على حجز لإلغائه في يوم {date}."
    except Exception: return "فشل الاتصال بنظام الإلغاء."

def book_appointment(patient_name: str, phone_number: str, date: str, time: str, area: str) -> str:
    """Saves a clinic appointment."""
    standard_time = normalize_to_ampm(time)
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            res = http_client.post(GOOGLE_SHEET_URL, json={"action": "book", "patient_name": patient_name, "phone_number": phone_number, "date": date, "time": standard_time, "area": area}, timeout=15.0).json()
            if res.get("status") == "error": return f"فشل الحجز: {res.get('message')}"
    except Exception as e: return f"خطأ في الاتصال بنظام الحجز: {e}"
    return f"تم تسجيل الحجز بنجاح باسم {patient_name} يوم {date} الساعة {standard_time} لمنطقة {area}."

# ---------------------------------------------------------
# ADMIN DASHBOARD API
# ---------------------------------------------------------
security = HTTPBasic()
def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, ADMIN_USERNAME) and secrets.compare_digest(credentials.password, ADMIN_PASSWORD)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
    return credentials.username

class PauseRequest(BaseModel):
    phone_number: str
    is_paused: bool

@app.post("/admin/api/toggle_pause")
def toggle_pause(req: PauseRequest, admin: str = Depends(verify_admin)):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO patients (phone_number, is_paused) VALUES (%s, %s)
                    ON CONFLICT (phone_number) DO UPDATE SET is_paused = EXCLUDED.is_paused
                """, (req.phone_number, req.is_paused))
                conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/api/data")
def get_admin_data(admin: str = Depends(verify_admin)):
    patients, chats = [], []
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                try:
                    cursor.execute("SELECT id, phone_number, role, content, COALESCE(created_at, NOW()) AS created_at FROM chat_history ORDER BY id ASC")
                    chats = cursor.fetchall()
                except Exception:
                    conn.rollback()
                    cursor.execute("SELECT id, phone_number, role, content FROM chat_history ORDER BY id ASC")
                    chats = cursor.fetchall()

                try:
                    cursor.execute("""
                        SELECT active.phone_number, COALESCE(p.name, '') AS name, COALESCE(p.preferences, '') AS preferences, 
                               COALESCE(p.is_paused, FALSE) AS is_paused, MAX(c.created_at) AS last_msg_time, MAX(c.id) AS last_msg_id
                        FROM (SELECT DISTINCT phone_number FROM chat_history UNION SELECT phone_number FROM patients) active
                        LEFT JOIN patients p ON active.phone_number = p.phone_number
                        LEFT JOIN chat_history c ON active.phone_number = c.phone_number
                        GROUP BY active.phone_number, p.name, p.preferences, p.is_paused
                        ORDER BY last_msg_time DESC NULLS LAST, last_msg_id DESC NULLS LAST
                    """)
                    patients = cursor.fetchall()
                except Exception:
                    conn.rollback()
                    cursor.execute("SELECT * FROM patients ORDER BY name")
                    patients = cursor.fetchall()
    except Exception as e: print(f"Admin API DB Error: {e}")
    return {"patients": patients, "chats": chats}

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(admin: str = Depends(verify_admin)):
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>لوحة تحكم Jothen Clinic</title>
        <style>
            * { box-sizing: border-box; }
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #e5ddd5; margin: 0; display: flex; height: 100vh; overflow: hidden; }
            .sidebar { width: 380px; min-width: 340px; background: #ffffff; border-left: 1px solid #d1d7db; display: flex; flex-direction: column; height: 100%; }
            .sidebar-header { background: #f0f2f5; padding: 16px 20px; font-weight: bold; font-size: 17px; border-bottom: 1px solid #d1d7db; color: #111b21; }
            .patient-list { overflow-y: auto; flex-grow: 1; }
            .patient-card { padding: 12px 16px; border-bottom: 1px solid #f0f2f5; cursor: pointer; display: flex; flex-direction: column; gap: 4px; transition: background 0.15s ease; }
            .patient-card:hover { background: #f5f6f6; }
            .patient-card.active { background: #ebebeb; }
            .p-header-row { display: flex; justify-content: space-between; align-items: baseline; }
            .p-name { font-weight: 600; color: #111b21; font-size: 15px; }
            .p-time { font-size: 11px; color: #667781; direction: ltr; }
            .p-last-msg { font-size: 13px; color: #667781; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 320px; }
            .p-phone { color: #8696a0; font-size: 12px; direction: ltr; text-align: right; }
            .p-badges { display: flex; gap: 6px; margin-top: 2px; }
            .badge { padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; }
            .badge-human { background: #ffebee; color: #c62828; border: 1px solid #ffcdd2; }
            .chat-area { flex-grow: 1; display: flex; flex-direction: column; background: #efeae2; height: 100%; }
            .chat-header { background: #f0f2f5; padding: 12px 20px; font-size: 16px; border-bottom: 1px solid #d1d7db; display: flex; justify-content: space-between; align-items: center; min-height: 65px; }
            .chat-header-info { display: flex; align-items: center; gap: 12px; }
            .controls { display: flex; gap: 10px; }
            .btn { background: #ffffff; border: 1px solid #d1d7db; border-radius: 6px; padding: 8px 12px; font-size: 13px; cursor: pointer; font-weight: 600; transition: all 0.2s; }
            .btn:hover { background: #f0f2f5; }
            .btn-pause { background: #ffcdd2; color: #b71c1c; border-color: #ef9a9a; }
            .btn-pause:hover { background: #ef9a9a; }
            .btn-resume { background: #c8e6c9; color: #1b5e20; border-color: #a5d6a7; }
            .btn-resume:hover { background: #a5d6a7; }
            .messages { flex-grow: 1; padding: 24px 36px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
            .msg { max-width: 65%; padding: 8px 14px 6px 14px; border-radius: 8px; font-size: 14px; line-height: 1.45; position: relative; box-shadow: 0 1px 1px rgba(0,0,0,0.08); word-break: break-word; }
            .msg.user { background: #ffffff; align-self: flex-start; border-top-right-radius: 0; }
            .msg.model { background: #d9fdd3; align-self: flex-end; border-top-left-radius: 0; }
            .msg-meta { font-size: 10.5px; color: #667781; text-align: left; direction: ltr; margin-top: 3px; }
            .empty-state { margin: auto; text-align: center; color: #8696a0; font-size: 16px; }
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="sidebar-header">المحادثات وملفات المرضى 📁</div>
            <div class="patient-list" id="patient-list"></div>
        </div>
        <div class="chat-area">
            <div class="chat-header" id="chat-header">
                <div class="chat-header-info" id="chat-header-info">
                    <strong style="color: #54656f;">اختر محادثة</strong>
                </div>
                <div class="controls" id="chat-controls" style="display:none;">
                    <button class="btn" id="order-toggle-btn" onclick="toggleOrder()">⬇️ الأحدث بالأسفل</button>
                    <button class="btn" id="pause-btn" onclick="togglePause()"></button>
                </div>
            </div>
            <div class="messages" id="messages"><div class="empty-state">المحادثات الحية ستظهر هنا...</div></div>
        </div>
        <script>
            let allChats = [], allPatients = [], currentPhone = null, autoScroll = true, newestAtTop = false, currentIsPaused = false;
            const messagesDiv = document.getElementById('messages');
            
            messagesDiv.addEventListener('scroll', () => {
                if (!newestAtTop) autoScroll = (messagesDiv.scrollHeight - messagesDiv.scrollTop <= messagesDiv.clientHeight + 60);
            });

            function formatTime(isoString) {
                if (!isoString) return '';
                const d = new Date(isoString);
                if (isNaN(d.getTime())) return '';
                let hours = d.getHours();
                const ampm = hours >= 12 ? 'م' : 'ص';
                hours = hours % 12 || 12;
                const timeStr = `${hours}:${d.getMinutes().toString().padStart(2, '0')} ${ampm}`;
                return d.toDateString() === new Date().toDateString() ? timeStr : `${d.getMonth() + 1}/${d.getDate()} ${timeStr}`;
            }

            function toggleOrder() {
                newestAtTop = !newestAtTop;
                document.getElementById('order-toggle-btn').innerHTML = newestAtTop ? '⬆️ الأحدث بالأعلى' : '⬇️ الأحدث بالأسفل';
                renderActiveChat();
            }

            async function togglePause() {
                if(!currentPhone) return;
                currentIsPaused = !currentIsPaused;
                await fetch('/admin/api/toggle_pause', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone_number: currentPhone, is_paused: currentIsPaused})
                });
                updatePauseButton();
                loadData();
            }

            function updatePauseButton() {
                const btn = document.getElementById('pause-btn');
                if(currentIsPaused) {
                    btn.className = 'btn btn-resume';
                    btn.innerHTML = '▶️ تفعيل البوت (إنهاء التدخل البشري)';
                } else {
                    btn.className = 'btn btn-pause';
                    btn.innerHTML = '⏸️ إيقاف البوت (تدخل بشري)';
                }
            }

            async function loadData() {
                try {
                    const res = await fetch('/admin/api/data');
                    const data = await res.json();
                    allChats = data.chats || [];
                    allPatients = data.patients || [];
                    
                    const pList = document.getElementById('patient-list');
                    pList.innerHTML = '';
                    
                    allPatients.forEach(p => {
                        const div = document.createElement('div');
                        div.className = 'patient-card' + (currentPhone === p.phone_number ? ' active' : '');
                        const patientMsgs = allChats.filter(c => c.phone_number === p.phone_number);
                        const lastMsg = patientMsgs.length > 0 ? patientMsgs[patientMsgs.length - 1].content : '';
                        
                        div.innerHTML = `
                            <div class="p-header-row">
                                <span class="p-name">${p.name || 'مريض جديد'}</span>
                                <span class="p-time">${formatTime(p.last_msg_time)}</span>
                            </div>
                            <div class="p-last-msg">${lastMsg ? (lastMsg.length > 40 ? lastMsg.substring(0,40)+'...' : lastMsg) : 'لا رسائل'}</div>
                            <div class="p-phone">${p.phone_number}</div>
                            <div class="p-badges">
                                ${p.is_paused ? '<span class="badge badge-human">⏸️ تدخل بشري</span>' : ''}
                                ${p.preferences ? `<span style="color:#008069; font-size:11px;">📌 ${p.preferences.replace(/ \| /g, ' • ')}</span>` : ''}
                            </div>
                        `;
                        div.onclick = () => showChat(p.phone_number, p.name || 'مريض جديد', p.is_paused);
                        pList.appendChild(div);
                        if(currentPhone === p.phone_number) currentIsPaused = p.is_paused;
                    });
                    
                    if (currentPhone) {
                        updatePauseButton();
                        renderActiveChat();
                    }
                } catch (err) {}
            }

            function showChat(phone, name, isPaused) {
                currentPhone = phone;
                currentIsPaused = isPaused;
                document.getElementById('chat-header-info').innerHTML = `<strong>${name}</strong><span style="font-size: 13px; color: #667781;" dir="ltr">${phone}</span>`;
                document.getElementById('chat-controls').style.display = 'flex';
                updatePauseButton();
                autoScroll = true;
                renderActiveChat();
            }
            
            function renderActiveChat() {
                if (!currentPhone) return;
                messagesDiv.innerHTML = '';
                let patientChats = allChats.filter(c => c.phone_number === currentPhone);
                if (newestAtTop) patientChats = [...patientChats].reverse();

                patientChats.forEach(c => {
                    const div = document.createElement('div');
                    div.className = `msg ${c.role}`;
                    div.innerHTML = `<div class="msg-text">${c.content.replace(/\\n/g, '<br>')}</div><div class="msg-meta">${formatTime(c.created_at)}</div>`;
                    messagesDiv.appendChild(div);
                });
                
                if (!newestAtTop && autoScroll) messagesDiv.scrollTop = messagesDiv.scrollHeight;
                else if (newestAtTop) messagesDiv.scrollTop = 0;
            }

            loadData(); setInterval(loadData, 5000);
        </script>
    </body>
    </html>
    """

# ---------------------------------------------------------
# WEBHOOK ENDPOINTS
# ---------------------------------------------------------
@app.get("/webhook")
def verify_webhook(request: Request):
    if request.query_params.get("hub.mode") == "subscribe" and request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(content=request.query_params.get("hub.challenge"))
    return Response(content="Verification failed", status_code=403)

@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        entries = body.get("entry", [])
        if entries:
            value = entries[0].get("changes", [{}])[0].get("value", {})
            target_phone_id = value.get("metadata", {}).get("phone_number_id", PHONE_NUMBER_ID)
            if target_phone_id == "979476801911389" and os.getenv("ENABLE_REAL_CLINIC", "true").lower() != "true":
                return Response(content="REAL_NUMBER_PAUSED", status_code=200)

            messages = value.get("messages", [])
            if messages:
                incoming_msg = messages[0]
                if is_duplicate_message(incoming_msg.get("id")): return Response(content="DUPLICATE", status_code=200)

                msg_type = incoming_msg.get("type")
                if msg_type in ["text", "image"]:
                    sender_phone = incoming_msg.get("from", "").strip()
                    user_text = incoming_msg.get("text", {}).get("body", "").strip() if msg_type == "text" else incoming_msg.get("image", {}).get("caption", "[قام المريض بإرسال صورة]").strip()
                    if sender_phone in BLOCKED_NUMBERS or sender_phone.endswith("1142286600"): return Response(content="BLOCKED", status_code=200)
                    
                    background_tasks.add_task(handle_ai_conversation, sender_phone, user_text, target_phone_id)
    except Exception as e: print(e)
    return Response(content="EVENT_RECEIVED", status_code=200)

async def handle_ai_conversation(sender_phone: str, user_text: str, phone_number_id: str):
    save_chat_turn(sender_phone, "user", user_text)
    
    profile = load_patient_profile(sender_phone)
    if profile.get("is_paused"):
        print(f"🛑 Chat with {sender_phone} is paused. Human takes over.")
        return # Skip AI processing, human will answer on WhatsApp

    ai_response, attached_images = generate_ai_reply(sender_phone, user_text, profile)

    if attached_images:
        for img in attached_images: await send_whatsapp_image(sender_phone, img["url"], img["caption"], phone_number_id)
    if ai_response:
        await send_whatsapp_message(sender_phone, ai_response, phone_number_id)
        save_chat_turn(sender_phone, "model", ai_response)

def generate_ai_reply(sender_phone: str, user_message: str, profile: dict):
    try:
        queued_images = []
        def send_clinic_media(media_types: list[str]) -> str:
            valid = [m for m in media_types if m in OFFER_IMAGES]
            for m in valid: queued_images.append(OFFER_IMAGES[m])
            if valid: return f"Images queued: {', '.join(valid)}. Inform patient."
            return "Error: Unknown media."

        system_instruction = f"""
        أنتِ موظفة استقبال ذكية ومحترفة ولطيفة جداً بعيادة Jothen Clinic للتجميل. اسمك "نور".
        تاريخ اليوم: {datetime.datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d")} بتوقيت القاهرة.
        العميل: {profile['name'] or 'عميل جديد'} | الملاحظات: {profile['preferences'] or 'لا يوجد'}

        طريقة الكلام (هام جداً):
        - تحدثي بأسلوب مصري راقي، استخدمي كلمات مثل: "يا فندم"، "تحت أمرك"، "من عيني"، "أهلاً بحضرتك".
        - كوني دافئة ومرحبة، ولا تبدي كإنسان آلي. استخدمي إيموجيز لطيفة (🌸، ✨، 💖).

        تعديل أو إلغاء المواعيد (Rescheduling & Canceling):
        - إذا طلب العميل تعديل أو إلغاء موعده، **أولاً** استخدمي أداة `check_patient_appointments` لتعرفي متى كان موعده بالضبط.
        - إذا أراد الإلغاء تماماً، استخدمي أداة `cancel_appointment`.
        - إذا أراد تغيير الموعد (تأجيل/تقديم)، استخدمي أداة `cancel_appointment` لإلغاء الموعد القديم، ثم `check_schedule` للتأكد من الموعد الجديد، ثم `book_appointment` لحجز الجديد.

        إرسال الصور:
        - استخدمي أداة `send_clinic_media` بقائمة الصور المطلوبة: ['machines', 'women_packages', 'women_areas', 'men_offers', 'branches']. لا تكتبي الروابط أبداً.

        الحجز العادي:
        - جلسة الجسم 45 دقيقة، نصف الجسم 30، المناطق الصغيرة 15. تأكدي بأداة `check_schedule` قبل تأكيد الحجز.
        - لا تحجزي موعدين متعارضين. المواعيد بصيغة AM/PM.
        - احفظي تفضيلات العميل بأداة `update_patient_file`.
        """

        chat = client.chats.create(
            model='gemini-3.6-flash',
            history=load_chat_history(sender_phone, limit=10),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.3,
                tools=[check_schedule, check_patient_appointments, cancel_appointment, book_appointment, update_patient_file, send_clinic_media], 
            )
        )
        return chat.send_message(user_message).text or "", queued_images
    except Exception as e: print(e); return "أهلاً يا فندم! ثواني وهكون مع حضرتك.", []

# ---------------------------------------------------------
# OUTBOUND META MESSAGING
# ---------------------------------------------------------
async def send_whatsapp_message(to: str, text: str, phone_id: str):
    async with httpx.AsyncClient() as c:
        await c.post(f"https://graph.facebook.com/v21.0/{phone_id}/messages", headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})

async def send_whatsapp_image(to: str, url: str, caption: str, phone_id: str):
    async with httpx.AsyncClient() as c:
        await c.post(f"https://graph.facebook.com/v21.0/{phone_id}/messages", headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}, json={"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": url, "caption": caption}})
