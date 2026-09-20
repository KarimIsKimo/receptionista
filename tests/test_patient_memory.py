import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock


os.environ.setdefault("DATABASE_URL", "postgresql://example.invalid/test")
os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test-whatsapp-token")
os.environ.setdefault("APPS_SCRIPT_URL", "https://example.invalid/apps-script")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

with mock.patch("google.genai.Client"):
    import main  # noqa: E402


class PatientMemoryPersistenceTests(unittest.TestCase):
    def test_existing_name_is_preserved_during_extraction(self):
        response = SimpleNamespace(
            text=json.dumps(
                {"name": "Different Name", "preferences": "Prefers evening visits"}
            )
        )
        client = mock.Mock()
        client.models.generate_content.return_value = response

        with mock.patch.object(main, "gemini_client", client), mock.patch.object(
            main,
            "load_recent_memory_conversation",
            return_value=[{"role": "user", "content": "Evenings work best"}],
        ), mock.patch.object(
            main,
            "set_extracted_patient_memory",
            return_value={
                "name": "Existing Name",
                "preferences": "Prefers evening visits",
            },
        ) as setter:
            result = main.extract_patient_memory_sync(
                "201000000000",
                {"name": "Existing Name", "preferences": ""},
            )

        setter.assert_called_once_with(
            "201000000000", "Existing Name", "Prefers evening visits"
        )
        self.assertEqual(result["name"], "Existing Name")
        request = client.models.generate_content.call_args.kwargs
        self.assertEqual(request["config"].response_mime_type, "application/json")
        self.assertEqual(
            request["config"].response_schema["required"],
            ["name", "preferences"],
        )
        payload = json.loads(request["contents"])
        self.assertEqual(payload["current_name"], "Existing Name")
        self.assertEqual(len(payload["recent_conversation"]), 1)

    def test_extracted_preferences_replace_instead_of_append(self):
        calls = []

        def fake_db(sql, params=(), **kwargs):
            calls.append((sql, params))

        with mock.patch.object(main, "db_execute", side_effect=fake_db):
            result = main.set_extracted_patient_memory(
                "01012345678", "", "Only the final clean preference"
            )

        sql, params = calls[0]
        self.assertIn("preferences = EXCLUDED.preferences", sql)
        self.assertNotIn("||", sql)
        self.assertEqual(
            params,
            ("201012345678", "", "Only the final clean preference"),
        )
        self.assertEqual(result["preferences"], "Only the final clean preference")

    def test_recent_memory_context_keeps_user_model_and_staff_turns(self):
        rows = [
            {"role": "staff", "content": "Staff"},
            {"role": "model", "content": "AI"},
            {"role": "user", "content": "Patient"},
        ]
        with mock.patch.object(main, "db_execute", return_value=rows) as database:
            history = main.load_recent_memory_conversation("01012345678", 99)

        sql, params = database.call_args.args
        self.assertIn("role IN ('user', 'model', 'staff')", sql)
        self.assertEqual(params, ("201012345678", 20))
        self.assertEqual(
            history,
            [
                {"role": "user", "content": "Patient"},
                {"role": "model", "content": "AI"},
                {"role": "staff", "content": "Staff"},
            ],
        )


class PatientMemoryWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_updates_before_pause_check_when_ai_is_active(self):
        events = []
        profile = {
            "phone_number": "201000000000",
            "name": "",
            "preferences": "",
            "is_paused": False,
        }

        def save_turn(*args, **kwargs):
            events.append("saved")
            return {"id": 1}

        async def update_memory(phone, current):
            events.append("memory")
            return {**current, "preferences": "Evenings"}

        def bot_active():
            events.append("bot_state")
            return True

        async def generate(phone, message, current, **kwargs):
            events.append("reply")
            self.assertEqual(current["preferences"], "Evenings")
            return "AI reply", []

        with mock.patch.object(main, "get_user_lock", new=mock.AsyncMock(return_value=asyncio.Lock())), mock.patch.object(
            main, "save_chat_turn", side_effect=save_turn
        ), mock.patch.object(main, "load_patient_profile", return_value=profile), mock.patch.object(
            main, "update_booking_draft_from_message", return_value=None
        ), mock.patch.object(
            main, "update_patient_memory", side_effect=update_memory
        ) as memory, mock.patch.object(
            main, "is_bot_globally_active", side_effect=bot_active
        ), mock.patch.object(
            main, "get_operating_mode", return_value="AI_ACTIVE"
        ), mock.patch.object(
            main, "generate_ai_reply", side_effect=generate
        ), mock.patch.object(
            main,
            "send_whatsapp_message",
            new=mock.AsyncMock(return_value={"ok": True, "message_id": "reply-1"}),
        ):
            await main.handle_ai_conversation(
                "201000000000", "Please remember that evenings work best", "phone-id", "message-1"
            )

        memory.assert_awaited_once()
        self.assertEqual(events[:4], ["saved", "memory", "bot_state", "reply"])

    async def test_paused_patient_memory_updates_without_ai_reply(self):
        events = []
        profile = {
            "phone_number": "201000000000",
            "name": "Patient",
            "preferences": "Old",
            "is_paused": True,
        }

        async def update_memory(phone, current):
            events.append("memory")
            return {**current, "preferences": "Rewritten"}

        generate = mock.AsyncMock(return_value=("must not send", []))
        send = mock.AsyncMock(return_value={"ok": True})
        with mock.patch.object(main, "get_user_lock", new=mock.AsyncMock(return_value=asyncio.Lock())), mock.patch.object(
            main, "save_chat_turn", return_value={"id": 1}
        ), mock.patch.object(main, "load_patient_profile", return_value=profile), mock.patch.object(
            main, "update_booking_draft_from_message", return_value=None
        ), mock.patch.object(
            main, "update_patient_memory", side_effect=update_memory
        ) as memory, mock.patch.object(
            main, "is_bot_globally_active", return_value=True
        ), mock.patch.object(
            main, "get_operating_mode", return_value="AI_ACTIVE"
        ), mock.patch.object(main, "generate_ai_reply", generate), mock.patch.object(
            main, "send_whatsapp_message", send
        ):
            await main.handle_ai_conversation(
                "201000000000", "Please remember evenings", "phone-id", "message-2"
            )

        memory.assert_awaited_once()
        self.assertEqual(events, ["memory"])
        generate.assert_not_awaited()
        send.assert_not_awaited()

    async def test_gemini_memory_failure_does_not_break_reply_processing(self):
        profile = {
            "phone_number": "201000000000",
            "name": "Patient",
            "preferences": "Existing",
            "is_paused": False,
        }
        send = mock.AsyncMock(return_value={"ok": True, "message_id": "reply-2"})

        with mock.patch.object(main, "get_user_lock", new=mock.AsyncMock(return_value=asyncio.Lock())), mock.patch.object(
            main, "save_chat_turn", return_value={"id": 1}
        ), mock.patch.object(main, "load_patient_profile", return_value=profile), mock.patch.object(
            main, "update_booking_draft_from_message", return_value=None
        ), mock.patch.object(
            main,
            "extract_patient_memory_sync",
            side_effect=RuntimeError("temporary Gemini failure"),
        ), mock.patch.object(
            main, "is_bot_globally_active", return_value=True
        ), mock.patch.object(
            main, "get_operating_mode", return_value="AI_ACTIVE"
        ), mock.patch.object(
            main, "generate_ai_reply", new=mock.AsyncMock(return_value=("AI reply", []))
        ) as generate, mock.patch.object(main, "send_whatsapp_message", send):
            await main.handle_ai_conversation(
                "201000000000", "Please remember that evenings work best", "phone-id", "message-3"
            )

        generate.assert_awaited_once()
        send.assert_awaited_once_with("201000000000", "AI reply", "phone-id")


if __name__ == "__main__":
    unittest.main()
