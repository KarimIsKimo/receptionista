import asyncio
import contextlib
import os
import pathlib
import threading
import unittest
from unittest import mock

from receptionist.supervisor import (
    PassiveAppointmentSupervisor,
    eligible_confirmation_actions,
    normalize_mode,
    patient_facing_ai_allowed,
    supervisor_summary,
    validate_inference,
)

os.environ.setdefault("DATABASE_URL", "postgresql://example.invalid/test")
os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test-whatsapp-token")
os.environ.setdefault("APPS_SCRIPT_URL", "https://example.invalid/apps-script")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
with mock.patch("google.genai.Client"):
    import main


PHONE = "201000000001"


def row(message_id, role, content):
    return {"id": message_id, "role": role, "content": content, "created_at": "2026-09-20T10:00:00Z"}


def inferred(action="book", **changes):
    value = {
        "action": action,
        "confidence": "high",
        "patient_name": "Mona",
        "date": "2026-09-24",
        "time": "8:00 PM",
        "area": "Laser",
        "old_date": "",
        "old_time": "",
        "appointment_id": "",
        "evidence_message_ids": [1, 2],
    }
    value.update(changes)
    return value


class FakeBooking:
    def __init__(self, result=None):
        self.result = result or {"ok": True, "code": "booked"}
        self.calls = []

    def book(self, *args):
        self.calls.append(("book", args)); return dict(self.result)

    def cancel(self, *args):
        self.calls.append(("cancel", args)); return dict(self.result)

    def reschedule(self, *args):
        self.calls.append(("reschedule", args)); return dict(self.result)


class MemorySupervisor(PassiveAppointmentSupervisor):
    """Exercise orchestration without pretending external SQL is in-memory safe."""

    def __init__(self, rows, model_result, booking=None, *, finalize_failure=False, barrier=None):
        self.rows = rows
        self.model_result = model_result
        self.booking = booking or FakeBooking()
        self.events = {}
        self.attention = []
        self.completed = []
        self.system_events = []
        self.refreshes = []
        self.lock = threading.Lock()
        self.finalize_failure = finalize_failure
        self.barrier = barrier
        self.infer_calls = 0
        self.get_mode = lambda: "HUMAN"

    def _context(self, phone, trigger_id): return self.rows

    def infer(self, phone, rows):
        self.infer_calls += 1
        if self.barrier:
            self.barrier.wait(timeout=2)
        return dict(self.model_result)

    def _complete_trigger(self, message_id, error=""): self.completed.append((message_id, error))

    def _reserve_event(self, key, trigger, inference):
        with self.lock:
            existing = self.events.get(key)
            if existing:
                return False, dict(existing)
            self.events[key] = {"status": "inferred", "side_effect_started_at": None}
            return True, dict(self.events[key])

    def _begin_side_effect(self, key):
        with self.lock:
            event = self.events[key]
            if event["status"] != "inferred" or event["side_effect_started_at"]:
                return False
            event.update(status="processing", side_effect_started_at="durable")
            return True

    def _mark_attention_event(self, key, phone, status, code, kind, title, inference):
        with self.lock:
            self.events[key].update(status=status, result_code=code)
            self.attention.append((key, kind, code))

    def _create_attention(self, key, phone, kind, title, details, **kwargs):
        self.attention.append((key, kind, details.get("action") or details.get("trigger_message_id")))
        return True

    def _execute(self, phone, inference):
        return super()._execute(phone, inference)

    def db(self, sql, params=(), **kwargs):
        if "SELECT name FROM patients" in sql:
            return {"name": "Mona"}
        return None

    def refresh_snapshot(self, phone):
        self.refreshes.append(phone)
        return {"ok": True}

    def _finalize_success(self, key, phone, inference, result_code):
        if self.finalize_failure:
            raise RuntimeError("local commit failed")
        with self.lock:
            self.events[key]["status"] = "succeeded"
            self.system_events.append((phone, self._system_text(inference), key))


class InferenceSafetyTests(unittest.TestCase):
    def test_irrelevant_staff_messages_are_not_eligible_for_inference(self):
        for text in (
            "شكراً",
            "تمام",
            "لحظة واحدة",
            "هسأل الدكتورة",
            "سعر الجلسة 500 جنيه ومتاح باكدج كمان",
        ):
            with self.subTest(text=text):
                self.assertEqual(eligible_confirmation_actions(text), set())

    def test_explicit_completion_messages_remain_eligible(self):
        cases = {
            "حجزتلك الخميس الساعة ٨": "book",
            "تم إلغاء الموعد": "cancel",
            "غيرتلك الموعد للسبت الساعة ٧": "reschedule",
            "You're booked Thursday at 8": "book",
            "I've rescheduled your appointment": "reschedule",
        }
        for text, action in cases.items():
            with self.subTest(text=text):
                self.assertIn(action, eligible_confirmation_actions(text))

    def test_modes_are_fail_safe_and_separate_patient_reply_permission(self):
        self.assertEqual(normalize_mode(None), "HUMAN")
        self.assertFalse(patient_facing_ai_allowed("HUMAN", True))
        self.assertFalse(patient_facing_ai_allowed("AI_BACKUP", True))
        self.assertFalse(patient_facing_ai_allowed("AI_ACTIVE", False))
        self.assertTrue(patient_facing_ai_allowed("AI_ACTIVE", True))

    def test_patient_request_never_becomes_completed_booking(self):
        rows = [row(1, "user", "Can I come Thursday at 8?")]
        _, state = validate_inference(inferred(evidence_message_ids=[1]), rows, 1)
        self.assertEqual(state, "uncertain")

    def test_discussion_language_does_not_count_as_confirmation(self):
        for text in ("Let me check.", "Thursday 8 might work.", "Is 8 available?"):
            rows = [row(1, "user", "Thursday at 8"), row(2, "staff", text)]
            _, state = validate_inference(inferred(), rows, 2)
            self.assertEqual(state, "uncertain", text)

    def test_arabic_and_english_staff_confirmations_are_ready(self):
        for text in ("تمام حجزتلك الخميس الساعة ٨", "You're booked Thursday at 8 PM."):
            rows = [row(1, "user", "Thursday 8"), row(2, "staff", text)]
            _, state = validate_inference(inferred(), rows, 2)
            self.assertEqual(state, "ready", text)

    def test_post_inference_validation_still_requires_matching_action_language(self):
        rows = [row(1, "user", "Thursday 8"), row(2, "staff", "You're booked Thursday at 8")]
        raw = inferred(
            "cancel", date="2026-09-24", time="8:00 PM", area="",
            evidence_message_ids=[1, 2],
        )
        _, state = validate_inference(raw, rows, 2)
        self.assertEqual(state, "uncertain")

    def test_uncertain_or_ambiguous_exact_target_never_mutates(self):
        rows = [row(1, "user", "Cancel tomorrow"), row(2, "staff", "Okay, cancelled.")]
        raw = inferred("cancel", date="2026-09-21", time="", area="", evidence_message_ids=[1, 2])
        parsed, state = validate_inference(raw, rows, 2)
        self.assertEqual(state, "ambiguous")
        supervisor = MemorySupervisor(rows, raw)
        supervisor.process_trigger({"message_id": 2, "phone_number": PHONE})
        self.assertFalse(supervisor.booking.calls)
        self.assertTrue(supervisor.attention)


class PassiveOrchestrationTests(unittest.TestCase):
    def test_irrelevant_staff_turn_completes_without_inference(self):
        for text in ("شكراً", "تمام", "لحظة واحدة", "هسأل الدكتورة", "الباكدج تشمل 3 مناطق"):
            with self.subTest(text=text):
                rows = [row(1, "user", "عايزة أعرف السعر"), row(2, "staff", text)]
                supervisor = MemorySupervisor(rows, inferred())
                supervisor.process_trigger({"message_id": 2, "phone_number": PHONE})
                self.assertEqual(supervisor.infer_calls, 0)
                self.assertFalse(supervisor.booking.calls)
                self.assertEqual(supervisor.completed, [(2, "irrelevant_staff_message")])

    def test_eligible_staff_completion_still_runs_structured_inference(self):
        rows = [row(1, "user", "Thursday 8"), row(2, "staff", "You're booked Thursday at 8")]
        supervisor = MemorySupervisor(rows, inferred())
        supervisor.process_trigger({"message_id": 2, "phone_number": PHONE})
        self.assertEqual(supervisor.infer_calls, 1)
        self.assertEqual(supervisor.booking.calls[0][0], "book")

    def test_confirmed_book_reschedule_and_cancel_use_authoritative_service(self):
        scenarios = [
            ("book", "Confirmed for Thursday at 8.", {}, "book"),
            ("reschedule", "I've rescheduled your appointment.", {
                "old_date": "2026-09-22", "old_time": "7:00 PM", "area": ""
            }, "reschedule"),
            ("cancel", "Okay, cancelled.", {
                "date": "2026-09-22", "time": "", "old_time": "7:00 PM", "area": ""
            }, "cancel"),
        ]
        for action, text, changes, expected in scenarios:
            with self.subTest(action=action):
                rows = [row(1, "user", "Please change it"), row(2, "staff", text)]
                supervisor = MemorySupervisor(rows, inferred(action, **changes))
                supervisor.process_trigger({"message_id": 2, "phone_number": PHONE})
                self.assertEqual(supervisor.booking.calls[0][0], expected)
                self.assertEqual(supervisor.refreshes, [PHONE])
                self.assertEqual(len(supervisor.system_events), 1)
                self.assertEqual(supervisor.events[next(iter(supervisor.events))]["status"], "succeeded")

    def test_unavailable_or_manually_taken_slot_does_not_choose_alternative(self):
        rows = [row(1, "user", "Thursday 8"), row(2, "staff", "Confirmed for Thursday at 8")]
        booking = FakeBooking({"ok": False, "code": "slot_unavailable"})
        supervisor = MemorySupervisor(rows, inferred(), booking)
        supervisor.process_trigger({"message_id": 2, "phone_number": PHONE})
        self.assertEqual(len(booking.calls), 1)
        self.assertFalse(supervisor.refreshes)
        self.assertFalse(supervisor.system_events)
        self.assertEqual(supervisor.attention[0][1], "appointment_sync_conflict")

    def test_scheduling_unavailable_creates_attention(self):
        rows = [row(1, "user", "Thursday 8"), row(2, "staff", "Confirmed for Thursday at 8")]
        supervisor = MemorySupervisor(rows, inferred(), FakeBooking({"ok": False, "code": "booking_service_unavailable"}))
        supervisor.process_trigger({"message_id": 2, "phone_number": PHONE})
        self.assertEqual(supervisor.attention[0][1], "scheduling_unavailable")

    def test_transport_exception_is_uncertain_and_is_never_retried(self):
        rows = [row(1, "user", "Thursday 8"), row(2, "staff", "Confirmed for Thursday at 8")]
        booking = FakeBooking()
        booking.book = mock.Mock(side_effect=TimeoutError("response lost"))
        supervisor = MemorySupervisor(rows, inferred(), booking)
        trigger = {"message_id": 2, "phone_number": PHONE}
        supervisor.process_trigger(trigger)
        supervisor.process_trigger(trigger)
        self.assertEqual(booking.book.call_count, 1)
        self.assertTrue(any(kind == "passive_sync_uncertain_result" for _, kind, _ in supervisor.attention))

    def test_two_workers_and_duplicate_inference_cannot_repeat_mutation(self):
        rows = [row(1, "user", "Thursday 8"), row(2, "staff", "Confirmed for Thursday at 8")]
        booking = FakeBooking()
        supervisor = MemorySupervisor(rows, inferred(), booking, barrier=threading.Barrier(2))
        trigger = {"message_id": 2, "phone_number": PHONE}
        workers = [threading.Thread(target=supervisor.process_trigger, args=(trigger,)) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=3)
        self.assertEqual(len(booking.calls), 1)
        self.assertEqual(len(supervisor.system_events), 1)

    def test_external_success_then_local_failure_is_never_replayed_after_restart(self):
        rows = [row(1, "user", "Thursday 8"), row(2, "staff", "Confirmed for Thursday at 8")]
        booking = FakeBooking()
        supervisor = MemorySupervisor(rows, inferred(), booking, finalize_failure=True)
        trigger = {"message_id": 2, "phone_number": PHONE}
        with self.assertRaises(RuntimeError): supervisor.process_trigger(trigger)
        supervisor.finalize_failure = False
        supervisor.process_trigger(trigger)
        self.assertEqual(len(booking.calls), 1)
        self.assertTrue(any(kind == "passive_sync_uncertain_result" for _, kind, _ in supervisor.attention))

    def test_passive_module_has_no_whatsapp_delivery_dependency(self):
        source = pathlib.Path("receptionist/supervisor.py").read_text(encoding="utf-8")
        self.assertNotIn("send_whatsapp_message", source)
        self.assertNotIn("graph.facebook.com", source)


class HumanModePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_staff_send_in_human_does_not_pause_patient(self):
        row = {"id": 81, "role": "staff", "content": "One moment", "created_at": "now"}
        with mock.patch.object(main, "send_whatsapp_message", new=mock.AsyncMock(
            return_value={"ok": True, "message_id": "out-81"}
        )), mock.patch.object(main, "save_chat_turn", return_value=row), mock.patch.object(
            main, "get_operating_mode", return_value="HUMAN"
        ), mock.patch.object(main, "set_patient_pause") as pause, mock.patch.object(main, "audit"):
            result = await main.api_send_message(
                main.StaffMessageReq(phone_number=PHONE, message="One moment", pause_after_send=True),
                main.BackgroundTasks(), admin="owner",
            )
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["pause_applied"])
        pause.assert_not_called()

    async def test_staff_send_in_ai_active_preserves_patient_takeover(self):
        row = {"id": 82, "role": "staff", "content": "I will handle this", "created_at": "now"}
        with mock.patch.object(main, "send_whatsapp_message", new=mock.AsyncMock(
            return_value={"ok": True, "message_id": "out-82"}
        )), mock.patch.object(main, "save_chat_turn", return_value=row), mock.patch.object(
            main, "get_operating_mode", return_value="AI_ACTIVE"
        ), mock.patch.object(main, "set_patient_pause") as pause, mock.patch.object(main, "audit"):
            result = await main.api_send_message(
                main.StaffMessageReq(phone_number=PHONE, message="I will handle this"),
                main.BackgroundTasks(), admin="owner",
            )
        self.assertTrue(result["data"]["pause_applied"])
        pause.assert_called_once_with(PHONE, True, source="staff_message")

    async def test_meta_staff_echo_in_human_does_not_pause_patient(self):
        body = {"entry": [{"changes": [{"field": "smb_message_echoes", "value": {
            "recipient_id": PHONE,
            "messages": [{"id": "echo-1", "type": "text", "text": {"body": "شكراً"}}],
        }}]}]}

        class Request:
            async def json(self): return body

        with mock.patch.object(main, "claim_message", return_value=True), mock.patch.object(
            main, "save_chat_turn", return_value={"id": 83}
        ), mock.patch.object(main, "get_operating_mode", return_value="HUMAN"), mock.patch.object(
            main, "set_patient_pause"
        ) as pause, mock.patch.object(main, "audit"):
            result = await main.receive_message(Request(), main.BackgroundTasks())
        self.assertEqual(result["status"], "EVENT_RECEIVED")
        self.assertEqual(result["scheduled"], 1)
        pause.assert_not_called()

    async def test_staff_confirmation_is_queued_in_same_database_transaction(self):
        executed = []

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, sql, params=()): executed.append((" ".join(sql.split()), params))
            def fetchone(self):
                return {"id": 55, "role": "staff", "content": "Confirmed for Thursday at 8", "whatsapp_message_id": "w1", "created_at": "now"}

        class Connection:
            commits = 0
            def cursor(self, **kwargs): return Cursor()
            def commit(self): self.commits += 1

        connection = Connection()

        @contextlib.contextmanager
        def borrow():
            yield connection

        with mock.patch.object(main, "get_db_connection", side_effect=borrow):
            saved = main.save_chat_turn(PHONE, "staff", "Confirmed for Thursday at 8", "w1", enqueue_passive_sync=True)
        self.assertEqual(saved["id"], 55)
        self.assertEqual(connection.commits, 1)
        insert_chat = next(i for i, (sql, _) in enumerate(executed) if "INSERT INTO chat_history" in sql)
        insert_queue = next(i for i, (sql, _) in enumerate(executed) if "INSERT INTO passive_sync_queue" in sql)
        self.assertLess(insert_chat, insert_queue)

    async def test_human_mode_stores_and_learns_but_never_replies(self):
        profile = {"name": "Mona", "preferences": "", "tags": [], "is_paused": False}
        save = mock.Mock(return_value={"id": 22})
        memory = mock.AsyncMock(return_value={**profile, "preferences": "Evenings"})
        generate = mock.AsyncMock()
        send = mock.AsyncMock()
        with mock.patch.object(main, "get_user_lock", new=mock.AsyncMock(return_value=asyncio.Lock())), mock.patch.object(
            main, "save_chat_turn", save
        ), mock.patch.object(main, "load_patient_profile", return_value=profile), mock.patch.object(
            main, "update_booking_draft_from_message", return_value=None
        ), mock.patch.object(main, "update_patient_memory", memory), mock.patch.object(
            main, "patient_facing_ai_is_enabled", return_value=False
        ), mock.patch.object(main, "generate_ai_reply", generate), mock.patch.object(
            main, "send_whatsapp_message", send
        ):
            await main.handle_ai_conversation(PHONE, "Please remember evenings", "pid", "wamid-1")
        save.assert_called_once()
        memory.assert_awaited_once()
        generate.assert_not_awaited()
        send.assert_not_awaited()

    async def test_ai_active_preserves_existing_reply_flow(self):
        profile = {"name": "Mona", "preferences": "", "tags": [], "is_paused": False}
        send = mock.AsyncMock(return_value={"ok": True, "message_id": "out-1"})
        with mock.patch.object(main, "load_patient_profile", return_value=profile), mock.patch.object(
            main, "update_booking_draft_from_message", return_value=None
        ), mock.patch.object(main, "patient_facing_ai_is_enabled", return_value=True), mock.patch.object(
            main, "generate_ai_reply", new=mock.AsyncMock(return_value=("Hello", []))
        ) as generate, mock.patch.object(main, "send_whatsapp_message", send), mock.patch.object(
            main, "save_chat_turn"
        ):
            await main.process_conversation_batch(PHONE, [{"chat_history_id": 3, "content": "Hello"}], "pid")
        generate.assert_awaited_once()
        send.assert_awaited_once()

    async def test_returning_to_human_immediately_blocks_replies(self):
        profile = {"name": "Mona", "preferences": "", "tags": [], "is_paused": False}
        with mock.patch.object(main, "load_patient_profile", return_value=profile), mock.patch.object(
            main, "update_booking_draft_from_message", return_value=None
        ), mock.patch.object(main, "patient_facing_ai_is_enabled", return_value=False), mock.patch.object(
            main, "generate_ai_reply", new=mock.AsyncMock()
        ) as generate, mock.patch.object(main, "send_whatsapp_message", new=mock.AsyncMock()) as send:
            await main.process_conversation_batch(
                PHONE, [{"chat_history_id": 84, "content": "Hello"}], "pid"
            )
        generate.assert_not_awaited()
        send.assert_not_awaited()


class EmergencyActivationTests(unittest.TestCase):
    def test_activation_atomically_repairs_master_mode_and_legacy_pauses(self):
        executed = []

        class Cursor:
            row = None
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, sql, params=()):
                compact = " ".join(sql.split())
                executed.append((compact, params))
                if "SELECT COUNT(*) AS resumed_count FROM summaries" in compact:
                    self.row = {"resumed_count": 7}
                elif "SELECT COUNT(*) AS paused_count" in compact:
                    self.row = {"paused_count": 2}
                else:
                    self.row = None
            def fetchone(self): return self.row

        class Connection:
            commits = 0
            def cursor(self, **_): return Cursor()
            def commit(self): self.commits += 1

        connection = Connection()
        @contextlib.contextmanager
        def borrow(): yield connection

        with mock.patch.object(main, "get_db_connection", side_effect=borrow), mock.patch.object(
            main, "ENABLE_REAL_CLINIC", True
        ):
            result = main.activate_ai_receptionist("owner")

        sql = "\n".join(item[0] for item in executed)
        self.assertEqual(connection.commits, 1)
        self.assertTrue(result["effective_patient_facing_ai"])
        self.assertEqual(result["resumed_conversations"], 7)
        self.assertEqual(result["explicit_paused_conversations"], 2)
        self.assertIn("'bot_globally_active','true'", sql)
        self.assertIn("'operating_mode','AI_ACTIVE'", sql)
        self.assertIn("IN ('legacy','staff_message')", sql)
        self.assertNotIn("IN ('legacy','staff_message','manual')", sql)
        self.assertIn("next_conversation_summary_version()", sql)
        self.assertIn("ai_receptionist_activated", sql)

    def test_supervisor_activation_endpoint_works_when_legacy_master_was_off(self):
        activated = {"mode": "AI_ACTIVE", "master_enabled": True,
                     "effective_patient_facing_ai": True,
                     "resumed_conversations": 12, "explicit_paused_conversations": 1}
        with mock.patch.object(main, "activate_ai_receptionist", return_value=activated) as activate:
            result = main.api_activate_ai_receptionist(admin="owner")
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["master_enabled"])
        self.assertTrue(result["data"]["effective_patient_facing_ai"])
        activate.assert_called_once_with("owner")

    def test_supervisor_never_claims_active_when_master_is_off(self):
        summary_row = {"needs_reply": 0, "waiting_over_15": 0,
                       "human_handled_today": 0, "todays_bookings": 0,
                       "last_staff_activity": None, "attention_count": 0,
                       "sync_failures": 0}
        data = supervisor_summary(lambda *a, **k: summary_row, "AI_ACTIVE", False, True, True)
        self.assertEqual(data["operating_mode"], "AI_BACKUP")
        self.assertEqual(data["ai_receptionist_status"], "standby")
        self.assertFalse(data["effective_patient_facing_ai"])


if __name__ == "__main__":
    unittest.main()
