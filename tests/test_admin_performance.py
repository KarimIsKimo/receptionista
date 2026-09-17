import contextlib
import datetime as dt
import inspect
import os
import threading
import time
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
                "is_archived": False,
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
            state["is_archived"] = False
        if role in {"model", "staff"}:
            state["latest_response_message_id"] = message_id

    def assert_summary_sql(self, sql):
        assert "conversation_summary_message" in sql
        assert "latest_patient_message_id" in sql
        assert "latest_response_message_id" in sql
        assert "is_archived=CASE" in sql
        assert "change_version=EXCLUDED.change_version" in sql


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

    def test_inbound_patient_message_reopens_archived_summary_transactionally(self):
        cursor = SummaryCursor()
        cursor.rows["2010"] = {
            "first_meaningful_role": "user",
            "latest_patient_message_id": 5,
            "latest_response_message_id": 9,
            "unread_count": 0,
            "is_archived": True,
        }

        main.update_conversation_summary_for_message(
            cursor,
            "2010",
            {
                "id": 10,
                "role": "user",
                "content": "hello again",
                "created_at": dt.datetime.now(dt.timezone.utc),
            },
        )

        state = cursor.rows["2010"]
        self.assertFalse(state["is_archived"])
        self.assertGreater(
            state["latest_patient_message_id"],
            state["latest_response_message_id"],
        )
        self.assertEqual(state["unread_count"], 1)

    def test_non_patient_messages_do_not_reopen_archived_summary(self):
        for role in ("model", "system", "staff"):
            with self.subTest(role=role):
                cursor = SummaryCursor()
                cursor.rows["2010"] = {
                    "first_meaningful_role": "user",
                    "latest_patient_message_id": 5,
                    "latest_response_message_id": 5,
                    "unread_count": 0,
                    "is_archived": True,
                }
                main.update_conversation_summary_for_message(
                    cursor,
                    "2010",
                    {
                        "id": 10,
                        "role": role,
                        "content": "internal/outbound",
                        "created_at": dt.datetime.now(dt.timezone.utc),
                    },
                )
                self.assertTrue(cursor.rows["2010"]["is_archived"])

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
        self.assertIn("CREATE TABLE IF NOT EXISTS conversation_summary_clock", schema)
        self.assertIn("CREATE OR REPLACE FUNCTION next_conversation_summary_version", schema)
        self.assertIn("SET version=version+1", schema)
        self.assertIn("change_version,updated_at", schema)
        self.assertIn("COALESCE(unread.count,0),0,NOW()", schema)
        self.assertNotIn("conversation_summary_change_seq", schema)
        self.assertNotIn("nextval(", schema)

    def test_every_inbox_mutation_uses_the_transactional_clock(self):
        sources = "\n".join(
            inspect.getsource(target)
            for target in (
                main.update_patient_file,
                main.set_patient_preferences,
                main.set_extracted_patient_memory,
                main.set_patient_pause,
                main.set_patient_tags,
                main.touch_conversation_summary,
                main.update_conversation_summary_for_message,
                main.api_create_conversation,
                main.api_conversation_state,
                main.AdminOperations.mark_read,
                main.AdminOperations.mark_unread,
                main.AdminOperations._cache_appointments,
                main.AdminOperations._mark_snapshot_status,
            )
        )
        self.assertNotIn("nextval(", sources)
        self.assertNotIn("conversation_summary_change_seq", sources)
        self.assertIn("change_version=EXCLUDED.change_version", sources)
        for target in (
            main.update_patient_file,
            main.set_patient_preferences,
            main.set_extracted_patient_memory,
            main.set_patient_pause,
            main.set_patient_tags,
        ):
            source = inspect.getsource(target)
            self.assertIn("WITH patient AS", source)
            self.assertIn("INSERT INTO conversation_summaries", source)
        for target in (
            main.AdminOperations.mark_read,
            main.AdminOperations.mark_unread,
        ):
            source = inspect.getsource(target)
            self.assertIn("UPDATE conversation_summary_clock", source)
            self.assertIn("change_version=clock.version", source)
        for target in (
            main.AdminOperations._cache_appointments,
            main.AdminOperations._mark_snapshot_status,
        ):
            self.assertIn(
                "change_version=EXCLUDED.change_version",
                inspect.getsource(target),
            )

    def test_transactional_clock_allocation_is_commit_order_serialized(self):
        class ClockRow:
            def __init__(self):
                self.version = 0
                self.committed = 0
                self.row_lock = threading.Lock()

            @contextlib.contextmanager
            def transaction(self):
                with self.row_lock:
                    self.version += 1
                    allocated = self.version
                    yield allocated
                    self.committed = allocated

        clock = ClockRow()
        first_allocated = threading.Event()
        allow_first_commit = threading.Event()
        second_allocated = threading.Event()
        allow_second_commit = threading.Event()
        observations = []

        def first_transaction():
            with clock.transaction() as version:
                observations.append(("first_allocated", version))
                first_allocated.set()
                allow_first_commit.wait(1)
            observations.append(("first_committed", clock.committed))

        def second_transaction():
            first_allocated.wait(1)
            with clock.transaction() as version:
                observations.append(("second_allocated", version))
                second_allocated.set()
                allow_second_commit.wait(1)
            observations.append(("second_committed", clock.committed))

        first = threading.Thread(target=first_transaction)
        second = threading.Thread(target=second_transaction)
        first.start()
        second.start()
        self.assertTrue(first_allocated.wait(0.5))
        time.sleep(0.03)
        self.assertFalse(second_allocated.is_set())
        self.assertEqual(clock.committed, 0)

        allow_first_commit.set()
        self.assertTrue(second_allocated.wait(0.5))
        self.assertEqual(clock.committed, 1)
        self.assertIn(("second_allocated", 2), observations)
        allow_second_commit.set()
        first.join(1)
        second.join(1)

        self.assertEqual(clock.committed, 2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

    def test_pool_is_closed_by_application_lifespan(self):
        source = inspect.getsource(main.lifespan)
        self.assertIn("database_pool.start()", source)
        self.assertIn("database_pool.close()", source)
        self.assertIn("finally", source)


if __name__ == "__main__":
    unittest.main()
