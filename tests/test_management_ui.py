"""Real-browser regressions; install Playwright + Chromium to run locally.

CI runs these with a browser. CHROMIUM_EXECUTABLE optionally points to an already
installed test browser. No application dependency is required for these tests.
"""
import functools
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlparse


class ManagementSourceTests(unittest.TestCase):
    def test_management_is_not_an_extra_mobile_nav_item(self):
        html = pathlib.Path("static/admin.html").read_text()
        nav = re.search(r'<nav class="nav">(.*?)</nav>', html, re.S).group(1)
        self.assertNotIn('data-view="management"', nav)
        self.assertNotIn('data-view="health"', nav)
        self.assertEqual(nav.count("data-view="), 4)
        self.assertIn('id="bulkBar" class="bulk-bar hidden"', html)

    def test_display_controls_are_css_driven_and_accessible(self):
        css = pathlib.Path("static/management.css").read_text()
        html = pathlib.Path("static/admin.html").read_text()
        for text in ("--text-scale", "data-density=compact", "data-theme=dark", "data-contrast=high", "prefers-reduced-motion:reduce", ":focus-visible", "data-touch=large", "data-focus-mode=true"):
            self.assertIn(text, css)
        self.assertNotRegex(html, r"font-size:\d+px")
        self.assertIn('aria-labelledby="dialogTitle"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('formnovalidate', html)

    def test_javascript_syntax_and_no_browser_confirmation_dialogs(self):
        js = pathlib.Path("static/management.js").read_text()
        self.assertNotRegex(js, r"\b(?:alert|confirm)\(")
        if not shutil.which("node"):
            self.skipTest("Node unavailable")
        result = subprocess.run(["node", "--check", "static/management.js"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


class ManagementBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Install test-only Playwright to run browser tests")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(
                executable_path=os.getenv("CHROMIUM_EXECUTABLE") or None,
                args=["--no-sandbox", "--disable-gpu"],
            )
        except Exception as exc:
            cls.playwright.stop()
            if os.getenv("REQUIRE_BROWSER_TESTS"):
                raise
            raise unittest.SkipTest(f"Chromium unavailable: {str(exc).splitlines()[0]}")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(pathlib.Path.cwd())))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/static/admin.html"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.page = self.context.new_page()
        self.errors, self.calls = [], []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.patients = [{"phone_number": f"2010000{i:05}", "name": f"Patient {i}", "preferences": "Keep exact notes", "tags": ["historical free tag"], "last_message_id": i+1, "last_message_role": "user", "last_message": "Patient message unchanged", "last_message_at": "2026-09-19T08:00:00+03:00", "waiting_since": "2026-09-19T08:00:00+03:00", "needs_reply": True, "is_paused": True, "unread_count": 1, "conversation_origin": "patient", "appointment_status": "unknown"} for i in range(60)]
        self.page.route("**/admin/api/**", self.route_api)
        self.page.goto(self.url)
        self.page.wait_for_selector(".patient-card")

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def route_api(self, route):
        request = route.request
        self.calls.append((request.method, request.url, request.post_data))
        path = urlparse(request.url).path
        if path == "/admin/api/inbox":
            data = {"patients": self.patients, "server_cursor": 60, "next_cursor": None}
        elif path.endswith("/inbox/updates"):
            data = {"patients": [], "server_cursor": 60, "has_more": False}
        elif path.endswith("/inbox/metadata"):
            data = {"patients": []}
        elif path.endswith("/messages"):
            after = parse_qs(urlparse(request.url).query).get("after_id")
            data = {"messages": [] if after else [{"id": i+1, "role": "user" if i%2==0 else "staff", "content": "Exact patient conversation "*4, "created_at": "2026-09-19T08:00:00+03:00"} for i in range(60)], "has_more": False}
        elif path.endswith("/appointments"):
            data = {"appointments": [], "upcoming": [], "previous": [], "snapshot_status": "unknown"}
        elif path.endswith("/read"):
            data = {"last_read_message_id": 60, "unread_count": 0}
        elif "/patient/" in path:
            data = dict(self.patients[0])
        elif path.endswith("/management/ai"):
            data = {"human_handled": 30, "ai_assigned": 20, "needs_staff_reply": 10, "global_active": True}
        elif path.endswith("/settings"):
            data = {"instruction": "Existing live instructions must remain unchanged."}
        elif path.endswith("/management/tags"):
            data = {"tags": [{"id": 1, "name": "VIP", "is_archived": False}, {"id": 2, "name": "Old catalog tag", "is_archived": True}]}
        elif path.endswith("/management/clinic"):
            data = {"read_only": True, "branch": "Nasr City", "address": "Existing clinic", "timezone": "Africa/Cairo", "opening_time": "12:00", "closing_time": "22:00", "closed_weekdays": [4], "slot_interval_minutes": 30, "appointment_duration_minutes": None, "booking_horizon_days": 180, "services": None}
        elif path.endswith("/management/activity"):
            data = {"audit": [{"id":1,"actor":"admin","action":"patient_pause_on","phone_number":self.patients[0]["phone_number"],"created_at":"2026-09-19T08:00:00+03:00"}], "next_cursor": None}
        elif path.endswith("/system-health"):
            data = {"overall": "unknown", "checked_at":"2026-09-19T08:00:00+03:00", "components": {k:{"status":"healthy" if k in {"database","scheduling"} else "unknown", "detail":"Do not render raw credentials=SECRET"} for k in ("database","scheduling","gemini","whatsapp")}}
        elif path.endswith("/management/bulk"):
            phones = json.loads(request.post_data)["phones"]
            data = {"results": [{"phone_number":p,"ok":True,"code":"updated"} for p in phones], "updated":len(phones)}
        else:
            data = {}
        route.fulfill(json={"ok":True,"code":"test","data":data})

    def management(self, section):
        self.page.click('.nav [data-view="settings"]')
        self.page.click(f'[data-management="{section}"]')

    def test_display_preferences_persist_scale_density_contrast_theme_and_rtl(self):
        p = self.page
        self.assertEqual(p.get_attribute("html", "lang"), "ar")
        self.management("accessibility")
        p.select_option("#textSize", "extra-large")
        p.select_option("#density", "compact")
        p.select_option("#theme", "dark")
        for key in ("highContrast", "reducedMotion", "largeTouch", "focusMode"):
            p.check("#"+key)
        p.click("#mobileLang")
        self.assertEqual(p.get_attribute("html","dir"),"ltr")
        p.reload()
        self.assertEqual(p.get_attribute("html","lang"),"en")
        self.assertEqual(p.get_attribute("html","data-theme"),"dark")
        self.assertEqual(p.get_attribute("html","data-density"),"compact")
        self.assertEqual(p.get_attribute("html","data-contrast"),"high")
        self.assertEqual(p.get_attribute("html","data-focus-mode"),"true")
        self.assertGreater(float(p.eval_on_selector("html","e=>parseFloat(getComputedStyle(e).fontSize)")),20)
        self.assertEqual(p.eval_on_selector("#chatPane","e=>getComputedStyle(e).transitionDuration"),"0s")
        prefs=p.evaluate('JSON.parse(localStorage.getItem("jothen-display-v1"))')
        self.assertTrue(prefs["largeTouch"])
        p.click("#mobileLang")
        self.assertEqual(p.get_attribute("html","dir"),"rtl")

    def test_system_theme_and_browser_reduced_motion(self):
        self.page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        self.page.wait_for_function('document.documentElement.dataset.theme==="dark"')
        self.assertEqual(self.page.eval_on_selector("#chatPane","e=>getComputedStyle(e).transitionDuration"),"0s")
        self.page.emulate_media(color_scheme="light", reduced_motion="no-preference")
        self.page.wait_for_function('document.documentElement.dataset.theme==="light"')

    def test_switching_display_preserves_selected_patient_reply_and_scroll(self):
        p=self.page
        p.set_viewport_size({"width":1440,"height":900})
        p.locator(".patient-card").first.click()
        p.wait_for_selector(".message")
        p.fill("#replyText","Unsent staff reply لا تترجم")
        p.evaluate('document.querySelector("#patientList").scrollTop=320;document.querySelector("#messages").scrollTop=400')
        before=p.evaluate('({inbox:document.querySelector("#patientList").scrollTop,chat:document.querySelector("#messages").scrollTop,name:document.querySelector("#chatName").textContent})')
        self.management("accessibility")
        p.select_option("#textSize","large")
        p.check("#focusMode")
        p.click("#mobileLang")
        p.click('.nav [data-view="inbox"]')
        p.wait_for_timeout(100)
        self.assertEqual(p.input_value("#replyText"),"Unsent staff reply لا تترجم")
        self.assertEqual(p.text_content("#chatName"),before["name"])
        self.assertAlmostEqual(p.eval_on_selector("#patientList","e=>e.scrollTop"),before["inbox"],delta=2)
        self.assertAlmostEqual(p.eval_on_selector("#messages","e=>e.scrollTop"),before["chat"],delta=2)

    def test_mobile_management_no_overflow_and_keyboard_dialog(self):
        p=self.page
        p.set_viewport_size({"width":320,"height":740})
        for language in ("ar","en"):
            if p.get_attribute("html","lang")!=language:p.click("#mobileLang")
            for section in ("ai","clinic","tags","activity","system","data","accessibility"):
                self.management(section)
                self.assertFalse(p.evaluate('document.documentElement.scrollWidth>innerWidth'), section)
        self.management("system")
        p.wait_for_selector(".health-card")
        self.assertNotIn("SECRET",p.text_content("#healthGrid"))
        self.management("tags")
        p.click("#createTag")
        self.assertEqual(p.get_attribute("#actionDialog","aria-labelledby"),"dialogTitle")
        self.assertIsNotNone(p.locator('label[for="actionField-name"]').text_content())
        p.keyboard.press("Escape")
        self.assertFalse(p.eval_on_selector("#actionDialog","e=>e.open"))
        p.keyboard.press("Tab")
        self.assertTrue(p.evaluate('getComputedStyle(document.activeElement).outlineStyle!=="none"'))

    def test_bulk_is_explicit_confirmed_and_one_request(self):
        p=self.page
        self.assertFalse(p.is_visible("#bulkBar"))
        self.assertEqual(p.locator(".selection-mark").count(),0)
        p.click("#selectModeBtn")
        p.locator(".patient-card").nth(0).click()
        p.locator(".patient-card").nth(1).click()
        p.select_option("#bulkAction","pause")
        p.click("#applyBulk")
        self.assertTrue(p.eval_on_selector("#actionDialog","e=>e.open"))
        self.assertFalse(any("/management/bulk" in c[1] for c in self.calls))
        p.click("#dialogConfirm")
        p.wait_for_function('!document.getElementById("actionDialog").open')
        calls=[c for c in self.calls if "/management/bulk" in c[1]]
        self.assertEqual(len(calls),1)
        self.assertEqual(len(json.loads(calls[0][2])["phones"]),2)
        self.assertEqual(p.locator('.patient-card[aria-pressed="true"]').count(),0)

    def test_activity_filters_and_tag_picker_keep_patient_text(self):
        p=self.page
        self.management("activity")
        p.fill('#auditFilters [name="actor"]',"karim")
        p.select_option("#auditAction","patient_pause_on")
        p.fill('#auditFilters [name="start"]',"2026-09-18")
        p.click("#refreshAudit")
        p.wait_for_selector("#auditList article")
        self.assertTrue(any("actor=karim" in c[1] and "action=patient_pause_on" in c[1] and "start=2026-09-18" in c[1] for c in self.calls))
        p.click('.nav [data-view="inbox"]')
        p.locator(".patient-card").first.click()
        p.wait_for_selector(".message")
        p.click("#mobileActionsBtn")
        p.click('[data-mobile-action="tags"]')
        p.click('[data-pick-tag="1"]')
        self.assertEqual(p.input_value('[name="tags"]'),"historical free tag, VIP")
        self.assertEqual(p.locator('[data-pick-tag="2"]').count(),0)
        p.keyboard.press("Escape")


if __name__ == "__main__":
    unittest.main()
