import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from receptionist.admin_ops import AdminOperations, build_dashboard_metrics, sort_inbox_rows


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
        self.patient_row = None
        self.message_rows = []
        self.summary_row = {}
        self.analytics_rows = []
        self.fail_health = False
        self.calls = []
        self.last_read_message_id = 0

    def __call__(self, sql, params=(), fetchone=False, fetchall=False, commit=True):
        self.calls.append((sql, params))
        if "admin_mark_read" in sql:
            self.last_read_message_id = max(self.last_read_message_id, int(params[1]))
            return {"last_read_message_id": self.last_read_message_id}
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

    def test_sort_tie_breaks_on_message_id(self):
        rows = [
            {"phone_number": "1", "last_message_id": 10, "last_message_at": NOW},
            {"phone_number": "2", "last_message_id": 11, "last_message_at": NOW},
        ]
        self.assertEqual(sort_inbox_rows(rows)[0]["phone_number"], "2")

    def test_patient_detail_preserves_unknown_appointment_state(self):
        self.db.patient_row = {
            "phone_number": "2010", "name": "Mona", "appointment_status": None,
            "next_appointment": None, "last_active_at": NOW,
        }
        result = self.ops.patient_detail("2010")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["appointment_status"], "unknown")

    def test_mark_read_uses_only_displayed_cursor_during_arrival_race(self):
        # Message 11 represents a patient message arriving after the browser loaded
        # through message 10. The server must never look up and consume that MAX(id).
        newly_arrived_message_id = 11
        result = self.ops.mark_read("2010", 10)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["last_read_message_id"], 10)
        sql, params = self.db.calls[-1]
        self.assertEqual(params, ("2010", 10))
        self.assertNotIn("MAX(id)", sql)
        self.assertIn("GREATEST", sql)
        self.assertGreater(newly_arrived_message_id, result["data"]["last_read_message_id"])

    def test_mark_read_cursor_never_moves_backwards(self):
        self.db.last_read_message_id = 12
        result = self.ops.mark_read("2010", 10)
        self.assertEqual(result["data"]["last_read_message_id"], 12)

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

    def test_recent_snapshot_is_fresh_and_malformed_timestamp_is_unknown(self):
        self.assertEqual(
            self.ops._snapshot_state("healthy", NOW - dt.timedelta(minutes=9)),
            "healthy",
        )
        self.assertEqual(self.ops._snapshot_state("healthy", "not-a-time"), "unknown")

    def test_booked_filter_requires_fresh_healthy_snapshot(self):
        self.ops.inbox(state="booked")
        sql, _ = next(
            (sql, params) for sql, params in self.db.calls if "admin_inbox" in sql
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
