# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ism.api import sms as sms_api
from ism.api.comms_webhooks import (
	clear_webhook_rate_limits,
	get_sms_opt_out_status,
	telnyx_webhook,
)
from ism.comms.contracts import (
	SUPPORTED_SMS_PROVIDERS,
	TELNYX_MESSAGES_ENDPOINT,
	SMSDispatchRequest,
)
from ism.comms.providers import (
	clear_mock_sms_outbox,
	get_mock_sms_outbox,
	is_phone_opted_out,
	send_sms,
	set_mock_sms_next_error,
	upsert_sms_opt_out,
)
from ism.comms.telnyx import (
	process_telnyx_webhook_event,
	verify_custom_hmac_webhook_signature,
	verify_telnyx_webhook_signature,
	verify_webhook_request,
)
from ism.tests.base import ISMTestCase


def _generate_ed25519_keypair() -> tuple[Ed25519PrivateKey, str, str]:
	"""Generate an ephemeral Ed25519 keypair returning (private_key, raw_b64_pub, pem_pub)."""
	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	raw_pub_bytes = pub.public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	pem_pub_bytes = pub.public_bytes(
		encoding=serialization.Encoding.PEM,
		format=serialization.PublicFormat.SubjectPublicKeyInfo,
	)
	return priv, base64.b64encode(raw_pub_bytes).decode("ascii"), pem_pub_bytes.decode("ascii")


def _sign_ed25519(priv: Ed25519PrivateKey, raw_body: bytes, ts: int) -> dict[str, str]:
	ts_str = str(ts)
	sig = priv.sign(f"{ts_str}|".encode() + raw_body)
	return {
		"telnyx-signature-ed25519": base64.b64encode(sig).decode("ascii"),
		"telnyx-timestamp": ts_str,
	}


def _sign_hmac(secret: str, raw_body: bytes, ts: int, *, prefix: str = "") -> dict[str, str]:
	ts_str = str(ts)
	digest = hmac.new(
		secret.encode("utf-8"),
		f"{ts_str}|".encode() + raw_body,
		hashlib.sha256,
	).hexdigest()
	return {
		"X-ISM-Webhook-Signature": f"{prefix}{digest}",
		"X-ISM-Webhook-Timestamp": ts_str,
	}


def _make_fake_request(raw_body: bytes, headers: dict[str, str]):
	return SimpleNamespace(
		headers=headers,
		remote_addr="198.51.100.50",
		get_data=lambda cache=True, as_text=False: (
			raw_body.decode("utf-8", errors="replace") if as_text else raw_body
		),
	)


class TestCommsTelnyxAndWebhooks(ISMTestCase):
	"""Verify Telnyx v2 adapter, provider bus, Seam 2 compat, webhooks, and opt-out compliance."""

	def setUp(self):
		super().setUp()
		clear_mock_sms_outbox()
		clear_webhook_rate_limits()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.local, "response"):
			frappe.local.response.pop("http_status_code", None)

	def tearDown(self):
		clear_mock_sms_outbox()
		clear_webhook_rate_limits()
		if hasattr(frappe.local, "response"):
			frappe.local.response.pop("http_status_code", None)
		frappe.set_user("Administrator")
		super().tearDown()
		frappe.clear_cache(doctype="ISM Settings")

	def _configure_settings(self, **updates):
		settings = frappe.get_single("ISM Settings")
		for fieldname, value in updates.items():
			setattr(settings, fieldname, value)
		settings.save(ignore_permissions=True)
		return settings

	def _invoke_webhook(self, raw_body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
		frappe.local.response["http_status_code"] = 200
		fake_req = _make_fake_request(raw_body, headers)
		with (
			patch.object(frappe.local, "request", fake_req, create=True),
			patch.object(frappe.local, "request_ip", "198.51.100.50", create=True),
		):
			resp = telnyx_webhook()
		status_code = int(frappe.local.response.get("http_status_code") or 200)
		return status_code, resp

	def test_telnyx_outbound_success_and_otp_redaction(self):
		self._configure_settings(
			sms_gateway_provider="Telnyx",
			telnyx_api_key="KEY_TELNYX_TEST_SECRET",
			sms_sender_id="+14165550100",
			telnyx_messaging_profile_id="prof_12345",
			sms_enforce_opt_out=1,
		)

		self.assertIsNone(sms_api.get_sms_unconfigured_reason())

		mock_ok = MagicMock(status_code=200)
		mock_ok.json.return_value = {"data": {"id": "telnyx_msg_001"}}

		with patch("ism.api.sms.requests.post", return_value=mock_ok) as post_mock:
			res = sms_api.dispatch_sms(
				"(416) 555-0199",
				"Your verification code is 748291. Do not share.",
				message_type="OTP",
				redact_body=True,
			)

		self.assertTrue(res["success"])
		self.assertEqual(res["message_id"], "telnyx_msg_001")
		self.assertEqual(res["provider"], "Telnyx")
		self.assertEqual(res["status"], "Queued")
		self.assertTrue(res["audit_log"])

		post_mock.assert_called_once()
		args, kwargs = post_mock.call_args
		self.assertEqual(args[0], TELNYX_MESSAGES_ENDPOINT)
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer KEY_TELNYX_TEST_SECRET")
		self.assertFalse(kwargs["allow_redirects"])
		self.assertEqual(kwargs["timeout"], 10)
		sent_payload = json.loads(kwargs["data"])
		self.assertEqual(sent_payload["to"], "+14165550199")
		self.assertEqual(sent_payload["from"], "+14165550100")
		self.assertEqual(sent_payload["messaging_profile_id"], "prof_12345")
		self.assertEqual(sent_payload["type"], "SMS")

		audit = frappe.get_doc("SMS Audit Log", res["audit_log"])
		self.assertEqual(audit.provider, "Telnyx")
		self.assertEqual(audit.provider_message_id, "telnyx_msg_001")
		self.assertEqual(audit.status, "Queued")
		self.assertEqual(int(audit.is_redacted), 1)
		self.assertNotIn("748291", audit.message_preview)
		self.assertIn("[REDACTED]", audit.message_preview)

	def test_telnyx_config_validation_and_error_and_carrier_opt_out(self):
		# Missing E.164 sender and missing profile id -> unconfigured
		self._configure_settings(
			sms_gateway_provider="Telnyx",
			telnyx_api_key="KEY_TELNYX_TEST_SECRET",
			sms_sender_id="ISM",
			telnyx_messaging_profile_id="",
		)
		reason = sms_api.get_sms_unconfigured_reason()
		self.assertIsNotNone(reason)
		self.assertIn("Telnyx", reason)

		# Valid with profile id alone
		self._configure_settings(
			sms_gateway_provider="Telnyx",
			telnyx_api_key="KEY_TELNYX_TEST_SECRET",
			sms_sender_id="ISM",
			telnyx_messaging_profile_id="prof_only_01",
			sms_enforce_opt_out=1,
		)
		self.assertIsNone(sms_api.get_sms_unconfigured_reason())

		# Generic 500 error
		mock_500 = MagicMock(status_code=500, text="Internal Error")
		mock_500.json.return_value = {
			"errors": [{"code": "50000", "detail": "Upstream gateway error"}]
		}
		with patch("ism.api.sms.requests.post", return_value=mock_500):
			err_res = sms_api.dispatch_sms("+14165550188", "Hello")
		self.assertFalse(err_res["success"])
		self.assertEqual(err_res["error_code"], "50000")
		self.assertFalse(err_res["opted_out"])
		self.assertFalse(is_phone_opted_out("+14165550188"))

		# Carrier opt-out code 40300 -> auto-populates SMS Opt Out and blocks subsequent send
		mock_40300 = MagicMock(status_code=422, text="Opted out")
		mock_40300.json.return_value = {
			"errors": [{"code": "40300", "detail": "Destination number is opted out"}]
		}
		with patch("ism.api.sms.requests.post", return_value=mock_40300):
			opt_res = sms_api.dispatch_sms("+14165550188", "Hello again")
		self.assertFalse(opt_res["success"])
		self.assertTrue(opt_res["opted_out"])
		self.assertEqual(opt_res["error_code"], "40300")
		self.assertTrue(is_phone_opted_out("+14165550188"))

		opt_doc = frappe.get_doc("SMS Opt Out", "+14165550188")
		self.assertEqual(int(opt_doc.is_opted_out), 1)
		self.assertEqual(opt_doc.source, "Carrier Opt-Out Error")

		with patch("ism.api.sms.requests.post") as post_mock:
			blocked = sms_api.dispatch_sms("+14165550188", "Blocked follow-up")
		post_mock.assert_not_called()
		self.assertFalse(blocked["success"])
		self.assertTrue(blocked["opted_out"])
		self.assertEqual(blocked["error_code"], "OPTED_OUT")
		self.assertEqual(blocked["status"], "Blocked - Opted Out")

	def test_mock_provider_outbox_and_forced_error_simulation(self):
		self._configure_settings(
			sms_gateway_provider="Mock (Deterministic Test)",
			sms_sender_id="ISM-MOCK",
			sms_enforce_opt_out=1,
		)
		self.assertIsNone(sms_api.get_sms_unconfigured_reason())

		r1 = send_sms(SMSDispatchRequest(to_phone="416-555-0171", body="First mock message"))
		r2 = send_sms(SMSDispatchRequest(to_phone="+14165550172", body="Second mock message"))
		self.assertTrue(r1.success)
		self.assertTrue(r2.success)
		self.assertEqual(r1.message_id, "mock_sms_000001")
		self.assertEqual(r2.message_id, "mock_sms_000002")

		outbox = get_mock_sms_outbox()
		self.assertEqual(len(outbox), 2)
		self.assertEqual(outbox[0]["phone_e164"], "+14165550171")
		self.assertEqual(outbox[1]["message_id"], "mock_sms_000002")

		# Forced generic error consumes once
		set_mock_sms_next_error("50300", "Simulated carrier outage")
		r_err = send_sms(SMSDispatchRequest(to_phone="+14165550173", body="Should fail"))
		self.assertFalse(r_err.success)
		self.assertEqual(r_err.error_code, "50300")
		self.assertFalse(r_err.opted_out)

		r_ok = send_sms(SMSDispatchRequest(to_phone="+14165550173", body="Should succeed after"))
		self.assertTrue(r_ok.success)

		# Forced opt-out error code 40008 triggers SMS Opt Out upsert
		set_mock_sms_next_error("40008", "Simulated carrier block")
		r_opt = send_sms(SMSDispatchRequest(to_phone="+14165550174", body="Carrier block"))
		self.assertFalse(r_opt.success)
		self.assertTrue(r_opt.opted_out)
		self.assertTrue(is_phone_opted_out("+14165550174"))

	def test_ed25519_verification_replay_guard_and_zero_hmac_downgrade_in_telnyx_mode(self):
		priv, raw_b64_pub, pem_pub = _generate_ed25519_keypair()
		now = int(time.time())
		payload = {
			"data": {
				"id": "evt_ed25519_ok_1",
				"event_type": "message.received",
				"payload": {
					"id": "in_msg_001",
					"from": {"phone_number": "+14165550161"},
					"to": [{"phone_number": "+14165550100"}],
					"text": "Hello shop",
				},
			}
		}
		raw_body = json.dumps(payload).encode("utf-8")

		# Direct unit verification supports both raw base64 and PEM Ed25519 keys
		headers_ok = _sign_ed25519(priv, raw_body, now)
		self.assertTrue(
			verify_telnyx_webhook_signature(
				raw_body,
				headers_ok["telnyx-signature-ed25519"],
				headers_ok["telnyx-timestamp"],
				raw_b64_pub,
				now_ts=now,
			)
		)
		self.assertTrue(
			verify_telnyx_webhook_signature(
				raw_body,
				headers_ok["telnyx-signature-ed25519"],
				headers_ok["telnyx-timestamp"],
				pem_pub,
				now_ts=now,
			)
		)

		# Configure Telnyx mode with BOTH Ed25519 public key AND HMAC secret
		self._configure_settings(
			sms_gateway_provider="Telnyx",
			telnyx_api_key="KEY_TELNYX_TEST_SECRET",
			sms_sender_id="+14165550100",
			telnyx_webhook_public_key=raw_b64_pub,
			sms_webhook_hmac_secret="hmac_secret_must_not_bypass_telnyx",
			telnyx_webhook_tolerance_seconds=300,
		)

		# 1. Valid Ed25519 accepted (200)
		code, resp = self._invoke_webhook(raw_body, headers_ok)
		self.assertEqual(code, 200)
		self.assertTrue(resp["ok"])
		self.assertEqual(resp["action"], "inbound_logged")

		# 2. Tampered body rejected (401)
		tampered_body = raw_body.replace(b"Hello shop", b"STOP")
		code, resp = self._invoke_webhook(tampered_body, headers_ok)
		self.assertEqual(code, 401)
		self.assertFalse(resp["ok"])
		self.assertFalse(is_phone_opted_out("+14165550161"))

		# 3. Stale timestamp replay rejected (401)
		stale_headers = _sign_ed25519(priv, raw_body, now - 600)
		code, resp = self._invoke_webhook(raw_body, stale_headers)
		self.assertEqual(code, 401)
		self.assertFalse(resp["ok"])

		# 4. Zero silent HMAC downgrade in Telnyx mode (valid HMAC headers MUST be rejected 401)
		hmac_headers = _sign_hmac("hmac_secret_must_not_bypass_telnyx", raw_body, now)
		verified, reason = verify_webhook_request(raw_body, hmac_headers, now_ts=now)
		self.assertFalse(verified)
		self.assertEqual(reason, "invalid_ed25519_signature")
		code, resp = self._invoke_webhook(raw_body, hmac_headers)
		self.assertEqual(code, 401)
		self.assertFalse(resp["ok"])

		# 5. Missing Ed25519 public key in Telnyx mode fails closed (401)
		self._configure_settings(telnyx_webhook_public_key="")
		verified, reason = verify_webhook_request(raw_body, headers_ok, now_ts=now)
		self.assertFalse(verified)
		self.assertEqual(reason, "missing_ed25519_key")
		code, resp = self._invoke_webhook(raw_body, headers_ok)
		self.assertEqual(code, 401)

	def test_custom_hmac_and_mock_mode_webhook_verification(self):
		priv, raw_b64_pub, _pem_pub = _generate_ed25519_keypair()
		now = int(time.time())
		raw_body = json.dumps(
			{
				"data": {
					"id": "evt_hmac_1",
					"event_type": "message.received",
					"payload": {
						"id": "in_hmac_001",
						"from": {"phone_number": "+14165550162"},
						"text": "Status check",
					},
				}
			}
		).encode("utf-8")

		self._configure_settings(
			sms_gateway_provider="Custom HTTP Webhook",
			sms_gateway_endpoint="https://relay.example.test/sms",
			telnyx_webhook_public_key="",
			sms_webhook_hmac_secret="custom_hmac_secret_xyz",
			telnyx_webhook_tolerance_seconds=300,
		)

		# Valid HMAC with sha256= prefix succeeds
		hmac_headers = _sign_hmac("custom_hmac_secret_xyz", raw_body, now, prefix="sha256=")
		self.assertTrue(
			verify_custom_hmac_webhook_signature(
				raw_body,
				hmac_headers["X-ISM-Webhook-Signature"],
				hmac_headers["X-ISM-Webhook-Timestamp"],
				"custom_hmac_secret_xyz",
				now_ts=now,
			)
		)
		code, resp = self._invoke_webhook(raw_body, hmac_headers)
		self.assertEqual(code, 200)
		self.assertTrue(resp["ok"])

		# Invalid HMAC rejected (401)
		bad_headers = _sign_hmac("wrong_secret", raw_body, now)
		code, _resp = self._invoke_webhook(raw_body, bad_headers)
		self.assertEqual(code, 401)

		# Valid signature with invalid JSON -> 400
		invalid_json = b"{not-valid-json"
		bad_json_headers = _sign_hmac("custom_hmac_secret_xyz", invalid_json, now)
		code, resp = self._invoke_webhook(invalid_json, bad_json_headers)
		self.assertEqual(code, 400)
		self.assertFalse(resp["ok"])

		# Mock mode with Ed25519 key accepts Ed25519
		self._configure_settings(
			sms_gateway_provider="Mock (Deterministic Test)",
			telnyx_webhook_public_key=raw_b64_pub,
			sms_webhook_hmac_secret="",
		)
		ed_headers = _sign_ed25519(priv, raw_body, now)
		code, resp = self._invoke_webhook(raw_body, ed_headers)
		self.assertEqual(code, 200)
		self.assertEqual(resp["action"], "duplicate_ignored")

		# Mock mode with NO credentials fails closed (401)
		self._configure_settings(
			sms_gateway_provider="Mock (Deterministic Test)",
			telnyx_webhook_public_key="",
			sms_webhook_hmac_secret="",
		)
		code, _resp = self._invoke_webhook(raw_body, ed_headers)
		self.assertEqual(code, 401)

	def test_delivery_status_transitions_monotonicity_idempotency_and_otp_sanitization(self):
		self._configure_settings(
			sms_gateway_provider="Telnyx",
			telnyx_api_key="KEY_TELNYX_TEST_SECRET",
			sms_sender_id="+14165550100",
		)
		mock_ok = MagicMock(status_code=200)
		mock_ok.json.return_value = {"data": {"id": "telnyx_dlr_001"}}
		with patch("ism.api.sms.requests.post", return_value=mock_ok):
			res = sms_api.dispatch_sms(
				"+14165550163",
				"Your code is 849201",
				message_type="OTP",
				redact_body=True,
			)
		audit_name = res["audit_log"]

		# 1. message.sent -> Sent
		sent_evt = {
			"data": {
				"id": "evt_dlr_sent_1",
				"event_type": "message.sent",
				"payload": {
					"id": "telnyx_dlr_001",
					"to": [{"phone_number": "+14165550163", "status": "sent"}],
					"text": "Your code is 849201",
				},
			}
		}
		out1 = process_telnyx_webhook_event(sent_evt)
		self.assertEqual(out1["action"], "status_updated")
		self.assertEqual(out1["status"], "Sent")

		# 2. Duplicate event_id ignored
		out_dup = process_telnyx_webhook_event(sent_evt)
		self.assertEqual(out_dup["action"], "duplicate_ignored")

		# 3. message.finalized (delivered) -> Delivered + delivered_at + OTP text stripped from sanitized JSON
		deliv_evt = {
			"data": {
				"id": "evt_dlr_final_1",
				"event_type": "message.finalized",
				"payload": {
					"id": "telnyx_dlr_001",
					"to": [{"phone_number": "+14165550163", "status": "delivered"}],
					"text": "Your code is 849201",
				},
			}
		}
		out2 = process_telnyx_webhook_event(deliv_evt)
		self.assertEqual(out2["action"], "status_updated")
		self.assertEqual(out2["status"], "Delivered")

		audit = frappe.get_doc("SMS Audit Log", audit_name)
		self.assertEqual(audit.status, "Delivered")
		self.assertIsNotNone(audit.delivered_at)
		self.assertNotIn("849201", audit.webhook_payload_sanitized or "")
		self.assertNotIn("849201", audit.message_preview or "")

		# 4. Monotonic guard: late out-of-order message.sent with new event_id does NOT regress Delivered
		late_sent = {
			"data": {
				"id": "evt_dlr_late_sent",
				"event_type": "message.sent",
				"payload": {
					"id": "telnyx_dlr_001",
					"to": [{"phone_number": "+14165550163", "status": "sent"}],
				},
			}
		}
		out_late = process_telnyx_webhook_event(late_sent)
		self.assertEqual(out_late["status"], "Delivered")

		# 5. Delivery failure with carrier opt-out code 40300 updates status=Failed and SMS Opt Out
		mock_ok2 = MagicMock(status_code=200)
		mock_ok2.json.return_value = {"data": {"id": "telnyx_dlr_002"}}
		with patch("ism.api.sms.requests.post", return_value=mock_ok2):
			res2 = sms_api.dispatch_sms("+14165550164", "Reminder")
		fail_evt = {
			"data": {
				"id": "evt_dlr_fail_40300",
				"event_type": "message.finalized",
				"payload": {
					"id": "telnyx_dlr_002",
					"to": [{"phone_number": "+14165550164", "status": "delivery_failed"}],
					"errors": [{"code": "40300", "detail": "Recipient opted out at carrier"}],
				},
			}
		}
		out_fail = process_telnyx_webhook_event(fail_evt)
		self.assertEqual(out_fail["status"], "Failed")
		self.assertTrue(out_fail["opt_out_updated"])
		self.assertTrue(is_phone_opted_out("+14165550164"))

		audit2 = frappe.get_doc("SMS Audit Log", res2["audit_log"])
		self.assertEqual(audit2.status, "Failed")
		self.assertEqual(audit2.error_code, "40300")

	def test_inbound_stop_start_help_compliance_lifecycle(self):
		priv, raw_b64_pub, _pem_pub = _generate_ed25519_keypair()
		now = int(time.time())
		phone = "+14165550165"
		custom_help = "ISM Shop Help: Reply STOP to opt out or call 416-555-0100."

		self._configure_settings(
			sms_gateway_provider="Mock (Deterministic Test)",
			telnyx_webhook_public_key=raw_b64_pub,
			sms_enforce_opt_out=1,
			sms_help_reply_text=custom_help,
		)

		# 1. Inbound STOP opts out number and blocks outbound SMS
		stop_body = json.dumps(
			{
				"data": {
					"id": "evt_in_stop_1",
					"event_type": "message.received",
					"payload": {
						"id": "in_stop_1",
						"from": {"phone_number": phone},
						"to": [{"phone_number": "+14165550100"}],
						"text": "stop!",
					},
				}
			}
		).encode("utf-8")
		code, resp = self._invoke_webhook(stop_body, _sign_ed25519(priv, stop_body, now))
		self.assertEqual(code, 200)
		self.assertEqual(resp["action"], "opted_out")
		self.assertEqual(resp["keyword"], "STOP")
		self.assertTrue(is_phone_opted_out(phone))

		# Duplicate STOP event_id is idempotently ignored
		code_dup, resp_dup = self._invoke_webhook(stop_body, _sign_ed25519(priv, stop_body, now))
		self.assertEqual(code_dup, 200)
		self.assertEqual(resp_dup["action"], "duplicate_ignored")

		blocked = sms_api.dispatch_sms(phone, "Estimate ready")
		self.assertFalse(blocked["success"])
		self.assertTrue(blocked["opted_out"])
		self.assertEqual(blocked["error_code"], "OPTED_OUT")
		self.assertEqual(len(get_mock_sms_outbox()), 0)

		# 2. Inbound HELP sends compliance reply even while opted out (respect_opt_out=False)
		help_body = json.dumps(
			{
				"data": {
					"id": "evt_in_help_1",
					"event_type": "message.received",
					"payload": {
						"id": "in_help_1",
						"from": {"phone_number": phone},
						"text": "HELP",
					},
				}
			}
		).encode("utf-8")
		code, resp = self._invoke_webhook(help_body, _sign_ed25519(priv, help_body, now))
		self.assertEqual(code, 200)
		self.assertEqual(resp["action"], "help_replied")
		self.assertTrue(resp["reply_result"]["success"])
		outbox = get_mock_sms_outbox()
		self.assertEqual(len(outbox), 1)
		self.assertEqual(outbox[0]["phone_e164"], phone)
		self.assertEqual(outbox[0]["body"], custom_help)

		# 3. Inbound START re-opts-in number and resumes outbound SMS
		start_body = json.dumps(
			{
				"data": {
					"id": "evt_in_start_1",
					"event_type": "message.received",
					"payload": {
						"id": "in_start_1",
						"from": {"phone_number": phone},
						"text": "START",
					},
				}
			}
		).encode("utf-8")
		code, resp = self._invoke_webhook(start_body, _sign_ed25519(priv, start_body, now))
		self.assertEqual(code, 200)
		self.assertEqual(resp["action"], "opted_in")
		self.assertFalse(is_phone_opted_out(phone))

		resumed = sms_api.dispatch_sms(phone, "Your vehicle is ready for pickup.")
		self.assertTrue(resumed["success"])
		self.assertFalse(resumed["opted_out"])
		self.assertEqual(len(get_mock_sms_outbox()), 2)

	def test_get_sms_opt_out_status_rbac_and_seam2_compat(self):
		phone = "+14165550166"
		upsert_sms_opt_out(
			phone,
			is_opted_out=True,
			source="Telnyx STOP Keyword",
			last_keyword="STOP",
			event_id="evt_rbac_1",
		)

		# System Manager / Administrator allowed
		frappe.set_user("Administrator")
		status = get_sms_opt_out_status("(416) 555-0166")
		self.assertEqual(status["phone_e164"], phone)
		self.assertTrue(status["is_opted_out"])
		self.assertEqual(status["source"], "Telnyx STOP Keyword")

		# Unlisted phone returns is_opted_out=False
		clean_status = get_sms_opt_out_status("416-555-0167")
		self.assertEqual(clean_status["phone_e164"], "+14165550167")
		self.assertFalse(clean_status["is_opted_out"])

		# Unauthorized role (Guest or ISM Technician) raises PermissionError
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			get_sms_opt_out_status(phone)

		with patch("frappe.get_roles", return_value=["ISM Technician"]):
			with self.assertRaises(frappe.PermissionError):
				get_sms_opt_out_status(phone)

		frappe.set_user("Administrator")

		# Seam 2 backward-compatibility assertions
		self.assertEqual(sms_api.SUPPORTED_SMS_PROVIDERS, SUPPORTED_SMS_PROVIDERS)
		self.assertIn("169.254.169.254", sms_api._BLOCKED_SMS_HOSTS)
		self.assertTrue(sms_api._is_valid_sms_webhook_url("https://relay.example.test/sms"))
		self.assertFalse(sms_api._is_valid_sms_webhook_url("http://relay.example.test/sms"))

		frappe.db.set_single_value("ISM Settings", "sms_gateway_provider", "Twilio")
		try:
			legacy = sms_api.dispatch_sms("+14165550199", "Legacy Twilio check")
			self.assertFalse(legacy["success"])
			self.assertEqual(legacy["error"], "Unsupported SMS provider: Twilio")
		finally:
			frappe.db.set_single_value("ISM Settings", "sms_gateway_provider", "Disabled")

	def test_inbound_keyword_full_body_matching_and_conversational_replies(self):
		"""CH-471: match exact single-word and canonical two-word keywords without false-positiving conversational replies."""
		priv, raw_b64_pub, _pem_pub = _generate_ed25519_keypair()
		now = int(time.time())
		self._configure_settings(
			sms_gateway_provider="Mock (Deterministic Test)",
			telnyx_webhook_public_key=raw_b64_pub,
			sms_enforce_opt_out=1,
		)

		def _send_inbound(phone_num: str, evt_id: str, text: str) -> tuple[int, dict]:
			body = json.dumps(
				{
					"data": {
						"id": evt_id,
						"event_type": "message.received",
						"payload": {
							"id": f"msg_{evt_id}",
							"from": {"phone_number": phone_num},
							"to": [{"phone_number": "+14165550100"}],
							"text": text,
						},
					}
				}
			).encode("utf-8")
			return self._invoke_webhook(body, _sign_ed25519(priv, body, now))

		# Conversational replies starting with CANCEL/YES must NOT trigger opt-out or opt-in
		conv_phone = "+14165550171"
		code, resp = _send_inbound(conv_phone, "evt_conv_cancel", "CANCEL my appointment")
		self.assertEqual(code, 200)
		self.assertEqual(resp["action"], "inbound_logged")
		self.assertFalse(is_phone_opted_out(conv_phone))

		# Opt out with STOP!, then verify "YES, 3pm works" does NOT re-opt-in
		code, resp = _send_inbound(conv_phone, "evt_stop_bang", "STOP!")
		self.assertEqual(code, 200)
		self.assertEqual(resp["action"], "opted_out")
		self.assertEqual(resp["keyword"], "STOP")
		self.assertTrue(is_phone_opted_out(conv_phone))

		code, resp = _send_inbound(conv_phone, "evt_conv_yes", "YES, 3pm works")
		self.assertEqual(code, 200)
		self.assertEqual(resp["action"], "inbound_logged")
		self.assertTrue(is_phone_opted_out(conv_phone))

		# Canonical two-word and hyphenated opt-out phrases MUST opt out
		for idx, (phrase, expected_kw) in enumerate(
			[("OPT OUT", "OPTOUT"), ("OPT-OUT", "OPTOUT"), ("STOP ALL", "STOPALL")]
		):
			p = f"+1416555017{idx + 2}"
			c, r = _send_inbound(p, f"evt_kw_{idx}", phrase)
			self.assertEqual(c, 200)
			self.assertEqual(r["action"], "opted_out")
			self.assertEqual(r["keyword"], expected_kw)
			self.assertTrue(is_phone_opted_out(p))
