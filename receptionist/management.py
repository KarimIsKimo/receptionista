"""Admin-only management, separate from receptionist/booking behavior.

Bulk edits use one bounded transaction, the existing transactional inbox clock,
and no external side effects. Historical free-form tags are never migrated away.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import tempfile
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor
from starlette.background import BackgroundTask

from .admin_ops import failure, success
from .clinic import CLINIC


log = logging.getLogger(__name__)
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS management_tags (
        id BIGSERIAL PRIMARY KEY,
        name VARCHAR(50) NOT NULL CHECK (length(btrim(name)) > 0),
        is_archived BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_management_tag_name ON management_tags(lower(name))",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor_id ON audit_log(actor, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action_id ON audit_log(action, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_phone_id ON audit_log(phone_number, id DESC)",
)
AUDIT_ACTIONS = (
    "staff_manual_message", "patient_pause_on", "patient_pause_off",
    "appointment_booked", "appointment_cancelled", "appointment_rescheduled",
    "rename_patient", "update_patient_preferences", "update_patient_tags",
    "archive_conversation", "reopen_conversation", "pin_conversation",
    "unpin_conversation", "mark_unread", "global_bot_on", "global_bot_off",
    "update_system_instruction", "create_conversation", "tag_created",
    "tag_renamed", "tag_archived", "tag_restored", "export_patients",
    "export_conversations",
)
ACTION_AUDIT = {
    "archive": "archive_conversation", "reopen": "reopen_conversation",
    "unread": "mark_unread", "pause": "patient_pause_on",
    "resume": "patient_pause_off", "add_tag": "update_patient_tags",
}


class BulkRequest(BaseModel):
    phones: list[str] = Field(min_length=1, max_length=100)
    action: Literal["archive", "reopen", "unread", "pause", "resume", "add_tag"]
    tag_id: int | None = Field(default=None, ge=1)


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    is_archived: bool | None = None


def valid_phone(value: str) -> str:
    """Accept formatting/local Egyptian numbers, never silently strip letters."""
    if not isinstance(value, str) or not re.fullmatch(r"\+?[0-9 ()-]{5,30}", value.strip()):
        raise ValueError("invalid_phone")
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("01") and len(digits) == 11:
        digits = "2" + digits
    elif digits.startswith("1") and len(digits) == 10:
        digits = "20" + digits
    if not re.fullmatch(r"[1-9][0-9]{4,14}", digits):
        raise ValueError("invalid_phone")
    return digits


def clinic_configuration(config=CLINIC) -> dict:
    # Only expose explicit public fields, never serialize config/environment.
    return {
        "read_only": True, "source": "receptionist.clinic.CLINIC",
        "branch": config.branch, "address": config.address,
        "timezone": config.timezone_name,
        "opening_time": config.opening_time.isoformat(timespec="minutes"),
        "closing_time": config.closing_time.isoformat(timespec="minutes"),
        "closed_weekdays": list(config.closed_weekdays),
        "slot_interval_minutes": config.slot_minutes,
        "appointment_duration_minutes": None,
        "booking_horizon_days": config.booking_horizon_days,
        "services": None,  # No authoritative catalog; backend accepts an area string.
    }


def csv_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " | ".join(str(x) for x in value)
    elif isinstance(value, (dt.datetime, dt.date)):
        value = value.isoformat()
    text = str(value)
    # Prevent spreadsheet formulas even behind whitespace/control characters.
    if text.lstrip(" \t\r\n\x00").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        text = "'" + text
    return text


class ManagementOperations:
    def __init__(self, connection, db):
        self.connection = connection
        self.db = db

    def ai_state(self):
        try:
            row = self.db("""
                SELECT COUNT(*) FILTER (WHERE COALESCE(p.is_paused,FALSE)) AS human_handled,
                       COUNT(*) FILTER (WHERE NOT COALESCE(p.is_paused,FALSE)) AS ai_assigned,
                       COUNT(*) FILTER (WHERE s.latest_patient_message_id>s.latest_response_message_id)
                         AS needs_reply,
                       COUNT(*) FILTER (WHERE s.latest_patient_message_id>s.latest_response_message_id
                         AND (COALESCE(p.is_paused,FALSE) OR
                           COALESCE((SELECT content FROM clinic_settings WHERE key='bot_globally_active'),'true')<>'true'))
                         AS needs_staff_reply,
                       COALESCE((SELECT content FROM clinic_settings WHERE key='bot_globally_active'),'true')='true'
                         AS global_active
                FROM conversation_summaries s LEFT JOIN patients p USING(phone_number)
                WHERE NOT s.is_archived
            """, fetchone=True)
            return success("management_ai_loaded", dict(row))
        except Exception:
            return failure("database_unavailable", "Could not load AI state.", retryable=True)

    def tags(self):
        try:
            rows = self.db("SELECT id,name,is_archived FROM management_tags ORDER BY is_archived,lower(name),id", fetchall=True)
            return success("tags_loaded", {"tags": [dict(x) for x in rows]})
        except Exception:
            return failure("database_unavailable", "Could not load reusable tags.", retryable=True)

    def save_tag(self, actor, *, tag_id=None, name=None, is_archived=None):
        name = name.strip() if name is not None else None
        if name is not None and (not name or len(name)>50 or any(ord(c)<32 for c in name)):
            return failure("invalid_tag", "Use a tag name of 1–50 characters.", state="degraded")
        if tag_id is None and not name or tag_id is not None and name is None and is_archived is None:
            return failure("invalid_tag", "No tag change supplied.", state="degraded")
        try:
            with self.connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if tag_id is None:
                        cur.execute("INSERT INTO management_tags(name) VALUES (%s) RETURNING id,name,is_archived", (name,))
                        action = "tag_created"
                    else:
                        cur.execute("""UPDATE management_tags SET name=COALESCE(%s,name),
                            is_archived=COALESCE(%s,is_archived),updated_at=NOW()
                            WHERE id=%s RETURNING id,name,is_archived""", (name,is_archived,tag_id))
                        action = "tag_archived" if is_archived else "tag_restored" if is_archived is False else "tag_renamed"
                    row = cur.fetchone()
                    if not row:
                        conn.rollback()
                        return failure("tag_not_found", "Tag not found.", state="degraded")
                    cur.execute("INSERT INTO audit_log(actor,action) VALUES (%s,%s)", (actor,action))
                conn.commit()
            return success("tag_saved", dict(row))
        except Exception as exc:
            if getattr(exc, "pgcode", None) == "23505":
                return failure("tag_exists", "This reusable tag already exists (possibly archived).", state="degraded")
            log.exception("Management tag update failed")
            return failure("database_unavailable", "Could not save the tag.", retryable=True)

    def bulk(self, req: BulkRequest, actor: str):
        # Validate the whole request before any writes; duplicates are normalized.
        try:
            phones = sorted({valid_phone(x) for x in req.phones})
        except ValueError:
            return failure("invalid_phone", "Every selected phone must be valid. No changes were made.", state="degraded")
        results = []
        try:
            with self.connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    tag = None
                    if req.action == "add_tag":
                        cur.execute("SELECT name FROM management_tags WHERE id=%s AND NOT is_archived FOR SHARE", (req.tag_id,))
                        tag_row = cur.fetchone()
                        if not tag_row:
                            conn.rollback()
                            return failure("tag_not_found", "Choose an active reusable tag.", state="degraded")
                        tag = tag_row["name"]
                    # Lock all patient rows in a stable order before the clock,
                    # avoiding lock inversion with profile writers.
                    cur.execute("SELECT phone_number,tags FROM patients WHERE phone_number=ANY(%s) ORDER BY phone_number FOR UPDATE", (phones,))
                    patients = {r["phone_number"]: r for r in cur.fetchall()}
                    if req.action == "unread":
                        # Lock read cursors before the clock, matching read/unread writers.
                        for phone in patients:
                            cur.execute("""INSERT INTO admin_inbox_state(phone_number) VALUES (%s)
                                ON CONFLICT(phone_number) DO UPDATE SET phone_number=EXCLUDED.phone_number""", (phone,))
                    # Serialize version assignment without consuming a version.
                    # The row lock is held until commit, so no later transaction
                    # can allocate/commit past this bulk operation. Each call to
                    # next_conversation_summary_version() below therefore maps to
                    # one actual conversation_summaries change.
                    cur.execute("""SELECT version FROM conversation_summary_clock
                        WHERE id=1 FOR UPDATE""")
                    cur.fetchone()
                    for phone in phones:
                        patient = patients.get(phone)
                        if not patient:
                            results.append({"phone_number": phone, "ok": False, "code": "patient_not_found"})
                            continue
                        if req.action == "add_tag" and tag not in patient["tags"] and len(patient["tags"])>=20:
                            results.append({"phone_number": phone, "ok": False, "code": "tag_limit"})
                            continue
                        if req.action in {"pause", "resume"}:
                            cur.execute("UPDATE patients SET is_paused=%s,updated_at=NOW() WHERE phone_number=%s", (req.action=="pause",phone))
                        elif req.action == "add_tag":
                            cur.execute("""UPDATE patients SET tags=CASE WHEN %s=ANY(tags)
                                THEN tags ELSE array_append(tags,%s) END,updated_at=NOW()
                                WHERE phone_number=%s""", (tag,tag,phone))
                        cur.execute("""INSERT INTO conversation_summaries(phone_number,change_version)
                            VALUES (%s,next_conversation_summary_version()) ON CONFLICT(phone_number) DO UPDATE SET
                            change_version=EXCLUDED.change_version,updated_at=NOW()
                            RETURNING latest_patient_message_id,unread_count""", (phone,))
                        summary = cur.fetchone()
                        if req.action in {"archive", "reopen"}:
                            cur.execute("UPDATE conversation_summaries SET is_archived=%s WHERE phone_number=%s", (req.action=="archive",phone))
                        elif req.action == "unread" and summary["latest_patient_message_id"]:
                            cur.execute("""UPDATE admin_inbox_state SET last_read_message_id=%s,updated_at=NOW()
                                WHERE phone_number=%s""", (summary["latest_patient_message_id"]-1,phone))
                            cur.execute("UPDATE conversation_summaries SET unread_count=1 WHERE phone_number=%s", (phone,))
                        cur.execute("INSERT INTO audit_log(actor,action,phone_number) VALUES (%s,%s,%s)", (actor,ACTION_AUDIT[req.action],phone))
                        results.append({"phone_number": phone, "ok": True, "code": "updated"})
                conn.commit()
            # All valid item mutations, audit rows and cursor changes commit together.
            return success("bulk_complete", {"results": results, "updated": sum(r["ok"] for r in results)})
        except Exception:
            log.exception("Bulk management transaction rolled back")
            return failure("database_unavailable", "No changes were committed. Please retry.", retryable=True)

    def activity(self, *, actor="", action="", phone="", start=None, end=None, before=None, limit=50):
        where, params = ["action=ANY(%s)"], [list(AUDIT_ACTIONS)]
        if action and action not in AUDIT_ACTIONS:
            return failure("invalid_action", "Unknown activity action.", state="degraded")
        if start and end and start>end:
            return failure("invalid_date", "Start date must precede end date.", state="degraded")
        if phone:
            try:
                phone = valid_phone(phone)
            except ValueError:
                return failure("invalid_phone", "Use a valid patient phone.", state="degraded")
        for column, value in (("actor",actor),("action",action),("phone_number",phone)):
            if value:
                where.append(f"{column}=%s")
                params.append(value)
        if start:
            where.append("created_at >= %s")
            params.append(dt.datetime.combine(start,dt.time.min,CLINIC.timezone))
        if end:
            where.append("created_at < %s")
            params.append(dt.datetime.combine(end+dt.timedelta(days=1),dt.time.min,CLINIC.timezone))
        if before:
            where.append("id<%s")
            params.append(before)
        try:
            # Deliberately exclude details: historical payloads may contain notes,
            # message bodies or internal diagnostics. Only safe operational fields.
            rows = self.db("SELECT id,actor,action,phone_number,created_at FROM audit_log WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT %s", tuple(params+[limit+1]), fetchall=True)
            return success("activity_loaded", {"audit": [dict(r) for r in rows[:limit]], "next_cursor": rows[limit-1]["id"] if len(rows)>limit else None})
        except Exception:
            return failure("database_unavailable", "Could not load activity.", retryable=True)

    def export(self, kind: str, actor: str):
        """Build a spooled download in bounded DB pages, not browser memory.

        Failure happens before response headers, never silently a partial CSV.
        Pages release the pool connection; this is an operational export, not a
        point-in-time database backup. Concurrent edits may appear in later pages.
        """
        if kind not in {"patients", "conversations"}:
            raise HTTPException(400, "Unknown export")
        file = tempfile.TemporaryFile(mode="w+b")
        headers = ["phone", "name", "tags", "notes_preferences", "created_date", "last_activity"] if kind=="patients" else ["phone", "patient", "state", "last_activity", "unread_count", "handling_mode"]
        try:
            bound = self.db("SELECT MAX(phone_number) AS phone FROM patients", fetchone=True)["phone"]
            file.write(b"\xef\xbb\xbf")
            buf = io.StringIO()
            csv.writer(buf).writerow(headers)
            file.write(buf.getvalue().encode("utf-8"))
            after = ""
            while bound:
                columns = "p.phone_number,p.name,p.tags,p.preferences,p.created_at,s.last_message_at" if kind=="patients" else "p.phone_number,p.name,s.is_archived,s.last_message_at,s.unread_count,p.is_paused"
                rows = self.db("SELECT "+columns+" FROM patients p LEFT JOIN conversation_summaries s USING(phone_number) WHERE p.phone_number>%s AND p.phone_number<=%s ORDER BY p.phone_number LIMIT 500", (after,bound), fetchall=True)
                if not rows:
                    break
                buf = io.StringIO()
                writer = csv.writer(buf)
                for r in rows:
                    values = ["'"+r["phone_number"],r["name"]]
                    if kind=="patients":
                        values += [r["tags"],r["preferences"],r["created_at"],r["last_message_at"]]
                    else:
                        values += ["archived" if r["is_archived"] else "open",r["last_message_at"],r["unread_count"] or 0,"human" if r["is_paused"] else "ai"]
                    writer.writerow([csv_cell(v) for v in values])
                file.write(buf.getvalue().encode("utf-8"))
                after = rows[-1]["phone_number"]
            self.db("INSERT INTO audit_log(actor,action) VALUES (%s,%s)", (actor,"export_"+kind))
            file.seek(0)
        except Exception:
            file.close()
            log.exception("Management export failed")
            raise HTTPException(503, "Export unavailable; no file was produced.") from None
        def chunks():
            try:
                while chunk := file.read(65536):
                    yield chunk
            finally:
                file.close()
        return StreamingResponse(chunks(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="jothen-{kind}.csv"', "Cache-Control": "no-store"}, background=BackgroundTask(file.close))


def create_management_router(connection, db, verify_admin):
    router = APIRouter(prefix="/admin/api/management")
    ops = ManagementOperations(connection, db)

    @router.get("/ai")
    def ai(admin: str = Depends(verify_admin)):
        return ops.ai_state()

    @router.get("/clinic")
    def clinic(admin: str = Depends(verify_admin)):
        return success("clinic_loaded", clinic_configuration())

    @router.get("/tags")
    def tags(admin: str = Depends(verify_admin)):
        return ops.tags()

    @router.post("/tags")
    def create_tag(req: TagCreate, admin: str = Depends(verify_admin)):
        return ops.save_tag(admin, name=req.name)

    @router.patch("/tags/{tag_id}")
    def update_tag(tag_id: int, req: TagUpdate, admin: str = Depends(verify_admin)):
        return ops.save_tag(admin, tag_id=tag_id, name=req.name, is_archived=req.is_archived)

    @router.post("/bulk")
    def bulk(req: BulkRequest, admin: str = Depends(verify_admin)):
        return ops.bulk(req, admin)

    @router.get("/activity")
    def activity(actor: str = Query("", max_length=100), action: str = Query("", max_length=80),
                 phone: str = Query("", max_length=30), start: dt.date | None = None,
                 end: dt.date | None = None, before: int | None = Query(None, ge=1),
                 limit: int = Query(50, ge=1, le=100), admin: str = Depends(verify_admin)):
        return ops.activity(actor=actor, action=action, phone=phone, start=start, end=end, before=before, limit=limit)

    @router.get("/export/{kind}")
    def export(kind: str, admin: str = Depends(verify_admin)):
        return ops.export(kind, admin)

    return router
