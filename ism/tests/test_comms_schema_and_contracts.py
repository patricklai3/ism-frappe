# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from ism import hooks as ism_hooks
from ism.comms import (
	DEFAULT_PORTAL_COOKIE_NAME,
	HELP_KEYWORDS,
	OTP_PURPOSES,
	SESSION_AUTH_METHODS,
	SESSION_SCOPE_TYPES,
	START_KEYWORDS,
	STOP_KEYWORDS,
	SUPPORTED_SMS_PROVIDERS,
	TELNYX_MESSAGES_ENDPOINT,
	TELNYX_OPT_OUT_ERROR_CODES,
	EmailDispatchResult,
	OTPChallengeResult,
	OTPVerifyResult,
	PortalSessionInfo,
	SMSDispatchRequest,
	SMSDispatchResult,
	get_client_ip,
	is_valid_sms_webhook_url,
	mask_phone_e164,
	normalize_phone_e164,
	redact_otp_body,
)
from ism.setup_tasks.seed import setup_default_ism_settings
from ism.tests.base import _TEARDOWN_ORDER, ISMTestCase


class TestCommsSchemaAndContracts(ISMTestCase):
	"""Verify Wave 1A frozen contracts, 4 comms DocTypes, redaction guards, and hooks."""

	def test_frozen_contract_constants_and_dataclasses(self):
		self.assertEqual(
			SUPPORTED_SMS_PROVIDERS,
			("Disabled", "Telnyx", "Custom HTTP Webhook", "Mock (Deterministic Test)"),
		)
		self.assertEqual(TELNYX_MESSAGES_ENDPOINT, "https://api.telnyx.com/v2/messages")
		self.assertEqual(OTP_PURPOSES, ("portal_login", "booking_verify", "portal_action"))
		self.assertEqual(SESSION_AUTH_METHODS, ("sms_otp", "magic_link", "booking_otp"))
		self.assertEqual(SESSION_SCOPE_TYPES, ("customer", "repair_order", "booking"))
		self.assertEqual(DEFAULT_PORTAL_COOKIE_NAME, "ism_portal_session")
		self.assertIn("STOP", STOP_KEYWORDS)
		self.assertIn("START", START_KEYWORDS)
		self.assertIn("HELP", HELP_KEYWORDS)
		self.assertEqual(TELNYX_OPT_OUT_ERROR_CODES, frozenset({"40300", "40008"}))

		req = SMSDispatchRequest(to_phone="+14165550199", body="Hello")
		self.assertEqual(req.to_dict()["message_type"], "Notification")
		self.assertTrue(req.to_dict()["respect_opt_out"])

		res = SMSDispatchResult(success=True, provider="Mock (Deterministic Test)", status="Sent")
		self.assertTrue(res.to_dict()["success"])

		chal = OTPChallengeResult(success=True, challenge_id="c1", phone_masked="+1******0199")
		self.assertEqual(chal.to_dict()["challenge_id"], "c1")

		ver = OTPVerifyResult(verified=True, challenge_id="c1", phone_e164="+14165550199")
		self.assertTrue(ver.to_dict()["verified"])

		email_res = EmailDispatchResult(success=True, recipient="a@example.com", email_type="invoice")
		self.assertEqual(email_res.to_dict()["recipient"], "a@example.com")

		sess = PortalSessionInfo(
			session_id="sess_123",
			status="Active",
			auth_method="sms_otp",
			scope_type="customer",
			cookie_value="v1.sess_123.secret.sig",
		)
		self.assertNotIn("cookie_value", sess.to_dict())
		self.assertEqual(sess.to_dict(include_cookie=True)["cookie_value"], "v1.sess_123.secret.sig")

	def test_normalize_phone_e164_edge_cases(self):
		self.assertEqual(normalize_phone_e164("416-555-0199"), "+14165550199")
		self.assertEqual(normalize_phone_e164("(416) 555.0199"), "+14165550199")
		self.assertEqual(normalize_phone_e164("1-416-555-0199"), "+14165550199")
		self.assertEqual(normalize_phone_e164("tel:+1 (416) 555-0199"), "+14165550199")
		self.assertEqual(normalize_phone_e164("00442079460958"), "+442079460958")
		self.assertEqual(normalize_phone_e164("+44 20 7946 0958"), "+442079460958")
		self.assertEqual(normalize_phone_e164("020 7946 0958", default_country_code="+44"), "+442079460958")

		invalid_numbers = [
			"",
			"   ",
			"user@example.com",
			"416-555-0199 ext 12",
			"416-555-0199\n",
			"016-555-0199",
			"416-155-0199",
			"12345",
			"+0123456789",
			"+1234567890123456",
			"416+5550199",
		]
		for bad in invalid_numbers:
			with self.subTest(raw=bad):
				with self.assertRaises(ValueError):
					normalize_phone_e164(bad)

	def test_mask_phone_and_redact_otp_body(self):
		self.assertEqual(mask_phone_e164("+14165550199"), "+1******0199")
		self.assertEqual(mask_phone_e164("+442079460958"), "+44******0958")
		self.assertEqual(mask_phone_e164(""), "")

		self.assertEqual(
			redact_otp_body("Your verification code is 482910. Expires in 5 minutes."),
			"Your verification code is [REDACTED]. Expires in 5 minutes.",
		)
		self.assertEqual(
			redact_otp_body(
				"Your verification code is 482910.",
				preview_override="Code 991122 was sent. Valid 5 min.",
			),
			"Code [REDACTED] was sent. Valid 5 min.",
		)
		self.assertEqual(redact_otp_body(""), "[REDACTED]")

	def test_is_valid_sms_webhook_url_and_client_ip(self):
		self.assertTrue(is_valid_sms_webhook_url("https://relay.example.com/sms"))
		self.assertTrue(is_valid_sms_webhook_url("http://127.0.0.1:8080/sms"))
		self.assertTrue(is_valid_sms_webhook_url("http://localhost:8080/sms"))
		self.assertFalse(is_valid_sms_webhook_url("http://relay.example.com/sms"))
		self.assertFalse(is_valid_sms_webhook_url("https://169.254.169.254/latest"))
		self.assertFalse(is_valid_sms_webhook_url("https://metadata.google.internal/"))
		self.assertFalse(is_valid_sms_webhook_url("https://user:pass@relay.example.com/sms"))

		with patch.object(frappe.local, "request", None, create=True):
			self.assertEqual(get_client_ip(ip_override="198.51.100.42"), "198.51.100.42")

		fake_req = SimpleNamespace(remote_addr="203.0.113.10")
		with (
			patch.object(frappe.local, "request", fake_req, create=True),
			patch.object(frappe.local, "request_ip", "203.0.113.10", create=True),
		):
			self.assertEqual(get_client_ip(ip_override="198.51.100.42"), "203.0.113.10")

	def test_sms_audit_log_otp_redaction_and_truncation(self):
		otp_log = frappe.get_doc({
			"doctype": "SMS Audit Log",
			"direction": "Outbound",
			"provider": "Mock (Deterministic Test)",
			"message_type": "OTP",
			"phone_e164": "+14165550199",
			"status": "Sent",
			"is_redacted": 0,
			"message_preview": "Your ISM code is 739104. Valid 5 minutes.",
		}).insert(ignore_permissions=True)
		self.track(otp_log)

		self.assertEqual(int(otp_log.is_redacted), 1)
		self.assertNotIn("739104", otp_log.message_preview)
		self.assertIn("[REDACTED]", otp_log.message_preview)

		long_log = frappe.get_doc({
			"doctype": "SMS Audit Log",
			"direction": "Outbound",
			"provider": "Mock (Deterministic Test)",
			"message_type": "Notification",
			"phone_e164": "+14165550199",
			"status": "Sent",
			"message_preview": "A" * 650,
		}).insert(ignore_permissions=True)
		self.track(long_log)
		self.assertEqual(len(long_log.message_preview), 500)

	def test_sms_opt_out_normalization_and_validation(self):
		opt_out = frappe.get_doc({
			"doctype": "SMS Opt Out",
			"phone_e164": "(416) 555-0144",
			"is_opted_out": 1,
			"source": "Telnyx STOP Keyword",
			"last_keyword": "STOP",
		}).insert(ignore_permissions=True)
		self.track(opt_out)
		self.assertEqual(opt_out.name, "+14165550144")
		self.assertEqual(opt_out.phone_e164, "+14165550144")

		bad_doc = frappe.get_doc({
			"doctype": "SMS Opt Out",
			"phone_e164": "not-a-phone",
		})
		with self.assertRaises(frappe.ValidationError):
			bad_doc.insert(ignore_permissions=True)

	def test_otp_challenge_and_portal_session_doctypes(self):
		now = now_datetime()
		challenge = frappe.get_doc({
			"doctype": "Portal OTP Challenge",
			"challenge_id": f"chal_{frappe.generate_hash(length=12)}",
			"purpose": "portal_login",
			"phone_e164": "+14165550199",
			"code_salt": "a" * 32,
			"code_hash": "b" * 64,
			"status": "Pending",
			"expires_at": add_to_date(now, seconds=300),
		}).insert(ignore_permissions=True)
		self.track(challenge)
		self.assertEqual(challenge.status, "Pending")

		session = frappe.get_doc({
			"doctype": "Customer Portal Session",
			"session_id": f"sess_{frappe.generate_hash(length=12)}",
			"token_hash": frappe.generate_hash(length=64),
			"phone_e164": "+14165550199",
			"auth_method": "sms_otp",
			"scope_type": "customer",
			"status": "Active",
			"issued_at": now,
			"expires_at": add_to_date(now, days=30),
			"source_otp_challenge": challenge.name,
		}).insert(ignore_permissions=True)
		self.track(session)
		self.assertEqual(session.status, "Active")

	def test_hooks_teardown_and_seed_idempotency(self):
		daily = ism_hooks.scheduler_events.get("daily", [])
		self.assertIn("ism.comms.otp.cleanup_expired_otp_challenges", daily)
		self.assertIn("ism.comms.sessions.cleanup_expired_portal_sessions", daily)
		self.assertEqual(ism_hooks.default_log_clearing_doctypes.get("SMS Audit Log"), 180)
		for dt in ("Repair Order Schedule Entry", "SMS Audit Log", "Portal OTP Challenge", "Customer Portal Session"):
			self.assertIn(dt, ism_hooks.ignore_links_on_delete)

		self.assertLess(_TEARDOWN_ORDER["Customer Portal Session"], _TEARDOWN_ORDER["Repair Order"])
		self.assertLess(_TEARDOWN_ORDER["Portal OTP Challenge"], _TEARDOWN_ORDER["Repair Order"])
		self.assertLess(_TEARDOWN_ORDER["SMS Audit Log"], _TEARDOWN_ORDER["Repair Order"])
		self.assertLess(_TEARDOWN_ORDER["SMS Opt Out"], _TEARDOWN_ORDER["Customer"])

		settings = frappe.get_single("ISM Settings")
		original_secret = settings.get_password("portal_session_signing_secret", raise_exception=False)
		self.assertTrue(original_secret)

		frappe.db.set_single_value("ISM Settings", {"sms_enforce_opt_out": 0, "portal_session_secure_cookie": 0})
		frappe.clear_cache(doctype="ISM Settings")
		try:
			setup_default_ism_settings()
			reloaded = frappe.get_single("ISM Settings")
			self.assertEqual(int(reloaded.sms_enforce_opt_out), 0)
			self.assertEqual(int(reloaded.portal_session_secure_cookie), 0)
			self.assertEqual(
				reloaded.get_password("portal_session_signing_secret", raise_exception=False),
				original_secret,
			)
		finally:
			frappe.db.rollback()
			frappe.clear_cache(doctype="ISM Settings")
