# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""SSR, Dual-Path Cookie Exchange, Zero-PII, and Asset Manifest tests for Wave 3B Customer Portal."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import frappe
from frappe.auth import CookieManager
from frappe.utils import add_days, today
from frappe.website.serve import get_response_content
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request, Response

from ism.api.approval import get_or_create_approval_token
from ism.comms.contracts import DEFAULT_PORTAL_COOKIE_NAME
from ism.portal.auth import clear_portal_auth_rate_limits, create_booking_portal_link
from ism.templates.pages.quote_approval import get_context as get_quote_approval_context
from ism.tests.base import ISMTestCase
from ism.www.portal import get_context as get_portal_page_context


def _extract_cookie_value(set_cookie_headers: list[str], cookie_name: str) -> str | None:
	prefix = f"{cookie_name}="
	for header in set_cookie_headers:
		if header.startswith(prefix):
			return header[len(prefix) :].split(";", 1)[0]
	return None


class TestPortalPagesAndSSR(ISMTestCase):
	"""Verify /portal and /quote-approval SSR shells, zero-PII guarantees, cookie exchange, and assets."""

	_SETTINGS_FIELDS = (
		"enable_customer_portal",
		"portal_brand_name",
		"portal_support_phone",
		"portal_pickup_hours_text",
		"portal_session_cookie_name",
		"portal_session_cookie_samesite",
		"portal_session_secure_cookie",
		"portal_booking_token_ttl_days",
	)

	def _save_settings(self, updates: dict) -> None:
		settings = frappe.get_single("ISM Settings")
		for k, v in updates.items():
			if hasattr(settings, k):
				setattr(settings, k, v)
		settings.flags.ignore_permissions = True
		settings.save()

	def setUp(self):
		super().setUp()
		self._saved_user = frappe.session.user
		self._had_request = hasattr(frappe.local, "request")
		self._saved_request = getattr(frappe.local, "request", None)
		self._had_cookie_manager = hasattr(frappe.local, "cookie_manager")
		self._saved_cookie_manager = getattr(frappe.local, "cookie_manager", None)
		self._saved_settings = {}
		settings = frappe.get_single("ISM Settings")
		for fieldname in self._SETTINGS_FIELDS:
			self._saved_settings[fieldname] = getattr(settings, fieldname, None)

		self._save_settings(
			{
				"enable_customer_portal": 1,
				"portal_brand_name": "ISM Test Auto Clinic",
				"portal_support_phone": "416-555-0100",
				"portal_pickup_hours_text": "Mon-Fri 8:00 AM - 6:00 PM",
				"portal_session_cookie_name": DEFAULT_PORTAL_COOKIE_NAME,
				"portal_session_cookie_samesite": "Lax",
				"portal_session_secure_cookie": 1,
				"portal_booking_token_ttl_days": 30,
			}
		)
		clear_portal_auth_rate_limits()

	def tearDown(self):
		try:
			super().tearDown()
		finally:
			frappe.set_user(self._saved_user)
			if self._had_cookie_manager:
				frappe.local.cookie_manager = self._saved_cookie_manager
			elif hasattr(frappe.local, "cookie_manager"):
				delattr(frappe.local, "cookie_manager")
			if self._had_request:
				frappe.local.request = self._saved_request
			elif hasattr(frappe.local, "request"):
				delattr(frappe.local, "request")
			clear_portal_auth_rate_limits()
			self._save_settings(self._saved_settings)
			frappe.clear_cache(doctype="ISM Settings")
			if hasattr(frappe.db, "value_cache"):
				frappe.db.value_cache.clear()

	def _attach_cookie_manager(self) -> CookieManager:
		cm = CookieManager()
		frappe.local.cookie_manager = cm
		return cm

	def _flush_set_cookie_headers(self, cm: CookieManager) -> list[str]:
		response = Response("ok")
		cm.flush_cookies(response)
		return response.headers.getlist("Set-Cookie")

	def _attach_request(self, cookie_value: str | None = None, path: str = "/portal") -> None:
		headers = {"User-Agent": "ISM-Portal-SSR-Test/1.0"}
		if cookie_value:
			headers["Cookie"] = f"{DEFAULT_PORTAL_COOKIE_NAME}={cookie_value}"
		builder = EnvironBuilder(
			path=path,
			method="GET",
			base_url="https://portal.example.com",
			headers=headers,
			environ_base={"REMOTE_ADDR": "198.51.100.55"},
		)
		frappe.local.request = Request(builder.get_environ())

	def _create_seeded_visit(self):
		customer = self.create_test_customer(
			customer_name="Secret SSR Customer 991",
			mobile_no="+14165550191",
		)
		vehicle = self.create_test_vehicle(
			customer=customer.name,
			vin="5YJ3E1EA1NF991122",
			license_plate="SSR-991",
			model="Model 3",
			model_year=2024,
		)
		ro = self.create_test_repair_order(
			customer=customer.name,
			vehicle=vehicle.name,
			status="Pending Authorization",
			services=[
				{
					"service_title": "Brake Fluid Flush",
					"status": "Pending Approval",
					"labor_hours": 1.0,
					"labor_rate": 160.0,
					"labor_amount": 160.0,
					"total_amount": 160.0,
				}
			],
		)
		se = self.create_test_schedule_entry(
			customer=customer.name,
			vehicle=vehicle.name,
			repair_order=ro.name,
			scheduled_date=add_days(today(), 3),
			scheduled_time="09:00:00",
			estimated_duration=60,
			status="Scheduled",
			provisional_customer_name="Secret SSR Customer 991",
			provisional_phone="+14165550191",
		)
		return customer, vehicle, ro, se

	def test_unauthenticated_portal_ssr_renders_branding_and_zero_pii(self):
		customer, vehicle, ro, se = self._create_seeded_visit()
		self._attach_request(cookie_value=None)
		self._attach_cookie_manager()

		with patch.object(frappe, "form_dict", frappe._dict()):
			ctx = frappe._dict()
			get_portal_page_context(ctx)
			self.assertFalse(ctx.portal_boot["authenticated"])
			self.assertIsNone(ctx.portal_boot["active_ro"])
			self.assertIsNone(ctx.portal_boot["active_schedule_entry"])
			self.assertEqual(ctx.portal_boot["shop"]["company_name"], "ISM Test Auto Clinic")

			html = get_response_content("portal")

		self.assertIn("ISM Test Auto Clinic", html)
		self.assertIn("/assets/ism/portal/portal.css", html)
		self.assertIn("/assets/ism/portal/app.js", html)
		self.assertIn('name="ism-csrf-token"', html)
		self.assertIn('id="ism-portal-root"', html)

		for secret in (
			customer.customer_name,
			"+14165550191",
			"4165550191",
			vehicle.vin,
			vehicle.license_plate,
			ro.name,
			se.name,
			"signature_image",
			"/private/files/",
		):
			self.assertNotIn(secret, html)

	def test_portal_ignores_non_string_query_params_safely(self):
		self._attach_request(cookie_value=None)
		self._attach_cookie_manager()

		with patch.object(
			frappe,
			"form_dict",
			frappe._dict(
				{
					"token": {"!=": ""},
					"booking_token": ["injected"],
					"ro": {"!=": ""},
				}
			),
		):
			ctx = frappe._dict()
			get_portal_page_context(ctx)
			self.assertFalse(ctx.portal_boot["authenticated"])
			self.assertIsNone(ctx.portal_boot["active_ro"])
			self.assertIsNone(ctx.portal_boot["active_schedule_entry"])

	def test_portal_path1_ro_token_sets_cookie_zero_otp_and_omits_raw_token(self):
		_customer, _vehicle, ro, _se = self._create_seeded_visit()
		token, _url = get_or_create_approval_token(ro.name)

		self._attach_request(cookie_value=None)
		cm = self._attach_cookie_manager()

		with patch.object(frappe, "form_dict", frappe._dict({"token": token})):
			ctx = frappe._dict()
			get_portal_page_context(ctx)
			html = get_response_content("portal")

		self.assertTrue(ctx.portal_boot["authenticated"])
		self.assertTrue(ctx.portal_boot["should_strip_query_token"])
		self.assertEqual(ctx.portal_boot["active_ro"], ro.name)
		self.assertNotIn(token, html)

		headers = self._flush_set_cookie_headers(cm)
		cookie_val = _extract_cookie_value(headers, DEFAULT_PORTAL_COOKIE_NAME)
		self.assertTrue(cookie_val)
		self.assertTrue(any("HttpOnly" in h and "SameSite=Lax" in h for h in headers))

		# Path 3: Returning visit with cookie auto-restores authenticated boot state
		self._attach_request(cookie_value=cookie_val)
		self._attach_cookie_manager()
		with patch.object(frappe, "form_dict", frappe._dict()):
			ctx_return = frappe._dict()
			get_portal_page_context(ctx_return)

		self.assertTrue(ctx_return.portal_boot["authenticated"])
		self.assertEqual(ctx_return.portal_boot["active_ro"], ro.name)
		self.assertFalse(ctx_return.portal_boot["should_strip_query_token"])

	def test_portal_path1_booking_token_sets_cookie_and_invalid_token_has_zero_pii(self):
		customer, vehicle, ro, se = self._create_seeded_visit()
		booking_url = create_booking_portal_link(se.name)
		booking_token = parse_qs(urlparse(booking_url).query)["booking_token"][0]

		self._attach_request(cookie_value=None)
		cm = self._attach_cookie_manager()

		with patch.object(frappe, "form_dict", frappe._dict({"booking_token": booking_token})):
			ctx = frappe._dict()
			get_portal_page_context(ctx)
			html = get_response_content("portal")

		self.assertTrue(ctx.portal_boot["authenticated"])
		self.assertTrue(ctx.portal_boot["should_strip_query_token"])
		self.assertEqual(ctx.portal_boot["active_schedule_entry"], se.name)
		self.assertNotIn(booking_token, html)

		headers = self._flush_set_cookie_headers(cm)
		self.assertTrue(_extract_cookie_value(headers, DEFAULT_PORTAL_COOKIE_NAME))

		# Invalid token renders generic error and zero PII
		self._attach_request(cookie_value=None)
		self._attach_cookie_manager()
		with patch.object(frappe, "form_dict", frappe._dict({"token": "definitely-invalid-token"})):
			bad_ctx = frappe._dict()
			get_portal_page_context(bad_ctx)
			bad_html = get_response_content("portal")

		self.assertFalse(bad_ctx.portal_boot["authenticated"])
		self.assertTrue(bad_ctx.portal_boot["token_error"])
		for secret in (customer.customer_name, vehicle.vin, ro.name, se.name):
			self.assertNotIn(secret, bad_html)

	def test_quote_approval_upgrade_sets_cookie_preserves_contract_and_forbids_img_tags(self):
		_customer, _vehicle, ro, _se = self._create_seeded_visit()
		token, _url = get_or_create_approval_token(ro.name)

		self._attach_request(cookie_value=None)
		cm = self._attach_cookie_manager()

		with patch.object(frappe, "form_dict", frappe._dict({"token": token})):
			ctx = frappe._dict()
			get_quote_approval_context(ctx)
			html = get_response_content("quote_approval")

		self.assertEqual(ctx.token, token)
		self.assertTrue(ctx.approval_data["valid"])
		self.assertTrue(ctx.portal_boot["authenticated"])
		self.assertEqual(ctx.portal_boot["initial_section"], "quote")
		self.assertEqual(ctx.portal_boot["active_ro"], ro.name)
		self.assertTrue(ctx.portal_boot["should_strip_query_token"])
		self.assertNotIn("token", ctx.portal_boot)

		headers = self._flush_set_cookie_headers(cm)
		self.assertTrue(_extract_cookie_value(headers, DEFAULT_PORTAL_COOKIE_NAME))

		self.assertIn(ro.name, html)
		self.assertNotIn(token, html)
		self.assertNotIn("signature_image", html)
		self.assertNotIn("/private/files/", html)

		template_path = frappe.get_app_path("ism", "templates", "pages", "quote_approval.html")
		with open(template_path, encoding="utf-8") as tmpl_file:
			tmpl_src = tmpl_file.read()
		self.assertNotIn("signature_image", tmpl_src)
		self.assertNotIn("/private/files/", tmpl_src)
		self.assertNotIn("<img", tmpl_src.lower())

		# Type-confusion safety preserved
		with patch.object(frappe, "form_dict", frappe._dict({"token": {"!=": ""}})):
			bad_ctx = frappe._dict()
			get_quote_approval_context(bad_ctx)
			self.assertEqual(bad_ctx.token, "")
			self.assertFalse(bad_ctx.approval_data["valid"])
			self.assertFalse(bad_ctx.portal_boot["authenticated"])

	def test_path1_exchange_commits_when_not_in_test_and_skips_commit_on_invalid_token(self):
		_customer, _vehicle, ro, se = self._create_seeded_visit()
		ro_token, _url = get_or_create_approval_token(ro.name)
		booking_url = create_booking_portal_link(se.name)
		booking_token = parse_qs(urlparse(booking_url).query)["booking_token"][0]

		prev_in_test = getattr(frappe.flags, "in_test", True)
		frappe.flags.in_test = False
		try:
			# Valid /portal?token=... commits when in_test=False
			self._attach_request(cookie_value=None, path="/portal")
			self._attach_cookie_manager()
			with (
				patch.object(frappe, "form_dict", frappe._dict({"token": ro_token})),
				patch.object(frappe.db, "commit") as mock_commit,
			):
				ctx = frappe._dict()
				get_portal_page_context(ctx)
				self.assertTrue(ctx.portal_boot["authenticated"])
				mock_commit.assert_called_once()

			# Valid /portal?booking_token=... commits when in_test=False
			self._attach_request(cookie_value=None, path="/portal")
			self._attach_cookie_manager()
			with (
				patch.object(frappe, "form_dict", frappe._dict({"booking_token": booking_token})),
				patch.object(frappe.db, "commit") as mock_commit,
			):
				ctx = frappe._dict()
				get_portal_page_context(ctx)
				self.assertTrue(ctx.portal_boot["authenticated"])
				mock_commit.assert_called_once()

			# Valid /quote-approval?token=... commits when in_test=False
			self._attach_request(cookie_value=None, path="/quote-approval")
			self._attach_cookie_manager()
			with (
				patch.object(frappe, "form_dict", frappe._dict({"token": ro_token})),
				patch.object(frappe.db, "commit") as mock_commit,
			):
				ctx = frappe._dict()
				get_quote_approval_context(ctx)
				self.assertTrue(ctx.portal_boot["authenticated"])
				mock_commit.assert_called_once()

			# Invalid token on /portal does NOT commit
			self._attach_request(cookie_value=None, path="/portal")
			self._attach_cookie_manager()
			with (
				patch.object(frappe, "form_dict", frappe._dict({"token": "bad-token-no-commit"})),
				patch.object(frappe.db, "commit") as mock_commit,
			):
				ctx = frappe._dict()
				get_portal_page_context(ctx)
				self.assertFalse(ctx.portal_boot["authenticated"])
				mock_commit.assert_not_called()

			# Invalid token on /quote-approval does NOT commit
			self._attach_request(cookie_value=None, path="/quote-approval")
			self._attach_cookie_manager()
			with (
				patch.object(frappe, "form_dict", frappe._dict({"token": "bad-token-no-commit"})),
				patch.object(frappe.db, "commit") as mock_commit,
			):
				ctx = frappe._dict()
				get_quote_approval_context(ctx)
				self.assertFalse(ctx.portal_boot["authenticated"])
				mock_commit.assert_not_called()
		finally:
			frappe.flags.in_test = prev_in_test

	def test_portal_public_asset_manifest_complete_and_non_empty(self):
		portal_public_dir = frappe.get_app_path("ism", "public", "portal")
		expected_files = (
			"portal.css",
			"api.js",
			"auth.js",
			"quote_pin.js",
			"reschedule.js",
			"payments.js",
			"invoices.js",
			"app.js",
		)
		for filename in expected_files:
			full_path = os.path.join(portal_public_dir, filename)
			self.assertTrue(os.path.isfile(full_path), msg=f"Missing portal asset: {filename}")
			self.assertGreater(os.path.getsize(full_path), 0, msg=f"Empty portal asset: {filename}")
