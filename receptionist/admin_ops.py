"""Structured backend operations for the admin CRM dashboard.

The Google Apps Script remains authoritative for appointment data. Cached appointment
snapshots always carry an explicit status and are never interpreted as empty after a
failed refresh.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import re
from typing import Any, Callable

from .booking import AppsScriptTemporaryError, normalize_phone, normalize_time
from .clinic import CLINIC, ClinicConfig
from .dates import parse_date_expression


APPOINTMENT_SNAPSHOT_TTL = dt.timedelta(minutes=10)


def encode_inbox_cursor(row: dict) -> str:
    """Encode the complete stable inbox ordering key as an opaque cursor."""
    timestamp = row.get("last_message_at")
    if isinstance(timestamp, dt.datetime):
        timestamp = timestamp.isoformat()
    elif timestamp is not None:
        timestamp = str(timestamp)
    payload = {
        "p": bool(row.get("is_pinned")),
        "t": timestamp,
        "i": int(row.get("last_message_id") or 0),
        "n": str(row.get("phone_number") or ""),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_inbox_cursor(value: str) -> tuple[bool, str | None, int, str]:
    """Validate and decode a cursor without trusting browser-provided values."""
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
        pinned = payload["p"]
        timestamp = payload["t"]
        message_id = payload["i"]
        phone_number = payload["n"]
        if not isinstance(pinned, bool):
            raise ValueError
        if timestamp is not None:
            if not isinstance(timestamp, str):
                raise ValueError
            parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 0:
            raise ValueError
        if not isinstance(phone_number, str) or not phone_number:
            raise ValueError
        return pinned, timestamp, message_id, phone_number
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("Invalid inbox cursor") from exc


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
    def activity(row: dict) -> dt.datetime:
        value = row.get("last_message_at")
        if not isinstance(value, dt.datetime):
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value

    return sorted(
        rows,
        key=lambda row: (
            bool(row.get("is_pinned")),
            activity(row),
            int(row.get("last_message_id") or 0),
            str(row.get("phone_number") or ""),
        ),
        reverse=True,
    )


def classify_conversation_origin(first_meaningful_role: Any) -> str:
    """Classify from the earliest patient/staff message, never the latest turn."""
    if first_meaningful_role == "user":
        return "patient"
    if first_meaningful_role == "staff":
        return "reception"
    return "unknown"


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
    VALID_FILTERS = {
        "all",
        "unread",
        "patient_initiated",
        "reception_initiated",
        "human",
        "ai",
        "booked",
        "no_booking",
        "needs_reply",
        "archived",
    }

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

    def _prepare_inbox_row(self, row: dict) -> dict:
        row["conversation_origin"] = classify_conversation_origin(
            row.pop("first_message_role", None)
        )
        return self._apply_snapshot_state(row)

    def inbox(
        self,
        *,
        search: str = "",
        state: str = "all",
        limit: int = 50,
        before: str | None = None,
        before_id: int | None = None,
        after_id: int | None = None,
    ) -> dict:
        if state not in self.VALID_FILTERS:
            return failure("invalid_filter", "Unknown inbox filter.", state="degraded")
        if sum(value is not None for value in (before, before_id, after_id)) > 1:
            return failure(
                "invalid_cursor",
                "Use only one inbox pagination cursor.",
                state="degraded",
            )

        where: list[str] = []
        params: list[Any] = []
        search = search.strip()
        if search:
            where.append(
                "(s.phone_number ILIKE %s OR COALESCE(p.name,'') ILIKE %s "
                "OR COALESCE(p.preferences,'') ILIKE %s "
                "OR array_to_string(COALESCE(p.tags,'{}'), ' ') ILIKE %s)"
            )
            term = f"%{search}%"
            params.extend([term, term, term, term])
        if before is not None:
            try:
                pinned, timestamp, message_id, phone_number = decode_inbox_cursor(before)
            except ValueError:
                return failure("invalid_cursor", "Invalid inbox cursor.", state="degraded")
            where.append(
                "(s.is_pinned, "
                "COALESCE(s.last_message_at,'-infinity'::timestamptz), "
                "COALESCE(s.last_message_id,0), s.phone_number) < "
                "(%s::boolean, COALESCE(%s::timestamptz,'-infinity'::timestamptz), "
                "%s::bigint, %s::text)"
            )
            params.extend([pinned, timestamp, message_id, phone_number])
        elif before_id is not None:
            # Compatibility for older dashboard clients: use the message id only
            # to locate its complete sort key, then apply the same keyset boundary.
            where.append(
                "(s.is_pinned, "
                "COALESCE(s.last_message_at,'-infinity'::timestamptz), "
                "COALESCE(s.last_message_id,0), s.phone_number) < "
                "(SELECT legacy.is_pinned, "
                "COALESCE(legacy.last_message_at,'-infinity'::timestamptz), "
                "COALESCE(legacy.last_message_id,0), legacy.phone_number "
                "FROM conversation_summaries legacy "
                "WHERE legacy.last_message_id=%s)"
            )
            params.append(before_id)
        elif after_id is not None:
            where.append(
                "(s.is_pinned, "
                "COALESCE(s.last_message_at,'-infinity'::timestamptz), "
                "COALESCE(s.last_message_id,0), s.phone_number) > "
                "(SELECT legacy.is_pinned, "
                "COALESCE(legacy.last_message_at,'-infinity'::timestamptz), "
                "COALESCE(legacy.last_message_id,0), legacy.phone_number "
                "FROM conversation_summaries legacy "
                "WHERE legacy.last_message_id=%s)"
            )
            params.append(after_id)

        filters = {
            "unread": "s.unread_count > 0",
            "needs_reply": "s.latest_patient_message_id > s.latest_response_message_id",
            "patient_initiated": "s.first_meaningful_role = 'user'",
            "reception_initiated": "s.first_meaningful_role = 'staff'",
            "human": "COALESCE(p.is_paused,FALSE) = TRUE",
            "ai": "COALESCE(p.is_paused,FALSE) = FALSE",
            "booked": "aps.status = 'healthy' AND aps.fetched_at > NOW() - INTERVAL '10 minutes' AND aps.next_appointment IS NOT NULL",
            "no_booking": "aps.status = 'healthy' AND aps.fetched_at > NOW() - INTERVAL '10 minutes' AND aps.next_appointment IS NULL",
            "archived": "s.is_archived = TRUE",
        }
        if state != "archived":
            where.append("s.is_archived = FALSE")
        if state in filters:
            where.append(filters[state])
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        params.append(max(1, min(limit, 100)) + 1)

        try:
            # Capture the watermark before the page query. A change that commits
            # concurrently will therefore either appear in this page or be returned
            # by inbox_updates; it can never be skipped by an over-new cursor.
            watermark = self.db(
                """
                /* admin_inbox_cursor */
                SELECT version AS server_cursor
                FROM conversation_summary_clock
                WHERE id=1
                """,
                fetchone=True,
            )
            rows = self.db(
                f"""
                /* admin_inbox */
                SELECT
                    s.phone_number,
                    COALESCE(p.name,'') AS name,
                    COALESCE(p.tags,'{{}}') AS tags,
                    COALESCE(p.preferences,'') AS preferences,
                    COALESCE(p.is_paused,FALSE) AS is_paused,
                    s.last_message_id,
                    s.last_message_role,
                    s.last_message,
                    s.last_message_at,
                    s.unread_count,
                    s.first_meaningful_role AS first_message_role,
                    (s.latest_patient_message_id > s.latest_response_message_id) AS needs_reply,
                    s.latest_patient_message_at AS waiting_since,
                    s.is_pinned,
                    s.is_archived,
                    s.change_version,
                    aps.status AS appointment_status,
                    aps.next_appointment,
                    aps.fetched_at AS appointment_fetched_at
                FROM conversation_summaries s
                LEFT JOIN patients p ON p.phone_number=s.phone_number
                LEFT JOIN admin_appointment_snapshots aps ON aps.phone_number=s.phone_number
                {where_sql}
                ORDER BY s.is_pinned DESC, s.last_message_at DESC NULLS LAST,
                         s.last_message_id DESC NULLS LAST, s.phone_number DESC
                LIMIT %s
                """,
                tuple(params),
                fetchall=True,
            )
        except Exception:
            return failure("database_unavailable", "Could not load the inbox.", retryable=True)

        has_more = len(rows) > limit
        patients = sort_inbox_rows(
            [self._prepare_inbox_row(dict(row)) for row in rows[:limit]]
        )
        next_cursor = encode_inbox_cursor(patients[-1]) if has_more and patients else None
        legacy_before_id = (
            int(patients[-1].get("last_message_id") or 0)
            if has_more and patients
            else None
        )
        return success(
            "inbox_loaded",
            {
                "patients": patients,
                "has_more": has_more,
                "next_cursor": next_cursor,
                "next_before_id": legacy_before_id or None,
                "server_cursor": int(
                    (watermark or {}).get("server_cursor") or 0
                ),
            },
        )

    def inbox_updates(self, *, after_version: int, limit: int = 100) -> dict:
        """Return changed summary rows without reconstructing chat history."""
        limit = max(1, min(limit, 200))
        try:
            rows = self.db(
                """
                /* admin_inbox_updates */
                SELECT
                    s.phone_number,
                    COALESCE(p.name,'') AS name,
                    COALESCE(p.tags,'{}') AS tags,
                    COALESCE(p.preferences,'') AS preferences,
                    COALESCE(p.is_paused,FALSE) AS is_paused,
                    s.last_message_id,s.last_message_role,s.last_message,
                    s.last_message_at,s.unread_count,
                    s.first_meaningful_role AS first_message_role,
                    (s.latest_patient_message_id > s.latest_response_message_id) AS needs_reply,
                    s.latest_patient_message_at AS waiting_since,
                    s.is_pinned,s.is_archived,s.change_version,
                    aps.status AS appointment_status,
                    aps.next_appointment,
                    aps.fetched_at AS appointment_fetched_at
                FROM conversation_summaries s
                LEFT JOIN patients p ON p.phone_number=s.phone_number
                LEFT JOIN admin_appointment_snapshots aps ON aps.phone_number=s.phone_number
                WHERE s.change_version>%s
                ORDER BY s.change_version ASC
                LIMIT %s
                """,
                (after_version, limit + 1),
                fetchall=True,
            )
        except Exception:
            return failure(
                "database_unavailable",
                "Could not load inbox updates.",
                retryable=True,
            )
        has_more = len(rows) > limit
        patients = [self._prepare_inbox_row(dict(row)) for row in rows[:limit]]
        cursor = max(
            [int(row.get("change_version") or 0) for row in patients] or [after_version]
        )
        return success(
            "inbox_updates_loaded",
            {"patients": patients, "server_cursor": cursor, "has_more": has_more},
        )

    def mark_read(self, phone_number: str, displayed_message_id: int) -> dict:
        phone = normalize_phone(phone_number)
        if displayed_message_id < 0:
            return failure("invalid_cursor", "Displayed message id must not be negative.", state="degraded")
        try:
            row = self.db(
                """
                /* admin_mark_read */
                WITH clock AS (
                    UPDATE conversation_summary_clock
                    SET version=version+1
                    WHERE id=1
                    RETURNING version
                ), read_state AS (
                    INSERT INTO admin_inbox_state(phone_number,last_read_message_id,updated_at)
                    VALUES (%s,%s,NOW())
                    ON CONFLICT(phone_number) DO UPDATE SET
                        last_read_message_id=GREATEST(
                            admin_inbox_state.last_read_message_id,
                            EXCLUDED.last_read_message_id
                        ),
                        updated_at=NOW()
                    RETURNING last_read_message_id
                ), updated AS (
                    UPDATE conversation_summaries summary
                    SET unread_count=(
                            SELECT COUNT(*)::integer
                            FROM chat_history message, read_state
                            WHERE message.phone_number=summary.phone_number
                              AND message.role='user'
                              AND message.id>read_state.last_read_message_id
                        ),
                        change_version=clock.version,
                        updated_at=NOW()
                    FROM read_state, clock
                    WHERE summary.phone_number=%s
                    RETURNING summary.unread_count
                )
                SELECT read_state.last_read_message_id,
                       COALESCE((SELECT unread_count FROM updated),0) AS unread_count
                FROM read_state
                """,
                (phone, displayed_message_id, phone),
                fetchone=True,
            )
            return success("inbox_marked_read", {
                "phone_number": phone,
                "last_read_message_id": int(row["last_read_message_id"] if row else 0),
                "unread_count": int(row["unread_count"] if row else 0),
            })
        except Exception:
            return failure("database_unavailable", "Could not update unread state.", retryable=True)

    def mark_unread(self, phone_number: str) -> dict:
        """Mark only the latest inbound patient message unread using the same cursor."""
        phone = normalize_phone(phone_number)
        try:
            row = self.db(
                """
                /* admin_mark_unread */
                WITH clock AS (
                    UPDATE conversation_summary_clock
                    SET version=version+1
                    WHERE id=1
                    RETURNING version
                ), latest_user AS (
                    SELECT latest_patient_message_id AS message_id
                    FROM conversation_summaries
                    WHERE phone_number=%s AND latest_patient_message_id>0
                ), read_state AS (
                    INSERT INTO admin_inbox_state(phone_number,last_read_message_id,updated_at)
                    SELECT %s, GREATEST(message_id - 1, 0), NOW()
                    FROM latest_user
                    WHERE TRUE
                    ON CONFLICT(phone_number) DO UPDATE SET
                        last_read_message_id=EXCLUDED.last_read_message_id,
                        updated_at=NOW()
                    RETURNING last_read_message_id
                ), updated AS (
                    UPDATE conversation_summaries
                    SET unread_count=1,
                        change_version=clock.version,
                        updated_at=NOW()
                    FROM clock
                    WHERE phone_number=%s AND EXISTS (SELECT 1 FROM read_state)
                    RETURNING unread_count
                )
                SELECT read_state.last_read_message_id,
                       latest_user.message_id AS marked_unread_message_id
                FROM read_state CROSS JOIN latest_user
                """,
                (phone, phone, phone),
                fetchone=True,
            )
            if not row:
                return success(
                    "no_patient_messages",
                    {"phone_number": phone, "unread_count": 0},
                )
            return success(
                "inbox_marked_unread",
                {
                    "phone_number": phone,
                    "last_read_message_id": int(row["last_read_message_id"]),
                    "marked_unread_message_id": int(row["marked_unread_message_id"]),
                    "unread_count": 1,
                },
            )
        except Exception:
            return failure("database_unavailable", "Could not mark conversation unread.", retryable=True)

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
                    summary.phone_number,
                    COALESCE(p.name,'') AS name,
                    COALESCE(p.tags,'{}') AS tags,
                    COALESCE(p.preferences,'') AS preferences,
                    COALESCE(p.is_paused,FALSE) AS is_paused,
                    p.created_at,
                    summary.last_message_at AS last_active_at,
                    CASE
                        WHEN summary.first_meaningful_role = 'user' THEN 'patient'
                        WHEN summary.first_meaningful_role = 'staff' THEN 'reception'
                        ELSE 'unknown'
                    END AS conversation_origin,
                    summary.unread_count,
                    (summary.latest_patient_message_id > summary.latest_response_message_id)
                        AS needs_reply,
                    summary.latest_patient_message_at AS waiting_since,
                    summary.is_pinned,
                    summary.is_archived,
                    aps.status AS appointment_status,
                    aps.appointments,
                    aps.next_appointment,
                    aps.fetched_at AS appointment_fetched_at
                FROM conversation_summaries summary
                LEFT JOIN patients p ON p.phone_number=summary.phone_number
                LEFT JOIN admin_appointment_snapshots aps
                  ON aps.phone_number=summary.phone_number
                WHERE summary.phone_number=%s
                """,
                (phone,),
                fetchone=True,
            )
            if not row:
                return failure("patient_not_found", "Patient was not found.", state="degraded")
            patient = self._apply_snapshot_state(dict(row))
            classified = self._classify_appointments(patient.get("appointments"))
            if patient["appointment_status"] in {"unavailable", "unknown"}:
                # Retain the raw cached payload for diagnostics, but never expose
                # old items in authoritative upcoming/previous UI sections.
                classified.update(
                    upcoming=[],
                    previous=[],
                    unclassified=[],
                    next_appointment=None,
                )
            patient.update(classified)
            return success("patient_loaded", patient)
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

    def _classify_appointments(self, appointments: Any) -> dict[str, Any]:
        """Classify a cached or live appointment list without guessing malformed times."""
        if not isinstance(appointments, list):
            appointments = []
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
        upcoming.sort(
            key=lambda item: self._appointment_datetime(item)
            or dt.datetime.max.replace(tzinfo=self.config.timezone)
        )
        previous.sort(
            key=lambda item: self._appointment_datetime(item)
            or dt.datetime.min.replace(tzinfo=self.config.timezone),
            reverse=True,
        )
        return {
            "appointments": appointments,
            "upcoming": upcoming,
            "previous": previous,
            "unclassified": unknown,
            "next_appointment": upcoming[0] if upcoming else None,
        }

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
            WITH snapshot AS (
                INSERT INTO admin_appointment_snapshots(
                    phone_number,appointments,next_appointment,status,fetched_at
                ) VALUES (%s,%s::jsonb,%s::jsonb,%s,NOW())
                ON CONFLICT(phone_number) DO UPDATE SET
                    appointments=EXCLUDED.appointments,
                    next_appointment=EXCLUDED.next_appointment,
                    status=EXCLUDED.status,
                    fetched_at=NOW()
                RETURNING phone_number
            )
            INSERT INTO conversation_summaries(phone_number,updated_at)
            SELECT phone_number,NOW() FROM snapshot
            WHERE TRUE
            ON CONFLICT(phone_number) DO UPDATE SET
                change_version=EXCLUDED.change_version,
                updated_at=NOW()
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
            WITH snapshot AS (
                INSERT INTO admin_appointment_snapshots(
                    phone_number,appointments,next_appointment,status,fetched_at
                ) VALUES (%s,NULL,NULL,%s,NULL)
                ON CONFLICT(phone_number) DO UPDATE SET status=EXCLUDED.status
                RETURNING phone_number
            )
            INSERT INTO conversation_summaries(phone_number,updated_at)
            SELECT phone_number,NOW() FROM snapshot
            WHERE TRUE
            ON CONFLICT(phone_number) DO UPDATE SET
                change_version=EXCLUDED.change_version,
                updated_at=NOW()
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
        classified = self._classify_appointments(appointments)
        next_appointment = classified["next_appointment"]
        try:
            self._cache_appointments(phone, appointments, next_appointment, "healthy")
        except Exception:
            # The live authoritative response remains usable even if cache persistence fails.
            pass
        return success(
            "appointments_loaded",
            {
                "phone_number": phone,
                **classified,
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
