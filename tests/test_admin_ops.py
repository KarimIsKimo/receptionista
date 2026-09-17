import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from receptionist.admin_ops import (
    AdminOperations,
    build_dashboard_metrics,
    classify_conversation_origin,
    decode_inbox_cursor,
    sort_inbox_rows,
)


NOW = dt.datetime(2026, 9, 14, 18, 0, tzinfo=ZoneInfo("Africa/Cairo"))


class FakeBooking:
    def __init__(self):
        self.appointment_result = {"ok": True, "appointments": []}
        self.schedule_payload = {"booked": []}
        self.schedule_error = None

    def appointments(self, phone):
        return self.appointment_result

    def _request(self, method, payload):
        if self.schedule_error:
            raise self.schedule_error
        return self.schedule_payload


class FakeDB:
    def __init__(self):
        self.inbox_rows = []
        self.metadata_rows = []
        self.update_rows = []
        self.patient_row = None
        self.message_rows = []
        self.summary_row = {}
        self.analytics_rows = []
        self.fail_health = False
        self.calls = []
        self.last_read_message_id = 0
        self.latest_user_message_id = 0
        self.server_cursor = 0

    def __call__(self, sql, params=(), fetchone=False, fetchall=False, commit=True):
        self.calls.append((sql, params))
        if "admin_mark_read" in sql:
            self.last_read_message_id = max(self.last_read_message_id, int(params[1]))
            return {
                "last_read_message_id": self.last_read_message_id,
                "unread_count": int(
                    self.latest_user_message_id > self.last_read_message_id
                ),
            }
        if "admin_mark_unread" in sql:
            if not self.latest_user_message_id:
                return None
            self.last_read_message_id = max(self.latest_user_message_id - 1, 0)
            return {
                "last_read_message_id": self.last_read_message_id,
                "marked_unread_message_id": self.latest_user_message_id,
            }
        if "admin_inbox_metadata" in sql:
            return self.metadata_rows
        if "admin_inbox_updates" in sql:
            return self.update_rows
        if "admin_inbox_cursor" in sql:
            return {"server_cursor": self.server_cursor}
        if "admin_inbox" in sql:
            return self.inbox_rows
        if "admin_patient_detail" in sql:
            return self.patient_row
        if "admin_messages" in sql:
            return self.message_rows
        if "admin_dashboard_summary" in sql:
            return self.summary_row
        if "admin_analytics" in sql:
            return self.analytics_rows
        if "admin_cache_appointments" in sql:
            return None
        if "admin_health_db" in sql:
            if self.fail_health:
                raise RuntimeError("down")
            return {"ok": 1}
        raise AssertionError(f"Unexpected SQL: {sql}")


class PaginatedInboxDB:
    """In-memory model of the composite keyset query used by the inbox."""

    def __init__(self, rows):
        self.rows = rows
        self.update_rows = []
        self.server_cursor = 500
        self.calls = []

    @staticmethod
    def key(row):
        return (
            bool(row.get("is_pinned")),
            row.get("last_message_at")
            or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            int(row.get("last_message_id") or 0),
            str(row.get("phone_number") or ""),
        )

    def __call__(self, sql, params=(), fetchone=False, fetchall=False, commit=True):
        self.calls.append((sql, params))
        if "admin_inbox_cursor" in sql:
            return {"server_cursor": self.server_cursor}
        if "admin_inbox_updates" in sql:
            return [dict(row) for row in self.update_rows[: params[-1]]]
        if "admin_inbox" not in sql:
            raise AssertionError(sql)

        rows = sort_inbox_rows([dict(row) for row in self.rows])
        if sql.count(
            "s.latest_patient_message_id > s.latest_response_message_id"
        ) > 1:
            rows = [
                row
                for row in rows
                if int(row.get("latest_patient_message_id") or 0)
                > int(row.get("latest_response_message_id") or 0)
            ]
        if "-infinity'::timestamptz" in sql:
            pinned, timestamp, message_id, phone = params[:4]
            moment = (
                dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if timestamp is not None
                else dt.datetime.min.replace(tzinfo=dt.timezone.utc)
            )
            cursor_key = (pinned, moment, message_id, phone)
            rows = [row for row in rows if self.key(row) < cursor_key]
        return rows[: params[-1]]


class AdminOperationsTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.booking = FakeBooking()
        self.ops = AdminOperations(self.db, self.booking, now_func=lambda: NOW)

    def test_inbox_sorting_uses_latest_actual_message(self):
        older = NOW - dt.timedelta(minutes=5)
        newer = NOW - dt.timedelta(minutes=1)
        self.db.inbox_rows = [
            {"phone_number": "1", "last_message_id": 10, "last_message_at": older},
            {"phone_number": "2", "last_message_id": 11, "last_message_at": newer},
        ]
        result = self.ops.inbox()
        self.assertTrue(result["ok"])
        self.assertEqual([x["phone_number"] for x in result["data"]["patients"]], ["2", "1"])

    def test_initial_inbox_cursor_uses_prequery_global_watermark(self):
        self.db.server_cursor = 87
        result = self.ops.inbox(state="booked")
        self.assertEqual(result["data"]["patients"], [])
        self.assertEqual(result["data"]["server_cursor"], 87)
        self.assertIn("admin_inbox_cursor", self.db.calls[-2][0])
        self.assertIn("FROM conversation_summary_clock", self.db.calls[-2][0])
        self.assertNotIn("conversation_summary_change_seq", self.db.calls[-2][0])
        self.assertIn("admin_inbox", self.db.calls[-1][0])

    def test_sort_tie_breaks_on_message_id(self):
        rows = [
            {"phone_number": "1", "last_message_id": 10, "last_message_at": NOW},
            {"phone_number": "2", "last_message_id": 11, "last_message_at": NOW},
        ]
        self.assertEqual(sort_inbox_rows(rows)[0]["phone_number"], "2")

    def test_pinned_conversations_sort_before_newer_unpinned_rows(self):
        rows = [
            {"phone_number": "new", "last_message_id": 11, "last_message_at": NOW},
            {
                "phone_number": "pinned",
                "last_message_id": 10,
                "last_message_at": NOW - dt.timedelta(days=1),
                "is_pinned": True,
            },
        ]
        self.assertEqual(sort_inbox_rows(rows)[0]["phone_number"], "pinned")

    def test_patient_origin_uses_earliest_meaningful_role(self):
        self.db.inbox_rows = [{
            "phone_number": "1", "last_message_id": 10,
            "last_message_at": NOW, "first_message_role": "user",
        }]
        patient = self.ops.inbox()["data"]["patients"][0]
        self.assertEqual(patient["conversation_origin"], "patient")
        self.assertEqual(classify_conversation_origin("user"), "patient")

    def test_reception_origin_uses_earliest_meaningful_role(self):
        self.db.inbox_rows = [{
            "phone_number": "1", "last_message_id": 10,
            "last_message_at": NOW, "first_message_role": "staff",
        }]
        patient = self.ops.inbox()["data"]["patients"][0]
        self.assertEqual(patient["conversation_origin"], "reception")
        self.assertEqual(classify_conversation_origin("staff"), "reception")

    def test_unknown_origin_when_no_meaningful_message_exists(self):
        self.db.inbox_rows = [{
            "phone_number": "1", "last_message_id": 10,
            "last_message_at": NOW, "first_message_role": None,
        }]
        patient = self.ops.inbox()["data"]["patients"][0]
        self.assertEqual(patient["conversation_origin"], "unknown")
        self.assertEqual(classify_conversation_origin("model"), "unknown")

    def test_origin_filters_use_earliest_user_or_staff_message(self):
        self.ops.inbox(state="patient_initiated")
        patient_sql, _ = self.db.calls[-1]
        self.assertIn("s.first_meaningful_role = 'user'", patient_sql)
        self.assertIn("FROM conversation_summaries s", patient_sql)
        self.assertNotIn("chat_history", patient_sql)
        self.ops.inbox(state="reception_initiated")
        reception_sql, _ = self.db.calls[-1]
        self.assertIn("s.first_meaningful_role = 'staff'", reception_sql)
        self.assertNotIn("chat_history", reception_sql)

    def test_incremental_inbox_does_not_reconstruct_message_history(self):
        self.db.update_rows = [{
            "phone_number": "2010", "change_version": 42,
            "first_message_role": "user",
        }]
        result = self.ops.inbox_updates(after_version=40)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["server_cursor"], 42)
        sql, params = self.db.calls[-1]
        self.assertIn("conversation_summaries", sql)
        self.assertNotIn("chat_history", sql)
        self.assertEqual(params, (40, 101))

    def test_reopened_archived_conversation_is_exposed_incrementally(self):
        self.db.update_rows = [{
            "phone_number": "2010",
            "last_message_id": 10,
            "last_message_role": "user",
            "last_message": "hello again",
            "last_message_at": NOW,
            "latest_patient_message_id": 10,
            "latest_response_message_id": 9,
            "unread_count": 1,
            "is_archived": False,
            "needs_reply": True,
            "change_version": 42,
            "first_message_role": "user",
        }]

        result = self.ops.inbox_updates(after_version=41)

        self.assertTrue(result["ok"])
        patient = result["data"]["patients"][0]
        self.assertFalse(patient["is_archived"])
        self.assertTrue(patient["needs_reply"])
        self.assertEqual(patient["unread_count"], 1)
        sql, _ = self.db.calls[-1]
        self.assertNotIn("s.is_archived = FALSE", sql)

    def test_composite_cursor_pages_pinned_and_recent_rows_without_gaps(self):
        rows = [{
            "phone_number": "pinned-old",
            "last_message_id": 100,
            "last_message_at": NOW - dt.timedelta(days=30),
            "last_message_role": "staff",
            "is_pinned": True,
            "is_archived": False,
            "change_version": 1,
        }]
        rows.extend({
            "phone_number": f"recent-{index:02d}",
            "last_message_id": 1000 + index,
            "last_message_at": NOW - dt.timedelta(minutes=index),
            "last_message_role": "user",
            "is_pinned": False,
            "is_archived": False,
            "change_version": index + 1,
        } for index in range(60))
        db = PaginatedInboxDB(rows)
        ops = AdminOperations(db, self.booking, now_func=lambda: NOW)

        first = ops.inbox(limit=50)
        cursor = first["data"]["next_cursor"]
        second = ops.inbox(limit=50, before=cursor)
        phones = [
            row["phone_number"]
            for page in (first, second)
            for row in page["data"]["patients"]
        ]

        self.assertEqual(phones[0], "pinned-old")
        self.assertEqual(len(phones), 61)
        self.assertEqual(len(set(phones)), 61)
        self.assertEqual(set(phones), {row["phone_number"] for row in rows})
        self.assertFalse(second["data"]["has_more"])
        pinned, _, _, _ = decode_inbox_cursor(cursor)
        self.assertFalse(pinned)
        second_sql, _ = next(
            (sql, params)
            for sql, params in reversed(db.calls)
            if "/* admin_inbox */" in sql
        )
        self.assertIn("s.is_pinned", second_sql)
        self.assertIn("s.last_message_at", second_sql)
        self.assertIn("s.last_message_id", second_sql)
        self.assertIn("s.phone_number", second_sql)

    def test_pin_changes_between_pages_are_deduplicated_via_incremental_rows(self):
        rows = [{
            "phone_number": "pinned-old",
            "last_message_id": 100,
            "last_message_at": NOW - dt.timedelta(days=30),
            "last_message_role": "staff",
            "is_pinned": True,
            "is_archived": False,
            "change_version": 1,
        }]
        rows.extend({
            "phone_number": f"recent-{index:02d}",
            "last_message_id": 1000 + index,
            "last_message_at": NOW - dt.timedelta(minutes=index),
            "last_message_role": "user",
            "is_pinned": False,
            "is_archived": False,
            "change_version": index + 1,
        } for index in range(60))
        db = PaginatedInboxDB(rows)
        ops = AdminOperations(db, self.booking, now_func=lambda: NOW)
        first = ops.inbox(limit=50)
        cursor = first["data"]["next_cursor"]
        first_phones = {
            row["phone_number"] for row in first["data"]["patients"]
        }

        old_pinned = next(row for row in rows if row["phone_number"] == "pinned-old")
        newly_pinned = next(
            row for row in rows if row["phone_number"] not in first_phones
        )
        old_pinned["is_pinned"] = False
        old_pinned["change_version"] = 600
        newly_pinned["is_pinned"] = True
        newly_pinned["change_version"] = 601
        db.update_rows = [old_pinned, newly_pinned]
        updates = ops.inbox_updates(after_version=500)
        second = ops.inbox(limit=50, before=cursor)

        client_rows = {}
        for response in (first, updates, second):
            for row in response["data"]["patients"]:
                client_rows[row["phone_number"]] = row

        self.assertEqual(set(client_rows), {row["phone_number"] for row in rows})
        self.assertEqual(len(client_rows), 61)
        self.assertFalse(client_rows["pinned-old"]["is_pinned"])
        self.assertTrue(client_rows[newly_pinned["phone_number"]]["is_pinned"])

    def test_resolved_first_needs_reply_page_keeps_older_page_reachable(self):
        rows = [{
            "phone_number": f"needs-reply-{index:02d}",
            "last_message_id": 2000 + index,
            "last_message_at": NOW - dt.timedelta(minutes=index),
            "last_message_role": "user",
            "latest_patient_message_id": 2000 + index,
            "latest_response_message_id": 0,
            "needs_reply": True,
            "is_pinned": False,
            "is_archived": False,
            "change_version": index + 1,
        } for index in range(61)]
        db = PaginatedInboxDB(rows)
        ops = AdminOperations(db, self.booking, now_func=lambda: NOW)

        first = ops.inbox(state="needs_reply", limit=50)
        self.assertEqual(len(first["data"]["patients"]), 50)
        self.assertIsNotNone(first["data"]["next_cursor"])

        first_phones = {
            row["phone_number"] for row in first["data"]["patients"]
        }
        resolved_updates = []
        for row in rows:
            if row["phone_number"] in first_phones:
                row["latest_response_message_id"] = row["latest_patient_message_id"]
                row["needs_reply"] = False
                row["change_version"] += 1000
                resolved_updates.append(row)
        db.update_rows = resolved_updates
        updates = ops.inbox_updates(after_version=500, limit=100)

        client_rows = {
            row["phone_number"]: row for row in first["data"]["patients"]
        }
        for row in updates["data"]["patients"]:
            client_rows[row["phone_number"]] = row
        self.assertEqual(
            [row for row in client_rows.values() if row.get("needs_reply")],
            [],
        )

        second = ops.inbox(
            state="needs_reply",
            limit=50,
            before=first["data"]["next_cursor"],
        )
        second_phones = [
            row["phone_number"] for row in second["data"]["patients"]
        ]
        self.assertEqual(len(second_phones), 11)
        self.assertTrue(first_phones.isdisjoint(second_phones))
        self.assertEqual(len(set(second_phones)), 11)
        self.assertFalse(second["data"]["has_more"])
        self.assertIsNone(second["data"]["next_cursor"])

        for row in rows:
            row["latest_response_message_id"] = row["latest_patient_message_id"]
        exhausted = ops.inbox(state="needs_reply", limit=50)
        self.assertEqual(exhausted["data"]["patients"], [])
        self.assertFalse(exhausted["data"]["has_more"])
        self.assertIsNone(exhausted["data"]["next_cursor"])

    def test_malformed_composite_cursor_is_rejected(self):
        for cursor in ("not-a-valid-cursor", "%%%%"):
            with self.subTest(cursor=cursor):
                result = self.ops.inbox(before=cursor)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "invalid_cursor")

    def test_needs_reply_filter_is_distinct_from_unread(self):
        self.ops.inbox(state="needs_reply")
        needs_sql, _ = self.db.calls[-1]
        self.assertIn(
            "s.latest_patient_message_id > s.latest_response_message_id",
            needs_sql,
        )
        self.ops.inbox(state="unread")
        unread_sql, _ = self.db.calls[-1]
        self.assertIn("s.unread_count > 0", unread_sql)

    def test_patient_detail_preserves_unknown_appointment_state(self):
        self.db.patient_row = {
            "phone_number": "2010", "name": "Mona", "appointment_status": None,
            "next_appointment": None, "last_active_at": NOW,
        }
        result = self.ops.patient_detail("2010")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["appointment_status"], "unknown")

    def test_patient_detail_renders_fresh_cached_appointments_without_live_call(self):
        self.db.patient_row = {
            "phone_number": "2010",
            "name": "Mona",
            "appointment_status": "healthy",
            "appointment_fetched_at": NOW - dt.timedelta(minutes=2),
            "appointments": [
                {"date": "2026-09-14", "time": "5:00 PM"},
                {"date": "2026-09-14", "time": "7:00 PM"},
            ],
        }
        result = self.ops.patient_detail("2010")
        self.assertEqual(len(result["data"]["previous"]), 1)
        self.assertEqual(len(result["data"]["upcoming"]), 1)
        self.assertEqual(result["data"]["next_appointment"]["time"], "7:00 PM")
        self.assertEqual(self.booking.appointment_result, {"ok": True, "appointments": []})

    def test_unavailable_cached_snapshot_is_not_presented_as_upcoming(self):
        self.db.patient_row = {
            "phone_number": "2010",
            "appointment_status": "unavailable",
            "appointment_fetched_at": NOW - dt.timedelta(minutes=2),
            "appointments": [{"date": "2026-09-15", "time": "7:00 PM"}],
        }
        result = self.ops.patient_detail("2010")
        self.assertEqual(result["data"]["appointment_status"], "unavailable")
        self.assertEqual(result["data"]["upcoming"], [])
        self.assertIsNone(result["data"]["next_appointment"])

    def test_mark_read_uses_only_displayed_cursor_during_arrival_race(self):
        # Message 11 represents a patient message arriving after the browser loaded
        # through message 10. The server must never look up and consume that MAX(id).
        newly_arrived_message_id = 11
        self.db.latest_user_message_id = newly_arrived_message_id
        result = self.ops.mark_read("2010", 10)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["last_read_message_id"], 10)
        sql, params = self.db.calls[-1]
        self.assertEqual(params, ("2010", 10, "2010"))
        self.assertNotIn("MAX(id)", sql)
        self.assertIn("GREATEST", sql)
        self.assertIn("UPDATE conversation_summary_clock", sql)
        self.assertIn("change_version=clock.version", sql)
        self.assertGreater(newly_arrived_message_id, result["data"]["last_read_message_id"])
        self.assertEqual(result["data"]["unread_count"], 1)

    def test_mark_read_cursor_never_moves_backwards(self):
        self.db.last_read_message_id = 12
        result = self.ops.mark_read("2010", 10)
        self.assertEqual(result["data"]["last_read_message_id"], 12)

    def test_mark_unread_moves_cursor_only_before_latest_patient_message(self):
        self.db.last_read_message_id = 20
        self.db.latest_user_message_id = 17
        result = self.ops.mark_unread("2010")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["last_read_message_id"], 16)
        self.assertEqual(result["data"]["marked_unread_message_id"], 17)
        self.assertEqual(result["data"]["unread_count"], 1)
        sql, _ = self.db.calls[-1]
        self.assertIn("UPDATE conversation_summary_clock", sql)
        self.assertIn("change_version=clock.version", sql)
        sql, params = self.db.calls[-1]
        self.assertEqual(params, ("2010", "2010", "2010"))
        self.assertNotIn("MAX(id)", sql)
        self.assertIn("latest_patient_message_id", sql)
        self.assertNotIn("GREATEST(\n                        admin_inbox_state.last_read_message_id", sql)

    def test_mark_unread_without_patient_messages_is_a_noop(self):
        result = self.ops.mark_unread("2010")
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "no_patient_messages")
        self.assertEqual(result["data"]["unread_count"], 0)

    def test_message_pagination_reverses_database_page(self):
        self.db.message_rows = [
            {"id": 5, "role": "user"}, {"id": 4, "role": "model"}, {"id": 3, "role": "staff"}
        ]
        result = self.ops.messages("2010", limit=2)
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["has_more"])
        self.assertEqual([x["id"] for x in result["data"]["messages"]], [4, 5])

    def test_analytics_counts_and_conversion(self):
        metrics = build_dashboard_metrics({
            "total_patients": 20,
            "conversations_today": 8,
            "unique_incoming_patients_today": 4,
            "incoming_messages_today": 10,
            "ai_messages_today": 8,
            "staff_messages_today": 2,
            "human_takeover_count": 3,
            "bookings_today": 2,
            "cancellations_today": 1,
        })
        self.assertIsNone(metrics["booking_conversion_rate"]["value"])
        self.assertEqual(metrics["booking_conversion_rate"]["status"], "unknown")
        self.assertIsNone(metrics["upcoming_appointments"]["value"])

    def test_schedule_failure_is_not_empty_availability(self):
        self.booking.schedule_error = RuntimeError("down")
        result = self.ops.schedule("2026-09-15")
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["state"], "unavailable")
        self.assertEqual(result["data"]["slots"], [])

    def test_schedule_preserves_authoritative_booked_fields(self):
        appointment = {
            "time": "7:00 PM", "patient_name": "Mona", "phone": "2010", "area": "underarm"
        }
        self.booking.schedule_payload = {"booked": [appointment]}
        result = self.ops.schedule("2026-09-15")
        slot = next(x for x in result["data"]["slots"] if x["time"] == "7:00 PM")
        self.assertEqual(slot["status"], "booked")
        self.assertEqual(slot["appointment"], appointment)

    def test_malformed_schedule_response_is_error(self):
        for malformed in ({"booked": {}}, {"booked": [{"name": "missing time"}]}, {"booked": 3}):
            with self.subTest(malformed=malformed):
                self.booking.schedule_payload = malformed
                result = self.ops.schedule("2026-09-15")
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "scheduling_unavailable")

    def test_appointment_result_classifies_cairo_date_and_time(self):
        self.booking.appointment_result = {
            "ok": True,
            "appointments": [
                {"date": "2026-09-14", "time": "5:30 PM", "case": "earlier_today"},
                {"date": "2026-09-14", "time": "7:00 PM", "case": "later_today"},
                {"date": "2026-09-15", "time": "8:00 PM", "case": "tomorrow"},
                {"date": "2026-09-15", "time": "soon", "case": "malformed"},
            ],
        }
        result = self.ops.patient_appointments("2010")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["previous"]), 1)
        self.assertEqual(
            [x["case"] for x in result["data"]["upcoming"]],
            ["later_today", "tomorrow"],
        )
        self.assertEqual(result["data"]["unclassified"][0]["case"], "malformed")
        self.assertEqual(result["data"]["next_appointment"]["case"], "later_today")

    def test_current_apps_script_history_strings_are_time_classified(self):
        self.booking.appointment_result = {
            "ok": True,
            "appointments": [
                "2026-09-14 الساعة 5:30 PM لمنطقة A",
                "2026-09-14 الساعة 7:00 PM لمنطقة B",
                "2026-09-15 الساعة 8:00 PM لمنطقة C",
                "2026-09-15 بدون وقت صالح",
            ],
        }
        result = self.ops.patient_appointments("2010")
        self.assertEqual(result["data"]["previous"], [
            "2026-09-14 الساعة 5:30 PM لمنطقة A"
        ])
        self.assertEqual(len(result["data"]["upcoming"]), 2)
        self.assertEqual(result["data"]["next_appointment"],
                         "2026-09-14 الساعة 7:00 PM لمنطقة B")
        self.assertEqual(result["data"]["unclassified"], [
            "2026-09-15 بدون وقت صالح"
        ])

    def test_snapshot_freshness_expires_after_ten_minutes(self):
        self.db.inbox_rows = [
            {
                "phone_number": "2010",
                "last_message_id": 10,
                "last_message_at": NOW,
                "appointment_status": "healthy",
                "appointment_fetched_at": NOW - dt.timedelta(minutes=11),
                "next_appointment": {"date": "2026-09-15", "time": "7:00 PM"},
            }
        ]
        result = self.ops.inbox()
        patient = result["data"]["patients"][0]
        self.assertEqual(patient["appointment_status"], "stale")
        self.assertEqual(patient["appointment_freshness"], "stale")

    def test_metadata_refresh_ages_snapshot_without_new_messages(self):
        self.db.metadata_rows = [
            {
                "phone_number": "2010",
                "appointment_status": "healthy",
                "appointment_fetched_at": NOW - dt.timedelta(minutes=11),
                "next_appointment": {"date": "2026-09-15", "time": "7:00 PM"},
            }
        ]
        result = self.ops.inbox_metadata(["2010"])
        self.assertTrue(result["ok"])
        patient = result["data"]["patients"][0]
        self.assertEqual(patient["appointment_status"], "stale")
        sql, params = next(
            (sql, params)
            for sql, params in self.db.calls
            if "admin_inbox_metadata" in sql
        )
        self.assertNotIn("chat_history", sql)
        self.assertEqual(params, (["2010"],))

    def test_recent_snapshot_is_fresh_and_malformed_timestamp_is_unknown(self):
        self.assertEqual(
            self.ops._snapshot_state("healthy", NOW - dt.timedelta(minutes=9)),
            "healthy",
        )
        self.assertEqual(self.ops._snapshot_state("healthy", "not-a-time"), "unknown")
        self.assertEqual(
            self.ops._snapshot_state("healthy", NOW - dt.timedelta(minutes=10)),
            "stale",
        )

    def test_booked_filter_requires_fresh_healthy_snapshot(self):
        self.ops.inbox(state="booked")
        sql, _ = next(
            (sql, params)
            for sql, params in self.db.calls
            if "/* admin_inbox */" in sql
        )
        self.assertIn("aps.status = 'healthy'", sql)
        self.assertIn("INTERVAL '10 minutes'", sql)

    def test_failed_refresh_marks_snapshot_unavailable_without_replacing_data(self):
        self.booking.appointment_result = {
            "ok": False,
            "message": "temporary",
        }
        result = self.ops.patient_appointments("2010")
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["snapshot_status"], "unavailable")
        sql, params = next(
            (sql, params)
            for sql, params in self.db.calls
            if "admin_cache_appointments_failure" in sql
        )
        self.assertIn("DO UPDATE SET status", sql)
        self.assertNotIn("fetched_at=", sql)
        self.assertEqual(params, ("2010", "unavailable"))

    def test_malformed_appointment_result_is_error(self):
        self.booking.appointment_result = {"ok": True, "appointments": {}}
        result = self.ops.patient_appointments("2010")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "malformed_scheduling_response")

    def test_analytics_uses_cairo_day_at_utc_boundary(self):
        boundary = dt.datetime(2026, 9, 14, 22, 30, tzinfo=dt.timezone.utc)
        ops = AdminOperations(self.db, self.booking, now_func=lambda: boundary)
        result = ops.analytics(14)
        self.assertTrue(result["ok"])
        sql, params = next(
            (sql, params) for sql, params in self.db.calls if "admin_analytics" in sql
        )
        self.assertNotIn("CURRENT_DATE", sql)
        self.assertEqual(params, (dt.date(2026, 9, 15), 14, dt.date(2026, 9, 15)))

    def test_health_distinguishes_unknown_and_unavailable(self):
        self.db.fail_health = True
        self.booking.schedule_error = RuntimeError("down")
        result = self.ops.system_health(
            global_bot_active=True,
            gemini_configured=True,
            gemini_initialized=True,
            whatsapp_configured=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["components"]["database"]["status"], "unavailable")
        self.assertEqual(result["data"]["components"]["scheduling"]["status"], "unavailable")
        self.assertEqual(result["data"]["components"]["gemini"]["status"], "unknown")
        self.assertEqual(result["data"]["components"]["whatsapp"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
