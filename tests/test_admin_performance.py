import contextlib
import datetime as dt
import inspect
import os
import unittest
from unittest import mock


os.environ.setdefault("DATABASE_URL", "postgresql://example.invalid/test")
os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test-whatsapp-token")
os.environ.setdefault("APPS_SCRIPT_URL", "https://example.invalid/apps-script")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

with mock.patch("google.genai.Client"):
    import main  # noqa: E402


class SummaryCursor:
    """Small state model that validates the values sent to the summary upsert."""

    def __init__(self):
        self.rows = {}

    def execute(self, sql, params):
        self.assert_summary_sql(sql)
        phone, message_id, role, content, created_at = params[:5]
        state = self.rows.setdefault(
            phone,
            {
                "first_meaningful_role": None,
                "latest_patient_message_id": 0,
                "latest_response_message_id": 0,
                "unread_count": 0,
            },
        )
        state.update(
            last_message_id=message_id,
            last_message_role=role,
            last_message=content,
            last_message_at=created_at,
        )
        if state["first_meaningful_role"] is None and role in {"user", "staff"}:
            state["first_meaningful_role"] = role
        if role == "user":
            state["latest_patient_message_id"] = message_id
            state["unread_count"] += 1
        if role in {"model", "staff"}:
            state["latest_response_message_id"] = message_id

    def assert_summary_sql(self, sql):
        assert "conversation_summary_message" in sql
        assert "latest_patient_message_id" in sql
        assert "latest_response_message_id" in sql
        assert "change_version=nextval" in sql


class SaveCursor:
    def __init__(self, events):
        self.events = events
        self.saved = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params):
        if "INSERT INTO chat_history" in sql:
            self.events.append("message")
            self.saved = {
                "id": 91,
                "role": params[1],
                "content": params[2],
                "whatsapp_message_id": params[3],
                "created_at": dt.datetime.now(dt.timezone.utc),
            }
        elif "conversation_summary_message" in sql:
            self.events.append("summary")
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.saved


class SaveConnection:
    def __init__(self, events):
        self.events = events
        self.cursor_instance = SaveCursor(events)

    def cursor(self, **_):
        return self.cursor_instance

    def commit(self):
        self.events.append("commit")


class InitCursor:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.statements.append(sql)


class InitConnection:
    def __init__(self):
        self.cursor_instance = InitCursor()

    def cursor(self, **_):
        return self.cursor_instance

    def commit(self):
        pass


class AdminPerformanceTests(unittest.TestCase):
    def test_user_model_and_staff_turns_advance_summary_state(self):
        cursor = SummaryCursor()
        timestamp = dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc)
        for message_id, role, content in (
            (1, "user", "hello"),
            (2, "model", "AI reply"),
            (3, "staff", "human reply"),
        ):
            main.update_conversation_summary_for_message(
                cursor,
                "2010",
                {
                    "id": message_id,
                    "role": role,
                    "content": content,
                    "created_at": timestamp + dt.timedelta(seconds=message_id),
                },
            )

        state = cursor.rows["2010"]
        self.assertEqual(state["last_message_id"], 3)
        self.assertEqual(state["last_message_role"], "staff")
        self.assertEqual(state["first_meaningful_role"], "user")
        self.assertEqual(state["latest_patient_message_id"], 1)
        self.assertEqual(state["latest_response_message_id"], 3)
        self.assertEqual(state["unread_count"], 1)

    def test_message_and_summary_are_committed_in_one_transaction(self):
        events = []
        connection = SaveConnection(events)

        @contextlib.contextmanager
        def borrow():
            yield connection

        with mock.patch.object(main, "get_db_connection", side_effect=borrow):
            saved = main.save_chat_turn("2010", "staff", "confirmed", "wamid.1")

        self.assertEqual(saved["id"], 91)
        self.assertEqual(events, ["message", "summary", "commit"])

    def test_schema_backfill_is_idempotent_and_indexes_match_access_paths(self):
        connection = InitConnection()

        @contextlib.contextmanager
        def borrow():
            yield connection

        with mock.patch.object(main, "get_db_connection", side_effect=borrow):
            main.init_db()

        schema = "\n".join(connection.cursor_instance.statements)
        self.assertIn("conversation_summary_backfill", schema)
        self.assertIn("conversation_summary_backfill_v1", schema)
        self.assertIn("WHERE existing.phone_number IS NULL", schema)
        self.assertIn("ON CONFLICT(phone_number) DO NOTHING", schema)
        self.assertIn("idx_chat_phone_id_desc", schema)
        self.assertIn("idx_chat_user_phone_id", schema)
        self.assertIn("idx_chat_origin_phone_id", schema)
        self.assertIn("idx_conversation_summary_changes", schema)

    def test_pool_is_closed_by_application_lifespan(self):
        source = inspect.getsource(main.lifespan)
        self.assertIn("database_pool.start()", source)
        self.assertIn("database_pool.close()", source)
        self.assertIn("finally", source)


if __name__ == "__main__":
    unittest.main()
