# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import hashlib
import inspect
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.auth import CookieManager
from frappe.utils import add_to_date, get_datetime, now_datetime
from werkzeug.wrappers import Response

from ism.comms.contracts import DEFAULT_PORTAL_COOKIE_NAME
from ism.comms.sessions import (
	LAST_SEEN_THROTTLE_SECONDS,
	cleanup_expired_portal_sessions,
	clear_portal_session_cookie,
	get_portal_cookie_name,
	get_portal_session_signing_secret,
	issue_portal_session,
	revoke_all_sessions_for_customer,
	revoke_all_sessions_for_phone,
	revoke_portal_session,
	rotate_portal_session,
	set_portal_session_cookie,
	should_use_secure_cookie,
	verify_portal_session,
)
from ism.tests.base import ISMTestCase


class TestCommsSessions(ISMTestCase):
	"""Verify Wave 2B persistent signed portal sessions, cookie attributes, rotation, and revocation."""

	def _track_session(self, session_id: str) -> str:
		name = frappe.db.get_value("Customer Portal Session", {"session_id": session_id}, "name")
		if name:
			self.track("Customer Portal Session", name)
		return name

	def _create_otp_challenge(self, phone_e164: str = "+14165550199"):
		doc = frappe.get_doc({
			"doctype": "Portal OTP Challenge",
			"challenge_id": f"chal_{frappe.generate_hash(length=12)}",
			"purpose": "portal_login",
			"phone_e164": phone_e164,
			"code_salt": "a" * 32,
			"code_hash": "b" * 64,
			"status": "Verified",
			"expires_at": add_to_date(now_datetime(), seconds=300),
		}).insert(ignore_permissions=True)
		return self.track(doc)

	def test_issue_and_verify_all_auth_methods_and_secret_hashing(self):
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)
		ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
		se = self.create_test_schedule_entry(customer=customer.name, repair_order=ro.name)
		challenge = self._create_otp_challenge("+14165550199")

		# 1. magic_link session bound to repair_order
		raw_magic = "magic_tok_super_secret_value_98765"
		raw_ua = "Mozilla/5.0 (ISM Test Browser)"
		magic_info = issue_portal_session(
			auth_method="magic_link",
			scope_type="repair_order",
			customer=customer.name,
			repair_order=ro.name,
			source_magic_token=raw_magic,
			client_ip="198.51.100.11",
			user_agent=raw_ua,
			set_response_cookie=False,
		)
		magic_doc_name = self._track_session(magic_info.session_id)
		self.assertIsNotNone(magic_doc_name)
		self.assertEqual(magic_info.auth_method, "magic_link")
		self.assertEqual(magic_info.scope_type, "repair_order")
		self.assertEqual(magic_info.repair_order, ro.name)
		self.assertNotIn("cookie_value", magic_info.to_dict())
		self.assertEqual(magic_info.to_dict(include_cookie=True)["cookie_value"], magic_info.cookie_value)

		# Verify wire format v1.<session_id>.<raw_secret>.<hmac_b64url> and secret hashing
		parts = magic_info.cookie_value.split(".")
		self.assertEqual(len(parts), 4)
		version, sid, raw_secret, sig = parts
		self.assertEqual(version, "v1")
		self.assertEqual(sid, magic_info.session_id)
		self.assertTrue(raw_secret)
		self.assertTrue(sig)

		magic_doc = frappe.get_doc("Customer Portal Session", magic_doc_name)
		expected_token_hash = hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()
		self.assertEqual(magic_doc.token_hash, expected_token_hash)
		self.assertNotEqual(magic_doc.token_hash, raw_secret)
		self.assertNotEqual(magic_doc.token_hash, magic_info.cookie_value)
		self.assertEqual(magic_doc.source_token_hash, hashlib.sha256(raw_magic.encode("utf-8")).hexdigest()[:24])
		self.assertEqual(magic_doc.user_agent_hash, hashlib.sha256(raw_ua.encode("utf-8")).hexdigest()[:24])
		for field_val in magic_doc.as_dict().values():
			if isinstance(field_val, str):
				self.assertNotIn(raw_secret, field_val)
				self.assertNotIn(magic_info.cookie_value, field_val)
				self.assertNotIn(raw_magic, field_val)
				self.assertNotIn(raw_ua, field_val)

		verified_magic = verify_portal_session(magic_info.cookie_value)
		self.assertIsNotNone(verified_magic)
		self.assertEqual(verified_magic.session_id, magic_info.session_id)
		self.assertEqual(verified_magic.auth_method, "magic_link")
		self.assertEqual(verified_magic.repair_order, ro.name)
		self.assertIsNone(verified_magic.cookie_value)

		# 2. sms_otp session bound to customer + formatted phone + OTP challenge
		otp_info = issue_portal_session(
			auth_method="sms_otp",
			scope_type="customer",
			customer=customer.name,
			phone_e164="(416) 555-0199",
			source_otp_challenge=challenge.name,
			set_response_cookie=False,
		)
		otp_doc_name = self._track_session(otp_info.session_id)
		self.assertEqual(otp_info.phone_e164, "+14165550199")
		otp_doc = frappe.get_doc("Customer Portal Session", otp_doc_name)
		self.assertEqual(otp_doc.source_otp_challenge, challenge.name)
		verified_otp = verify_portal_session(otp_info.cookie_value, required_scope_type="customer")
		self.assertIsNotNone(verified_otp)
		self.assertEqual(verified_otp.customer, customer.name)
		self.assertEqual(verified_otp.phone_e164, "+14165550199")
		self.assertIsNone(verify_portal_session(otp_info.cookie_value, required_scope_type="booking"))

		# 3. booking_otp session bound to phone + schedule_entry
		booking_info = issue_portal_session(
			auth_method="booking_otp",
			scope_type="booking",
			phone_e164="+14165550188",
			schedule_entry=se.name,
			set_response_cookie=False,
		)
		self._track_session(booking_info.session_id)
		verified_booking = verify_portal_session(booking_info.cookie_value, required_scope_type="booking")
		self.assertIsNotNone(verified_booking)
		self.assertEqual(verified_booking.auth_method, "booking_otp")
		self.assertEqual(verified_booking.schedule_entry, se.name)

		# Validation errors on invalid inputs
		with self.assertRaises(ValueError):
			issue_portal_session(auth_method="email_otp", phone_e164="+14165550199", set_response_cookie=False)
		with self.assertRaises(ValueError):
			issue_portal_session(
				auth_method="sms_otp", scope_type="admin", phone_e164="+14165550199", set_response_cookie=False
			)
		with self.assertRaises(ValueError):
			issue_portal_session(auth_method="sms_otp", set_response_cookie=False)

	def test_hmac_tamper_rejected_before_db_query(self):
		info = issue_portal_session(
			auth_method="sms_otp",
			phone_e164="+14165550155",
			set_response_cookie=False,
		)
		self._track_session(info.session_id)

		parts = info.cookie_value.split(".")
		tampered_sig = parts[3][:-1] + ("A" if parts[3][-1] != "A" else "B")
		tampered_secret = parts[2][:-1] + ("A" if parts[2][-1] != "A" else "B")
		tampered_cookies = [
			f"{parts[0]}.{parts[1]}.{parts[2]}.{tampered_sig}",
			f"{parts[0]}.{parts[1]}.{tampered_secret}.{parts[3]}",
			f"v2.{parts[1]}.{parts[2]}.{parts[3]}",
			"malformed-cookie",
			"v1.only.three",
			"v1.too.many.parts.here",
			"",
		]

		real_get_value = frappe.db.get_value
		queried_session_table = []

		def spying_get_value(doctype, *args, **kwargs):
			if doctype == "Customer Portal Session":
				queried_session_table.append((doctype, args, kwargs))
			return real_get_value(doctype, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=spying_get_value):
			for bad_cookie in tampered_cookies:
				with self.subTest(cookie=bad_cookie):
					self.assertIsNone(verify_portal_session(bad_cookie))

		self.assertEqual(queried_session_table, [])

	def test_lazy_signing_secret_generation_when_blank(self):
		settings = frappe.get_single("ISM Settings")
		original_secret = settings.get_password("portal_session_signing_secret", raise_exception=False)

		try:
			frappe.db.sql(
				"DELETE FROM `__Auth` WHERE doctype = 'ISM Settings' AND name = 'ISM Settings' AND fieldname = 'portal_session_signing_secret'"
			)
			frappe.db.set_single_value("ISM Settings", "portal_session_signing_secret", "")
			frappe.clear_cache(doctype="ISM Settings")

			reloaded = frappe.get_single("ISM Settings")
			self.assertFalse(reloaded.get_password("portal_session_signing_secret", raise_exception=False))

			# Unauthenticated verify returns None without throwing when secret is absent
			self.assertIsNone(verify_portal_session("v1.abc.def.ghi"))

			# Issuing lazily generates and persists a secret in ISM Settings
			info = issue_portal_session(
				auth_method="sms_otp",
				phone_e164="+14165550166",
				set_response_cookie=False,
			)
			self._track_session(info.session_id)

			frappe.clear_cache(doctype="ISM Settings")
			persisted = get_portal_session_signing_secret(create_if_missing=False)
			self.assertIsNotNone(persisted)
			self.assertGreaterEqual(len(persisted), 48)
			self.assertIsNotNone(verify_portal_session(info.cookie_value))
		finally:
			if original_secret:
				restored = frappe.get_single("ISM Settings")
				restored.portal_session_signing_secret = original_secret
				restored.save(ignore_permissions=True)
			frappe.clear_cache(doctype="ISM Settings")

	def test_request_cookie_reading_and_throttled_last_seen_at(self):
		info = issue_portal_session(
			auth_method="sms_otp",
			phone_e164="+14165550177",
			client_ip="198.51.100.1",
			set_response_cookie=False,
		)
		doc_name = self._track_session(info.session_id)
		cookie_name = get_portal_cookie_name()

		fake_request = SimpleNamespace(
			cookies={cookie_name: info.cookie_value},
			remote_addr="203.0.113.50",
		)
		initial_last_seen = get_datetime(
			frappe.db.get_value("Customer Portal Session", doc_name, "last_seen_at")
		)

		# Immediate verify within 300s must NOT rewrite last_seen_at or last_ip
		with (
			patch.object(frappe.local, "request", fake_request, create=True),
			patch.object(frappe.local, "request_ip", "203.0.113.50", create=True),
		):
			verified = verify_portal_session()
			self.assertIsNotNone(verified)
			self.assertEqual(verified.session_id, info.session_id)

		row_after_fast = frappe.db.get_value(
			"Customer Portal Session", doc_name, ["last_seen_at", "last_ip"], as_dict=True
		)
		self.assertEqual(get_datetime(row_after_fast.last_seen_at), initial_last_seen)
		self.assertEqual(row_after_fast.last_ip, "198.51.100.1")

		# Backdate last_seen_at > 300s ago; touch=False still skips write, touch=True updates it
		stale_seen = add_to_date(now_datetime(), seconds=-(LAST_SEEN_THROTTLE_SECONDS + 15))
		frappe.db.set_value("Customer Portal Session", doc_name, "last_seen_at", stale_seen, update_modified=False)

		with (
			patch.object(frappe.local, "request", fake_request, create=True),
			patch.object(frappe.local, "request_ip", "203.0.113.50", create=True),
		):
			self.assertIsNotNone(verify_portal_session(touch=False))
			still_stale = get_datetime(
				frappe.db.get_value("Customer Portal Session", doc_name, "last_seen_at")
			)
			self.assertEqual(still_stale, get_datetime(stale_seen))

			self.assertIsNotNone(verify_portal_session(touch=True))

		row_after_touch = frappe.db.get_value(
			"Customer Portal Session", doc_name, ["last_seen_at", "last_ip"], as_dict=True
		)
		self.assertGreater(get_datetime(row_after_touch.last_seen_at), get_datetime(stale_seen))
		self.assertEqual(row_after_touch.last_ip, "203.0.113.50")

	def test_expired_session_rejected_and_lazy_marked_expired(self):
		info = issue_portal_session(
			auth_method="sms_otp",
			phone_e164="+14165550133",
			set_response_cookie=False,
		)
		doc_name = self._track_session(info.session_id)

		frappe.db.set_value(
			"Customer Portal Session",
			doc_name,
			"expires_at",
			add_to_date(now_datetime(), seconds=-10),
			update_modified=False,
		)

		self.assertIsNone(verify_portal_session(info.cookie_value))
		self.assertEqual(frappe.db.get_value("Customer Portal Session", doc_name, "status"), "Expired")

	def test_rotate_portal_session_invalidates_old_and_upgrades_scope(self):
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)
		ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
		se = self.create_test_schedule_entry(customer=customer.name, repair_order=ro.name)

		old_info = issue_portal_session(
			auth_method="booking_otp",
			scope_type="booking",
			phone_e164="+14165550144",
			schedule_entry=se.name,
			set_response_cookie=False,
		)
		old_doc_name = self._track_session(old_info.session_id)

		new_info = rotate_portal_session(
			old_info.cookie_value,
			customer=customer.name,
			repair_order=ro.name,
			scope_type="customer",
			set_response_cookie=False,
		)
		new_doc_name = self._track_session(new_info.session_id)

		self.assertNotEqual(new_info.session_id, old_info.session_id)
		self.assertNotEqual(new_info.cookie_value, old_info.cookie_value)
		self.assertEqual(new_info.auth_method, "booking_otp")
		self.assertEqual(new_info.scope_type, "customer")
		self.assertEqual(new_info.customer, customer.name)
		self.assertEqual(new_info.repair_order, ro.name)
		self.assertEqual(new_info.schedule_entry, se.name)
		self.assertEqual(new_info.phone_e164, "+14165550144")

		old_row = frappe.db.get_value(
			"Customer Portal Session",
			old_doc_name,
			["status", "revoked_at", "revoked_reason", "token_hash"],
			as_dict=True,
		)
		new_token_hash = frappe.db.get_value("Customer Portal Session", new_doc_name, "token_hash")
		self.assertEqual(old_row.status, "Rotated")
		self.assertIsNotNone(old_row.revoked_at)
		self.assertEqual(old_row.revoked_reason, "Rotated")
		self.assertNotEqual(old_row.token_hash, new_token_hash)

		# Old cookie immediately fails verification and re-rotation
		self.assertIsNone(verify_portal_session(old_info.cookie_value))
		with self.assertRaises(frappe.AuthenticationError):
			rotate_portal_session(old_info.cookie_value, set_response_cookie=False)

		# New cookie verifies with upgraded scope
		verified = verify_portal_session(new_info.cookie_value, required_scope_type="customer")
		self.assertIsNotNone(verified)
		self.assertEqual(verified.customer, customer.name)
		self.assertEqual(verified.repair_order, ro.name)
		self.assertEqual(verified.schedule_entry, se.name)

		# Rotating tampered or expired cookies raises AuthenticationError
		with self.assertRaises(frappe.AuthenticationError):
			rotate_portal_session("v1.bad.cookie.sig", set_response_cookie=False)

		frappe.db.set_value(
			"Customer Portal Session",
			new_doc_name,
			"expires_at",
			add_to_date(now_datetime(), seconds=-5),
			update_modified=False,
		)
		with self.assertRaises(frappe.AuthenticationError):
			rotate_portal_session(new_info.cookie_value, set_response_cookie=False)
		self.assertEqual(frappe.db.get_value("Customer Portal Session", new_doc_name, "status"), "Expired")

	def test_revoke_single_and_bulk_sessions(self):
		cust_a = self.create_test_customer()
		cust_b = self.create_test_customer()

		s1 = issue_portal_session(
			auth_method="sms_otp",
			customer=cust_a.name,
			phone_e164="+14165550111",
			set_response_cookie=False,
		)
		s2 = issue_portal_session(
			auth_method="sms_otp",
			customer=cust_a.name,
			phone_e164="+14165550111",
			set_response_cookie=False,
		)
		s3 = issue_portal_session(
			auth_method="magic_link",
			customer=cust_a.name,
			phone_e164="+14165550122",
			set_response_cookie=False,
		)
		s4 = issue_portal_session(
			auth_method="sms_otp",
			customer=cust_b.name,
			phone_e164="+14165550199",
			set_response_cookie=False,
		)
		for info in (s1, s2, s3, s4):
			self._track_session(info.session_id)

		# Tampered cookie cannot revoke s1
		parts = s1.cookie_value.split(".")
		forged = f"{parts[0]}.{parts[1]}.{parts[2]}.forgedsig"
		self.assertFalse(revoke_portal_session(forged, clear_response_cookie=False))
		self.assertIsNotNone(verify_portal_session(s1.cookie_value))

		# Valid "Sign out on this device" revokes s1 and clears cookie
		mock_cm = MagicMock()
		with patch.object(frappe.local, "cookie_manager", mock_cm, create=True):
			self.assertTrue(revoke_portal_session(s1.cookie_value, reason="User signed out"))
		mock_cm.delete_cookie.assert_called_once_with(get_portal_cookie_name())
		self.assertIsNone(verify_portal_session(s1.cookie_value))
		self.assertFalse(revoke_portal_session(s1.cookie_value, clear_response_cookie=False))

		# Bulk revoke by phone revokes s2 (the remaining active session for +14165550111) only
		revoked_phone_count = revoke_all_sessions_for_phone("(416) 555-0111", reason="Phone opt-out")
		self.assertEqual(revoked_phone_count, 1)
		self.assertIsNone(verify_portal_session(s2.cookie_value))
		self.assertIsNotNone(verify_portal_session(s3.cookie_value))
		self.assertIsNotNone(verify_portal_session(s4.cookie_value))

		# Bulk revoke by customer revokes s3 (remaining active session for cust_a) without touching cust_b
		revoked_cust_count = revoke_all_sessions_for_customer(cust_a.name, reason="Staff security reset")
		self.assertEqual(revoked_cust_count, 1)
		self.assertIsNone(verify_portal_session(s3.cookie_value))
		self.assertIsNotNone(verify_portal_session(s4.cookie_value))

		# Staff revocation by explicit session_id without clearing staff cookie
		mock_cm_staff = MagicMock()
		with patch.object(frappe.local, "cookie_manager", mock_cm_staff, create=True):
			self.assertTrue(
				revoke_portal_session(
					session_id=s4.session_id,
					reason="Staff revoked",
					clear_response_cookie=False,
				)
			)
		mock_cm_staff.delete_cookie.assert_not_called()
		self.assertIsNone(verify_portal_session(s4.cookie_value))

	def test_cookie_manager_attributes_and_real_frappe_v16_flush(self):
		# Verify real Frappe v16 CookieManager signature supports our kwargs and emits proper Set-Cookie
		set_sig = inspect.signature(CookieManager.set_cookie)
		self.assertIn("httponly", set_sig.parameters)
		self.assertIn("secure", set_sig.parameters)
		self.assertIn("samesite", set_sig.parameters)
		self.assertIn("max_age", set_sig.parameters)
		self.assertTrue(hasattr(CookieManager, "delete_cookie"))

		settings = frappe.get_single("ISM Settings")
		orig_secure = settings.portal_session_secure_cookie
		orig_samesite = settings.portal_session_cookie_samesite
		orig_cookie_name = settings.portal_session_cookie_name
		orig_ttl_days = settings.portal_session_ttl_days

		try:
			# Default Secure=1, SameSite=Lax -> flush via real Frappe CookieManager to Werkzeug Response
			real_cm = CookieManager()
			with patch.object(frappe.local, "cookie_manager", real_cm, create=True):
				info = issue_portal_session(
					auth_method="sms_otp",
					phone_e164="+14165550190",
					set_response_cookie=True,
				)
				self._track_session(info.session_id)

			resp = Response("ok")
			real_cm.flush_cookies(resp)
			set_cookie_headers = resp.headers.getlist("Set-Cookie")
			matching = [h for h in set_cookie_headers if h.startswith(f"{DEFAULT_PORTAL_COOKIE_NAME}=")]
			self.assertEqual(len(matching), 1)
			header = matching[0]
			self.assertIn(info.cookie_value, header)
			self.assertIn("HttpOnly", header)
			self.assertIn("Secure", header)
			self.assertIn("SameSite=Lax", header)
			self.assertIn("Path=/", header)
			self.assertIn(f"Max-Age={int(orig_ttl_days or 30) * 86400}", header)

			# Clearing via real CookieManager emits expiring Set-Cookie
			clear_cm = CookieManager()
			with patch.object(frappe.local, "cookie_manager", clear_cm, create=True):
				clear_portal_session_cookie()
			clear_resp = Response("ok")
			clear_cm.flush_cookies(clear_resp)
			clear_headers = [
				h for h in clear_resp.headers.getlist("Set-Cookie") if h.startswith(f"{DEFAULT_PORTAL_COOKIE_NAME}=")
			]
			self.assertEqual(len(clear_headers), 1)
			clear_header = clear_headers[0]
			self.assertTrue(clear_header.startswith(f"{DEFAULT_PORTAL_COOKIE_NAME}=;"))
			self.assertIn("Path=/", clear_header)
			if "Max-Age=0" not in clear_header:
				expires_part = next(
					(seg.strip()[8:] for seg in clear_header.split(";") if seg.strip().startswith("Expires=")),
					None,
				)
				self.assertIsNotNone(expires_part, f"Expected Expires or Max-Age=0 in: {clear_header}")
				expires_dt = parsedate_to_datetime(expires_part)
				self.assertLess(expires_dt, datetime.now(UTC))

			# Mock CookieManager assertion with SameSite=Strict and Secure matrix
			frappe.db.set_single_value(
				"ISM Settings",
				{"portal_session_cookie_samesite": "Strict", "portal_session_secure_cookie": 1},
			)
			frappe.clear_cache(doctype="ISM Settings")
			mock_cm = MagicMock()
			with patch.object(frappe.local, "cookie_manager", mock_cm, create=True):
				set_portal_session_cookie("v1.a.b.c", max_age_seconds=3600)
			mock_cm.set_cookie.assert_called_once()
			call_args, call_kwargs = mock_cm.set_cookie.call_args
			self.assertEqual(call_args, (DEFAULT_PORTAL_COOKIE_NAME, "v1.a.b.c"))
			self.assertEqual(call_kwargs["max_age"], 3600)
			self.assertTrue(call_kwargs["httponly"])
			self.assertTrue(call_kwargs["secure"])
			self.assertEqual(call_kwargs["samesite"], "Strict")

			# Secure=False ONLY when portal_session_secure_cookie=0 AND active request is loopback HTTP
			frappe.db.set_single_value("ISM Settings", "portal_session_secure_cookie", 0)
			frappe.clear_cache(doctype="ISM Settings")

			with patch.object(frappe.local, "request", None, create=True):
				self.assertTrue(should_use_secure_cookie())

			loopback_http_slot = SimpleNamespace(
				scheme="http", is_secure=False, host="wt1.localhost:8001", headers={}
			)
			with patch.object(frappe.local, "request", loopback_http_slot, create=True):
				self.assertFalse(should_use_secure_cookie())

			loopback_ip_http = SimpleNamespace(
				scheme="http", is_secure=False, host="127.0.0.1:8001", headers={}
			)
			with patch.object(frappe.local, "request", loopback_ip_http, create=True):
				self.assertFalse(should_use_secure_cookie())

			loopback_https = SimpleNamespace(
				scheme="https", is_secure=True, host="wt1.localhost:8001", headers={}
			)
			with patch.object(frappe.local, "request", loopback_https, create=True):
				self.assertTrue(should_use_secure_cookie())

			proxied_https = SimpleNamespace(
				scheme="http",
				is_secure=False,
				host="wt1.localhost:8001",
				headers={"X-Forwarded-Proto": "https"},
			)
			with patch.object(frappe.local, "request", proxied_https, create=True):
				self.assertTrue(should_use_secure_cookie())

			non_loopback_http = SimpleNamespace(
				scheme="http", is_secure=False, host="shop.example.com", headers={}
			)
			with patch.object(frappe.local, "request", non_loopback_http, create=True):
				self.assertTrue(should_use_secure_cookie())

			# Graceful no-op when cookie_manager is absent
			with patch.object(frappe.local, "cookie_manager", None, create=True):
				set_portal_session_cookie("v1.a.b.c", max_age_seconds=3600)
				clear_portal_session_cookie()
		finally:
			frappe.db.set_single_value(
				"ISM Settings",
				{
					"portal_session_secure_cookie": orig_secure,
					"portal_session_cookie_samesite": orig_samesite or "Lax",
					"portal_session_cookie_name": orig_cookie_name or DEFAULT_PORTAL_COOKIE_NAME,
					"portal_session_ttl_days": orig_ttl_days or 30,
				},
			)
			frappe.clear_cache(doctype="ISM Settings")

	def test_cleanup_expired_portal_sessions(self):
		now = now_datetime()

		def _insert_raw(*, status: str, expires_days: int, revoked_days: int | None = None) -> str:
			doc = frappe.get_doc({
				"doctype": "Customer Portal Session",
				"session_id": f"sess_clean_{frappe.generate_hash(length=12)}",
				"token_hash": frappe.generate_hash(length=64),
				"phone_e164": "+14165550198",
				"auth_method": "sms_otp",
				"scope_type": "customer",
				"status": status,
				"issued_at": add_to_date(now, days=min(expires_days - 30, -1)),
				"expires_at": add_to_date(now, days=expires_days),
				"revoked_at": add_to_date(now, days=revoked_days) if revoked_days is not None else None,
			}).insert(ignore_permissions=True)
			self.track(doc)
			return doc.name

		active_valid = _insert_raw(status="Active", expires_days=10)
		active_overdue_recent = _insert_raw(status="Active", expires_days=-2)
		expired_old = _insert_raw(status="Expired", expires_days=-95)
		revoked_old = _insert_raw(status="Revoked", expires_days=-60, revoked_days=-95)
		rotated_old = _insert_raw(status="Rotated", expires_days=-60, revoked_days=-95)
		revoked_recent = _insert_raw(status="Revoked", expires_days=20, revoked_days=-5)

		cleaned = cleanup_expired_portal_sessions()
		self.assertGreaterEqual(cleaned, 4)

		self.assertEqual(frappe.db.get_value("Customer Portal Session", active_valid, "status"), "Active")
		self.assertEqual(
			frappe.db.get_value("Customer Portal Session", active_overdue_recent, "status"), "Expired"
		)
		self.assertEqual(frappe.db.get_value("Customer Portal Session", revoked_recent, "status"), "Revoked")
		self.assertFalse(frappe.db.exists("Customer Portal Session", expired_old))
		self.assertFalse(frappe.db.exists("Customer Portal Session", revoked_old))
		self.assertFalse(frappe.db.exists("Customer Portal Session", rotated_old))
