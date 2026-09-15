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


class PreferenceDB:
    def __init__(self, initial="A"):
        self.value = initial
        self.calls = []

    def __call__(self, sql, params=(), **kwargs):
        self.calls.append((sql, params))
        self.value = params[1]


class PatientPreferenceTests(unittest.TestCase):
    def test_admin_edit_replaces_existing_notes_exactly(self):
        database = PreferenceDB("A")
        with mock.patch.object(main, "db_execute", side_effect=database), mock.patch.object(
            main, "audit"
        ):
            result = main.api_patient_preferences(
                main.PatientPreferencesReq(phone_number="01012345678", preferences="B"),
                admin="tester",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(database.value, "B")
        sql, params = database.calls[0]
        self.assertIn("preferences=EXCLUDED.preferences", sql)
        self.assertNotIn("||", sql)
        self.assertEqual(params, ("201012345678", "B"))

    def test_repeated_admin_edits_do_not_accumulate_versions(self):
        database = PreferenceDB("A")
        with mock.patch.object(main, "db_execute", side_effect=database), mock.patch.object(
            main, "audit"
        ):
            for value in ("B", "C", "final notes"):
                main.api_patient_preferences(
                    main.PatientPreferencesReq(
                        phone_number="01012345678", preferences=value
                    ),
                    admin="tester",
                )
                self.assertEqual(database.value, value)

        self.assertEqual(database.value, "final notes")


if __name__ == "__main__":
    unittest.main()
