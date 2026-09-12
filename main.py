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
STAFF_NOTIFICATION_PHONE = os.getenv("STAFF_NOTIFICATION_PHONE", "201022227818")

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
# DB HELPERS & LIVE SETTINGS (Supabase)
# ---------------------------------------------------------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

DEFAULT_SYSTEM_INSTRUCTION = """<role_definition>
أنتِ "نور"، موظفة استقبال ذكية ولطيفة في "عيادات جوثن" (Jothen Clinics).
مهمتك الوحيدة: خدمة عملاء فرع "مدينة نصر" فقط، وحجز مواعيد ليزر إزالة الشعر.
</role_definition>

<hard_constraints>
1. IF user asks about location -> REPLY EXACTLY: "عيادة 104، 8 ش الدكتور حسن الشريف، مدينة نصر" AND trigger send_clinic_media(['branches']). NEVER mention other locations.
2. IF user asks for phone number -> NEVER ask. You already know it.
3. IF user asks routine laser prep or general FAQ questions (e.g., shaving/شيفنج, numbing cream, sun exposure, sessions, gaps, post-care, pain, cooling) -> ANSWER directly using <laser_faqs_and_prep>.
4. IF user asks complex Medical Advice not in FAQs (e.g., burns, pregnancy, medications), Doctors, Botox, Filler, Dermatology, or has a complaint -> TRIGGER notify_staff(issue_summary) IMMEDIATELY in the background.
    - CRITICAL: DO NOT tell the patient that management or a doctor will contact them.
    - INSTEAD, apologize politely that your role is limited to laser bookings.
5. IF user asks for a price NOT listed in <knowledge_base> -> TRIGGER notify_staff(issue_summary="استفسار عن سعر غير مسجل") in the background. 
    - CRITICAL: DO NOT say you are checking with management.
    - INSTEAD, state politely that you only have standard packages available.
6. IF user asks to book on Friday -> REJECT. Friday is a holiday.
7. IF user asks to book outside 12:00 PM to 10:00 PM -> REJECT. Request a valid time.
8. IF the user's message is ambiguous or contains typos (e.g., "back 5") -> Politely ask them to clarify what they mean.
</hard_constraints>

<knowledge_base>
<laser_faqs_and_prep>
- الشيفنج (Shaving): نعم يا فندم، لازم يتم إزالة الشعر بالشفرة (الشيفنج) في نفس يوم الجلسة أو قبلها بيوم، وممنوع استخدام السويت أو الشمع أو الفتلة.
- المخدر (Numbing Cream): متاح استخدام كريم مخدر قبل الجلسة بنصف أو ساعة للمناطق الحساسة.
- الشمس (Sun Exposure): يفضل عدم التعرض المباشر للشمس أو عمل تان (Tan) قبل وبعد الجلسة بأسبوعين.
- عدد الجلسات: في المتوسط بنحتاج من 6 لـ 8 جلسات.
- الفرق بين الجلسات: الجلسات بتكون كل 3 لـ 4 أسابيع للوجه، وكل 4 لـ 6 أسابيع لباقي مناطق الجسم.
- العناية بعد الجلسة: بننصح باستخدام كريم مرطب طبي ومضاد حيوي بعد الجلسة مباشرة، وممنوع تماماً استخدام أي عطور، مزيلات عرق، أو مقشرات على المنطقة لمدة 48 ساعة.
- الألم والتبريد: أجهزتنا مزودة بأقوى نظام تبريد مزدوج بيخلي الجلسة مريحة جداً وبدون ألم، مجرد لسعة خفيفة جداً.
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

def get_live_instructions() -> str:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS clinic_settings (
                        key VARCHAR(50) PRIMARY KEY,
                        content TEXT NOT NULL,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)
                conn.commit()
                cursor.execute("SELECT content FROM clinic_settings WHERE key = 'system_instruction'")
                row = cursor.fetchone()
                if row and row[0].strip(): return row[0]
                cursor.execute("INSERT INTO clinic_settings (key, content) VALUES ('system_instruction', %s) ON CONFLICT DO NOTHING", (DEFAULT_SYSTEM_INSTRUCTION,))
                conn.commit()
    except Exception as e: print(f"Error loading instructions: {e}")
    return DEFAULT_SYSTEM_INSTRUCTION

def save_live_instructions(new_content: str) -> bool:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO clinic_settings (key, content, updated_at)
                    VALUES ('system_instruction', %s, NOW())
                    ON CONFLICT (key) DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()
                """, (new_content,))
                conn.commit()
                return True
    except Exception as e:
        print(f"Error saving instructions: {e}")
        return False

def is_duplicate_message(message_id: str) -> bool:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,))
                if cursor.fetchone(): return True
                cursor.execute("INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING", (message_id,))
                conn.commit()
                return False
    except Exception: return False

def save_chat_turn(phone_number: str, role: str, content: str):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                try: cursor.execute("INSERT INTO chat_history (phone_number, role, content, created_at) VALUES (%s, %s, %s, NOW())", (phone_number, role, content))
                except Exception:
                    conn.rollback()
                    cursor.execute("INSERT INTO chat_history (phone_number, role, content) VALUES (%s, %s, %s)", (phone_number, role, content))
                conn.commit()
    except Exception as e: print(f"DB Save Chat Error: {e}")

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
    except Exception: pass
    return history

def load_patient_profile(phone_number: str) -> dict:
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT name, preferences, is_paused FROM patients WHERE phone_number = %s", (phone_number,))
                row = cursor.fetchone()
                if row: return {"name": row.get("name") or "", "preferences": row.get("preferences") or "", "is_paused": row.get("is_paused", False)}
    except Exception: pass
    return {"name": "", "preferences": "", "is_paused": False}

def update_patient_file(phone_number: str, name: str, preferences: str) -> str:
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
    except Exception as e: return f"حدث خطأ أثناء حفظ الملف: {e}"

# ---------------------------------------------------------
# GOOGLE SHEETS TOOLS (APPOINTMENTS)
# ---------------------------------------------------------
def normalize_to_ampm(time_str: str) -> str:
    time_str = time_str.strip().upper()
    try: return datetime.datetime.strptime(time_str, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError: pass
    try: return datetime.datetime.strptime(time_str, "%I:%M %p").strftime("%I:%M %p").lstrip("0")
    except ValueError: pass
    return time_str

def check_schedule(date: str) -> str:
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            res = http_client.get(f"{GOOGLE_SHEET_URL}?date={date}", timeout=15.0).json()
            if "booked" in res and not res["booked"]: return f"يوم {date} متاح بالكامل."
            
            if "booked" in res:
                # Updated to handle the new detailed object format from Google Apps Script
                if len(res["booked"]) > 0 and isinstance(res["booked"][0], dict):
                    booked_times = [b.get("time", "") for b in res["booked"]]
                else:
                    booked_times = res["booked"]
                return f"المواعيد المحجوزة مسبقاً يوم {date} هي: {', '.join(booked_times)}"
            return "حدث خطأ في قراءة الجدول."
    except Exception: return "لا يمكن قراءة الجدول الآن."

def check_patient_appointments(phone_number: str) -> str:
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            res = http_client.get(f"{GOOGLE_SHEET_URL}?phone={phone_number}", timeout=15.0).json()
            if "appointments" in res and res["appointments"]: return f"حجوزات العميل الحالية: {', '.join(res['appointments'])}"
            return "لا يوجد حجوزات سابقة أو قادمة لهذا العميل."
    except Exception: return "فشل في قراءة حجوزات العميل."

def cancel_appointment(phone_number: str, date: str) -> str:
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            res = http_client.post(GOOGLE_SHEET_URL, json={"action": "cancel", "phone_number": phone_number, "date": date}, timeout=15.0).json()
            if res.get("deleted"): return f"تم إلغاء الحجز القديم يوم {date} بنجاح."
            return f"لم يتم العثور على حجز لإلغائه في يوم {date}."
    except Exception: return "فشل الاتصال بنظام الإلغاء."

def book_appointment(patient_name: str, phone_number: str, date: str, time: str, area: str) -> str:
    standard_time = normalize_to_ampm(time)
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            payload = {"action": "book", "patient_name": patient_name, "phone_number": phone_number, "branch": "مدينة نصر", "date": date, "time": standard_time, "area": area}
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

class PauseRequest(BaseModel): phone_number: str; is_paused: bool
class SettingsUpdate(BaseModel): instruction: str
class BookReq(BaseModel): patient_name: str; phone_number: str; date: str; time: str; area: str
class CancelReq(BaseModel): phone_number: str; date: str

@app.post("/admin/api/toggle_pause")
def toggle_pause(req: PauseRequest, admin: str = Depends(verify_admin)):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO patients (phone_number, is_paused) VALUES (%s, %s) ON CONFLICT (phone_number) DO UPDATE SET is_paused = EXCLUDED.is_paused", (req.phone_number, req.is_paused))
                conn.commit()
        return {"status": "success"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/api/settings")
def get_settings(admin: str = Depends(verify_admin)):
    return {"instruction": get_live_instructions()}

@app.post("/admin/api/settings")
def update_settings(data: SettingsUpdate, admin: str = Depends(verify_admin)):
    if not save_live_instructions(data.instruction): raise HTTPException(status_code=500, detail="Failed to save settings")
    return {"status": "success"}

@app.get("/admin/api/schedule")
def api_get_schedule(date: str, admin: str = Depends(verify_admin)):
    try:
        with httpx.Client(follow_redirects=True) as http_client:
            res = http_client.get(f"{GOOGLE_SHEET_URL}?date={date}", timeout=15.0).json()
            return res
    except Exception as e: return {"error": str(e)}

@app.post("/admin/api/book")
def api_admin_book(req: BookReq, admin: str = Depends(verify_admin)):
    res = book_appointment(req.patient_name, req.phone_number, req.date, req.time, req.area)
    return {"status": res}

@app.post("/admin/api/cancel")
def api_admin_cancel(req: CancelReq, admin: str = Depends(verify_admin)):
    res = cancel_appointment(req.phone_number, req.date)
    return {"status": res}

@app.get("/admin/api/data")
def get_admin_data(admin: str = Depends(verify_admin)):
    patients, chats = [], []
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                try:
                    cursor.execute("SELECT id, phone_number, role, content, COALESCE(created_at, NOW()) AS created_at FROM chat_history ORDER BY id ASC")
                    chats = cursor.fetchall()
                except Exception: pass
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
                except Exception: pass
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
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #e5ddd5; margin: 0; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
            
            /* TOP NAVIGATION */
            .top-nav { height: 60px; background: #ffffff; border-bottom: 1px solid #d1d7db; display: flex; align-items: center; justify-content: space-between; padding: 0 24px; box-shadow: 0 1px 2px rgba(0,0,0,0.05); z-index: 10; }
            .tabs-container { display: flex; gap: 15px; height: 100%; }
            .nav-tab { background: transparent; border: none; font-size: 15px; font-weight: bold; color: #54656f; cursor: pointer; padding: 0 15px; border-bottom: 3px solid transparent; transition: all 0.2s; }
            .nav-tab:hover { color: #008069; }
            .nav-tab.active { color: #008069; border-bottom: 3px solid #008069; }
            
            /* CHATS VIEW */
            #view-chats { display: flex; height: calc(100vh - 60px); width: 100%; }
            .sidebar { width: 380px; min-width: 340px; background: #ffffff; border-left: 1px solid #d1d7db; display: flex; flex-direction: column; height: 100%; }
            .sidebar-header { background: #f0f2f5; padding: 16px 20px; font-weight: bold; font-size: 16px; border-bottom: 1px solid #d1d7db; color: #111b21; }
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
            .btn-settings { background: #e3f2fd; color: #1565c0; border-color: #bbdefb; padding: 6px 15px; font-size: 13px; border-radius: 20px; }
            .btn-settings:hover { background: #bbdefb; }
            .btn-pause { background: #ffcdd2; color: #b71c1c; border-color: #ef9a9a; }
            .btn-pause:hover { background: #ef9a9a; }
            .btn-resume { background: #c8e6c9; color: #1b5e20; border-color: #a5d6a7; }
            .btn-resume:hover { background: #a5d6a7; }
            .messages { flex-grow: 1; padding: 24px 36px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
            .msg { max-width: 65%; padding: 8px 14px 6px 14px; border-radius: 8px; font-size: 14px; line-height: 1.45; position: relative; box-shadow: 0 1px 1px rgba(0,0,0,0.08); word-break: break-word; }
            .msg.user { background: #ffffff; align-self: flex-start; border-top-right-radius: 0; }
            .msg.model { background: #d9fdd3; align-self: flex-end; border-top-left-radius: 0; }
            .msg-meta { font-size: 10.5px; color: #667781; text-align: left; direction: ltr; margin-top: 3px; }
            
            /* SCHEDULE VIEW - UPDATED FOR GRID LAYOUT */
            #view-schedule { display: none; height: calc(100vh - 60px); width: 100%; background: #efeae2; padding: 30px; overflow-y: auto; }
            .schedule-container { background: #fff; border-radius: 12px; padding: 24px; width: 100%; max-width: 1400px; margin: 0 auto; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
            .schedule-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 2px solid #f0f2f5; }
            .schedule-header h2 { margin: 0; color: #111b21; font-size: 20px; }
            .date-picker { padding: 10px 15px; border: 1px solid #d1d7db; border-radius: 8px; font-size: 15px; font-family: inherit; outline: none; color: #111b21; cursor: pointer; }
            
            /* GRID MAGIC HERE */
            #schedule-slots { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
            
            .slot-row { display: flex; flex-direction: column; padding: 16px; border-radius: 10px; border: 1px solid #d1d7db; transition: transform 0.1s; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
            .slot-row:hover { transform: translateY(-2px); box-shadow: 0 4px 8px rgba(0,0,0,0.08); }
            .slot-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
            .slot-time { font-weight: bold; font-family: monospace; font-size: 18px; direction: ltr; margin: 0; }
            .slot-badge { font-weight: bold; font-size: 12px; padding: 4px 10px; border-radius: 12px; border: 1px solid; }
            
            /* MODALS */
            .modal-overlay { display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.5); z-index:9999; justify-content:center; align-items:center; }
            .modal-content { background:#fff; border-radius:12px; padding:24px; box-shadow:0 4px 20px rgba(0,0,0,0.2); }
            .form-group { margin-bottom: 15px; display: flex; flex-direction: column; gap: 5px; }
            .form-group label { font-size: 13px; font-weight: bold; color: #54656f; }
            .form-group input, .form-group select { padding: 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; font-family: inherit; }
            .empty-state { grid-column: 1 / -1; text-align: center; color: #8696a0; font-size: 16px; padding: 40px; }
        </style>
    </head>
    <body>
        <!-- Top Navigation -->
        <div class="top-nav">
            <div class="tabs-container">
                <button class="nav-tab active" id="tab-btn-chats" onclick="switchView('chats')">💬 المحادثات</button>
                <button class="nav-tab" id="tab-btn-schedule" onclick="switchView('schedule')">📅 جدول جوجل شيت</button>
            </div>
            <button class="btn btn-settings" onclick="openSettingsModal()">⚙️ إعدادات البوت والأسعار</button>
        </div>

        <!-- 1. CHATS VIEW -->
        <div id="view-chats">
            <div class="sidebar">
                <div class="sidebar-header">ملفات المرضى 📁</div>
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
        </div>

        <!-- 2. SCHEDULE VIEW -->
        <div id="view-schedule">
            <div class="schedule-container">
                <div class="schedule-header">
                    <h2>حجوزات العيادة 📅</h2>
                    <input type="date" id="schedule-date-picker" class="date-picker" onchange="loadSchedule()">
                </div>
                <div id="schedule-slots">
                    <!-- Populated by JS -->
                </div>
            </div>
        </div>
        
        <!-- Booking Form Modal -->
        <div id="booking-modal" class="modal-overlay">
            <div class="modal-content" style="width: 400px; max-width: 90%;">
                <h3 style="margin-top:0;">إضافة حجز جديد</h3>
                <div class="form-group">
                    <label>تاريخ وتوقت الحجز</label>
                    <div style="display:flex; gap:10px;">
                        <input type="text" id="book-date" disabled style="background:#f0f2f5; flex:1; direction:ltr;">
                        <input type="text" id="book-time" disabled style="background:#f0f2f5; flex:1; direction:ltr;">
                    </div>
                </div>
                <div class="form-group">
                    <label>اسم المريض</label>
                    <input type="text" id="book-name" placeholder="مثال: أحمد كريم">
                </div>
                <div class="form-group">
                    <label>رقم الهاتف (WhatsApp)</label>
                    <input type="text" id="book-phone" placeholder="مثال: 201012345678" style="direction:ltr; text-align:left;">
                </div>
                <div class="form-group">
                    <label>المنطقة المراد عملها</label>
                    <select id="book-area">
                        <option value="جسم كامل">جسم كامل</option>
                        <option value="نصف جسم">نصف جسم</option>
                        <option value="أندر آرم">أندر آرم</option>
                        <option value="بكيني">بكيني</option>
                        <option value="وجه">وجه</option>
                        <option value="تحديد ذقن">تحديد ذقن</option>
                        <option value="أخرى">أخرى (مخصصة)</option>
                    </select>
                </div>
                <div style="display:flex; justify-content:flex-end; gap:10px; margin-top:20px;">
                    <button class="btn" onclick="closeBookingModal()">إلغاء</button>
                    <button class="btn" id="submit-booking-btn" style="background:#008069; color:#fff;" onclick="submitBooking()">تأكيد الحجز</button>
                </div>
            </div>
        </div>

        <!-- Settings Form Modal -->
        <div id="settings-modal" class="modal-overlay">
            <div class="modal-content" style="width:800px; max-width:90%; height:85vh; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <h3 style="margin:0;">⚙️ تعديل تعليمات البوت والأسعار</h3>
                    <button onclick="closeSettingsModal()" style="border:none; background:transparent; font-size:22px; cursor:pointer; color:#666;">✖</button>
                </div>
                <p style="font-size:12.5px; color:#667781; margin-top:0; margin-bottom:12px;">تعديلك لأسعار أو شروط البوت هنا يتم حفظه فوراً في قاعدة البيانات وسيطبقه البوت على أي رسالة قادمة.</p>
                <textarea id="instruction-textarea" style="flex-grow:1; width:100%; font-family:monospace; font-size:13px; padding:12px; border:1px solid #ccc; border-radius:8px; resize:none; line-height:1.5;" dir="rtl"></textarea>
                <div style="display:flex; justify-content:flex-end; gap:10px; margin-top:15px;">
                    <button class="btn" onclick="closeSettingsModal()">إلغاء</button>
                    <button class="btn" id="save-settings-btn" style="background:#008069; color:#fff;" onclick="saveSettings()">💾 حفظ التعديلات فوراً</button>
                </div>
            </div>
        </div>

        <script>
            let allChats = [], allPatients = [], currentPhone = null, autoScroll = true, newestAtTop = false, currentIsPaused = false;
            const messagesDiv = document.getElementById('messages');
            
            function switchView(viewName) {
                document.getElementById('view-chats').style.display = viewName === 'chats' ? 'flex' : 'none';
                document.getElementById('view-schedule').style.display = viewName === 'schedule' ? 'flex' : 'none';
                document.getElementById('tab-btn-chats').className = viewName === 'chats' ? 'nav-tab active' : 'nav-tab';
                document.getElementById('tab-btn-schedule').className = viewName === 'schedule' ? 'nav-tab active' : 'nav-tab';
                
                if (viewName === 'schedule') {
                    const dp = document.getElementById('schedule-date-picker');
                    if (!dp.value) {
                        const today = new Date();
                        const offset = today.getTimezoneOffset() * 60000;
                        const localDate = new Date(today.getTime() - offset);
                        dp.value = localDate.toISOString().split('T')[0];
                    }
                    loadSchedule();
                }
            }

            // ==========================================
            // SCHEDULE & GOOGLE SHEETS LOGIC
            // ==========================================
            const CLINIC_TIMES = ["12:00 PM", "12:30 PM", "01:00 PM", "01:30 PM", "02:00 PM", "02:30 PM", "03:00 PM", "03:30 PM", "04:00 PM", "04:30 PM", "05:00 PM", "05:30 PM", "06:00 PM", "06:30 PM", "07:00 PM", "07:30 PM", "08:00 PM", "08:30 PM", "09:00 PM", "09:30 PM", "10:00 PM"];

            function normalizeTimeJS(t) {
                if (!t) return "";
                let clean = String(t).toUpperCase().replace(/[^A-Z0-9:]/g, ''); 
                if (clean.startsWith("0")) return clean.substring(1);
                return clean;
            }

            async function loadSchedule() {
                const date = document.getElementById('schedule-date-picker').value;
                const slotsDiv = document.getElementById('schedule-slots');
                slotsDiv.innerHTML = '<div class="empty-state">⏳ جاري قراءة الحجوزات من جوجل شيت...</div>';
                
                try {
                    const res = await fetch(`/admin/api/schedule?date=${date}`);
                    const data = await res.json();
                    
                    if (data.error) {
                        slotsDiv.innerHTML = `<div class="empty-state" style="color:#cf1322;">❌ خطأ من جوجل شيت: ${data.error}</div>`;
                        return;
                    }
                    
                    let bookedSlots = {};
                    if (data.booked && Array.isArray(data.booked)) {
                        data.booked.forEach(b => {
                            if (typeof b === 'string') {
                                bookedSlots[normalizeTimeJS(b)] = { name: "غير مسجل", phone: "", area: "" };
                            } else {
                                bookedSlots[normalizeTimeJS(b.time)] = {
                                    name: b.name || "غير مسجل",
                                    phone: b.phone || "",
                                    area: b.area || ""
                                };
                            }
                        });
                    }

                    slotsDiv.innerHTML = '';
                    CLINIC_TIMES.forEach(t => {
                        const normalizedClinicTime = normalizeTimeJS(t);
                        const bookingData = bookedSlots[normalizedClinicTime];
                        const isBooked = !!bookingData;
                        
                        const slotDiv = document.createElement('div');
                        slotDiv.className = 'slot-row';
                        slotDiv.style.background = isBooked ? '#fff1f0' : '#f6ffed';
                        slotDiv.style.borderColor = isBooked ? '#ffa39e' : '#b7eb8f';
                        
                        if (isBooked) {
                            slotDiv.innerHTML = `
                                <div class="slot-top">
                                    <span class="slot-badge" style="color:#cf1322; background:#fff; border-color:#ffa39e;">🔴 محجوزة</span>
                                    <div class="slot-time" style="color: #cf1322;">${t}</div>
                                </div>
                                <div style="font-size:13.5px; color:#54656f; font-weight:600; line-height: 1.6; margin-bottom: 16px; flex-grow: 1;">
                                    👤 <span style="color:#111b21;">${bookingData.name}</span> <br>
                                    📞 <span dir="ltr" style="color:#111b21;">${bookingData.phone}</span> <br>
                                    🎯 المنطقة: <span style="color:#111b21;">${bookingData.area}</span>
                                </div>
                                <button onclick="promptCancel('${date}', '${t}', '${bookingData.phone}')" style="background:#ff4d4f; color:#fff; border:none; padding:10px; border-radius:8px; cursor:pointer; font-weight:bold; transition: 0.2s; width:100%;">إلغاء الحجز</button>
                            `;
                        } else {
                            slotDiv.innerHTML = `
                                <div class="slot-top" style="margin-bottom:0;">
                                    <span class="slot-badge" style="color:#389e0d; border-color:transparent;">🟢 متاحة</span>
                                    <div class="slot-time" style="color: #389e0d;">${t}</div>
                                </div>
                                <div style="flex-grow: 1;"></div>
                                <button onclick="openBookingModal('${date}', '${t}')" style="background:#52c41a; color:#fff; border:none; padding:10px; border-radius:8px; cursor:pointer; font-weight:bold; transition: 0.2s; width:100%; margin-top:16px;">+ حجز موعد</button>
                            `;
                        }
                        slotsDiv.appendChild(slotDiv);
                    });
                } catch (e) {
                    slotsDiv.innerHTML = '<div class="empty-state" style="color:#cf1322;">❌ حدث خطأ في الاتصال بجوجل شيت. يرجى المحاولة لاحقاً.</div>';
                }
            }

            async function promptCancel(date, time, currentPhoneHint) {
                const phone = prompt(`أنت على وشك إلغاء حجز الساعة ${time} يوم ${date}.\n\nالرجاء إدخال رقم هاتف المريض لتأكيد الإلغاء:`, currentPhoneHint || "");
                if (!phone) return;
                
                const res = await fetch('/admin/api/cancel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone_number: phone, date: date})
                });
                const data = await res.json();
                alert(data.status);
                loadSchedule();
            }

            function openBookingModal(date, time) {
                document.getElementById('book-date').value = date;
                document.getElementById('book-time').value = time;
                document.getElementById('book-name').value = '';
                document.getElementById('book-phone').value = '';
                document.getElementById('booking-modal').style.display = 'flex';
            }
            function closeBookingModal() { document.getElementById('booking-modal').style.display = 'none'; }

            async function submitBooking() {
                const name = document.getElementById('book-name').value;
                const phone = document.getElementById('book-phone').value;
                const area = document.getElementById('book-area').value;
                const date = document.getElementById('book-date').value;
                const time = document.getElementById('book-time').value;

                if(!name || !phone) return alert('الرجاء إدخال اسم المريض ورقم الهاتف');

                const btn = document.getElementById('submit-booking-btn');
                btn.innerText = 'جاري الحفظ في جوجل شيت...';

                const res = await fetch('/admin/api/book', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ patient_name: name, phone_number: phone, date: date, time: time, area: area })
                });
                const data = await res.json();
                alert(data.status);
                
                closeBookingModal();
                btn.innerText = 'تأكيد الحجز';
                loadSchedule();
            }

            // ==========================================
            // CHATS & SETTINGS LOGIC
            // ==========================================
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
            
            async function openSettingsModal() {
                const res = await fetch('/admin/api/settings');
                const data = await res.json();
                document.getElementById('instruction-textarea').value = data.instruction;
                document.getElementById('settings-modal').style.display = 'flex';
            }

            function closeSettingsModal() { document.getElementById('settings-modal').style.display = 'none'; }

            async function saveSettings() {
                const btn = document.getElementById('save-settings-btn');
                btn.innerText = 'جاري الحفظ...';
                const val = document.getElementById('instruction-textarea').value;
                await fetch('/admin/api/settings', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({instruction: val})
                });
                btn.innerText = '💾 تم الحفظ بنجاح!';
                setTimeout(() => { btn.innerText = '💾 حفظ التعديلات فوراً'; closeSettingsModal(); }, 1200);
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
            """Sends a silent alert to clinic management regarding a patient's medical question or unlisted price request."""
            try:
                alert_body = f"🚨 *تنبيه استفسار يحتاج متابعة*\n\n📱 *رقم المريض:* {sender_phone}\n📝 *المشكلة:* {issue_summary}\n\n_البوت مستمر في الرد ولم يتوقف._"
                with httpx.Client() as c:
                    c.post(
                        f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages",
                        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                        json={"messaging_product": "whatsapp", "to": STAFF_NOTIFICATION_PHONE, "type": "text", "text": {"body": alert_body}},
                        timeout=10.0
                    )
                return "تم الإرسال للإدارة بنجاح. أجيبي المريض بناءً على التعليمات فقط."
            except Exception as e:
                print(f"Failed to notify staff: {e}")
                return "تم التنبيه."

        now_cairo = datetime.datetime.now(ZoneInfo("Africa/Cairo"))
        arabic_days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
        today_day_name = arabic_days[now_cairo.weekday()]
        today_date = f"{now_cairo.strftime('%Y-%m-%d')} (اليوم هو: {today_day_name})"

        base_instruction = get_live_instructions()

        system_instruction = f"""
        {base_instruction}

        === سياق العميل والمحادثة الحالية ===
        - العميل الحالي: {profile['name'] or 'عميل جديد'}
        - الملاحظات/التفضيلات: {profile['preferences'] or 'لا يوجد'}
        - رقم هاتف العميل: {sender_phone}
        - تاريخ اليوم: {today_date} بتوقيت القاهرة.
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
