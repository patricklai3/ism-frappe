# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 3A End-to-End tests for ism.api.payments and ism.payments facade exports."""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, flt, now_datetime

import ism.payments as payments_pkg
from ism.api import payments as payments_api
from ism.payments.contracts import (
	DEPOSIT_REQ_CANCELLED,
	DEPOSIT_REQ_PAID,
	DEPOSIT_REQ_REQUESTED,
	INTENT_INVOICE_BALANCE,
	INTENT_MANUAL_DEPOSIT,
	SQUARE_CURRENCY,
	SQUARE_ENV_DISABLED,
	SQUARE_ENV_MOCK,
	SQUARE_ENV_SANDBOX,
	TX_STATUS_PENDING,
	TX_STATUS_POSTED,
	TX_STATUS_REFUNDED,
)
from ism.payments.erpnext_bridge import resolve_square_accounting_config
from ism.payments.square_client import MockSquareClient
from ism.payments.webhooks import compute_square_webhook_signature
from ism.setup_tasks.seed import ensure_square_bank_account_and_mop
from ism.tests.base import ISMTestCase


class _FakeWebhookRequest:
	"""Minimal werkzeug-like request stub exposing raw bytes and headers."""

	def __init__(
		self,
		raw_body: bytes,
		headers: dict[str, str] | None = None,
		remote_addr: str = "198.51.100.42",
	) -> None:
		self._raw_body = raw_body
		self.headers = headers or {}
		self.remote_addr = remote_addr

	def get_data(self, as_text: bool = False):
		if as_text:
			return self._raw_body.decode("utf-8", errors="replace")
		return self._raw_body


class TestPaymentsApiE2E(ISMTestCase):
	"""End-to-end verification of whitelisted payment APIs, security guards, webhooks, and facade."""

	WEBHOOK_URL = "https://portal.example.test/api/method/ism.api.payments.square_webhook"
	WEBHOOK_KEY = "sq_whsec_e2e_test_secret_key_999"

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		ensure_square_bank_account_and_mop()
		MockSquareClient.reset_store()
		payments_api.clear_payment_rate_limits()
		self._settings_snapshot = self._snapshot_square_settings()
		self._enable_mock_square_settings()
		self.customer = self.create_test_customer()
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)
		frappe.db.commit()
		self._had_request = hasattr(frappe.local, "request")
		self._prev_request = getattr(frappe.local, "request", None)
		self._had_request_ip = hasattr(frappe.local, "request_ip")
		self._prev_request_ip = getattr(frappe.local, "request_ip", None)
		self._had_form_dict = hasattr(frappe.local, "form_dict")
		self._prev_form_dict = getattr(frappe.local, "form_dict", None)

	def _restore_request_attr(self) -> None:
		if self._had_request:
			frappe.local.request = self._prev_request
		elif hasattr(frappe.local, "request"):
			delattr(frappe.local, "request")

	def tearDown(self):
		frappe.set_user("Administrator")
		self._restore_request_attr()
		frappe.local.request_ip = self._prev_request_ip
		if self._had_form_dict and self._prev_form_dict is not None:
			frappe.local.form_dict = self._prev_form_dict
		payments_api.clear_payment_rate_limits()
		MockSquareClient.reset_store()
		self._restore_square_settings(self._settings_snapshot)
		super().tearDown()

	def _snapshot_square_settings(self) -> dict:
		settings = frappe.get_single("ISM Settings")
		return {
			"square_environment": settings.square_environment,
			"square_currency": settings.square_currency,
			"square_application_id": settings.square_application_id,
			"square_location_id": settings.square_location_id,
			"square_webhook_notification_url": settings.square_webhook_notification_url,
			"square_mode_of_payment": settings.square_mode_of_payment,
			"square_bank_account": settings.square_bank_account,
			"square_access_token": settings.get_password("square_access_token", raise_exception=False),
			"square_webhook_signature_key": settings.get_password(
				"square_webhook_signature_key", raise_exception=False
			),
		}

	def _restore_square_settings(self, snapshot: dict) -> None:
		settings = frappe.get_single("ISM Settings")
		for key, value in snapshot.items():
			settings.set(key, value)
		settings.save(ignore_permissions=True)
		frappe.db.commit()

	def _enable_mock_square_settings(self) -> None:
		settings = frappe.get_single("ISM Settings")
		settings.square_environment = SQUARE_ENV_MOCK
		settings.square_currency = SQUARE_CURRENCY
		settings.square_application_id = "sandbox-sq0idb-mock-app"
		settings.square_location_id = "LMOCK00000001"
		settings.square_webhook_notification_url = self.WEBHOOK_URL
		settings.square_webhook_signature_key = self.WEBHOOK_KEY
		if not settings.square_mode_of_payment:
			settings.square_mode_of_payment = "Square"
		settings.save(ignore_permissions=True)
		frappe.db.commit()

	def _create_user_with_role(self, prefix: str, role: str) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": prefix,
				"enabled": 1,
				"user_type": "System User",
				"roles": [{"role": role}],
			}
		).insert(ignore_permissions=True)
		self.track(user)
		return user.name

	def _create_ro(
		self,
		*,
		status: str = "Authorized - In Progress",
		labor_hours: float = 2.0,
		tax_rate: float = 13.0,
		with_token: bool = True,
		**extra,
	):
		ro_dict = {
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 42000,
			"approval_status": "Approved",
			"status": status,
			"tax_rate": tax_rate,
			"services": [
				{
					"service_title": "Brake & Rotor Service",
					"status": "Approved",
					"labor_hours": labor_hours,
					"labor_rate": 100.0,
				}
			],
		}
		ro_dict.update(extra)
		ro = frappe.get_doc(ro_dict).insert(ignore_permissions=True)
		if with_token:
			ro.generate_approval_token()
			ro.reload()
		self.track(ro)
		frappe.db.commit()
		return ro

	def _invoke_webhook(self, raw_body: bytes, signature: str | None = None) -> tuple[int, dict]:
		headers = {}
		if signature is not None:
			headers["x-square-hmacsha256-signature"] = signature
		frappe.local.request = _FakeWebhookRequest(raw_body=raw_body, headers=headers)
		frappe.local.response = frappe._dict()
		frappe.set_user("Guest")
		try:
			body = payments_api.square_webhook()
			status_code = frappe.local.response.get("http_status_code")
		finally:
			frappe.set_user("Administrator")
			self._restore_request_attr()
		return status_code, body

	def test_01_staff_endpoints_rbac_and_round_trip(self):
		"""Staff endpoints enforce STAFF_PAYMENT_ROLES and round-trip deposit + ledger cleanly."""
		ro = self._create_ro()
		advisor = self._create_user_with_role("pay_adv", "ISM Service Advisor")
		tech = self._create_user_with_role("pay_tech", "ISM Technician")

		# Guest and Technician denied on staff endpoints
		for denied_user in ("Guest", tech):
			frappe.set_user(denied_user)
			with self.assertRaises(frappe.PermissionError):
				payments_api.request_ro_deposit(ro.name, 100.0, note="Unauthorized")
			with self.assertRaises(frappe.PermissionError):
				payments_api.get_ro_payment_ledger(ro.name)
			with self.assertRaises(frappe.PermissionError):
				payments_api.cancel_ro_deposit_request(ro.name, reason="Unauthorized")

		# Service Advisor can request deposit, read ledger, and cancel deposit
		frappe.set_user(advisor)
		req_res = payments_api.request_ro_deposit(ro.name, "125.50", note="Parts pre-order")
		self.assertEqual(req_res["deposit_request"]["status"], DEPOSIT_REQ_REQUESTED)
		self.assertEqual(flt(req_res["deposit_request"]["amount"], 2), 125.5)

		ledger = payments_api.get_ro_payment_ledger(ro.name)
		self.assertIn("summary", ledger)
		self.assertIn("transactions", ledger)
		self.assertEqual(ledger["summary"]["repair_order"], ro.name)
		self.assertEqual(ledger["summary"]["deposit_request"]["status"], DEPOSIT_REQ_REQUESTED)
		self.assertIsInstance(ledger["transactions"], list)

		cancel_res = payments_api.cancel_ro_deposit_request(ro.name, reason="Customer paying at pickup")
		self.assertEqual(cancel_res["deposit_request"]["status"], DEPOSIT_REQ_CANCELLED)
		self.assertEqual(flt(cancel_res["deposit_request"]["amount"], 2), 0.0)

	def test_02_boot_config_omits_secrets_across_environments(self):
		"""get_square_sdk_boot_config() returns non-secret CAD config across Mock, Sandbox, and Disabled."""
		secret_token = "EAAA_SUPER_SECRET_ACCESS_TOKEN_12345"
		secret_wh_key = "WHSEC_SUPER_SECRET_KEY_67890"

		settings = frappe.get_single("ISM Settings")
		settings.square_access_token = secret_token
		settings.square_webhook_signature_key = secret_wh_key
		settings.square_application_id = "sandbox-sq0idb-testapp"
		settings.square_location_id = "LSANDBOX0001"
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		env_expectations = [
			(SQUARE_ENV_MOCK, True, "mock"),
			(SQUARE_ENV_SANDBOX, True, "sandbox"),
			(SQUARE_ENV_DISABLED, False, "disabled"),
		]
		for env_name, expected_enabled, expected_mode in env_expectations:
			settings = frappe.get_single("ISM Settings")
			settings.square_environment = env_name
			settings.save(ignore_permissions=True)
			frappe.db.commit()

			frappe.set_user("Guest")
			try:
				cfg = payments_api.get_square_sdk_boot_config()
			finally:
				frappe.set_user("Administrator")

			serialized = json.dumps(cfg)
			self.assertEqual(cfg["enabled"], expected_enabled)
			self.assertEqual(cfg["mode"], expected_mode)
			self.assertEqual(cfg["currency"], SQUARE_CURRENCY)
			self.assertNotIn("square_access_token", cfg)
			self.assertNotIn("square_webhook_signature_key", cfg)
			self.assertNotIn(secret_token, serialized)
			self.assertNotIn(secret_wh_key, serialized)

	def test_03_get_ro_payment_summary_auth_idor_zero_pii_and_rate_limit(self):
		"""get_ro_payment_summary validates guest approval_token (constant-time, no IDOR/PII) and rate limits."""
		ro1 = self._create_ro()
		ro2 = self._create_ro()
		advisor = self._create_user_with_role("sum_adv", "ISM Service Advisor")

		# Staff without token succeeds
		frappe.set_user(advisor)
		staff_summary = payments_api.get_ro_payment_summary(ro1.name)
		self.assertEqual(staff_summary["repair_order"], ro1.name)
		self.assertEqual(staff_summary["currency"], SQUARE_CURRENCY)

		# Guest with valid token for RO1 succeeds
		frappe.set_user("Guest")
		guest_summary = payments_api.get_ro_payment_summary(ro1.name, approval_token=ro1.approval_token)
		self.assertEqual(guest_summary["repair_order"], ro1.name)

		# Missing token, wrong token, and cross-RO IDOR token -> PermissionError with zero PII
		for bad_token in (None, "", "wrong-token-value", ro2.approval_token):
			with self.assertRaises(frappe.PermissionError) as ctx:
				payments_api.get_ro_payment_summary(ro1.name, approval_token=bad_token)
			err_text = str(ctx.exception)
			self.assertNotIn(ro1.name, err_text)
			self.assertNotIn(self.customer.name, err_text)

		# Expired token -> PermissionError with zero PII
		frappe.set_user("Administrator")
		frappe.db.set_value(
			"Repair Order",
			ro1.name,
			"approval_token_expiry",
			add_to_date(now_datetime(), hours=-2),
			update_modified=False,
		)
		frappe.db.commit()

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError) as ctx:
			payments_api.get_ro_payment_summary(ro1.name, approval_token=ro1.approval_token)
		self.assertNotIn(ro1.name, str(ctx.exception))

		# Guest Redis rate limit enforcement
		payments_api.clear_payment_rate_limits()
		frappe.local.request_ip = "203.0.113.10"
		with patch.dict(
			payments_api._PAYMENT_RATE_LIMITS,
			{"summary": {"ip": 3, "ro": 2}, "charge": {"ip": 20, "ro": 10}},
		):
			for _ in range(2):
				with self.assertRaises(frappe.PermissionError):
					payments_api.get_ro_payment_summary(ro2.name, approval_token="bad-token")
			with self.assertRaises(frappe.TooManyRequestsError):
				payments_api.get_ro_payment_summary(ro2.name, approval_token="bad-token")

	def test_04_submit_square_payment_pci_amount_idor_and_rate_limit_guards(self):
		"""submit_square_payment rejects raw PCI fields, amount overrides, bad cents, IDOR, and excess rate."""
		ro1 = self._create_ro()
		ro2 = self._create_ro()
		payments_api.request_ro_deposit(ro1.name, 150.0, note="Deposit")

		frappe.set_user("Guest")

		# 1. Raw PCI fields in kwargs (top-level & nested)
		for bad_kwargs in (
			{"card_number": "4111111111111111"},
			{"cvv": "123"},
			{"payload": {"card_data": {"pan": "4111111111111111"}}},
		):
			with self.assertRaises(frappe.ValidationError):
				payments_api.submit_square_payment(
					repair_order=ro1.name,
					payment_intent=INTENT_MANUAL_DEPOSIT,
					source_id="mock_tok_ok",
					approval_token=ro1.approval_token,
					**bad_kwargs,
				)

		# 2. Raw PCI field sneaking in via frappe.local.form_dict
		frappe.local.form_dict = frappe._dict({"cvc": "999"})
		try:
			with self.assertRaises(frappe.ValidationError):
				payments_api.submit_square_payment(
					repair_order=ro1.name,
					payment_intent=INTENT_MANUAL_DEPOSIT,
					source_id="mock_tok_ok",
					approval_token=ro1.approval_token,
				)
		finally:
			frappe.local.form_dict = frappe._dict()

		# 3. Client-supplied amount override in kwargs and form_dict
		with self.assertRaises(frappe.ValidationError):
			payments_api.submit_square_payment(
				repair_order=ro1.name,
				payment_intent=INTENT_MANUAL_DEPOSIT,
				source_id="mock_tok_ok",
				approval_token=ro1.approval_token,
				amount=1.00,
			)

		frappe.local.form_dict = frappe._dict({"amount": "1.00"})
		try:
			with self.assertRaises(frappe.ValidationError):
				payments_api.submit_square_payment(
					repair_order=ro1.name,
					payment_intent=INTENT_MANUAL_DEPOSIT,
					source_id="mock_tok_ok",
					approval_token=ro1.approval_token,
				)
		finally:
			frappe.local.form_dict = frappe._dict()

		# 4. Cross-RO IDOR rejected before any charge
		with self.assertRaises(frappe.PermissionError):
			payments_api.submit_square_payment(
				repair_order=ro1.name,
				payment_intent=INTENT_MANUAL_DEPOSIT,
				source_id="mock_tok_ok",
				approval_token=ro2.approval_token,
			)

		# 5. Invalid expected_amount_cents values rejected
		for bad_cents in (True, False, -100, 0, "150.00", "abc", 150.0):
			with self.assertRaises(frappe.ValidationError):
				payments_api.submit_square_payment(
					repair_order=ro1.name,
					payment_intent=INTENT_MANUAL_DEPOSIT,
					source_id="mock_tok_ok",
					expected_amount_cents=bad_cents,
					approval_token=ro1.approval_token,
				)

		# 6. Redis rate limit on guest charge endpoint
		payments_api.clear_payment_rate_limits()
		frappe.local.request_ip = "203.0.113.55"
		with patch.dict(
			payments_api._PAYMENT_RATE_LIMITS,
			{"summary": {"ip": 60, "ro": 30}, "charge": {"ip": 3, "ro": 2}},
		):
			for _ in range(2):
				with self.assertRaises(frappe.PermissionError):
					payments_api.submit_square_payment(
						repair_order=ro1.name,
						payment_intent=INTENT_MANUAL_DEPOSIT,
						source_id="mock_tok_ok",
						approval_token="bad-token",
					)
			with self.assertRaises(frappe.TooManyRequestsError):
				payments_api.submit_square_payment(
					repair_order=ro1.name,
					payment_intent=INTENT_MANUAL_DEPOSIT,
					source_id="mock_tok_ok",
					approval_token="bad-token",
				)

	def test_05_submit_square_payment_guest_mock_deposit_and_invoice_balance_e2e(self):
		"""Guest Mock deposit and invoice balance charges post ERPNext Bank PEs and update RO."""
		# --- A. Manual Deposit E2E ---
		ro_dep = self._create_ro(status="Authorized - In Progress", labor_hours=2.0, tax_rate=13.0)
		acct_cfg = resolve_square_accounting_config(ro_dep.company)
		payments_api.request_ro_deposit(ro_dep.name, 150.0, note="Online deposit")

		frappe.set_user("Guest")
		try:
			dep_result = payments_api.submit_square_payment(
				repair_order=ro_dep.name,
				payment_intent=INTENT_MANUAL_DEPOSIT,
				source_id="mock_tok_ok",
				idempotency_key=f"api-dep-{uuid.uuid4().hex[:16]}",
				expected_amount_cents="15000",  # numeric string coercion from HTTP POST
				approval_token=ro_dep.approval_token,
			)
		finally:
			frappe.set_user("Administrator")

		self.track("ISM Payment Transaction", dep_result["transaction"])
		self.track("Payment Entry", dep_result["payment_entry"])

		self.assertEqual(dep_result["status"], TX_STATUS_POSTED)
		self.assertEqual(dep_result["currency"], SQUARE_CURRENCY)
		self.assertEqual(dep_result["amount_cents"], 15000)
		self.assertEqual(dep_result["paid_to_account"], acct_cfg["bank_account"])

		pe_dep = frappe.get_doc("Payment Entry", dep_result["payment_entry"])
		self.assertEqual(pe_dep.docstatus, 1)
		self.assertEqual(pe_dep.paid_to, acct_cfg["bank_account"])
		self.assertEqual(flt(pe_dep.paid_amount, 2), 150.0)
		self.assertIn(f"[RO:{ro_dep.name}]", pe_dep.remarks or "")

		tx_dep = frappe.get_doc("ISM Payment Transaction", dep_result["transaction"])
		self.assertEqual(tx_dep.initiated_by, "Customer Portal")

		ro_dep.reload()
		self.assertEqual(flt(ro_dep.deposit_amount, 2), 150.0)
		self.assertEqual(ro_dep.deposit_request_status, DEPOSIT_REQ_PAID)

		# --- B. Invoice Balance E2E ---
		ro_inv = self._create_ro(status="Work Completed", labor_hours=1.0, tax_rate=13.0)
		chk = ro_inv.process_payment_and_release(paid_amount=0, odometer_out=42050)
		si_name = chk["sales_invoice"]
		self.track("Sales Invoice", si_name)
		frappe.db.commit()

		frappe.set_user("Guest")
		try:
			inv_result = payments_api.submit_square_payment(
				repair_order=ro_inv.name,
				payment_intent=INTENT_INVOICE_BALANCE,
				source_id="mock_tok_ok",
				idempotency_key=f"api-inv-{uuid.uuid4().hex[:16]}",
				expected_amount_cents="11300",
				approval_token=ro_inv.approval_token,
			)
		finally:
			frappe.set_user("Administrator")

		self.track("ISM Payment Transaction", inv_result["transaction"])
		self.track("Payment Entry", inv_result["payment_entry"])

		self.assertEqual(inv_result["status"], TX_STATUS_POSTED)
		self.assertEqual(inv_result["sales_invoice"], si_name)
		self.assertEqual(inv_result["paid_to_account"], acct_cfg["bank_account"])

		pe_inv = frappe.get_doc("Payment Entry", inv_result["payment_entry"])
		self.assertEqual(pe_inv.docstatus, 1)
		self.assertEqual(pe_inv.paid_to, acct_cfg["bank_account"])
		self.assertEqual(len(pe_inv.references), 1)
		self.assertEqual(pe_inv.references[0].reference_name, si_name)

		si = frappe.get_doc("Sales Invoice", si_name)
		self.assertEqual(flt(si.outstanding_amount, 2), 0.0)

		ro_inv.reload()
		self.assertEqual(ro_inv.payment_status, "Paid")
		self.assertEqual(flt(ro_inv.outstanding_amount, 2), 0.0)

	def test_06_square_webhook_hmac_status_codes_replay_and_refund_immutability(self):
		"""square_webhook enforces 401/400/200, reconciles pending payment, dedups replay, and alerts on refund."""
		ro = self._create_ro(status="Authorized - In Progress", labor_hours=2.0, tax_rate=13.0)
		payments_api.request_ro_deposit(ro.name, 100.0, note="Webhook test deposit")

		# Create a Pending transaction via mock_tok_pending
		frappe.set_user("Guest")
		try:
			pending_res = payments_api.submit_square_payment(
				repair_order=ro.name,
				payment_intent=INTENT_MANUAL_DEPOSIT,
				source_id="mock_tok_pending",
				idempotency_key=f"api-wh-{uuid.uuid4().hex[:16]}",
				approval_token=ro.approval_token,
			)
		finally:
			frappe.set_user("Administrator")

		tx_name = pending_res["transaction"]
		sq_pay_id = pending_res["square_payment_id"]
		self.track("ISM Payment Transaction", tx_name)
		self.assertEqual(pending_res["status"], TX_STATUS_PENDING)
		self.assertIsNone(pending_res["payment_entry"])

		# 1. Missing HMAC header -> 401
		status_code, body = self._invoke_webhook(b'{"type": "payment.updated"}', signature=None)
		self.assertEqual(status_code, 401)
		self.assertEqual(body, {"status": "unauthorized"})

		# 2. Invalid HMAC header -> 401
		status_code, body = self._invoke_webhook(b'{"type": "payment.updated"}', signature="bad_sig==")
		self.assertEqual(status_code, 401)
		self.assertEqual(body, {"status": "unauthorized"})

		# 3. Valid HMAC over malformed JSON -> 400
		malformed_bytes = b"{not-valid-json"
		malformed_sig = compute_square_webhook_signature(
			self.WEBHOOK_URL, malformed_bytes, self.WEBHOOK_KEY
		)
		status_code, body = self._invoke_webhook(malformed_bytes, signature=malformed_sig)
		self.assertEqual(status_code, 400)
		self.assertEqual(body, {"status": "invalid_payload"})

		# 4. Valid HMAC over untracked payment ID -> 200 ignored
		untracked_payload = json.dumps(
			{
				"event_id": f"evt-untracked-{uuid.uuid4().hex[:10]}",
				"type": "payment.updated",
				"data": {
					"object": {
						"payment": {
							"id": "sq_unknown_payment_999",
							"status": "COMPLETED",
							"amount_money": {"amount": 10000, "currency": "CAD"},
						}
					}
				},
			}
		).encode("utf-8")
		untracked_sig = compute_square_webhook_signature(
			self.WEBHOOK_URL, untracked_payload, self.WEBHOOK_KEY
		)
		status_code, body = self._invoke_webhook(untracked_payload, signature=untracked_sig)
		self.assertEqual(status_code, 200)
		self.assertEqual(body["status"], "ignored")

		# 5. Valid HMAC over payment.updated (COMPLETED) -> 200 processed + PE posted
		pay_evt_id = f"evt-pay-{uuid.uuid4().hex[:10]}"
		pay_payload = json.dumps(
			{
				"event_id": pay_evt_id,
				"type": "payment.updated",
				"data": {
					"object": {
						"payment": {
							"id": sq_pay_id,
							"reference_id": tx_name,
							"status": "COMPLETED",
							"amount_money": {"amount": 10000, "currency": "CAD"},
							"card_details": {"card": {"card_brand": "VISA", "last_4": "4242"}},
						}
					}
				},
			}
		).encode("utf-8")
		pay_sig = compute_square_webhook_signature(self.WEBHOOK_URL, pay_payload, self.WEBHOOK_KEY)
		status_code, body = self._invoke_webhook(pay_payload, signature=pay_sig)
		self.assertEqual(status_code, 200)
		self.assertEqual(body["status"], "processed")

		tx = frappe.get_doc("ISM Payment Transaction", tx_name)
		self.assertEqual(tx.status, TX_STATUS_POSTED)
		self.assertTrue(tx.payment_entry)
		pe_name = tx.payment_entry
		self.track("Payment Entry", pe_name)

		pe = frappe.get_doc("Payment Entry", pe_name)
		self.assertEqual(pe.docstatus, 1)

		# 6. Replay exact same event_id -> 200 duplicate_ignored, no extra PE
		status_code, body = self._invoke_webhook(pay_payload, signature=pay_sig)
		self.assertEqual(status_code, 200)
		self.assertEqual(body["status"], "duplicate_ignored")

		# 7. Valid HMAC over refund.updated (COMPLETED) -> 200, PE stays docstatus==1, Seam 5 alert created
		refund_id = f"sq_ref_{uuid.uuid4().hex[:10]}"
		refund_payload = json.dumps(
			{
				"event_id": f"evt-ref-{uuid.uuid4().hex[:10]}",
				"type": "refund.updated",
				"data": {
					"object": {
						"refund": {
							"id": refund_id,
							"payment_id": sq_pay_id,
							"status": "COMPLETED",
							"amount_money": {"amount": 10000, "currency": "CAD"},
							"reason": "Customer requested refund",
						}
					}
				},
			}
		).encode("utf-8")
		refund_sig = compute_square_webhook_signature(
			self.WEBHOOK_URL, refund_payload, self.WEBHOOK_KEY
		)
		status_code, body = self._invoke_webhook(refund_payload, signature=refund_sig)
		self.assertEqual(status_code, 200)
		self.assertEqual(body["status"], "processed")

		tx.reload()
		self.assertEqual(tx.status, TX_STATUS_REFUNDED)
		self.assertEqual(tx.erpnext_sync_status, "Manual Review Required")

		# Submitted ERPNext Payment Entry MUST remain untouched (docstatus == 1)
		pe.reload()
		self.assertEqual(pe.docstatus, 1)

		# Seam 5 Shop Action Item alert created for Repair Order
		alerts = frappe.get_all(
			"Shop Action Item",
			filters={
				"reference_doctype": "Repair Order",
				"reference_name": ro.name,
				"category": "General",
				"source": "Agent",
			},
			fields=["name", "title", "priority", "dedup_key"],
			ignore_permissions=True,
		)
		for alert in alerts:
			self.track("Shop Action Item", alert["name"])
		self.assertTrue(
			any("refund" in (a.get("dedup_key") or "").lower() for a in alerts),
			f"Expected Seam 5 refund alert on RO {ro.name}, got: {alerts}",
		)

	def test_07_frozen_payments_package_facade_exports(self):
		"""ism.payments re-exports the complete frozen Section 5 Wave 3.2 public Python facade."""
		expected_symbols = (
			"get_sdk_boot_config",
			"get_ro_payment_state",
			"execute_tokenized_payment",
			"create_deposit_request",
			"cancel_deposit_request",
			"process_square_webhook_event",
			"verify_square_webhook_signature",
			"resolve_square_accounting_config",
			"list_ro_payment_transactions",
			"reconcile_and_post_square_payment",
			"compute_square_webhook_signature",
			"parse_square_webhook_event",
			"get_square_client",
			"SquareApiError",
			"cad_to_cents",
			"cents_to_cad",
			"assert_cad_currency",
			"assert_no_raw_card_fields",
			"normalize_idempotency_key",
			"build_square_deposit_remark",
			"build_square_invoice_remark",
			"STAFF_PAYMENT_ROLES",
			"PROHIBITED_PCI_FIELD_NAMES",
		)
		for symbol in expected_symbols:
			self.assertIn(symbol, payments_pkg.__all__, f"{symbol} missing from ism.payments.__all__")
			self.assertTrue(hasattr(payments_pkg, symbol), f"{symbol} missing from ism.payments")
