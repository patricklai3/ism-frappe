# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Tests for Project 4 Wave 2A: dual-path portal auth, booking links, OTP anti-enumeration, and scope guards."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.auth import CookieManager
from frappe.utils import add_to_date, now_datetime
from werkzeug.wrappers import Response

from ism.comms.contracts import DEFAULT_PORTAL_COOKIE_NAME
from ism.comms.otp import get_last_mock_otp
from ism.comms.providers import clear_mock_sms_outbox, get_mock_sms_outbox
from ism.comms.sessions import issue_portal_session
from ism.portal.auth import (
	assert_ro_mutating_unlocked,
	assert_session_owns_ro,
	assert_session_owns_schedule_entry,
	clear_portal_auth_rate_limits,
	create_booking_portal_link,
	exchange_booking_token,
	exchange_ro_approval_token,
	get_active_portal_session,
	get_public_portal_branding,
	is_ro_mutating_locked,
	list_accessible_visit_keys,
	request_portal_login_otp,
	require_portal_session,
	resolve_customer_phone_e164,
	resolve_subjects_for_phone,
	sign_out_portal_session,
	verify_portal_login_otp,
)
from ism.portal.contracts import find_forbidden_portal_keys, hash_portal_token
from ism.tests.base import ISMTestCase, retry_on_deadlock


class TestPortalAuthAndSessions(ISMTestCase):
	"""Verify Wave 2A dual-path authentication, anti-enumeration, booking links, and anti-IDOR scope guards."""

	_SETTINGS_FIELDS = (
		"sms_gateway_provider",
		"sms_default_country_code",
		"sms_enforce_opt_out",
		"otp_code_length",
		"otp_ttl_seconds",
		"otp_resend_cooldown_seconds",
		"otp_max_verify_attempts",
		"otp_lockout_seconds",
		"otp_phone_rate_limit_per_hour",
		"otp_ip_rate_limit_per_hour",
		"portal_session_cookie_name",
		"portal_session_cookie_samesite",
		"portal_session_secure_cookie",
		"enable_customer_portal",
		"portal_brand_name",
		"portal_support_phone",
		"portal_pickup_hours_text",
		"portal_booking_token_ttl_days",
	)

	def _save_settings(self, updates: dict) -> None:
		def _do_save():
			settings = frappe.get_single("ISM Settings")
			for fieldname, value in updates.items():
				setattr(settings, fieldname, value)
			settings.save(ignore_permissions=True)

		retry_on_deadlock(_do_save)
		frappe.clear_cache(doctype="ISM Settings")

	def setUp(self):
		super().setUp()
		self._saved_user = frappe.session.user
		self._saved_settings = {}
		settings = frappe.get_single("ISM Settings")
		for fieldname in self._SETTINGS_FIELDS:
			self._saved_settings[fieldname] = getattr(settings, fieldname, None)

		self._save_settings({
			"sms_gateway_provider": "Mock (Deterministic Test)",
			"sms_default_country_code": "+1",
			"sms_enforce_opt_out": 1,
			"otp_code_length": 6,
			"otp_ttl_seconds": 300,
			"otp_resend_cooldown_seconds": 60,
			"otp_max_verify_attempts": 5,
			"otp_lockout_seconds": 900,
			"otp_phone_rate_limit_per_hour": 5,
			"otp_ip_rate_limit_per_hour": 20,
			"portal_session_cookie_name": DEFAULT_PORTAL_COOKIE_NAME,
			"portal_session_cookie_samesite": "Lax",
			"portal_session_secure_cookie": 1,
			"enable_customer_portal": 1,
			"portal_booking_token_ttl_days": 30,
		})

		clear_portal_auth_rate_limits()
		clear_mock_sms_outbox()

	def tearDown(self):
		frappe.set_user(self._saved_user)
		if hasattr(frappe.local, "cookie_manager"):
			frappe.local.cookie_manager = None
		if hasattr(frappe.local, "request"):
			frappe.local.request = None
		clear_portal_auth_rate_limits()
		clear_mock_sms_outbox()
		try:
			self._save_settings(self._saved_settings)
		finally:
			super().tearDown()

	def _track_session(self, session_id: str | None) -> str | None:
		if not session_id:
			return None
		name = frappe.db.get_value("Customer Portal Session", {"session_id": session_id}, "name")
		if name:
			self.track("Customer Portal Session", name)
		return name

	def _track_otp_artifacts(self, phone_e164: str) -> None:
		for name in frappe.get_all("Portal OTP Challenge", filters={"phone_e164": phone_e164}, pluck="name"):
			self.track("Portal OTP Challenge", name)
		for name in frappe.get_all("SMS Audit Log", filters={"phone_e164": phone_e164}, pluck="name"):
			self.track("SMS Audit Log", name)

	def _attach_cookie_manager(self) -> CookieManager:
		cm = CookieManager()
		frappe.local.cookie_manager = cm
		return cm

	def _flush_set_cookie_headers(self, cm: CookieManager) -> list[str]:
		response = Response("ok")
		cm.flush_cookies(response)
		return response.headers.getlist("Set-Cookie")

	def _attach_request_cookie(self, cookie_value: str | None) -> None:
		cookies = {DEFAULT_PORTAL_COOKIE_NAME: cookie_value} if cookie_value else {}
		frappe.local.request = SimpleNamespace(
			cookies=cookies,
			remote_addr="198.51.100.42",
			headers={"User-Agent": "ISM-Portal-Test/1.0"},
			scheme="https",
			host="portal.example.com",
		)

	def test_public_portal_branding_zero_pii_and_fallbacks(self):
		customer = self.create_test_customer(
			customer_name="Secret PII Customer",
			mobile_no="+14165550111",
		)
		vehicle = self.create_test_vehicle(customer=customer.name, license_plate="PII999")
		ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)

		self._save_settings({
			"portal_brand_name": "ISM Service Hub",
			"portal_support_phone": "(416) 555-0100",
			"portal_pickup_hours_text": "Mon-Fri 8:00 AM - 6:00 PM",
		})

		frappe.set_user("Guest")
		branding = get_public_portal_branding()

		self.assertTrue(branding["portal_enabled"])
		self.assertEqual(branding["company_name"], "ISM Service Hub")
		self.assertEqual(branding["support_phone"], "(416) 555-0100")
		self.assertEqual(branding["pickup_hours_text"], "Mon-Fri 8:00 AM - 6:00 PM")
		self.assertEqual(branding["otp_code_length"], 6)
		self.assertEqual(branding["otp_resend_cooldown_seconds"], 60)
		self.assertEqual(find_forbidden_portal_keys(branding), [])

		serialized = str(branding)
		for pii_marker in ("Secret PII Customer", "+14165550111", "PII999", vehicle.vin, ro.name):
			self.assertNotIn(pii_marker, serialized)

	def test_path1_ro_approval_token_exchange_zero_otp_and_rejections(self):
		customer = self.create_test_customer(mobile_no="(416) 555-0121")
		vehicle = self.create_test_vehicle(customer=customer.name)
		ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)

		raw_token = f"RoTok_{frappe.generate_hash(length=28)}"
		frappe.db.set_value(
			"Repair Order",
			ro.name,
			{
				"approval_token": raw_token,
				"approval_token_expiry": add_to_date(now_datetime(), hours=48),
			},
			update_modified=False,
		)

		frappe.set_user("Guest")
		cm = self._attach_cookie_manager()
		res = exchange_ro_approval_token(
			raw_token,
			client_ip="198.51.100.10",
			user_agent="Mozilla/5.0 Test",
			set_response_cookie=True,
		)

		self.assertTrue(res["valid"])
		self.assertEqual(res["repair_order"], ro.name)
		session = res["session"]
		self._track_session(session.session_id)
		self.assertEqual(session.auth_method, "magic_link")
		self.assertEqual(session.scope_type, "repair_order")
		self.assertEqual(session.customer, customer.name)
		self.assertEqual(session.repair_order, ro.name)
		self.assertEqual(session.phone_e164, "+14165550121")

		# Zero OTP prompt / zero SMS dispatched
		self.assertEqual(get_mock_sms_outbox(), [])
		self.assertEqual(
			frappe.db.count("Portal OTP Challenge", {"phone_e164": "+14165550121"}),
			0,
		)

		# Persistent HttpOnly / SameSite=Lax Set-Cookie flushed
		headers = self._flush_set_cookie_headers(cm)
		portal_headers = [h for h in headers if h.startswith(f"{DEFAULT_PORTAL_COOKIE_NAME}=")]
		self.assertEqual(len(portal_headers), 1)
		self.assertIn("HttpOnly", portal_headers[0])
		self.assertIn("SameSite=Lax", portal_headers[0])
		self.assertIn(session.cookie_value, portal_headers[0])

		# Wrong case, invalid, empty, non-string, and expired tokens rejected with zero PII
		for bad_token in (raw_token.swapcase(), "bogus_token_value", "", "   ", {"token": raw_token}, 12345):
			bad_res = exchange_ro_approval_token(
				bad_token,  # type: ignore[arg-type]
				client_ip="198.51.100.11",
				set_response_cookie=False,
			)
			self.assertFalse(bad_res["valid"])
			self.assertNotIn("session", bad_res)
			self.assertNotIn(ro.name, str(bad_res))
			self.assertNotIn(customer.name, str(bad_res))

		# Expired token rejected
		frappe.set_user("Administrator")
		frappe.db.set_value(
			"Repair Order",
			ro.name,
			"approval_token_expiry",
			add_to_date(now_datetime(), hours=-1),
			update_modified=False,
		)
		frappe.set_user("Guest")
		exp_res = exchange_ro_approval_token(
			raw_token,
			client_ip="198.51.100.12",
			set_response_cookie=False,
		)
		self.assertFalse(exp_res["valid"])
		self.assertNotIn(ro.name, str(exp_res))

	def test_create_booking_portal_link_hash_only_and_follows_converted_ro(self):
		customer_a = self.create_test_customer(mobile_no="+14165550131")
		vehicle_a = self.create_test_vehicle(customer=customer_a.name)
		sibling_ro = self.create_test_repair_order(customer=customer_a.name, vehicle=vehicle_a.name)

		se = self.create_test_schedule_entry(
			provisional_customer_name="Walk-In Booking",
			provisional_phone="(416) 555-0131",
		)

		link_1 = create_booking_portal_link(se.name)
		parsed = urlparse(link_1)
		self.assertEqual(parsed.path, "/portal")
		qs = parse_qs(parsed.query)
		self.assertIn("booking_token", qs)
		raw_token_1 = qs["booking_token"][0]
		self.assertGreaterEqual(len(raw_token_1), 32)

		# Strictly SHA-256 hash stored — never raw token
		stored = frappe.db.get_value(
			"Schedule Entry",
			se.name,
			["portal_booking_token_hash", "portal_booking_token_expires_at"],
			as_dict=True,
		)
		self.assertEqual(stored["portal_booking_token_hash"], hash_portal_token(raw_token_1))
		self.assertNotEqual(stored["portal_booking_token_hash"], raw_token_1)
		self.assertIsNotNone(stored["portal_booking_token_expires_at"])

		# Path 1 exchange as Guest -> zero OTP, persistent cookie
		frappe.set_user("Guest")
		cm = self._attach_cookie_manager()
		res = exchange_booking_token(raw_token_1, client_ip="198.51.100.20", set_response_cookie=True)
		self.assertTrue(res["valid"])
		self.assertEqual(res["schedule_entry"], se.name)
		self.assertIsNone(res["repair_order"])

		session = res["session"]
		self._track_session(session.session_id)
		self.assertEqual(session.auth_method, "magic_link")
		self.assertEqual(session.scope_type, "booking")
		self.assertEqual(session.schedule_entry, se.name)
		self.assertEqual(session.phone_e164, "+14165550131")
		self.assertEqual(get_mock_sms_outbox(), [])

		headers = self._flush_set_cookie_headers(cm)
		portal_headers = [h for h in headers if h.startswith(f"{DEFAULT_PORTAL_COOKIE_NAME}=")]
		self.assertEqual(len(portal_headers), 1)
		self.assertIn("HttpOnly", portal_headers[0])
		self.assertIn("SameSite=Lax", portal_headers[0])

		# Before conversion: only schedule_entry accessible
		self.assertEqual(
			list_accessible_visit_keys(session),
			{"repair_orders": [], "schedule_entries": [se.name]},
		)
		assert_session_owns_schedule_entry(session, se.name)

		# Convert Schedule Entry -> Repair Order; existing booking session auto-follows se.repair_order
		frappe.set_user("Administrator")
		converted_ro = self.create_test_repair_order(customer=customer_a.name, vehicle=vehicle_a.name)
		frappe.db.set_value(
			"Schedule Entry",
			se.name,
			{"customer": customer_a.name, "repair_order": converted_ro.name},
			update_modified=False,
		)

		frappe.set_user("Guest")
		self.assertEqual(
			list_accessible_visit_keys(session),
			{"repair_orders": [converted_ro.name], "schedule_entries": [se.name]},
		)
		assert_session_owns_ro(session, converted_ro.name)
		# Sibling RO on the same customer remains strictly blocked
		with self.assertRaises(frappe.PermissionError):
			assert_session_owns_ro(session, sibling_ro.name)

		# Rotating link invalidates previous token
		frappe.set_user("Administrator")
		link_2 = create_booking_portal_link(se.name)
		raw_token_2 = parse_qs(urlparse(link_2).query)["booking_token"][0]
		self.assertNotEqual(raw_token_1, raw_token_2)

		frappe.set_user("Guest")
		old_res = exchange_booking_token(raw_token_1, client_ip="198.51.100.21", set_response_cookie=False)
		self.assertFalse(old_res["valid"])

		new_res = exchange_booking_token(raw_token_2, client_ip="198.51.100.22", set_response_cookie=False)
		self.assertTrue(new_res["valid"])
		self._track_session(new_res["session"].session_id)
		self.assertEqual(new_res["repair_order"], converted_ro.name)

		# Expired booking token rejected
		frappe.set_user("Administrator")
		frappe.db.set_value(
			"Schedule Entry",
			se.name,
			"portal_booking_token_expires_at",
			add_to_date(now_datetime(), hours=-2),
			update_modified=False,
		)
		frappe.set_user("Guest")
		exp_res = exchange_booking_token(raw_token_2, client_ip="198.51.100.23", set_response_cookie=False)
		self.assertFalse(exp_res["valid"])

		# Nonexistent Schedule Entry raises DoesNotExistError
		with self.assertRaises(frappe.DoesNotExistError):
			create_booking_portal_link("SE-NONEXISTENT-99999")

	def test_path2_sms_otp_happy_path_and_strict_sms_only_rejections(self):
		customer = self.create_test_customer(mobile_no="(416) 555-0141")
		self.addCleanup(self._track_otp_artifacts, "+14165550141")

		frappe.set_user("Guest")

		# Strictly SMS-only: reject emails, non-strings, empty, invalid phone syntax
		for invalid_input in ("driver@example.com", "user+tag@shop.ca", "", "   ", None, 4165550141, ["+14165550141"], "123"):
			with self.assertRaises(frappe.ValidationError):
				request_portal_login_otp(invalid_input, client_ip="198.51.100.30")  # type: ignore[arg-type]
		self.assertEqual(get_mock_sms_outbox(), [])

		# Valid known phone -> delegates to P1 request_sms_otp
		req = request_portal_login_otp(
			"(416) 555-0141",
			client_ip="198.51.100.31",
			user_agent="ISM-Mobile/1.0",
		)
		self.assertTrue(req["success"])
		self.assertTrue(req["challenge_id"])
		self.assertEqual(req["phone_masked"], "+1******0141")
		self.assertEqual(req["expires_in_seconds"], 300)
		self.assertEqual(req["resend_available_in_seconds"], 60)

		code = get_last_mock_otp("+14165550141")
		self.assertIsNotNone(code)
		self.assertEqual(len(code), 6)
		self.assertEqual(len(get_mock_sms_outbox()), 1)

		# Wrong code fails generically
		wrong_code = "000000" if code != "000000" else "999999"
		bad_verify = verify_portal_login_otp(
			req["challenge_id"],
			wrong_code,
			raw_phone="(416) 555-0141",
			client_ip="198.51.100.31",
			set_response_cookie=False,
		)
		self.assertFalse(bad_verify["verified"])
		self.assertIsNone(bad_verify["session"])
		self.assertEqual(bad_verify["error_code"], "INVALID_CODE")

		# Correct code issues persistent customer-scoped session cookie
		cm = self._attach_cookie_manager()
		ok_verify = verify_portal_login_otp(
			req["challenge_id"],
			code,
			raw_phone="(416) 555-0141",
			client_ip="198.51.100.31",
			user_agent="ISM-Mobile/1.0",
			set_response_cookie=True,
		)
		self.assertTrue(ok_verify["verified"])
		session = ok_verify["session"]
		self._track_session(session.session_id)
		self.assertEqual(session.auth_method, "sms_otp")
		self.assertEqual(session.scope_type, "customer")
		self.assertEqual(session.customer, customer.name)
		self.assertEqual(session.phone_e164, "+14165550141")
		self.assertIn(customer.name, ok_verify["customers"])

		headers = self._flush_set_cookie_headers(cm)
		portal_headers = [h for h in headers if h.startswith(f"{DEFAULT_PORTAL_COOKIE_NAME}=")]
		self.assertEqual(len(portal_headers), 1)
		self.assertIn("HttpOnly", portal_headers[0])
		self.assertIn("SameSite=Lax", portal_headers[0])

		# Replaying consumed challenge fails generically
		replay = verify_portal_login_otp(
			req["challenge_id"],
			code,
			client_ip="198.51.100.31",
			set_response_cookie=False,
		)
		self.assertFalse(replay["verified"])
		self.assertIsNone(replay["session"])

	def test_path2_unknown_phone_anti_enumeration_and_shared_rate_limit_bucket(self):
		known_phone = "+14165550151"
		unknown_phone = "+14165550152"
		self.create_test_customer(mobile_no=known_phone)
		self.addCleanup(self._track_otp_artifacts, known_phone)

		# Lower IP hourly limit to 2 to verify unknown + known share the exact same IP bucket
		self._save_settings({"otp_ip_rate_limit_per_hour": 2})

		frappe.set_user("Guest")

		# 1. Request OTP for unknown phone -> zero SMS, zero DB rows, uniform opaque shape
		unknown_res = request_portal_login_otp(unknown_phone, client_ip="198.51.100.50")
		self.assertTrue(unknown_res["success"])
		self.assertTrue(unknown_res["challenge_id"])
		self.assertNotIn("noop", unknown_res["challenge_id"].lower())
		self.assertEqual(unknown_res["phone_masked"], "+1******0152")
		self.assertEqual(unknown_res["expires_in_seconds"], 300)
		self.assertEqual(unknown_res["resend_available_in_seconds"], 60)
		self.assertEqual(get_mock_sms_outbox(), [])
		self.assertEqual(frappe.db.count("Portal OTP Challenge", {"phone_e164": unknown_phone}), 0)
		self.assertEqual(frappe.db.count("SMS Audit Log", {"phone_e164": unknown_phone}), 0)

		# Immediate resend for unknown phone hits identical cooldown bucket
		unknown_cd = request_portal_login_otp(unknown_phone, client_ip="198.51.100.50")
		self.assertFalse(unknown_cd["success"])
		self.assertEqual(unknown_cd["error_code"], "COOLDOWN_ACTIVE")
		self.assertGreater(unknown_cd["resend_available_in_seconds"], 0)

		# Verify synthetic noop challenge fails identically to invalid real challenge
		noop_verify = verify_portal_login_otp(
			unknown_res["challenge_id"],
			"123456",
			raw_phone=unknown_phone,
			client_ip="198.51.100.50",
			set_response_cookie=False,
		)
		self.assertFalse(noop_verify["verified"])
		self.assertIsNone(noop_verify["session"])
		self.assertEqual(noop_verify["error_code"], "INVALID_CODE")
		self.assertIn("Invalid or expired verification code", noop_verify["error"])

		# 2. Known phone from same IP consumes slot 2/2 and returns identical response key set
		known_res = request_portal_login_otp(known_phone, client_ip="198.51.100.50")
		self.assertTrue(known_res["success"])
		self.assertEqual(set(unknown_res.keys()), set(known_res.keys()))
		self.assertEqual(unknown_res["expires_in_seconds"], known_res["expires_in_seconds"])
		self.assertEqual(
			unknown_res["resend_available_in_seconds"],
			known_res["resend_available_in_seconds"],
		)
		self.assertEqual(len(get_mock_sms_outbox()), 1)

		# 3. Third request from same IP (even for another unknown phone) hits shared IP rate limit
		third_res = request_portal_login_otp("+14165550153", client_ip="198.51.100.50")
		self.assertFalse(third_res["success"])
		self.assertEqual(third_res["error_code"], "RATE_LIMITED")
		self.assertEqual(len(get_mock_sms_outbox()), 1)

	def test_path3_cookie_restore_scope_upgrade_and_sign_out_revocation(self):
		phone = "+14165550161"
		customer = self.create_test_customer(mobile_no=phone)
		vehicle = self.create_test_vehicle(customer=customer.name)
		ro_1 = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
		ro_2 = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
		self.addCleanup(self._track_otp_artifacts, phone)

		raw_token = f"RoRestore_{frappe.generate_hash(length=24)}"
		frappe.db.set_value(
			"Repair Order",
			ro_1.name,
			{
				"approval_token": raw_token,
				"approval_token_expiry": add_to_date(now_datetime(), hours=24),
			},
			update_modified=False,
		)

		frappe.set_user("Guest")

		# Unauthenticated require_portal_session raises AuthenticationError
		self._attach_request_cookie(None)
		self.assertIsNone(get_active_portal_session())
		with self.assertRaises(frappe.AuthenticationError):
			require_portal_session()

		# Path 1 establishes cookie -> Path 3 restores from request cookie
		exch = exchange_ro_approval_token(raw_token, client_ip="198.51.100.60", set_response_cookie=False)
		self.assertTrue(exch["valid"])
		ro_session = exch["session"]
		self._track_session(ro_session.session_id)

		self._attach_request_cookie(ro_session.cookie_value)
		restored = require_portal_session(touch=True)
		self.assertEqual(restored.session_id, ro_session.session_id)
		self.assertEqual(restored.scope_type, "repair_order")

		# Upgrade RO-scoped cookie to customer-scoped via SMS OTP rotation
		otp_req = request_portal_login_otp(phone, client_ip="198.51.100.60")
		self.assertTrue(otp_req["success"])
		code = get_last_mock_otp(phone)
		self.assertIsNotNone(code)

		upgraded = verify_portal_login_otp(
			otp_req["challenge_id"],
			code,
			upgrade_existing_session=True,
			client_ip="198.51.100.60",
			set_response_cookie=False,
		)
		self.assertTrue(upgraded["verified"])
		customer_session = upgraded["session"]
		self._track_session(customer_session.session_id)
		self.assertNotEqual(customer_session.session_id, ro_session.session_id)
		self.assertEqual(customer_session.scope_type, "customer")

		# Old session marked Rotated and rejected; new session sees both ROs
		self.assertEqual(
			frappe.db.get_value("Customer Portal Session", {"session_id": ro_session.session_id}, "status"),
			"Rotated",
		)
		self._attach_request_cookie(ro_session.cookie_value)
		self.assertIsNone(get_active_portal_session())

		self._attach_request_cookie(customer_session.cookie_value)
		active_upgraded = require_portal_session()
		assert_session_owns_ro(active_upgraded, ro_1.name)
		assert_session_owns_ro(active_upgraded, ro_2.name)

		# Explicit sign-out revokes server-side and clears cookie
		cm = self._attach_cookie_manager()
		sign_out_res = sign_out_portal_session(reason="Customer clicked sign out")
		self.assertTrue(sign_out_res["signed_out"])
		self.assertTrue(sign_out_res["revoked"])
		self.assertEqual(
			frappe.db.get_value("Customer Portal Session", {"session_id": customer_session.session_id}, "status"),
			"Revoked",
		)
		self.assertIn(DEFAULT_PORTAL_COOKIE_NAME, cm.to_delete)

		# Reusing revoked cookie fails immediately
		self.assertIsNone(get_active_portal_session())
		with self.assertRaises(frappe.AuthenticationError):
			require_portal_session()

	def test_strict_scope_isolation_and_anti_idor_zero_existence_oracle(self):
		phone_a = "+14165550171"
		phone_b = "+14165550172"

		cust_a = self.create_test_customer(mobile_no=phone_a)
		veh_a = self.create_test_vehicle(customer=cust_a.name)
		ro_a1 = self.create_test_repair_order(customer=cust_a.name, vehicle=veh_a.name)
		ro_a2 = self.create_test_repair_order(customer=cust_a.name, vehicle=veh_a.name)
		se_a1 = self.create_test_schedule_entry(customer=cust_a.name, repair_order=ro_a1.name)
		se_a2 = self.create_test_schedule_entry(customer=cust_a.name, repair_order=ro_a2.name)
		se_a_prov = self.create_test_schedule_entry(
			provisional_customer_name="Cust A Provisional",
			provisional_phone="(416) 555-0171",
		)

		cust_b = self.create_test_customer(mobile_no=phone_b)
		veh_b = self.create_test_vehicle(customer=cust_b.name)
		ro_b1 = self.create_test_repair_order(customer=cust_b.name, vehicle=veh_b.name)
		se_b1 = self.create_test_schedule_entry(customer=cust_b.name, repair_order=ro_b1.name)

		# 1. scope_type="repair_order" bound to ro_a1
		ro_scope = issue_portal_session(
			auth_method="magic_link",
			scope_type="repair_order",
			customer=cust_a.name,
			phone_e164=phone_a,
			repair_order=ro_a1.name,
			set_response_cookie=False,
		)
		self._track_session(ro_scope.session_id)

		frappe.set_user("Guest")
		assert_session_owns_ro(ro_scope, ro_a1.name)
		assert_session_owns_schedule_entry(ro_scope, se_a1.name)

		# Capture uniform error messages to prove zero existence oracle
		err_sibling = self._capture_permission_error(assert_session_owns_ro, ro_scope, ro_a2.name)
		err_other_cust = self._capture_permission_error(assert_session_owns_ro, ro_scope, ro_b1.name)
		err_missing = self._capture_permission_error(assert_session_owns_ro, ro_scope, "RO-DOES-NOT-EXIST-999")
		self.assertEqual(err_sibling, err_other_cust)
		self.assertEqual(err_sibling, err_missing)
		self.assertNotIn(ro_a2.name, err_sibling)
		self.assertNotIn(ro_b1.name, err_other_cust)

		with self.assertRaises(frappe.PermissionError):
			assert_session_owns_schedule_entry(ro_scope, se_a2.name)
		with self.assertRaises(frappe.PermissionError):
			assert_session_owns_schedule_entry(ro_scope, se_a_prov.name)
		with self.assertRaises(frappe.PermissionError):
			assert_session_owns_schedule_entry(ro_scope, se_b1.name)

		# 2. scope_type="booking" (P5 booking_otp) bound to se_a1
		frappe.set_user("Administrator")
		booking_scope = issue_portal_session(
			auth_method="booking_otp",
			scope_type="booking",
			customer=cust_a.name,
			phone_e164=phone_a,
			schedule_entry=se_a1.name,
			set_response_cookie=False,
		)
		self._track_session(booking_scope.session_id)

		frappe.set_user("Guest")
		assert_session_owns_schedule_entry(booking_scope, se_a1.name)
		assert_session_owns_ro(booking_scope, ro_a1.name)
		with self.assertRaises(frappe.PermissionError):
			assert_session_owns_ro(booking_scope, ro_a2.name)
		with self.assertRaises(frappe.PermissionError):
			assert_session_owns_schedule_entry(booking_scope, se_a2.name)
		with self.assertRaises(frappe.PermissionError):
			assert_session_owns_schedule_entry(booking_scope, se_b1.name)

		# 3. scope_type="customer" for Customer A
		frappe.set_user("Administrator")
		cust_scope = issue_portal_session(
			auth_method="sms_otp",
			scope_type="customer",
			customer=cust_a.name,
			phone_e164=phone_a,
			set_response_cookie=False,
		)
		self._track_session(cust_scope.session_id)

		frappe.set_user("Guest")
		keys = list_accessible_visit_keys(cust_scope)
		self.assertEqual(keys["repair_orders"], sorted([ro_a1.name, ro_a2.name]))
		self.assertEqual(keys["schedule_entries"], sorted([se_a1.name, se_a2.name, se_a_prov.name]))

		assert_session_owns_ro(cust_scope, ro_a1.name)
		assert_session_owns_ro(cust_scope, ro_a2.name)
		assert_session_owns_schedule_entry(cust_scope, se_a_prov.name)

		err_b_ro = self._capture_permission_error(assert_session_owns_ro, cust_scope, ro_b1.name)
		err_b_se = self._capture_permission_error(assert_session_owns_schedule_entry, cust_scope, se_b1.name)
		self.assertEqual(err_b_ro, err_b_se)

	def _capture_permission_error(self, fn, *args) -> str:
		try:
			fn(*args)
		except frappe.PermissionError as exc:
			return str(exc)
		self.fail(f"Expected frappe.PermissionError from {fn.__name__}{args}")

	def test_formatted_phone_matching_and_zero_false_positives(self):
		# Formatted Customer.mobile_no
		cust_fmt = self.create_test_customer(mobile_no="(416) 555-0199")

		# Cross-country number sharing the exact same last 10 digits (must NOT match +14165550199!)
		cust_uk = self.create_test_customer(mobile_no="+444165550199")

		# Dynamic-Link Contact with formatted phone in Contact Phone child table
		cust_contact = self.create_test_customer()
		contact = frappe.get_doc({
			"doctype": "Contact",
			"first_name": "Portal Contact",
			"phone_nos": [{"phone": "1 (416) 555.0199", "is_primary_mobile_no": 1}],
			"links": [{"link_doctype": "Customer", "link_name": cust_contact.name}],
		}).insert(ignore_permissions=True)
		self.track(contact)

		# Provisional Schedule Entry with hyphenated phone
		se_prov = self.create_test_schedule_entry(
			provisional_customer_name="Hyphen Phone Driver",
			provisional_phone="416-555-0199",
		)

		subjects = resolve_subjects_for_phone("+14165550199")
		self.assertIn(cust_fmt.name, subjects["customers"])
		self.assertIn(cust_contact.name, subjects["customers"])
		self.assertNotIn(cust_uk.name, subjects["customers"])
		self.assertIn(se_prov.name, subjects["schedule_entries"])

		# resolve_customer_phone_e164 across Customer, Contact, and fallback_phone
		self.assertEqual(resolve_customer_phone_e164(cust_fmt.name), "+14165550199")
		self.assertEqual(resolve_customer_phone_e164(cust_contact.name), "+14165550199")
		self.assertEqual(resolve_customer_phone_e164(contact_person=contact.name), "+14165550199")
		self.assertEqual(resolve_customer_phone_e164(fallback_phone="416.555.0199"), "+14165550199")
		self.assertIsNone(resolve_customer_phone_e164(fallback_phone="not-a-phone@example.com"))

	def test_link_exchange_rate_limit_enforcement(self):
		with patch("ism.portal.auth._LINK_EXCHANGE_IP_LIMIT", 2):
			res_1 = exchange_ro_approval_token("missing_tok_1", client_ip="198.51.100.91", set_response_cookie=False)
			res_2 = exchange_ro_approval_token("missing_tok_2", client_ip="198.51.100.91", set_response_cookie=False)
			self.assertFalse(res_1["valid"])
			self.assertFalse(res_2["valid"])
			with self.assertRaises(frappe.TooManyRequestsError):
				exchange_ro_approval_token("missing_tok_3", client_ip="198.51.100.91", set_response_cookie=False)

		with patch("ism.portal.auth._LINK_EXCHANGE_TOKEN_LIMIT", 2):
			res_1 = exchange_booking_token("same_book_tok", client_ip="198.51.100.92", set_response_cookie=False)
			res_2 = exchange_booking_token("same_book_tok", client_ip="198.51.100.93", set_response_cookie=False)
			self.assertFalse(res_1["valid"])
			self.assertFalse(res_2["valid"])
			with self.assertRaises(frappe.TooManyRequestsError):
				exchange_booking_token("same_book_tok", client_ip="198.51.100.94", set_response_cookie=False)

	def test_lifecycle_lock_helpers(self):
		for locked_status in ("Closed / Released", "Cancelled"):
			self.assertTrue(is_ro_mutating_locked(locked_status))
			with self.assertRaises(frappe.ValidationError):
				assert_ro_mutating_unlocked(locked_status)

		for active_status in (
			"Checked In",
			"Draft Estimate",
			"Pending Authorization",
			"Authorized - In Progress",
			"Waiting on Parts",
			"Work Completed",
			"Billed / Paid",
			None,
			"",
		):
			self.assertFalse(is_ro_mutating_locked(active_status))
			assert_ro_mutating_unlocked(active_status)
