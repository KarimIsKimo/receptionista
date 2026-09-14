import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from receptionist.booking import BookingService

NOW = dt.datetime(2026, 9, 14, 18, 0, tzinfo=ZoneInfo("Africa/Cairo"))


class FakeAppsScript:
    def __init__(self, booked=None):
        self.booked = booked or []
        self.posts = []
        self.fail = False

    def get(self, params):
        if self.fail:
            raise RuntimeError("temporary")
        if "phone" in params:
            return {"appointments": []}
        return {"booked": self.booked}

    def post(self, payload):
        self.posts.append(payload)
        if payload["action"] == "cancel":
            return {"deleted": True}
        if payload["action"] == "reschedule":
            return {"success": True, "rescheduled": True}
        return {"status": "success"}


class BookingTests(unittest.TestCase):
    def service(self, fake):
        return BookingService("https://example.invalid", get_func=fake.get, post_func=fake.post, now_func=lambda: NOW)

    def test_rejects_past_date_without_calling_google(self):
        fake = FakeAppsScript()
        answer = self.service(fake).book("Mona", "01012345678", "2026-09-13", "7:00 PM", "underarm")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["code"], "invalid_appointment")
        self.assertEqual(fake.posts, [])

    def test_rejects_elapsed_slot_today(self):
        answer = self.service(FakeAppsScript()).book("Mona", "01012345678", "today", "5:30 PM", "underarm")
        self.assertFalse(answer["ok"])
        self.assertIn("بدأ", answer["message"])

    def test_friday_is_closed(self):
        answer = self.service(FakeAppsScript()).book("Mona", "01012345678", "2026-09-18", "7:00 PM", "underarm")
        self.assertFalse(answer["ok"])
        self.assertIn("الجمعة", answer["message"])

    def test_book_rechecks_and_rejects_taken_slot(self):
        fake = FakeAppsScript(booked=[{"time": "7:00 PM"}])
        answer = self.service(fake).book("Mona", "01012345678", "tomorrow", "19:00", "underarm")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["code"], "slot_unavailable")
        self.assertEqual(fake.posts, [])

    def test_success_returns_structured_appointment(self):
        fake = FakeAppsScript()
        answer = self.service(fake).book("Mona", "01012345678", "tomorrow", "19:00", "underarm")
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["code"], "booked")
        self.assertEqual(answer["appointment"]["phone_number"], "201012345678")
        self.assertEqual(fake.posts[0]["date"], "2026-09-15")

    def test_temporary_google_failure_never_confirms_booking(self):
        fake = FakeAppsScript()
        fake.fail = True
        answer = self.service(fake).book("Mona", "01012345678", "tomorrow", "7:00 PM", "underarm")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["code"], "booking_service_unavailable")
        self.assertTrue(answer["retryable"])
        self.assertEqual(fake.posts, [])

    def test_empty_or_ambiguous_response_never_confirms_booking(self):
        responses = ({}, {"message": "maybe"}, {"status": "pending"})
        for response in responses:
            with self.subTest(response=response):
                fake = FakeAppsScript()
                fake.post = lambda payload, response=response: response
                answer = self.service(fake).book(
                    "Mona", "01012345678", "tomorrow", "7:00 PM", "underarm"
                )
                self.assertFalse(answer["ok"])
                self.assertEqual(answer["code"], "booking_not_confirmed")
                self.assertTrue(answer["retryable"])

    def test_valid_future_slot_later_today_is_confirmed(self):
        fake = FakeAppsScript()
        answer = self.service(fake).book(
            "Mona", "01012345678", "today", "7:00 PM", "underarm"
        )
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["code"], "booked")
        self.assertEqual(answer["appointment"]["date"], "2026-09-14")
        self.assertEqual(answer["appointment"]["time"], "7:00 PM")

    def test_malformed_apps_script_responses_never_confirm(self):
        malformed_cases = (
            (lambda params: [], lambda payload: {"status": "success"}),
            (lambda params: {"booked": "7:00 PM"}, lambda payload: {"status": "success"}),
            (lambda params: {"booked": []}, lambda payload: []),
        )
        for get_func, post_func in malformed_cases:
            with self.subTest(get_func=get_func, post_func=post_func):
                service = BookingService(
                    "https://example.invalid",
                    get_func=get_func,
                    post_func=post_func,
                    now_func=lambda: NOW,
                )
                answer = service.book(
                    "Mona", "01012345678", "tomorrow", "7:00 PM", "underarm"
                )
                self.assertFalse(answer["ok"])
                self.assertEqual(answer["code"], "booking_service_unavailable")
                self.assertTrue(answer["retryable"])

    def test_cancel_returns_structured_result(self):
        answer = self.service(FakeAppsScript()).cancel("01012345678", "Thursday")
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["code"], "cancelled")

    def test_reschedule_preserves_old_booking_unless_confirmed(self):
        fake = FakeAppsScript()
        answer = self.service(fake).reschedule("01012345678", "2026-09-15", "2026-09-16", "8:00 PM")
        self.assertTrue(answer["ok"])
        self.assertEqual(fake.posts[0]["action"], "reschedule")


if __name__ == "__main__":
    unittest.main()
