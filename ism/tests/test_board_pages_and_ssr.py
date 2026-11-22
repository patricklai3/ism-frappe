# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""SSR, RBAC, security headers, Socket.IO bundling, and asset manifest tests for /board and /cockpit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import frappe
from frappe import app as frappe_app
from frappe.auth import CookieManager
from frappe.website.serve import get_response
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

import ism.www.board as board_page
import ism.www.cockpit as cockpit_page
from ism.tests.base import ISMTestCase


class TestBoardAndCockpitPagesSSR(ISMTestCase):
	"""Verify /board and /cockpit SSR controllers, role gates, headers, and vendored Socket.IO assets."""

	def setUp(self) -> None:
		super().setUp()
		self._saved_user = frappe.session.user
		self._had_request = hasattr(frappe.local, "request")
		self._saved_request = getattr(frappe.local, "request", None)
		self._had_cookie_manager = hasattr(frappe.local, "cookie_manager")
		self._saved_cookie_manager = getattr(frappe.local, "cookie_manager", None)
		self._had_response_headers = hasattr(frappe.local, "response_headers")
		self._saved_response_headers = getattr(frappe.local, "response_headers", None)
		self._saved_redirect = getattr(frappe.local.flags, "redirect_location", None)

	def tearDown(self) -> None:
		try:
			frappe.set_user("Administrator")
			if self._saved_redirect is None:
				frappe.local.flags.pop("redirect_location", None)
			else:
				frappe.local.flags.redirect_location = self._saved_redirect

			if self._had_response_headers:
				frappe.local.response_headers = self._saved_response_headers
			elif hasattr(frappe.local, "response_headers"):
				delattr(frappe.local, "response_headers")

			if self._had_cookie_manager:
				frappe.local.cookie_manager = self._saved_cookie_manager
			elif hasattr(frappe.local, "cookie_manager"):
				delattr(frappe.local, "cookie_manager")

			if self._had_request:
				frappe.local.request = self._saved_request
			elif hasattr(frappe.local, "request"):
				delattr(frappe.local, "request")
		finally:
			frappe.set_user(self._saved_user)
			super().tearDown()

	def _create_user_with_roles(self, prefix: str, roles: list[str]) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.test"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": prefix,
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": r} for r in roles],
			}
		).insert(ignore_permissions=True)
		self.track(user)
		return email

	def _attach_request(self, path: str) -> None:
		builder = EnvironBuilder(path=path, method="GET", base_url="https://shop.example.test")
		frappe.local.request = Request(builder.get_environ())
		frappe.local.cookie_manager = CookieManager()
		frappe.local.response_headers = {}

	def _assert_security_headers(self, headers: Any) -> None:
		self.assertEqual(headers.get("X-Frame-Options"), "DENY")
		self.assertEqual(headers.get("Content-Security-Policy"), "frame-ancestors 'none'")
		self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
		self.assertEqual(headers.get("Cache-Control"), "no-store, private")

	def test_guest_redirects_to_login_on_board_and_cockpit(self) -> None:
		frappe.set_user("Guest")

		self._attach_request("/board?thread=RO%3ARO-TEST-001")
		ctx_board = frappe._dict()
		with self.assertRaises(frappe.Redirect):
			board_page.get_context(ctx_board)
		self.assertIn("/login?redirect-to=", frappe.local.flags.redirect_location or "")
		self.assertIn("%2Fboard", frappe.local.flags.redirect_location or "")
		self.assertIn("RO-TEST-001", frappe.local.flags.redirect_location or "")
		self._assert_security_headers(ctx_board.response_headers)

		frappe.local.flags.pop("redirect_location", None)
		self._attach_request("/cockpit")
		ctx_cockpit = frappe._dict()
		with self.assertRaises(frappe.Redirect):
			cockpit_page.get_context(ctx_cockpit)
		self.assertEqual(frappe.local.flags.redirect_location, "/login?redirect-to=/cockpit")
		self._assert_security_headers(ctx_cockpit.response_headers)

	def test_unauthorized_user_restricted_on_board_and_redirected_from_cockpit(self) -> None:
		non_ism_user = self._create_user_with_roles("board_unauth", ["Guest"])
		frappe.set_user(non_ism_user)

		self._attach_request("/board")
		ctx_board = frappe._dict()
		board_page.get_context(ctx_board)
		self.assertFalse(ctx_board.authorized)
		self.assertEqual(ctx_board.actor_role, "UNAUTHORIZED")
		self.assertFalse(ctx_board.can_access_cockpit)
		self.assertEqual(ctx_board.boot_json, "{}")
		self.assertTrue(bool(ctx_board.asset_version))
		self._assert_security_headers(ctx_board.response_headers)

		self._attach_request("/cockpit")
		ctx_cockpit = frappe._dict()
		with self.assertRaises(frappe.Redirect):
			cockpit_page.get_context(ctx_cockpit)
		self.assertEqual(frappe.local.flags.redirect_location, "/board")

	def test_technician_authorized_on_board_and_redirected_from_cockpit(self) -> None:
		tech_user = self._create_user_with_roles("board_tech_ssr", ["ISM Technician"])
		frappe.set_user(tech_user)

		self._attach_request("/board")
		ctx_board = frappe._dict()
		board_page.get_context(ctx_board)
		self.assertTrue(ctx_board.authorized)
		self.assertEqual(ctx_board.actor_role, "TECH")
		self.assertFalse(ctx_board.can_approve)
		self.assertFalse(ctx_board.can_access_cockpit)
		self.assertTrue(bool(ctx_board.asset_version))
		boot = json.loads(ctx_board.boot_json)
		self.assertEqual(boot["actor_role"], "TECH")
		self.assertFalse(boot["can_access_cockpit"])
		self.assertIn("socketio_port", boot)
		self._assert_security_headers(ctx_board.response_headers)

		self._attach_request("/cockpit")
		ctx_cockpit = frappe._dict()
		with self.assertRaises(frappe.Redirect):
			cockpit_page.get_context(ctx_cockpit)
		self.assertEqual(frappe.local.flags.redirect_location, "/board")

	def test_manager_and_advisor_live_website_response_headers_assets_and_socketio(self) -> None:
		advisor_user = self._create_user_with_roles("board_adv_ssr", ["ISM Service Advisor"])
		manager_user = self._create_user_with_roles("board_mgr_ssr", ["ISM Manager"])

		# Service Advisor SSR checks for both /board and /cockpit
		frappe.set_user(advisor_user)
		self._attach_request("/cockpit")
		ctx_adv_cockpit = frappe._dict()
		cockpit_page.get_context(ctx_adv_cockpit)
		self.assertEqual(ctx_adv_cockpit.user_role_label, "Service Advisor")
		self.assertTrue(bool(ctx_adv_cockpit.asset_version))
		self._assert_security_headers(ctx_adv_cockpit.response_headers)

		# Live Frappe HTTP responses as Manager
		frappe.set_user(manager_user)
		self._attach_request("/board")
		resp_board = get_response("board")
		self.assertEqual(resp_board.status_code, 200)
		frappe_app.process_response(resp_board)
		self._assert_security_headers(resp_board.headers)
		board_html = resp_board.get_data(as_text=True)
		self.assertIn('<meta name="referrer" content="no-referrer" />', board_html.split("</head>", 1)[0])
		self.assertIn("window.ISM_BOARD_BOOT =", board_html)
		self.assertIn("/assets/ism/board/vendor/socket.io.min.js?v=", board_html)
		self.assertIn("/assets/ism/board/board.css?v=", board_html)
		self.assertIn("/assets/ism/board/app.js?v=", board_html)
		# Active-RO-only UI markers present; legacy singleton channels & intake/AP triggers absent
		self.assertIn("Action Feed", board_html)
		self.assertIn("Active ROs", board_html)
		self.assertIn("Copy Selected for Agent", board_html)
		for forbidden in (
			"SHOP:INTAKE",
			"SHOP:AP_RECEIVING",
			"SHOP:DISPATCH",
			"+ Intake",
			"+ New Intake",
			"Receive / Vendor Bill",
			"Shop Channels",
		):
			self.assertNotIn(forbidden, board_html)

		self._attach_request("/cockpit")
		resp_cockpit = get_response("cockpit")
		self.assertEqual(resp_cockpit.status_code, 200)
		frappe_app.process_response(resp_cockpit)
		self._assert_security_headers(resp_cockpit.headers)
		cockpit_html = resp_cockpit.get_data(as_text=True)
		self.assertIn('<meta name="referrer" content="no-referrer" />', cockpit_html.split("</head>", 1)[0])
		self.assertIn("window.ISM_COCKPIT_BOOT =", cockpit_html)
		self.assertIn("/assets/ism/board/vendor/socket.io.min.js?v=", cockpit_html)
		self.assertIn("/assets/ism/board/board.css?v=", cockpit_html)
		self.assertIn("/assets/ism/cockpit/cockpit.css?v=", cockpit_html)
		self.assertIn("/assets/ism/cockpit/cockpit.js?v=", cockpit_html)
		self.assertIn("Action Queue", cockpit_html)
		self.assertIn("Internal Time (T)", cockpit_html)
		for forbidden in (
			"SHOP:INTAKE",
			"SHOP:AP_RECEIVING",
			"SHOP:DISPATCH",
			"+ Intake",
			"+ New Intake",
			"Receive / Vendor Bill",
			"Shop Channels",
		):
			self.assertNotIn(forbidden, cockpit_html)

	def test_board_and_cockpit_public_assets_exist_and_non_empty(self) -> None:
		ism_root = Path(frappe.get_app_path("ism"))
		required_assets = [
			ism_root / "www" / "board.py",
			ism_root / "www" / "board.html",
			ism_root / "www" / "cockpit.py",
			ism_root / "www" / "cockpit.html",
			ism_root / "public" / "board" / "vendor" / "socket.io.min.js",
			ism_root / "public" / "board" / "board.css",
			ism_root / "public" / "board" / "api.js",
			ism_root / "public" / "board" / "realtime.js",
			ism_root / "public" / "board" / "capture.js",
			ism_root / "public" / "board" / "cards.js",
			ism_root / "public" / "board" / "app.js",
			ism_root / "public" / "cockpit" / "cockpit.css",
			ism_root / "public" / "cockpit" / "cockpit.js",
		]
		for path in required_assets:
			self.assertTrue(path.is_file(), f"Missing Shop OS asset: {path}")
			self.assertGreater(path.stat().st_size, 0, f"Empty Shop OS asset: {path}")

		socketio_text = (ism_root / "public" / "board" / "vendor" / "socket.io.min.js").read_text(
			encoding="utf-8", errors="replace"
		)
		self.assertIn("Socket.IO v4", socketio_text)
