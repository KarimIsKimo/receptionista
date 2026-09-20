import contextlib
import asyncio
import datetime as dt
import io
import unittest
from unittest import mock

from receptionist.management import (
    AUDIT_ACTIONS,
    BulkRequest,
    ManagementOperations,
    clinic_configuration,
    csv_cell,
    valid_phone,
)


class FakeCursor:
    def __init__(self, state):
        self.state = state
        self.row = None
        self.rows = []

    def __enter__(self): return self
    def __exit__(self, *_): return False

    def execute(self, sql, params=()):
        self.state["sql"].append((" ".join(sql.split()), params))
        if "FROM management_tags" in sql and "FOR SHARE" in sql:
            tag = self.state.get("tag")
            self.row = {"name": tag} if tag else None
        elif "FROM patients" in sql and "FOR UPDATE" in sql:
            self.rows = [dict(phone_number=p, tags=list(v.get("tags", []))) for p, v in self.state["patients"].items() if p in params[0]]
        elif "FROM conversation_summary_clock" in sql and "FOR UPDATE" in sql:
            self.row = {"version": self.state["version"]}
        elif "RETURNING latest_patient_message_id" in sql:
            self.state["version"] += 1
            self.state["summary_versions"].append(self.state["version"])
            self.row = {"latest_patient_message_id": 12, "unread_count": 0}
        elif "UPDATE patients SET is_paused" in sql:
            paused, phone = params
            self.state["patients"][phone]["is_paused"] = paused
        elif "UPDATE patients SET tags" in sql:
            tag, _, phone = params
            if tag not in self.state["patients"][phone]["tags"]:
                self.state["patients"][phone]["tags"].append(tag)
        elif "INSERT INTO audit_log" in sql:
            self.state["audit"].append(params)

    def fetchone(self): return self.row
    def fetchall(self): return self.rows


class FakeConnection:
    def __init__(self, state):
        self.state = state
        self.commits = self.rollbacks = 0
    def cursor(self, **_): return FakeCursor(self.state)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


class ManagementTests(unittest.TestCase):
    def make_ops(self, patients=None, tag="VIP"):
        state = {"patients": patients or {}, "tag": tag, "version": 40,
                 "summary_versions": [], "sql": [], "audit": []}
        conn = FakeConnection(state)
        @contextlib.contextmanager
        def borrow():
            try: yield conn
            except Exception:
                conn.rollback()
                raise
        return ManagementOperations(borrow, mock.Mock()), state, conn

    def test_bulk_is_one_transaction_and_assigns_distinct_summary_versions(self):
        phones = {"201000000001": {"tags": []}, "201000000002": {"tags": ["old free-form"]}}
        ops, state, conn = self.make_ops(phones)
        result = ops.bulk(BulkRequest(phones=list(phones), action="pause"), "admin")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["updated"], 2)
        self.assertEqual(conn.commits, 1)
        self.assertEqual(state["version"], 42)
        self.assertEqual(state["summary_versions"], [41, 42])
        compact_sql = [sql for sql, _ in state["sql"]]
        patient_lock = next(i for i, sql in enumerate(compact_sql) if "FROM patients" in sql and "FOR UPDATE" in sql)
        clock_lock = next(i for i, sql in enumerate(compact_sql) if "FROM conversation_summary_clock" in sql and "FOR UPDATE" in sql)
        first_summary = next(i for i, sql in enumerate(compact_sql) if "INSERT INTO conversation_summaries" in sql)
        self.assertLess(patient_lock, clock_lock)
        self.assertLess(clock_lock, first_summary)
        self.assertFalse(any("next_conversation_summary_version() AS version" in sql for sql in compact_sql))
        self.assertTrue(all(p["is_paused"] for p in state["patients"].values()))
        self.assertEqual(len(state["audit"]), 2)

    def test_bulk_add_tag_preserves_historical_free_form_tags(self):
        phone = "201000000001"
        ops, state, _ = self.make_ops({phone: {"tags": ["legacy custom"]}})
        result = ops.bulk(BulkRequest(phones=[phone], action="add_tag", tag_id=1), "admin")
        self.assertTrue(result["ok"])
        self.assertEqual(state["patients"][phone]["tags"], ["legacy custom", "VIP"])

    def test_bulk_rejects_one_bad_phone_before_any_write(self):
        ops, state, conn = self.make_ops({"201000000001": {"tags": []}})
        result = ops.bulk(BulkRequest(phones=["201000000001", "not-a-phone"], action="archive"), "admin")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_phone")
        self.assertFalse(state["sql"])
        self.assertEqual(conn.commits, 0)

    def test_missing_catalog_tag_rolls_back_without_mutation(self):
        ops, state, conn = self.make_ops({"201000000001": {"tags": ["legacy"]}}, tag=None)
        result = ops.bulk(BulkRequest(phones=["201000000001"], action="add_tag", tag_id=5), "admin")
        self.assertFalse(result["ok"])
        self.assertEqual(conn.rollbacks, 1)
        self.assertEqual(state["patients"]["201000000001"]["tags"], ["legacy"])

    def test_activity_is_allowlisted_and_never_selects_details(self):
        calls = []
        def db(sql, params=(), **kwargs):
            calls.append((sql, params, kwargs)); return []
        result = ManagementOperations(mock.Mock(), db).activity(actor="admin", action="rename_patient", start=dt.date(2026, 9, 1), end=dt.date(2026, 9, 2))
        self.assertTrue(result["ok"])
        self.assertNotIn("details", calls[0][0].lower())
        self.assertIn(list(AUDIT_ACTIONS), calls[0][1])
        self.assertEqual(calls[0][1][1:3], ("admin", "rename_patient"))

    def test_invalid_audit_action_does_not_query_database(self):
        db = mock.Mock()
        result = ManagementOperations(mock.Mock(), db).activity(action="raw_internal_event")
        self.assertFalse(result["ok"])
        db.assert_not_called()

    def test_clinic_configuration_is_truthful_read_only_allowlist(self):
        data = clinic_configuration()
        self.assertTrue(data["read_only"])
        self.assertEqual(data["timezone"], "Africa/Cairo")
        self.assertIsNone(data["services"])
        self.assertIsNone(data["appointment_duration_minutes"])
        for secret in ("token", "key", "url", "password", "database"):
            self.assertNotIn(secret, data)

    def test_csv_cells_prevent_spreadsheet_formulas(self):
        for value in ("=cmd()", "+SUM(A1)", " -2", "@IMPORT", "\tformula"):
            self.assertTrue(csv_cell(value).startswith("'"), value)
        self.assertEqual(csv_cell("normal patient note"), "normal patient note")

    def test_phone_normalization_is_strict(self):
        self.assertEqual(valid_phone("010 0000-0001"), "201000000001")
        with self.assertRaises(ValueError): valid_phone("patient201000000001")

    def test_export_excludes_internal_fields_and_streams_safe_csv(self):
        calls = []
        row = {"phone_number":"201000000001", "name":"=Mona", "tags":["legacy"],
               "preferences":"@private", "created_at":dt.date(2026, 9, 1), "last_message_at":None}
        def db(sql, params=(), **kwargs):
            calls.append(sql)
            if "MAX(phone_number)" in sql: return {"phone":"201000000001"}
            if "FROM patients p" in sql: return [row] if params[0] == "" else []
            return None
        response = ManagementOperations(mock.Mock(), db).export("patients", "admin")
        async def consume():
            return b"".join([chunk async for chunk in response.body_iterator])
        content = asyncio.run(consume()).decode("utf-8-sig")
        self.assertIn("phone,name,tags,notes_preferences,created_date,last_activity", content)
        self.assertIn("'=Mona", content)
        self.assertIn("'@private", content)
        self.assertFalse(any("chat_history" in sql for sql in calls))


if __name__ == "__main__":
    unittest.main()
