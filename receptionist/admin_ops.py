"""Structured backend operations for the admin CRM dashboard.

The Google Apps Script remains authoritative for appointment data. Cached appointment
snapshots always carry an explicit status and are never interpreted as empty after a
failed refresh.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable

from .booking import AppsScriptTemporaryError, normalize_phone, normalize_time
from .clinic import CLINIC, ClinicConfig
from .dates import parse_date_expression


APPOINTMENT_SNAPSHOT_TTL = dt.timedelta(minutes=10)


def success(code: str, data: Any = None, **meta: Any) -> dict:
    response = {"ok": True, "code": code, "data": data if data is not None else {}}
    response.update(meta)
    return response


def failure(
    code: str,
    message: str,
    *,
    state: str = "unavailable",
    retryable: bool = False,
    data: Any = None,
) -> dict:
    return {
        "ok": False,
        "code": code,
        "message": message,
        "state": state,
        "retryable": retryable,
        "data": data,
    }


def sort_inbox_rows(rows: list[dict]) -> list[dict]:
    """Deterministic ordering by actual latest message, never profile update time."""
    return sorted(
        rows,
        key=lambda row: (
            row.get("last_message_at") or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            int(row.get("last_message_id") or 0),
        ),
        reverse=True,
    )


def build_dashboard_metrics(row: dict) -> dict:
    incoming = int(row.get("unique_incoming_patients_today") or 0)
    bookings = int(row.get("bookings_today") or 0)
    return {
        "total_patients": int(row.get("total_patients") or 0),
        "conversations_today": int(row.get("conversations_today") or 0),
        "unique_incoming_patients_today": incoming,
        "incoming_messages_today": int(row.get("incoming_messages_today") or 0),
        "ai_messages_today": int(row.get("ai_messages_today") or 0),
        "staff_messages_today": int(row.get("staff_messages_today") or 0),
        "human_takeover_count": int(row.get("human_takeover_count") or 0),
        "bookings_today": bookings,
        "cancellations_today": int(row.get("cancellations_today") or 0),
        "booking_conversion_rate": {
            "value": None,
            "unit": "percent",
            "status": "unknown",
            "reason": "Bookings are not linked to a specific conversation session.",
        },
        # Apps Script has no global future-appointments endpoint. A partial cache
        # must not be presented as a global metric.
        "upcoming_appointments": {
            "value": None,
            "status": "unknown",
            "reason": "Scheduling backend does not expose a reliable global count.",
        },
    }


class AdminOperations:
    VALID_FILTERS = {"all", "unread", "human", "ai", "booked", "no_booking"}

    def __init__(
        self,
        db_execute: Callable[..., Any],
        booking_service: Any,
        *,
        config: ClinicConfig = CLINIC,
        now_func: Callable[[], dt.datetime] | None = None,
    ):
        self.db = db_execute
        self.booking = booking_service
        self.config = config
        self.now_func = now_func or config.now

    def _now(self) -> dt.datetime:
        value = self.now_func()
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.config.timezone)
        return value.astimezone(self.config.timezone)

    def _snapshot_state(self, status: Any, fetched_at: Any) -> str:
        """Return the authoritative usability state of a cached snapshot."""
        if status != "healthy":
            return status if status in {"stale", "unavailable", "unknown"} else "unknown"
        if not fetched_at:
            return "unknown"
        try:
            fetched = fetched_at
            if isinstance(fetched, str):
                fetched = dt.datetime.fromisoformat(fetched.replace("Z", "+00:00"))
            if not isinstance(fetched, dt.datetime):
                return "unknown"
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=self.config.timezone)
            if self._now() - fetched.astimezone(self.config.timezone) >= APPOINTMENT_SNAPSHOT_TTL:
                return "stale"
        except (TypeError, ValueError, OverflowError):
            return "unknown"
        return "healthy"

    def _apply_snapshot_state(self, row: dict) -> dict:
        state = self._snapshot_state(
            row.get("appointment_status"), row.get("appointment_fetched_at")
        )
        row["appointment_status"] = state
        row["appointment_freshness"] = "fresh" if state == "healthy" else state
        return row

    def inbox(
        self,
        *,
        search: str = "",
        state: str = "all",
        limit: int = 50,
        before_id: int | None = None,
        after_id: int | None = None,
    ) -> dict:
        if state not in self.VALID_FILTERS:
            return failure("invalid_filter", "Unknown inbox filter.", state="degraded")
        if before_id is not None and after_id is not None:
            return failure("invalid_cursor", "Use before_id or after_id, not both.", state="degraded")

        where: list[str] = []
        params: list[Any] = []
        search = search.strip()
        if search:
            where.append("(a.phone_number ILIKE %s OR COALESCE(p.name,'') ILIKE %s)")
            term = f"%{search}%"
            params.extend([term, term])
        if before_id is not None:
            where.append("COALESCE(l.id,0) < %s")
            params.append(before_id)
        if after_id is not None:
            where.append("COALESCE(l.id,0) > %s")
            params.append(after_id)

        filters = {
            "unread": "COALESCE(u.unread_count,0) > 0",
            "human": "COALESCE(p.is_paused,FALSE) = TRUE",
            "ai": "COALESCE(p.is_paused,FALSE) = FALSE",
            "booked": "aps.status = 'healthy' AND aps.fetched_at > NOW() - INTERVAL '10 minutes' AND aps.next_appointment IS NOT NULL",
            "no_booking": "aps.status = 'healthy' AND aps.fetched_at > NOW() - INTERVAL '10 minutes' AND aps.next_appointment IS NULL",
        }
        if state in filters:
            where.append(filters[state])
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        params.append(max(1, min(limit, 100)) + 1)

        try:
            rows = self.db(
                f"""
                /* admin_inbox */
                WITH active AS (
                    SELECT phone_number FROM patients
                    UNION
                    SELECT DISTINCT phone_number FROM chat_history
                ),
                latest AS (
                    SELECT DISTINCT ON (phone_number)
                        phone_number, id, role, content, created_at
                    FROM chat_history
                    ORDER BY phone_number, id DESC
                ),
                unread AS (
                    SELECT c.phone_number, COUNT(*)::int AS unread_count
                    FROM chat_history c
                    LEFT JOIN admin_inbox_state s ON s.phone_number=c.phone_number
                    WHERE c.role='user' AND c.id > COALESCE(s.last_read_message_id,0)
                    GROUP BY c.phone_number
                )
                SELECT
                    a.phone_number,
                    COALESCE(p.name,'') AS name,
                    COALESCE(p.tags,'{{}}') AS tags,
                    COALESCE(p.is_paused,FALSE) AS is_paused,
                    l.id AS last_message_id,
                    l.role AS last_message_role,
                    l.content AS last_message,
                    l.created_at AS last_message_at,
                    COALESCE(u.unread_count,0) AS unread_count,
                    aps.status AS appointment_status,
                    aps.next_appointment,
                    aps.fetched_at AS appointment_fetched_at
                FROM active a
                LEFT JOIN patients p ON p.phone_number=a.phone_number
                LEFT JOIN latest l ON l.phone_number=a.phone_number
                LEFT JOIN unread u ON u.phone_number=a.phone_number
                LEFT JOIN admin_appointment_snapshots aps ON aps.phone_number=a.phone_number
                {where_sql}
                ORDER BY l.created_at DESC NULLS LAST, l.id DESC NULLS LAST
                LIMIT %s
                """,
                tuple(params),
                fetchall=True,
            )
        except Exception:
            return failure("database_unavailable", "Could not load the inbox.", retryable=True)

        has_more = len(rows) > limit
        patients = sort_inbox_rows(
            [self._apply_snapshot_state(dict(row)) for row in rows[:limit]]
        )
        ids = [int(row.get("last_message_id") or 0) for row in patients]
        return success(
            "inbox_loaded",
            {
                "patients": patients,
                "has_more": has_more,
                "next_before_id": min(ids) if has_more and ids else None,
                "server_cursor": max(ids) if ids else (after_id or 0),
            },
        )

    def mark_read(self, phone_number: str, displayed_message_id: int) -> dict:
        phone = normalize_phone(phone_number)
        if displayed_message_id < 0:
            return failure("invalid_cursor", "Displayed message id must not be negative.", state="degraded")
        try:
            row = self.db(
                """
                /* admin_mark_read */
                INSERT INTO admin_inbox_state(phone_number,last_read_message_id,updated_at)
                VALUES (%s,%s,NOW())
                ON CONFLICT(phone_number) DO UPDATE SET
                    last_read_message_id=GREATEST(
                        admin_inbox_state.last_read_message_id,
                        EXCLUDED.last_read_message_id
                    ),
                    updated_at=NOW()
                RETURNING last_read_message_id
                """,
                (phone, displayed_message_id),
                fetchone=True,
            )
            return success("inbox_marked_read", {"phone_number": phone, "last_read_message_id": int(row["last_read_message_id"] if row else 0)})
        except Exception:
            return failure("database_unavailable", "Could not update unread state.", retryable=True)

    def inbox_metadata(self, phone_numbers: list[str]) -> dict:
        """Refresh snapshot status without reloading messages or patient profiles."""
        phones = list(
            dict.fromkeys(
                normalize_phone(phone)
                for phone in phone_numbers
                if normalize_phone(phone)
            )
        )[:100]
        if not phones:
            return success("inbox_metadata_loaded", {"patients": []})
        try:
            rows = self.db(
                """
                /* admin_inbox_metadata */
                WITH requested AS (
                    SELECT UNNEST(%s::text[]) AS phone_number
                )
                SELECT
                    r.phone_number,
                    aps.status AS appointment_status,
                    aps.next_appointment,
                    aps.fetched_at AS appointment_fetched_at
                FROM requested r
                LEFT JOIN admin_appointment_snapshots aps
                  ON aps.phone_number=r.phone_number
                """,
                (phones,),
                fetchall=True,
            )
        except Exception:
            return failure(
                "database_unavailable",
                "Could not refresh inbox appointment metadata.",
                retryable=True,
            )
        return success(
            "inbox_metadata_loaded",
            {
                "patients": [
                    self._apply_snapshot_state(dict(row)) for row in rows
                ],
                "refreshed_at": self._now().isoformat(),
            },
        )

    def patient_detail(self, phone_number: str) -> dict:
        phone = normalize_phone(phone_number)
        try:
            row = self.db(
                """
                /* admin_patient_detail */
                SELECT
                    %s AS phone_number,
                    COALESCE(p.name,'') AS name,
                    COALESCE(p.tags,'{}') AS tags,
                    COALESCE(p.preferences,'') AS preferences,
                    COALESCE(p.is_paused,FALSE) AS is_paused,
                    p.created_at,
                    MAX(c.created_at) AS last_active_at,
                    aps.status AS appointment_status,
                    aps.appointments,
                    aps.next_appointment,
                    aps.fetched_at AS appointment_fetched_at
                FROM (SELECT %s::varchar AS phone_number) wanted
                LEFT JOIN patients p ON p.phone_number=wanted.phone_number
                LEFT JOIN chat_history c ON c.phone_number=wanted.phone_number
                LEFT JOIN admin_appointment_snapshots aps ON aps.phone_number=wanted.phone_number
                GROUP BY p.name,p.tags,p.preferences,p.is_paused,p.created_at,
                         aps.status,aps.appointments,aps.next_appointment,aps.fetched_at
                """,
                (phone, phone),
                fetchone=True,
            )
            if not row:
                return failure("patient_not_found", "Patient was not found.", state="degraded")
            return success("patient_loaded", self._apply_snapshot_state(dict(row)))
        except Exception:
            return failure("database_unavailable", "Could not load patient details.", retryable=True)

    def messages(
        self,
        phone_number: str,
        *,
        limit: int = 60,
        before_id: int | None = None,
        after_id: int | None = None,
    ) -> dict:
        if before_id is not None and after_id is not None:
            return failure("invalid_cursor", "Use before_id or after_id, not both.", state="degraded")
        phone = normalize_phone(phone_number)
        limit = max(1, min(limit, 200))
        try:
            if after_id is not None:
                rows = self.db(
                    """
                    /* admin_messages_after */
                    SELECT id,role,content,created_at,whatsapp_message_id
                    FROM chat_history
                    WHERE phone_number=%s AND id>%s
                    ORDER BY id ASC LIMIT %s
                    """,
                    (phone, after_id, limit),
                    fetchall=True,
                )
                return success("messages_loaded", {"messages": [dict(x) for x in rows], "has_more": False})

            params: list[Any] = [phone]
            clause = ""
            if before_id is not None:
                clause = " AND id<%s"
                params.append(before_id)
            params.append(limit + 1)
            rows = self.db(
                f"""
                /* admin_messages_page */
                SELECT id,role,content,created_at,whatsapp_message_id
                FROM chat_history
                WHERE phone_number=%s{clause}
                ORDER BY id DESC LIMIT %s
                """,
                tuple(params),
                fetchall=True,
            )
            has_more = len(rows) > limit
            messages = [dict(x) for x in rows[:limit]]
            messages.reverse()
            return success("messages_loaded", {"messages": messages, "has_more": has_more})
        except Exception:
            return failure("database_unavailable", "Could not load messages.", retryable=True)

    @staticmethod
    def _appointment_date(item: Any) -> dt.date | None:
        raw: Any = None
        if isinstance(item, dict):
            for key in ("date", "appointment_date", "Date"):
                if item.get(key):
                    raw = item[key]
                    break
        elif isinstance(item, str):
            raw = item
        if not raw:
            return None
        text = str(raw)
        for token in text.replace("/", "-").split():
            for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
                try:
                    return dt.datetime.strptime(token[:10], fmt).date()
                except ValueError:
                    continue
        return None

    def _appointment_datetime(self, item: Any) -> dt.datetime | None:
        date = self._appointment_date(item)
        if isinstance(item, dict):
            raw_time = next(
                (
                    item.get(key)
                    for key in ("time", "appointment_time", "Time", "الوقت")
                    if item.get(key)
                ),
                None,
            )
        elif isinstance(item, str):
            match = re.search(r"\b(\d{1,2}:\d{1,2}\s*[AP]M)\b", item, re.IGNORECASE)
            raw_time = match.group(1) if match else None
        else:
            raw_time = None
        if date is None or not isinstance(raw_time, str) or not raw_time.strip():
            return None
        try:
            parsed_time = dt.datetime.strptime(normalize_time(raw_time), "%I:%M %p").time()
        except ValueError:
            return None
        return dt.datetime.combine(date, parsed_time, self.config.timezone)

    def _cache_appointments(
        self,
        phone: str,
        appointments: list,
        next_appointment: Any,
        status: str,
    ) -> None:
        self.db(
            """
            /* admin_cache_appointments */
            INSERT INTO admin_appointment_snapshots(
                phone_number,appointments,next_appointment,status,fetched_at
            ) VALUES (%s,%s::jsonb,%s::jsonb,%s,NOW())
            ON CONFLICT(phone_number) DO UPDATE SET
                appointments=EXCLUDED.appointments,
                next_appointment=EXCLUDED.next_appointment,
                status=EXCLUDED.status,
                fetched_at=NOW()
            """,
            (
                phone,
                json.dumps(appointments, ensure_ascii=False, default=str),
                json.dumps(next_appointment, ensure_ascii=False, default=str),
                status,
            ),
        )

    def _mark_snapshot_status(self, phone: str, status: str) -> None:
        """Record a failed refresh without replacing or re-dating prior good data."""
        self.db(
            """
            /* admin_cache_appointments_failure */
            INSERT INTO admin_appointment_snapshots(
                phone_number,appointments,next_appointment,status,fetched_at
            ) VALUES (%s,NULL,NULL,%s,NULL)
            ON CONFLICT(phone_number) DO UPDATE SET status=EXCLUDED.status
            """,
            (phone, status),
        )

    def patient_appointments(self, phone_number: str) -> dict:
        phone = normalize_phone(phone_number)
        result = self.booking.appointments(phone)
        if not isinstance(result, dict) or result.get("ok") is not True:
            try:
                self._mark_snapshot_status(phone, "unavailable")
            except Exception:
                pass
            return failure(
                "scheduling_unavailable",
                str(result.get("message") if isinstance(result, dict) else "Malformed scheduling response."),
                retryable=True,
                data={"snapshot_status": "unavailable"},
            )
        appointments = result.get("appointments")
        if not isinstance(appointments, list):
            try:
                self._mark_snapshot_status(phone, "unknown")
            except Exception:
                pass
            return failure(
                "malformed_scheduling_response",
                "Scheduling returned malformed appointment data.",
                state="degraded",
                retryable=True,
                data={"snapshot_status": "unknown"},
            )

        now = self._now()
        upcoming: list[Any] = []
        previous: list[Any] = []
        unknown: list[Any] = []
        for item in appointments:
            moment = self._appointment_datetime(item)
            if moment is None:
                unknown.append(item)
            elif moment > now:
                upcoming.append(item)
            else:
                previous.append(item)
        upcoming.sort(key=lambda item: self._appointment_datetime(item) or dt.datetime.max.replace(tzinfo=self.config.timezone))
        previous.sort(key=lambda item: self._appointment_datetime(item) or dt.datetime.min.replace(tzinfo=self.config.timezone), reverse=True)
        next_appointment = upcoming[0] if upcoming else None
        try:
            self._cache_appointments(phone, appointments, next_appointment, "healthy")
        except Exception:
            # The live authoritative response remains usable even if cache persistence fails.
            pass
        return success(
            "appointments_loaded",
            {
                "phone_number": phone,
                "appointments": appointments,
                "upcoming": upcoming,
                "previous": previous,
                "unclassified": unknown,
                "next_appointment": next_appointment,
                "source": "google_apps_script",
                "fetched_at": now.isoformat(),
                "snapshot_status": "healthy",
                "snapshot_freshness": "fresh",
            },
        )

    def schedule(self, date_value: str) -> dict:
        try:
            date = parse_date_expression(date_value, now=self._now(), timezone=self.config.timezone)
        except ValueError as exc:
            return failure("invalid_date", str(exc), state="degraded")

        closed = date.weekday() in self.config.closed_weekdays
        now = self._now()
        if closed:
            slots = [{"time": slot, "status": "clinic_closed", "appointment": None} for slot in self.config.slots]
            return success("schedule_loaded", {"date": date.isoformat(), "state": "clinic_closed", "slots": slots, "source": "clinic_config"})

        try:
            payload = self.booking._request("GET", {"date": date.isoformat()})
            booked = payload.get("booked")
            if booked is None:
                booked = []
            if not isinstance(booked, list):
                raise AppsScriptTemporaryError("invalid_schedule")
            by_time: dict[str, Any] = {}
            for item in booked:
                raw_time = (
                    next(
                        (item.get(key) for key in ("time", "appointment_time", "Time", "الوقت") if item.get(key)),
                        None,
                    )
                    if isinstance(item, dict)
                    else item
                )
                if not isinstance(raw_time, str) or not raw_time.strip():
                    raise AppsScriptTemporaryError("invalid_schedule_item")
                normalized = normalize_time(raw_time)
                if normalized not in self.config.slots:
                    raise AppsScriptTemporaryError("invalid_schedule_time")
                by_time[normalized] = item
        except Exception:
            return failure(
                "scheduling_unavailable",
                "Could not load the authoritative schedule. Availability is unknown.",
                retryable=True,
                data={"date": date.isoformat(), "state": "unavailable", "slots": []},
            )

        slots = []
        for slot in self.config.slots:
            slot_time = dt.datetime.strptime(slot, "%I:%M %p").time()
            moment = dt.datetime.combine(date, slot_time, self.config.timezone)
            if moment <= now:
                status = "past"
            elif slot in by_time:
                status = "booked"
            else:
                status = "available"
            slots.append({"time": slot, "status": status, "appointment": by_time.get(slot)})
        return success(
            "schedule_loaded",
            {
                "date": date.isoformat(),
                "state": "healthy",
                "slots": slots,
                "source": "google_apps_script",
                "fetched_at": now.isoformat(),
            },
        )

    def dashboard_summary(self) -> dict:
        try:
            row = self.db(
                """
                /* admin_dashboard_summary */
                SELECT
                    (SELECT COUNT(*) FROM patients) AS total_patients,
                    (SELECT COUNT(DISTINCT phone_number) FROM chat_history
                     WHERE created_at >= date_trunc('day',NOW() AT TIME ZONE 'Africa/Cairo')
                         AT TIME ZONE 'Africa/Cairo') AS conversations_today,
                    (SELECT COUNT(DISTINCT phone_number) FROM chat_history
                     WHERE role='user' AND created_at >= date_trunc('day',NOW() AT TIME ZONE 'Africa/Cairo')
                         AT TIME ZONE 'Africa/Cairo') AS unique_incoming_patients_today,
                    (SELECT COUNT(*) FROM chat_history
                     WHERE role='user' AND created_at >= date_trunc('day',NOW() AT TIME ZONE 'Africa/Cairo')
                         AT TIME ZONE 'Africa/Cairo') AS incoming_messages_today,
                    (SELECT COUNT(*) FROM chat_history
                     WHERE role='model' AND created_at >= date_trunc('day',NOW() AT TIME ZONE 'Africa/Cairo')
                         AT TIME ZONE 'Africa/Cairo') AS ai_messages_today,
                    (SELECT COUNT(*) FROM chat_history
                     WHERE role='staff' AND created_at >= date_trunc('day',NOW() AT TIME ZONE 'Africa/Cairo')
                         AT TIME ZONE 'Africa/Cairo') AS staff_messages_today,
                    (SELECT COUNT(*) FROM patients WHERE is_paused) AS human_takeover_count,
                    (SELECT COUNT(*) FROM audit_log WHERE action='appointment_booked'
                     AND created_at >= date_trunc('day',NOW() AT TIME ZONE 'Africa/Cairo')
                         AT TIME ZONE 'Africa/Cairo') AS bookings_today,
                    (SELECT COUNT(*) FROM audit_log WHERE action='appointment_cancelled'
                     AND created_at >= date_trunc('day',NOW() AT TIME ZONE 'Africa/Cairo')
                         AT TIME ZONE 'Africa/Cairo') AS cancellations_today
                """,
                fetchone=True,
            )
            return success("dashboard_summary_loaded", build_dashboard_metrics(dict(row or {})))
        except Exception:
            return failure("database_unavailable", "Could not calculate dashboard metrics.", retryable=True)

    def analytics(self, days: int = 14) -> dict:
        days = max(7, min(days, 90))
        local_day = self._now().date()
        try:
            rows = self.db(
                """
                /* admin_analytics */
                WITH dates AS (
                    SELECT generate_series(
                        (%s::date - (%s - 1) * INTERVAL '1 day')::date,
                        %s::date, INTERVAL '1 day'
                    )::date AS day
                )
                SELECT d.day,
                    COUNT(c.id)::int AS messages,
                    COUNT(c.id) FILTER (WHERE c.role='user')::int AS incoming,
                    COUNT(c.id) FILTER (WHERE c.role='model')::int AS ai,
                    COUNT(c.id) FILTER (WHERE c.role='staff')::int AS staff,
                    COUNT(DISTINCT c.phone_number)::int AS active_patients
                FROM dates d
                LEFT JOIN chat_history c
                  ON (c.created_at AT TIME ZONE 'Africa/Cairo')::date=d.day
                GROUP BY d.day ORDER BY d.day
                """,
                (local_day, days, local_day),
                fetchall=True,
            )
            return success("analytics_loaded", {"days": days, "daily": [dict(x) for x in rows]})
        except Exception:
            return failure("database_unavailable", "Could not load analytics.", retryable=True)

    def system_health(
        self,
        *,
        global_bot_active: bool,
        gemini_configured: bool,
        gemini_initialized: bool,
        whatsapp_configured: bool,
    ) -> dict:
        components: dict[str, dict] = {}
        try:
            self.db("/* admin_health_db */ SELECT 1 AS ok", fetchone=True)
            components["database"] = {"status": "healthy"}
        except Exception:
            components["database"] = {"status": "unavailable"}

        try:
            payload = self.booking._request("GET", {"date": self._now().date().isoformat()})
            booked = payload.get("booked", [])
            if not isinstance(booked, list):
                raise ValueError("malformed")
            components["scheduling"] = {"status": "healthy"}
        except Exception:
            components["scheduling"] = {"status": "unavailable"}

        components["gemini"] = {
            "status": "unknown" if gemini_configured and gemini_initialized else "unavailable",
            "configured": gemini_configured,
            "initialized": gemini_initialized,
            "detail": "Configured; no billable health request was sent." if gemini_configured and gemini_initialized else "Not configured or initialization failed.",
        }
        components["whatsapp"] = {
            "status": "unknown" if whatsapp_configured else "unavailable",
            "configured": whatsapp_configured,
            "detail": "Configured; no patient-facing probe was sent." if whatsapp_configured else "Not configured.",
        }
        components["global_bot"] = {"status": "healthy" if global_bot_active else "degraded", "active": global_bot_active}
        statuses = {item["status"] for item in components.values()}
        overall = "unavailable" if statuses == {"unavailable"} else "degraded" if ("unavailable" in statuses or "degraded" in statuses) else "healthy" if statuses == {"healthy"} else "unknown"
        return success("system_health_loaded", {"overall": overall, "components": components, "checked_at": self._now().isoformat()})
