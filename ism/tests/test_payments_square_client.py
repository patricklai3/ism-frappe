# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 2A tests: Square Payments API adapter, MockSquareClient, and HMAC-SHA256 webhooks."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import frappe
import requests

from ism.payments.contracts import (
	SQUARE_API_BASE_URLS,
	SQUARE_API_VERSION,
	SQUARE_CURRENCY,
	SQUARE_DEFAULT_MOP,
	SQUARE_ENV_DISABLED,
	SQUARE_ENV_MOCK,
	SQUARE_ENV_PRODUCTION,
	SQUARE_ENV_SANDBOX,
	SQUARE_SDK_SCRIPT_URLS,
	cad_to_cents,
)
from ism.payments.square_client import (
	SQUARE_HTTP_TIMEOUT,
	MockSquareClient,
	SquareApiError,
	SquareClient,
	get_sdk_boot_config,
	get_square_client,
	get_square_settings_snapshot,
)
from ism.payments.webhooks import (
	compute_square_webhook_signature,
	parse_square_webhook_event,
	verify_square_webhook_signature,
)
from ism.tests.base import ISMTestCase


class TestPaymentsSquareClient(ISMTestCase):
	"""Unit & integration tests for square_client.py and webhooks.py."""

	def setUp(self):
		super().setUp()
		MockSquareClient.reset_store()
		self._orig_square_settings = self._snapshot_square_settings()

	def tearDown(self):
		MockSquareClient.reset_store()
		self._restore_square_settings(self._orig_square_settings)
		super().tearDown()

	def _snapshot_square_settings(self) -> dict:
		settings = frappe.get_single("ISM Settings")
		return {
			"square_environment": settings.square_environment,
			"square_application_id": settings.square_application_id,
			"square_location_id": settings.square_location_id,
			"square_currency": settings.square_currency,
			"square_access_token": settings.get_password("square_access_token", raise_exception=False),
			"square_webhook_signature_key": settings.get_password(
				"square_webhook_signature_key", raise_exception=False
			),
			"square_webhook_notification_url": settings.square_webhook_notification_url,
			"square_mode_of_payment": settings.square_mode_of_payment,
			"square_bank_account": settings.square_bank_account,
		}

	def _restore_square_settings(self, snapshot: dict) -> None:
		settings = frappe.get_single("ISM Settings")
		for fieldname, value in snapshot.items():
			settings.set(fieldname, value)
		settings.save(ignore_permissions=True)

	def _configure_square_settings(
		self,
		*,
		environment: str,
		application_id: str | None = "sandbox-sq0idb-testapp",
		location_id: str | None = "LTEST00000001",
		access_token: str | None = "EAAA_SECRET_ACCESS_TOKEN_12345",
		webhook_signature_key: str | None = "whsec_SUPER_SECRET_KEY_67890",
		webhook_notification_url: str | None = "https://shop.example.ca/api/method/ism.api.payments.square_webhook",
	) -> None:
		settings = frappe.get_single("ISM Settings")
		settings.square_environment = environment
		settings.square_application_id = application_id
		settings.square_location_id = location_id
		settings.square_currency = SQUARE_CURRENCY
		settings.square_access_token = access_token
		settings.square_webhook_signature_key = webhook_signature_key
		settings.square_webhook_notification_url = webhook_notification_url
		settings.square_mode_of_payment = settings.square_mode_of_payment or SQUARE_DEFAULT_MOP
		settings.save(ignore_permissions=True)

	# ------------------------------------------------------------------
	# 1. Settings Snapshot & SDK Boot Config (Zero Secret Leakage)
	# ------------------------------------------------------------------

	def test_sdk_boot_config_omits_secrets_across_all_environments(self):
		secret_token = "EAAA_TOP_SECRET_TOKEN_XYZ"
		secret_wh_key = "whsec_TOP_SECRET_KEY_ABC"
		cases = [
			(SQUARE_ENV_DISABLED, False, "disabled", None),
			(SQUARE_ENV_MOCK, True, "mock", None),
			(SQUARE_ENV_SANDBOX, True, "sandbox", SQUARE_SDK_SCRIPT_URLS[SQUARE_ENV_SANDBOX]),
			(SQUARE_ENV_PRODUCTION, True, "production", SQUARE_SDK_SCRIPT_URLS[SQUARE_ENV_PRODUCTION]),
		]

		for env, expected_enabled, expected_mode, expected_sdk_url in cases:
			self._configure_square_settings(
				environment=env,
				access_token=secret_token,
				webhook_signature_key=secret_wh_key,
			)
			boot = get_sdk_boot_config()
			self.assertEqual(boot["enabled"], expected_enabled)
			self.assertEqual(boot["environment"], env)
			self.assertEqual(boot["mode"], expected_mode)
			self.assertEqual(boot["currency"], SQUARE_CURRENCY)
			self.assertEqual(boot["sdk_url"], expected_sdk_url)

			# Strictly forbid secret keys or values anywhere in boot config
			self.assertNotIn("square_access_token", boot)
			self.assertNotIn("access_token", boot)
			self.assertNotIn("square_webhook_signature_key", boot)
			self.assertNotIn("webhook_signature_key", boot)
			serialized = json.dumps(boot)
			self.assertNotIn(secret_token, serialized)
			self.assertNotIn(secret_wh_key, serialized)

			# Server-only snapshot DOES retrieve secrets via get_password
			snapshot = get_square_settings_snapshot()
			self.assertEqual(snapshot["access_token"], secret_token)
			self.assertEqual(snapshot["webhook_signature_key"], secret_wh_key)

	def test_get_square_client_factory(self):
		self._configure_square_settings(environment=SQUARE_ENV_DISABLED)
		with self.assertRaises(frappe.ValidationError):
			get_square_client()

		self._configure_square_settings(environment=SQUARE_ENV_MOCK)
		mock_client = get_square_client()
		self.assertIsInstance(mock_client, MockSquareClient)

		self._configure_square_settings(environment=SQUARE_ENV_SANDBOX)
		sandbox_client = get_square_client()
		self.assertIsInstance(sandbox_client, SquareClient)
		self.assertNotIsInstance(sandbox_client, MockSquareClient)
		self.assertEqual(sandbox_client._base_url, SQUARE_API_BASE_URLS[SQUARE_ENV_SANDBOX])

		self._configure_square_settings(environment=SQUARE_ENV_PRODUCTION)
		prod_client = get_square_client()
		self.assertIsInstance(prod_client, SquareClient)
		self.assertEqual(prod_client._base_url, SQUARE_API_BASE_URLS[SQUARE_ENV_PRODUCTION])

	# ------------------------------------------------------------------
	# 2. CAD Decimal Edge Cases
	# ------------------------------------------------------------------

	def test_cad_to_cents_decimal_edge_cases(self):
		self.assertEqual(cad_to_cents(19.99), 1999)
		self.assertEqual(cad_to_cents("19.99"), 1999)
		self.assertEqual(cad_to_cents(Decimal("19.99")), 1999)
		self.assertEqual(cad_to_cents(0.10 + 0.20), 30)
		self.assertEqual(cad_to_cents(Decimal("0.10") + Decimal("0.20")), 30)
		self.assertEqual(cad_to_cents("100.005"), 10001)  # ROUND_HALF_UP
		self.assertEqual(cad_to_cents(1), 100)

		for invalid in (0, -5, "0.00", None, True, False, "nan", "inf", "abc"):
			with self.assertRaises(frappe.ValidationError):
				cad_to_cents(invalid)

	# ------------------------------------------------------------------
	# 3. MockSquareClient Fixtures & Idempotency Replay
	# ------------------------------------------------------------------

	def test_mock_square_client_all_fixtures_and_idempotency(self):
		client = MockSquareClient(location_id="LMOCK_SHOP_1")

		# 1a. mock_tok_ok
		p_ok = client.create_payment(
			source_id="mock_tok_ok",
			amount_cents=12500,
			idempotency_key="idem-mock-ok-001",
			reference_id="PAY-2026-00001",
			note="Deposit for RO-00001",
			buyer_email_address="customer@example.ca",
		)
		self.assertEqual(p_ok["status"], "COMPLETED")
		self.assertTrue(p_ok["id"].startswith("sq_mock_"))
		self.assertEqual(len(p_ok["id"]), len("sq_mock_") + 16)
		self.assertEqual(p_ok["amount_money"], {"amount": 12500, "currency": "CAD"})
		self.assertEqual(p_ok["card_details"]["card"]["card_brand"], "VISA")
		self.assertEqual(p_ok["card_details"]["card"]["last_4"], "4242")
		self.assertEqual(p_ok["reference_id"], "PAY-2026-00001")

		# Idempotency replay returns exact same payload (even if source_id varies)
		p_replay = client.create_payment(
			source_id="mock_tok_decline",
			amount_cents=12500,
			idempotency_key="idem-mock-ok-001",
			reference_id="PAY-2026-00001",
			note="Deposit for RO-00001",
		)
		self.assertEqual(p_replay, p_ok)

		# get_payment retrieves stored mock payment
		fetched = client.get_payment(p_ok["id"])
		self.assertEqual(fetched, p_ok)

		# 1b. mock_tok_ok_* prefix
		p_ok_suffix = client.create_payment(
			source_id="mock_tok_ok_visa_4242",
			amount_cents=5000,
			idempotency_key="idem-mock-ok-002",
			reference_id="PAY-2026-00002",
			note="Suffix test",
		)
		self.assertEqual(p_ok_suffix["status"], "COMPLETED")
		self.assertEqual(p_ok_suffix["amount_money"]["amount"], 5000)

		# 2. mock_tok_pending
		p_pending = client.create_payment(
			source_id="mock_tok_pending",
			amount_cents=7500,
			idempotency_key="idem-mock-pending-001",
			reference_id="PAY-2026-00003",
			note="Pending test",
		)
		self.assertEqual(p_pending["status"], "PENDING")
		self.assertEqual(p_pending["amount_money"], {"amount": 7500, "currency": "CAD"})

		# 3. mock_tok_decline
		with self.assertRaises(SquareApiError) as decline_ctx:
			client.create_payment(
				source_id="mock_tok_decline",
				amount_cents=7500,
				idempotency_key="idem-mock-decline-001",
				reference_id="PAY-2026-00004",
				note="Decline test",
			)
		self.assertEqual(decline_ctx.exception.code, "CARD_DECLINED")
		self.assertEqual(decline_ctx.exception.status_code, 402)
		self.assertFalse(decline_ctx.exception.retryable)

		# 4. mock_tok_timeout
		with self.assertRaises(requests.Timeout):
			client.create_payment(
				source_id="mock_tok_timeout",
				amount_cents=7500,
				idempotency_key="idem-mock-timeout-001",
				reference_id="PAY-2026-00005",
				note="Timeout test",
			)

		# 5. mock_tok_mismatch
		p_mismatch = client.create_payment(
			source_id="mock_tok_mismatch",
			amount_cents=7500,
			idempotency_key="idem-mock-mismatch-001",
			reference_id="PAY-2026-00006",
			note="Mismatch test",
		)
		self.assertEqual(p_mismatch["status"], "COMPLETED")
		self.assertEqual(p_mismatch["amount_money"], {"amount": 7400, "currency": "CAD"})

		# Unknown payment ID on get_payment raises 404 SquareApiError
		with self.assertRaises(SquareApiError) as not_found_ctx:
			client.get_payment("sq_mock_does_not_exist")
		self.assertEqual(not_found_ctx.exception.status_code, 404)
		self.assertEqual(not_found_ctx.exception.code, "NOT_FOUND")

	# ------------------------------------------------------------------
	# 4. Live SquareClient Transport, Headers, Validation & Redaction
	# ------------------------------------------------------------------

	@patch("ism.payments.square_client.requests.post")
	def test_live_square_client_create_payment_payload_and_headers(self, mock_post: MagicMock):
		mock_resp = MagicMock()
		mock_resp.status_code = 200
		mock_resp.json.return_value = {
			"payment": {
				"id": "sq_live_001",
				"status": "COMPLETED",
				"amount_money": {"amount": 1999, "currency": "CAD"},
			}
		}
		mock_post.return_value = mock_resp

		client = SquareClient(
			access_token="EAAA_LIVE_SECRET_999",
			location_id="LLIVE001",
			environment=SQUARE_ENV_SANDBOX,
		)
		long_note = "N" * 600
		result = client.create_payment(
			source_id="cnon:card-nonce-ok",
			amount_cents=1999,
			idempotency_key="idem-live-001",
			reference_id="ISM-PAY-00001",
			note=long_note,
			buyer_email_address="driver@example.ca",
		)

		self.assertEqual(result["id"], "sq_live_001")
		mock_post.assert_called_once()
		args, kwargs = mock_post.call_args
		self.assertEqual(args[0], f"{SQUARE_API_BASE_URLS[SQUARE_ENV_SANDBOX]}/v2/payments")
		self.assertEqual(kwargs["timeout"], SQUARE_HTTP_TIMEOUT)
		self.assertEqual(
			kwargs["headers"],
			{
				"Authorization": "Bearer EAAA_LIVE_SECRET_999",
				"Square-Version": SQUARE_API_VERSION,
				"Content-Type": "application/json",
			},
		)
		sent_body = kwargs["json"]
		self.assertEqual(sent_body["source_id"], "cnon:card-nonce-ok")
		self.assertEqual(sent_body["idempotency_key"], "idem-live-001")
		self.assertEqual(sent_body["amount_money"], {"amount": 1999, "currency": "CAD"})
		self.assertEqual(sent_body["location_id"], "LLIVE001")
		self.assertTrue(sent_body["autocomplete"])
		self.assertEqual(sent_body["reference_id"], "ISM-PAY-00001")
		self.assertEqual(len(sent_body["note"]), 500)
		self.assertEqual(sent_body["buyer_email_address"], "driver@example.ca")

	@patch("ism.payments.square_client.requests.get")
	def test_live_square_client_get_payment(self, mock_get: MagicMock):
		mock_resp = MagicMock()
		mock_resp.status_code = 200
		mock_resp.json.return_value = {
			"payment": {
				"id": "sq_live_fetch_1",
				"status": "COMPLETED",
				"amount_money": {"amount": 4200, "currency": "CAD"},
			}
		}
		mock_get.return_value = mock_resp

		client = SquareClient(
			access_token="EAAA_PROD_SECRET_111",
			location_id="LPROD001",
			environment=SQUARE_ENV_PRODUCTION,
		)
		result = client.get_payment("sq_live_fetch_1")
		self.assertEqual(result["id"], "sq_live_fetch_1")
		args, kwargs = mock_get.call_args
		self.assertEqual(args[0], f"{SQUARE_API_BASE_URLS[SQUARE_ENV_PRODUCTION]}/v2/payments/sq_live_fetch_1")
		self.assertEqual(kwargs["timeout"], SQUARE_HTTP_TIMEOUT)
		self.assertEqual(kwargs["headers"]["Square-Version"], SQUARE_API_VERSION)
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer EAAA_PROD_SECRET_111")

	@patch("ism.payments.square_client.requests.post")
	def test_live_square_client_error_redaction_and_retryable_flag(self, mock_post: MagicMock):
		secret_token = "EAAA_SUPER_SECRET_TOKEN_777"
		secret_nonce = "cnon:super-secret-nonce-888"

		# 503 error echoing token & source_id in detail -> retryable=True and both redacted
		mock_resp = MagicMock()
		mock_resp.status_code = 503
		mock_resp.json.return_value = {
			"errors": [
				{
					"category": "API_ERROR",
					"code": "SERVICE_UNAVAILABLE",
					"detail": f"Upstream failure for {secret_nonce} with token {secret_token}",
				}
			]
		}
		mock_post.return_value = mock_resp

		client = SquareClient(
			access_token=secret_token,
			location_id="LLIVE001",
			environment=SQUARE_ENV_SANDBOX,
		)
		with self.assertRaises(SquareApiError) as ctx:
			client.create_payment(
				source_id=secret_nonce,
				amount_cents=1000,
				idempotency_key="idem-err-503",
				reference_id="PAY-ERR-1",
				note="Redaction test",
			)

		err = ctx.exception
		self.assertEqual(err.code, "SERVICE_UNAVAILABLE")
		self.assertEqual(err.category, "API_ERROR")
		self.assertEqual(err.status_code, 503)
		self.assertTrue(err.retryable)
		self.assertNotIn(secret_token, str(err))
		self.assertNotIn(secret_nonce, str(err))
		self.assertNotIn(secret_token, err.detail)
		self.assertNotIn(secret_nonce, err.detail)
		self.assertIn("[REDACTED]", err.detail)

	def test_square_client_input_validation_bounds(self):
		client = MockSquareClient()
		# amount_cents <= 0
		with self.assertRaises(frappe.ValidationError):
			client.create_payment(
				source_id="mock_tok_ok",
				amount_cents=0,
				idempotency_key="idem-1",
				reference_id="ref-1",
				note="n",
			)
		# idempotency_key > 45 chars
		with self.assertRaises(frappe.ValidationError):
			client.create_payment(
				source_id="mock_tok_ok",
				amount_cents=100,
				idempotency_key="k" * 46,
				reference_id="ref-1",
				note="n",
			)
		# reference_id > 40 chars
		with self.assertRaises(frappe.ValidationError):
			client.create_payment(
				source_id="mock_tok_ok",
				amount_cents=100,
				idempotency_key="idem-2",
				reference_id="r" * 41,
				note="n",
			)

	# ------------------------------------------------------------------
	# 5. Webhook HMAC-SHA256 Verification
	# ------------------------------------------------------------------

	def test_webhook_hmac_signature_verification(self):
		url = "https://shop.example.ca/api/method/ism.api.payments.square_webhook"
		key = "whsec_test_signing_key_123"
		raw_body = b'{"event_id":"evt_1","type":"payment.updated","data":{"object":{"payment":{"id":"sq_1"}}}}'

		valid_sig = compute_square_webhook_signature(url, raw_body, key)
		self.assertTrue(verify_square_webhook_signature(raw_body, valid_sig, url, key))

		# Tampered body
		tampered_body = raw_body.replace(b"sq_1", b"sq_2")
		self.assertFalse(verify_square_webhook_signature(tampered_body, valid_sig, url, key))

		# Wrong notification URL
		self.assertFalse(
			verify_square_webhook_signature(raw_body, valid_sig, "https://other.example.ca/hook", key)
		)

		# Wrong key
		self.assertFalse(verify_square_webhook_signature(raw_body, valid_sig, url, "wrong_key"))

		# Missing / empty header
		self.assertFalse(verify_square_webhook_signature(raw_body, None, url, key))
		self.assertFalse(verify_square_webhook_signature(raw_body, "", url, key))
		self.assertFalse(verify_square_webhook_signature(raw_body, "   ", url, key))

		# Settings fallback when notification_url and signature_key omitted
		self._configure_square_settings(
			environment=SQUARE_ENV_MOCK,
			webhook_notification_url=url,
			webhook_signature_key=key,
		)
		self.assertTrue(verify_square_webhook_signature(raw_body, valid_sig))
		self.assertFalse(verify_square_webhook_signature(tampered_body, valid_sig))

		# Missing settings key/URL -> False
		self._configure_square_settings(
			environment=SQUARE_ENV_MOCK,
			webhook_notification_url=None,
			webhook_signature_key=None,
		)
		self.assertFalse(verify_square_webhook_signature(raw_body, valid_sig))

	# ------------------------------------------------------------------
	# 6. Webhook Payload Normalization & PII Redaction
	# ------------------------------------------------------------------

	def test_parse_square_webhook_event_payment_refund_dispute_unknown_and_malformed(self):
		# Payment event (includes PII fields in raw payload that must NOT appear in sanitized_summary)
		payment_payload = {
			"event_id": "evt_pay_001",
			"type": "payment.updated",
			"merchant_id": "ML_MERCHANT_1",
			"created_at": "2026-11-21T08:00:00Z",
			"data": {
				"id": "sq_pay_123",
				"object": {
					"payment": {
						"id": "sq_pay_123",
						"reference_id": "ISM-PAY-00010",
						"status": "COMPLETED",
						"amount_money": {"amount": 25000, "currency": "CAD"},
						"receipt_url": "https://squareup.com/receipt/preview/sq_pay_123",
						"receipt_number": "RCPT123",
						"buyer_email_address": "private.customer@example.ca",
						"billing_address": {"address_line_1": "123 Secret St"},
						"card_details": {
							"card": {
								"card_brand": "MASTERCARD",
								"last_4": "5555",
							}
						},
					}
				},
			},
		}
		ev_pay = parse_square_webhook_event(json.dumps(payment_payload).encode("utf-8"))
		self.assertFalse(ev_pay["ignored"])
		self.assertEqual(ev_pay["event_id"], "evt_pay_001")
		self.assertEqual(ev_pay["event_type"], "payment.updated")
		self.assertEqual(ev_pay["square_payment_id"], "sq_pay_123")
		self.assertEqual(ev_pay["reference_id"], "ISM-PAY-00010")
		self.assertEqual(ev_pay["payment_status"], "COMPLETED")
		self.assertEqual(ev_pay["amount_cents"], 25000)
		self.assertEqual(ev_pay["currency"], "CAD")
		self.assertEqual(ev_pay["card_brand"], "MASTERCARD")
		self.assertEqual(ev_pay["card_last4"], "5555")
		self.assertEqual(ev_pay["receipt_number"], "RCPT123")
		self.assertNotIn("private.customer@example.ca", ev_pay["sanitized_summary"])
		self.assertNotIn("123 Secret St", ev_pay["sanitized_summary"])

		# Refund event
		refund_payload = {
			"event_id": "evt_ref_001",
			"type": "refund.updated",
			"merchant_id": "ML_MERCHANT_1",
			"created_at": "2026-11-21T09:00:00Z",
			"data": {
				"id": "sq_ref_999",
				"object": {
					"refund": {
						"id": "sq_ref_999",
						"payment_id": "sq_pay_123",
						"status": "COMPLETED",
						"amount_money": {"amount": 5000, "currency": "CAD"},
					}
				},
			},
		}
		ev_ref = parse_square_webhook_event(json.dumps(refund_payload).encode("utf-8"))
		self.assertFalse(ev_ref["ignored"])
		self.assertEqual(ev_ref["square_payment_id"], "sq_pay_123")
		self.assertEqual(ev_ref["refund_id"], "sq_ref_999")
		self.assertEqual(ev_ref["refund_status"], "COMPLETED")
		self.assertEqual(ev_ref["refund_amount_cents"], 5000)
		self.assertEqual(ev_ref["currency"], "CAD")

		# Dispute event
		dispute_payload = {
			"event_id": "evt_disp_001",
			"type": "dispute.created",
			"merchant_id": "ML_MERCHANT_1",
			"created_at": "2026-11-21T10:00:00Z",
			"data": {
				"id": "sq_disp_777",
				"object": {
					"dispute": {
						"dispute_id": "sq_disp_777",
						"state": "INQUIRY_EVIDENCE_REQUIRED",
						"amount_money": {"amount": 25000, "currency": "CAD"},
						"disputed_payment": {"payment_id": "sq_pay_123"},
					}
				},
			},
		}
		ev_disp = parse_square_webhook_event(json.dumps(dispute_payload).encode("utf-8"))
		self.assertFalse(ev_disp["ignored"])
		self.assertEqual(ev_disp["dispute_id"], "sq_disp_777")
		self.assertEqual(ev_disp["dispute_state"], "INQUIRY_EVIDENCE_REQUIRED")
		self.assertEqual(ev_disp["square_payment_id"], "sq_pay_123")
		self.assertEqual(ev_disp["amount_cents"], 25000)

		# Unknown event type -> ignored=True without crashing
		unknown_payload = {
			"event_id": "evt_unk_001",
			"type": "customer.created",
			"data": {"object": {"customer": {"email_address": "secret@example.ca"}}},
		}
		ev_unk = parse_square_webhook_event(json.dumps(unknown_payload).encode("utf-8"))
		self.assertTrue(ev_unk["ignored"])
		self.assertEqual(ev_unk["event_type"], "customer.created")
		self.assertNotIn("secret@example.ca", ev_unk["sanitized_summary"])

		# Malformed JSON -> raises ValueError
		with self.assertRaises(ValueError):
			parse_square_webhook_event(b"{not-valid-json")

		# Non-dict JSON -> raises ValueError
		with self.assertRaises(ValueError):
			parse_square_webhook_event(b"[1, 2, 3]")
