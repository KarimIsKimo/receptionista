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

    def test_appointment_metadata_refresh_is_independent_and_preserves_view(self):
        self.assertIn("/admin/api/inbox/metadata?phones=", self.html)
        self.assertIn("expireLocalAppointmentMetadata", self.html)
        self.assertIn("now-fetchedAt>=600000", self.html)
        self.assertIn("expireLocalAppointmentMetadata();refreshInboxMetadata()", self.html)
        self.assertIn("scrollTop=list.scrollTop", self.html)
        self.assertIn("list.scrollTop=scrollTop", self.html)
        self.assertIn('state.filter==="booked"', self.html)
        self.assertIn('patient.appointment_status==="healthy"', self.html)
        self.assertIn("refreshAppointmentPatient", self.html)

    def test_mobile_visible_patient_actions_menu_exists(self):
        self.assertIn('id="mobileActionsBtn"', self.html)
        self.assertIn('id="mobileActionsDialog"', self.html)
        self.assertIn(".mobile-actions-trigger{display:grid!important}", self.html)
        for action in (
            'key:"toggle"', 'key:"profile"', 'key:"book"', 'key:"cancel"',
            'key:"reschedule"', 'key:"name"', 'key:"notes"', 'key:"tags"',
            'key:"unread"',
        ):
            self.assertIn(action, self.html)

    def test_origin_filters_badges_and_time_groups_exist(self):
        for expected in (
            '"patient_initiated"', '"reception_initiated"',
            "origin-badge", "conversation_origin", "patientStarted",
            "receptionStarted", "inboxTimeGroup", 'data-time-group=',
            '"today"', '"yesterday"', '"older"',
        ):
            self.assertIn(expected, self.html)

    def test_rename_updates_selected_profile_and_row_without_inbox_reload(self):
        self.assertIn('/admin/api/rename_patient', self.html)
        self.assertIn('await updatePatientUi({name:r.data.name})', self.html)
        self.assertIn('scrollTop=list.scrollTop', self.html)
        self.assertIn('list.scrollTop=scrollTop', self.html)
        rename_handler = re.search(
            r'if\(action==="name"\)(.*?)if\(action==="notes"\)',
            self.html,
        )
        self.assertIsNotNone(rename_handler)
        self.assertNotIn("loadInbox(true)", rename_handler.group(1))

    def test_mark_unread_uses_dedicated_cursor_action(self):
        self.assertIn('/unread",{}', self.html)
        self.assertIn("suppressAutoReadPhone", self.html)
        self.assertIn('data-action="unread"', self.html)
        self.assertIn('state.suppressAutoReadPhone===state.selected', self.html)

    def test_patient_controls_cover_all_requested_actions(self):
        for action in (
            'data-action="name"', 'data-action="notes"',
            'data-action="tags"', 'data-action="toggle"',
            'data-action="book"', 'data-action="cancel"',
            'data-action="reschedule"',
        ):
            self.assertIn(action, self.html)

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
