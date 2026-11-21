# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Manual deposit request lifecycle, Desk state projection, and payment ledger queries."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from ism.payments.contracts import (
	DEPOSIT_REQ_CANCELLED,
	DEPOSIT_REQ_NONE,
	DEPOSIT_REQ_PAID,
	DEPOSIT_REQ_REQUESTED,
	IN_FLIGHT_TX_STATUSES,
	INTENT_INVOICE_BALANCE,
	INTENT_MANUAL_DEPOSIT,
	SQUARE_CURRENCY,
	SQUARE_DEFAULT_MOP,
	SQUARE_ENV_DISABLED,
	STAFF_PAYMENT_ROLES,
	PaymentStateSummary,
	assert_cad_currency,
	cad_to_cents,
	cents_to_cad,
)

_CLOSED_RO_STATUSES = frozenset({"Billed / Paid", "Closed / Released", "Cancelled"})


def _require_staff_payment_role() -> str:
	user = frappe.session.user or "Guest"
	if user == "Guest":
		frappe.throw(
			_("You do not have permission to manage Repair Order deposit requests."),
			frappe.PermissionError,
		)
	user_roles = set(frappe.get_roles(user))
	if not STAFF_PAYMENT_ROLES.intersection(user_roles):
		frappe.throw(
			_("You do not have permission to manage Repair Order deposit requests."),
			frappe.PermissionError,
		)
	return user


def _lock_repair_order(ro_name: str) -> dict[str, Any]:
	if not ro_name or not isinstance(ro_name, str):
		frappe.throw(_("Repair Order is required."), frappe.ValidationError)

	rows = frappe.db.sql(
		"""
		SELECT
			name, status, customer, company, currency, sales_invoice,
			grand_total, deposit_amount, paid_amount, outstanding_amount,
			payment_status, deposit_request_status, deposit_request_amount,
			deposit_request_note, deposit_request_requested_by,
			deposit_request_requested_at, deposit_request_paid_at,
			deposit_request_transaction
		FROM `tabRepair Order`
		WHERE name = %s
		FOR UPDATE
		""",
		(ro_name,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("Repair Order {0} does not exist.").format(ro_name), frappe.DoesNotExistError)
	return rows[0]


def _has_in_flight_tx(ro_name: str, payment_intent: str | None = None) -> bool:
	filters: dict[str, Any] = {
		"repair_order": ro_name,
		"status": ["in", list(IN_FLIGHT_TX_STATUSES)],
	}
	if payment_intent:
		filters["payment_intent"] = payment_intent
	return bool(frappe.db.exists("ISM Payment Transaction", filters))


def _is_online_payments_enabled(company: str | None) -> bool:
	env = frappe.db.get_single_value("ISM Settings", "square_environment") or SQUARE_ENV_DISABLED
	if env == SQUARE_ENV_DISABLED or not company:
		return False

	try:
		from ism.payments.erpnext_bridge import resolve_square_accounting_config

		resolve_square_accounting_config(company)
		return True
	except ImportError:
		pass
	except Exception:
		return False

	mop = frappe.db.get_single_value("ISM Settings", "square_mode_of_payment") or SQUARE_DEFAULT_MOP
	configured_account = frappe.db.get_single_value("ISM Settings", "square_bank_account")

	candidate_account = None
	if configured_account:
		acc_company = frappe.db.get_value("Account", configured_account, "company")
		if acc_company == company:
			candidate_account = configured_account

	if not candidate_account:
		candidate_account = frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": mop, "company": company},
			"default_account",
		)

	if not candidate_account:
		return False

	acc = frappe.db.get_value(
		"Account",
		candidate_account,
		["company", "account_type", "account_currency", "is_group", "disabled"],
		as_dict=True,
	)
	if not acc:
		return False

	return (
		acc.get("company") == company
		and acc.get("account_type") == "Bank"
		and str(acc.get("account_currency") or "").upper() == SQUARE_CURRENCY
		and cint(acc.get("is_group")) == 0
		and cint(acc.get("disabled")) == 0
	)


def create_deposit_request(
	ro_name: str,
	amount: float | int | str,
	note: str | None = None,
) -> dict[str, Any]:
	"""Staff-only manual deposit request initiation on a Repair Order."""
	user = _require_staff_payment_role()
	ro = _lock_repair_order(ro_name)

	if ro.get("sales_invoice"):
		frappe.throw(
			_("Cannot request a manual deposit after a Sales Invoice is linked to this Repair Order."),
			frappe.ValidationError,
		)

	if ro.get("status") in _CLOSED_RO_STATUSES:
		frappe.throw(
			_("Cannot request a deposit on a Repair Order with status {0}.").format(ro.get("status")),
			frappe.ValidationError,
		)

	assert_cad_currency(ro.get("currency") or SQUARE_CURRENCY, context="Repair Order")

	amount_cents = cad_to_cents(amount)
	clean_amount = cents_to_cad(amount_cents)

	grand_total = flt(ro.get("grand_total"))
	if grand_total > 0:
		remaining = max(0.0, grand_total - flt(ro.get("paid_amount")))
		if flt(clean_amount) > remaining + 0.01:
			frappe.throw(
				_("Requested deposit ({0} CAD) exceeds remaining Repair Order balance ({1} CAD).").format(
					clean_amount, flt(remaining, 2)
				),
				frappe.ValidationError,
			)

	if _has_in_flight_tx(ro_name, payment_intent=INTENT_MANUAL_DEPOSIT):
		frappe.throw(
			_("An online deposit payment is already in progress for this Repair Order."),
			frappe.ValidationError,
		)

	clean_note = (str(note).strip()[:500] if note is not None else "") or None
	requested_at = now_datetime()

	frappe.db.set_value(
		"Repair Order",
		ro_name,
		{
			"deposit_request_status": DEPOSIT_REQ_REQUESTED,
			"deposit_request_amount": clean_amount,
			"deposit_request_note": clean_note,
			"deposit_request_requested_by": user,
			"deposit_request_requested_at": requested_at,
			"deposit_request_paid_at": None,
			"deposit_request_transaction": None,
		},
		update_modified=True,
	)

	frappe.publish_realtime(
		"ism_payment_event",
		{
			"event": "deposit_request_created",
			"repair_order": ro_name,
			"status": DEPOSIT_REQ_REQUESTED,
			"amount": clean_amount,
			"amount_cents": amount_cents,
			"requested_by": user,
		},
		after_commit=True,
	)

	return get_ro_payment_state(ro_name)


def cancel_deposit_request(
	ro_name: str,
	reason: str | None = None,
) -> dict[str, Any]:
	"""Staff-only cancellation of an active ('Requested') manual deposit request."""
	user = _require_staff_payment_role()
	ro = _lock_repair_order(ro_name)

	if ro.get("deposit_request_status") != DEPOSIT_REQ_REQUESTED:
		frappe.throw(
			_("No active deposit request to cancel on Repair Order {0}.").format(ro_name),
			frappe.ValidationError,
		)

	if _has_in_flight_tx(ro_name, payment_intent=INTENT_MANUAL_DEPOSIT):
		frappe.throw(
			_("Cannot cancel deposit request while an online deposit payment is in progress."),
			frappe.ValidationError,
		)

	frappe.db.set_value(
		"Repair Order",
		ro_name,
		{
			"deposit_request_status": DEPOSIT_REQ_CANCELLED,
			"deposit_request_amount": 0.0,
		},
		update_modified=True,
	)

	clean_reason = (str(reason).strip()[:500] if reason is not None else "") or ""
	comment_text = _("Online deposit request cancelled by {0}.").format(user)
	if clean_reason:
		comment_text = f"{comment_text} Reason: {clean_reason}"

	frappe.get_doc({
		"doctype": "Comment",
		"comment_type": "Comment",
		"reference_doctype": "Repair Order",
		"reference_name": ro_name,
		"content": comment_text,
	}).insert(ignore_permissions=True)

	frappe.publish_realtime(
		"ism_payment_event",
		{
			"event": "deposit_request_cancelled",
			"repair_order": ro_name,
			"status": DEPOSIT_REQ_CANCELLED,
			"reason": clean_reason or None,
			"cancelled_by": user,
		},
		after_commit=True,
	)

	return get_ro_payment_state(ro_name)


def get_ro_payment_state(ro_name: str) -> PaymentStateSummary:
	"""Canonical PaymentStateSummary projection (safe for both staff and Guest portal callers)."""
	if not ro_name or not isinstance(ro_name, str):
		frappe.throw(_("Repair Order is required."), frappe.ValidationError)

	ro = frappe.db.get_value(
		"Repair Order",
		ro_name,
		[
			"name",
			"status",
			"company",
			"currency",
			"sales_invoice",
			"payment_status",
			"grand_total",
			"deposit_amount",
			"paid_amount",
			"outstanding_amount",
			"deposit_request_status",
			"deposit_request_amount",
			"deposit_request_note",
			"deposit_request_requested_at",
			"deposit_request_paid_at",
			"deposit_request_transaction",
		],
		as_dict=True,
	)
	if not ro:
		frappe.throw(_("Repair Order {0} does not exist.").format(ro_name), frappe.DoesNotExistError)

	si_name = ro.get("sales_invoice")
	invoice_submitted = False
	invoice_grand_total = 0.0
	invoice_outstanding_amount = 0.0

	if si_name:
		si = frappe.db.get_value(
			"Sales Invoice",
			si_name,
			["docstatus", "grand_total", "outstanding_amount"],
			as_dict=True,
		)
		if si and cint(si.get("docstatus")) == 1:
			invoice_submitted = True
			invoice_grand_total = flt(si.get("grand_total"), 2)
			invoice_outstanding_amount = max(0.0, flt(si.get("outstanding_amount"), 2))

	dep_status = ro.get("deposit_request_status") or DEPOSIT_REQ_NONE
	dep_amount = flt(ro.get("deposit_request_amount"), 2)
	dep_amount_cents = cad_to_cents(dep_amount) if dep_amount > 0 else 0

	deposit_request = {
		"status": dep_status,
		"amount": dep_amount,
		"amount_cents": dep_amount_cents,
		"note": ro.get("deposit_request_note"),
		"requested_at": str(ro.get("deposit_request_requested_at"))
		if ro.get("deposit_request_requested_at")
		else None,
		"paid_at": str(ro.get("deposit_request_paid_at")) if ro.get("deposit_request_paid_at") else None,
		"transaction": ro.get("deposit_request_transaction"),
	}

	ro_status = ro.get("status")
	payable_intent: str | None = None
	payable_amount = 0.0
	payable_amount_cents = 0

	if ro_status != "Cancelled":
		if invoice_submitted and invoice_outstanding_amount > 0:
			payable_intent = INTENT_INVOICE_BALANCE
			payable_amount = invoice_outstanding_amount
			payable_amount_cents = cad_to_cents(payable_amount)
		elif (
			not si_name
			and ro_status not in _CLOSED_RO_STATUSES
			and dep_status == DEPOSIT_REQ_REQUESTED
			and dep_amount > 0
		):
			payable_intent = INTENT_MANUAL_DEPOSIT
			payable_amount = dep_amount
			payable_amount_cents = dep_amount_cents

	if ro_status == "Cancelled":
		portal_payment_badge = "No Payment Due"
	elif invoice_submitted:
		portal_payment_badge = "Balance Due" if invoice_outstanding_amount > 0 else "Paid in Full"
	elif payable_intent == INTENT_MANUAL_DEPOSIT:
		portal_payment_badge = "Manual Deposit Requested"
	elif (
		dep_status == DEPOSIT_REQ_PAID
		or flt(ro.get("deposit_amount")) > 0
		or flt(ro.get("paid_amount")) > 0
	):
		portal_payment_badge = "Deposit Paid"
	else:
		portal_payment_badge = "No Payment Due"

	tx_rows = frappe.get_all(
		"ISM Payment Transaction",
		filters={"repair_order": ro_name},
		fields=[
			"name",
			"payment_intent",
			"status",
			"amount",
			"card_brand",
			"card_last4",
			"square_receipt_url",
			"posted_at",
		],
		order_by="creation desc",
		limit=20,
		ignore_permissions=True,
	)

	has_in_flight_payment = _has_in_flight_tx(ro_name)

	transactions = [
		{
			"name": tx.get("name"),
			"payment_intent": tx.get("payment_intent"),
			"status": tx.get("status"),
			"amount": flt(tx.get("amount"), 2),
			"card_brand": tx.get("card_brand"),
			"card_last4": tx.get("card_last4"),
			"receipt_url": tx.get("square_receipt_url"),
			"posted_at": str(tx.get("posted_at")) if tx.get("posted_at") else None,
		}
		for tx in tx_rows
	]

	return PaymentStateSummary(
		repair_order=ro_name,
		currency=SQUARE_CURRENCY,
		payment_status=ro.get("payment_status") or "Unpaid",
		grand_total=flt(ro.get("grand_total"), 2),
		deposit_amount=flt(ro.get("deposit_amount"), 2),
		paid_amount=flt(ro.get("paid_amount"), 2),
		outstanding_amount=flt(ro.get("outstanding_amount"), 2),
		sales_invoice=si_name,
		invoice_submitted=invoice_submitted,
		invoice_grand_total=invoice_grand_total,
		invoice_outstanding_amount=invoice_outstanding_amount,
		deposit_request=deposit_request,
		payable_intent=payable_intent,
		payable_amount=payable_amount,
		payable_amount_cents=payable_amount_cents,
		has_in_flight_payment=has_in_flight_payment,
		portal_payment_badge=portal_payment_badge,
		online_payments_enabled=_is_online_payments_enabled(ro.get("company")),
		transactions=transactions,
	)


def list_ro_payment_transactions(ro_name: str) -> list[dict[str, Any]]:
	"""Staff-only detailed ledger list for Repair Order Desk / advisor review (excluding secrets)."""
	_require_staff_payment_role()

	if not ro_name or not isinstance(ro_name, str):
		frappe.throw(_("Repair Order is required."), frappe.ValidationError)
	if not frappe.db.exists("Repair Order", ro_name):
		frappe.throw(_("Repair Order {0} does not exist.").format(ro_name), frappe.DoesNotExistError)

	rows = frappe.get_all(
		"ISM Payment Transaction",
		filters={"repair_order": ro_name},
		fields=[
			"name",
			"repair_order",
			"sales_invoice",
			"customer",
			"company",
			"payment_intent",
			"status",
			"currency",
			"amount",
			"amount_cents",
			"refunded_amount",
			"square_environment",
			"idempotency_key",
			"square_payment_id",
			"square_order_id",
			"square_location_id",
			"square_status",
			"square_receipt_url",
			"square_receipt_number",
			"card_brand",
			"card_last4",
			"payment_entry",
			"paid_to_account",
			"posted_at",
			"erpnext_sync_status",
			"initiated_by",
			"error_message",
			"last_webhook_event_id",
			"last_webhook_event_type",
			"creation",
			"modified",
		],
		order_by="creation desc",
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]
