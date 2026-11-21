# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import add_to_date, now_datetime

import ism.comms as comms_facade
from ism.comms.otp import (
	_OTP_COOLDOWN_PREFIX,
	_hash_phone,
	check_otp_lockout,
	cleanup_expired_otp_challenges,
	clear_comms_rate_limits,
	consume_verified_otp_challenge,
	get_last_mock_otp,
	request_sms_otp,
	verify_sms_otp,
)
from ism.comms.providers import (
	clear_mock_sms_outbox,
	set_mock_sms_next_error,
	upsert_sms_opt_out,
)
from ism.tests.base import ISMTestCase


class TestCommsOTP(ISMTestCase):
	"""Verify Wave 3A SMS-only OTP challenges, hashing, throttling, lockout, redaction, and facade exports."""

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
	)

	def setUp(self):
		super().setUp()
		self._saved_settings = {}
		settings = frappe.get_single("ISM Settings")
		for fieldname in self._SETTINGS_FIELDS:
			self._saved_settings[fieldname] = getattr(settings, fieldname, None)

		settings.sms_gateway_provider = "Mock (Deterministic Test)"
		settings.sms_default_country_code = "+1"
		settings.sms_enforce_opt_out = 1
		settings.otp_code_length = 6
		settings.otp_ttl_seconds = 300
		settings.otp_resend_cooldown_seconds = 60
		settings.otp_max_verify_attempts = 5
		settings.otp_lockout_seconds = 900
		settings.otp_phone_rate_limit_per_hour = 5
		settings.otp_ip_rate_limit_per_hour = 20
		settings.save(ignore_permissions=True)

		clear_comms_rate_limits()
		clear_mock_sms_outbox()
		frappe.clear_cache(doctype="ISM Settings")

	def tearDown(self):
		clear_comms_rate_limits()
		clear_mock_sms_outbox()
		try:
			settings = frappe.get_single("ISM Settings")
			for fieldname, value in self._saved_settings.items():
				setattr(settings, fieldname, value)
			settings.save(ignore_permissions=True)
			frappe.clear_cache(doctype="ISM Settings")
		finally:
			super().tearDown()

	def _track_challenges_for_phone(self, phone_e164: str) -> list[str]:
		names = frappe.get_all("Portal OTP Challenge", filters={"phone_e164": phone_e164}, pluck="name")
		for name in names:
			self.track("Portal OTP Challenge", name)
		audit_names = frappe.get_all("SMS Audit Log", filters={"phone_e164": phone_e164}, pluck="name")
		for name in audit_names:
			self.track("SMS Audit Log", name)
		return names

	def _clear_phone_cooldown_only(self, phone_e164: str) -> None:
		cache = frappe.cache()
		key = f"{_OTP_COOLDOWN_PREFIX}:{_hash_phone(phone_e164)}"
		if hasattr(cache, "make_key") and hasattr(cache, "delete"):
			cache.delete(cache.make_key(key))
		else:
			cache.delete_value(key)

	def test_happy_path_mock_otp_verify_single_use_and_zero_plaintext_audit(self):
		phone = "(416) 555-0141"
		phone_e164 = "+14165550141"

		req_res = request_sms_otp(
			phone,
			purpose="portal_login",
			client_ip="198.51.100.11",
			user_agent="ISM-OTP-Test/1.0",
		)
		self._track_challenges_for_phone(phone_e164)

		self.assertTrue(req_res.success)
		self.assertIsNotNone(req_res.challenge_id)
		self.assertEqual(req_res.phone_masked, "+1******0141")
		self.assertEqual(req_res.expires_in_seconds, 300)
		self.assertEqual(req_res.resend_available_in_seconds, 60)
		self.assertIsNone(req_res.error_code)

		code = get_last_mock_otp(phone_e164)
		self.assertIsNotNone(code)
		self.assertEqual(len(code), 6)
		self.assertTrue(code.isdigit())

		# Mandatory zero-plaintext check on Portal OTP Challenge and SMS Audit Log
		challenge_name = frappe.db.get_value(
			"Portal OTP Challenge",
			{"challenge_id": req_res.challenge_id},
			"name",
		)
		self.assertIsNotNone(challenge_name)
		challenge_doc = frappe.get_doc("Portal OTP Challenge", challenge_name)
		self.assertEqual(challenge_doc.status, "Pending")
		self.assertEqual(len(challenge_doc.code_salt), 32)
		self.assertEqual(len(challenge_doc.code_hash), 64)
		self.assertNotEqual(challenge_doc.code_hash, code)
		for value in challenge_doc.as_dict().values():
			if isinstance(value, str):
				self.assertNotIn(code, value)

		self.assertTrue(challenge_doc.sms_audit_log)
		audit_doc = frappe.get_doc("SMS Audit Log", challenge_doc.sms_audit_log)
		self.assertEqual(audit_doc.message_type, "OTP")
		self.assertEqual(audit_doc.is_redacted, 1)
		self.assertEqual(audit_doc.reference_doctype, "Portal OTP Challenge")
		self.assertEqual(audit_doc.reference_name, challenge_doc.name)
		self.assertIn("[REDACTED]", audit_doc.message_preview)
		for value in audit_doc.as_dict().values():
			if isinstance(value, str):
				self.assertNotIn(code, value)

		# Wrong purpose / phone must be rejected without mutating attempts
		wrong_purpose = verify_sms_otp(req_res.challenge_id, code, purpose="booking_verify")
		self.assertFalse(wrong_purpose.verified)
		self.assertEqual(wrong_purpose.error_code, "PURPOSE_MISMATCH")

		wrong_phone = verify_sms_otp(req_res.challenge_id, code, phone="+14165550199")
		self.assertFalse(wrong_phone.verified)
		self.assertEqual(wrong_phone.error_code, "PHONE_MISMATCH")

		challenge_doc.reload()
		self.assertEqual(challenge_doc.attempts, 0)
		self.assertEqual(challenge_doc.status, "Pending")

		# Valid single-use verification
		verify_res = verify_sms_otp(
			req_res.challenge_id,
			code,
			phone=phone,
			purpose="portal_login",
			consume=True,
		)
		self.assertTrue(verify_res.verified)
		self.assertEqual(verify_res.challenge_id, req_res.challenge_id)
		self.assertEqual(verify_res.phone_e164, phone_e164)
		self.assertEqual(verify_res.purpose, "portal_login")

		challenge_doc.reload()
		self.assertEqual(challenge_doc.status, "Verified")
		self.assertIsNotNone(challenge_doc.verified_at)
		self.assertIsNotNone(challenge_doc.consumed_at)

		# Replay must be rejected
		replay_res = verify_sms_otp(req_res.challenge_id, code, purpose="portal_login")
		self.assertFalse(replay_res.verified)
		self.assertEqual(replay_res.error_code, "ALREADY_USED")

	def test_sms_only_rejects_email_invalid_phone_and_invalid_purpose(self):
		with self.assertRaises(ValueError):
			request_sms_otp("customer@example.com")

		with self.assertRaises(ValueError):
			check_otp_lockout("customer@example.com")

		with self.assertRaises(ValueError):
			request_sms_otp("not-a-phone")

		with self.assertRaises(ValueError):
			request_sms_otp("")

		with self.assertRaises(ValueError):
			request_sms_otp("+14165550141", purpose="email_fallback")

	def test_two_step_consume_false_and_consume_verified_otp_challenge(self):
		phone_e164 = "+14165550142"
		req_res = request_sms_otp(phone_e164, purpose="booking_verify", client_ip="198.51.100.12")
		self._track_challenges_for_phone(phone_e164)
		self.assertTrue(req_res.success)

		# Unverified challenge cannot be consumed
		premature = consume_verified_otp_challenge(req_res.challenge_id, expected_purpose="booking_verify")
		self.assertFalse(premature.verified)
		self.assertEqual(premature.error_code, "NOT_VERIFIED")

		code = get_last_mock_otp(phone_e164)
		verify_res = verify_sms_otp(
			req_res.challenge_id,
			code,
			purpose="booking_verify",
			consume=False,
		)
		self.assertTrue(verify_res.verified)

		challenge_name = frappe.db.get_value(
			"Portal OTP Challenge",
			{"challenge_id": req_res.challenge_id},
			"name",
		)
		challenge_doc = frappe.get_doc("Portal OTP Challenge", challenge_name)
		self.assertEqual(challenge_doc.status, "Verified")
		self.assertIsNotNone(challenge_doc.verified_at)
		self.assertIsNone(challenge_doc.consumed_at)

		# Mismatched purpose/phone rejected without consuming
		bad_purpose = consume_verified_otp_challenge(req_res.challenge_id, expected_purpose="portal_login")
		self.assertFalse(bad_purpose.verified)
		self.assertEqual(bad_purpose.error_code, "PURPOSE_MISMATCH")

		bad_phone = consume_verified_otp_challenge(
			req_res.challenge_id,
			expected_purpose="booking_verify",
			expected_phone="+14165550199",
		)
		self.assertFalse(bad_phone.verified)
		self.assertEqual(bad_phone.error_code, "PHONE_MISMATCH")

		# Atomic single-use consumption succeeds once
		consumed = consume_verified_otp_challenge(
			req_res.challenge_id,
			expected_purpose="booking_verify",
			expected_phone="(416) 555-0142",
		)
		self.assertTrue(consumed.verified)
		self.assertEqual(consumed.phone_e164, phone_e164)
		self.assertEqual(consumed.purpose, "booking_verify")

		# Second consumption fails
		second = consume_verified_otp_challenge(req_res.challenge_id, expected_purpose="booking_verify")
		self.assertFalse(second.verified)
		self.assertEqual(second.error_code, "ALREADY_CONSUMED")

		# Grace window expiry check on a second verified unconsumed challenge
		self._clear_phone_cooldown_only(phone_e164)
		req_res2 = request_sms_otp(phone_e164, purpose="booking_verify", client_ip="198.51.100.12")
		self._track_challenges_for_phone(phone_e164)
		code2 = get_last_mock_otp(phone_e164)
		self.assertTrue(verify_sms_otp(req_res2.challenge_id, code2, consume=False).verified)
		chal2_name = frappe.db.get_value(
			"Portal OTP Challenge",
			{"challenge_id": req_res2.challenge_id},
			"name",
		)
		frappe.db.set_value(
			"Portal OTP Challenge",
			chal2_name,
			"verified_at",
			add_to_date(now_datetime(), minutes=-20),
		)
		grace_res = consume_verified_otp_challenge(req_res2.challenge_id, expected_purpose="booking_verify")
		self.assertFalse(grace_res.verified)
		self.assertEqual(grace_res.error_code, "GRACE_EXPIRED")

	def test_wrong_code_attempt_countdown_lockout_and_blocking_new_requests(self):
		settings = frappe.get_single("ISM Settings")
		settings.otp_max_verify_attempts = 3
		settings.otp_lockout_seconds = 600
		settings.save(ignore_permissions=True)

		phone_e164 = "+14165550143"
		req_res = request_sms_otp(phone_e164, purpose="portal_login", client_ip="198.51.100.13")
		self._track_challenges_for_phone(phone_e164)
		self.assertTrue(req_res.success)

		real_code = get_last_mock_otp(phone_e164)
		wrong_code = "000000" if real_code != "000000" else "999999"

		attempt1 = verify_sms_otp(req_res.challenge_id, wrong_code)
		self.assertFalse(attempt1.verified)
		self.assertEqual(attempt1.error_code, "INVALID_CODE")
		self.assertEqual(attempt1.attempts_remaining, 2)

		attempt2 = verify_sms_otp(req_res.challenge_id, wrong_code)
		self.assertFalse(attempt2.verified)
		self.assertEqual(attempt2.error_code, "INVALID_CODE")
		self.assertEqual(attempt2.attempts_remaining, 1)

		attempt3 = verify_sms_otp(req_res.challenge_id, wrong_code)
		self.assertFalse(attempt3.verified)
		self.assertEqual(attempt3.error_code, "LOCKED_OUT")
		self.assertEqual(attempt3.attempts_remaining, 0)
		self.assertIsNotNone(attempt3.locked_until)

		lock_info = check_otp_lockout(phone_e164)
		self.assertTrue(lock_info["locked"])
		self.assertGreater(lock_info["retry_after_seconds"], 0)
		self.assertIsNotNone(lock_info["locked_until"])

		# Even the real code must be rejected once locked
		real_after_lock = verify_sms_otp(req_res.challenge_id, real_code)
		self.assertFalse(real_after_lock.verified)
		self.assertEqual(real_after_lock.error_code, "LOCKED_OUT")

		# New OTP requests are blocked during lockout without creating rows
		count_before = frappe.db.count("Portal OTP Challenge", {"phone_e164": phone_e164})
		blocked_req = request_sms_otp(phone_e164, purpose="portal_login", client_ip="198.51.100.13")
		self.assertFalse(blocked_req.success)
		self.assertEqual(blocked_req.error_code, "LOCKED_OUT")
		self.assertIsNotNone(blocked_req.locked_until)
		self.assertGreater(blocked_req.resend_available_in_seconds, 0)
		self.assertEqual(frappe.db.count("Portal OTP Challenge", {"phone_e164": phone_e164}), count_before)

		# Once lockout expires in DB, check_otp_lockout clears and returns unlocked
		chal_name = frappe.db.get_value("Portal OTP Challenge", {"challenge_id": req_res.challenge_id}, "name")
		frappe.db.set_value(
			"Portal OTP Challenge",
			chal_name,
			"locked_until",
			add_to_date(now_datetime(), seconds=-5),
		)
		self.assertFalse(check_otp_lockout(phone_e164)["locked"])

	def test_resend_cooldown_and_superseding_prior_pending_challenge(self):
		phone_e164 = "+14165550144"

		res1 = request_sms_otp(phone_e164, purpose="portal_login", client_ip="198.51.100.14")
		self._track_challenges_for_phone(phone_e164)
		self.assertTrue(res1.success)
		code1 = get_last_mock_otp(phone_e164)

		# Immediate resend blocked by cooldown
		cooldown_res = request_sms_otp(phone_e164, purpose="portal_login", client_ip="198.51.100.14")
		self.assertFalse(cooldown_res.success)
		self.assertEqual(cooldown_res.error_code, "COOLDOWN_ACTIVE")
		self.assertGreater(cooldown_res.resend_available_in_seconds, 0)

		# Simulate cooldown expiry; new request supersedes prior Pending challenge
		self._clear_phone_cooldown_only(phone_e164)
		res2 = request_sms_otp(phone_e164, purpose="portal_login", client_ip="198.51.100.14")
		self._track_challenges_for_phone(phone_e164)
		self.assertTrue(res2.success)
		self.assertNotEqual(res1.challenge_id, res2.challenge_id)
		code2 = get_last_mock_otp(phone_e164)

		chal1_status = frappe.db.get_value(
			"Portal OTP Challenge",
			{"challenge_id": res1.challenge_id},
			"status",
		)
		self.assertEqual(chal1_status, "Superseded")

		old_verify = verify_sms_otp(res1.challenge_id, code1)
		self.assertFalse(old_verify.verified)
		self.assertEqual(old_verify.error_code, "SUPERSEDED")

		new_verify = verify_sms_otp(res2.challenge_id, code2)
		self.assertTrue(new_verify.verified)

	def test_per_phone_and_per_ip_hourly_rate_limits(self):
		settings = frappe.get_single("ISM Settings")
		settings.otp_phone_rate_limit_per_hour = 2
		settings.otp_ip_rate_limit_per_hour = 3
		settings.save(ignore_permissions=True)

		# 1. Per-phone hourly limit (2 allowed, 3rd rejected)
		phone_target = "+14165550145"
		r1 = request_sms_otp(phone_target, client_ip="198.51.100.21")
		self._clear_phone_cooldown_only(phone_target)
		r2 = request_sms_otp(phone_target, client_ip="198.51.100.22")
		self._clear_phone_cooldown_only(phone_target)
		r3 = request_sms_otp(phone_target, client_ip="198.51.100.23")
		self._track_challenges_for_phone(phone_target)

		self.assertTrue(r1.success)
		self.assertTrue(r2.success)
		self.assertFalse(r3.success)
		self.assertEqual(r3.error_code, "RATE_LIMITED")
		self.assertGreater(r3.resend_available_in_seconds, 0)

		# 2. Per-IP hourly limit across distinct phones (3 allowed, 4th rejected)
		shared_ip = "203.0.113.77"
		ip_phones = ("+14165550146", "+14165550147", "+14165550148")
		for ph in ip_phones:
			res = request_sms_otp(ph, client_ip=shared_ip)
			self._track_challenges_for_phone(ph)
			self.assertTrue(res.success)

		fourth_phone = "+14165550149"
		ip_blocked = request_sms_otp(fourth_phone, client_ip=shared_ip)
		self._track_challenges_for_phone(fourth_phone)
		self.assertFalse(ip_blocked.success)
		self.assertEqual(ip_blocked.error_code, "RATE_LIMITED")
		self.assertGreater(ip_blocked.resend_available_in_seconds, 0)

		# Confirm 4th phone's own cooldown/quota was not burned by the IP block
		other_ip_res = request_sms_otp(fourth_phone, client_ip="203.0.113.88")
		self._track_challenges_for_phone(fourth_phone)
		self.assertTrue(other_ip_res.success)

	def test_opted_out_phone_rejected_without_burning_quota(self):
		phone_e164 = "+14165550150"
		opt_name = upsert_sms_opt_out(phone_e164, is_opted_out=True, source="Telnyx STOP Keyword")
		self.track("SMS Opt Out", opt_name)

		blocked = request_sms_otp(phone_e164, client_ip="198.51.100.50")
		self.assertFalse(blocked.success)
		self.assertEqual(blocked.error_code, "OPTED_OUT")
		self.assertIn("START", blocked.error)
		self.assertEqual(frappe.db.count("Portal OTP Challenge", {"phone_e164": phone_e164}), 0)

		# Re-opt-in immediately succeeds (proves opt-out check did not burn cooldown/quota)
		upsert_sms_opt_out(phone_e164, is_opted_out=False, source="Customer Re-Opt-In (START)")
		allowed = request_sms_otp(phone_e164, client_ip="198.51.100.50")
		self._track_challenges_for_phone(phone_e164)
		self.assertTrue(allowed.success)

	def test_expiry_and_delivery_failure(self):
		# 1. Expired challenge
		phone_exp = "+14165550151"
		req_exp = request_sms_otp(phone_exp, client_ip="198.51.100.51")
		self._track_challenges_for_phone(phone_exp)
		self.assertTrue(req_exp.success)
		code_exp = get_last_mock_otp(phone_exp)

		chal_name = frappe.db.get_value(
			"Portal OTP Challenge",
			{"challenge_id": req_exp.challenge_id},
			"name",
		)
		frappe.db.set_value(
			"Portal OTP Challenge",
			chal_name,
			"expires_at",
			add_to_date(now_datetime(), seconds=-10),
		)

		exp_verify = verify_sms_otp(req_exp.challenge_id, code_exp)
		self.assertFalse(exp_verify.verified)
		self.assertEqual(exp_verify.error_code, "EXPIRED")
		self.assertEqual(frappe.db.get_value("Portal OTP Challenge", chal_name, "status"), "Expired")

		# 2. Carrier delivery failure
		phone_fail = "+14165550152"
		set_mock_sms_next_error("50001", "Simulated carrier outage")
		fail_res = request_sms_otp(phone_fail, client_ip="198.51.100.52")
		fail_names = self._track_challenges_for_phone(phone_fail)

		self.assertFalse(fail_res.success)
		self.assertEqual(fail_res.error_code, "50001")
		self.assertEqual(len(fail_names), 1)
		fail_doc = frappe.get_doc("Portal OTP Challenge", fail_names[0])
		self.assertEqual(fail_doc.status, "Delivery Failed")
		self.assertTrue(fail_doc.sms_audit_log)

	def test_get_last_mock_otp_strictly_gated_to_mock_provider(self):
		phone_e164 = "+14165550153"
		req_res = request_sms_otp(phone_e164, client_ip="198.51.100.53")
		self._track_challenges_for_phone(phone_e164)
		self.assertTrue(req_res.success)

		mock_code = get_last_mock_otp(phone_e164)
		self.assertIsNotNone(mock_code)

		for non_mock_provider in ("Disabled", "Telnyx", "Custom HTTP Webhook"):
			frappe.db.set_single_value("ISM Settings", "sms_gateway_provider", non_mock_provider)
			frappe.clear_cache(doctype="ISM Settings")
			self.assertIsNone(
				get_last_mock_otp(phone_e164),
				f"get_last_mock_otp must return None when provider is {non_mock_provider!r}",
			)

		frappe.db.set_single_value("ISM Settings", "sms_gateway_provider", "Mock (Deterministic Test)")
		frappe.clear_cache(doctype="ISM Settings")
		self.assertEqual(get_last_mock_otp(phone_e164), mock_code)

	def test_cleanup_expired_otp_challenges_and_public_facade_exports(self):
		now = now_datetime()

		overdue_doc = frappe.get_doc(
			{
				"doctype": "Portal OTP Challenge",
				"challenge_id": f"otp_overdue_{frappe.generate_hash(length=10)}",
				"purpose": "portal_login",
				"phone_e164": "+14165550154",
				"code_salt": "a" * 32,
				"code_hash": "b" * 64,
				"status": "Pending",
				"expires_at": add_to_date(now, minutes=-5),
			}
		).insert(ignore_permissions=True)
		self.track(overdue_doc)

		old_doc = frappe.get_doc(
			{
				"doctype": "Portal OTP Challenge",
				"challenge_id": f"otp_old_{frappe.generate_hash(length=10)}",
				"purpose": "portal_login",
				"phone_e164": "+14165550155",
				"code_salt": "c" * 32,
				"code_hash": "d" * 64,
				"status": "Verified",
				"expires_at": add_to_date(now, days=-10),
			}
		).insert(ignore_permissions=True)
		self.track(old_doc)
		frappe.db.sql(
			"UPDATE `tabPortal OTP Challenge` SET creation = %s WHERE name = %s",
			(add_to_date(now, days=-10), old_doc.name),
		)

		fresh_doc = frappe.get_doc(
			{
				"doctype": "Portal OTP Challenge",
				"challenge_id": f"otp_fresh_{frappe.generate_hash(length=10)}",
				"purpose": "portal_login",
				"phone_e164": "+14165550156",
				"code_salt": "e" * 32,
				"code_hash": "f" * 64,
				"status": "Pending",
				"expires_at": add_to_date(now, minutes=5),
			}
		).insert(ignore_permissions=True)
		self.track(fresh_doc)

		affected = cleanup_expired_otp_challenges()
		self.assertGreaterEqual(affected, 2)

		self.assertEqual(frappe.db.get_value("Portal OTP Challenge", overdue_doc.name, "status"), "Expired")
		self.assertFalse(frappe.db.exists("Portal OTP Challenge", old_doc.name))
		self.assertEqual(frappe.db.get_value("Portal OTP Challenge", fresh_doc.name, "status"), "Pending")

		# Final public facade ism.comms exports check
		required_exports = {
			# contracts
			"SUPPORTED_SMS_PROVIDERS",
			"OTP_PURPOSES",
			"SMSDispatchRequest",
			"SMSDispatchResult",
			"OTPChallengeResult",
			"OTPVerifyResult",
			"PortalSessionInfo",
			"EmailDispatchResult",
			"normalize_phone_e164",
			"mask_phone_e164",
			"redact_otp_body",
			# providers
			"MockSMSProvider",
			"send_sms",
			"is_phone_opted_out",
			"upsert_sms_opt_out",
			# telnyx
			"TelnyxSMSProvider",
			"verify_telnyx_webhook_signature",
			"verify_webhook_request",
			"process_telnyx_webhook_event",
			# otp
			"request_sms_otp",
			"verify_sms_otp",
			"consume_verified_otp_challenge",
			"check_otp_lockout",
			"get_last_mock_otp",
			"clear_comms_rate_limits",
			"cleanup_expired_otp_challenges",
			# sessions
			"issue_portal_session",
			"verify_portal_session",
			"rotate_portal_session",
			"revoke_portal_session",
			"cleanup_expired_portal_sessions",
			# email_dispatch
			"dispatch_transactional_email",
			"clear_email_rate_limits",
		}
		exported = set(comms_facade.__all__)
		self.assertTrue(required_exports.issubset(exported))
		for sym in comms_facade.__all__:
			self.assertTrue(hasattr(comms_facade, sym), f"Missing attribute on ism.comms: {sym}")
