"""Human-first operating mode and passive appointment synchronization.

This module has no WhatsApp dependency by design.  It may inspect persisted
conversation turns and call the authoritative BookingService, but it cannot send
patient-facing messages.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from typing import Any, Callable

from psycopg2.extras import RealDictCursor


log = logging.getLogger(__name__)
OPERATING_MODES = {"HUMAN", "AI_BACKUP", "AI_ACTIVE"}

SCHEMA = (
    "ALTER TABLE patients ADD COLUMN IF NOT EXISTS pause_source VARCHAR(30) NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS passive_event_key TEXT",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_passive_event_key
       ON chat_history(passive_event_key) WHERE passive_event_key IS NOT NULL""",
    """CREATE TABLE IF NOT EXISTS passive_sync_queue (
        message_id BIGINT PRIMARY KEY REFERENCES chat_history(id) ON DELETE CASCADE,
        phone_number VARCHAR(30) NOT NULL,
        status VARCHAR(30) NOT NULL DEFAULT 'pending',
        lease_until TIMESTAMPTZ,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ
    )""",
    """CREATE INDEX IF NOT EXISTS idx_passive_sync_pending
       ON passive_sync_queue(status, lease_until, message_id)""",
    """CREATE TABLE IF NOT EXISTS passive_appointment_events (
        event_key TEXT PRIMARY KEY,
        trigger_message_id BIGINT NOT NULL REFERENCES chat_history(id),
        phone_number VARCHAR(30) NOT NULL,
        action VARCHAR(20) NOT NULL,
        confidence VARCHAR(20) NOT NULL,
        evidence_message_ids BIGINT[] NOT NULL DEFAULT '{}',
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        status VARCHAR(40) NOT NULL DEFAULT 'inferred',
        result_code TEXT NOT NULL DEFAULT '',
        side_effect_started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_passive_events_status
       ON passive_appointment_events(status, updated_at)""",
    """CREATE TABLE IF NOT EXISTS supervisor_attention (
        id BIGSERIAL PRIMARY KEY,
        event_key TEXT NOT NULL UNIQUE,
        phone_number VARCHAR(30) NOT NULL,
        kind VARCHAR(60) NOT NULL,
        title TEXT NOT NULL,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        status VARCHAR(20) NOT NULL DEFAULT 'open',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved_at TIMESTAMPTZ,
        resolved_by TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_supervisor_attention_open
       ON supervisor_attention(status, created_at DESC)""",
    """INSERT INTO clinic_settings(key,content)
       VALUES ('operating_mode','HUMAN') ON CONFLICT(key) DO NOTHING""",
)

INFERENCE_INSTRUCTION = """
You are an internal clinic operations observer. You NEVER reply to a patient.
Review the recent persisted patient (user) and human receptionist (staff) turns.
Detect only a scheduling action that the HUMAN RECEPTIONIST clearly states was
already completed or definitively confirmed. A patient's request, preference,
question, agreement, or proposed time is never enough. Phrases such as "let me
check", "might work", or an unanswered request must return action=none.

Return strict JSON with exactly these fields:
{
  "action":"book|reschedule|cancel|none",
  "confidence":"high|uncertain",
  "patient_name":"",
  "date":"YYYY-MM-DD or empty",
  "time":"h:mm AM/PM or empty",
  "area":"",
  "old_date":"YYYY-MM-DD or empty",
  "old_time":"h:mm AM/PM or empty",
  "appointment_id":"",
  "evidence_message_ids":[1]
}

Use Africa/Cairo and the supplied local date. Evidence IDs must identify the
actual persisted turns proving completion and must include the confirming staff
turn. Do not infer missing facts. If completion, exact target, date, time, area,
or intent is uncertain, use confidence=uncertain or action=none. False negatives
are strongly preferred to false appointment mutations. Return JSON only.
""".strip()

_CONFIRMATION_PATTERNS = {
    "book": re.compile(
        r"(?:حجزت(?:لك|لحضرتك)?|حجزنا|تم\s+(?:تأكيد\s+)?الحجز|"
        r"الحجز\s+(?:اتأكد|تأكد|تم)|ثبتنا|أكدنا|"
        r"\byou(?:'re| are) booked\b|\bbooking (?:is )?confirmed\b|"
        r"\bconfirmed for\b|\bi(?:'ve| have) booked\b|"
        r"\bappointment (?:is )?confirmed\b)", re.I
    ),
    "cancel": re.compile(
        r"(?:تم\s+(?:إلغاء|الغاء)|(?:ألغيت|الغيت|لغيت)(?:لك)?|اتلغى|"
        r"\b(?:cancelled|canceled)\b|\bi(?:'ve| have) cancel(?:led|ed)\b)", re.I
    ),
    "reschedule": re.compile(
        r"(?:تم\s+(?:تغيير|تعديل)\s+(?:الحجز|الموعد)|غيرت(?:لك)?|"
        r"عدلنا\s+(?:الحجز|الموعد)|اتغير\s+(?:الحجز|الموعد)|"
        r"\brescheduled\b|\bmoved your appointment\b|"
        r"\bchanged your appointment\b)", re.I
    ),
}


def normalize_mode(value: Any) -> str:
    mode = str(value or "").strip().upper()
    return mode if mode in OPERATING_MODES else "HUMAN"


def patient_facing_ai_allowed(mode: Any, master_enabled: bool) -> bool:
    return normalize_mode(mode) == "AI_ACTIVE" and bool(master_enabled)


def eligible_confirmation_actions(content: Any) -> set[str]:
    """Return actions whose explicit completion language matches this staff turn.

    This is only a cost/latency gate. ``validate_inference`` repeats the
    action-specific check after Gemini and remains the mutation safety boundary.
    """
    text = str(content or "")
    return {
        action
        for action, pattern in _CONFIRMATION_PATTERNS.items()
        if pattern.search(text)
    }


def validate_inference(raw: Any, rows: list[dict], trigger_id: int) -> tuple[dict | None, str]:
    """Validate model output and require explicit staff-completion evidence."""
    if not isinstance(raw, dict):
        return None, "malformed_inference"
    action = str(raw.get("action") or "none").lower()
    confidence = str(raw.get("confidence") or "uncertain").lower()
    if action not in {"book", "reschedule", "cancel", "none"}:
        return None, "malformed_inference"
    ids = raw.get("evidence_message_ids")
    if not isinstance(ids, list) or any(not isinstance(value, int) for value in ids):
        return None, "malformed_inference"
    by_id = {int(row["id"]): row for row in rows}
    evidence = [by_id[value] for value in ids if value in by_id]
    if len(evidence) != len(set(ids)):
        return None, "invalid_evidence"
    cleaned = {
        "action": action,
        "confidence": confidence,
        "patient_name": str(raw.get("patient_name") or "").strip()[:200],
        "date": str(raw.get("date") or "").strip(),
        "time": str(raw.get("time") or "").strip(),
        "area": str(raw.get("area") or "").strip()[:200],
        "old_date": str(raw.get("old_date") or "").strip(),
        "old_time": str(raw.get("old_time") or "").strip(),
        "appointment_id": str(raw.get("appointment_id") or "").strip()[:200],
        "evidence_message_ids": sorted(set(ids)),
    }
    if action == "none":
        return cleaned, "none"
    trigger_row = by_id.get(trigger_id)
    if (
        confidence != "high"
        or trigger_id not in cleaned["evidence_message_ids"]
        or not trigger_row
        or trigger_row.get("role") != "staff"
        or action not in eligible_confirmation_actions(trigger_row.get("content"))
    ):
        return cleaned, "uncertain"
    required = ["date", "time", "area"] if action == "book" else ["date"] if action == "cancel" else ["date", "time", "old_date"]
    if any(not cleaned[field] for field in required):
        return cleaned, "ambiguous"
    exact_target = cleaned["old_time"] or cleaned["appointment_id"]
    if action == "cancel":
        exact_target = exact_target or cleaned["time"]
    if action in {"cancel", "reschedule"} and not exact_target:
        return cleaned, "ambiguous"
    for field in ("date", "old_date"):
        if cleaned[field]:
            try:
                dt.date.fromisoformat(cleaned[field])
            except ValueError:
                return cleaned, "ambiguous"
    return cleaned, "ready"


def event_key(trigger_id: int, inference: dict) -> str:
    evidence = ",".join(str(value) for value in inference.get("evidence_message_ids", []))
    material = f"{trigger_id}:{inference.get('action')}:{evidence}".encode()
    return hashlib.sha256(material).hexdigest()


class PassiveAppointmentSupervisor:
    """Durable, at-most-once coordinator for passive scheduling mutations."""

    def __init__(
        self,
        connection: Callable,
        db: Callable,
        booking: Any,
        infer: Callable[[str, list[dict]], dict],
        refresh_snapshot: Callable[[str], dict],
        update_summary: Callable[[Any, str, dict], None],
        get_mode: Callable[[], str],
        *,
        lease_seconds: int = 300,
    ):
        self.connection = connection
        self.db = db
        self.booking = booking
        self.infer = infer
        self.refresh_snapshot = refresh_snapshot
        self.update_summary = update_summary
        self.get_mode = get_mode
        self.lease_seconds = max(30, min(int(lease_seconds), 900))

    def claim_trigger(self) -> dict | None:
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    WITH candidate AS (
                        SELECT message_id FROM passive_sync_queue
                        WHERE status='pending' OR
                              (status='processing' AND lease_until<NOW())
                        ORDER BY message_id
                        FOR UPDATE SKIP LOCKED LIMIT 1
                    )
                    UPDATE passive_sync_queue queue
                    SET status='processing',
                        lease_until=NOW() + (%s * INTERVAL '1 second'),
                        attempts=attempts+1,last_error=''
                    FROM candidate WHERE queue.message_id=candidate.message_id
                    RETURNING queue.message_id,queue.phone_number,queue.attempts
                """, (self.lease_seconds,))
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else None

    def _complete_trigger(self, message_id: int, error: str = "") -> None:
        self.db("""UPDATE passive_sync_queue SET status='completed',completed_at=NOW(),
                   lease_until=NULL,last_error=%s WHERE message_id=%s""",
                (error[:500], message_id))

    def _context(self, phone: str, trigger_id: int) -> list[dict]:
        rows = self.db("""SELECT id,role,content,created_at FROM chat_history
            WHERE phone_number=%s AND id<=%s AND role IN ('user','staff')
            ORDER BY id DESC LIMIT 20""", (phone, trigger_id), fetchall=True)
        return [dict(row) for row in reversed(rows or [])]

    def _create_attention(
        self,
        key: str,
        phone: str,
        kind: str,
        title: str,
        details: dict,
        *,
        audit_action: str | None = None,
    ) -> bool:
        """Create one visible attention item and one inbox cursor change.

        The unique event key makes retries harmless.  The summary version is
        allocated in the same transaction, preserving the commit-ordered clock.
        """
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""INSERT INTO supervisor_attention(
                    event_key,phone_number,kind,title,details)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(event_key) DO NOTHING RETURNING phone_number""",
                    (key, phone, kind, title, json.dumps(details, ensure_ascii=False)))
                inserted = cur.fetchone()
                if inserted:
                    cur.execute("""INSERT INTO conversation_summaries(phone_number,updated_at)
                        VALUES (%s,NOW()) ON CONFLICT(phone_number) DO UPDATE SET
                        change_version=EXCLUDED.change_version,updated_at=NOW()""", (phone,))
                    if audit_action:
                        cur.execute("""INSERT INTO audit_log(actor,action,phone_number,details)
                            VALUES ('passive_ai',%s,%s,%s)""",
                            (audit_action, phone, json.dumps(details, ensure_ascii=False)))
            conn.commit()
        return bool(inserted)

    def _reserve_event(self, key: str, trigger: dict, inference: dict) -> tuple[bool, dict | None]:
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""INSERT INTO passive_appointment_events(
                    event_key,trigger_message_id,phone_number,action,confidence,
                    evidence_message_ids,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(event_key) DO NOTHING RETURNING event_key,status
                """, (key, trigger["message_id"], trigger["phone_number"],
                       inference["action"], inference["confidence"],
                       inference["evidence_message_ids"],
                       json.dumps(inference, ensure_ascii=False)))
                inserted = cur.fetchone()
                if inserted:
                    conn.commit()
                    return True, dict(inserted)
                cur.execute("""SELECT event_key,status,side_effect_started_at,result_code
                    FROM passive_appointment_events WHERE event_key=%s""", (key,))
                existing = cur.fetchone()
            conn.commit()
        return False, dict(existing) if existing else None

    def _begin_side_effect(self, key: str) -> bool:
        row = self.db("""UPDATE passive_appointment_events
            SET status='processing',side_effect_started_at=NOW(),updated_at=NOW()
            WHERE event_key=%s AND status='inferred' AND side_effect_started_at IS NULL
            RETURNING event_key""", (key,), fetchone=True)
        return bool(row)

    def _mark_attention_event(self, key: str, phone: str, status: str, code: str,
                              kind: str, title: str, inference: dict) -> None:
        details = {
            "action": inference.get("action"),
            "evidence_message_ids": inference.get("evidence_message_ids", []),
            "result_code": code,
        }
        audit_action = ("passive_booking_ambiguous"
                        if status in {"ambiguous", "uncertain"}
                        else "passive_booking_failed")
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""UPDATE passive_appointment_events SET status=%s,result_code=%s,
                    completed_at=NOW(),updated_at=NOW()
                    WHERE event_key=%s AND status NOT IN
                      ('succeeded','failed','ambiguous','uncertain')
                    RETURNING event_key""", (status, code, key))
                if not cur.fetchone():
                    conn.commit()
                    return
                cur.execute("""INSERT INTO supervisor_attention(
                    event_key,phone_number,kind,title,details)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(event_key) DO NOTHING RETURNING phone_number""",
                    (key, phone, kind, title, json.dumps(details, ensure_ascii=False)))
                if cur.fetchone():
                    cur.execute("""INSERT INTO conversation_summaries(phone_number,updated_at)
                        VALUES (%s,NOW()) ON CONFLICT(phone_number) DO UPDATE SET
                        change_version=EXCLUDED.change_version,updated_at=NOW()""", (phone,))
                cur.execute("""INSERT INTO audit_log(actor,action,phone_number,details)
                    VALUES ('passive_ai',%s,%s,%s)""",
                    (audit_action, phone, json.dumps(details, ensure_ascii=False)))
            conn.commit()

    def _recover_existing(self, key: str, trigger: dict, existing: dict | None, inference: dict) -> None:
        if not existing or existing.get("status") in {"succeeded", "failed", "ambiguous", "uncertain"}:
            return
        if existing.get("side_effect_started_at"):
            self._mark_attention_event(
                key, trigger["phone_number"], "uncertain", "uncertain_external_result",
                "passive_sync_uncertain_result",
                "Appointment action may have completed before an interruption — review authoritative schedule.",
                inference,
            )

    def _execute(self, phone: str, inference: dict) -> dict:
        action = inference["action"]
        if action == "book":
            profile = self.db("SELECT name FROM patients WHERE phone_number=%s", (phone,), fetchone=True) or {}
            name = inference["patient_name"] or profile.get("name") or ""
            if not name:
                return {"ok": False, "code": "missing_name"}
            return self.booking.book(name, phone, inference["date"], inference["time"], inference["area"])
        if action == "cancel":
            return self.booking.cancel(phone, inference["date"], inference["old_time"] or inference["time"], inference["appointment_id"] or None)
        return self.booking.reschedule(
            phone, inference["old_date"], inference["date"], inference["time"],
            inference["old_time"] or None, inference["appointment_id"] or None,
        )

    def _system_text(self, inference: dict) -> str:
        action = inference["action"]
        if action == "book":
            return f"Appointment automatically recorded from receptionist conversation: {inference['date']}, {inference['time']} — {inference['area']}."
        if action == "cancel":
            return f"Appointment cancellation automatically recorded from receptionist conversation: {inference['date']}, {inference['old_time'] or inference['time']}."
        return f"Appointment reschedule automatically recorded from receptionist conversation: {inference['old_date']} {inference['old_time']} → {inference['date']} {inference['time']}."

    def _finalize_success(self, key: str, phone: str, inference: dict, result_code: str) -> None:
        action_audit = {
            "book": "passive_booking_created",
            "reschedule": "passive_booking_rescheduled",
            "cancel": "passive_booking_cancelled",
        }[inference["action"]]
        details = {
            "action": inference["action"], "date": inference["date"],
            "time": inference["time"], "old_date": inference["old_date"],
            "old_time": inference["old_time"],
            "evidence_message_ids": inference["evidence_message_ids"],
            "result_code": result_code,
        }
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""INSERT INTO chat_history(
                    phone_number,role,content,passive_event_key)
                    VALUES (%s,'system',%s,%s)
                    ON CONFLICT(passive_event_key) DO NOTHING
                    RETURNING id,role,content,whatsapp_message_id,created_at
                """, (phone, self._system_text(inference), key))
                row = cur.fetchone()
                if row:
                    message = dict(row); message["phone_number"] = phone
                    self.update_summary(cur, phone, message)
                cur.execute("""UPDATE passive_appointment_events SET status='succeeded',
                    result_code=%s,completed_at=NOW(),updated_at=NOW()
                    WHERE event_key=%s""", (result_code, key))
                cur.execute("""INSERT INTO audit_log(actor,action,phone_number,details)
                    VALUES ('passive_ai',%s,%s,%s)""",
                    (action_audit, phone, json.dumps(details, ensure_ascii=False)))
            conn.commit()

    def process_trigger(self, trigger: dict) -> None:
        message_id, phone = int(trigger["message_id"]), trigger["phone_number"]
        mode = normalize_mode(self.get_mode())
        if mode == "AI_ACTIVE":
            self._complete_trigger(message_id, "patient_facing_ai_active")
            return
        rows = self._context(phone, message_id)
        trigger_row = next(
            (row for row in rows if int(row.get("id") or 0) == message_id),
            None,
        )
        if (
            not trigger_row
            or trigger_row.get("role") != "staff"
            or not eligible_confirmation_actions(trigger_row.get("content"))
        ):
            self._complete_trigger(message_id, "irrelevant_staff_message")
            return
        try:
            raw = self.infer(phone, rows)
        except Exception:
            log.exception("Passive appointment inference failed for %s", phone)
            key = f"inference-failed:{message_id}"
            self._create_attention(key, phone, "passive_sync_failed",
                "Appointment synchronization analysis failed — review conversation.",
                {"trigger_message_id": message_id}, audit_action="passive_booking_failed")
            self._complete_trigger(message_id, "inference_failed")
            return
        inference, state = validate_inference(raw, rows, message_id)
        if inference is None:
            key = f"malformed-inference:{message_id}"
            self._create_attention(key, phone, "passive_sync_failed",
                "Appointment synchronization returned invalid data — review conversation.",
                {"trigger_message_id": message_id}, audit_action="passive_booking_failed")
            self._complete_trigger(message_id, state)
            return
        if state == "none":
            self._complete_trigger(message_id)
            return
        key = event_key(message_id, inference)
        inserted, existing = self._reserve_event(key, trigger, inference)
        if not inserted:
            self._recover_existing(key, trigger, existing, inference)
            self._complete_trigger(message_id, "duplicate_event")
            return
        if state in {"uncertain", "ambiguous"}:
            self._mark_attention_event(
                key, phone, state, state,
                "passive_booking_ambiguous",
                "Possible appointment change — review conversation.", inference,
            )
            self._complete_trigger(message_id, state)
            return
        if not self._begin_side_effect(key):
            self._complete_trigger(message_id, "event_not_claimed")
            return
        try:
            result = self._execute(phone, inference)
        except Exception as exc:
            # A transport exception can occur after Apps Script accepted the
            # mutation. Never retry an outcome that is externally uncertain.
            log.exception("Authoritative passive appointment action has an uncertain result")
            self._mark_attention_event(
                key, phone, "uncertain", "uncertain_external_result",
                "passive_sync_uncertain_result",
                "Appointment action result is uncertain — review authoritative schedule.",
                inference,
            )
            self._complete_trigger(message_id, type(exc).__name__)
            return
        if not isinstance(result, dict) or result.get("ok") is not True:
            code = str(result.get("code") if isinstance(result, dict) else "malformed_booking_result")
            kind = "scheduling_unavailable" if code in {"booking_service_unavailable", "malformed_booking_result"} else "appointment_sync_conflict"
            title = "Scheduling system unavailable — appointment was not changed." if kind == "scheduling_unavailable" else "Appointment synchronization needs review — no alternative was chosen."
            self._mark_attention_event(key, phone, "failed", code, kind, title, inference)
            self._complete_trigger(message_id, code)
            return
        # External success occurred. From here on, any local failure must never
        # replay the mutation: side_effect_started_at is already durable.
        try:
            refresh = self.refresh_snapshot(phone)
        except Exception:
            log.exception("Appointment changed but snapshot refresh failed")
            refresh = {"ok": False, "code": "snapshot_refresh_failed"}
        refresh_ok = isinstance(refresh, dict) and refresh.get("ok") is True
        self._finalize_success(key, phone, inference, str(result.get("code") or "success"))
        if not refresh_ok:
            self._create_attention(f"snapshot:{key}", phone, "scheduling_unavailable",
                "Appointment changed, but the authoritative snapshot could not be refreshed.",
                {"event_key": key, "action": inference["action"]})
        self._complete_trigger(message_id)

    def process_one(self) -> bool:
        trigger = self.claim_trigger()
        if not trigger:
            return False
        try:
            self.process_trigger(trigger)
        except Exception as exc:
            # Leave processing + an expiring lease. A later worker will recover;
            # event side-effect markers prevent mutation replay.
            log.exception("Passive appointment synchronization failed")
            self.db("UPDATE passive_sync_queue SET last_error=%s WHERE message_id=%s",
                    (type(exc).__name__, trigger["message_id"]))
        return True

    def recover_uncertain_events(self) -> int:
        rows = self.db("""SELECT event_key,trigger_message_id,phone_number,payload,
            status,side_effect_started_at,result_code
            FROM passive_appointment_events
            WHERE status='processing' AND side_effect_started_at IS NOT NULL
              AND updated_at < NOW() - INTERVAL '5 minutes' LIMIT 50""", fetchall=True)
        for row in rows or []:
            inference = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            self._recover_existing(row["event_key"], {
                "message_id": row["trigger_message_id"],
                "phone_number": row["phone_number"],
            }, dict(row), inference)
        return len(rows or [])


def supervisor_summary(db: Callable, mode: str, master_enabled: bool,
                       gemini_ready: bool, scheduling_ready: bool) -> dict:
    row = db("""SELECT
        COUNT(*) FILTER (WHERE latest_patient_message_id>latest_response_message_id
                         AND NOT is_archived) AS needs_reply,
        COUNT(*) FILTER (WHERE latest_patient_message_id>latest_response_message_id
                         AND latest_patient_message_at<NOW()-INTERVAL '15 minutes'
                         AND NOT is_archived) AS waiting_over_15,
        (SELECT COUNT(DISTINCT phone_number) FROM chat_history
          WHERE role='staff' AND created_at >=
            ((NOW() AT TIME ZONE 'Africa/Cairo')::date AT TIME ZONE 'Africa/Cairo'))
          AS human_handled_today,
        (SELECT COUNT(*) FROM audit_log WHERE action IN
          ('appointment_booked') AND created_at >=
            ((NOW() AT TIME ZONE 'Africa/Cairo')::date AT TIME ZONE 'Africa/Cairo'))
          AS todays_bookings,
        (SELECT MAX(created_at) FROM chat_history WHERE role='staff') AS last_staff_activity,
        (SELECT COUNT(*) FROM supervisor_attention WHERE status='open') AS attention_count,
        (SELECT COUNT(*) FROM supervisor_attention WHERE status='open' AND kind IN
          ('scheduling_unavailable','appointment_sync_conflict','passive_sync_failed'))
          AS sync_failures
        FROM conversation_summaries""", fetchone=True) or {}
    data = dict(row)
    last = data.get("last_staff_activity")
    if isinstance(last, str):
        try: last = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError: last = None
    now = dt.datetime.now(dt.timezone.utc)
    if isinstance(last, dt.datetime) and last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    configured_mode = normalize_mode(mode)
    effective_ai = patient_facing_ai_allowed(configured_mode, master_enabled)
    # Never present an impossible "AI ACTIVE" state to the owner. Older
    # deployments could contain AI_ACTIVE while the legacy master switch was
    # off; expose that conservatively as standby until activation repairs both.
    effective_mode = configured_mode if configured_mode != "AI_ACTIVE" or effective_ai else "AI_BACKUP"
    data.update({
        "operating_mode": effective_mode,
        "configured_operating_mode": configured_mode,
        "master_enabled": bool(master_enabled),
        "effective_patient_facing_ai": effective_ai,
        "receptionist_status": "active" if last and now-last.astimezone(dt.timezone.utc) <= dt.timedelta(minutes=15) else "no_recent_activity",
        "ai_receptionist_status": "active" if effective_ai else "standby",
        "appointment_sync_status": "active" if gemini_ready and scheduling_ready and not data.get("sync_failures") else "degraded",
    })
    return data


def list_attention(db: Callable, limit: int = 100) -> list[dict]:
    # Materialize long-wait alerts with a key tied to the exact unanswered turn.
    db("""INSERT INTO supervisor_attention(event_key,phone_number,kind,title,details)
        SELECT 'waiting:'||phone_number||':'||latest_patient_message_id,phone_number,
               'patient_waiting','Patient has waited more than 15 minutes.',
               jsonb_build_object('message_id',latest_patient_message_id,
                                  'waiting_since',latest_patient_message_at)
        FROM conversation_summaries
        WHERE latest_patient_message_id>latest_response_message_id
          AND latest_patient_message_at<NOW()-INTERVAL '15 minutes'
          AND NOT is_archived
        ON CONFLICT(event_key) DO NOTHING""")
    rows = db("""SELECT id,event_key,phone_number,kind,title,details,created_at
        FROM supervisor_attention WHERE status='open'
        ORDER BY created_at DESC,id DESC LIMIT %s""", (max(1, min(limit, 200)),), fetchall=True)
    return [dict(row) for row in rows or []]
