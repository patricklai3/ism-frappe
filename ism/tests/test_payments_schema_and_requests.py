# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import frappe

from ism.payments.contracts import (
	DEPOSIT_REQ_NONE,
	INTENT_MANUAL_DEPOSIT,
	PROHIBITED_PCI_FIELD_NAMES,
	SQUARE_CURRENCY,
	SQUARE_DEFAULT_MOP,
	SQUARE_ENV_DISABLED,
	SQUARE_ENV_MOCK,
	SQUARE_ENV_PRODUCTION,
	SQUARE_ENV_SANDBOX,
	SQUARE_IDEMPOTENCY_KEY_MAX_LEN,
	TX_STATUS_INITIATED,
	assert_cad_currency,
	assert_no_raw_card_fields,
	build_square_deposit_remark,
	build_square_invoice_remark,
	cad_to_cents,
	cents_to_cad,
	normalize_idempotency_key,
)
from ism.setup_tasks.seed import ensure_square_bank_account_and_mop, setup_default_ism_settings
from ism.tests.base import ISMTestCase


class TestPaymentsSchemaAndRequests(ISMTestCase):
	"""Wave 1 verification: frozen contracts, ISM Settings, seeding, ISM Payment Transaction, and RO defaults."""

	def _snapshot_square_settings(self) -> dict:
		settings = frappe.get_single("ISM Settings")
		return {
			"square_environment": settings.square_environment,
			"square_application_id": settings.square_application_id,
			"square_location_id": settings.square_location_id,
			"square_currency": settings.square_currency,
			"square_webhook_notification_url": settings.square_webhook_notification_url,
			"square_mode_of_payment": settings.square_mode_of_payment,
			"square_bank_account": settings.square_bank_account,
		}

	def _restore_square_settings(self, snapshot: dict) -> None:
		settings = frappe.get_single("ISM Settings")
		for fieldname, value in snapshot.items():
			settings.set(fieldname, value)
		settings.square_access_token = None
		settings.square_webhook_signature_key = None
		settings.save(ignore_permissions=True)

	def _create_base_ro(self):
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)
		ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
		return customer, vehicle, ro

	def test_contracts_decimal_pci_idempotency_and_remarks(self):
		# Exact Decimal ROUND_HALF_UP conversions (no float rounding drift)
		self.assertEqual(cad_to_cents(19.99), 1999)
		self.assertEqual(cad_to_cents("19.995"), 2000)
		self.assertEqual(cad_to_cents(Decimal("123.45")), 12345)
		self.assertEqual(cad_to_cents(100), 10000)
		self.assertEqual(cents_to_cad(1999), 19.99)
		self.assertEqual(cents_to_cad(0), 0.0)

		# Reject bool, None, NaN, Inf, <= 0, sub-cent, invalid strings
		for bad_amount in (True, False, None, 0, -5, "-0.01", "0.001", "NaN", float("nan"), float("inf"), float("-inf"), "abc"):
			with self.assertRaises(frappe.ValidationError, msg=f"Should reject {bad_amount!r}"):
				cad_to_cents(bad_amount)

		for bad_cents in (True, False, -1, 12.5, "100"):
			with self.assertRaises(frappe.ValidationError, msg=f"Should reject cents {bad_cents!r}"):
				cents_to_cad(bad_cents)

		# Currency guard
		self.assertEqual(assert_cad_currency("cad"), SQUARE_CURRENCY)
		self.assertEqual(assert_cad_currency("CAD"), SQUARE_CURRENCY)
		for bad_curr in (None, "", "USD", "EUR"):
			with self.assertRaises(frappe.ValidationError):
				assert_cad_currency(bad_curr)

		# Idempotency key normalization & 45-char cap
		generated = normalize_idempotency_key(None)
		self.assertTrue(1 <= len(generated) <= SQUARE_IDEMPOTENCY_KEY_MAX_LEN)
		generated_empty = normalize_idempotency_key("   ")
		self.assertTrue(1 <= len(generated_empty) <= SQUARE_IDEMPOTENCY_KEY_MAX_LEN)
		exact_45 = "k" * SQUARE_IDEMPOTENCY_KEY_MAX_LEN
		self.assertEqual(normalize_idempotency_key(exact_45), exact_45)
		with self.assertRaises(frappe.ValidationError):
			normalize_idempotency_key("k" * (SQUARE_IDEMPOTENCY_KEY_MAX_LEN + 1))
		with self.assertRaises(frappe.ValidationError):
			normalize_idempotency_key("bad key with space")

		# Recursive PCI card-field rejection
		assert_no_raw_card_fields({"source_id": "cnon:card-nonce-ok", "nested": [{"safe": True}]})
		for field_name in PROHIBITED_PCI_FIELD_NAMES:
			with self.assertRaises(frappe.ValidationError):
				assert_no_raw_card_fields({field_name: "4111"})
		with self.assertRaises(frappe.ValidationError):
			assert_no_raw_card_fields({"outer": {"items": [{"CVV": "123"}]}})

		# Remark tags for billing.py advance discovery
		dep_remark = build_square_deposit_remark("RO-2026-00001", "sq_pay_123")
		self.assertIn("[RO:RO-2026-00001]", dep_remark)
		self.assertIn("[SQ:sq_pay_123]", dep_remark)
		inv_remark = build_square_invoice_remark("RO-2026-00001", "SINV-2026-00001", "sq_pay_456")
		self.assertIn("[RO:RO-2026-00001]", inv_remark)
		self.assertIn("[SQ:sq_pay_456]", inv_remark)
		self.assertIn("SINV-2026-00001", inv_remark)

	def test_square_seeding_idempotent_and_preserves_operator_customizations(self):
		snapshot = self._snapshot_square_settings()
		try:
			setup_default_ism_settings()
			seeded_first = ensure_square_bank_account_and_mop()
			self.assertTrue(seeded_first, "Expected at least one company Square Bank Account to be seeded")

			for comp_name, acc_name in seeded_first.items():
				acc = frappe.db.get_value(
					"Account",
					acc_name,
					["company", "account_type", "account_currency", "is_group", "disabled"],
					as_dict=True,
				)
				self.assertEqual(acc.company, comp_name)
				self.assertEqual(acc.account_type, "Bank")
				self.assertEqual(acc.account_currency, SQUARE_CURRENCY)
				self.assertEqual(acc.is_group, 0)
				self.assertEqual(acc.disabled, 0)

			mop = frappe.get_doc("Mode of Payment", SQUARE_DEFAULT_MOP)
			self.assertEqual(mop.type, "Bank")
			self.assertEqual(mop.enabled, 1)
			mapped = {row.company: row.default_account for row in mop.accounts}
			for comp_name, acc_name in seeded_first.items():
				self.assertIn(comp_name, mapped)

			# Re-running is idempotent
			seeded_second = ensure_square_bank_account_and_mop()
			self.assertEqual(seeded_first, seeded_second)

			# Operator customization in ISM Settings must NOT be overwritten on re-seed
			settings = frappe.get_single("ISM Settings")
			self.assertEqual(settings.square_currency, SQUARE_CURRENCY)
			self.assertTrue(settings.square_mode_of_payment)
			self.assertTrue(settings.square_bank_account)

			custom_mop = frappe.db.get_value(
				"Mode of Payment",
				{"name": ["!=", SQUARE_DEFAULT_MOP]},
				"name",
			)
			if custom_mop:
				settings.square_mode_of_payment = custom_mop
				settings.save(ignore_permissions=True)
				ensure_square_bank_account_and_mop()
				settings.reload()
				self.assertEqual(settings.square_mode_of_payment, custom_mop)
		finally:
			self._restore_square_settings(snapshot)

	def test_ism_settings_square_validation(self):
		snapshot = self._snapshot_square_settings()
		try:
			ensure_square_bank_account_and_mop()
			settings = frappe.get_single("ISM Settings")

			# Empty env/currency coerces cleanly to Disabled / CAD
			settings.square_environment = ""
			settings.square_currency = ""
			settings.save(ignore_permissions=True)
			settings.reload()
			self.assertEqual(settings.square_environment, SQUARE_ENV_DISABLED)
			self.assertEqual(settings.square_currency, SQUARE_CURRENCY)

			# Non-CAD currency rejected
			if frappe.db.exists("Currency", "USD"):
				settings.square_currency = "USD"
				with self.assertRaises(frappe.ValidationError):
					settings.save(ignore_permissions=True)
				settings.reload()

			# Non-Bank account (e.g. Cash) rejected
			cash_acc = frappe.db.get_value("Account", {"account_type": "Cash", "is_group": 0}, "name")
			if cash_acc:
				settings.square_bank_account = cash_acc
				with self.assertRaises(frappe.ValidationError):
					settings.save(ignore_permissions=True)
				settings.reload()

			# Group account rejected
			group_bank = frappe.db.get_value("Account", {"account_type": "Bank", "is_group": 1}, "name")
			if group_bank:
				settings.square_bank_account = group_bank
				with self.assertRaises(frappe.ValidationError):
					settings.save(ignore_permissions=True)
				settings.reload()

			# Mock mode allows empty live credentials while keeping CAD + Bank Account valid
			settings.square_environment = SQUARE_ENV_MOCK
			settings.square_application_id = ""
			settings.square_location_id = ""
			settings.square_access_token = ""
			settings.save(ignore_permissions=True)
			settings.reload()
			self.assertEqual(settings.square_environment, SQUARE_ENV_MOCK)

			# Sandbox requires application_id, location_id, access_token, mode_of_payment
			settings.square_environment = SQUARE_ENV_SANDBOX
			with self.assertRaises(frappe.ValidationError):
				settings.save(ignore_permissions=True)
			settings.reload()

			# Production requires https:// webhook URL if provided
			settings.square_environment = SQUARE_ENV_PRODUCTION
			settings.square_application_id = "sq0idp-test-app"
			settings.square_location_id = "LTEST123"
			settings.square_access_token = "sq0atp-test-secret"
			settings.square_mode_of_payment = SQUARE_DEFAULT_MOP
			settings.square_webhook_notification_url = "http://insecure.example.com/webhook"
			with self.assertRaises(frappe.ValidationError):
				settings.save(ignore_permissions=True)
			settings.reload()
		finally:
			self._restore_square_settings(snapshot)

	def test_payment_transaction_validation_uniqueness_trash_guard_and_audit_trace(self):
		customer, _vehicle, ro = self._create_base_ro()

		key1 = f"test-idem-{uuid.uuid4().hex[:16]}"
		key2 = f"test-idem-{uuid.uuid4().hex[:16]}"

		# Two Initiated rows with blank/None square_payment_id both succeed (MariaDB NULL unique semantics)
		tx1 = frappe.get_doc({
			"doctype": "ISM Payment Transaction",
			"repair_order": ro.name,
			"customer": customer.name,
			"company": ro.company,
			"payment_intent": INTENT_MANUAL_DEPOSIT,
			"status": TX_STATUS_INITIATED,
			"currency": SQUARE_CURRENCY,
			"amount": 150.25,
			"square_environment": SQUARE_ENV_MOCK,
			"idempotency_key": key1,
			"square_payment_id": "",
		})
		tx1.insert(ignore_permissions=True)
		self.track(tx1)
		self.assertEqual(tx1.amount_cents, 15025)
		self.assertIsNone(tx1.square_payment_id)
		self.assertTrue(tx1.name.startswith("PAYTX-"))

		tx2 = frappe.get_doc({
			"doctype": "ISM Payment Transaction",
			"repair_order": ro.name,
			"customer": customer.name,
			"company": ro.company,
			"payment_intent": INTENT_MANUAL_DEPOSIT,
			"status": TX_STATUS_INITIATED,
			"currency": SQUARE_CURRENCY,
			"amount": 75.00,
			"square_environment": SQUARE_ENV_MOCK,
			"idempotency_key": key2,
			"square_payment_id": None,
			"card_last4": "4242",
		})
		tx2.insert(ignore_permissions=True)
		self.track(tx2)
		self.assertEqual(tx2.amount_cents, 7500)

		# Unique idempotency_key enforced
		dup_idem = frappe.get_doc({
			"doctype": "ISM Payment Transaction",
			"repair_order": ro.name,
			"customer": customer.name,
			"company": ro.company,
			"payment_intent": INTENT_MANUAL_DEPOSIT,
			"currency": SQUARE_CURRENCY,
			"amount": 50.0,
			"square_environment": SQUARE_ENV_MOCK,
			"idempotency_key": key1,
		})
		with self.assertRaises(frappe.UniqueValidationError):
			dup_idem.insert(ignore_permissions=True)

		# Unique square_payment_id enforced when populated
		sq_id = f"sq_pay_{uuid.uuid4().hex[:12]}"
		tx1.square_payment_id = sq_id
		tx1.save(ignore_permissions=True)

		tx2.square_payment_id = sq_id
		with self.assertRaises(frappe.UniqueValidationError):
			tx2.save(ignore_permissions=True)
		tx2.reload()

		# Invalid card_last4 rejected
		tx2.card_last4 = "12ab"
		with self.assertRaises(frappe.ValidationError):
			tx2.save(ignore_permissions=True)
		tx2.reload()

		# Mismatched amount_cents rejected
		tx2.amount_cents = 9999
		with self.assertRaises(frappe.ValidationError):
			tx2.save(ignore_permissions=True)
		tx2.reload()

		# Overlong idempotency_key rejected
		bad_key_tx = frappe.get_doc({
			"doctype": "ISM Payment Transaction",
			"repair_order": ro.name,
			"customer": customer.name,
			"company": ro.company,
			"payment_intent": INTENT_MANUAL_DEPOSIT,
			"currency": SQUARE_CURRENCY,
			"amount": 25.0,
			"square_environment": SQUARE_ENV_MOCK,
			"idempotency_key": "x" * (SQUARE_IDEMPOTENCY_KEY_MAX_LEN + 1),
		})
		with self.assertRaises(frappe.ValidationError):
			bad_key_tx.insert(ignore_permissions=True)

		# Audit trace secret redaction & 50-entry cap
		for idx in range(55):
			tx1.append_audit_entry(
				source="webhook",
				event_type="payment.updated",
				summary=f"evt {idx} Authorization: Bearer sq0atp-supersecret123456 source_id=cnon:secret cvv=999",
				event_id=f"ev_{idx}",
				status_before=TX_STATUS_INITIATED,
				status_after=TX_STATUS_INITIATED,
			)
		entries = json.loads(tx1.webhook_audit_trace)
		self.assertEqual(len(entries), 50)
		self.assertEqual(entries[0]["event_id"], "ev_5")
		self.assertEqual(entries[-1]["event_id"], "ev_54")
		serialized = tx1.webhook_audit_trace
		self.assertNotIn("sq0atp-supersecret123456", serialized)
		self.assertNotIn("cnon:secret", serialized)
		self.assertNotIn("cvv=999", serialized)
		self.assertTrue(all("999" not in e["summary"] for e in entries))
		self.assertIn("[REDACTED]", serialized)

		# on_trash blocks deletion when payment_entry is linked outside test/force mode
		tx1.payment_entry = "ACC-PAY-DUMMY-0001"
		prev_in_test = frappe.flags.in_test
		try:
			frappe.flags.in_test = False
			with self.assertRaises(frappe.ValidationError):
				tx1.on_trash()
		finally:
			frappe.flags.in_test = prev_in_test
		# Inside test teardown mode, on_trash permits cleanup
		tx1.on_trash()

		# Production permissions: delete == 0 for all roles
		meta = frappe.get_meta("ISM Payment Transaction")
		for perm in meta.permissions:
			self.assertEqual(perm.delete, 0, f"Role {perm.role} must have delete=0 on ISM Payment Transaction")

	def test_repair_order_deposit_request_defaults_and_billing_tab_depends_on(self):
		_customer, _vehicle, ro = self._create_base_ro()
		self.assertEqual(ro.deposit_request_status, DEPOSIT_REQ_NONE)
		self.assertEqual(float(ro.deposit_request_amount or 0), 0.0)
		self.assertFalse(ro.deposit_request_transaction)

		ro_meta = frappe.get_meta("Repair Order")
		tab_billing = ro_meta.get_field("tab_billing")
		self.assertIsNotNone(tab_billing)
		self.assertIn("deposit_request_status", tab_billing.depends_on or "")
		self.assertIn("deposit_amount", tab_billing.depends_on or "")
