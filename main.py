import os
import traceback
import httpx
import datetime
import secrets
import asyncio
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
    return {"status": "Jothen Clinic Nasr City AI Receptionist - Active"}

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

# ⚠️ CHANGE THIS to the Manager/Doctor's phone number to receive alerts (Format: CountryCode + Number)
STAFF_NOTIFICATION_PHONE = os.getenv("STAFF_NOTIFICATION_PHONE", "201026438897")

OFFER_IMAGES = {
    "branches": {"url": f"{BASE_URL}/images/branches.jpg", "caption": "فروعنا وأماكن تواجدنا 📍"},
    "machines": {"url": f"{BASE_URL}/images/machines.jpg", "caption": "أحدث أجهزة إزالة الشعر بالليزر المتوفرة لدينا ⚡"},
    "men_offers": {"url": f"{BASE_URL}/images/men_offers.jpg", "caption": "عروض وباقات ليزر إزالة الشعر المخصصة للرجال 🧔"},
    "women_areas": {"url": f"{BASE_URL}/images/women_areas.jpg", "caption": "أسعار وعروض المناطق المنفردة لليزر السيدات 🌸"},
    "women_packages": {"url": f"{BASE_URL}/images/women_packages.jpg", "caption": "باقات وعروض الليزر الكاملة للسيدات ✨"}
}

client = genai.Client()
user_locks = {}

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
            return "حدث خطأ في قراءة الجدول."
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
    """Saves a clinic appointment for the Nasr City branch."""
    standard_time = normalize_to_ampm(time)
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            payload = {
                "action": "book",
                "patient_name": patient_name,
                "phone_number": phone_number,
                "branch": "مدينة نصر",
                "date": date,
                "time": standard_time,
                "area": area
            }
            res = http_client.post(GOOGLE_SHEET_URL, json=payload, timeout=15.0).json()
            if res.get("status") == "error": return f"فشل الحجز: {res.get('message')}"
    except Exception as e: return f"خطأ في الاتصال بنظام الحجز: {e}"
    return f"تم تسجيل الحجز بنجاح باسم {patient_name} بفرع مدينة نصر يوم {date} الساعة {standard_time} لمنطقة {area}."

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
        <title>لوحة تحكم عيادة جوثن - مدينة نصر</title>
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
    except Exception as e: 
        print(f"Webhook error: {e}")
    return Response(content="EVENT_RECEIVED", status_code=200)

async def handle_ai_conversation(sender_phone: str, user_text: str, phone_number_id: str):
    if sender_phone not in user_locks:
        user_locks[sender_phone] = asyncio.Lock()
    
    async with user_locks[sender_phone]:
        save_chat_turn(sender_phone, "user", user_text)
        
        profile = load_patient_profile(sender_phone)
        if profile.get("is_paused"):
            print(f"🛑 Chat with {sender_phone} is manually paused by admin. Human takes over.")
            return

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
            """Sends one or more clinic media cards: 'branches', 'machines', 'men_offers', 'women_areas', 'women_packages'."""
            valid = [m for m in media_types if m in OFFER_IMAGES]
            for m in valid: queued_images.append(OFFER_IMAGES[m])
            if valid: return f"Images queued: {', '.join(valid)}. Inform patient."
            return "Error: Unknown media."

        def notify_staff(issue_summary: str) -> str:
            """Sends an alert to clinic management regarding a patient's medical question, complaint, or unlisted price request, WITHOUT pausing the bot."""
            try:
                alert_body = f"🚨 *تنبيه استفسار يحتاج متابعة*\n\n📱 *رقم المريض:* {sender_phone}\n📝 *المشكلة:* {issue_summary}\n\n_البوت مستمر في الرد ولم يتوقف._"
                with httpx.Client() as c:
                    c.post(
                        f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages",
                        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                        json={
                            "messaging_product": "whatsapp",
                            "to": STAFF_NOTIFICATION_PHONE,
                            "type": "text",
                            "text": {"body": alert_body}
                        },
                        timeout=10.0
                    )
                return "تم الإرسال للإدارة بنجاح. أخبري المريض بلطف واستمري في المحادثة."
            except Exception as e:
                print(f"Failed to notify staff: {e}")
                return "تم تسجيل الطلب."

        now_cairo = datetime.datetime.now(ZoneInfo("Africa/Cairo"))
        arabic_days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
        today_day_name = arabic_days[now_cairo.weekday()]
        today_date = f"{now_cairo.strftime('%Y-%m-%d')} (اليوم هو: {today_day_name})"

        system_instruction = f"""
        <role_definition>
        أنتِ "نور"، موظفة استقبال ذكية ولطيفة في "عيادات جوثن" (Jothen Clinics).
        مهمتك الوحيدة: خدمة عملاء فرع "مدينة نصر" فقط، وحجز مواعيد ليزر إزالة الشعر.
        العميل الحالي: {profile['name'] or 'عميل جديد'} (رقم الهاتف: {sender_phone})
        تاريخ اليوم: {today_date}
        </role_definition>

              <hard_constraints>
        1. IF user asks about location -> REPLY EXACTLY: "عيادة 104، 8 ش الدكتور حسن الشريف، مدينة نصر" AND trigger send_clinic_media(['branches']). NEVER mention other locations.
        2. IF user asks for phone number -> NEVER ask. You already know it is {sender_phone}.
        
        3. IF user asks routine laser prep or general FAQ questions (e.g., shaving/شيفنج, numbing cream, sun exposure, number of sessions, gaps, post-care, pain, cooling) -> ANSWER directly using <laser_faqs_and_prep>.
           
        4. IF user asks complex Medical Advice that is NOT in the FAQs (e.g., burns, pregnancy, specific medications), Doctors, Botox, Filler, Dermatology, or has a complaint -> TRIGGER notify_staff(issue_summary) IMMEDIATELY in the background.
           - CRITICAL: DO NOT tell the patient that management or a doctor will contact them.
           - INSTEAD, just politely apologize that your role is limited to laser bookings. (e.g., "عذراً يا فندم، أنا مسؤولة بس عن حجوزات ومواعيد الليزر ومقدرش أفيد حضرتك طبياً في النقطة دي، أقدر أساعدك في حجز موعد؟").
           
        5. IF user asks for a price NOT listed in <knowledge_base> -> TRIGGER notify_staff(issue_summary="استفسار عن سعر غير مسجل") in the background. 
           - CRITICAL: DO NOT say you are checking with management.
           - INSTEAD, politely state that you only have the standard packages available. (e.g., "عذراً يا فندم، دي كل باقات وعروض الليزر المتاحة عندي حالياً، تحبي أساعدك في حجز أي باقة منهم؟").
           
        6. IF user asks to book on Friday -> REJECT. Friday is a holiday.
        7. IF user asks to book outside 12:00 PM to 10:00 PM -> REJECT. Request a valid time.
        8. IF the user's message is ambiguous, confusing, or contains typos (e.g., "back 5") -> Politely ask the user to clarify what they mean.
        </hard_constraints>


        <knowledge_base>
        <laser_faqs_and_prep>
        - الشيفنج (Shaving): نعم يا فندم، لازم يتم إزالة الشعر بالشفرة (الشيفنج) في نفس يوم الجلسة أو قبلها بيوم، وممنوع تماماً استخدام السويت أو الشمع أو الفتلة.
        - المخدر (Numbing Cream): متاح استخدام كريم مخدر قبل الجلسة بنصف أو ساعة للمناطق الحساسة.
        - الشمس (Sun Exposure): يفضل عدم التعرض المباشر للشمس أو عمل تان (Tan) قبل وبعد الجلسة بأسبوعين.
        - عدد الجلسات (Number of Sessions): في المتوسط بنحتاج من 6 لـ 8 جلسات، لكن العدد النهائي بيختلف من شخص للتاني حسب طبيعة الجسم وسمك الشعر.
        - الفرق بين الجلسات (Time Between Sessions): الجلسات بتكون كل 3 لـ 4 أسابيع للوجه، وكل 4 لـ 6 أسابيع لباقي مناطق الجسم.
        - العناية بعد الجلسة (Post-Care): بننصح باستخدام كريم مرطب طبي ومضاد حيوي بعد الجلسة مباشرة لتجنب أي التهاب، وممنوع تماماً استخدام أي عطور، مزيلات عرق، أو مقشرات على المنطقة لمدة 48 ساعة.
        - الألم والتبريد (Pain / Cooling): أجهزتنا مزودة بأقوى نظام تبريد مزدوج بيخلي الجلسة مريحة جداً وبدون ألم، مجرد لسعة خفيفة جداً ومحتملة.
        </laser_faqs_and_prep>

        <prices_women>
        - باقات النبضات: 1000 نبضة (800ج)، 2000 نبضة (1500ج)، 3000 نبضة (2000ج)، 5000 نبضة (3000ج)، 7000 نبضة (3500ج)، 10000 نبضة (5000ج).
        - عرض: 4 جلسات أندر آرم أو بيكيني بخصم 10%.
        - أندر آرم (150ج)، بيكيني+لاين (300ج)، بيكيني+أندر آرم+لاين (350ج).
        - وجه (250ج)، وجه+ذقن (350ج)، وجه+رقبة (450ج).
        - جسم كامل (2500ج)، جسم كامل بدون بطن وظهر (2000ج)، نصف جسم (1250ج).
        - ACTION: Always append send_clinic_media(['women_packages', 'women_areas']) when quoting these.
        </prices_women>

        <prices_men>
        - تحديد ذقن (300ج)، ذقن ورقبة (500ج)، ذقن ورقبة وفك (750ج)، وجه كامل (500ج).
        - أندر آرم (400ج)، بوكسر (500ج)، بوكسر وأندر آرم وذقن (1000ج).
        - عصعص (750ج)، جسم كامل (4000ج بدلاً من 5000ج).
        - ACTION: Always append send_clinic_media(['men_offers']) when quoting these.
        </prices_men>
        </knowledge_base>

        <booking_workflow>
        STEP 1: Identify missing info (Name, Date, Time, Area).
        STEP 2: Ask user for missing info politely.
        STEP 3: Validate Time (12 PM - 10 PM) and Day (Not Friday).
        STEP 4: Call check_schedule(date) to verify availability.
        STEP 5: If available, call book_appointment(patient_name, phone_number, date, time, area).
        </booking_workflow>

        <tone_and_style>
        - لهجة مصرية عامية راقية (يا فندم، من عيني، تحت أمرك).
        - لا تكرري الترحيب في كل رسالة.
        - استخدمي إيموجيز (🌸، ✨).
        </tone_and_style>
        """

        chat = client.chats.create(
            model='gemini-3.6-flash',
            history=load_chat_history(sender_phone, limit=10),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.1,
                tools=[
                    check_schedule,
                    check_patient_appointments,
                    cancel_appointment,
                    book_appointment,
                    update_patient_file,
                    send_clinic_media,
                    notify_staff
                ],
            )
        )
        return chat.send_message(user_message).text or "", queued_images
    except Exception as e:
        print(f"Gemini API Error: {e}")
        traceback.print_exc()
        return "أهلاً بحضرتك يا فندم! ثواني وهكون مع حضرتك.", []

# ---------------------------------------------------------
# OUTBOUND META MESSAGING
# ---------------------------------------------------------
async def send_whatsapp_message(to: str, text: str, phone_id: str):
    async with httpx.AsyncClient() as c:
        await c.post(
            f"https://graph.facebook.com/v21.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
        )

async def send_whatsapp_image(to: str, url: str, caption: str, phone_id: str):
    async with httpx.AsyncClient() as c:
        await c.post(
            f"https://graph.facebook.com/v21.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            json={"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": url, "caption": caption}}
        )
