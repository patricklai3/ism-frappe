# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import flt, nowdate

from ism.ism.doctype.repair_order.billing import process_ro_payment_and_release, record_ro_deposit
from ism.payments.contracts import (
	DEPOSIT_REQ_CANCELLED,
	DEPOSIT_REQ_NONE,
	DEPOSIT_REQ_PAID,
	DEPOSIT_REQ_REQUESTED,
	INTENT_INVOICE_BALANCE,
	INTENT_MANUAL_DEPOSIT,
	SQUARE_CURRENCY,
	SQUARE_ENV_DISABLED,
	SQUARE_ENV_MOCK,
	TX_STATUS_INITIATED,
	TX_STATUS_PENDING,
	TX_STATUS_POSTED,
)
from ism.payments.deposit_requests import (
	cancel_deposit_request,
	create_deposit_request,
	get_ro_payment_state,
	list_ro_payment_transactions,
)
from ism.setup_tasks.seed import ensure_square_bank_account_and_mop
from ism.tests.base import ISMTestCase


class TestPaymentsDepositRequests(ISMTestCase):
	"""Wave 2C verification: manual deposit lifecycle, RBAC, locks, state projection, and ledger."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.customer = self.create_test_customer()
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _create_user_with_role(self, prefix: str, role: str) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": prefix,
			"enabled": 1,
			"user_type": "System User",
			"roles": [{"role": role}],
		}).insert(ignore_permissions=True)
		self.track(user)
		return user.name

	def _create_ro(self, **kwargs):
		ro = self.create_test_repair_order(
			customer=self.customer.name,
			vehicle=self.vehicle.name,
			**kwargs,
		)
		return ro

	def _create_payment_tx(self, ro, status: str = TX_STATUS_INITIATED, **kwargs):
		tx_data = {
			"doctype": "ISM Payment Transaction",
			"repair_order": ro.name,
			"customer": ro.customer,
			"company": ro.company,
			"payment_intent": INTENT_MANUAL_DEPOSIT,
			"status": status,
			"currency": SQUARE_CURRENCY,
			"amount": 50.0,
			"square_environment": SQUARE_ENV_MOCK,
			"idempotency_key": f"test-idem-{frappe.generate_hash(length=12)}",
		}
		tx_data.update(kwargs)
		tx = frappe.get_doc(tx_data).insert(ignore_permissions=True)
		self.track(tx)
		return tx

	def _snapshot_square_settings(self) -> dict:
		settings = frappe.get_single("ISM Settings")
		return {
			"square_environment": settings.square_environment,
			"square_mode_of_payment": settings.square_mode_of_payment,
			"square_bank_account": settings.square_bank_account,
		}

	def _restore_square_settings(self, snapshot: dict) -> None:
		settings = frappe.get_single("ISM Settings")
		for k, v in snapshot.items():
			settings.set(k, v)
		settings.save(ignore_permissions=True)

	def test_rbac_enforcement_for_deposit_requests_and_ledger(self):
		ro = self._create_ro()
		mgr = self._create_user_with_role("dep_mgr", "ISM Manager")
		advisor = self._create_user_with_role("dep_adv", "ISM Service Advisor")
		tech = self._create_user_with_role("dep_tech", "ISM Technician")

		# Technician and Guest denied on create, cancel, list
		for denied_user in (tech, "Guest"):
			frappe.set_user(denied_user)
			with self.assertRaises(frappe.PermissionError):
				create_deposit_request(ro.name, 50.0)
			with self.assertRaises(frappe.PermissionError):
				cancel_deposit_request(ro.name, "unauthorized")
			with self.assertRaises(frappe.PermissionError):
				list_ro_payment_transactions(ro.name)

		# Service Advisor can create, list, and cancel
		frappe.set_user(advisor)
		state = create_deposit_request(ro.name, 75.0, note="Advisor deposit")
		self.assertEqual(state["deposit_request"]["status"], DEPOSIT_REQ_REQUESTED)
		self.assertEqual(state["deposit_request"]["amount"], 75.0)
		self.assertEqual(list_ro_payment_transactions(ro.name), [])
		state = cancel_deposit_request(ro.name, reason="Customer paying in person")
		self.assertEqual(state["deposit_request"]["status"], DEPOSIT_REQ_CANCELLED)

		# Manager can create, list, and cancel
		frappe.set_user(mgr)
		state = create_deposit_request(ro.name, 100.0, note="Manager deposit")
		self.assertEqual(state["deposit_request"]["status"], DEPOSIT_REQ_REQUESTED)
		self.assertEqual(state["deposit_request"]["amount_cents"], 10000)
		self.assertEqual(list_ro_payment_transactions(ro.name), [])
		state = cancel_deposit_request(ro.name)
		self.assertEqual(state["deposit_request"]["status"], DEPOSIT_REQ_CANCELLED)

	def test_default_none_pre_estimate_allowed_and_estimate_cap_enforced(self):
		ro = self._create_ro()
		self.assertEqual(ro.deposit_request_status, DEPOSIT_REQ_NONE)
		self.assertEqual(flt(ro.grand_total), 0.0)

		# Invalid amounts rejected
		for bad_amount in (0, -25.0, True, float("nan"), "not-a-number"):
			with self.assertRaises(frappe.ValidationError):
				create_deposit_request(ro.name, bad_amount)

		# Pre-estimate RO (grand_total == 0) allows positive CAD deposit request
		with patch("frappe.publish_realtime") as mock_rt:
			state = create_deposit_request(ro.name, "125.50", note="  Parts deposit  ")
		self.assertEqual(state["deposit_request"]["status"], DEPOSIT_REQ_REQUESTED)
		self.assertEqual(state["deposit_request"]["amount"], 125.50)
		self.assertEqual(state["deposit_request"]["amount_cents"], 12550)
		self.assertEqual(state["deposit_request"]["note"], "Parts deposit")
		mock_rt.assert_called()

		# Estimated RO (grand_total > 0) caps deposit at remaining balance
		ro_est = self._create_ro(
			services=[
				{
					"service_title": "Diagnostics",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
				}
			],
			tax_rate=13.0,
		)
		remaining = flt(ro_est.grand_total) - flt(ro_est.paid_amount)
		self.assertGreater(remaining, 0.0)

		with self.assertRaises(frappe.ValidationError):
			create_deposit_request(ro_est.name, remaining + 1.0)

		ok_state = create_deposit_request(ro_est.name, remaining)
		self.assertEqual(ok_state["deposit_request"]["amount"], flt(remaining, 2))

	def test_invoiced_and_closed_ro_rejected(self):
		ro_closed = self._create_ro()
		for blocked_status in ("Billed / Paid", "Closed / Released", "Cancelled"):
			frappe.db.set_value("Repair Order", ro_closed.name, "status", blocked_status)
			with self.assertRaises(frappe.ValidationError):
				create_deposit_request(ro_closed.name, 50.0)

		# Linked Sales Invoice blocks manual deposit request
		ro_inv = self._create_ro(
			approval_status="Approved",
			status="Work Completed",
			services=[
				{
					"service_title": "Oil Change",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
				}
			],
			tax_rate=13.0,
		)
		chk = process_ro_payment_and_release(
			ro_inv,
			payment_method="Debit / Interac",
			payment_amount=flt(ro_inv.grand_total),
			odometer_out=10500,
			release_vehicle=False,
		)
		self.track("Sales Invoice", chk["sales_invoice"])
		with self.assertRaises(frappe.ValidationError):
			create_deposit_request(ro_inv.name, 25.0)

	def test_in_flight_transaction_blocks_overwrite_and_cancel(self):
		ro = self._create_ro()
		create_deposit_request(ro.name, 50.0)

		# Initiated blocks both overwrite and cancel
		tx = self._create_payment_tx(ro, status=TX_STATUS_INITIATED)
		with self.assertRaises(frappe.ValidationError):
			create_deposit_request(ro.name, 60.0)
		with self.assertRaises(frappe.ValidationError):
			cancel_deposit_request(ro.name, "trying while initiated")

		# Pending also blocks both overwrite and cancel
		frappe.db.set_value("ISM Payment Transaction", tx.name, "status", TX_STATUS_PENDING)
		with self.assertRaises(frappe.ValidationError):
			create_deposit_request(ro.name, 60.0)
		with self.assertRaises(frappe.ValidationError):
			cancel_deposit_request(ro.name, "trying while pending")

		# Once no longer in-flight, cancel succeeds and logs audit Comment
		frappe.db.set_value("ISM Payment Transaction", tx.name, "status", "Failed")
		state = cancel_deposit_request(ro.name, reason="Card declined, customer switched to cash")
		self.assertEqual(state["deposit_request"]["status"], DEPOSIT_REQ_CANCELLED)
		self.assertEqual(state["deposit_request"]["amount"], 0.0)

		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name},
			pluck="content",
		)
		self.assertTrue(any("Card declined, customer switched to cash" in c for c in comments))

		# Cancelling again when not Requested fails
		with self.assertRaises(frappe.ValidationError):
			cancel_deposit_request(ro.name)

	def test_payment_state_badge_transitions_online_enabled_and_guest_safe_read(self):
		snapshot = self._snapshot_square_settings()
		try:
			ensure_square_bank_account_and_mop()
			settings = frappe.get_single("ISM Settings")
			settings.square_environment = SQUARE_ENV_DISABLED
			settings.save(ignore_permissions=True)

			ro = self._create_ro(
				approval_status="Approved",
				status="Authorized - In Progress",
				services=[
					{
						"service_title": "Suspension Check",
						"status": "Approved",
						"labor_hours": 2.0,
						"labor_rate": 100.0,
					}
				],
				tax_rate=13.0,
			)

			# 1. No Payment Due + online_payments_enabled False when Disabled
			s0 = get_ro_payment_state(ro.name)
			self.assertEqual(s0["portal_payment_badge"], "No Payment Due")
			self.assertIsNone(s0["payable_intent"])
			self.assertEqual(s0["payable_amount_cents"], 0)
			self.assertFalse(s0["online_payments_enabled"])

			# Enable Mock environment -> online_payments_enabled True
			settings.reload()
			settings.square_environment = SQUARE_ENV_MOCK
			settings.save(ignore_permissions=True)

			# 2. Manual Deposit Requested
			create_deposit_request(ro.name, 100.0, note="Booking deposit")
			s1 = get_ro_payment_state(ro.name)
			self.assertEqual(s1["portal_payment_badge"], "Manual Deposit Requested")
			self.assertEqual(s1["payable_intent"], INTENT_MANUAL_DEPOSIT)
			self.assertEqual(s1["payable_amount"], 100.0)
			self.assertEqual(s1["payable_amount_cents"], 10000)
			self.assertTrue(s1["online_payments_enabled"])

			# 3. Deposit Paid (via recorded deposit + Paid request status)
			record_ro_deposit(ro, amount=100.0, payment_method="Debit / Interac")
			tx = self._create_payment_tx(
				ro,
				status=TX_STATUS_POSTED,
				amount=100.0,
				card_brand="VISA",
				card_last4="4242",
				square_receipt_url="https://squareup.com/receipt/preview/test",
			)
			frappe.db.set_value(
				"Repair Order",
				ro.name,
				{
					"deposit_request_status": DEPOSIT_REQ_PAID,
					"deposit_request_transaction": tx.name,
				},
			)
			s2 = get_ro_payment_state(ro.name)
			self.assertEqual(s2["portal_payment_badge"], "Deposit Paid")
			self.assertIsNone(s2["payable_intent"])
			self.assertEqual(len(s2["transactions"]), 1)
			self.assertEqual(s2["transactions"][0]["card_last4"], "4242")
			self.assertEqual(
				s2["transactions"][0]["receipt_url"],
				"https://squareup.com/receipt/preview/test",
			)

			# Guest-safe read check
			frappe.set_user("Guest")
			s_guest = get_ro_payment_state(ro.name)
			self.assertEqual(s_guest["portal_payment_badge"], "Deposit Paid")
			self.assertEqual(s_guest["repair_order"], ro.name)
			frappe.set_user("Administrator")

			# 4. Balance Due (submitted Sales Invoice with outstanding > 0)
			item = self.get_or_create_test_item()
			si = frappe.get_doc({
				"doctype": "Sales Invoice",
				"customer": ro.customer,
				"company": ro.company,
				"currency": SQUARE_CURRENCY,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": item,
						"qty": 1,
						"rate": 200.0,
					}
				],
			}).insert(ignore_permissions=True)
			si.submit()
			si_name = si.name
			self.track("Sales Invoice", si_name)
			frappe.db.set_value("Repair Order", ro.name, "sales_invoice", si_name)

			s3 = get_ro_payment_state(ro.name)
			self.assertEqual(s3["portal_payment_badge"], "Balance Due")
			self.assertEqual(s3["payable_intent"], INTENT_INVOICE_BALANCE)
			self.assertTrue(s3["invoice_submitted"])
			self.assertGreater(s3["invoice_outstanding_amount"], 0.0)
			self.assertEqual(s3["payable_amount"], s3["invoice_outstanding_amount"])

			# 5. Paid in Full (settle remaining balance)
			pe = frappe.get_doc({
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"party_type": "Customer",
				"party": ro.customer,
				"company": ro.company,
				"mode_of_payment": "Debit / Interac",
				"paid_to": frappe.db.get_value(
					"Mode of Payment Account",
					{"parent": "Debit / Interac", "company": ro.company},
					"default_account",
				),
				"paid_amount": s3["invoice_outstanding_amount"],
				"received_amount": s3["invoice_outstanding_amount"],
				"posting_date": nowdate(),
				"reference_no": "SETTLE-TEST-1",
				"reference_date": nowdate(),
				"references": [
					{
						"reference_doctype": "Sales Invoice",
						"reference_name": si_name,
						"allocated_amount": s3["invoice_outstanding_amount"],
					}
				],
			}).insert(ignore_permissions=True)
			pe.submit()
			self.track(pe)

			s4 = get_ro_payment_state(ro.name)
			self.assertEqual(s4["portal_payment_badge"], "Paid in Full")
			self.assertIsNone(s4["payable_intent"])
			self.assertEqual(s4["payable_amount_cents"], 0)
		finally:
			frappe.set_user("Administrator")
			self._restore_square_settings(snapshot)

	def test_list_ro_payment_transactions_returns_staff_ledger(self):
		ro = self._create_ro()
		tx = self._create_payment_tx(ro, status=TX_STATUS_POSTED, amount=88.25, card_last4="1111")
		rows = list_ro_payment_transactions(ro.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["name"], tx.name)
		self.assertEqual(flt(rows[0]["amount"]), 88.25)
		self.assertEqual(rows[0]["card_last4"], "1111")
