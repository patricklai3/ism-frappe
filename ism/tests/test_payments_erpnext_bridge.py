# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 2B tests for Square CAD ERPNext accounting bridge, concurrency guards, and Seam 5 alerts."""

from __future__ import annotations

import hashlib
import json
import uuid

import frappe
import requests
from frappe.utils import flt, nowdate

from ism.ism.doctype.repair_order.billing import _resolve_paid_to_account
from ism.payments.contracts import (
	DEPOSIT_REQ_PAID,
	DEPOSIT_REQ_REQUESTED,
	INTENT_INVOICE_BALANCE,
	INTENT_MANUAL_DEPOSIT,
	SQUARE_CURRENCY,
	SQUARE_ENV_DISABLED,
	SQUARE_ENV_MOCK,
	TX_STATUS_FAILED,
	TX_STATUS_INITIATED,
	TX_STATUS_PARTIALLY_REFUNDED,
	TX_STATUS_PENDING,
	TX_STATUS_POSTED,
	TX_STATUS_RECONCILIATION_ERROR,
	TX_STATUS_REFUNDED,
	TX_STATUS_VOIDED,
	WebhookEventNormalized,
)
from ism.payments.erpnext_bridge import (
	execute_tokenized_payment,
	process_square_webhook_event,
	reconcile_and_post_square_payment,
	resolve_square_accounting_config,
)
from ism.setup_tasks.seed import ensure_square_bank_account_and_mop
from ism.tests.base import ISMTestCase


class _FakeSquareApiError(Exception):
	"""Duck-typed SquareApiError for standalone Wave 2B bridge testing."""

	def __init__(self, code: str, detail: str, status_code: int = 402, retryable: bool = False):
		super().__init__(detail)
		self.code = code
		self.detail = detail
		self.status_code = status_code
		self.retryable = retryable
		self.category = "PAYMENT_METHOD_ERROR"


class _FakeSquareClient:
	"""Deterministic injectable Square client matching Wave 2A MockSquareClient semantics."""

	def __init__(self, on_create_payment=None):
		self.calls: list[dict] = []
		self._idem_cache: dict[str, dict] = {}
		self._on_create_payment = on_create_payment

	def create_payment(
		self,
		*,
		source_id: str,
		amount_cents: int,
		idempotency_key: str,
		reference_id: str,
		note: str,
		buyer_email_address: str | None = None,
	) -> dict:
		call = {
			"source_id": source_id,
			"amount_cents": amount_cents,
			"idempotency_key": idempotency_key,
			"reference_id": reference_id,
			"note": note,
			"buyer_email_address": buyer_email_address,
		}
		self.calls.append(call)

		if idempotency_key in self._idem_cache:
			return self._idem_cache[idempotency_key]

		if source_id == "mock_tok_decline":
			raise _FakeSquareApiError(
				code="CARD_DECLINED",
				detail="Card was declined by issuing bank.",
				status_code=402,
				retryable=False,
			)
		if source_id == "mock_tok_timeout":
			raise requests.Timeout("Simulated Square API read timeout")

		digest = hashlib.sha256(f"{idempotency_key}:{reference_id}".encode()).hexdigest()[:16]
		sq_id = f"sq_mock_{digest}"
		returned_cents = amount_cents - 100 if source_id == "mock_tok_mismatch" else amount_cents
		status = "PENDING" if source_id == "mock_tok_pending" else "COMPLETED"

		payload = {
			"id": sq_id,
			"status": status,
			"reference_id": reference_id,
			"order_id": f"ord_{digest}",
			"location_id": "MOCK_LOC_CAD",
			"amount_money": {"amount": returned_cents, "currency": SQUARE_CURRENCY},
			"receipt_url": f"https://squareup.com/receipt/preview/{sq_id}",
			"receipt_number": f"R{digest[:6].upper()}",
			"card_details": {
				"card": {
					"card_brand": "VISA",
					"last_4": "4242",
				}
			},
		}
		self._idem_cache[idempotency_key] = payload

		if self._on_create_payment is not None:
			self._on_create_payment(call, payload)

		return payload


class TestPaymentsErpnextBridge(ISMTestCase):
	"""End-to-end Wave 2B verification of ERPNext Bank Account bridge and concurrency invariants."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		ensure_square_bank_account_and_mop()
		frappe.db.commit()

		self._settings_snapshot = self._snapshot_square_settings()
		self._enable_mock_square_settings()

		self.customer = self.create_test_customer()
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)
		frappe.db.commit()

	def tearDown(self):
		try:
			frappe.set_user("Administrator")
			frappe.db.rollback()
			self._restore_square_settings(self._settings_snapshot)
			frappe.db.commit()
		finally:
			super().tearDown()

	def _snapshot_square_settings(self) -> dict:
		settings = frappe.get_single("ISM Settings")
		return {
			"square_environment": settings.square_environment,
			"square_currency": settings.square_currency,
			"square_mode_of_payment": settings.square_mode_of_payment,
			"square_bank_account": settings.square_bank_account,
		}

	def _restore_square_settings(self, snapshot: dict) -> None:
		settings = frappe.get_single("ISM Settings")
		for field, value in snapshot.items():
			settings.set(field, value)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)

	def _enable_mock_square_settings(self) -> None:
		settings = frappe.get_single("ISM Settings")
		settings.square_environment = SQUARE_ENV_MOCK
		settings.square_currency = SQUARE_CURRENCY
		if not settings.square_mode_of_payment:
			settings.square_mode_of_payment = "Square"
		settings.save(ignore_permissions=True)
		frappe.db.commit()

	def _create_ro(self, *, status="Authorized - In Progress", labor_hours=2.0, tax_rate=13.0, **extra):
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
					"service_title": "Suspension Inspection & Repair",
					"status": "Approved",
					"labor_hours": labor_hours,
					"labor_rate": 100.0,
				}
			],
		}
		ro_dict.update(extra)
		ro = frappe.get_doc(ro_dict).insert(ignore_permissions=True)
		self.track(ro)
		frappe.db.commit()
		return ro

	def _set_deposit_request(self, ro, amount: float, note: str = "Parts deposit") -> None:
		frappe.db.set_value(
			"Repair Order",
			ro.name,
			{
				"deposit_request_status": DEPOSIT_REQ_REQUESTED,
				"deposit_request_amount": flt(amount, 2),
				"deposit_request_note": note,
			},
		)
		frappe.db.commit()
		ro.reload()

	def _count_ro_payment_entries(self, ro_name: str, sq_payment_id: str | None = None) -> int:
		filters: dict = {"docstatus": 1, "remarks": ["like", f"%[RO:{ro_name}]%"]}
		if sq_payment_id:
			filters["reference_no"] = sq_payment_id
		return frappe.db.count("Payment Entry", filters=filters)

	def test_01_guest_manual_deposit_posts_square_bank_pe_and_reconciles_at_checkout(self):
		"""Guest online Manual Deposit posts CAD Bank PE, syncs RO, and reconciles on SI.advances."""
		ro = self._create_ro(status="Work Completed", labor_hours=2.0, tax_rate=13.0)
		# $200 + 13% HST = $226.00
		self.assertEqual(flt(ro.grand_total, 2), 226.0)
		self._set_deposit_request(ro, 100.0)

		acct_cfg = resolve_square_accounting_config(ro.company)
		client = _FakeSquareClient()

		frappe.set_user("Guest")
		try:
			result = execute_tokenized_payment(
				ro.name,
				INTENT_MANUAL_DEPOSIT,
				"mock_tok_ok",
				idempotency_key=f"dep-{uuid.uuid4().hex[:16]}",
				expected_amount_cents=10000,
				initiated_by="Customer Portal",
				client_ip="127.0.0.1",
				square_client=client,
			)
			self.assertEqual(frappe.session.user, "Guest")
		finally:
			frappe.set_user("Administrator")

		self.track("ISM Payment Transaction", result["transaction"])
		self.track("Payment Entry", result["payment_entry"])

		self.assertEqual(result["status"], TX_STATUS_POSTED)
		self.assertEqual(result["erpnext_sync_status"], "Posted")
		self.assertEqual(result["amount"], 100.0)
		self.assertEqual(result["amount_cents"], 10000)
		self.assertEqual(result["currency"], SQUARE_CURRENCY)
		self.assertEqual(result["paid_to_account"], acct_cfg["bank_account"])
		self.assertEqual(result["card_brand"], "VISA")
		self.assertEqual(result["card_last4"], "4242")
		self.assertFalse(result["idempotent_replay"])

		pe = frappe.get_doc("Payment Entry", result["payment_entry"])
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual(pe.payment_type, "Receive")
		self.assertEqual(pe.party_type, "Customer")
		self.assertEqual(pe.party, ro.customer)
		self.assertEqual(pe.mode_of_payment, acct_cfg["mode_of_payment"])
		self.assertEqual(pe.paid_to, acct_cfg["bank_account"])
		self.assertEqual(pe.reference_no, result["square_payment_id"])
		self.assertIn(f"[RO:{ro.name}]", pe.remarks)
		self.assertIn(f"[SQ:{result['square_payment_id']}]", pe.remarks)
		self.assertEqual(flt(pe.unallocated_amount, 2), 100.0)

		acc_meta = frappe.db.get_value("Account", pe.paid_to, ["account_type", "account_currency"], as_dict=True)
		self.assertEqual(acc_meta.account_type, "Bank")
		self.assertEqual(acc_meta.account_currency, SQUARE_CURRENCY)

		ro.reload()
		self.assertEqual(flt(ro.deposit_amount, 2), 100.0)
		self.assertEqual(flt(ro.paid_amount, 2), 100.0)
		self.assertEqual(flt(ro.outstanding_amount, 2), 126.0)
		self.assertEqual(ro.payment_status, "Partially Paid")
		self.assertEqual(ro.deposit_request_status, DEPOSIT_REQ_PAID)
		self.assertEqual(ro.deposit_request_transaction, result["transaction"])
		self.assertIsNotNone(ro.deposit_request_paid_at)

		# Seam 5 Shop Action Item emitted with valid General/Agent schema
		alerts = frappe.get_all(
			"Shop Action Item",
			filters={"dedup_key": f"square:payment_received:{result['square_payment_id']}"},
			fields=["name", "category", "source", "priority", "reference_doctype", "reference_name"],
		)
		self.assertEqual(len(alerts), 1)
		self.assertEqual(alerts[0].category, "General")
		self.assertEqual(alerts[0].source, "Agent")
		self.assertEqual(alerts[0].reference_doctype, "Repair Order")
		self.assertEqual(alerts[0].reference_name, ro.name)

		# Subsequent checkout automatically allocates this Square advance onto Sales Invoice.advances
		chk = ro.process_payment_and_release(payment_method="Cash", odometer_out=42050)
		if chk.get("sales_invoice"):
			self.track("Sales Invoice", chk["sales_invoice"])
		for pe_name in chk.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		self.assertEqual(chk["status"], "success")
		self.assertEqual(flt(chk["deposit_allocated"], 2), 100.0)
		self.assertEqual(flt(chk["paid_amount"], 2), 226.0)
		self.assertEqual(flt(chk["outstanding_amount"], 2), 0.0)
		self.assertEqual(chk["payment_status"], "Paid")

		si = frappe.get_doc("Sales Invoice", chk["sales_invoice"])
		self.assertEqual(flt(si.total_advance, 2), 100.0)
		self.assertEqual(flt(si.outstanding_amount, 2), 0.0)
		adv_refs = [adv.reference_name for adv in (si.get("advances") or [])]
		self.assertIn(result["payment_entry"], adv_refs)

	def test_02_guest_invoice_balance_settlement_zeroes_si_and_marks_ro_paid(self):
		"""Guest online Invoice Balance allocates Payment Entry to SI and marks RO Paid."""
		ro = self._create_ro(status="Work Completed", labor_hours=1.0, tax_rate=13.0)
		# Checkout unpaid so submitted CAD Sales Invoice has $113.00 outstanding
		chk = ro.process_payment_and_release(paid_amount=0, odometer_out=42025)
		si_name = chk["sales_invoice"]
		self.track("Sales Invoice", si_name)
		frappe.db.commit()

		si = frappe.get_doc("Sales Invoice", si_name)
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(flt(si.outstanding_amount, 2), 113.0)

		acct_cfg = resolve_square_accounting_config(ro.company)
		client = _FakeSquareClient()

		frappe.set_user("Guest")
		try:
			result = execute_tokenized_payment(
				ro.name,
				INTENT_INVOICE_BALANCE,
				"mock_tok_ok",
				idempotency_key=f"inv-{uuid.uuid4().hex[:16]}",
				expected_amount_cents=11300,
				square_client=client,
			)
		finally:
			frappe.set_user("Administrator")

		self.track("ISM Payment Transaction", result["transaction"])
		self.track("Payment Entry", result["payment_entry"])

		self.assertEqual(result["status"], TX_STATUS_POSTED)
		self.assertEqual(result["sales_invoice"], si_name)
		self.assertEqual(result["paid_to_account"], acct_cfg["bank_account"])

		pe = frappe.get_doc("Payment Entry", result["payment_entry"])
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual(pe.paid_to, acct_cfg["bank_account"])
		self.assertEqual(pe.reference_no, result["square_payment_id"])
		self.assertEqual(len(pe.references), 1)
		self.assertEqual(pe.references[0].reference_doctype, "Sales Invoice")
		self.assertEqual(pe.references[0].reference_name, si_name)
		self.assertEqual(flt(pe.references[0].allocated_amount, 2), 113.0)

		si.reload()
		self.assertEqual(flt(si.outstanding_amount, 2), 0.0)

		ro.reload()
		self.assertEqual(flt(ro.paid_amount, 2), 113.0)
		self.assertEqual(flt(ro.outstanding_amount, 2), 0.0)
		self.assertEqual(ro.payment_status, "Paid")

	def test_03_strict_square_bank_account_enforcement_blocks_before_charging(self):
		"""Missing, Cash, USD, or disabled Bank Account fails pre-flight before Square is called."""
		ro = self._create_ro()
		self._set_deposit_request(ro, 50.0)
		company = ro.company
		client = _FakeSquareClient()

		# (a) Cash account configured -> strictly rejected (NEVER falls back to Cash)
		cash_acc = frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Cash", "is_group": 0, "disabled": 0},
			"name",
		)
		self.assertIsNotNone(cash_acc)
		frappe.db.set_single_value("ISM Settings", "square_bank_account", cash_acc)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError):
			resolve_square_accounting_config(company)
		with self.assertRaises(frappe.ValidationError):
			execute_tokenized_payment(
				ro.name, INTENT_MANUAL_DEPOSIT, "mock_tok_ok", square_client=client
			)
		self.assertEqual(len(client.calls), 0)

		# (b) USD Bank account configured -> strictly rejected (CAD only)
		bank_group = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 1, "account_type": "Bank"},
			"name",
		) or frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 1, "account_name": "Bank Accounts"},
			"name",
		)
		self.assertIsNotNone(bank_group)
		usd_acc = frappe.get_doc({
			"doctype": "Account",
			"account_name": f"Test Square USD {uuid.uuid4().hex[:6]}",
			"parent_account": bank_group,
			"company": company,
			"account_type": "Bank",
			"account_currency": "USD",
			"is_group": 0,
		}).insert(ignore_permissions=True)
		self.track(usd_acc)
		frappe.db.set_single_value("ISM Settings", "square_bank_account", usd_acc.name)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError):
			resolve_square_accounting_config(company)
		with self.assertRaises(frappe.ValidationError):
			execute_tokenized_payment(
				ro.name, INTENT_MANUAL_DEPOSIT, "mock_tok_ok", square_client=client
			)
		self.assertEqual(len(client.calls), 0)

		# (c) Missing account (both ISM Settings blank and Mode of Payment unmapped)
		missing_mop = frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": f"UnmappedSquare-{uuid.uuid4().hex[:6]}",
			"type": "Bank",
			"enabled": 1,
		}).insert(ignore_permissions=True)
		self.track(missing_mop)
		frappe.db.set_single_value("ISM Settings", "square_bank_account", "")
		frappe.db.set_single_value("ISM Settings", "square_mode_of_payment", missing_mop.name)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError):
			resolve_square_accounting_config(company)
		with self.assertRaises(frappe.ValidationError):
			execute_tokenized_payment(
				ro.name, INTENT_MANUAL_DEPOSIT, "mock_tok_ok", square_client=client
			)
		self.assertEqual(len(client.calls), 0)

		# (d) Disabled Square environment -> fails pre-flight before charging
		self._restore_square_settings(self._settings_snapshot)
		frappe.db.set_single_value("ISM Settings", "square_environment", SQUARE_ENV_DISABLED)
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError):
			execute_tokenized_payment(
				ro.name, INTENT_MANUAL_DEPOSIT, "mock_tok_ok", square_client=client
			)
		self.assertEqual(len(client.calls), 0)

	def test_04_sync_vs_webhook_race_posts_exactly_one_payment_entry(self):
		"""When webhook reconciles tx before sync HTTP returns, sync posts 0 extra Payment Entries."""
		ro = self._create_ro()
		self._set_deposit_request(ro, 80.0)
		webhook_outcomes: list[dict] = []

		def _fire_webhook_mid_request(call: dict, payload: dict) -> None:
			event: WebhookEventNormalized = {
				"event_id": f"evt_race_{uuid.uuid4().hex[:10]}",
				"event_type": "payment.updated",
				"square_payment_id": payload["id"],
				"reference_id": call["reference_id"],
				"payment_status": "COMPLETED",
				"amount_cents": payload["amount_money"]["amount"],
				"currency": SQUARE_CURRENCY,
				"receipt_url": payload["receipt_url"],
				"receipt_number": payload["receipt_number"],
				"card_brand": "VISA",
				"card_last4": "4242",
				"ignored": False,
			}
			webhook_outcomes.append(process_square_webhook_event(event))

		client = _FakeSquareClient(on_create_payment=_fire_webhook_mid_request)

		frappe.set_user("Guest")
		try:
			sync_result = execute_tokenized_payment(
				ro.name,
				INTENT_MANUAL_DEPOSIT,
				"mock_tok_ok",
				idempotency_key=f"race-{uuid.uuid4().hex[:16]}",
				square_client=client,
			)
		finally:
			frappe.set_user("Administrator")

		self.track("ISM Payment Transaction", sync_result["transaction"])
		self.track("Payment Entry", sync_result["payment_entry"])

		self.assertEqual(len(webhook_outcomes), 1)
		self.assertEqual(webhook_outcomes[0]["status"], "processed")
		webhook_pe = webhook_outcomes[0]["result"]["payment_entry"]

		self.assertEqual(sync_result["status"], TX_STATUS_POSTED)
		self.assertEqual(sync_result["payment_entry"], webhook_pe)
		self.assertTrue(sync_result["idempotent_replay"])
		self.assertEqual(self._count_ro_payment_entries(ro.name, sync_result["square_payment_id"]), 1)

	def test_05_webhook_after_sync_replay_and_idempotency_zero_extra_pes(self):
		"""Replaying webhooks (same/new event_id) and same idempotency_key creates 0 extra PEs."""
		ro = self._create_ro()
		self._set_deposit_request(ro, 65.0)
		client = _FakeSquareClient()
		idem_key = f"idem-{uuid.uuid4().hex[:16]}"

		frappe.set_user("Guest")
		try:
			first = execute_tokenized_payment(
				ro.name,
				INTENT_MANUAL_DEPOSIT,
				"mock_tok_ok",
				idempotency_key=idem_key,
				square_client=client,
			)
			# Idempotent sync replay with same key does not call Square again
			replay_sync = execute_tokenized_payment(
				ro.name,
				INTENT_MANUAL_DEPOSIT,
				"mock_tok_ok",
				idempotency_key=idem_key,
				square_client=client,
			)

			# Webhook replay with first event_id
			evt1: WebhookEventNormalized = {
				"event_id": "evt_replay_same_1",
				"event_type": "payment.updated",
				"square_payment_id": first["square_payment_id"],
				"reference_id": first["transaction"],
				"payment_status": "COMPLETED",
				"amount_cents": 6500,
				"currency": SQUARE_CURRENCY,
				"ignored": False,
			}
			wh1 = process_square_webhook_event(evt1)
			# Exact same event_id replayed again
			wh1_dup = process_square_webhook_event(evt1)
			# Brand new event_id for already-posted payment
			evt2 = dict(evt1)
			evt2["event_id"] = "evt_replay_new_2"
			wh2 = process_square_webhook_event(evt2)

			# Unknown payment -> cleanly ignored
			wh_unknown = process_square_webhook_event({
				"event_id": "evt_unknown_99",
				"event_type": "payment.updated",
				"square_payment_id": "sq_does_not_exist_999",
				"reference_id": "PAYTX-9999-99999",
				"payment_status": "COMPLETED",
				"amount_cents": 1000,
				"currency": SQUARE_CURRENCY,
				"ignored": False,
			})
		finally:
			frappe.set_user("Administrator")

		self.track("ISM Payment Transaction", first["transaction"])
		self.track("Payment Entry", first["payment_entry"])

		self.assertEqual(len(client.calls), 1)
		self.assertTrue(replay_sync["idempotent_replay"])
		self.assertEqual(replay_sync["payment_entry"], first["payment_entry"])
		self.assertEqual(wh1["status"], "duplicate_ignored")
		self.assertEqual(wh1_dup["status"], "duplicate_ignored")
		self.assertEqual(wh2["status"], "duplicate_ignored")
		self.assertEqual(wh_unknown["status"], "ignored")
		self.assertEqual(wh_unknown["reason"], "untracked_payment")
		self.assertEqual(self._count_ro_payment_entries(ro.name, first["square_payment_id"]), 1)

	def test_06_pre_response_webhook_correlation_by_reference_id(self):
		"""Initiated tx with square_payment_id=None correlates via reference_id=tx.name and posts 1 PE."""
		ro = self._create_ro()
		self._set_deposit_request(ro, 90.0)

		tx = frappe.new_doc("ISM Payment Transaction")
		tx.repair_order = ro.name
		tx.customer = ro.customer
		tx.company = ro.company
		tx.payment_intent = INTENT_MANUAL_DEPOSIT
		tx.status = TX_STATUS_INITIATED
		tx.currency = SQUARE_CURRENCY
		tx.amount = 90.0
		tx.amount_cents = 9000
		tx.square_environment = SQUARE_ENV_MOCK
		tx.idempotency_key = f"preresp-{uuid.uuid4().hex[:16]}"
		tx.square_payment_id = None
		tx.insert(ignore_permissions=True)
		self.track(tx)
		frappe.db.commit()

		sq_id = f"sq_preresp_{uuid.uuid4().hex[:12]}"
		frappe.set_user("Guest")
		try:
			wh_res = process_square_webhook_event({
				"event_id": f"evt_preresp_{uuid.uuid4().hex[:8]}",
				"event_type": "payment.updated",
				"square_payment_id": sq_id,
				"reference_id": tx.name,
				"payment_status": "COMPLETED",
				"amount_cents": 9000,
				"currency": SQUARE_CURRENCY,
				"card_brand": "MASTERCARD",
				"card_last4": "5555",
				"receipt_url": f"https://squareup.com/receipt/preview/{sq_id}",
				"ignored": False,
			})
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(wh_res["status"], "processed")
		pe_name = wh_res["result"]["payment_entry"]
		self.track("Payment Entry", pe_name)

		tx.reload()
		self.assertEqual(tx.square_payment_id, sq_id)
		self.assertEqual(tx.status, TX_STATUS_POSTED)
		self.assertEqual(tx.payment_entry, pe_name)
		self.assertEqual(tx.card_brand, "MASTERCARD")
		self.assertEqual(tx.card_last4, "5555")
		self.assertEqual(self._count_ro_payment_entries(ro.name, sq_id), 1)

		ro.reload()
		self.assertEqual(ro.deposit_request_status, DEPOSIT_REQ_PAID)
		self.assertEqual(flt(ro.deposit_amount, 2), 90.0)

	def test_07_concurrent_duplicate_charge_guard_and_decline_timeout_transitions(self):
		"""In-flight Pending/Initiated tx blocks second charge; Failed decline allows clean retry."""
		ro = self._create_ro()
		self._set_deposit_request(ro, 55.0)
		client = _FakeSquareClient()

		# 1. Timeout marks tx Pending (still in-flight)
		frappe.set_user("Guest")
		try:
			pending_res = execute_tokenized_payment(
				ro.name,
				INTENT_MANUAL_DEPOSIT,
				"mock_tok_timeout",
				idempotency_key=f"tmo-{uuid.uuid4().hex[:16]}",
				square_client=client,
			)
		finally:
			frappe.set_user("Administrator")

		self.track("ISM Payment Transaction", pending_res["transaction"])
		self.assertEqual(pending_res["status"], TX_STATUS_PENDING)
		self.assertIsNone(pending_res["payment_entry"])
		self.assertEqual(len(client.calls), 1)

		# 2. Second attempt with different idempotency_key while Pending -> rejected before calling Square
		with self.assertRaises(frappe.ValidationError):
			execute_tokenized_payment(
				ro.name,
				INTENT_MANUAL_DEPOSIT,
				"mock_tok_ok",
				idempotency_key=f"dup-{uuid.uuid4().hex[:16]}",
				square_client=client,
			)
		self.assertEqual(len(client.calls), 1)

		# 3. Initiated status also blocks a concurrent second attempt
		frappe.db.set_value("ISM Payment Transaction", pending_res["transaction"], "status", TX_STATUS_INITIATED)
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError):
			execute_tokenized_payment(
				ro.name,
				INTENT_MANUAL_DEPOSIT,
				"mock_tok_ok",
				idempotency_key=f"dup2-{uuid.uuid4().hex[:16]}",
				square_client=client,
			)
		self.assertEqual(len(client.calls), 1)

		# 4. Card decline on separate RO marks tx Failed and allows a subsequent retry with new key
		ro2 = self._create_ro()
		self._set_deposit_request(ro2, 45.0)
		client2 = _FakeSquareClient()
		decline_key = f"dec-{uuid.uuid4().hex[:16]}"
		with self.assertRaises(frappe.ValidationError):
			execute_tokenized_payment(
				ro2.name,
				INTENT_MANUAL_DEPOSIT,
				"mock_tok_decline",
				idempotency_key=decline_key,
				square_client=client2,
			)
		failed_tx_name = frappe.db.get_value(
			"ISM Payment Transaction",
			{"idempotency_key": decline_key},
			"name",
		)
		self.assertIsNotNone(failed_tx_name)
		self.track("ISM Payment Transaction", failed_tx_name)
		self.assertEqual(
			frappe.db.get_value("ISM Payment Transaction", failed_tx_name, "status"),
			TX_STATUS_FAILED,
		)

		retry_res = execute_tokenized_payment(
			ro2.name,
			INTENT_MANUAL_DEPOSIT,
			"mock_tok_ok",
			idempotency_key=f"ok-{uuid.uuid4().hex[:16]}",
			square_client=client2,
		)
		self.track("ISM Payment Transaction", retry_res["transaction"])
		self.track("Payment Entry", retry_res["payment_entry"])
		self.assertEqual(retry_res["status"], TX_STATUS_POSTED)

	def test_08_stale_ui_expected_amount_cents_mismatch_rejected_before_square(self):
		"""Stale expected_amount_cents rejects before Square call and before creating a ledger row."""
		ro = self._create_ro()
		self._set_deposit_request(ro, 75.0)
		client = _FakeSquareClient()
		idem_key = f"stale-{uuid.uuid4().hex[:16]}"

		with self.assertRaises(frappe.ValidationError):
			execute_tokenized_payment(
				ro.name,
				INTENT_MANUAL_DEPOSIT,
				"mock_tok_ok",
				idempotency_key=idem_key,
				expected_amount_cents=5000,
				square_client=client,
			)
		self.assertEqual(len(client.calls), 0)
		self.assertFalse(frappe.db.exists("ISM Payment Transaction", {"idempotency_key": idem_key}))

	def test_09_mismatch_refund_post_cancel_dispute_preserve_accounting_and_emit_dedup_alerts(self):
		"""Mismatch, partial/full refund, post-post cancel, and dispute never cancel PE/SI and dedup alerts."""
		# (a) Amount mismatch (mock_tok_mismatch): NO PE posted, Reconciliation Error + Critical alert
		ro_mis = self._create_ro()
		self._set_deposit_request(ro_mis, 60.0)
		client_mis = _FakeSquareClient()
		mis_res = execute_tokenized_payment(
			ro_mis.name,
			INTENT_MANUAL_DEPOSIT,
			"mock_tok_mismatch",
			idempotency_key=f"mis-{uuid.uuid4().hex[:16]}",
			square_client=client_mis,
		)
		self.track("ISM Payment Transaction", mis_res["transaction"])
		self.assertEqual(mis_res["status"], TX_STATUS_RECONCILIATION_ERROR)
		self.assertEqual(mis_res["erpnext_sync_status"], "Manual Review Required")
		self.assertIsNone(mis_res["payment_entry"])
		self.assertEqual(self._count_ro_payment_entries(ro_mis.name), 0)

		mis_alerts = frappe.get_all(
			"Shop Action Item",
			filters={"dedup_key": f"square:amount_mismatch:{mis_res['square_payment_id']}"},
			fields=["name", "category", "source", "priority"],
		)
		self.assertEqual(len(mis_alerts), 1)
		self.assertEqual(mis_alerts[0].category, "General")
		self.assertEqual(mis_alerts[0].source, "Agent")
		self.assertEqual(mis_alerts[0].priority, "Critical")

		# (b) Settled Invoice Balance payment -> partial refund -> full refund -> post-post CANCELED -> dispute
		ro = self._create_ro(status="Work Completed", labor_hours=1.0, tax_rate=13.0)
		chk = ro.process_payment_and_release(paid_amount=0, odometer_out=42100)
		si_name = chk["sales_invoice"]
		self.track("Sales Invoice", si_name)
		frappe.db.commit()

		client = _FakeSquareClient()
		frappe.set_user("Guest")
		try:
			paid = execute_tokenized_payment(
				ro.name,
				INTENT_INVOICE_BALANCE,
				"mock_tok_ok",
				idempotency_key=f"settle-{uuid.uuid4().hex[:16]}",
				square_client=client,
			)
			sq_id = paid["square_payment_id"]

			# Partial refund ($40.00 of $113.00)
			r1 = process_square_webhook_event({
				"event_id": "evt_ref_partial_1",
				"event_type": "refund.updated",
				"square_payment_id": sq_id,
				"refund_id": "ref_001",
				"refund_status": "COMPLETED",
				"refund_amount_cents": 4000,
				"currency": SQUARE_CURRENCY,
				"ignored": False,
			})
			self.assertEqual(r1["status"], "processed")
			self.assertEqual(r1["tx_status"], TX_STATUS_PARTIALLY_REFUNDED)
			self.assertEqual(flt(r1["refunded_amount"], 2), 40.0)

			# Duplicate refund event_id ignored
			r1_dup = process_square_webhook_event({
				"event_id": "evt_ref_partial_1",
				"event_type": "refund.updated",
				"square_payment_id": sq_id,
				"refund_id": "ref_001",
				"refund_status": "COMPLETED",
				"refund_amount_cents": 4000,
				"currency": SQUARE_CURRENCY,
				"ignored": False,
			})
			self.assertEqual(r1_dup["status"], "duplicate_ignored")

			# Completing refund ($73.00 -> total $113.00)
			r2 = process_square_webhook_event({
				"event_id": "evt_ref_full_2",
				"event_type": "refund.updated",
				"square_payment_id": sq_id,
				"refund_id": "ref_002",
				"refund_status": "COMPLETED",
				"refund_amount_cents": 7300,
				"currency": SQUARE_CURRENCY,
				"ignored": False,
			})
			self.assertEqual(r2["status"], "processed")
			self.assertEqual(r2["tx_status"], TX_STATUS_REFUNDED)
			self.assertEqual(flt(r2["refunded_amount"], 2), 113.0)

			# Post-settlement CANCELED webhook
			cancel_res = process_square_webhook_event({
				"event_id": "evt_cancel_after_post_1",
				"event_type": "payment.updated",
				"square_payment_id": sq_id,
				"reference_id": paid["transaction"],
				"payment_status": "CANCELED",
				"amount_cents": 11300,
				"currency": SQUARE_CURRENCY,
				"ignored": False,
			})
			self.assertEqual(cancel_res["status"], "processed")

			# Dispute created + state updated
			disp1 = process_square_webhook_event({
				"event_id": "evt_disp_1",
				"event_type": "dispute.created",
				"square_payment_id": sq_id,
				"dispute_id": "dsp_001",
				"dispute_state": "INQUIRY_EVIDENCE_REQUIRED",
				"ignored": False,
			})
			disp2 = process_square_webhook_event({
				"event_id": "evt_disp_2",
				"event_type": "dispute.state.updated",
				"square_payment_id": sq_id,
				"dispute_id": "dsp_001",
				"dispute_state": "PROCESSING",
				"ignored": False,
			})
			self.assertEqual(disp1["status"], "processed")
			self.assertEqual(disp2["status"], "processed")
		finally:
			frappe.set_user("Administrator")

		self.track("ISM Payment Transaction", paid["transaction"])
		self.track("Payment Entry", paid["payment_entry"])

		# Submitted PE and SI MUST remain untouched (docstatus == 1)
		self.assertEqual(frappe.db.get_value("Payment Entry", paid["payment_entry"], "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", si_name, "docstatus"), 1)

		tx = frappe.get_doc("ISM Payment Transaction", paid["transaction"])
		self.assertEqual(tx.status, TX_STATUS_REFUNDED)
		self.assertEqual(flt(tx.refunded_amount, 2), 113.0)
		self.assertEqual(tx.erpnext_sync_status, "Manual Review Required")
		refund_list = json.loads(tx.refund_ids_json or "[]")
		self.assertEqual({r["refund_id"] for r in refund_list}, {"ref_001", "ref_002"})

		# Deduplicated Seam 5 alerts (1 refund alert upgraded to Critical, 1 cancel alert, 1 dispute alert)
		for kind in ("refund", "post_settlement_void_or_fail", "dispute"):
			rows = frappe.get_all(
				"Shop Action Item",
				filters={"dedup_key": f"square:{kind}:{sq_id}"},
				fields=["name", "category", "source", "priority", "reference_doctype", "reference_name"],
			)
			self.assertEqual(len(rows), 1, f"Expected 1 deduplicated Shop Action Item for kind={kind}")
			self.assertEqual(rows[0].category, "General")
			self.assertEqual(rows[0].source, "Agent")
			self.assertEqual(rows[0].priority, "Critical")
			self.assertEqual(rows[0].reference_doctype, "Repair Order")
			self.assertEqual(rows[0].reference_name, ro.name)

	def test_10_concurrent_counter_overpayment_race_flags_manual_review(self):
		"""Concurrent counter payment shrinking SI outstanding triggers safe allocation + Critical alert."""
		ro = self._create_ro(status="Work Completed", labor_hours=1.0, tax_rate=0.0)
		chk = ro.process_payment_and_release(paid_amount=0, odometer_out=42200)
		si_name = chk["sales_invoice"]
		self.track("Sales Invoice", si_name)
		frappe.db.commit()

		# Simulate Initiated online Invoice Balance tx for full $100.00
		tx = frappe.new_doc("ISM Payment Transaction")
		tx.repair_order = ro.name
		tx.sales_invoice = si_name
		tx.customer = ro.customer
		tx.company = ro.company
		tx.payment_intent = INTENT_INVOICE_BALANCE
		tx.status = TX_STATUS_INITIATED
		tx.currency = SQUARE_CURRENCY
		tx.amount = 100.0
		tx.amount_cents = 10000
		tx.square_environment = SQUARE_ENV_MOCK
		tx.idempotency_key = f"overpay-{uuid.uuid4().hex[:16]}"
		tx.insert(ignore_permissions=True)
		self.track(tx)
		frappe.db.commit()

		# Counter takes $60 Cash payment before Square settles -> only $40 remains outstanding
		si = frappe.get_doc("Sales Invoice", si_name)
		cash_acc = _resolve_paid_to_account(ro.company, "Cash")
		pe_counter = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": ro.customer,
			"company": ro.company,
			"posting_date": nowdate(),
			"mode_of_payment": "Cash",
			"paid_to": cash_acc,
			"paid_amount": 60.0,
			"received_amount": 60.0,
			"references": [
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si.name,
					"total_amount": si.grand_total,
					"outstanding_amount": si.outstanding_amount,
					"allocated_amount": 60.0,
				}
			],
		}).insert(ignore_permissions=True)
		pe_counter.submit()
		self.track(pe_counter)
		frappe.db.commit()

		sq_id = f"sq_overpay_{uuid.uuid4().hex[:10]}"
		frappe.set_user("Guest")
		try:
			res = reconcile_and_post_square_payment(
				tx.name,
				{
					"id": sq_id,
					"status": "COMPLETED",
					"amount_money": {"amount": 10000, "currency": SQUARE_CURRENCY},
				},
				source="webhook",
				webhook_event_id=f"evt_overpay_{uuid.uuid4().hex[:8]}",
				webhook_event_type="payment.updated",
			)
		finally:
			frappe.set_user("Administrator")

		self.track("Payment Entry", res["payment_entry"])
		self.assertEqual(res["status"], TX_STATUS_POSTED)
		self.assertEqual(res["erpnext_sync_status"], "Manual Review Required")

		pe_sq = frappe.get_doc("Payment Entry", res["payment_entry"])
		self.assertEqual(flt(pe_sq.references[0].allocated_amount, 2), 40.0)
		self.assertEqual(flt(pe_sq.unallocated_amount, 2), 60.0)

		si.reload()
		self.assertEqual(flt(si.outstanding_amount, 2), 0.0)

		alerts = frappe.get_all(
			"Shop Action Item",
			filters={"dedup_key": f"square:overpayment_review:{sq_id}"},
			fields=["name", "priority", "category", "source"],
		)
		self.assertEqual(len(alerts), 1)
		self.assertEqual(alerts[0].priority, "Critical")
		self.assertEqual(alerts[0].category, "General")
		self.assertEqual(alerts[0].source, "Agent")

	def test_11_monotonic_refund_and_terminal_status_webhook_guards(self):
		"""CH-469: out-of-order refunds, non-CAD refunds, and delayed PENDING/APPROVED webhooks never regress state."""
		ro = self._create_ro()
		self._set_deposit_request(ro, 100.0)
		paid = execute_tokenized_payment(
			ro.name,
			INTENT_MANUAL_DEPOSIT,
			"mock_tok_ok",
			idempotency_key=f"mono-{uuid.uuid4().hex[:16]}",
			square_client=_FakeSquareClient(),
		)
		self.track("ISM Payment Transaction", paid["transaction"])
		self.track("Payment Entry", paid["payment_entry"])
		sq_id = paid["square_payment_id"]

		# (a) Non-CAD refund must NOT be credited to CAD refunded_amount
		usd_ref = process_square_webhook_event({
			"event_id": "evt_ref_usd_1",
			"event_type": "refund.updated",
			"square_payment_id": sq_id,
			"refund_id": "ref_usd_1",
			"refund_status": "COMPLETED",
			"refund_amount_cents": 5000,
			"currency": "USD",
			"ignored": False,
		})
		self.assertEqual(flt(usd_ref["refunded_amount"], 2), 0.0)
		self.assertEqual(usd_ref["tx_status"], TX_STATUS_POSTED)

		# (b) Out-of-order refund.created (PENDING) arriving after refund.updated (COMPLETED)
		ref_comp = process_square_webhook_event({
			"event_id": "evt_ref_comp_1",
			"event_type": "refund.updated",
			"square_payment_id": sq_id,
			"refund_id": "ref_ooo_1",
			"refund_status": "COMPLETED",
			"refund_amount_cents": 4000,
			"currency": "cad",
			"ignored": False,
		})
		self.assertEqual(ref_comp["tx_status"], TX_STATUS_PARTIALLY_REFUNDED)
		self.assertEqual(flt(ref_comp["refunded_amount"], 2), 40.0)

		ref_stale = process_square_webhook_event({
			"event_id": "evt_ref_stale_pending_1",
			"event_type": "refund.created",
			"square_payment_id": sq_id,
			"refund_id": "ref_ooo_1",
			"refund_status": "PENDING",
			"refund_amount_cents": 4000,
			"currency": SQUARE_CURRENCY,
			"ignored": False,
		})
		self.assertEqual(ref_stale["tx_status"], TX_STATUS_PARTIALLY_REFUNDED)
		self.assertEqual(flt(ref_stale["refunded_amount"], 2), 40.0)
		tx = frappe.get_doc("ISM Payment Transaction", paid["transaction"])
		refund_entries = json.loads(tx.refund_ids_json or "[]")
		self.assertEqual(refund_entries[0]["status"], "COMPLETED")

		# (c) Delayed PENDING / APPROVED webhook must NOT resurrect any terminal transaction status
		terminal_statuses = (
			TX_STATUS_FAILED,
			TX_STATUS_VOIDED,
			TX_STATUS_REFUNDED,
			TX_STATUS_PARTIALLY_REFUNDED,
			TX_STATUS_RECONCILIATION_ERROR,
		)
		for idx, term_status in enumerate(terminal_statuses):
			term_tx = frappe.get_doc({
				"doctype": "ISM Payment Transaction",
				"repair_order": ro.name,
				"customer": ro.customer,
				"company": ro.company,
				"payment_intent": INTENT_MANUAL_DEPOSIT,
				"status": term_status,
				"erpnext_sync_status": "Not Posted",
				"amount": 25.0,
				"amount_cents": 2500,
				"currency": SQUARE_CURRENCY,
				"idempotency_key": f"term-{idx}-{uuid.uuid4().hex[:12]}",
				"square_payment_id": f"sq_term_{idx}_{uuid.uuid4().hex[:8]}",
			}).insert(ignore_permissions=True)
			self.track("ISM Payment Transaction", term_tx.name)
			frappe.db.commit()

			delayed_sq_status = "PENDING" if idx % 2 == 0 else "APPROVED"
			res = reconcile_and_post_square_payment(
				term_tx.name,
				{"id": term_tx.square_payment_id, "status": delayed_sq_status},
				source="webhook",
				webhook_event_id=f"evt_delayed_{idx}_{uuid.uuid4().hex[:8]}",
				webhook_event_type="payment.updated",
			)
			self.assertTrue(res["idempotent_replay"])
			self.assertEqual(res["status"], term_status)
			term_tx.reload()
			self.assertEqual(term_tx.status, term_status)
