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


class AdminControlEndpointTests(unittest.TestCase):
    def test_operating_mode_endpoint_requires_known_explicit_mode(self):
        with mock.patch.object(main, "set_operating_mode", return_value="HUMAN") as setter:
            result = main.api_set_operating_mode(main.OperatingModeReq(mode="HUMAN"), admin="owner")
        self.assertTrue(result["ok"])
        setter.assert_called_once_with("HUMAN", "owner")

        with mock.patch.object(main, "set_operating_mode", side_effect=ValueError("invalid")):
            rejected = main.api_set_operating_mode(main.OperatingModeReq(mode="AUTOMATIC"), admin="owner")
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["code"], "invalid_operating_mode")

    def test_rename_endpoint_reuses_patient_update_and_returns_clean_name(self):
        with mock.patch.object(main, "update_patient_file") as update, mock.patch.object(
            main, "audit"
        ) as audit:
            result = main.api_rename_patient(
                main.RenamePatientReq(
                    phone_number="01012345678",
                    name="  Mona Ali  ",
                ),
                admin="tester",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["name"], "Mona Ali")
        update.assert_called_once_with("201012345678", name="Mona Ali")
        audit.assert_called_once_with(
            "tester", "rename_patient", "201012345678", "Mona Ali"
        )

    def test_rename_endpoint_rejects_whitespace_only_name(self):
        with mock.patch.object(main, "update_patient_file") as update:
            result = main.api_rename_patient(
                main.RenamePatientReq(phone_number="01012345678", name="   "),
                admin="tester",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_name")
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
