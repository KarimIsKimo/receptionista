import os
import re
import json
import asyncio
import secrets
import logging
import datetime as dt
from contextlib import asynccontextmanager
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, Response, BackgroundTasks, Depends, HTTPException, status, Query
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ============================================================
# JOTHEN CLINICS - NASR CITY AI RECEPTIONIST v2
# ============================================================
# Major changes from the original:
# - Centralized configuration; no real secrets hard-coded.
# - Automatic DB schema creation + indexes.
# - Atomic webhook idempotency (prevents duplicate replies).
# - Processes every message in a webhook payload, not only [0].
# - Correct patient upsert / rename behavior.
# - Stronger validation for dates, times, phone numbers and areas.
# - Async-safe AI execution via asyncio.to_thread().
# - Per-patient locks with bounded cleanup.
# - Manual staff replies from the dashboard.
# - Patient notes/tags.
# - Dashboard statistics.
# - Search/filter/pagination-ready patient API.
# - Audit log.
# - Health/status endpoints.
# - Safer error responses (no raw DB errors to the browser).
# - Global bot switch + per-patient human takeover.
# ============================================================

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("jothen")

CAIRO = ZoneInfo("Africa/Cairo")
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

# Render-compatible configuration.
# These names match the user's current deployment. Newer aliases are also accepted.
DATABASE_URL = required_env("DATABASE_URL")
VERIFY_TOKEN = required_env("VERIFY_TOKEN")
WHATSAPP_ACCESS_TOKEN = required_env("WHATSAPP_ACCESS_TOKEN")

# The current Render service does not expose these two variables, so preserve the
# existing production defaults while allowing them to be moved into Render later.
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1360825553771801").strip()
GOOGLE_SHEET_URL = (
    os.getenv("APPS_SCRIPT_URL", "").strip()
    or os.getenv("GOOGLE_SHEET_URL", "").strip()
)
if not GOOGLE_SHEET_URL:
    raise RuntimeError("Missing APPS_SCRIPT_URL (or GOOGLE_SHEET_URL)")

GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY", "").strip()
    or os.getenv("GOOGLE_API_KEY", "").strip()
)
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY (or GOOGLE_API_KEY)")

# Preserve the existing dashboard credentials as a compatibility fallback.
# For production, add ADMIN_USERNAME and ADMIN_PASSWORD in Render.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "jothen123")

BASE_URL = os.getenv("BASE_URL", "https://receptionista.onrender.com").rstrip("/")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
STAFF_NOTIFICATION_PHONE = os.getenv(
    "STAFF_NOTIFICATION_PHONE", "201026438897"
).strip()

# Existing deployment flag is retained for compatibility.
ENABLE_REAL_CLINIC = os.getenv("ENABLE_REAL_CLINIC", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

BLOCKED_NUMBERS = {
    re.sub(r"\D", "", x)
    for x in os.getenv("BLOCKED_NUMBERS", "").split(",")
    if re.sub(r"\D", "", x)
}

IMAGE_DIR = os.getenv("IMAGE_DIR", "images")
STATIC_DIR = "static"
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

OFFER_IMAGES = {
    "branches": {
        "url": f"{BASE_URL}/images/branches.jpg",
        "caption": "فروعنا وأماكن تواجدنا 📍",
    },
    "machines": {
        "url": f"{BASE_URL}/images/machines.jpg",
        "caption": "أحدث أجهزة إزالة الشعر بالليزر المتوفرة لدينا ⚡",
    },
    "men_offers": {
        "url": f"{BASE_URL}/images/men_offers.jpg",
        "caption": "عروض وباقات ليزر إزالة الشعر المخصصة للرجال 🧔",
    },
    "women_areas": {
        "url": f"{BASE_URL}/images/women_areas.jpg",
        "caption": "أسعار وعروض المناطق المنفردة لليزر السيدات 🌸",
    },
    "women_packages": {
        "url": f"{BASE_URL}/images/women_packages.jpg",
        "caption": "باقات وعروض الليزر الكاملة للسيدات ✨",
    },
}

DEFAULT_SYSTEM_INSTRUCTION = """<role_definition>
أنتِ "نور"، موظفة استقبال ذكية ولطيفة في "عيادات جوثن" (Jothen Clinics).
مهمتك: خدمة عملاء فرع مدينة نصر وحجز مواعيد ليزر إزالة الشعر.
</role_definition>

<hard_constraints>
1. إذا سأل العميل عن الموقع:
   أجيبي: "عيادة 104، 8 ش الدكتور حسن الشريف، مدينة نصر"
   واستدعي send_clinic_media(["branches"]).
2. إذا سأل عن رقم الهاتف فلا تسأليه مرة أخرى؛ استخدمي الرقم المتاح في إعدادات العيادة إذا كان مسجلاً.
3. أسئلة الليزر الروتينية يتم الرد عليها من الـFAQ أدناه.
4. الأسئلة الطبية المعقدة أو الشكاوى أو Botox/Filler/Dermatology:
   استدعي notify_staff ثم اعتذري بلطف أن دورك يقتصر على حجوزات الليزر والمعلومات الأساسية.
   لا تقولي إن الطبيب/الإدارة سيتواصل معه.
5. السعر غير الموجود في قاعدة الأسعار:
   استدعي notify_staff ثم قولي إن الأسعار المتاحة لديك هي الأسعار القياسية فقط.
6. الجمعة إجازة ولا يمكن الحجز فيها.
7. ساعات الحجز من 12:00 PM إلى 10:00 PM.
8. إذا كانت الرسالة غامضة أو بها typo، اطلبي التوضيح بدل التخمين.
9. لا تخترعي موعداً متاحاً. يجب استخدام check_schedule قبل الحجز.
10. لا تؤكدي نجاح الحجز إلا بعد نجاح book_appointment.
</hard_constraints>

<laser_faqs_and_prep>
- الشيفنج: إزالة الشعر بالشفرة في نفس يوم الجلسة أو قبلها بيوم، وممنوع السويت أو الشمع أو الفتلة.
- المخدر: متاح استخدام كريم مخدر قبل الجلسة بنصف أو ساعة للمناطق الحساسة.
- الشمس: يفضل عدم التعرض المباشر للشمس أو عمل تان قبل وبعد الجلسة بأسبوعين.
- عدد الجلسات: في المتوسط 6 لـ 8 جلسات.
- الفرق: الوجه كل 3 لـ 4 أسابيع، وباقي الجسم كل 4 لـ 6 أسابيع.
- بعد الجلسة: مرطب طبي ومضاد حيوي حسب تعليمات العيادة، وممنوع العطور ومزيلات العرق والمقشرات لمدة 48 ساعة.
- الألم والتبريد: الأجهزة مزودة بنظام تبريد، والإحساس غالباً لسعات خفيفة.
</laser_faqs_and_prep>

<prices_women>
- 1000 نبضة 800ج، 2000 نبضة 1500ج، 3000 نبضة 2000ج، 5000 نبضة 3000ج، 7000 نبضة 3500ج، 10000 نبضة 5000ج.
- عرض 4 جلسات أندر آرم أو بيكيني بخصم 10%.
- أندر آرم 150ج، بيكيني+لاين 300ج، بيكيني+أندر آرم+لاين 350ج.
- وجه 250ج، وجه+ذقن 350ج، وجه+رقبة 450ج.
- جسم كامل 2500ج، جسم كامل بدون بطن وظهر 2000ج، نصف جسم 1250ج.
- عند ذكر هذه الأسعار استدعي send_clinic_media(["women_packages", "women_areas"]).
</prices_women>

<prices_men>
- تحديد ذقن 300ج، ذقن ورقبة 500ج، ذقن ورقبة وفك 750ج، وجه كامل 500ج.
- أندر آرم 400ج، بوكسر 500ج، بوكسر وأندر آرم وذقن 1000ج.
- عصعص 750ج، جسم كامل 4000ج بدلاً من 5000ج.
- عند ذكر هذه الأسعار استدعي send_clinic_media(["men_offers"]).
</prices_men>

<tone_and_style>
- لهجة مصرية عامية راقية.
- لا تكرري الترحيب في كل رسالة.
- استخدمي إيموجيز باعتدال.
- لا تذكري أدوات أو Gemini أو النظام الداخلي.
</tone_and_style>
"""

# ------------------------------------------------------------
# FastAPI lifecycle
# ------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    cleanup_task = asyncio.create_task(cleanup_locks())
    log.info("Jothen receptionist started")
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        log.info("Jothen receptionist stopped")

app = FastAPI(
    title="Jothen Clinic AI Receptionist",
    version="3.0.0",
    lifespan=lifespan,
)
app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")
app.mount("/static", StaticFiles(directory="static"), name="static")

security = HTTPBasic()

# A lock per WhatsApp customer. We clean old locks periodically.
user_locks: dict[str, asyncio.Lock] = {}
lock_last_used: dict[str, dt.datetime] = {}
lock_guard = asyncio.Lock()

def get_db_connection():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        connect_timeout=10,
        application_name="jothen-receptionist",
    )

def init_db():
    statements = [
        # Existing production databases may already contain these tables with the
        # older column set. The ALTER statements below are intentionally additive.
        """
        CREATE TABLE IF NOT EXISTS clinic_settings (
            key VARCHAR(100) PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS patients (
            phone_number VARCHAR(30) PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            preferences TEXT NOT NULL DEFAULT '',
            tags TEXT[] NOT NULL DEFAULT '{}',
            is_paused BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        ALTER TABLE patients ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}'
        """,
        """
        ALTER TABLE patients ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        """,
        """
        ALTER TABLE patients ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        """,
        """
        ALTER TABLE patients ADD COLUMN IF NOT EXISTS preferences TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE patients ADD COLUMN IF NOT EXISTS is_paused BOOLEAN NOT NULL DEFAULT FALSE
        """,
        """
        ALTER TABLE patients ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT ''
        """,
        """
        CREATE TABLE IF NOT EXISTS chat_history (
            id BIGSERIAL PRIMARY KEY,
            phone_number VARCHAR(30) NOT NULL,
            role VARCHAR(20) NOT NULL CHECK (role IN ('user','model','staff','system')),
            content TEXT NOT NULL,
            whatsapp_message_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS whatsapp_message_id TEXT
        """,
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id BIGSERIAL PRIMARY KEY,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            phone_number VARCHAR(30),
            details TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_chat_phone_created
        ON chat_history(phone_number, created_at, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_patients_updated
        ON patients(updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audit_created
        ON audit_log(created_at DESC)
        """,
    ]
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
            cur.execute(
                """
                INSERT INTO clinic_settings(key, content)
                VALUES ('system_instruction', %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (DEFAULT_SYSTEM_INSTRUCTION,),
            )
            cur.execute(
                """
                INSERT INTO clinic_settings(key, content)
                VALUES ('bot_globally_active', 'true')
                ON CONFLICT (key) DO NOTHING
                """
            )
        conn.commit()

# ------------------------------------------------------------
# Generic DB helpers
# ------------------------------------------------------------

def db_execute(sql: str, params=(), fetchone=False, fetchall=False, commit=True):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            result = cur.fetchone() if fetchone else cur.fetchall() if fetchall else None
        if commit:
            conn.commit()
    return result

def audit(actor: str, action: str, phone_number: str | None = None, details: str = ""):
    try:
        db_execute(
            "INSERT INTO audit_log(actor, action, phone_number, details) VALUES (%s,%s,%s,%s)",
            (actor, action, phone_number, details),
        )
    except Exception:
        log.exception("Audit write failed")

def get_setting(key: str, default: str = "") -> str:
    try:
        row = db_execute(
            "SELECT content FROM clinic_settings WHERE key=%s",
            (key,),
            fetchone=True,
        )
        return (row["content"] if row else default) or default
    except Exception:
        log.exception("get_setting failed")
        return default

def set_setting(key: str, content: str):
    db_execute(
        """
        INSERT INTO clinic_settings(key, content, updated_at)
        VALUES (%s,%s,NOW())
        ON CONFLICT(key)
        DO UPDATE SET content=EXCLUDED.content, updated_at=NOW()
        """,
        (key, content),
    )

def get_live_instructions() -> str:
    return get_setting("system_instruction", DEFAULT_SYSTEM_INSTRUCTION)

def save_live_instructions(content: str) -> bool:
    try:
        if not content.strip():
            return False
        set_setting("system_instruction", content.strip())
        audit("admin", "update_system_instruction")
        return True
    except Exception:
        log.exception("save_live_instructions failed")
        return False

def is_bot_globally_active() -> bool:
    return get_setting("bot_globally_active", "true").lower() == "true"

def set_bot_globally_active(active: bool):
    set_setting("bot_globally_active", "true" if active else "false")
    audit("admin", "global_bot_on" if active else "global_bot_off")

# ------------------------------------------------------------
# Patient / chat helpers
# ------------------------------------------------------------

def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("00"):
        digits = digits[2:]
    # Egyptian local numbers -> international form.
    if digits.startswith("01") and len(digits) == 11:
        digits = "2" + digits
    if digits.startswith("1") and len(digits) == 10:
        digits = "20" + digits
    return digits

def is_blocked(phone: str) -> bool:
    normalized = normalize_phone(phone)
    return normalized in BLOCKED_NUMBERS

def load_patient_profile(phone_number: str) -> dict:
    row = db_execute(
        """
        SELECT phone_number, name, preferences, tags, is_paused, created_at, updated_at
        FROM patients WHERE phone_number=%s
        """,
        (phone_number,),
        fetchone=True,
    )
    if not row:
        return {
            "phone_number": phone_number,
            "name": "",
            "preferences": "",
            "tags": [],
            "is_paused": False,
        }
    return dict(row)

def update_patient_file(phone_number: str, name: str = "", preferences: str = "") -> str:
    phone_number = normalize_phone(phone_number)
    db_execute(
        """
        INSERT INTO patients(phone_number, name, preferences)
        VALUES (%s,%s,%s)
        ON CONFLICT(phone_number)
        DO UPDATE SET
            name = CASE WHEN EXCLUDED.name <> '' THEN EXCLUDED.name ELSE patients.name END,
            preferences = CASE
                WHEN EXCLUDED.preferences = '' THEN patients.preferences
                WHEN patients.preferences = '' THEN EXCLUDED.preferences
                ELSE patients.preferences || ' | ' || EXCLUDED.preferences
            END,
            updated_at = NOW()
        """,
        (phone_number, (name or "").strip(), (preferences or "").strip()),
    )
    return "تم تحديث ملف العميل بنجاح."

def set_patient_pause(phone_number: str, paused: bool):
    phone_number = normalize_phone(phone_number)
    db_execute(
        """
        INSERT INTO patients(phone_number, is_paused)
        VALUES (%s,%s)
        ON CONFLICT(phone_number)
        DO UPDATE SET is_paused=EXCLUDED.is_paused, updated_at=NOW()
        """,
        (phone_number, paused),
    )

def set_patient_tags(phone_number: str, tags: list[str]):
    clean = []
    for tag in tags:
        tag = tag.strip()[:50]
        if tag and tag not in clean:
            clean.append(tag)
    db_execute(
        """
        INSERT INTO patients(phone_number, tags)
        VALUES (%s,%s)
        ON CONFLICT(phone_number)
        DO UPDATE SET tags=EXCLUDED.tags, updated_at=NOW()
        """,
        (normalize_phone(phone_number), clean),
    )

def save_chat_turn(
    phone_number: str,
    role: str,
    content: str,
    whatsapp_message_id: str | None = None,
):
    if not content:
        return
    db_execute(
        """
        INSERT INTO chat_history(phone_number, role, content, whatsapp_message_id)
        VALUES (%s,%s,%s,%s)
        """,
        (normalize_phone(phone_number), role, content, whatsapp_message_id),
    )

def load_chat_history(phone_number: str, limit: int = 12):
    rows = db_execute(
        """
        SELECT role, content
        FROM chat_history
        WHERE phone_number=%s
        ORDER BY id DESC
        LIMIT %s
        """,
        (normalize_phone(phone_number), max(1, min(limit, 50))),
        fetchall=True,
    )
    history = []
    for row in reversed(rows):
        role = "user" if row["role"] == "user" else "model"
        history.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=row["content"])],
            )
        )
    return history

# Atomic idempotency: INSERT succeeds for exactly one worker.
def claim_message(message_id: str) -> bool:
    if not message_id:
        return False
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO processed_messages(message_id)
                    VALUES (%s)
                    ON CONFLICT(message_id) DO NOTHING
                    RETURNING message_id
                    """,
                    (message_id,),
                )
                claimed = cur.fetchone() is not None
            conn.commit()
        return claimed
    except Exception:
        log.exception("claim_message failed")
        # Fail closed: do not send a possible duplicate reply when idempotency is unavailable.
        return False

# ------------------------------------------------------------
# Lock management
# ------------------------------------------------------------

async def get_user_lock(phone: str) -> asyncio.Lock:
    async with lock_guard:
        lock = user_locks.get(phone)
        if lock is None:
            lock = asyncio.Lock()
            user_locks[phone] = lock
        lock_last_used[phone] = dt.datetime.now(dt.timezone.utc)
        return lock

async def cleanup_locks():
    while True:
        await asyncio.sleep(1800)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        async with lock_guard:
            for phone in list(user_locks):
                lock = user_locks[phone]
                if not lock.locked() and lock_last_used.get(phone, cutoff) < cutoff:
                    user_locks.pop(phone, None)
                    lock_last_used.pop(phone, None)

# ------------------------------------------------------------
# Appointment / Google Apps Script helpers
# ------------------------------------------------------------

CLINIC_TIMES = [
    f"{hour:02d}:{minute:02d} {ampm}"
    for hour in range(12, 11)  # replaced below
    for minute in (0, 30)
    for ampm in ("PM",)
]
CLINIC_TIMES = [
    "12:00 PM","12:30 PM","1:00 PM","1:30 PM","2:00 PM","2:30 PM",
    "3:00 PM","3:30 PM","4:00 PM","4:30 PM","5:00 PM","5:30 PM",
    "6:00 PM","6:30 PM","7:00 PM","7:30 PM","8:00 PM","8:30 PM",
    "9:00 PM","9:30 PM","10:00 PM"
]

def normalize_time(value: str) -> str:
    raw = (value or "").strip().upper().replace(".", "")
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            return dt.datetime.strptime(raw, fmt).strftime("%-I:%M %p")
        except ValueError:
            pass
    # Windows-compatible fallback for environments where %-I is unsupported.
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            x = dt.datetime.strptime(raw, fmt)
            return x.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            pass
    return raw

def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat((value or "").strip())

def validate_booking(date_str: str, time_str: str):
    try:
        date = parse_date(date_str)
    except ValueError:
        raise ValueError("صيغة التاريخ غير صحيحة. استخدم YYYY-MM-DD.")

    if date.weekday() == 4:
        raise ValueError("الجمعة إجازة ولا يمكن الحجز فيها.")

    time_norm = normalize_time(time_str)
    if time_norm not in CLINIC_TIMES:
        raise ValueError("الموعد يجب أن يكون بين 12:00 PM و10:00 PM وبفواصل نصف ساعة.")

    return date, time_norm

def google_get(params: dict) -> dict:
    with httpx.Client(follow_redirects=True, timeout=15.0) as c:
        r = c.get(GOOGLE_SHEET_URL, params=params)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError("Google Sheet returned invalid JSON.")
        return data

def google_post(payload: dict) -> dict:
    with httpx.Client(follow_redirects=True, timeout=20.0) as c:
        r = c.post(GOOGLE_SHEET_URL, json=payload)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError("Google Sheet returned invalid JSON.")
        return data

def check_schedule(date: str) -> str:
    try:
        parse_date(date)
        data = google_get({"date": date})
        booked = data.get("booked", [])
        if not booked:
            return f"يوم {date} متاح بالكامل."

        times = []
        for item in booked:
            if isinstance(item, dict):
                times.append(item.get("time", ""))
            else:
                times.append(str(item))
        times = [x for x in times if x]
        return (
            f"المواعيد المحجوزة مسبقاً يوم {date} هي: {', '.join(times)}"
            if times else f"يوم {date} متاح بالكامل."
        )
    except Exception:
        log.exception("check_schedule failed")
        return "لا يمكن قراءة الجدول الآن."

def check_patient_appointments(phone_number: str) -> str:
    try:
        data = google_get({"phone": normalize_phone(phone_number)})
        appointments = data.get("appointments", [])
        if appointments:
            return f"حجوزات العميل الحالية: {', '.join(map(str, appointments))}"
        return "لا يوجد حجوزات سابقة أو قادمة لهذا العميل."
    except Exception:
        log.exception("check_patient_appointments failed")
        return "فشل في قراءة حجوزات العميل."

def cancel_appointment(phone_number: str, date: str) -> str:
    try:
        parse_date(date)
        data = google_post({
            "action": "cancel",
            "phone_number": normalize_phone(phone_number),
            "date": date,
        })
        if data.get("deleted"):
            return f"تم إلغاء الحجز القديم يوم {date} بنجاح."
        return f"لم يتم العثور على حجز لإلغائه في يوم {date}."
    except Exception:
        log.exception("cancel_appointment failed")
        return "فشل الاتصال بنظام الإلغاء."

def book_appointment(
    patient_name: str,
    phone_number: str,
    date: str,
    time: str,
    area: str,
) -> str:
    if not patient_name.strip():
        return "فشل الحجز: اسم العميل مطلوب."
    if not area.strip():
        return "فشل الحجز: المنطقة مطلوبة."

    try:
        _, standard_time = validate_booking(date, time)
        phone = normalize_phone(phone_number)
        if len(phone) < 10:
            return "فشل الحجز: رقم الهاتف غير صحيح."

        # IMPORTANT: the Apps Script remains the source of truth for appointment slots.
        data = google_post({
            "action": "book",
            "patient_name": patient_name.strip(),
            "phone_number": phone,
            "branch": "مدينة نصر",
            "date": date,
            "time": standard_time,
            "area": area.strip(),
        })
        if data.get("status") == "error":
            return f"فشل الحجز: {data.get('message', 'خطأ غير معروف')}"

        update_patient_file(
            phone,
            patient_name,
            f"حجز {area.strip()} ({date} {standard_time})",
        )
        audit("bot", "appointment_booked", phone, f"{date} {standard_time} - {area}")
        return (
            f"تم تسجيل الحجز بنجاح باسم {patient_name.strip()} "
            f"بفرع مدينة نصر يوم {date} الساعة {standard_time} "
            f"لمنطقة {area.strip()}."
        )
    except ValueError as e:
        return f"فشل الحجز: {e}"
    except Exception:
        log.exception("book_appointment failed")
        return "فشل الاتصال بنظام الحجز. لم يتم تأكيد الموعد."

# ------------------------------------------------------------
# WhatsApp outbound
# ------------------------------------------------------------

async def send_whatsapp_message(to: str, text: str, phone_id: str | None = None):
    phone_id = phone_id or PHONE_NUMBER_ID
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_phone(to),
        "type": "text",
        "text": {"body": text[:4096]},
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
                res = await c.post(url, headers=headers, json=payload)
                if 200 <= res.status_code < 300:
                    return True
                log.warning("WhatsApp text %s: %s", res.status_code, res.text[:500])
        except Exception:
            log.exception("WhatsApp text attempt %s failed", attempt + 1)
        await asyncio.sleep(1.5 * (attempt + 1))
    return False

async def send_whatsapp_image(to: str, image: dict, phone_id: str | None = None):
    phone_id = phone_id or PHONE_NUMBER_ID
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_phone(to),
        "type": "image",
        "image": {
            "link": image["url"],
            "caption": image.get("caption", "")[:1024],
        },
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
                res = await c.post(url, headers=headers, json=payload)
                if 200 <= res.status_code < 300:
                    return True
                log.warning("WhatsApp image %s: %s", res.status_code, res.text[:500])
        except Exception:
            log.exception("WhatsApp image attempt %s failed", attempt + 1)
        await asyncio.sleep(1.5 * (attempt + 1))
    return False

async def notify_staff(phone: str, issue_summary: str):
    if not STAFF_NOTIFICATION_PHONE:
        log.warning("STAFF_NOTIFICATION_PHONE is not configured: %s", issue_summary)
        return False
    body = (
        "🚨 تنبيه استفسار يحتاج متابعة\n\n"
        f"📱 رقم المريض: {phone}\n"
        f"📝 المشكلة: {issue_summary[:1000]}"
    )
    ok = await send_whatsapp_message(STAFF_NOTIFICATION_PHONE, body)
    audit("bot", "staff_notification", phone, issue_summary[:1000])
    return ok

# ------------------------------------------------------------
# Gemini
# ------------------------------------------------------------

gemini_client = None
try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception:
    log.exception("Gemini client initialization failed")

def build_system_instruction(profile: dict, phone: str) -> str:
    now = dt.datetime.now(CAIRO)
    days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    today = f"{now.strftime('%Y-%m-%d')} (اليوم هو: {days[now.weekday()]})"

    return f"""
{get_live_instructions()}

=== سياق العميل ===
- العميل: {profile.get('name') or 'عميل جديد'}
- الملاحظات: {profile.get('preferences') or 'لا يوجد'}
- العلامات: {', '.join(profile.get('tags') or []) or 'لا يوجد'}
- رقم الهاتف: {phone}
- التاريخ والوقت الحالي في القاهرة: {today} {now.strftime('%I:%M %p')}

=== قواعد الأمان للحجز ===
- لا تخترعي توفر موعد.
- استخدمي check_schedule قبل book_appointment.
- لا تعيدي تأكيد الحجز إلا بعد نجاح book_appointment.
- إذا كانت البيانات ناقصة، اسألي فقط عن البيانات الناقصة.
"""

def generate_ai_reply_sync(phone: str, user_message: str, profile: dict):
    if gemini_client is None:
        return "أهلاً بحضرتك يا فندم 🌸 حصل عطل مؤقت. برجاء المحاولة بعد قليل.", []

    queued_images = []

    def send_clinic_media(media_types: list[str]) -> str:
        valid = [m for m in media_types if m in OFFER_IMAGES]
        for m in valid:
            if OFFER_IMAGES[m] not in queued_images:
                queued_images.append(OFFER_IMAGES[m])
        return (
            f"Images queued: {', '.join(valid)}. "
            "The application will send them after your response."
            if valid else "No valid media requested."
        )

    def notify_staff_tool(issue_summary: str) -> str:
        # Tool functions are synchronous because Gemini tool execution is synchronous.
        try:
            if STAFF_NOTIFICATION_PHONE:
                body = (
                    "🚨 تنبيه استفسار يحتاج متابعة\n\n"
                    f"📱 رقم المريض: {phone}\n"
                    f"📝 المشكلة: {issue_summary[:1000]}"
                )
                with httpx.Client(timeout=10.0) as c:
                    r = c.post(
                        f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages",
                        headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
                        json={
                            "messaging_product": "whatsapp",
                            "to": normalize_phone(STAFF_NOTIFICATION_PHONE),
                            "type": "text",
                            "text": {"body": body},
                        },
                    )
                    r.raise_for_status()
            audit("bot", "staff_notification", phone, issue_summary[:1000])
            return "Staff notification attempted."
        except Exception:
            log.exception("notify_staff tool failed")
            return "Staff notification failed."

    try:
        chat = gemini_client.chats.create(
            model=GEMINI_MODEL,
            history=load_chat_history(phone, 12),
            config=types.GenerateContentConfig(
                system_instruction=build_system_instruction(profile, phone),
                temperature=0.1,
                tools=[
                    check_schedule,
                    check_patient_appointments,
                    cancel_appointment,
                    book_appointment,
                    update_patient_file,
                    send_clinic_media,
                    notify_staff_tool,
                ],
            ),
        )
        result = chat.send_message(user_message)
        return (result.text or "").strip(), queued_images
    except Exception:
        log.exception("Gemini generation failed")
        return "أهلاً بحضرتك يا فندم 🌸 ثواني وهكون مع حضرتك.", []

async def generate_ai_reply(phone: str, message: str, profile: dict):
    return await asyncio.to_thread(generate_ai_reply_sync, phone, message, profile)

# ------------------------------------------------------------
# Conversation handling
# ------------------------------------------------------------

async def handle_ai_conversation(
    sender_phone: str,
    user_text: str,
    phone_number_id: str,
    message_id: str | None = None,
):
    sender_phone = normalize_phone(sender_phone)
    lock = await get_user_lock(sender_phone)

    async with lock:
        save_chat_turn(sender_phone, "user", user_text, message_id)

        if not is_bot_globally_active():
            log.info("Global bot off; ignoring %s", sender_phone)
            return

        profile = load_patient_profile(sender_phone)
        if profile.get("is_paused"):
            log.info("Human takeover active for %s", sender_phone)
            return

        response_text, images = await generate_ai_reply(sender_phone, user_text, profile)

        for image in images:
            await send_whatsapp_image(sender_phone, image, phone_number_id)

        if response_text:
            ok = await send_whatsapp_message(sender_phone, response_text, phone_number_id)
            if ok:
                save_chat_turn(sender_phone, "model", response_text)
            else:
                audit("system", "whatsapp_send_failed", sender_phone, response_text[:500])

# ------------------------------------------------------------
# Webhook parsing
# ------------------------------------------------------------

def extract_text_message(message: dict) -> str | None:
    msg_type = message.get("type")
    if msg_type == "text":
        return (message.get("text", {}).get("body") or "").strip()
    if msg_type == "image":
        return (message.get("image", {}).get("caption") or "[قام المريض بإرسال صورة]").strip()
    if msg_type in {"audio", "video", "document", "sticker", "location", "contacts"}:
        labels = {
            "audio": "[أرسل المريض رسالة صوتية]",
            "video": "[أرسل المريض فيديو]",
            "document": "[أرسل المريض مستنداً]",
            "sticker": "[أرسل المريض ملصقاً]",
            "location": "[أرسل المريض موقعاً]",
            "contacts": "[أرسل المريض جهة اتصال]",
        }
        return labels[msg_type]
    return None

def extract_echo_text(echo: dict) -> str:
    msg_type = echo.get("type")
    if msg_type == "text":
        return (echo.get("text", {}).get("body") or "").strip()
    if msg_type == "image":
        return (echo.get("image", {}).get("caption") or "[أرسلت موظفة الاستقبال صورة]").strip()
    return f"[رسالة من العيادة: {msg_type or 'unknown'}]"

@app.get("/")
def home():
    return {"status": "ok", "service": "Jothen Clinic Nasr City AI Receptionist", "version": "3.0.0"}

@app.get("/config-status")
def config_status():
    # Safe diagnostic endpoint: never return tokens, passwords, or database URLs.
    return {
        "database_configured": bool(DATABASE_URL),
        "whatsapp_configured": bool(WHATSAPP_ACCESS_TOKEN),
        "verify_token_configured": bool(VERIFY_TOKEN),
        "gemini_configured": bool(GEMINI_API_KEY),
        "apps_script_configured": bool(GOOGLE_SHEET_URL),
        "phone_number_id_configured": bool(PHONE_NUMBER_ID),
        "real_clinic_enabled": ENABLE_REAL_CLINIC,
        "gemini_model": GEMINI_MODEL,
        "version": "3.0.0",
    }

@app.get("/health")
def health():
    try:
        db_execute("SELECT 1", fetchone=True)
        return {"status": "healthy", "database": "ok", "bot_active": is_bot_globally_active()}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

@app.get("/webhook")
def verify_webhook(request: Request):
    if (
        request.query_params.get("hub.mode") == "subscribe"
        and secrets.compare_digest(
            request.query_params.get("hub.verify_token", ""),
            VERIFY_TOKEN,
        )
    ):
        return PlainTextResponse(request.query_params.get("hub.challenge", ""))
    return Response(content="Verification failed", status_code=403)

@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except Exception:
        return Response(content="Invalid JSON", status_code=400)

    # Do not log full webhook bodies: they can contain patient PII.
    log.info("WhatsApp webhook received")

    scheduled = 0

    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                field = change.get("field", "")
                value = change.get("value") or {}
                target_phone_id = value.get("metadata", {}).get(
                    "phone_number_id",
                    PHONE_NUMBER_ID,
                )

                # Patient messages.
                if field != "smb_message_echoes":
                    for message in value.get("messages", []) or []:
                        message_id = message.get("id")
                        if not claim_message(message_id):
                            continue

                        sender = normalize_phone(message.get("from", ""))
                        if not sender or is_blocked(sender):
                            continue

                        text = extract_text_message(message)
                        if not text:
                            continue

                        if ENABLE_REAL_CLINIC:
                            background_tasks.add_task(
                                handle_ai_conversation,
                                sender,
                                text,
                                target_phone_id,
                                message_id,
                            )
                            scheduled += 1
                        else:
                            log.info("ENABLE_REAL_CLINIC is off; message stored but not answered: %s", sender)

                # Staff/manual messages echoed by Meta.
                echoes = (
                    value.get("smb_message_echoes")
                    or value.get("message_echoes")
                    or (value.get("messages") if field == "smb_message_echoes" else [])
                )
                for echo in echoes or []:
                    echo_id = echo.get("id")
                    if not claim_message(echo_id):
                        continue

                    customer_phone = normalize_phone(
                        value.get("recipient_id")
                        or (
                            value.get("contacts", [{}])[0].get("wa_id")
                            if value.get("contacts") else ""
                        )
                        or echo.get("to")
                        or ""
                    )
                    if not customer_phone:
                        continue

                    text = extract_echo_text(echo)
                    save_chat_turn(customer_phone, "staff", text, echo_id)
                    set_patient_pause(customer_phone, True)
                    audit(
                        "staff",
                        "manual_message_echo",
                        customer_phone,
                        text[:1000],
                    )

    except Exception:
        log.exception("Webhook processing error")
        # Return 200 after receipt to avoid repeated Meta retries for application errors.
        # The failed event is visible in Render logs.
    return {"status": "EVENT_RECEIVED", "scheduled": scheduled}

# ------------------------------------------------------------
# Admin authentication / models
# ------------------------------------------------------------

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    valid_user = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    valid_pass = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
            detail="Unauthorized",
        )
    return credentials.username

class PauseRequest(BaseModel):
    phone_number: str = Field(min_length=5, max_length=30)
    is_paused: bool

class SettingsUpdate(BaseModel):
    instruction: str = Field(min_length=20, max_length=50000)

class BookReq(BaseModel):
    patient_name: str = Field(min_length=1, max_length=200)
    phone_number: str = Field(min_length=5, max_length=30)
    date: str
    time: str
    area: str = Field(min_length=1, max_length=200)

class CancelReq(BaseModel):
    phone_number: str = Field(min_length=5, max_length=30)
    date: str

class RenamePatientReq(BaseModel):
    phone_number: str = Field(min_length=5, max_length=30)
    name: str = Field(min_length=1, max_length=200)

class GlobalBotReq(BaseModel):
    is_active: bool

class StaffMessageReq(BaseModel):
    phone_number: str = Field(min_length=5, max_length=30)
    message: str = Field(min_length=1, max_length=4096)
    pause_after_send: bool = True

class PatientTagsReq(BaseModel):
    phone_number: str = Field(min_length=5, max_length=30)
    tags: list[str] = Field(default_factory=list, max_length=20)

class PatientPreferencesReq(BaseModel):
    phone_number: str = Field(min_length=5, max_length=30)
    preferences: str = Field(default="", max_length=10000)

# ------------------------------------------------------------
# Admin API
# ------------------------------------------------------------

@app.post("/admin/api/toggle_pause")
def api_toggle_pause(req: PauseRequest, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    set_patient_pause(phone, req.is_paused)
    audit(admin, "patient_pause_on" if req.is_paused else "patient_pause_off", phone)
    return {"status": "success", "is_paused": req.is_paused}

@app.post("/admin/api/rename_patient")
def api_rename_patient(req: RenamePatientReq, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    update_patient_file(phone, name=req.name)
    audit(admin, "rename_patient", phone, req.name.strip())
    return {"status": "success"}

@app.post("/admin/api/patient_tags")
def api_patient_tags(req: PatientTagsReq, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    set_patient_tags(phone, req.tags)
    audit(admin, "update_patient_tags", phone, json.dumps(req.tags, ensure_ascii=False))
    return {"status": "success"}

@app.post("/admin/api/patient_preferences")
def api_patient_preferences(req: PatientPreferencesReq, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    update_patient_file(phone, preferences=req.preferences)
    audit(admin, "update_patient_preferences", phone, req.preferences[:1000])
    return {"status": "success"}

@app.get("/admin/api/settings")
def get_settings(admin: str = Depends(verify_admin)):
    return {"instruction": get_live_instructions()}

@app.post("/admin/api/settings")
def update_settings(data: SettingsUpdate, admin: str = Depends(verify_admin)):
    if not save_live_instructions(data.instruction):
        raise HTTPException(status_code=500, detail="Failed to save settings")
    return {"status": "success"}

@app.get("/admin/api/bot_status")
def api_get_bot_status(admin: str = Depends(verify_admin)):
    return {"is_active": is_bot_globally_active()}

@app.post("/admin/api/toggle_global_bot")
def api_toggle_global_bot(req: GlobalBotReq, admin: str = Depends(verify_admin)):
    set_bot_globally_active(req.is_active)
    return {"status": "success", "is_active": req.is_active}

@app.get("/admin/api/schedule")
def api_get_schedule(date: str, admin: str = Depends(verify_admin)):
    try:
        parse_date(date)
        return google_get({"date": date})
    except Exception:
        log.exception("Admin schedule read failed")
        raise HTTPException(status_code=502, detail="Unable to read appointment schedule")

@app.post("/admin/api/book")
def api_admin_book(req: BookReq, admin: str = Depends(verify_admin)):
    result = book_appointment(
        req.patient_name,
        req.phone_number,
        req.date,
        req.time,
        req.area,
    )
    return {"status": result}

@app.post("/admin/api/cancel")
def api_admin_cancel(req: CancelReq, admin: str = Depends(verify_admin)):
    result = cancel_appointment(req.phone_number, req.date)
    audit(admin, "appointment_cancel", normalize_phone(req.phone_number), req.date)
    return {"status": result}

@app.post("/admin/api/send_message")
async def api_send_message(req: StaffMessageReq, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    ok = await send_whatsapp_message(phone, req.message)
    if not ok:
        raise HTTPException(status_code=502, detail="WhatsApp message could not be sent")
    save_chat_turn(phone, "staff", req.message)
    if req.pause_after_send:
        set_patient_pause(phone, True)
    audit(admin, "staff_manual_message", phone, req.message[:1000])
    return {"status": "success", "paused": req.pause_after_send}

@app.get("/admin/api/data")
def get_admin_data(
    search: str = Query("", max_length=100),
    paused: bool | None = None,
    limit: int = Query(100, ge=1, le=500),
    include_chats: bool = Query(True),
    admin: str = Depends(verify_admin),
):
    search = search.strip()
    params = []
    where = []

    if search:
        where.append("(active.phone_number ILIKE %s OR COALESCE(p.name,'') ILIKE %s)")
        like = f"%{search}%"
        params.extend([like, like])
    if paused is not None:
        where.append("COALESCE(p.is_paused,FALSE) = %s")
        params.append(paused)

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    patients = db_execute(
        f"""
        SELECT
            active.phone_number,
            COALESCE(p.name,'') AS name,
            COALESCE(p.preferences,'') AS preferences,
            COALESCE(p.tags,'{{}}') AS tags,
            COALESCE(p.is_paused,FALSE) AS is_paused,
            p.created_at,
            p.updated_at,
            MAX(c.created_at) AS last_msg_time,
            MAX(c.id) AS last_msg_id,
            (
                SELECT ch.content FROM chat_history ch
                WHERE ch.phone_number=active.phone_number
                ORDER BY ch.id DESC LIMIT 1
            ) AS last_message
        FROM (
            SELECT DISTINCT phone_number FROM chat_history
            UNION
            SELECT phone_number FROM patients
        ) active
        LEFT JOIN patients p ON active.phone_number=p.phone_number
        LEFT JOIN chat_history c ON active.phone_number=c.phone_number
        {where_sql}
        GROUP BY active.phone_number,p.name,p.preferences,p.tags,p.is_paused,p.created_at,p.updated_at
        ORDER BY last_msg_time DESC NULLS LAST, last_msg_id DESC NULLS LAST
        LIMIT %s
        """,
        (*params, limit),
        fetchall=True,
    )

    result = {"patients": patients}
    if include_chats:
        chats = db_execute(
            """
            SELECT id, phone_number, role, content, created_at
            FROM chat_history
            ORDER BY id DESC
            LIMIT 1000
            """,
            fetchall=True,
        )
        chats.reverse()
        result["chats"] = chats
    else:
        result["chats"] = []
    return result

@app.get("/admin/api/stats")
def admin_stats(admin: str = Depends(verify_admin)):
    row = db_execute(
        """
        SELECT
          (SELECT COUNT(*) FROM patients) AS patients,
          (SELECT COUNT(*) FROM chat_history) AS messages,
          (SELECT COUNT(*) FROM chat_history WHERE role='user'
             AND created_at >= NOW() - INTERVAL '24 hours') AS incoming_24h,
          (SELECT COUNT(*) FROM chat_history WHERE role='staff'
             AND created_at >= NOW() - INTERVAL '24 hours') AS staff_24h,
          (SELECT COUNT(*) FROM patients WHERE is_paused) AS human_takeovers
        """,
        fetchone=True,
    )
    return {**dict(row), "bot_active": is_bot_globally_active()}

@app.get("/admin/api/patient/{phone_number}")
def admin_patient(
    phone_number: str,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(60, ge=1, le=200),
    admin: str = Depends(verify_admin),
):
    phone = normalize_phone(phone_number)
    profile = load_patient_profile(phone)
    params = [phone]
    clause = ""
    if before_id is not None:
        clause = " AND id < %s"
        params.append(before_id)
    params.append(limit + 1)
    rows = db_execute(
        f"""
        SELECT id, role, content, created_at
        FROM chat_history
        WHERE phone_number=%s{clause}
        ORDER BY id DESC
        LIMIT %s
        """,
        tuple(params),
        fetchall=True,
    )
    has_more = len(rows) > limit
    messages = rows[:limit]
    messages.reverse()
    return {"patient": profile, "messages": messages, "has_more": has_more}

@app.get("/admin/api/audit")
def admin_audit(limit: int = Query(100, ge=1, le=500), admin: str = Depends(verify_admin)):
    rows = db_execute(
        """
        SELECT id, actor, action, phone_number, details, created_at
        FROM audit_log ORDER BY id DESC LIMIT %s
        """,
        (limit,),
        fetchall=True,
    )
    return {"audit": rows}

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(admin: str = Depends(verify_admin)):
    html = Path("static/admin.html").read_text(encoding="utf-8")
    return HTMLResponse(html)

# ------------------------------------------------------------
# Background lock cleanup
# ------------------------------------------------------------

# The cleanup loop is intentionally owned by the FastAPI lifespan above.
