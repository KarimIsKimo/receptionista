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
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, Response, BackgroundTasks, Depends, HTTPException, status, Query
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from receptionist.admin_ops import AdminOperations, failure, success
from receptionist.booking import BookingService, normalize_time
from receptionist.clinic import CLINIC, clinic_prompt
from receptionist.conversation import (
    combine_inbound_messages,
    evolve_booking_draft,
    should_extract_memory,
)
from receptionist.database import DatabasePool
from receptionist.dates import parse_date_expression

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

CAIRO = CLINIC.timezone
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

def bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(float(os.getenv(name, str(default))), maximum))
    except (TypeError, ValueError):
        return default

MESSAGE_DEBOUNCE_SECONDS = bounded_env_float(
    "MESSAGE_DEBOUNCE_SECONDS", 3.0, 0.25, 15.0
)
MESSAGE_PROCESSING_LEASE_SECONDS = bounded_env_float(
    "MESSAGE_PROCESSING_LEASE_SECONDS", 300.0, 30.0, 900.0
)
BOOKING_DRAFT_TIMEOUT_MINUTES = int(
    bounded_env_float("BOOKING_DRAFT_TIMEOUT_MINUTES", 30.0, 5.0, 240.0)
)
DB_POOL_MIN_CONNECTIONS = int(
    bounded_env_float("DB_POOL_MIN_CONNECTIONS", 1.0, 1.0, 4.0)
)
DB_POOL_MAX_CONNECTIONS = int(
    bounded_env_float("DB_POOL_MAX_CONNECTIONS", 4.0, 2.0, 8.0)
)
DB_POOL_MAX_CONNECTIONS = max(DB_POOL_MIN_CONNECTIONS, DB_POOL_MAX_CONNECTIONS)

database_pool = DatabasePool(
    DATABASE_URL,
    min_connections=DB_POOL_MIN_CONNECTIONS,
    max_connections=DB_POOL_MAX_CONNECTIONS,
    connect_kwargs={
        "sslmode": "require",
        "connect_timeout": 10,
        "application_name": "jothen-receptionist",
    },
)

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
1. إذا سأل العميل عن الموقع، استخدمي الفرع والعنوان من "بيانات العيادة الرسمية" أدناه، ثم استدعي send_clinic_media(["branches"]).
2. إذا سأل عن رقم الهاتف فلا تسأليه مرة أخرى؛ استخدمي الرقم المتاح في إعدادات العيادة إذا كان مسجلاً.
3. أسئلة الليزر الروتينية يتم الرد عليها من الـFAQ أدناه.
4. الأسئلة الطبية المعقدة أو الشكاوى أو Botox/Filler/Dermatology:
   استدعي notify_staff ثم اعتذري بلطف أن دورك يقتصر على حجوزات الليزر والمعلومات الأساسية.
   لا تقولي إن الطبيب/الإدارة سيتواصل معه.
5. السعر غير الموجود في قاعدة الأسعار:
   استدعي notify_staff ثم قولي إن الأسعار المتاحة لديك هي الأسعار القياسية فقط.
6. اتبعي أيام وساعات الحجز الموجودة في "بيانات العيادة الرسمية" أدناه؛ فهي المرجع النهائي.
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
    database_pool.start()
    try:
        init_db()
        cleanup_task = asyncio.create_task(cleanup_locks())
        queue_task = asyncio.create_task(pending_queue_worker())
        log.info("Jothen receptionist started")
        try:
            yield
        finally:
            cleanup_task.cancel()
            queue_task.cancel()
            try:
                await asyncio.gather(cleanup_task, queue_task)
            except asyncio.CancelledError:
                pass
            log.info("Jothen receptionist stopped")
    finally:
        database_pool.close()

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
    return database_pool.connection()

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
        CREATE TABLE IF NOT EXISTS inbound_message_queue (
            message_id TEXT PRIMARY KEY,
            phone_number VARCHAR(30) NOT NULL,
            chat_history_id BIGINT NOT NULL UNIQUE REFERENCES chat_history(id) ON DELETE CASCADE,
            phone_number_id TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processing_started_at TIMESTAMPTZ,
            side_effects_started_at TIMESTAMPTZ,
            side_effects_completed_at TIMESTAMPTZ,
            recovery_state VARCHAR(40),
            processed_at TIMESTAMPTZ
        )
        """,
        """
        ALTER TABLE inbound_message_queue
        ADD COLUMN IF NOT EXISTS side_effects_started_at TIMESTAMPTZ
        """,
        """
        ALTER TABLE inbound_message_queue
        ADD COLUMN IF NOT EXISTS side_effects_completed_at TIMESTAMPTZ
        """,
        """
        ALTER TABLE inbound_message_queue
        ADD COLUMN IF NOT EXISTS recovery_state VARCHAR(40)
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_processing_leases (
            phone_number VARCHAR(30) PRIMARY KEY,
            owner_token TEXT NOT NULL,
            lease_until TIMESTAMPTZ NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS booking_drafts (
            phone_number VARCHAR(30) PRIMARY KEY,
            intent VARCHAR(30) NOT NULL DEFAULT '',
            patient_name TEXT NOT NULL DEFAULT '',
            service_area TEXT NOT NULL DEFAULT '',
            requested_date TEXT NOT NULL DEFAULT '',
            requested_time TEXT NOT NULL DEFAULT '',
            stage VARCHAR(40) NOT NULL DEFAULT 'idle',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
        CREATE TABLE IF NOT EXISTS admin_inbox_state (
            phone_number VARCHAR(30) PRIMARY KEY,
            last_read_message_id BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE SEQUENCE IF NOT EXISTS conversation_summary_change_seq
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            phone_number VARCHAR(30) PRIMARY KEY,
            last_message_id BIGINT,
            last_message_role VARCHAR(20),
            last_message TEXT,
            last_message_at TIMESTAMPTZ,
            first_meaningful_role VARCHAR(20),
            latest_patient_message_id BIGINT NOT NULL DEFAULT 0,
            latest_patient_message_at TIMESTAMPTZ,
            latest_response_message_id BIGINT NOT NULL DEFAULT 0,
            latest_response_at TIMESTAMPTZ,
            unread_count INTEGER NOT NULL DEFAULT 0,
            is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
            is_archived BOOLEAN NOT NULL DEFAULT FALSE,
            change_version BIGINT NOT NULL DEFAULT nextval('conversation_summary_change_seq'),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS admin_appointment_snapshots (
            phone_number VARCHAR(30) PRIMARY KEY,
            appointments JSONB,
            next_appointment JSONB,
            status VARCHAR(20) NOT NULL DEFAULT 'unknown',
            fetched_at TIMESTAMPTZ
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_chat_phone_created
        ON chat_history(phone_number, created_at, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_chat_phone_id_desc
        ON chat_history(phone_number, id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_chat_user_phone_id
        ON chat_history(phone_number, id)
        WHERE role='user'
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_chat_origin_phone_id
        ON chat_history(phone_number, id)
        WHERE role IN ('user','staff')
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inbound_queue_pending
        ON inbound_message_queue(phone_number, received_at, chat_history_id)
        WHERE processed_at IS NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_patients_updated
        ON patients(updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audit_created
        ON audit_log(created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_summary_activity
        ON conversation_summaries(is_archived, is_pinned DESC, last_message_at DESC, last_message_id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_summary_changes
        ON conversation_summaries(change_version)
        """,
        """
        /* conversation_summary_backfill */
        INSERT INTO conversation_summaries(
            phone_number,last_message_id,last_message_role,last_message,last_message_at,
            first_meaningful_role,latest_patient_message_id,latest_patient_message_at,
            latest_response_message_id,latest_response_at,unread_count,updated_at
        )
        SELECT
            active.phone_number,
            latest.id,latest.role,latest.content,latest.created_at,
            origin.role,
            COALESCE(patient_message.id,0),patient_message.created_at,
            COALESCE(response_message.id,0),response_message.created_at,
            COALESCE(unread.count,0),NOW()
        FROM (
            SELECT phone_number FROM patients
            UNION
            SELECT DISTINCT phone_number FROM chat_history
        ) active
        JOIN (
            SELECT 1 AS enabled
            WHERE NOT EXISTS (
                SELECT 1 FROM clinic_settings
                WHERE key='conversation_summary_backfill_v1'
            )
        ) migration ON TRUE
        LEFT JOIN conversation_summaries existing
          ON existing.phone_number=active.phone_number
        LEFT JOIN LATERAL (
            SELECT id,role,content,created_at
            FROM chat_history
            WHERE phone_number=active.phone_number
            ORDER BY id DESC LIMIT 1
        ) latest ON TRUE
        LEFT JOIN LATERAL (
            SELECT role
            FROM chat_history
            WHERE phone_number=active.phone_number AND role IN ('user','staff')
            ORDER BY id ASC LIMIT 1
        ) origin ON TRUE
        LEFT JOIN LATERAL (
            SELECT id,created_at
            FROM chat_history
            WHERE phone_number=active.phone_number AND role='user'
            ORDER BY id DESC LIMIT 1
        ) patient_message ON TRUE
        LEFT JOIN LATERAL (
            SELECT id,created_at
            FROM chat_history
            WHERE phone_number=active.phone_number AND role IN ('model','staff')
            ORDER BY id DESC LIMIT 1
        ) response_message ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*)::integer AS count
            FROM chat_history messages
            LEFT JOIN admin_inbox_state state
              ON state.phone_number=messages.phone_number
            WHERE messages.phone_number=active.phone_number
              AND messages.role='user'
              AND messages.id>COALESCE(state.last_read_message_id,0)
        ) unread ON TRUE
        WHERE existing.phone_number IS NULL
        ON CONFLICT(phone_number) DO NOTHING
        """,
        """
        INSERT INTO clinic_settings(key,content)
        VALUES ('conversation_summary_backfill_v1','complete')
        ON CONFLICT(key) DO NOTHING
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
    touch_conversation_summary(phone_number)
    return {"ok": True, "code": "patient_updated", "message": "تم تحديث ملف المريضة.", "patient_name": (name or "").strip()}

def set_patient_preferences(phone_number: str, preferences: str) -> dict:
    """Replace long-term patient preferences exactly."""
    phone = normalize_phone(phone_number)
    db_execute(
        """
        WITH patient AS (
            INSERT INTO patients(phone_number, preferences)
            VALUES (%s,%s)
            ON CONFLICT(phone_number)
            DO UPDATE SET preferences=EXCLUDED.preferences, updated_at=NOW()
            RETURNING phone_number
        )
        INSERT INTO conversation_summaries(phone_number,updated_at)
        SELECT phone_number,NOW() FROM patient
        WHERE TRUE
        ON CONFLICT(phone_number) DO UPDATE SET
            change_version=nextval('conversation_summary_change_seq'),
            updated_at=NOW()
        """,
        (phone, preferences),
    )
    return {
        "ok": True,
        "code": "patient_preferences_replaced",
        "phone_number": phone,
        "preferences": preferences,
    }

def set_extracted_patient_memory(
    phone_number: str,
    name: str,
    preferences: str,
) -> dict:
    """Persist Gemini's cleaned memory without overwriting a known patient name."""
    phone = normalize_phone(phone_number)
    clean_name = re.sub(r"\s+", " ", (name or "").strip())[:150]
    clean_preferences = re.sub(r"\s+", " ", (preferences or "").strip())[:4000]
    db_execute(
        """
        INSERT INTO patients(phone_number, name, preferences)
        VALUES (%s,%s,%s)
        ON CONFLICT(phone_number)
        DO UPDATE SET
            name = CASE
                WHEN COALESCE(BTRIM(patients.name), '') = ''
                THEN EXCLUDED.name
                ELSE patients.name
            END,
            preferences = EXCLUDED.preferences,
            updated_at = NOW()
        """,
        (phone, clean_name, clean_preferences),
    )
    touch_conversation_summary(phone)
    return {
        "phone_number": phone,
        "name": clean_name,
        "preferences": clean_preferences,
    }

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
    touch_conversation_summary(phone_number)

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
    touch_conversation_summary(phone_number)

def touch_conversation_summary(phone_number: str) -> None:
    """Advance the inbox state cursor after a profile-only change."""
    phone = normalize_phone(phone_number)
    db_execute(
        """
        /* conversation_summary_touch */
        INSERT INTO conversation_summaries(phone_number,updated_at)
        VALUES (%s,NOW())
        ON CONFLICT(phone_number) DO UPDATE SET
            change_version=nextval('conversation_summary_change_seq'),
            updated_at=NOW()
        """,
        (phone,),
    )

def update_conversation_summary_for_message(cur, phone: str, message: dict) -> None:
    """Update inbox state in the same transaction as a new chat record."""
    role = str(message.get("role") or "system")
    message_id = int(message["id"])
    created_at = message.get("created_at")
    cur.execute(
        """
        /* conversation_summary_message */
        INSERT INTO conversation_summaries(
            phone_number,last_message_id,last_message_role,last_message,last_message_at,
            first_meaningful_role,latest_patient_message_id,latest_patient_message_at,
            latest_response_message_id,latest_response_at,unread_count,updated_at
        ) VALUES (
            %s,%s,%s,%s,%s,
            CASE WHEN %s IN ('user','staff') THEN %s ELSE NULL END,
            CASE WHEN %s='user' THEN %s ELSE 0 END,
            CASE WHEN %s='user' THEN %s ELSE NULL END,
            CASE WHEN %s IN ('model','staff') THEN %s ELSE 0 END,
            CASE WHEN %s IN ('model','staff') THEN %s ELSE NULL END,
            CASE WHEN %s='user' THEN 1 ELSE 0 END,
            NOW()
        )
        ON CONFLICT(phone_number) DO UPDATE SET
            last_message_id=CASE
                WHEN EXCLUDED.last_message_id>=COALESCE(conversation_summaries.last_message_id,0)
                THEN EXCLUDED.last_message_id ELSE conversation_summaries.last_message_id END,
            last_message_role=CASE
                WHEN EXCLUDED.last_message_id>=COALESCE(conversation_summaries.last_message_id,0)
                THEN EXCLUDED.last_message_role ELSE conversation_summaries.last_message_role END,
            last_message=CASE
                WHEN EXCLUDED.last_message_id>=COALESCE(conversation_summaries.last_message_id,0)
                THEN EXCLUDED.last_message ELSE conversation_summaries.last_message END,
            last_message_at=CASE
                WHEN EXCLUDED.last_message_id>=COALESCE(conversation_summaries.last_message_id,0)
                THEN EXCLUDED.last_message_at ELSE conversation_summaries.last_message_at END,
            first_meaningful_role=COALESCE(
                conversation_summaries.first_meaningful_role,
                EXCLUDED.first_meaningful_role
            ),
            latest_patient_message_id=GREATEST(
                conversation_summaries.latest_patient_message_id,
                EXCLUDED.latest_patient_message_id
            ),
            latest_patient_message_at=CASE
                WHEN EXCLUDED.latest_patient_message_id>conversation_summaries.latest_patient_message_id
                THEN EXCLUDED.latest_patient_message_at
                ELSE conversation_summaries.latest_patient_message_at END,
            latest_response_message_id=GREATEST(
                conversation_summaries.latest_response_message_id,
                EXCLUDED.latest_response_message_id
            ),
            latest_response_at=CASE
                WHEN EXCLUDED.latest_response_message_id>conversation_summaries.latest_response_message_id
                THEN EXCLUDED.latest_response_at
                ELSE conversation_summaries.latest_response_at END,
            unread_count=conversation_summaries.unread_count +
                CASE WHEN EXCLUDED.last_message_role='user' THEN 1 ELSE 0 END,
            change_version=nextval('conversation_summary_change_seq'),
            updated_at=NOW()
        """,
        (
            phone,message_id,role,message.get("content") or "",created_at,
            role,role,role,message_id,role,created_at,
            role,message_id,role,created_at,role,
        ),
    )

def save_chat_turn(
    phone_number: str,
    role: str,
    content: str,
    whatsapp_message_id: str | None = None,
):
    if not content:
        return
    phone = normalize_phone(phone_number)
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO chat_history(phone_number, role, content, whatsapp_message_id)
                VALUES (%s,%s,%s,%s)
                RETURNING id, role, content, whatsapp_message_id, created_at
                """,
                (phone, role, content, whatsapp_message_id),
            )
            saved = dict(cur.fetchone())
            saved["phone_number"] = phone
            update_conversation_summary_for_message(cur, phone, saved)
        conn.commit()
    return saved

def history_content(role: str, content: str) -> types.Content:
    """Represent non-AI records without attributing them to the Gemini model."""
    if role == "model":
        gemini_role = "model"
        text = content
    elif role == "staff":
        gemini_role = "user"
        text = f"[سياق: هذه رسالة أرسلها موظف استقبال بشري للمريض، وليست رسالة من المريض]\n{content}"
    elif role == "system":
        gemini_role = "user"
        text = f"[سجل نظام داخلي/خطأ؛ لم يقله المريض ولا موظف الذكاء الاصطناعي]\n{content}"
    else:
        gemini_role = "user"
        text = content
    return types.Content(
        role=gemini_role,
        parts=[types.Part.from_text(text=text)],
    )

def normalize_gemini_history(history: list[types.Content]) -> list[types.Content]:
    """Return chronological, alternating Gemini turns beginning with a user."""
    normalized: list[types.Content] = []
    for content in history:
        role = content.role
        text = "\n".join(
            str(part.text)
            for part in (content.parts or [])
            if getattr(part, "text", None) is not None
        ).strip()
        if role not in {"user", "model"} or not text:
            continue
        if not normalized and role != "user":
            # A LIMIT window may cut off the user turn that preceded this model
            # response. Do not give Gemini an orphaned assistant statement.
            continue
        if normalized and normalized[-1].role == role:
            previous = "\n".join(
                str(part.text)
                for part in (normalized[-1].parts or [])
                if getattr(part, "text", None) is not None
            ).strip()
            normalized[-1] = types.Content(
                role=role,
                parts=[types.Part.from_text(text=f"{previous}\n\n{text}")],
            )
        else:
            normalized.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=text)],
                )
            )
    return normalized

def load_chat_history(
    phone_number: str,
    limit: int = 12,
    *,
    before_id: int | None = None,
):
    params: list = [normalize_phone(phone_number)]
    before_sql = ""
    if before_id is not None:
        before_sql = " AND id < %s"
        params.append(int(before_id))
    params.append(max(1, min(limit, 50)))
    rows = db_execute(
        f"""
        SELECT role, content
        FROM chat_history
        WHERE phone_number=%s
        {before_sql}
        ORDER BY id DESC
        LIMIT %s
        """,
        tuple(params),
        fetchall=True,
    )
    history = [history_content(row["role"], row["content"]) for row in reversed(rows)]
    return normalize_gemini_history(history)

def load_recent_memory_conversation(phone_number: str, limit: int = 20) -> list[dict]:
    """Load recent human-visible turns without converting staff messages to AI."""
    rows = db_execute(
        """
        SELECT role, content
        FROM chat_history
        WHERE phone_number=%s
          AND role IN ('user', 'model', 'staff')
        ORDER BY id DESC
        LIMIT %s
        """,
        (normalize_phone(phone_number), max(1, min(limit, 20))),
        fetchall=True,
    )
    return [
        {"role": row["role"], "content": row["content"]}
        for row in reversed(rows)
    ]

def persist_incoming_message(
    phone_number: str,
    content: str,
    message_id: str,
    phone_number_id: str,
    *,
    enqueue: bool = True,
) -> dict | None:
    """Atomically deduplicate, persist, and optionally queue an inbound message."""
    phone = normalize_phone(phone_number)
    if not message_id or not phone or not content:
        return None
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO processed_messages(message_id)
                VALUES (%s)
                ON CONFLICT(message_id) DO NOTHING
                RETURNING message_id
                """,
                (message_id,),
            )
            if cur.fetchone() is None:
                conn.commit()
                return None
            cur.execute(
                """
                INSERT INTO chat_history(phone_number, role, content, whatsapp_message_id)
                VALUES (%s,'user',%s,%s)
                RETURNING id, role, content, whatsapp_message_id, created_at
                """,
                (phone, content, message_id),
            )
            saved = dict(cur.fetchone())
            saved["phone_number"] = phone
            update_conversation_summary_for_message(cur, phone, saved)
            if enqueue:
                cur.execute(
                    """
                    INSERT INTO inbound_message_queue(
                        message_id, phone_number, chat_history_id, phone_number_id
                    ) VALUES (%s,%s,%s,%s)
                    """,
                    (message_id, phone, saved["id"], phone_number_id),
                )
        conn.commit()
    return saved

def load_booking_draft(phone_number: str) -> dict | None:
    phone = normalize_phone(phone_number)
    row = db_execute(
        """
        SELECT phone_number,intent,patient_name,service_area,requested_date,
               requested_time,stage,updated_at
        FROM booking_drafts
        WHERE phone_number=%s
        """,
        (phone,),
        fetchone=True,
    )
    if not row:
        return None
    draft = dict(row)
    updated_at = draft.get("updated_at")
    if isinstance(updated_at, str):
        try:
            updated_at = dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            updated_at = None
    if isinstance(updated_at, dt.datetime):
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=dt.timezone.utc)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            minutes=BOOKING_DRAFT_TIMEOUT_MINUTES
        )
        if updated_at.astimezone(dt.timezone.utc) < cutoff:
            clear_booking_draft(phone, "timeout")
            return None
    return draft

def save_booking_draft(phone_number: str, draft: dict) -> dict:
    phone = normalize_phone(phone_number)
    clean = {
        key: str(draft.get(key) or "").strip()
        for key in (
            "intent",
            "patient_name",
            "service_area",
            "requested_date",
            "requested_time",
            "stage",
        )
    }
    db_execute(
        """
        INSERT INTO booking_drafts(
            phone_number,intent,patient_name,service_area,requested_date,
            requested_time,stage,updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT(phone_number) DO UPDATE SET
            intent=EXCLUDED.intent,
            patient_name=EXCLUDED.patient_name,
            service_area=EXCLUDED.service_area,
            requested_date=EXCLUDED.requested_date,
            requested_time=EXCLUDED.requested_time,
            stage=EXCLUDED.stage,
            updated_at=NOW()
        """,
        (
            phone,
            clean["intent"],
            clean["patient_name"],
            clean["service_area"],
            clean["requested_date"],
            clean["requested_time"],
            clean["stage"],
        ),
    )
    return {"phone_number": phone, **clean}

def clear_booking_draft(phone_number: str, reason: str = "cleared") -> None:
    phone = normalize_phone(phone_number)
    db_execute("DELETE FROM booking_drafts WHERE phone_number=%s", (phone,))
    audit("bot", "booking_draft_cleared", phone, reason)

def update_booking_draft_from_message(
    phone_number: str,
    message: str,
    profile: dict,
) -> dict | None:
    current = load_booking_draft(phone_number)
    draft, action = evolve_booking_draft(current, message, profile, now=CLINIC.now())
    if action == "clear":
        clear_booking_draft(phone_number, "abandoned")
        return None
    if action == "upsert" and draft:
        return save_booking_draft(phone_number, draft)
    return current

def acquire_processing_lease(phone_number: str, owner_token: str) -> bool:
    row = db_execute(
        """
        INSERT INTO conversation_processing_leases(phone_number,owner_token,lease_until)
        VALUES (%s,%s,NOW() + (%s * INTERVAL '1 second'))
        ON CONFLICT(phone_number) DO UPDATE SET
            owner_token=EXCLUDED.owner_token,
            lease_until=EXCLUDED.lease_until
        WHERE conversation_processing_leases.lease_until < NOW()
        RETURNING owner_token
        """,
        (
            normalize_phone(phone_number),
            owner_token,
            MESSAGE_PROCESSING_LEASE_SECONDS,
        ),
        fetchone=True,
    )
    return bool(row and row.get("owner_token") == owner_token)

def refresh_processing_lease(phone_number: str, owner_token: str) -> bool:
    row = db_execute(
        """
        UPDATE conversation_processing_leases
        SET lease_until=NOW() + (%s * INTERVAL '1 second')
        WHERE phone_number=%s AND owner_token=%s
        RETURNING owner_token
        """,
        (
            MESSAGE_PROCESSING_LEASE_SECONDS,
            normalize_phone(phone_number),
            owner_token,
        ),
        fetchone=True,
    )
    return bool(row)

def release_processing_lease(phone_number: str, owner_token: str) -> None:
    db_execute(
        """
        DELETE FROM conversation_processing_leases
        WHERE phone_number=%s AND owner_token=%s
        """,
        (normalize_phone(phone_number), owner_token),
    )

def pending_batch_delay(phone_number: str) -> float | None:
    row = db_execute(
        """
        SELECT MAX(received_at) AS latest_received_at
        FROM inbound_message_queue
        WHERE phone_number=%s AND processed_at IS NULL
        """,
        (normalize_phone(phone_number),),
        fetchone=True,
    )
    latest = row.get("latest_received_at") if row else None
    if not latest:
        return None
    if isinstance(latest, str):
        latest = dt.datetime.fromisoformat(latest.replace("Z", "+00:00"))
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=dt.timezone.utc)
    ready_at = latest.astimezone(dt.timezone.utc) + dt.timedelta(
        seconds=MESSAGE_DEBOUNCE_SECONDS
    )
    return max(0.0, (ready_at - dt.datetime.now(dt.timezone.utc)).total_seconds())

def claim_pending_batch(phone_number: str) -> list[dict]:
    rows = db_execute(
        """
        WITH claimed AS (
            UPDATE inbound_message_queue
            SET processing_started_at=NOW()
            WHERE phone_number=%s AND processed_at IS NULL
            RETURNING message_id,phone_number,chat_history_id,phone_number_id,
                      received_at,side_effects_started_at,
                      side_effects_completed_at,recovery_state
        )
        SELECT claimed.message_id,claimed.phone_number,claimed.chat_history_id,
               claimed.phone_number_id,claimed.received_at,
               claimed.side_effects_started_at,
               claimed.side_effects_completed_at,claimed.recovery_state,
               history.content
        FROM claimed
        JOIN chat_history history ON history.id=claimed.chat_history_id
        ORDER BY claimed.chat_history_id ASC
        """,
        (normalize_phone(phone_number),),
        fetchall=True,
    )
    return [dict(row) for row in rows]

def reserve_pending_batch_side_effects(messages: list[dict]) -> bool:
    """Durably mark a batch before Gemini/tools/WhatsApp can cause side effects."""
    ids = [message["message_id"] for message in messages if message.get("message_id")]
    if not ids:
        return False
    rows = db_execute(
        """
        UPDATE inbound_message_queue
        SET side_effects_started_at=NOW(), recovery_state='in_progress'
        WHERE message_id=ANY(%s::text[])
          AND processed_at IS NULL
          AND side_effects_started_at IS NULL
        RETURNING message_id
        """,
        (ids,),
        fetchall=True,
    )
    return len(rows or []) == len(ids)

def mark_pending_batch_processed(messages: list[dict]) -> None:
    """Atomically record that the reserved batch finished in this process."""
    ids = [message["message_id"] for message in messages if message.get("message_id")]
    if ids:
        db_execute(
            """
            UPDATE inbound_message_queue
            SET side_effects_completed_at=NOW(),
                recovery_state='completed',
                processed_at=NOW()
            WHERE message_id=ANY(%s::text[])
              AND side_effects_started_at IS NOT NULL
            """,
            (ids,),
        )

def suppress_uncertain_pending_batch(messages: list[dict]) -> int:
    """Quarantine a recovered batch whose external effects may have happened.

    Apps Script and Meta do not share a transaction with Postgres. Once the
    durable pre-effect marker exists, replaying Gemini could duplicate a booking,
    cancellation, reschedule, or patient reply. Recovery therefore favors
    at-most-once external effects and leaves a visible system record for staff.
    """
    ids = [message["message_id"] for message in messages if message.get("message_id")]
    if not ids:
        return 0
    phone = normalize_phone(str(messages[0].get("phone_number") or ""))
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE inbound_message_queue
                SET recovery_state='suppressed_uncertain', processed_at=NOW()
                WHERE message_id=ANY(%s::text[])
                  AND processed_at IS NULL
                  AND side_effects_started_at IS NOT NULL
                  AND side_effects_completed_at IS NULL
                RETURNING message_id
                """,
                (ids,),
            )
            suppressed = cur.fetchall()
            if suppressed:
                details = ",".join(row["message_id"] for row in suppressed)[:1000]
                cur.execute(
                    """
                    INSERT INTO chat_history(phone_number,role,content)
                    VALUES (%s,'system',%s)
                    RETURNING id,role,content,whatsapp_message_id,created_at
                    """,
                    (
                        phone,
                        "تم إيقاف إعادة تنفيذ دفعة رسائل بعد انقطاع غير مؤكد لتجنب تكرار رد أو تغيير موعد. يلزم مراجعة المحادثة يدوياً.",
                    ),
                )
                saved_system_record = dict(cur.fetchone())
                saved_system_record["phone_number"] = phone
                update_conversation_summary_for_message(
                    cur,
                    phone,
                    saved_system_record,
                )
                cur.execute(
                    """
                    INSERT INTO audit_log(actor,action,phone_number,details)
                    VALUES ('system','inbound_recovery_suppressed',%s,%s)
                    """,
                    (phone, details),
                )
        conn.commit()
    return len(suppressed)

def list_pending_phones() -> list[str]:
    rows = db_execute(
        """
        SELECT DISTINCT queue.phone_number
        FROM inbound_message_queue queue
        LEFT JOIN conversation_processing_leases lease
          ON lease.phone_number=queue.phone_number AND lease.lease_until >= NOW()
        WHERE queue.processed_at IS NULL AND lease.phone_number IS NULL
        ORDER BY queue.phone_number
        LIMIT 50
        """,
        fetchall=True,
    )
    return [row["phone_number"] for row in rows]

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

booking_service = BookingService(
    GOOGLE_SHEET_URL,
    config=CLINIC,
    on_booked=lambda phone, name, preference: update_patient_file(
        phone, name=name, preferences=preference
    ),
    on_audit=lambda action, phone, details: audit("bot", action, phone, details),
)


def parse_date(value: str) -> dt.date:
    """Parse an absolute or relative date in the clinic's Cairo timezone."""
    return parse_date_expression(value, now=CLINIC.now(), timezone=CLINIC.timezone)


def validate_booking(date_str: str, time_str: str):
    date = booking_service._validate_date(date_str)
    return date, booking_service._validate_slot(date, time_str)


def google_get(params: dict) -> dict:
    return booking_service._request("GET", params)


def google_post(payload: dict) -> dict:
    return booking_service._request("POST", payload)


def check_schedule(date: str) -> dict:
    """Return structured available and booked slots for a date."""
    return booking_service.schedule(date)


def check_patient_appointments(phone_number: str) -> dict:
    """Return structured appointments for the supplied WhatsApp identity."""
    return booking_service.appointments(phone_number)


def cancel_appointment(
    phone_number: str,
    date: str,
    time: str | None = None,
    appointment_id: str | None = None,
) -> dict:
    """Cancel a patient's appointment and return a structured result."""
    return booking_service.cancel(phone_number, date, time, appointment_id)


def reschedule_appointment(
    phone_number: str,
    old_date: str,
    new_date: str,
    new_time: str,
    old_time: str | None = None,
    appointment_id: str | None = None,
) -> dict:
    """Move an existing appointment only when Apps Script confirms the change."""
    return booking_service.reschedule(
        phone_number,
        old_date,
        new_date,
        new_time,
        old_time,
        appointment_id,
    )


def book_appointment(
    patient_name: str,
    phone_number: str,
    date: str,
    time: str,
    area: str,
) -> dict:
    """Book only after re-checking the authoritative Apps Script schedule."""
    return booking_service.book(patient_name, phone_number, date, time, area)


# ------------------------------------------------------------
# WhatsApp outbound
# ------------------------------------------------------------

async def send_whatsapp_message(to: str, text: str, phone_id: str | None = None) -> dict:
    """Send text only when Meta returns an explicit accepted message id."""
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
                    try:
                        response_data = res.json()
                    except ValueError:
                        response_data = None
                    messages = response_data.get("messages") if isinstance(response_data, dict) else None
                    message_id = (
                        messages[0].get("id")
                        if isinstance(messages, list) and messages and isinstance(messages[0], dict)
                        else None
                    )
                    if message_id:
                        return {
                            "ok": True,
                            "code": "whatsapp_accepted",
                            "message_id": message_id,
                        }
                    log.warning("WhatsApp returned ambiguous success: %s", res.text[:500])
                else:
                    log.warning("WhatsApp text %s: %s", res.status_code, res.text[:500])
        except Exception:
            log.exception("WhatsApp text attempt %s failed", attempt + 1)
        await asyncio.sleep(1.5 * (attempt + 1))
    return {
        "ok": False,
        "code": "whatsapp_delivery_unconfirmed",
        "message": "WhatsApp did not explicitly confirm message acceptance.",
        "retryable": True,
    }

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
    delivery = await send_whatsapp_message(STAFF_NOTIFICATION_PHONE, body)
    audit("bot", "staff_notification", phone, issue_summary[:1000])
    return delivery.get("ok") is True

# ------------------------------------------------------------
# Gemini
# ------------------------------------------------------------

gemini_client = None
try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception:
    log.exception("Gemini client initialization failed")

admin_operations = AdminOperations(db_execute, booking_service, config=CLINIC)

def build_system_instruction(
    profile: dict,
    phone: str,
    booking_draft: dict | None = None,
) -> str:
    now = dt.datetime.now(CAIRO)
    days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    today = f"{now.strftime('%Y-%m-%d')} (اليوم هو: {days[now.weekday()]})"

    draft_context = {
        key: str((booking_draft or {}).get(key) or "")
        for key in (
            "intent",
            "patient_name",
            "service_area",
            "requested_date",
            "requested_time",
            "stage",
        )
    }

    return f"""
{get_live_instructions()}

{clinic_prompt()}

=== سياق العميل ===
- العميل: {profile.get('name') or 'عميل جديد'}
- الملاحظات: {profile.get('preferences') or 'لا يوجد'}
- العلامات: {', '.join(profile.get('tags') or []) or 'لا يوجد'}
- رقم الهاتف: {phone}
- التاريخ والوقت الحالي في القاهرة: {today} {now.strftime('%I:%M %p')}

=== مسودة الحجز المحفوظة ===
{json.dumps(draft_context, ensure_ascii=False)}
- القيم غير الفارغة هنا لا تُسجل إلا عندما يستطيع المحلل الحتمي استخراجها
  بشكل صريح ومحافظ ومن دون إسقاط جزء من كلام المريضة.
- هذه القيم سياق منظم وليست تأكيداً أن حجزاً تم. تأكيد التنفيذ يأتي فقط من
  نتيجة أداة الحجز المناسبة عندما تكون ok=true.
- لا تسألي مرة أخرى عن قيمة غير فارغة إلا إذا ناقضتها الرسالة الحالية بوضوح.
- القيمة الفارغة تعني أن المحلل لم يكن واثقاً؛ افهمي الرسالة الحالية أو اطلبي
  توضيحاً ولا تخمني.
- إذا كان intent=check_appointment فاستخدمي check_my_appointments ولا تنشئي
  حجزاً جديداً.
- إذا كان intent=reschedule فلا تعتبري requested_date/requested_time موعداً
  جديداً؛ ميزي الموعد القديم والجديد صراحة من كلام المريضة ونتائج الأدوات.
- إذا كانت الرسالة الحالية جزءاً قصيراً مثل "7"، استخدميها مع مرحلة المسودة لفهمها.

=== قواعد الأمان للحجز ===
- لا تخترعي توفر موعد.
- استخدمي check_schedule قبل book_appointment.
- لا تعيدي تأكيد الحجز إلا بعد نجاح book_appointment.
- رقم الهاتف يأتي تلقائياً من واتساب. ممنوع سؤال المريضة عن رقمها.
- إذا كان الاسم محفوظاً أعلاه فلا تسألي عنه مرة أخرى.
- إذا كانت البيانات ناقصة، اسألي فقط عن البيانات الناقصة.
- اعتمدي على نتائج الأدوات بصيغة JSON: ok=true يعني نجاحاً مؤكداً، وok=false يعني عدم التأكيد.
"""

MEMORY_EXTRACTION_INSTRUCTION = """
You maintain durable, non-medical receptionist memory for a clinic patient.
Return one strict JSON object with exactly these string fields:
{"name": "", "preferences": ""}

Rewrite preferences as one clean final value. Merge useful durable facts from the
current preferences and recent conversation without duplicating text.

Keep only explicitly stated, durable receptionist information, including:
- the patient's explicitly stated name
- recurring services or treatment areas
- whether they are an existing laser client
- stable scheduling preferences
- communication preferences
- other useful non-medical receptionist preferences

Never store old booking dates or times, completed appointment details, greetings,
temporary questions, old prices, conversation summaries, inferred diagnoses, or
sensitive medical information. Do not infer facts the patient did not state.

If the current name is non-empty, return it unchanged. If no durable preference
exists, return an empty preferences string. Return JSON only.
""".strip()

def extract_patient_memory_sync(phone: str, profile: dict) -> dict:
    """Extract durable patient memory. This function never sends WhatsApp output."""
    if gemini_client is None:
        raise RuntimeError("Gemini client is unavailable for memory extraction")

    recent_conversation = load_recent_memory_conversation(phone, 20)
    payload = {
        "current_name": profile.get("name") or "",
        "current_preferences": profile.get("preferences") or "",
        "recent_conversation": recent_conversation,
    }
    result = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=json.dumps(payload, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=MEMORY_EXTRACTION_INSTRUCTION,
            temperature=0,
            response_mime_type="application/json",
            response_schema={
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "preferences": {"type": "STRING"},
                },
                "required": ["name", "preferences"],
            },
        ),
    )
    parsed = json.loads((result.text or "").strip())
    if not isinstance(parsed, dict):
        raise ValueError("Gemini memory response is not a JSON object")
    if set(parsed) != {"name", "preferences"}:
        raise ValueError("Gemini memory response has an invalid schema")
    if not isinstance(parsed["name"], str) or not isinstance(parsed["preferences"], str):
        raise ValueError("Gemini memory fields must be strings")

    # Never allow a model response to replace a name already stored in Postgres.
    extracted_name = profile.get("name") or parsed["name"]
    return set_extracted_patient_memory(
        phone,
        extracted_name,
        parsed["preferences"],
    )

async def update_patient_memory(phone: str, profile: dict) -> dict:
    """Best-effort memory update that must never interrupt webhook processing."""
    try:
        memory = await asyncio.to_thread(extract_patient_memory_sync, phone, profile)
        updated = dict(profile)
        if not updated.get("name") and memory.get("name"):
            updated["name"] = memory["name"]
        updated["preferences"] = memory.get("preferences", "")
        return updated
    except Exception:
        log.exception("Patient memory extraction failed for %s", phone)
        return profile

def generate_ai_reply_sync(
    phone: str,
    user_message: str,
    profile: dict,
    *,
    history_before_id: int | None = None,
    booking_draft: dict | None = None,
):
    if gemini_client is None:
        return "أهلاً بحضرتك يا فندم 🌸 حصل عطل مؤقت. برجاء المحاولة بعد قليل.", []

    queued_images = []

    def send_clinic_media(media_types: list[str]) -> dict:
        valid = [m for m in media_types if m in OFFER_IMAGES]
        for m in valid:
            if OFFER_IMAGES[m] not in queued_images:
                queued_images.append(OFFER_IMAGES[m])
        return {
            "ok": bool(valid),
            "code": "media_queued" if valid else "invalid_media",
            "media": valid,
        }

    def notify_staff_tool(issue_summary: str) -> dict:
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
            return {"ok": True, "code": "staff_notified"}
        except Exception:
            log.exception("notify_staff tool failed")
            return {
                "ok": False,
                "code": "staff_notification_failed",
                "retryable": True,
            }

    def check_my_appointments() -> dict:
        """Read appointments for the current WhatsApp patient; never ask for a phone."""
        return check_patient_appointments(phone)

    def cancel_my_appointment(date: str, time: str) -> dict:
        """Cancel the current patient's exact appointment using its date and time."""
        nonlocal booking_draft
        outcome = cancel_appointment(phone, date, time)
        if outcome.get("ok") is True:
            clear_booking_draft(phone, "cancelled")
            booking_draft = None
        return outcome

    def reschedule_my_appointment(
        old_date: str,
        old_time: str,
        new_date: str,
        new_time: str,
    ) -> dict:
        """Reschedule the current patient's exact booking without requesting a phone."""
        nonlocal booking_draft
        outcome = reschedule_appointment(phone, old_date, new_date, new_time, old_time)
        if outcome.get("ok") is True:
            clear_booking_draft(phone, "rescheduled")
            booking_draft = None
        return outcome

    def book_my_appointment(
        patient_name: str = "",
        date: str = "",
        time: str = "",
        area: str = "",
    ) -> dict:
        """Book the current WhatsApp patient; their phone is already known."""
        nonlocal booking_draft
        draft = dict(booking_draft or {})
        values = {
            "patient_name": patient_name.strip()
            or str(draft.get("patient_name") or profile.get("name") or "").strip(),
            "date": date.strip() or str(draft.get("requested_date") or "").strip(),
            "time": time.strip() or str(draft.get("requested_time") or "").strip(),
            "area": area.strip() or str(draft.get("service_area") or "").strip(),
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            return {
                "ok": False,
                "code": "missing_booking_fields",
                "message": "بيانات الحجز غير مكتملة.",
                "missing": missing,
            }
        outcome = book_appointment(
            values["patient_name"],
            phone,
            values["date"],
            values["time"],
            values["area"],
        )
        if outcome.get("ok") is True:
            clear_booking_draft(phone, "booked")
            booking_draft = None
        elif outcome.get("code") == "slot_unavailable":
            draft.update(
                {
                    "patient_name": values["patient_name"],
                    "requested_date": values["date"],
                    "requested_time": "",
                    "service_area": values["area"],
                    "intent": "book",
                    "stage": "need_time",
                }
            )
            booking_draft = save_booking_draft(phone, draft)
        return outcome

    def remember_patient_details(
        patient_name: str = "",
        preferences: str = "",
    ) -> dict:
        """Persist details already supplied so they are not requested again."""
        return update_patient_file(phone, patient_name, preferences)

    try:
        chat = gemini_client.chats.create(
            model=GEMINI_MODEL,
            # The inbound turn has already been persisted. Exclude the complete
            # current burst and then pass its combined text exactly once below.
            history=load_chat_history(phone, 12, before_id=history_before_id),
            config=types.GenerateContentConfig(
                system_instruction=build_system_instruction(
                    profile,
                    phone,
                    booking_draft,
                ),
                temperature=0.1,
                tools=[
                    check_schedule,
                    check_my_appointments,
                    cancel_my_appointment,
                    reschedule_my_appointment,
                    book_my_appointment,
                    remember_patient_details,
                    send_clinic_media,
                    notify_staff_tool,
                ],
            ),
        )
        result = chat.send_message(user_message)
        return (result.text or "").strip(), queued_images
    except Exception:
        log.exception("Gemini generation failed")
        return "حصل عطل مؤقت في خدمة الرد. حاولي تبعتي رسالتك مرة تانية بعد شوية، ومفيش أي حجز اتأكد من الرسالة دي.", []

async def generate_ai_reply(
    phone: str,
    message: str,
    profile: dict,
    *,
    history_before_id: int | None = None,
    booking_draft: dict | None = None,
):
    return await asyncio.to_thread(
        generate_ai_reply_sync,
        phone,
        message,
        profile,
        history_before_id=history_before_id,
        booking_draft=booking_draft,
    )

# ------------------------------------------------------------
# Conversation handling
# ------------------------------------------------------------

async def process_conversation_batch(
    sender_phone: str,
    messages: list[dict],
    phone_number_id: str,
) -> None:
    """Process one ordered, persisted burst as a single receptionist turn."""
    sender_phone = normalize_phone(sender_phone)
    user_text = combine_inbound_messages(messages)
    if not user_text:
        return
    history_ids = [
        int(message["chat_history_id"])
        for message in messages
        if message.get("chat_history_id") is not None
    ]
    history_before_id = min(history_ids) if history_ids else None

    try:
        profile = load_patient_profile(sender_phone)
        try:
            booking_draft = update_booking_draft_from_message(
                sender_phone,
                user_text,
                profile,
            )
        except Exception:
            # Draft state improves determinism but must not become a new single
            # point of failure for ordinary reception conversations.
            log.exception("Booking draft update failed for %s", sender_phone)
            booking_draft = None
        # Memory learning deliberately occurs before both global and per-patient
        # pause checks so human-takeover conversations can still teach the CRM.
        if should_extract_memory(user_text):
            profile = await update_patient_memory(sender_phone, profile)
        if not ENABLE_REAL_CLINIC or not is_bot_globally_active():
            log.info("Bot replies disabled; stored message from %s", sender_phone)
            return
    except Exception:
        log.exception("Postgres unavailable while starting conversation")
        await send_whatsapp_message(
            sender_phone,
            "حصل عطل مؤقت في النظام ومفيش أي حجز اتأكد. حاولي مرة تانية بعد شوية.",
            phone_number_id,
        )
        return

    if profile.get("is_paused"):
        log.info("Human takeover active for %s", sender_phone)
        return

    response_text, images = await generate_ai_reply(
        sender_phone,
        user_text,
        profile,
        history_before_id=history_before_id,
        booking_draft=booking_draft,
    )

    for clinic_image in images:
        if not await send_whatsapp_image(sender_phone, clinic_image, phone_number_id):
            audit("system", "whatsapp_image_failed", sender_phone, clinic_image.get("url", ""))
            save_chat_turn(sender_phone, "system", "فشل إرسال صورة العيادة عبر واتساب.")

    if response_text:
        delivery = await send_whatsapp_message(sender_phone, response_text, phone_number_id)
        if delivery.get("ok") is True:
            try:
                save_chat_turn(
                    sender_phone,
                    "model",
                    response_text,
                    delivery.get("message_id"),
                )
            except Exception:
                log.exception("Reply sent but chat persistence failed")
        else:
            audit("system", "whatsapp_send_failed", sender_phone, response_text[:500])
            save_chat_turn(
                sender_phone,
                "system",
                "لم يؤكد واتساب استلام رد الذكاء الاصطناعي.",
            )


async def process_pending_inbound(phone_number: str) -> None:
    """Debounce and process a phone's queue under a cross-worker DB lease."""
    phone = normalize_phone(phone_number)
    owner_token = secrets.token_urlsafe(24)
    try:
        acquired = await asyncio.to_thread(acquire_processing_lease, phone, owner_token)
    except Exception:
        log.exception("Could not acquire conversation lease for %s", phone)
        return
    if not acquired:
        return
    try:
        while True:
            delay = await asyncio.to_thread(pending_batch_delay, phone)
            if delay is None:
                return
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            if not await asyncio.to_thread(refresh_processing_lease, phone, owner_token):
                log.warning("Conversation lease expired before processing %s", phone)
                return
            messages = await asyncio.to_thread(claim_pending_batch, phone)
            if not messages:
                return
            if any(message.get("side_effects_started_at") for message in messages):
                # A prior process crossed the durable pre-effect boundary but
                # never recorded completion. Replaying could duplicate an Apps
                # Script mutation or WhatsApp reply, so quarantine it instead.
                await asyncio.to_thread(suppress_uncertain_pending_batch, messages)
                continue
            reserved = await asyncio.to_thread(
                reserve_pending_batch_side_effects,
                messages,
            )
            if not reserved:
                # A concurrent/partial reservation is itself uncertain. Reload
                # on the next scanner pass and fail closed rather than replay.
                log.warning("Could not reserve all side effects for %s", phone)
                return
            phone_id = str(messages[-1].get("phone_number_id") or PHONE_NUMBER_ID)
            await process_conversation_batch(phone, messages, phone_id)
            await asyncio.to_thread(mark_pending_batch_processed, messages)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Leave the batch unprocessed. The lifecycle scanner will retry after
        # this worker releases its lease; no inbound message is discarded.
        log.exception("Queued conversation processing failed for %s", phone)
    finally:
        try:
            await asyncio.to_thread(release_processing_lease, phone, owner_token)
        except Exception:
            log.exception("Could not release conversation lease for %s", phone)


async def pending_queue_worker() -> None:
    """Recover queue work after process restarts and across Render workers."""
    while True:
        try:
            phones = await asyncio.to_thread(list_pending_phones)
            if phones:
                await asyncio.gather(*(process_pending_inbound(phone) for phone in phones))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Pending inbound queue scan failed")
        await asyncio.sleep(1.0)


async def handle_ai_conversation(
    sender_phone: str,
    user_text: str,
    phone_number_id: str,
    message_id: str | None = None,
):
    """Compatibility path for direct callers; webhooks use the durable queue."""
    sender_phone = normalize_phone(sender_phone)
    lock = await get_user_lock(sender_phone)
    async with lock:
        saved_turn = save_chat_turn(sender_phone, "user", user_text, message_id)
        if not saved_turn:
            raise RuntimeError("Incoming chat turn was not persisted")
        await process_conversation_batch(
            sender_phone,
            [
                {
                    "message_id": message_id,
                    "chat_history_id": saved_turn.get("id"),
                    "content": user_text,
                    "phone_number_id": phone_number_id,
                }
            ],
            phone_number_id,
        )


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
                ) or PHONE_NUMBER_ID

                # Patient messages.
                if field != "smb_message_echoes":
                    for message in value.get("messages", []) or []:
                        message_id = message.get("id")
                        sender = normalize_phone(message.get("from", ""))
                        if not sender or is_blocked(sender):
                            continue

                        text = extract_text_message(message)
                        if not text:
                            continue
                        saved = persist_incoming_message(
                            sender,
                            text,
                            message_id,
                            target_phone_id,
                        )
                        if not saved:
                            continue
                        # Queue processing still runs when replies are disabled:
                        # memory and booking-draft learning are internal and must
                        # continue during operational/human takeover pauses.
                        background_tasks.add_task(process_pending_inbound, sender)
                        scheduled += 1

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

class ReadCursorReq(BaseModel):
    displayed_message_id: int = Field(ge=0)

class CancelReq(BaseModel):
    phone_number: str = Field(min_length=5, max_length=30)
    date: str
    time: str | None = Field(default=None, max_length=30)
    appointment_id: str | None = Field(default=None, max_length=200)

class RescheduleReq(BaseModel):
    phone_number: str = Field(min_length=5, max_length=30)
    old_date: str
    old_time: str | None = Field(default=None, max_length=30)
    appointment_id: str | None = Field(default=None, max_length=200)
    new_date: str
    new_time: str

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

class NewConversationReq(BaseModel):
    phone_number: str = Field(min_length=5, max_length=30)
    name: str = Field(default="", max_length=200)

class ConversationStateReq(BaseModel):
    is_pinned: bool | None = None
    is_archived: bool | None = None

# ------------------------------------------------------------
# Admin API
# ------------------------------------------------------------

@app.post("/admin/api/toggle_pause")
def api_toggle_pause(req: PauseRequest, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    try:
        set_patient_pause(phone, req.is_paused)
        audit(admin, "patient_pause_on" if req.is_paused else "patient_pause_off", phone)
        return success("patient_state_updated", {"phone_number": phone, "is_paused": req.is_paused}, status="success")
    except Exception:
        return failure("database_unavailable", "Could not update patient state.", retryable=True)

@app.post("/admin/api/rename_patient")
def api_rename_patient(req: RenamePatientReq, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    name = req.name.strip()
    if not name:
        return failure("invalid_name", "Patient name must not be blank.", state="degraded")
    try:
        update_patient_file(phone, name=name)
        audit(admin, "rename_patient", phone, name)
        return success("patient_renamed", {"phone_number": phone, "name": name}, status="success")
    except Exception:
        return failure("database_unavailable", "Could not update patient name.", retryable=True)

@app.post("/admin/api/patient_tags")
def api_patient_tags(req: PatientTagsReq, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    try:
        set_patient_tags(phone, req.tags)
        audit(admin, "update_patient_tags", phone, json.dumps(req.tags, ensure_ascii=False))
        return success("patient_tags_updated", {"phone_number": phone, "tags": req.tags}, status="success")
    except Exception:
        return failure("database_unavailable", "Could not update tags.", retryable=True)

@app.post("/admin/api/patient_preferences")
def api_patient_preferences(req: PatientPreferencesReq, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    try:
        set_patient_preferences(phone, req.preferences)
        audit(admin, "update_patient_preferences", phone, req.preferences[:1000])
        return success("patient_notes_updated", {"phone_number": phone, "preferences": req.preferences}, status="success")
    except Exception:
        return failure("database_unavailable", "Could not update notes.", retryable=True)

@app.get("/admin/api/settings")
def get_settings(admin: str = Depends(verify_admin)):
    return success("settings_loaded", {"instruction": get_live_instructions()})

@app.post("/admin/api/settings")
def update_settings(data: SettingsUpdate, admin: str = Depends(verify_admin)):
    if not save_live_instructions(data.instruction):
        return failure("settings_update_failed", "Failed to save settings.", retryable=True)
    return success("settings_updated", status="success")

@app.get("/admin/api/bot_status")
def api_get_bot_status(admin: str = Depends(verify_admin)):
    return success("bot_status_loaded", {"is_active": is_bot_globally_active()})

@app.post("/admin/api/toggle_global_bot")
def api_toggle_global_bot(req: GlobalBotReq, admin: str = Depends(verify_admin)):
    try:
        set_bot_globally_active(req.is_active)
        return success("global_bot_updated", {"is_active": req.is_active}, status="success")
    except Exception:
        return failure("database_unavailable", "Could not update global bot state.", retryable=True)

@app.get("/admin/api/inbox")
def api_inbox(
    search: str = Query("", max_length=100),
    state: str = Query("all", max_length=30),
    limit: int = Query(50, ge=1, le=100),
    before_id: int | None = Query(None, ge=1),
    after_id: int | None = Query(None, ge=0),
    admin: str = Depends(verify_admin),
):
    return admin_operations.inbox(
        search=search, state=state, limit=limit,
        before_id=before_id, after_id=after_id,
    )

@app.get("/admin/api/inbox/updates")
def api_inbox_updates(
    after_version: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    admin: str = Depends(verify_admin),
):
    return admin_operations.inbox_updates(
        after_version=after_version,
        limit=limit,
    )

@app.post("/admin/api/conversations")
def api_create_conversation(
    req: NewConversationReq,
    admin: str = Depends(verify_admin),
):
    phone = normalize_phone(req.phone_number)
    name = req.name.strip()
    if len(phone) < 5:
        return failure("invalid_phone", "A valid WhatsApp phone number is required.", state="degraded")
    try:
        row = db_execute(
            """
            /* admin_create_conversation */
            WITH patient AS (
                INSERT INTO patients(phone_number,name,updated_at)
                VALUES (%s,%s,NOW())
                ON CONFLICT(phone_number) DO UPDATE SET
                    name=CASE WHEN EXCLUDED.name<>'' THEN EXCLUDED.name ELSE patients.name END,
                    updated_at=NOW()
                RETURNING phone_number,name
            ), summary AS (
                INSERT INTO conversation_summaries(phone_number,updated_at)
                SELECT phone_number,NOW() FROM patient
                WHERE TRUE
                ON CONFLICT(phone_number) DO UPDATE SET
                    is_archived=FALSE,
                    change_version=nextval('conversation_summary_change_seq'),
                    updated_at=NOW()
                RETURNING phone_number,change_version
            )
            SELECT patient.phone_number,patient.name,summary.change_version
            FROM patient JOIN summary USING(phone_number)
            """,
            (phone, name),
            fetchone=True,
        )
        audit(admin, "create_conversation", phone, name)
        return success("conversation_created", dict(row) if row else {"phone_number": phone, "name": name})
    except Exception:
        return failure("database_unavailable", "Could not create the conversation.", retryable=True)

@app.post("/admin/api/patient/{phone_number}/conversation-state")
def api_conversation_state(
    phone_number: str,
    req: ConversationStateReq,
    admin: str = Depends(verify_admin),
):
    phone = normalize_phone(phone_number)
    if req.is_pinned is None and req.is_archived is None:
        return failure("invalid_state", "No conversation state change was supplied.", state="degraded")
    try:
        row = db_execute(
            """
            /* admin_conversation_state */
            INSERT INTO conversation_summaries(phone_number,is_pinned,is_archived,updated_at)
            VALUES (%s,COALESCE(%s,FALSE),COALESCE(%s,FALSE),NOW())
            ON CONFLICT(phone_number) DO UPDATE SET
                is_pinned=COALESCE(%s,conversation_summaries.is_pinned),
                is_archived=COALESCE(%s,conversation_summaries.is_archived),
                change_version=nextval('conversation_summary_change_seq'),
                updated_at=NOW()
            RETURNING phone_number,is_pinned,is_archived,change_version
            """,
            (
                phone,req.is_pinned,req.is_archived,
                req.is_pinned,req.is_archived,
            ),
            fetchone=True,
        )
        action = "archive_conversation" if req.is_archived else "reopen_conversation" if req.is_archived is False else "pin_conversation" if req.is_pinned else "unpin_conversation"
        audit(admin, action, phone)
        return success("conversation_state_updated", dict(row))
    except Exception:
        return failure("database_unavailable", "Could not update conversation state.", retryable=True)

@app.post("/admin/api/patient/{phone_number}/read")
def api_mark_patient_read(
    phone_number: str,
    req: ReadCursorReq,
    admin: str = Depends(verify_admin),
):
    return admin_operations.mark_read(phone_number, req.displayed_message_id)

@app.post("/admin/api/patient/{phone_number}/unread")
def api_mark_patient_unread(
    phone_number: str,
    admin: str = Depends(verify_admin),
):
    return admin_operations.mark_unread(phone_number)

@app.get("/admin/api/inbox/metadata")
def api_inbox_metadata(
    phones: str = Query("", max_length=4000),
    admin: str = Depends(verify_admin),
):
    return admin_operations.inbox_metadata(phones.split(","))

@app.get("/admin/api/patient/{phone_number}")
def api_patient_detail(phone_number: str, admin: str = Depends(verify_admin)):
    return admin_operations.patient_detail(phone_number)

@app.get("/admin/api/patient/{phone_number}/messages")
def api_patient_messages(
    phone_number: str,
    before_id: int | None = Query(None, ge=1),
    after_id: int | None = Query(None, ge=0),
    limit: int = Query(60, ge=1, le=200),
    admin: str = Depends(verify_admin),
):
    return admin_operations.messages(
        phone_number, limit=limit, before_id=before_id, after_id=after_id
    )

@app.get("/admin/api/patient/{phone_number}/appointments")
def api_patient_appointments(phone_number: str, admin: str = Depends(verify_admin)):
    return admin_operations.patient_appointments(phone_number)

@app.get("/admin/api/schedule")
def api_get_schedule(date: str, admin: str = Depends(verify_admin)):
    return admin_operations.schedule(date)

@app.post("/admin/api/book")
def api_admin_book(req: BookReq, admin: str = Depends(verify_admin)):
    result = book_appointment(
        req.patient_name, req.phone_number, req.date, req.time, req.area
    )
    if result.get("ok") is not True:
        return result
    refresh = admin_operations.patient_appointments(req.phone_number)
    return success(
        "appointment_booked",
        {"appointment": result.get("appointment"), "appointments_refresh": refresh},
        message=result.get("message"),
        status=result.get("message", ""),
    )

@app.post("/admin/api/cancel")
def api_admin_cancel(req: CancelReq, admin: str = Depends(verify_admin)):
    result = cancel_appointment(
        req.phone_number,
        req.date,
        req.time,
        req.appointment_id,
    )
    if result.get("ok") is not True:
        return result
    refresh = admin_operations.patient_appointments(req.phone_number)
    return success(
        "appointment_cancelled",
        {
            "date": result.get("date"),
            "time": result.get("time"),
            "appointment_id": result.get("appointment_id"),
            "appointments_refresh": refresh,
        },
        message=result.get("message"),
        status=result.get("message", ""),
    )

@app.post("/admin/api/reschedule")
def api_admin_reschedule(req: RescheduleReq, admin: str = Depends(verify_admin)):
    result = reschedule_appointment(
        req.phone_number,
        req.old_date,
        req.new_date,
        req.new_time,
        req.old_time,
        req.appointment_id,
    )
    if result.get("ok") is not True:
        return result
    refresh = admin_operations.patient_appointments(req.phone_number)
    return success(
        "appointment_rescheduled",
        {
            "old_date": result.get("old_date"),
            "old_time": result.get("old_time"),
            "appointment_id": result.get("appointment_id"),
            "date": result.get("date"),
            "time": result.get("time"),
            "appointments_refresh": refresh,
        },
        message=result.get("message"),
        status=result.get("message", ""),
    )

@app.post("/admin/api/send_message")
async def api_send_message(req: StaffMessageReq, admin: str = Depends(verify_admin)):
    phone = normalize_phone(req.phone_number)
    delivery = await send_whatsapp_message(phone, req.message)
    if delivery.get("ok") is not True:
        try:
            save_chat_turn(phone, "system", "لم يؤكد واتساب استلام رسالة الموظف.")
        except Exception:
            pass
        return failure(
            "whatsapp_delivery_unconfirmed",
            "WhatsApp did not confirm message acceptance.",
            retryable=True,
        )
    try:
        row = save_chat_turn(
            phone, "staff", req.message, delivery.get("message_id")
        )
        if req.pause_after_send:
            set_patient_pause(phone, True)
        audit(admin, "staff_manual_message", phone, req.message[:1000])
        return success(
            "staff_message_sent",
            {
                "message": dict(row) if row else {
                    "role": "staff",
                    "content": req.message,
                    "whatsapp_message_id": delivery.get("message_id"),
                },
                "paused": req.pause_after_send,
            },
            status="success",
        )
    except Exception:
        return failure(
            "message_persistence_failed",
            "WhatsApp accepted the message but it could not be saved locally.",
            state="degraded",
        )

@app.get("/admin/api/dashboard-summary")
def api_dashboard_summary(admin: str = Depends(verify_admin)):
    return admin_operations.dashboard_summary()

@app.get("/admin/api/analytics")
def api_analytics(
    days: int = Query(14, ge=7, le=90),
    admin: str = Depends(verify_admin),
):
    return admin_operations.analytics(days)

@app.get("/admin/api/system-health")
def api_system_health(admin: str = Depends(verify_admin)):
    return admin_operations.system_health(
        global_bot_active=is_bot_globally_active(),
        gemini_configured=bool(GEMINI_API_KEY),
        gemini_initialized=gemini_client is not None,
        whatsapp_configured=bool(WHATSAPP_ACCESS_TOKEN and PHONE_NUMBER_ID),
    )

# Compatibility endpoints retained while clients migrate to the structured APIs.
@app.get("/admin/api/data")
def get_admin_data(
    search: str = Query("", max_length=100),
    paused: bool | None = None,
    limit: int = Query(100, ge=1, le=500),
    include_chats: bool = Query(True),
    admin: str = Depends(verify_admin),
):
    state = "human" if paused is True else "ai" if paused is False else "all"
    inbox_result = admin_operations.inbox(search=search, state=state, limit=min(limit,100))
    if inbox_result.get("ok") is not True:
        raise HTTPException(status_code=503, detail=inbox_result.get("message"))
    patients = inbox_result["data"]["patients"]
    chats = []
    if include_chats:
        try:
            chats = db_execute(
                """
                SELECT id,phone_number,role,content,created_at
                FROM chat_history ORDER BY id DESC LIMIT 1000
                """,
                fetchall=True,
            )
            chats.reverse()
        except Exception:
            chats = []
    return {"patients": patients, "chats": chats}

@app.get("/admin/api/stats")
def admin_stats(admin: str = Depends(verify_admin)):
    result = admin_operations.dashboard_summary()
    if result.get("ok") is not True:
        raise HTTPException(status_code=503, detail=result.get("message"))
    data = result["data"]
    return {
        "patients": data["total_patients"],
        "messages": data["incoming_messages_today"] + data["ai_messages_today"] + data["staff_messages_today"],
        "incoming_24h": data["incoming_messages_today"],
        "staff_24h": data["staff_messages_today"],
        "human_takeovers": data["human_takeover_count"],
        "bot_active": is_bot_globally_active(),
    }

@app.get("/admin/api/audit")
def admin_audit(
    limit: int = Query(100, ge=1, le=500),
    admin: str = Depends(verify_admin),
):
    try:
        rows = db_execute(
            """
            SELECT id,actor,action,phone_number,details,created_at
            FROM audit_log ORDER BY id DESC LIMIT %s
            """,
            (limit,),
            fetchall=True,
        )
        return success("audit_loaded", {"audit": rows})
    except Exception:
        return failure("database_unavailable", "Could not load audit events.", retryable=True)

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(admin: str = Depends(verify_admin)):
    html = Path("static/admin.html").read_text(encoding="utf-8")
    return HTMLResponse(html)

# ------------------------------------------------------------
# Background lock cleanup
# ------------------------------------------------------------

# The cleanup loop is intentionally owned by the FastAPI lifespan above.
