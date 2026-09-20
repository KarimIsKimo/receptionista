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
        self.assertIn("/admin/api/inbox/updates?after_version=", self.html)
        self.assertIn("state.cursor", self.html)
        self.assertIn("/messages?limit=100&after_id=", self.html)

    def test_inbox_load_more_uses_composite_keyset_cursor(self):
        self.assertIn('url+="&before="+encodeURIComponent(state.before)', self.html)
        self.assertIn("state.before=r.data.next_cursor", self.html)
        self.assertNotIn('url+="&before_id="+state.before', self.html)

    def test_filtered_empty_page_keeps_load_older_until_cursor_exhaustion(self):
        fallback = re.search(
            r"function inboxFallbackHtml\(\)(.*?)function bindLoadOlder",
            self.html,
            re.S,
        )
        self.assertIsNotNone(fallback)
        self.assertIn("state.before", fallback.group(1))
        self.assertIn('id="morePatients"', fallback.group(1))
        self.assertIn('tr("emptyInbox")', fallback.group(1))
        self.assertIn(
            "el.innerHTML=inboxFallbackHtml();bindLoadOlder()",
            self.html,
        )
        self.assertIn(
            "findIndex(function(x){return x.phone_number===row.phone_number})",
            self.html,
        )

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

    def test_language_defaults_to_arabic_and_uses_one_dictionary(self):
        self.assertIn('var I18N={', self.html)
        self.assertNotIn('var AR={', self.html)
        self.assertNotIn('var EN={', self.html)
        self.assertIn(
            'lang:normalizeLanguage(JothenManagement.readStorage("jothen-lang"))',
            self.html,
        )
        self.assertIn('function normalizeLanguage(value){return value==="en"?"en":"ar"}', self.html)
        self.assertIn('<html lang="ar" dir="rtl">', self.html)

    def test_language_switches_both_ways_and_persists(self):
        self.assertIn('setLanguage(state.lang==="ar"?"en":"ar")', self.html)
        self.assertIn('JothenManagement.writeStorage("jothen-lang",state.lang)', self.html)
        self.assertIn('document.documentElement.lang=state.lang', self.html)
        self.assertIn('document.documentElement.dir=state.lang==="ar"?"rtl":"ltr"', self.html)
        self.assertIn('switchLanguage:"English"', self.html)
        self.assertIn('switchLanguage:"العربية"', self.html)

    def test_language_switch_preserves_receptionist_work_state(self):
        switcher = re.search(
            r"function setLanguage\(lang\)(.*?)function changeView",
            self.html,
            re.S,
        )
        self.assertIsNotNone(switcher)
        code = switcher.group(1)
        for expected in (
            'inboxTop=patientList.scrollTop',
            'chatTop=messages.scrollTop',
            'reply=q("replyText").value',
            'search=q("inboxSearch").value',
            'q("replyText").value=reply',
            'q("inboxSearch").value=search',
            'patientList.scrollTop=inboxTop',
            'messages.scrollTop=chatTop',
        ):
            self.assertIn(expected, code)
        self.assertNotIn('.click()', code)

    def test_mobile_language_control_is_visible_compact_and_translated(self):
        self.assertIn('id="mobileLang"', self.html)
        self.assertIn('class="btn small language-toggle"', self.html)
        self.assertIn('.topbar-actions .language-toggle{display:inline-flex', self.html)
        self.assertIn('max-width:82px', self.html)
        self.assertIn('.topbar-actions .btn:not(.language-toggle){display:none}', self.html)
        for english_label in (
            'needs_reply:"Needs Reply"',
            'waiting_for_patient:"Waiting for Patient"',
            'human:"Human takeover"',
            'patientStarted:"Patient started"',
            'newConversation:"+ New conversation"',
            'patientActions:"Patient actions"',
        ):
            self.assertIn(english_label, self.html)

    def test_language_switch_retranslates_dynamic_controls_and_dialogs(self):
        self.assertIn('if(state.selected)renderChatHeader()', self.html)
        self.assertIn('if(state.scheduleState)renderSchedulePanel()', self.html)
        self.assertIn('data-field-label=', self.html)
        self.assertIn('dlg.dataset.titleKey=titleKey', self.html)
        self.assertIn('if(q("mobileActionsDialog").open)renderMobileActionsMenu()', self.html)
        self.assertIn('data-metric-key=', self.html)
        self.assertIn('data-status-key=', self.html)

    def test_patient_content_is_rendered_verbatim_not_translated(self):
        self.assertIn('esc(m.content)', self.html)
        self.assertIn('esc(p.preferences||"—")', self.html)
        self.assertIn("(p.tags||[]).map(function(x){return '<span class=\"tag\">'+esc(x)", self.html)
        self.assertNotIn('tr(m.content)', self.html)
        self.assertNotIn('tr(p.preferences)', self.html)

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
        management = pathlib.Path("static/management.js").read_text(encoding="utf-8")
        self.assertIn("/admin/api/management/activity?limit=50", management)
        self.assertIn('id="systemInstruction"', self.html)

    def test_human_first_supervisor_workspace_and_attention_queue_exist(self):
        management = pathlib.Path("static/management.js").read_text(encoding="utf-8")
        css = pathlib.Path("static/management.css").read_text(encoding="utf-8")
        for marker in (
            'id="supervisorStrip"', 'data-management="supervisor"',
            'id="operatingModes"', 'data-mode="HUMAN"',
            'data-mode="AI_BACKUP"', 'data-mode="AI_ACTIVE"',
            'id="attentionList"', 'id="refreshSupervisor"',
        ):
            self.assertIn(marker, self.html)
        self.assertIn('/admin/api/supervisor/summary', management)
        self.assertIn('/admin/api/supervisor/attention', management)
        self.assertIn('/admin/api/management/operating-mode', management)
        self.assertIn('openAction("activateMode"', management)
        self.assertIn('@media(max-width:760px)', css)
        self.assertIn('.mode-grid{grid-template-columns:1fr}', css)

    def test_supervisor_dynamic_text_is_bilingual(self):
        management = pathlib.Path("static/management.js").read_text(encoding="utf-8")
        for marker in (
            'supervisor:"Supervisor"', 'supervisor:"الإشراف"',
            'needsReply:"Needs reply"', 'needsReply:"تحتاج رد"',
            'appointmentSyncStatus:"Appointment sync"',
            'appointmentSyncStatus:"مزامنة المواعيد"',
            'resolveAttention:"Resolve"', 'resolveAttention:"تم الحل"',
        ):
            self.assertIn(marker, management)

    def test_schedule_actions_include_exact_slot_target(self):
        self.assertIn("time:slot.time", self.html)
        self.assertIn("appointment_id:appointmentId(a)", self.html)

    def test_appointment_metadata_refresh_is_independent_and_preserves_view(self):
        self.assertIn("/admin/api/inbox/metadata?phones=", self.html)
        self.assertIn("expireLocalAppointmentMetadata", self.html)
        self.assertIn("now-fetchedAt>=600000", self.html)
        self.assertIn("expireLocalAppointmentMetadata();refreshInboxMetadata()", self.html)
        self.assertIn("scrollTop=el.scrollTop", self.html)
        self.assertIn("el.scrollTop=scrollTop", self.html)
        self.assertIn('state.filter==="booked"', self.html)
        self.assertIn("snapshotIsFresh(p)", self.html)
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
        self.assertIn('optimisticPatientUpdate(phone,{name:d.name}', self.html)
        self.assertIn('await updatePatientUi({name:r.data.name},phone)', self.html)
        self.assertIn('scrollTop=el.scrollTop', self.html)
        self.assertIn('el.scrollTop=scrollTop', self.html)
        rename_handler = re.search(
            r'if\(action==="name"\)(.*?)if\(action==="notes"\)',
            self.html,
        )
        self.assertIsNotNone(rename_handler)
        self.assertNotIn("loadInbox(true)", rename_handler.group(1))

    def test_conversation_renders_before_appointment_refresh(self):
        select_handler = re.search(
            r"async function selectPatient\(phone\)(.*?)async function refreshSelectedAppointments",
            self.html,
            re.S,
        )
        self.assertIsNotNone(select_handler)
        code = select_handler.group(1)
        self.assertIn("Promise.all([", code)
        self.assertIn("renderMessages({initial:true})", code)
        self.assertIn("refreshSelectedAppointments(phone,token)", code)
        self.assertNotIn('"/appointments")])', code)

    def test_rapid_switches_cancel_and_ignore_stale_patient_requests(self):
        self.assertIn("state.selectionController.abort()", self.html)
        self.assertIn("state.appointmentController.abort()", self.html)
        self.assertIn("token!==state.selectionToken", self.html)
        self.assertIn('e.name!=="AbortError"', self.html)
        self.assertIn("state.profile.phone_number===selected", self.html)
        self.assertIn("if(state.selected!==phone)return", self.html)

    def test_polling_is_adaptive_hidden_aware_and_non_overlapping(self):
        self.assertIn("state.pollingInbox", self.html)
        self.assertIn("state.pollingMessages", self.html)
        self.assertIn("state.loadingInbox||state.pollingInbox", self.html)
        self.assertIn("||state.pollingMessages)return", self.html)
        self.assertIn("function schedulePolling", self.html)
        self.assertIn("document.hidden?30000", self.html)

    def test_incremental_updates_patch_rows_without_full_list_replacement(self):
        updater = re.search(
            r"function patchInboxRows\(rows\)(.*?)async function loadInbox",
            self.html,
            re.S,
        )
        self.assertIsNotNone(updater)
        code = updater.group(1)
        self.assertIn("existing.replaceWith(fresh)", code)
        self.assertIn("el.scrollTop=scrollTop", code)
        self.assertNotIn("renderPatients()", code)

    def test_needs_reply_new_conversation_pin_and_archive_controls_exist(self):
        for expected in (
            '"needs_reply"', '"archived"', 'id="newConversationBtn"',
            "/admin/api/conversations", 'data-action="pin"',
            'data-action="archive"', "/conversation-state",
            "waiting_since", "last_message_role",
        ):
            self.assertIn(expected, self.html)

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
