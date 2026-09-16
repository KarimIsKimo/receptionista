import asyncio
import datetime as dt
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo


os.environ.setdefault("DATABASE_URL", "postgresql://example.invalid/test")
os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test-whatsapp-token")
os.environ.setdefault("APPS_SCRIPT_URL", "https://example.invalid/apps-script")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

with mock.patch("google.genai.Client"):
    import main  # noqa: E402

from receptionist.conversation import (  # noqa: E402
    combine_inbound_messages,
    evolve_booking_draft,
    should_extract_memory,
)


NOW = dt.datetime(2026, 9, 14, 18, 0, tzinfo=ZoneInfo("Africa/Cairo"))
PHONE = "201000000000"


class FakeRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def webhook_body(message_id="wamid-1", text="عايزة أحجز ليزر"):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "clinic-phone-id"},
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": PHONE,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }


class GeminiHistoryTests(unittest.TestCase):
    def test_current_persisted_turn_is_sent_to_gemini_exactly_once(self):
        current = "عايزة أحجز بكرة"

        class Chat:
            def __init__(self):
                self.sent = []

            def send_message(self, message):
                self.sent.append(message)
                return SimpleNamespace(text="تمام")

        chat = Chat()
        captured = {}

        def create_chat(**kwargs):
            captured.update(kwargs)
            return chat

        client = SimpleNamespace(chats=SimpleNamespace(create=create_chat))

        def fake_db(sql, params=(), **kwargs):
            if "FROM chat_history" in sql:
                self.assertIn("id < %s", sql)
                self.assertEqual(params, (PHONE, 30, 12))
                return [
                    {"role": "model", "content": "أهلاً بحضرتك"},
                    {"role": "user", "content": "عندي استفسار"},
                ]
            raise AssertionError(f"Unexpected SQL: {sql}")

        with mock.patch.object(main, "gemini_client", client), mock.patch.object(
            main, "db_execute", side_effect=fake_db
        ), mock.patch.object(main, "get_live_instructions", return_value="Instructions"):
            reply, _ = main.generate_ai_reply_sync(
                PHONE,
                current,
                {"name": "Mona", "preferences": "", "tags": []},
                history_before_id=30,
            )

        history_text = "\n".join(
            content.parts[0].text for content in captured["history"]
        )
        self.assertNotIn(current, history_text)
        self.assertEqual(chat.sent, [current])
        self.assertEqual(reply, "تمام")

    def test_staff_and_system_records_never_masquerade_as_model(self):
        user = main.history_content("user", "Patient message")
        model = main.history_content("model", "AI message")
        staff = main.history_content("staff", "Human reply")
        system = main.history_content("system", "Delivery failed")

        self.assertEqual(user.role, "user")
        self.assertEqual(model.role, "model")
        self.assertEqual(staff.role, "user")
        self.assertIn("موظف استقبال بشري", staff.parts[0].text)
        self.assertIn("Human reply", staff.parts[0].text)
        self.assertEqual(system.role, "user")
        self.assertIn("سجل نظام داخلي/خطأ", system.parts[0].text)
        self.assertIn("Delivery failed", system.parts[0].text)

    def test_saved_preferences_and_booking_draft_are_explicit_context(self):
        with mock.patch.object(main, "get_live_instructions", return_value="Instructions"):
            prompt = main.build_system_instruction(
                {"name": "Mona", "preferences": "Prefers evening visits", "tags": []},
                PHONE,
                {
                    "intent": "book",
                    "patient_name": "Mona",
                    "service_area": "ليزر",
                    "requested_date": "2026-09-15",
                    "requested_time": "7:00 PM",
                    "stage": "ready_to_book",
                },
            )

        self.assertIn("Prefers evening visits", prompt)
        self.assertIn('"patient_name": "Mona"', prompt)
        self.assertIn('"stage": "ready_to_book"', prompt)
        self.assertIn("لا تسألي مرة أخرى", prompt)
        self.assertIn("ليست تأكيداً", prompt)
        self.assertNotIn("معلومة مؤكدة", prompt)


class BookingDraftTests(unittest.TestCase):
    def evolve(self, current, message, profile=None):
        return evolve_booking_draft(
            current,
            message,
            profile or {},
            now=NOW,
        )

    def test_fragmented_booking_keeps_saved_name_and_asks_only_for_missing_area(self):
        draft = None
        profile = {"name": "Mona"}
        for fragment in ("عايزة احجز", "ليزر", "بكرة", "بعد ٧"):
            draft, action = self.evolve(draft, fragment, profile)
            self.assertEqual(action, "upsert")

        self.assertEqual(draft["patient_name"], "Mona")
        # "ليزر" identifies the clinic service but not the treatment area;
        # do not silently book a generic/incorrect area.
        self.assertEqual(draft["service_area"], "")
        self.assertEqual(draft["requested_date"], "2026-09-15")
        self.assertEqual(draft["requested_time"], "7:00 PM")
        self.assertEqual(draft["stage"], "need_service")

    def test_new_patient_name_and_fields_are_collected_across_turns(self):
        draft, _ = self.evolve(None, "اسمي منى وعايزة احجز بيكيني")
        draft, _ = self.evolve(draft, "بكرة")
        draft, _ = self.evolve(draft, "الساعة 8")

        self.assertEqual(draft["patient_name"], "منى")
        self.assertEqual(draft["requested_date"], "2026-09-15")
        self.assertEqual(draft["requested_time"], "8:00 PM")
        self.assertEqual(draft["stage"], "ready_to_book")

    def test_multiple_treatment_areas_are_preserved_without_loss(self):
        examples = {
            "بيكيني واندر ارم": "بيكيني + أندر آرم",
            "بيكيني + اندر ارم + لاين": "بيكيني + أندر آرم + لاين",
            "وجه ورقبة": "وجه + رقبة",
            "صدر وظهر واندر ارم": "صدر + ظهر + أندر آرم",
        }
        for patient_words, expected in examples.items():
            with self.subTest(patient_words=patient_words):
                draft, action = self.evolve(
                    None,
                    f"عايزة احجز {patient_words}",
                    {"name": "Mona"},
                )
                self.assertEqual(action, "upsert")
                self.assertEqual(draft["service_area"], expected)

    def test_egyptian_half_hour_is_parsed_without_prefix_truncation(self):
        for message in ("عايزة احجز الساعة 7 ونص", "عايزة احجز بعد 7 ونص"):
            with self.subTest(message=message):
                draft, _ = self.evolve(None, message, {"name": "Mona"})
                self.assertEqual(draft["requested_time"], "7:30 PM")

    def test_unrecognized_fraction_and_alternative_times_stay_blank(self):
        current = {
            "intent": "book",
            "patient_name": "Mona",
            "service_area": "بيكيني",
            "requested_date": "2026-09-15",
            "requested_time": "6:00 PM",
            "stage": "ready_to_book",
        }
        for message in (
            "عايزة احجز الساعة 7 وربع",
            "عايزة احجز الساعة 7 أو 8",
            "عايزة احجز حوالي الساعة 7",
            "عايزة احجز الساعة 7 ونص إلا ربع",
            "عايزة احجز الساعة 7 لحد 8",
        ):
            with self.subTest(message=message):
                draft, _ = self.evolve(current, message, {"name": "Mona"})
                self.assertEqual(draft["requested_time"], "")

    def test_multiple_candidate_dates_stay_blank(self):
        draft, _ = self.evolve(
            {
                "intent": "book",
                "patient_name": "Mona",
                "service_area": "بيكيني",
                "requested_date": "2026-09-15",
                "requested_time": "7:00 PM",
                "stage": "ready_to_book",
            },
            "عايزة احجز بكرة أو الخميس",
            {"name": "Mona"},
        )
        self.assertEqual(draft["requested_date"], "")

    def test_existing_appointment_language_never_becomes_new_booking(self):
        examples = (
            "عندي حجز بكرة",
            "حجزي امتى؟",
            "موعدي امتى؟",
            "ممكن أعرف الحجز بتاعي؟",
            "أنا حاجزة الخميس؟",
        )
        for message in examples:
            with self.subTest(message=message):
                draft, action = self.evolve(None, message, {"name": "Mona"})
                self.assertEqual(action, "upsert")
                self.assertEqual(draft["intent"], "check_appointment")
                self.assertNotEqual(draft["intent"], "book")
                self.assertEqual(draft["requested_date"], "")
                self.assertEqual(draft["requested_time"], "")
                self.assertEqual(draft["stage"], "checking_appointment")

    def test_common_egyptian_cancellation_variants_are_supported(self):
        examples = (
            "ألغي",
            "الغي",
            "إلغاء",
            "الغاء",
            "عايزة ألغي الحجز",
            "عايز ألغي الموعد",
        )
        for message in examples:
            with self.subTest(message=message):
                draft, action = self.evolve(None, message, {"name": "Mona"})
                self.assertEqual(action, "upsert")
                self.assertEqual(draft["intent"], "cancel")

    def test_standalone_name_is_only_captured_while_waiting_for_name(self):
        current = {
            "intent": "book",
            "patient_name": "",
            "service_area": "بيكيني",
            "requested_date": "2026-09-15",
            "requested_time": "7:00 PM",
            "stage": "need_name",
        }
        draft, _ = self.evolve(current, "منى أحمد")
        self.assertEqual(draft["patient_name"], "منى أحمد")
        self.assertEqual(draft["stage"], "ready_to_book")

        for invalid in ("تمام", "123", "عايزة احجز", "أنا عايزة اعرف السعر"):
            with self.subTest(invalid=invalid):
                rejected, _ = self.evolve(current, invalid)
                self.assertEqual(rejected["patient_name"], "")

    def test_standalone_name_is_not_captured_outside_need_name_stage(self):
        draft, action = self.evolve(None, "منى أحمد")
        self.assertIsNone(draft)
        self.assertEqual(action, "none")

    def test_reschedule_with_old_and_new_times_keeps_them_unclassified(self):
        draft, action = self.evolve(
            None,
            "عايزة أغير موعدي بكرة الساعة 7 للخميس الساعة 8",
            {"name": "Mona"},
        )
        self.assertEqual(action, "upsert")
        self.assertEqual(draft["intent"], "reschedule")
        self.assertEqual(draft["requested_date"], "")
        self.assertEqual(draft["requested_time"], "")
        self.assertEqual(draft["stage"], "reschedule_unclassified")

    def test_bare_seven_uses_active_booking_stage(self):
        current = {
            "intent": "book",
            "patient_name": "Mona",
            "service_area": "ليزر",
            "requested_date": "2026-09-15",
            "requested_time": "",
            "stage": "need_time",
        }
        draft, _ = self.evolve(current, "7", {"name": "Mona"})
        self.assertEqual(draft["requested_time"], "7:00 PM")
        self.assertEqual(draft["stage"], "ready_to_book")

    def test_ambiguous_time_is_not_invented(self):
        current = {
            "intent": "book",
            "patient_name": "Mona",
            "service_area": "ليزر",
            "requested_date": "2026-09-15",
            "requested_time": "",
            "stage": "need_time",
        }
        draft, _ = self.evolve(current, "7 ولا 8", {"name": "Mona"})
        self.assertEqual(draft["requested_time"], "")
        self.assertEqual(draft["stage"], "need_time")

    def test_abandonment_clears_booking_draft(self):
        draft, action = self.evolve(
            {"intent": "book", "stage": "need_time"},
            "خلاص مش عايزة احجز",
        )
        self.assertIsNone(draft)
        self.assertEqual(action, "clear")

    def test_expired_draft_is_cleared(self):
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        row = {
            "phone_number": PHONE,
            "intent": "book",
            "patient_name": "Mona",
            "service_area": "ليزر",
            "requested_date": "2026-09-15",
            "requested_time": "7:00 PM",
            "stage": "ready_to_book",
            "updated_at": old,
        }
        with mock.patch.object(main, "db_execute", return_value=row), mock.patch.object(
            main, "clear_booking_draft"
        ) as clear:
            result = main.load_booking_draft(PHONE)

        self.assertIsNone(result)
        clear.assert_called_once_with(PHONE, "timeout")


class ConversationBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_rapid_burst_produces_one_coherent_ai_turn(self):
        fragments = ["هاي", "عايزة احجز", "ليزر", "بكرة", "بعد ٧"]
        messages = [
            {"chat_history_id": index + 10, "content": text}
            for index, text in enumerate(fragments)
        ]
        profile = {
            "phone_number": PHONE,
            "name": "Mona",
            "preferences": "Prefers evenings",
            "tags": [],
            "is_paused": False,
        }
        generate = mock.AsyncMock(return_value=("رد واحد", []))
        send = mock.AsyncMock(return_value={"ok": True, "message_id": "out-1"})

        with mock.patch.object(main, "ENABLE_REAL_CLINIC", True), mock.patch.object(
            main, "load_patient_profile", return_value=profile
        ), mock.patch.object(
            main, "update_booking_draft_from_message", return_value={"stage": "ready_to_book"}
        ), mock.patch.object(
            main, "update_patient_memory", new=mock.AsyncMock(return_value=profile)
        ), mock.patch.object(
            main, "is_bot_globally_active", return_value=True
        ), mock.patch.object(main, "generate_ai_reply", generate), mock.patch.object(
            main, "send_whatsapp_message", send
        ), mock.patch.object(main, "save_chat_turn"):
            await main.process_conversation_batch(PHONE, messages, "clinic-phone-id")

        generate.assert_awaited_once()
        args, kwargs = generate.await_args
        self.assertEqual(args[1], "\n".join(fragments))
        self.assertEqual(kwargs["history_before_id"], 10)
        send.assert_awaited_once_with(PHONE, "رد واحد", "clinic-phone-id")

    async def test_queue_lease_processes_ordered_batch_once(self):
        batch = [
            {"message_id": "m1", "phone_number": PHONE, "chat_history_id": 1, "content": "one", "phone_number_id": "pid", "side_effects_started_at": None},
            {"message_id": "m2", "phone_number": PHONE, "chat_history_id": 2, "content": "two", "phone_number_id": "pid", "side_effects_started_at": None},
        ]
        events = []

        def reserve(_messages):
            events.append("reserved")
            return True

        async def process(*_args):
            events.append("processed")

        def complete(_messages):
            events.append("completed")

        with mock.patch.object(main, "acquire_processing_lease", return_value=True), mock.patch.object(
            main, "pending_batch_delay", side_effect=[0.0, None]
        ), mock.patch.object(main, "refresh_processing_lease", return_value=True), mock.patch.object(
            main, "claim_pending_batch", return_value=batch
        ), mock.patch.object(
            main, "reserve_pending_batch_side_effects", side_effect=reserve
        ), mock.patch.object(
            main, "process_conversation_batch", side_effect=process
        ) as process_mock, mock.patch.object(
            main, "mark_pending_batch_processed", side_effect=complete
        ), mock.patch.object(main, "release_processing_lease"):
            await main.process_pending_inbound(PHONE)

        process_mock.assert_awaited_once_with(PHONE, batch, "pid")
        self.assertEqual(events, ["reserved", "processed", "completed"])

    async def test_recovered_uncertain_batch_is_quarantined_not_replayed(self):
        batch = [
            {
                "message_id": "m1",
                "phone_number": PHONE,
                "chat_history_id": 1,
                "content": "عايزة احجز",
                "phone_number_id": "pid",
                "side_effects_started_at": dt.datetime.now(dt.timezone.utc),
                "side_effects_completed_at": None,
            }
        ]
        process = mock.AsyncMock()
        with mock.patch.object(main, "acquire_processing_lease", return_value=True), mock.patch.object(
            main, "pending_batch_delay", side_effect=[0.0, None]
        ), mock.patch.object(main, "refresh_processing_lease", return_value=True), mock.patch.object(
            main, "claim_pending_batch", return_value=batch
        ), mock.patch.object(
            main, "suppress_uncertain_pending_batch", return_value=1
        ) as suppress, mock.patch.object(
            main, "reserve_pending_batch_side_effects"
        ) as reserve, mock.patch.object(
            main, "process_conversation_batch", process
        ), mock.patch.object(
            main, "mark_pending_batch_processed"
        ) as complete, mock.patch.object(main, "release_processing_lease"):
            await main.process_pending_inbound(PHONE)

        suppress.assert_called_once_with(batch)
        reserve.assert_not_called()
        process.assert_not_awaited()
        complete.assert_not_called()

    async def test_crash_after_reservation_is_not_replayed_on_recovery(self):
        fresh = [
            {
                "message_id": "m1",
                "phone_number": PHONE,
                "chat_history_id": 1,
                "content": "عايزة احجز",
                "phone_number_id": "pid",
                "side_effects_started_at": None,
                "side_effects_completed_at": None,
            }
        ]
        recovered = [{**fresh[0], "side_effects_started_at": dt.datetime.now(dt.timezone.utc)}]
        external_effects = []

        async def side_effect_then_crash(*_args):
            external_effects.append("booking_or_reply_may_have_succeeded")
            raise RuntimeError("process crashed")

        process = mock.AsyncMock(side_effect=side_effect_then_crash)
        with mock.patch.object(main, "acquire_processing_lease", return_value=True), mock.patch.object(
            main, "pending_batch_delay", side_effect=[0.0, 0.0, None]
        ), mock.patch.object(main, "refresh_processing_lease", return_value=True), mock.patch.object(
            main, "claim_pending_batch", side_effect=[fresh, recovered]
        ), mock.patch.object(
            main, "reserve_pending_batch_side_effects", return_value=True
        ) as reserve, mock.patch.object(
            main, "process_conversation_batch", process
        ), mock.patch.object(
            main, "suppress_uncertain_pending_batch", return_value=1
        ) as suppress, mock.patch.object(
            main, "mark_pending_batch_processed"
        ) as complete, mock.patch.object(main, "release_processing_lease"):
            await main.process_pending_inbound(PHONE)
            await main.process_pending_inbound(PHONE)

        reserve.assert_called_once_with(fresh)
        process.assert_awaited_once_with(PHONE, fresh, "pid")
        self.assertEqual(external_effects, ["booking_or_reply_may_have_succeeded"])
        suppress.assert_called_once_with(recovered)
        complete.assert_not_called()

    async def test_paused_patient_learns_memory_but_receives_no_ai_reply(self):
        profile = {
            "phone_number": PHONE,
            "name": "Mona",
            "preferences": "",
            "tags": [],
            "is_paused": True,
        }
        memory = mock.AsyncMock(return_value={**profile, "preferences": "Prefers evenings"})
        generate = mock.AsyncMock(return_value=("must not send", []))
        send = mock.AsyncMock()
        with mock.patch.object(main, "ENABLE_REAL_CLINIC", True), mock.patch.object(
            main, "load_patient_profile", return_value=profile
        ), mock.patch.object(
            main, "update_booking_draft_from_message", return_value=None
        ), mock.patch.object(main, "update_patient_memory", memory), mock.patch.object(
            main, "is_bot_globally_active", return_value=True
        ), mock.patch.object(main, "generate_ai_reply", generate), mock.patch.object(
            main, "send_whatsapp_message", send
        ):
            await main.process_conversation_batch(
                PHONE,
                [{"chat_history_id": 1, "content": "أنا دايماً أفضل مواعيد المساء"}],
                "pid",
            )

        memory.assert_awaited_once()
        generate.assert_not_awaited()
        send.assert_not_awaited()

    async def test_bot_resumes_after_human_takeover(self):
        paused = {"name": "Mona", "preferences": "", "tags": [], "is_paused": True}
        resumed = {**paused, "is_paused": False}
        generate = mock.AsyncMock(return_value=("أهلاً من جديد", []))
        send = mock.AsyncMock(return_value={"ok": True, "message_id": "out-2"})
        with mock.patch.object(main, "ENABLE_REAL_CLINIC", True), mock.patch.object(
            main, "load_patient_profile", side_effect=[paused, resumed]
        ), mock.patch.object(
            main, "update_booking_draft_from_message", return_value=None
        ), mock.patch.object(
            main, "update_patient_memory", new=mock.AsyncMock(side_effect=lambda _, p: p)
        ), mock.patch.object(
            main, "is_bot_globally_active", return_value=True
        ), mock.patch.object(main, "generate_ai_reply", generate), mock.patch.object(
            main, "send_whatsapp_message", send
        ), mock.patch.object(main, "save_chat_turn"):
            await main.process_conversation_batch(
                PHONE, [{"chat_history_id": 1, "content": "أفضل المساء"}], "pid"
            )
            await main.process_conversation_batch(
                PHONE, [{"chat_history_id": 2, "content": "ممكن نكمل الحجز؟"}], "pid"
            )

        generate.assert_awaited_once()
        send.assert_awaited_once_with(PHONE, "أهلاً من جديد", "pid")

    async def test_repeated_meta_webhook_is_not_scheduled_twice(self):
        first_tasks = main.BackgroundTasks()
        second_tasks = main.BackgroundTasks()
        with mock.patch.object(
            main,
            "persist_incoming_message",
            side_effect=[{"id": 1}, None],
        ) as persist:
            first = await main.receive_message(FakeRequest(webhook_body()), first_tasks)
            second = await main.receive_message(FakeRequest(webhook_body()), second_tasks)

        self.assertEqual(first["scheduled"], 1)
        self.assertEqual(second["scheduled"], 0)
        self.assertEqual(len(first_tasks.tasks), 1)
        self.assertEqual(len(second_tasks.tasks), 0)
        self.assertEqual(persist.call_count, 2)


class MemoryEfficiencyTests(unittest.TestCase):
    def test_trivial_messages_skip_memory_extraction(self):
        for message in (
            "هاي",
            "تمام",
            "🙏",
            "7",
            "الساعة ٧",
            "الساعة ٧ ونص",
            "الساعة ٧ وربع",
        ):
            with self.subTest(message=message):
                self.assertFalse(should_extract_memory(message))

    def test_durable_preference_is_eligible_for_memory(self):
        self.assertTrue(should_extract_memory("أنا دايماً أفضل المواعيد المسائية"))

    def test_burst_combination_preserves_order_and_every_fragment(self):
        messages = [{"content": "one"}, {"content": "two"}, {"content": "three"}]
        self.assertEqual(combine_inbound_messages(messages), "one\ntwo\nthree")


class QueuePersistenceTests(unittest.TestCase):
    def test_side_effect_reservation_is_durable_and_all_or_nothing(self):
        messages = [
            {"message_id": "m1"},
            {"message_id": "m2"},
        ]
        rows = [{"message_id": "m1"}, {"message_id": "m2"}]
        with mock.patch.object(main, "db_execute", return_value=rows) as database:
            reserved = main.reserve_pending_batch_side_effects(messages)

        self.assertTrue(reserved)
        sql, params = database.call_args.args
        self.assertIn("side_effects_started_at=NOW()", sql)
        self.assertIn("recovery_state='in_progress'", sql)
        self.assertIn("side_effects_started_at IS NULL", sql)
        self.assertEqual(params, (["m1", "m2"],))

    def test_uncertain_suppression_and_staff_record_share_one_transaction(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=()):
                self.calls.append((sql, params))

            def fetchall(self):
                return [{"message_id": "m1"}]

        class FakeConnection:
            def __init__(self):
                self.cursor_instance = FakeCursor()
                self.commits = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self, **_kwargs):
                return self.cursor_instance

            def commit(self):
                self.commits += 1

        connection = FakeConnection()
        messages = [
            {
                "message_id": "m1",
                "phone_number": PHONE,
                "side_effects_started_at": dt.datetime.now(dt.timezone.utc),
            }
        ]
        with mock.patch.object(main, "get_db_connection", return_value=connection):
            count = main.suppress_uncertain_pending_batch(messages)

        self.assertEqual(count, 1)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(len(connection.cursor_instance.calls), 3)
        update_sql = connection.cursor_instance.calls[0][0]
        system_sql = connection.cursor_instance.calls[1][0]
        audit_sql = connection.cursor_instance.calls[2][0]
        self.assertIn("recovery_state='suppressed_uncertain'", update_sql)
        self.assertIn("side_effects_completed_at IS NULL", update_sql)
        self.assertIn("INSERT INTO chat_history", system_sql)
        self.assertIn("INSERT INTO audit_log", audit_sql)


class SchedulingToolStateTests(unittest.TestCase):
    def test_unavailable_slot_is_not_confirmed_and_draft_returns_to_need_time(self):
        observed = {}

        class Chat:
            def __init__(self, config):
                self.config = config

            def send_message(self, _message):
                tool = next(
                    item
                    for item in self.config.tools
                    if item.__name__ == "book_my_appointment"
                )
                observed["result"] = tool()
                return SimpleNamespace(text=json.dumps(observed["result"]))

        def create_chat(**kwargs):
            return Chat(kwargs["config"])

        draft = {
            "intent": "book",
            "patient_name": "Mona",
            "service_area": "ليزر",
            "requested_date": "2026-09-15",
            "requested_time": "7:00 PM",
            "stage": "ready_to_book",
        }
        saved = {}

        def save(_phone, updated):
            saved.update(updated)
            return {"phone_number": PHONE, **updated}

        client = SimpleNamespace(chats=SimpleNamespace(create=create_chat))
        with mock.patch.object(main, "gemini_client", client), mock.patch.object(
            main, "load_chat_history", return_value=[]
        ), mock.patch.object(main, "get_live_instructions", return_value="Instructions"), mock.patch.object(
            main,
            "book_appointment",
            return_value={"ok": False, "code": "slot_unavailable", "message": "Taken"},
        ), mock.patch.object(main, "save_booking_draft", side_effect=save), mock.patch.object(
            main, "clear_booking_draft"
        ) as clear:
            main.generate_ai_reply_sync(
                PHONE,
                "احجزي",
                {"name": "Mona", "preferences": "", "tags": []},
                booking_draft=draft,
            )

        self.assertFalse(observed["result"]["ok"])
        self.assertEqual(observed["result"]["code"], "slot_unavailable")
        self.assertEqual(saved["requested_time"], "")
        self.assertEqual(saved["stage"], "need_time")
        clear.assert_not_called()

    def test_confirmed_book_cancel_and_reschedule_clear_draft(self):
        scenarios = (
            (
                "book_my_appointment",
                {},
                "book_appointment",
                {"ok": True, "code": "booked"},
                "booked",
            ),
            (
                "cancel_my_appointment",
                {"date": "2026-09-15", "time": "7:00 PM"},
                "cancel_appointment",
                {"ok": True, "code": "cancelled"},
                "cancelled",
            ),
            (
                "reschedule_my_appointment",
                {
                    "old_date": "2026-09-15",
                    "old_time": "7:00 PM",
                    "new_date": "2026-09-16",
                    "new_time": "8:00 PM",
                },
                "reschedule_appointment",
                {"ok": True, "code": "rescheduled"},
                "rescheduled",
            ),
        )
        draft = {
            "intent": "book",
            "patient_name": "Mona",
            "service_area": "ليزر",
            "requested_date": "2026-09-15",
            "requested_time": "7:00 PM",
            "stage": "ready_to_book",
        }
        for tool_name, tool_args, operation_name, result, reason in scenarios:
            with self.subTest(tool=tool_name):
                class Chat:
                    def __init__(self, config):
                        self.config = config

                    def send_message(self, _message):
                        tool = next(
                            item
                            for item in self.config.tools
                            if item.__name__ == tool_name
                        )
                        tool(**tool_args)
                        return SimpleNamespace(text="ok")

                client = SimpleNamespace(
                    chats=SimpleNamespace(create=lambda **kwargs: Chat(kwargs["config"]))
                )
                with mock.patch.object(main, "gemini_client", client), mock.patch.object(
                    main, "load_chat_history", return_value=[]
                ), mock.patch.object(
                    main, "get_live_instructions", return_value="Instructions"
                ), mock.patch.object(main, operation_name, return_value=result), mock.patch.object(
                    main, "clear_booking_draft"
                ) as clear:
                    main.generate_ai_reply_sync(
                        PHONE,
                        "نفذي الطلب",
                        {"name": "Mona", "preferences": "", "tags": []},
                        booking_draft=draft,
                    )
                clear.assert_called_once_with(PHONE, reason)

    def test_gemini_failure_returns_unconfirmed_safe_message(self):
        client = SimpleNamespace(
            chats=SimpleNamespace(create=mock.Mock(side_effect=RuntimeError("temporary")))
        )
        with mock.patch.object(main, "gemini_client", client), mock.patch.object(
            main, "load_chat_history", return_value=[]
        ), mock.patch.object(main, "get_live_instructions", return_value="Instructions"):
            reply, images = main.generate_ai_reply_sync(
                PHONE,
                "عايزة أحجز",
                {"name": "Mona", "preferences": "", "tags": []},
            )

        self.assertIn("مفيش أي حجز اتأكد", reply)
        self.assertEqual(images, [])


if __name__ == "__main__":
    unittest.main()
