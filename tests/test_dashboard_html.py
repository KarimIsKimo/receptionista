import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


class DashboardHtmlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = pathlib.Path("static/admin.html").read_text(encoding="utf-8")

    def test_uses_incremental_inbox_and_message_polling(self):
        self.assertIn("/admin/api/inbox?limit=100", self.html)
        self.assertIn("&after_id=", self.html)
        self.assertIn("/messages?limit=100&after_id=", self.html)

    def test_required_operations_views_and_states_exist(self):
        for required in (
            'id="view-inbox"', 'id="view-appointments"',
            'id="view-analytics"', 'id="view-health"',
            'id="view-settings"',
            "clinic_closed", "unavailable", ".message.system",
        ):
            self.assertIn(required, self.html)

    def test_bilingual_and_responsive_controls_exist(self):
        self.assertIn('dir="rtl"', self.html)
        self.assertIn('id="langBtn"', self.html)
        self.assertIn("@media(max-width:760px)", self.html)
        self.assertIn('id="backChat"', self.html)

    def test_read_cursor_comes_from_displayed_messages(self):
        self.assertIn("displayed_message_id:cursor", self.html)
        self.assertIn("markDisplayedRead(state.messageCursor)", self.html)
        self.assertNotIn('/read",{method:"POST"}', self.html)

    def test_message_scroll_behavior_preserves_viewport(self):
        self.assertIn("renderMessages({prepend:true})", self.html)
        self.assertIn("else if(options.prepend)", self.html)
        self.assertIn('id="newMessageBtn"', self.html)
        self.assertIn("isNearBottom()", self.html)

    def test_settings_and_audit_are_restored(self):
        self.assertIn("/admin/api/settings", self.html)
        self.assertIn("/admin/api/audit?limit=50", self.html)
        self.assertIn('id="systemInstruction"', self.html)

    def test_schedule_actions_include_exact_slot_target(self):
        self.assertIn("time:slot.time", self.html)
        self.assertIn("appointment_id:appointmentId(a)", self.html)

    def test_embedded_javascript_has_valid_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        scripts = re.findall(r"<script>([\s\S]*?)</script>", self.html)
        self.assertEqual(len(scripts), 1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(scripts[0])
            path = handle.name
        result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        pathlib.Path(path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
