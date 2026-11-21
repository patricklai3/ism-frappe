# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Row-locked Square CAD payment execution and immediate ERPNext Bank reconciliation bridge."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import frappe
import requests
from frappe import _
from frappe.utils import cint, flt, now_datetime, nowdate

from ism.payments.contracts import (
	DEPOSIT_REQ_PAID,
	DEPOSIT_REQ_REQUESTED,
	IN_FLIGHT_TX_STATUSES,
	INTENT_INVOICE_BALANCE,
	INTENT_MANUAL_DEPOSIT,
	PAYMENT_INTENTS,
	SQUARE_CURRENCY,
	SQUARE_DEFAULT_MOP,
	SQUARE_ENV_DISABLED,
	TX_STATUS_FAILED,
	TX_STATUS_INITIATED,
	TX_STATUS_PARTIALLY_REFUNDED,
	TX_STATUS_PENDING,
	TX_STATUS_POSTED,
	TX_STATUS_RECONCILIATION_ERROR,
	TX_STATUS_REFUNDED,
	TX_STATUS_VOIDED,
	SquareAccountingConfig,
	SquareChargeResult,
	WebhookEventNormalized,
	assert_cad_currency,
	build_square_deposit_remark,
	build_square_invoice_remark,
	cad_to_cents,
	cents_to_cad,
	normalize_idempotency_key,
)

if TYPE_CHECKING:
	from ism.payments.square_client import SquareClient

_BLOCKED_DEPOSIT_RO_STATUSES = frozenset({"Billed / Paid", "Closed / Released", "Cancelled"})
_SYNC_STATUS_NOT_POSTED = "Not Posted"
_SYNC_STATUS_POSTED = "Posted"
_SYNC_STATUS_MANUAL_REVIEW = "Manual Review Required"
_AMOUNT_TOLERANCE = 0.01
_MAX_PROCESSED_WEBHOOK_IDS = 500
_CARD_LAST4_RE = re.compile(r"^\d{4}$")

_SENSITIVE_MSG_PATTERNS = (
	(re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']+"), r"\1[REDACTED]"),
	(re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"), r"\1[REDACTED]"),
	(re.compile(r"\b(?:EAAAl|EAAAE|EAAA|sq0atp-|sq0csp-)[A-Za-z0-9_\-]{6,}\b"), "[REDACTED]"),
	(
		re.compile(
			r"(?i)\b(access_token|square_access_token|webhook_signature_key|square_webhook_signature_key"
			r"|signature_key|source_id|api_key|secret|password|card_number|pan|cvv|cvc)\b"
			r"(\s*[:=]\s*[\"']?)([^\"'\s,;}\]]+)([\"']?)"
		),
		r"\1\2[REDACTED]\4",
	),
)


@contextmanager
def _posting_user_context():
	"""Run ERPNext Payment Entry validation/submit under Administrator for Guest/portal callers.

	ERPNext's PaymentEntry.validate() calls get_party_account() which checks Account read
	permissions against frappe.session.user even when doc.flags.ignore_permissions is True.
	Restores the exact prior session user on exit without touching the active DB transaction.
	"""
	prev_user = getattr(frappe.session, "user", None)
	switched = bool(prev_user and prev_user != "Administrator")
	if switched:
		frappe.set_user("Administrator")
	try:
		yield
	finally:
		if switched and prev_user:
			frappe.set_user(prev_user)


def _clean_str(value: Any) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def _sanitize_error_message(message: Any, max_len: int = 400) -> str:
	raw = "" if message is None else str(message)
	scrubbed = raw
	for pattern, replacement in _SENSITIVE_MSG_PATTERNS:
		scrubbed = pattern.sub(replacement, scrubbed)
	return scrubbed.strip()[:max_len]


def _load_json_list(raw: Any) -> list[Any]:
	if not raw:
		return []
	if isinstance(raw, list):
		return list(raw)
	try:
		parsed = json.loads(raw)
		return parsed if isinstance(parsed, list) else []
	except (ValueError, TypeError):
		return []


def _load_square_client_module() -> Any | None:
	try:
		from ism.payments import square_client as sq_mod

		return sq_mod
	except ImportError:
		return None


def _get_square_settings_snapshot_safe() -> dict[str, Any]:
	sq_mod = _load_square_client_module()
	if sq_mod is not None and hasattr(sq_mod, "get_square_settings_snapshot"):
		return sq_mod.get_square_settings_snapshot()

	singles = frappe.db.get_singles_dict("ISM Settings") or {}
	return {
		"environment": _clean_str(singles.get("square_environment")) or SQUARE_ENV_DISABLED,
		"currency": _clean_str(singles.get("square_currency")) or SQUARE_CURRENCY,
		"mode_of_payment": _clean_str(singles.get("square_mode_of_payment")) or SQUARE_DEFAULT_MOP,
		"bank_account": _clean_str(singles.get("square_bank_account")),
		"application_id": _clean_str(singles.get("square_application_id")),
		"location_id": _clean_str(singles.get("square_location_id")),
	}


def _resolve_square_client(
	injected_client: SquareClient | None,
	settings_snapshot: dict[str, Any],
) -> Any:
	if injected_client is not None:
		return injected_client

	sq_mod = _load_square_client_module()
	if sq_mod is None or not hasattr(sq_mod, "get_square_client"):
		frappe.throw(
			_("Square payment client is not available."),
			frappe.ValidationError,
		)
	return sq_mod.get_square_client(settings_snapshot)


def _is_square_api_error(exc: Exception) -> bool:
	sq_mod = _load_square_client_module()
	if sq_mod is not None and hasattr(sq_mod, "SquareApiError"):
		if isinstance(exc, sq_mod.SquareApiError):
			return True
	if isinstance(exc, frappe.ValidationError | requests.RequestException):
		return False
	if exc.__class__.__name__ == "SquareApiError":
		return True
	return hasattr(exc, "code") and (
		hasattr(exc, "status_code") or hasattr(exc, "detail") or hasattr(exc, "category")
	)


def _format_square_api_error(exc: Exception) -> str:
	code = _clean_str(getattr(exc, "code", None))
	detail = _clean_str(getattr(exc, "detail", None)) or _clean_str(str(exc))
	if code and detail and code not in detail:
		msg = f"Square payment declined ({code}): {detail}"
	elif code:
		msg = f"Square payment declined ({code})."
	elif detail:
		msg = f"Square payment failed: {detail}"
	else:
		msg = "Square payment failed."
	return _sanitize_error_message(msg)


def _build_charge_result(tx, *, idempotent_replay: bool = False) -> SquareChargeResult:
	return SquareChargeResult(
		transaction=tx.name,
		repair_order=tx.repair_order,
		sales_invoice=tx.sales_invoice or None,
		payment_intent=tx.payment_intent,
		status=tx.status,
		erpnext_sync_status=tx.erpnext_sync_status or _SYNC_STATUS_NOT_POSTED,
		amount=flt(tx.amount, 2),
		amount_cents=int(tx.amount_cents),
		currency=tx.currency or SQUARE_CURRENCY,
		idempotency_key=tx.idempotency_key,
		square_payment_id=tx.square_payment_id or None,
		square_status=tx.square_status or None,
		payment_entry=tx.payment_entry or None,
		paid_to_account=tx.paid_to_account or None,
		card_brand=tx.card_brand or None,
		card_last4=tx.card_last4 or None,
		receipt_url=tx.square_receipt_url or None,
		receipt_number=tx.square_receipt_number or None,
		posted_at=str(tx.posted_at) if tx.posted_at else None,
		idempotent_replay=bool(idempotent_replay),
		error_message=tx.error_message or None,
	)


def _validate_square_bank_account(account_name: str | None, company: str) -> str:
	if not account_name:
		frappe.throw(
			_(
				"No Square Bank Account is configured for company {0}. "
				"Configure a CAD Bank Account in ISM Settings or Mode of Payment Account."
			).format(company),
			frappe.ValidationError,
		)

	acc = frappe.db.get_value(
		"Account",
		account_name,
		["name", "company", "is_group", "disabled", "account_type", "account_currency"],
		as_dict=True,
	)
	if not acc:
		frappe.throw(
			_("Configured Square Bank Account {0} does not exist.").format(account_name),
			frappe.ValidationError,
		)
	if acc.company != company:
		frappe.throw(
			_("Square Bank Account {0} belongs to company {1}, not {2}.").format(
				acc.name, acc.company, company
			),
			frappe.ValidationError,
		)
	if cint(acc.is_group) != 0:
		frappe.throw(
			_("Square Bank Account {0} must be a leaf account (not a group account).").format(acc.name),
			frappe.ValidationError,
		)
	if cint(acc.disabled) != 0:
		frappe.throw(
			_("Square Bank Account {0} is disabled.").format(acc.name),
			frappe.ValidationError,
		)
	if (acc.account_type or "").strip() != "Bank":
		frappe.throw(
			_(
				"Square Bank Account {0} must have Account Type 'Bank' (got '{1}'). "
				"Cash or non-Bank fallback is not permitted."
			).format(acc.name, acc.account_type or ""),
			frappe.ValidationError,
		)
	if (acc.account_currency or "").strip().upper() != SQUARE_CURRENCY:
		frappe.throw(
			_("Square Bank Account {0} must use {1} currency (got '{2}').").format(
				acc.name, SQUARE_CURRENCY, acc.account_currency or ""
			),
			frappe.ValidationError,
		)
	return acc.name


def resolve_square_accounting_config(company: str) -> SquareAccountingConfig:
	"""Resolve verified CAD Square Bank Account & Mode of Payment for `company`.

	Priority:
	  1. ISM Settings.square_bank_account if it belongs to `company`
	  2. Mode of Payment Account for (ISM Settings.square_mode_of_payment or "Square", company)

	Strictly asserts Account exists, is_group == 0, disabled == 0, account_type == "Bank",
	and account_currency == "CAD". NEVER falls back to Cash.
	"""
	clean_company = _clean_str(company)
	if not clean_company or not frappe.db.exists("Company", clean_company):
		frappe.throw(
			_("Valid Company is required to resolve Square accounting configuration (got {0!r}).").format(
				company
			),
			frappe.ValidationError,
		)

	singles = frappe.db.get_singles_dict("ISM Settings") or {}
	mop = _clean_str(singles.get("square_mode_of_payment")) or SQUARE_DEFAULT_MOP
	if not frappe.db.exists("Mode of Payment", mop):
		frappe.throw(
			_("Configured Square Mode of Payment '{0}' does not exist.").format(mop),
			frappe.ValidationError,
		)
	if frappe.db.has_column("Mode of Payment", "enabled"):
		mop_enabled = frappe.db.get_value("Mode of Payment", mop, "enabled")
		if mop_enabled is not None and cint(mop_enabled) == 0:
			frappe.throw(
				_("Configured Square Mode of Payment '{0}' is disabled.").format(mop),
				frappe.ValidationError,
			)

	candidate_account: str | None = None
	settings_account = _clean_str(singles.get("square_bank_account"))
	if settings_account:
		settings_acc_meta = frappe.db.get_value(
			"Account",
			settings_account,
			["name", "company"],
			as_dict=True,
		)
		if not settings_acc_meta:
			frappe.throw(
				_("Configured ISM Settings Square Bank Account '{0}' does not exist.").format(
					settings_account
				),
				frappe.ValidationError,
			)
		if settings_acc_meta.company == clean_company:
			candidate_account = settings_acc_meta.name

	if not candidate_account:
		candidate_account = _clean_str(
			frappe.db.get_value(
				"Mode of Payment Account",
				{"parent": mop, "company": clean_company},
				"default_account",
			)
		)

	verified_account = _validate_square_bank_account(candidate_account, clean_company)
	return SquareAccountingConfig(
		company=clean_company,
		mode_of_payment=mop,
		bank_account=verified_account,
		currency=SQUARE_CURRENCY,
	)


def _emit_payment_alert(
	*,
	ro_name: str,
	tx_name: str,
	kind: str,
	title: str,
	priority: str,
	details: str,
	payment_intent: str | None = None,
	tx_status: str | None = None,
	amount: float = 0.0,
	payment_entry: str | None = None,
	square_payment_id: str | None = None,
	publish_realtime: bool = True,
) -> str | None:
	"""Upsert a Seam 5 Shop Action Item (Guest-safe) and publish sanitized realtime event."""
	dedup_key = f"square:{kind}:{square_payment_id or tx_name}"
	clean_title = _sanitize_error_message(title, max_len=140)
	clean_details = _sanitize_error_message(details, max_len=1000)

	existing_name = frappe.db.get_value(
		"Shop Action Item",
		{"dedup_key": dedup_key, "status": ["in", ["Open", "In Progress"]]},
		"name",
	)
	if existing_name:
		item_doc = frappe.get_doc("Shop Action Item", existing_name)
		item_doc.title = clean_title
		item_doc.category = "General"
		item_doc.priority = priority
		item_doc.source = "Agent"
		item_doc.reference_doctype = "Repair Order"
		item_doc.reference_name = ro_name
		item_doc.details = clean_details
		item_doc.save(ignore_permissions=True)
		action_item_name = item_doc.name
	else:
		session_user = getattr(frappe.session, "user", None)
		reported_by = session_user if session_user and session_user != "Guest" else "Administrator"
		item_doc = frappe.new_doc("Shop Action Item")
		item_doc.title = clean_title
		item_doc.category = "General"
		item_doc.priority = priority
		item_doc.status = "Open"
		item_doc.source = "Agent"
		item_doc.reference_doctype = "Repair Order"
		item_doc.reference_name = ro_name
		item_doc.dedup_key = dedup_key
		item_doc.reported_by = reported_by
		item_doc.details = clean_details
		item_doc.insert(ignore_permissions=True)
		action_item_name = item_doc.name

	if publish_realtime:
		frappe.publish_realtime(
			"ism_payment_event",
			{
				"repair_order": ro_name,
				"transaction": tx_name,
				"payment_intent": payment_intent,
				"status": tx_status,
				"amount": flt(amount, 2),
				"currency": SQUARE_CURRENCY,
				"payment_entry": payment_entry,
				"event_kind": kind,
			},
			after_commit=True,
		)

	return action_item_name


def _unwrap_square_payment(square_payment: dict[str, Any] | None) -> dict[str, Any]:
	if not isinstance(square_payment, dict):
		return {}
	nested = square_payment.get("payment")
	if isinstance(nested, dict):
		return nested
	return square_payment


def _record_webhook_event_id(tx, event_id: str | None, event_type: str | None) -> bool:
	"""Check & record webhook event_id on tx. Returns True if duplicate (already processed)."""
	if not event_id:
		return False
	processed = _load_json_list(tx.processed_webhook_event_ids)
	if event_id in processed:
		return True
	processed.append(event_id)
	if len(processed) > _MAX_PROCESSED_WEBHOOK_IDS:
		processed = processed[-_MAX_PROCESSED_WEBHOOK_IDS:]
	tx.processed_webhook_event_ids = json.dumps(processed)
	tx.last_webhook_event_id = event_id
	if event_type:
		tx.last_webhook_event_type = event_type
	return False


def _lock_tx_row(tx_name: str) -> dict[str, Any] | None:
	rows = frappe.db.sql(
		"""
		SELECT name, repair_order, sales_invoice, payment_intent, status, payment_entry
		FROM `tabISM Payment Transaction`
		WHERE name = %s
		FOR UPDATE
		""",
		(tx_name,),
		as_dict=True,
	)
	return rows[0] if rows else None


def _lock_ro_row(ro_name: str) -> dict[str, Any] | None:
	rows = frappe.db.sql(
		"""
		SELECT name, customer, company, currency, status, sales_invoice,
		       grand_total, deposit_amount, paid_amount, outstanding_amount,
		       deposit_request_status, deposit_request_amount
		FROM `tabRepair Order`
		WHERE name = %s
		FOR UPDATE
		""",
		(ro_name,),
		as_dict=True,
	)
	return rows[0] if rows else None


def _lock_si_row(si_name: str) -> dict[str, Any] | None:
	rows = frappe.db.sql(
		"""
		SELECT name, customer, company, currency, docstatus, grand_total, outstanding_amount
		FROM `tabSales Invoice`
		WHERE name = %s
		FOR UPDATE
		""",
		(si_name,),
		as_dict=True,
	)
	return rows[0] if rows else None


def execute_tokenized_payment(
	repair_order: str,
	payment_intent: str,
	source_id: str,
	*,
	idempotency_key: str | None = None,
	expected_amount_cents: int | None = None,
	buyer_email: str | None = None,
	initiated_by: str = "Customer Portal",
	client_ip: str | None = None,
	square_client: SquareClient | None = None,
) -> SquareChargeResult:
	"""Canonical server-side CAD tokenized payment execution entry point.

	Step 1 (Short locked DB tx):
	  - Check existing idempotency_key row (canonical lock order: tx before RO).
	  - Lock Repair Order (FOR UPDATE) and Sales Invoice (FOR UPDATE if Invoice Balance).
	  - Enforce CAD currency, Square enabled, pre-flight Square Bank Account config.
	  - Compute server-authoritative amount_cents; verify stale-UI expected_amount_cents.
	  - Reject concurrent in-flight (Initiated/Pending) transaction for (repair_order, payment_intent).
	  - Insert ISM Payment Transaction(status="Initiated") and frappe.db.commit() BEFORE external call.
	Step 2 (External HTTPS call — zero DB locks held):
	  - Call square_client.create_payment(...) with reference_id=tx.name.
	  - On SquareApiError -> lock tx, mark Failed, commit, raise ValidationError.
	  - On requests.RequestException/Timeout -> lock tx, mark Pending, commit, return pending result.
	Step 3 (Row-locked reconciliation & immediate ERPNext Bank posting):
	  - Delegate to reconcile_and_post_square_payment(tx.name, payment_payload, source="sync_api").
	"""
	ro_name = _clean_str(repair_order)
	if not ro_name:
		frappe.throw(_("Repair Order is required."), frappe.ValidationError)

	intent = _clean_str(payment_intent)
	if intent not in PAYMENT_INTENTS:
		frappe.throw(
			_("Invalid payment intent '{0}'. Expected one of: {1}.").format(
				payment_intent, ", ".join(PAYMENT_INTENTS)
			),
			frappe.ValidationError,
		)

	clean_source_id = _clean_str(source_id)
	if not clean_source_id:
		frappe.throw(_("Square payment source token is required."), frappe.ValidationError)

	if expected_amount_cents is not None:
		if isinstance(expected_amount_cents, bool) or not isinstance(expected_amount_cents, int):
			frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)
		if expected_amount_cents <= 0:
			frappe.throw(_("expected_amount_cents must be greater than zero."), frappe.ValidationError)

	idem_key = normalize_idempotency_key(idempotency_key)

	settings = _get_square_settings_snapshot_safe()
	square_env = (
		_clean_str(settings.get("environment") or settings.get("square_environment")) or SQUARE_ENV_DISABLED
	)
	if square_env == SQUARE_ENV_DISABLED:
		frappe.throw(_("Online Square payments are currently disabled."), frappe.ValidationError)
	assert_cad_currency(
		settings.get("currency") or settings.get("square_currency") or SQUARE_CURRENCY,
		context="ISM Settings Square",
	)

	client = _resolve_square_client(square_client, settings)

	# Step 1a: Idempotency check first (preserves canonical lock order: tx -> RO -> SI)
	existing_rows = frappe.db.sql(
		"""
		SELECT name, repair_order, payment_intent
		FROM `tabISM Payment Transaction`
		WHERE idempotency_key = %s
		FOR UPDATE
		""",
		(idem_key,),
		as_dict=True,
	)
	if existing_rows:
		existing = existing_rows[0]
		if existing.get("repair_order") != ro_name or existing.get("payment_intent") != intent:
			frappe.throw(
				_("Idempotency key has already been used for a different Repair Order or payment intent."),
				frappe.ValidationError,
			)
		existing_tx = frappe.get_doc("ISM Payment Transaction", existing["name"])
		return _build_charge_result(existing_tx, idempotent_replay=True)

	# Step 1b: Lock Repair Order (and Sales Invoice if Invoice Balance)
	ro_row = _lock_ro_row(ro_name)
	if not ro_row:
		frappe.throw(_("Repair Order {0} not found.").format(ro_name), frappe.DoesNotExistError)

	assert_cad_currency(
		ro_row.get("currency") or SQUARE_CURRENCY,
		context=f"Repair Order {ro_name}",
	)
	company = _clean_str(ro_row.get("company")) or _clean_str(frappe.defaults.get_global_default("company"))
	resolve_square_accounting_config(company)

	linked_si: str | None = None
	if intent == INTENT_MANUAL_DEPOSIT:
		if ro_row.get("status") in _BLOCKED_DEPOSIT_RO_STATUSES:
			frappe.throw(
				_("Cannot pay an online deposit for Repair Order {0} in status '{1}'.").format(
					ro_name, ro_row.get("status")
				),
				frappe.ValidationError,
			)
		if _clean_str(ro_row.get("sales_invoice")):
			frappe.throw(
				_("Cannot pay a manual deposit after Sales Invoice {0} has been created.").format(
					ro_row.get("sales_invoice")
				),
				frappe.ValidationError,
			)
		if (ro_row.get("deposit_request_status") or "") != DEPOSIT_REQ_REQUESTED:
			frappe.throw(
				_("No active manual deposit request exists for Repair Order {0}.").format(ro_name),
				frappe.ValidationError,
			)
		server_amount = flt(ro_row.get("deposit_request_amount"), 2)
		if server_amount <= 0:
			frappe.throw(
				_("Deposit request amount for Repair Order {0} must be greater than zero.").format(ro_name),
				frappe.ValidationError,
			)
		amount_cents = cad_to_cents(server_amount)
	else:
		if ro_row.get("status") == "Cancelled":
			frappe.throw(
				_("Cannot pay invoice balance for Cancelled Repair Order {0}.").format(ro_name),
				frappe.ValidationError,
			)
		linked_si = _clean_str(ro_row.get("sales_invoice"))
		if not linked_si:
			frappe.throw(
				_("No Sales Invoice is linked to Repair Order {0}.").format(ro_name),
				frappe.ValidationError,
			)
		si_row = _lock_si_row(linked_si)
		if not si_row or cint(si_row.get("docstatus")) != 1:
			frappe.throw(
				_("Sales Invoice {0} must be submitted before online balance payment.").format(linked_si),
				frappe.ValidationError,
			)
		assert_cad_currency(si_row.get("currency"), context=f"Sales Invoice {linked_si}")
		si_company = _clean_str(si_row.get("company"))
		if si_company and si_company != company:
			resolve_square_accounting_config(si_company)
			company = si_company
		server_amount = flt(si_row.get("outstanding_amount"), 2)
		if server_amount <= 0:
			frappe.throw(
				_("Sales Invoice {0} has no remaining balance due.").format(linked_si),
				frappe.ValidationError,
			)
		amount_cents = cad_to_cents(server_amount)

	# Stale-UI guard
	if expected_amount_cents is not None and int(expected_amount_cents) != amount_cents:
		frappe.throw(
			_("Payable amount has changed; please refresh before paying."),
			frappe.ValidationError,
		)

	# Reject concurrent duplicate charge attempt for same (repair_order, payment_intent)
	in_flight = frappe.db.get_value(
		"ISM Payment Transaction",
		{
			"repair_order": ro_name,
			"payment_intent": intent,
			"status": ["in", list(IN_FLIGHT_TX_STATUSES)],
		},
		"name",
	)
	if in_flight:
		frappe.throw(
			_(
				"A payment ({0}) is already in progress for Repair Order {1}. "
				"Please wait for it to finish before submitting another payment."
			).format(in_flight, ro_name),
			frappe.ValidationError,
		)

	tx = frappe.new_doc("ISM Payment Transaction")
	tx.repair_order = ro_name
	tx.sales_invoice = linked_si
	tx.customer = ro_row.get("customer")
	tx.company = company
	tx.payment_intent = intent
	tx.status = TX_STATUS_INITIATED
	tx.currency = SQUARE_CURRENCY
	tx.amount = cents_to_cad(amount_cents)
	tx.amount_cents = amount_cents
	tx.refunded_amount = 0.0
	tx.square_environment = square_env
	tx.idempotency_key = idem_key
	tx.square_payment_id = None
	tx.erpnext_sync_status = _SYNC_STATUS_NOT_POSTED
	tx.initiated_by = (_clean_str(initiated_by) or "Customer Portal")[:140]
	tx.client_ip = (_clean_str(client_ip) or "")[:64] or None
	tx.append_audit_entry(
		source="sync_api",
		event_type="payment.initiated",
		summary=f"Initiated {intent} for {ro_name} ({amount_cents} cents {SQUARE_CURRENCY})",
		status_before=None,
		status_after=TX_STATUS_INITIATED,
	)

	try:
		tx.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback()
		winner_name = frappe.db.get_value(
			"ISM Payment Transaction",
			{"idempotency_key": idem_key},
			"name",
		)
		if not winner_name:
			raise
		winner = frappe.get_doc("ISM Payment Transaction", winner_name)
		if winner.repair_order != ro_name or winner.payment_intent != intent:
			frappe.throw(
				_("Idempotency key has already been used for a different Repair Order or payment intent."),
				frappe.ValidationError,
			)
		return _build_charge_result(winner, idempotent_replay=True)

	# Commit BEFORE outbound HTTP call so zero DB locks are held over network I/O
	frappe.db.commit()

	# Step 2: External HTTPS charge (zero DB locks held)
	if intent == INTENT_MANUAL_DEPOSIT:
		note = f"ISM Repair Order {ro_name} Deposit ({tx.name})"
	else:
		note = f"ISM Repair Order {ro_name} Invoice {linked_si} ({tx.name})"

	try:
		raw_response = client.create_payment(
			source_id=clean_source_id,
			amount_cents=int(tx.amount_cents),
			idempotency_key=tx.idempotency_key,
			reference_id=tx.name,
			note=note,
			buyer_email_address=_clean_str(buyer_email),
		)
	except requests.RequestException as exc:
		_lock_tx_row(tx.name)
		tx_doc = frappe.get_doc("ISM Payment Transaction", tx.name)
		if tx_doc.payment_entry or tx_doc.status == TX_STATUS_POSTED:
			return _build_charge_result(tx_doc)
		status_before = tx_doc.status
		err_msg = _sanitize_error_message(f"Square gateway timeout/network error: {exc}")
		tx_doc.status = TX_STATUS_PENDING
		tx_doc.error_message = err_msg
		tx_doc.append_audit_entry(
			source="sync_api",
			event_type="payment.network_timeout",
			summary=err_msg,
			status_before=status_before,
			status_after=TX_STATUS_PENDING,
		)
		tx_doc.save(ignore_permissions=True)
		frappe.db.commit()
		return _build_charge_result(tx_doc)
	except Exception as exc:
		if _is_square_api_error(exc):
			clean_err = _format_square_api_error(exc)
			_lock_tx_row(tx.name)
			tx_doc = frappe.get_doc("ISM Payment Transaction", tx.name)
			if not tx_doc.payment_entry and tx_doc.status != TX_STATUS_POSTED:
				status_before = tx_doc.status
				tx_doc.status = TX_STATUS_FAILED
				tx_doc.square_status = "FAILED"
				tx_doc.error_message = clean_err
				tx_doc.append_audit_entry(
					source="sync_api",
					event_type="payment.failed",
					summary=clean_err,
					status_before=status_before,
					status_after=TX_STATUS_FAILED,
				)
				tx_doc.save(ignore_permissions=True)
				frappe.db.commit()
			frappe.throw(clean_err, frappe.ValidationError)
		raise

	# Step 3: Row-locked reconciliation and immediate ERPNext Bank Account posting
	payment_payload = _unwrap_square_payment(raw_response)
	return reconcile_and_post_square_payment(tx.name, payment_payload, source="sync_api")


def reconcile_and_post_square_payment(
	tx_name: str,
	square_payment: dict[str, Any],
	*,
	source: str = "sync_api",
	webhook_event_id: str | None = None,
	webhook_event_type: str | None = None,
) -> SquareChargeResult:
	"""Idempotent row-locked reconciliation & immediate ERPNext Square Bank Account posting.

	Canonical MariaDB FOR UPDATE lock order:
	  1. ISM Payment Transaction
	  2. Repair Order
	  3. Sales Invoice (if linked)
	"""
	payment = _unwrap_square_payment(square_payment)

	# 1. Lock ISM Payment Transaction
	tx_row = _lock_tx_row(tx_name)
	if not tx_row:
		frappe.throw(
			_("ISM Payment Transaction {0} not found.").format(tx_name),
			frappe.DoesNotExistError,
		)

	# 2. Lock Repair Order
	ro_name = tx_row["repair_order"]
	ro_row = _lock_ro_row(ro_name)
	if not ro_row:
		frappe.throw(_("Repair Order {0} not found.").format(ro_name), frappe.DoesNotExistError)

	# 3. Lock Sales Invoice if linked
	si_name = _clean_str(tx_row.get("sales_invoice"))
	if not si_name and tx_row.get("payment_intent") == INTENT_INVOICE_BALANCE:
		si_name = _clean_str(ro_row.get("sales_invoice"))
	si_row = _lock_si_row(si_name) if si_name else None

	tx = frappe.get_doc("ISM Payment Transaction", tx_name)

	# Deduplicate webhook event_id
	if _record_webhook_event_id(tx, webhook_event_id, webhook_event_type):
		return _build_charge_result(tx, idempotent_replay=True)

	# Backfill non-sensitive Square metadata
	sq_payment_id = _clean_str(payment.get("id") or payment.get("square_payment_id"))
	sq_status = _clean_str(payment.get("status") or payment.get("payment_status"))
	sq_status_upper = (sq_status or "").upper()
	sq_order_id = _clean_str(payment.get("order_id") or payment.get("square_order_id"))
	sq_location_id = _clean_str(payment.get("location_id") or payment.get("square_location_id"))
	sq_receipt_url = _clean_str(payment.get("receipt_url") or payment.get("square_receipt_url"))
	sq_receipt_number = _clean_str(payment.get("receipt_number") or payment.get("square_receipt_number"))

	card_details = payment.get("card_details") if isinstance(payment.get("card_details"), dict) else {}
	card = card_details.get("card") if isinstance(card_details.get("card"), dict) else {}
	card_brand = _clean_str(card.get("card_brand") or payment.get("card_brand"))
	card_last4 = _clean_str(card.get("last_4") or payment.get("card_last4"))

	amount_money = payment.get("amount_money") if isinstance(payment.get("amount_money"), dict) else {}
	raw_paid_cents = (
		amount_money.get("amount")
		if amount_money.get("amount") is not None
		else payment.get("amount_cents")
	)
	paid_currency = _clean_str(amount_money.get("currency") or payment.get("currency"))

	if sq_payment_id and not tx.square_payment_id:
		tx.square_payment_id = sq_payment_id
	if sq_status_upper:
		tx.square_status = sq_status_upper
	if sq_order_id:
		tx.square_order_id = sq_order_id
	if sq_location_id:
		tx.square_location_id = sq_location_id
	if sq_receipt_url:
		tx.square_receipt_url = sq_receipt_url
	if sq_receipt_number:
		tx.square_receipt_number = sq_receipt_number
	if card_brand:
		tx.card_brand = card_brand
	if card_last4 and _CARD_LAST4_RE.fullmatch(card_last4):
		tx.card_last4 = card_last4

	status_before = tx.status
	effective_sq_id = tx.square_payment_id or sq_payment_id or tx.name

	if sq_status_upper == "COMPLETED":
		# At-most-once posting short-circuit
		if tx.payment_entry or tx.status == TX_STATUS_POSTED:
			tx.append_audit_entry(
				source=source,
				event_type=webhook_event_type or "payment.completed.duplicate",
				summary=f"Duplicate COMPLETED reconciliation ignored; Payment Entry {tx.payment_entry} already posted.",
				event_id=webhook_event_id,
				status_before=status_before,
				status_after=tx.status,
			)
			tx.save(ignore_permissions=True)
			frappe.db.commit()
			return _build_charge_result(tx, idempotent_replay=True)

		# Strict CAD & amount_cents verification before posting
		currency_ok = (paid_currency or "").upper() == SQUARE_CURRENCY
		amount_ok = (
			raw_paid_cents is not None
			and not isinstance(raw_paid_cents, bool)
			and int(raw_paid_cents) == int(tx.amount_cents)
		)
		if not (currency_ok and amount_ok):
			mismatch_msg = (
				f"Square payment mismatch for {tx.name} ({effective_sq_id}): "
				f"expected {int(tx.amount_cents)} {SQUARE_CURRENCY}, "
				f"received {raw_paid_cents!r} {paid_currency!r}. Payment Entry NOT posted."
			)
			tx.status = TX_STATUS_RECONCILIATION_ERROR
			tx.erpnext_sync_status = _SYNC_STATUS_MANUAL_REVIEW
			tx.error_message = mismatch_msg
			tx.append_audit_entry(
				source=source,
				event_type=webhook_event_type or "payment.amount_mismatch",
				summary=mismatch_msg,
				event_id=webhook_event_id,
				status_before=status_before,
				status_after=TX_STATUS_RECONCILIATION_ERROR,
			)
			tx.save(ignore_permissions=True)
			_emit_payment_alert(
				ro_name=tx.repair_order,
				tx_name=tx.name,
				square_payment_id=tx.square_payment_id,
				kind="amount_mismatch",
				title=f"Square Payment Mismatch on {tx.repair_order}",
				priority="Critical",
				details=mismatch_msg,
				payment_intent=tx.payment_intent,
				tx_status=tx.status,
				amount=tx.amount,
				payment_entry=None,
			)
			frappe.db.commit()
			return _build_charge_result(tx)

		company = tx.company or _clean_str(ro_row.get("company"))
		acct_cfg = resolve_square_accounting_config(company)
		amount = cents_to_cad(int(tx.amount_cents))

		manual_review_needed = False
		overpayment_msg: str | None = None

		if tx.payment_intent == INTENT_INVOICE_BALANCE:
			if not si_row or cint(si_row.get("docstatus")) != 1:
				err_msg = (
					f"Square payment {effective_sq_id} ({amount:.2f} {SQUARE_CURRENCY}) completed, "
					f"but Sales Invoice {si_name or '(missing)'} is no longer submitted."
				)
				tx.status = TX_STATUS_RECONCILIATION_ERROR
				tx.erpnext_sync_status = _SYNC_STATUS_MANUAL_REVIEW
				tx.error_message = err_msg
				tx.append_audit_entry(
					source=source,
					event_type=webhook_event_type or "payment.reconciliation_error",
					summary=err_msg,
					event_id=webhook_event_id,
					status_before=status_before,
					status_after=TX_STATUS_RECONCILIATION_ERROR,
				)
				tx.save(ignore_permissions=True)
				_emit_payment_alert(
					ro_name=tx.repair_order,
					tx_name=tx.name,
					square_payment_id=tx.square_payment_id,
					kind="invoice_reconciliation_error",
					title=f"Square Invoice Payment Reconciliation Error ({tx.repair_order})",
					priority="Critical",
					details=err_msg,
					payment_intent=tx.payment_intent,
					tx_status=tx.status,
					amount=tx.amount,
					payment_entry=None,
				)
				frappe.db.commit()
				return _build_charge_result(tx)

			si_outstanding = flt(si_row.get("outstanding_amount"), 2)
			if si_outstanding <= 0:
				err_msg = (
					f"Concurrent counter payment detected: Sales Invoice {si_row['name']} already has "
					f"$0.00 outstanding when Square payment {effective_sq_id} ({amount:.2f} {SQUARE_CURRENCY}) "
					"settled. Manager refund reconciliation required."
				)
				tx.status = TX_STATUS_RECONCILIATION_ERROR
				tx.erpnext_sync_status = _SYNC_STATUS_MANUAL_REVIEW
				tx.error_message = err_msg
				tx.append_audit_entry(
					source=source,
					event_type=webhook_event_type or "payment.overpayment_zero_outstanding",
					summary=err_msg,
					event_id=webhook_event_id,
					status_before=status_before,
					status_after=TX_STATUS_RECONCILIATION_ERROR,
				)
				tx.save(ignore_permissions=True)
				_emit_payment_alert(
					ro_name=tx.repair_order,
					tx_name=tx.name,
					square_payment_id=tx.square_payment_id,
					kind="overpayment_review",
					title=f"Square Overpayment Requires Refund ({tx.repair_order})",
					priority="Critical",
					details=err_msg,
					payment_intent=tx.payment_intent,
					tx_status=tx.status,
					amount=tx.amount,
					payment_entry=None,
				)
				frappe.db.commit()
				return _build_charge_result(tx)

		# Build & submit ERPNext Payment Entry (Guest-safe)
		pe = frappe.new_doc("Payment Entry")
		pe.flags.ignore_permissions = True
		pe.flags.skip_ro_payment_sync = False
		pe.payment_type = "Receive"
		pe.party_type = "Customer"
		pe.party = tx.customer or ro_row.get("customer")
		pe.company = acct_cfg["company"]
		pe.posting_date = nowdate()
		pe.mode_of_payment = acct_cfg["mode_of_payment"]
		pe.paid_to = acct_cfg["bank_account"]
		pe.paid_amount = flt(amount, 2)
		pe.received_amount = flt(amount, 2)
		pe.reference_no = effective_sq_id
		pe.reference_date = nowdate()
		pe.custom_remarks = 1

		if tx.payment_intent == INTENT_MANUAL_DEPOSIT:
			pe.remarks = build_square_deposit_remark(tx.repair_order, effective_sq_id)
		else:
			si_outstanding = flt(si_row["outstanding_amount"], 2)
			si_grand_total = flt(si_row["grand_total"], 2)
			allocated = min(flt(amount, 2), si_outstanding)
			pe.remarks = build_square_invoice_remark(tx.repair_order, si_row["name"], effective_sq_id)
			pe.append(
				"references",
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si_row["name"],
					"total_amount": si_grand_total,
					"outstanding_amount": si_outstanding,
					"allocated_amount": allocated,
				},
			)
			if flt(amount, 2) > si_outstanding + _AMOUNT_TOLERANCE:
				excess = flt(amount - si_outstanding, 2)
				manual_review_needed = True
				overpayment_msg = (
					f"Concurrent counter payment race on {si_row['name']}: Square collected "
					f"{amount:.2f} {SQUARE_CURRENCY}, allocated {allocated:.2f} {SQUARE_CURRENCY}, "
					f"leaving {excess:.2f} {SQUARE_CURRENCY} unallocated. Manager refund review required."
				)

		with _posting_user_context():
			pe.insert(ignore_permissions=True)
			pe.submit()

		tx.payment_entry = pe.name
		tx.paid_to_account = acct_cfg["bank_account"]
		tx.posted_at = now_datetime()
		tx.status = TX_STATUS_POSTED
		tx.erpnext_sync_status = (
			_SYNC_STATUS_MANUAL_REVIEW if manual_review_needed else _SYNC_STATUS_POSTED
		)
		tx.error_message = overpayment_msg
		tx.append_audit_entry(
			source=source,
			event_type=webhook_event_type or "payment.posted",
			summary=(
				f"Posted ERPNext Payment Entry {pe.name} ({amount:.2f} {SQUARE_CURRENCY}) "
				f"to {acct_cfg['bank_account']}."
			),
			event_id=webhook_event_id,
			status_before=status_before,
			status_after=TX_STATUS_POSTED,
		)
		tx.save(ignore_permissions=True)

		if tx.payment_intent == INTENT_MANUAL_DEPOSIT:
			frappe.db.set_value(
				"Repair Order",
				tx.repair_order,
				{
					"deposit_request_status": DEPOSIT_REQ_PAID,
					"deposit_request_paid_at": tx.posted_at,
					"deposit_request_transaction": tx.name,
				},
			)

		if manual_review_needed and overpayment_msg:
			_emit_payment_alert(
				ro_name=tx.repair_order,
				tx_name=tx.name,
				square_payment_id=tx.square_payment_id,
				kind="overpayment_review",
				title=f"Square Overpayment Requires Refund ({tx.repair_order})",
				priority="Critical",
				details=f"{overpayment_msg} Payment Entry: {pe.name}.",
				payment_intent=tx.payment_intent,
				tx_status=tx.status,
				amount=tx.amount,
				payment_entry=pe.name,
			)
		else:
			card_hint = (
				f" ({tx.card_brand} **** {tx.card_last4})"
				if tx.card_brand and tx.card_last4
				else ""
			)
			_emit_payment_alert(
				ro_name=tx.repair_order,
				tx_name=tx.name,
				square_payment_id=tx.square_payment_id,
				kind="payment_received",
				title=f"Square {tx.payment_intent} Received for {tx.repair_order}",
				priority="Medium",
				details=(
					f"Received ${amount:.2f} {SQUARE_CURRENCY}{card_hint} via Square ({effective_sq_id}). "
					f"Posted ERPNext Payment Entry {pe.name} to {acct_cfg['bank_account']}."
				),
				payment_intent=tx.payment_intent,
				tx_status=tx.status,
				amount=tx.amount,
				payment_entry=pe.name,
			)

		frappe.db.commit()
		return _build_charge_result(tx)

	if sq_status_upper in ("PENDING", "APPROVED"):
		if tx.payment_entry or tx.status == TX_STATUS_POSTED:
			tx.append_audit_entry(
				source=source,
				event_type=webhook_event_type or "payment.pending.ignored",
				summary=f"Ignored {sq_status_upper} update because Payment Entry {tx.payment_entry} is already posted.",
				event_id=webhook_event_id,
				status_before=status_before,
				status_after=tx.status,
			)
			tx.save(ignore_permissions=True)
			frappe.db.commit()
			return _build_charge_result(tx, idempotent_replay=True)

		tx.status = TX_STATUS_PENDING
		tx.append_audit_entry(
			source=source,
			event_type=webhook_event_type or "payment.pending",
			summary=f"Square payment {effective_sq_id} is {sq_status_upper}.",
			event_id=webhook_event_id,
			status_before=status_before,
			status_after=TX_STATUS_PENDING,
		)
		tx.save(ignore_permissions=True)
		frappe.db.commit()
		return _build_charge_result(tx)

	if sq_status_upper in ("CANCELED", "FAILED"):
		if tx.payment_entry:
			# NEVER cancel submitted Payment Entry / Sales Invoice from webhook
			post_fail_msg = (
				f"Square payment {effective_sq_id} transitioned to {sq_status_upper} after "
				f"ERPNext Payment Entry {tx.payment_entry} was already submitted. "
				"Submitted accounting records were preserved; manual reconciliation required."
			)
			tx.erpnext_sync_status = _SYNC_STATUS_MANUAL_REVIEW
			tx.error_message = post_fail_msg
			tx.append_audit_entry(
				source=source,
				event_type=webhook_event_type or f"payment.{sq_status_upper.lower()}_after_post",
				summary=post_fail_msg,
				event_id=webhook_event_id,
				status_before=status_before,
				status_after=tx.status,
			)
			tx.save(ignore_permissions=True)
			_emit_payment_alert(
				ro_name=tx.repair_order,
				tx_name=tx.name,
				square_payment_id=tx.square_payment_id,
				kind="post_settlement_void_or_fail",
				title=f"Square Payment {sq_status_upper.title()} After ERPNext Posting ({tx.repair_order})",
				priority="Critical",
				details=post_fail_msg,
				payment_intent=tx.payment_intent,
				tx_status=tx.status,
				amount=tx.amount,
				payment_entry=tx.payment_entry,
			)
			frappe.db.commit()
			return _build_charge_result(tx)

		target_status = TX_STATUS_VOIDED if sq_status_upper == "CANCELED" else TX_STATUS_FAILED
		msg = f"Square payment {effective_sq_id} {sq_status_upper.lower()}."
		tx.status = target_status
		tx.error_message = msg
		tx.append_audit_entry(
			source=source,
			event_type=webhook_event_type or f"payment.{sq_status_upper.lower()}",
			summary=msg,
			event_id=webhook_event_id,
			status_before=status_before,
			status_after=target_status,
		)
		tx.save(ignore_permissions=True)
		frappe.db.commit()
		return _build_charge_result(tx)

	tx.append_audit_entry(
		source=source,
		event_type=webhook_event_type or "payment.status_observed",
		summary=f"Observed Square status {sq_status_upper or 'UNKNOWN'} for {effective_sq_id}.",
		event_id=webhook_event_id,
		status_before=status_before,
		status_after=tx.status,
	)
	tx.save(ignore_permissions=True)
	frappe.db.commit()
	return _build_charge_result(tx)


def _handle_refund_webhook_event(tx_name: str, event: WebhookEventNormalized) -> dict[str, Any]:
	_lock_tx_row(tx_name)
	tx = frappe.get_doc("ISM Payment Transaction", tx_name)

	event_id = _clean_str(event.get("event_id"))
	event_type = _clean_str(event.get("event_type")) or "refund.updated"
	if _record_webhook_event_id(tx, event_id, event_type):
		return {
			"status": "duplicate_ignored",
			"event_id": event_id,
			"event_type": event_type,
			"transaction": tx.name,
		}

	refund_id = _clean_str(event.get("refund_id"))
	refund_status = (_clean_str(event.get("refund_status")) or "UNKNOWN").upper()
	raw_refund_cents = event.get("refund_amount_cents")
	refund_cents = (
		int(raw_refund_cents)
		if raw_refund_cents is not None and not isinstance(raw_refund_cents, bool) and int(raw_refund_cents) >= 0
		else 0
	)

	refunds = [r for r in _load_json_list(tx.refund_ids_json) if isinstance(r, dict)]
	if refund_id:
		updated = False
		for entry in refunds:
			if entry.get("refund_id") == refund_id:
				entry["amount_cents"] = refund_cents
				entry["status"] = refund_status
				entry["updated_at"] = str(now_datetime())
				updated = True
				break
		if not updated:
			refunds.append({
				"refund_id": refund_id,
				"amount_cents": refund_cents,
				"status": refund_status,
				"updated_at": str(now_datetime()),
			})
	tx.refund_ids_json = json.dumps(refunds)

	completed_refund_cents = sum(
		int(r.get("amount_cents") or 0)
		for r in refunds
		if str(r.get("status") or "").upper() == "COMPLETED"
	)
	tx.refunded_amount = cents_to_cad(completed_refund_cents)

	status_before = tx.status
	if completed_refund_cents > 0:
		if completed_refund_cents >= int(tx.amount_cents):
			tx.status = TX_STATUS_REFUNDED
		else:
			tx.status = TX_STATUS_PARTIALLY_REFUNDED

	# NEVER cancel submitted Payment Entry / Sales Invoice; flag for manager reconciliation
	tx.erpnext_sync_status = _SYNC_STATUS_MANUAL_REVIEW
	summary = (
		f"Square refund {refund_id or '(unknown)'} ({refund_status}) for "
		f"${cents_to_cad(refund_cents):.2f} {SQUARE_CURRENCY}. "
		f"Total completed refunds: ${flt(tx.refunded_amount, 2):.2f} / ${flt(tx.amount, 2):.2f} {SQUARE_CURRENCY}. "
		f"ERPNext Payment Entry {tx.payment_entry or 'N/A'} was NOT auto-cancelled."
	)
	tx.append_audit_entry(
		source="webhook",
		event_type=event_type,
		summary=summary,
		event_id=event_id,
		status_before=status_before,
		status_after=tx.status,
	)
	tx.save(ignore_permissions=True)

	priority = "Critical" if tx.status == TX_STATUS_REFUNDED else "High"
	_emit_payment_alert(
		ro_name=tx.repair_order,
		tx_name=tx.name,
		square_payment_id=tx.square_payment_id,
		kind="refund",
		title=f"Square Refund ({tx.status}) Requires Accounting Review ({tx.repair_order})",
		priority=priority,
		details=summary,
		payment_intent=tx.payment_intent,
		tx_status=tx.status,
		amount=tx.amount,
		payment_entry=tx.payment_entry or None,
	)
	frappe.db.commit()

	return {
		"status": "processed",
		"event_id": event_id,
		"event_type": event_type,
		"transaction": tx.name,
		"tx_status": tx.status,
		"refunded_amount": flt(tx.refunded_amount, 2),
		"erpnext_sync_status": tx.erpnext_sync_status,
	}


def _handle_dispute_webhook_event(tx_name: str, event: WebhookEventNormalized) -> dict[str, Any]:
	_lock_tx_row(tx_name)
	tx = frappe.get_doc("ISM Payment Transaction", tx_name)

	event_id = _clean_str(event.get("event_id"))
	event_type = _clean_str(event.get("event_type")) or "dispute.created"
	if _record_webhook_event_id(tx, event_id, event_type):
		return {
			"status": "duplicate_ignored",
			"event_id": event_id,
			"event_type": event_type,
			"transaction": tx.name,
		}

	dispute_id = _clean_str(event.get("dispute_id")) or "unknown"
	dispute_state = (_clean_str(event.get("dispute_state")) or "UNKNOWN").upper()
	status_before = tx.status
	tx.erpnext_sync_status = _SYNC_STATUS_MANUAL_REVIEW

	summary = (
		f"Square dispute {dispute_id} (state: {dispute_state}) reported on payment "
		f"{tx.square_payment_id or tx.name} (${flt(tx.amount, 2):.2f} {SQUARE_CURRENCY}). "
		f"ERPNext Payment Entry {tx.payment_entry or 'N/A'} was preserved for manager review."
	)
	tx.append_audit_entry(
		source="webhook",
		event_type=event_type,
		summary=summary,
		event_id=event_id,
		status_before=status_before,
		status_after=tx.status,
	)
	tx.save(ignore_permissions=True)

	_emit_payment_alert(
		ro_name=tx.repair_order,
		tx_name=tx.name,
		square_payment_id=tx.square_payment_id,
		kind="dispute",
		title=f"Square Payment Dispute ({dispute_state}) on {tx.repair_order}",
		priority="Critical",
		details=summary,
		payment_intent=tx.payment_intent,
		tx_status=tx.status,
		amount=tx.amount,
		payment_entry=tx.payment_entry or None,
	)
	frappe.db.commit()

	return {
		"status": "processed",
		"event_id": event_id,
		"event_type": event_type,
		"transaction": tx.name,
		"dispute_id": dispute_id,
		"dispute_state": dispute_state,
		"erpnext_sync_status": tx.erpnext_sync_status,
	}


def process_square_webhook_event(event: WebhookEventNormalized) -> dict[str, Any]:
	"""Dispatch a verified, normalized Square webhook event to the row-locked ledger."""
	if not isinstance(event, dict) or event.get("ignored"):
		return {
			"status": "ignored",
			"reason": "unsupported_event",
			"event_id": event.get("event_id") if isinstance(event, dict) else None,
			"event_type": event.get("event_type") if isinstance(event, dict) else None,
		}

	event_id = _clean_str(event.get("event_id"))
	event_type = _clean_str(event.get("event_type")) or ""
	sq_payment_id = _clean_str(event.get("square_payment_id"))
	reference_id = _clean_str(event.get("reference_id"))

	tx_name: str | None = None
	if sq_payment_id:
		tx_name = frappe.db.get_value(
			"ISM Payment Transaction",
			{"square_payment_id": sq_payment_id},
			"name",
		)
	if not tx_name and reference_id and frappe.db.exists("ISM Payment Transaction", reference_id):
		tx_name = reference_id

	if not tx_name:
		return {
			"status": "ignored",
			"reason": "untracked_payment",
			"event_id": event_id,
			"event_type": event_type,
		}

	if event_type.startswith("payment."):
		square_payment: dict[str, Any] = {
			"id": sq_payment_id,
			"status": event.get("payment_status"),
			"reference_id": reference_id,
			"amount_money": {
				"amount": event.get("amount_cents"),
				"currency": event.get("currency"),
			},
			"receipt_url": event.get("receipt_url"),
			"receipt_number": event.get("receipt_number"),
			"card_brand": event.get("card_brand"),
			"card_last4": event.get("card_last4"),
		}
		result = reconcile_and_post_square_payment(
			tx_name,
			square_payment,
			source="webhook",
			webhook_event_id=event_id,
			webhook_event_type=event_type,
		)
		return {
			"status": "duplicate_ignored" if result.get("idempotent_replay") else "processed",
			"event_id": event_id,
			"event_type": event_type,
			"result": result,
		}

	if event_type.startswith("refund."):
		return _handle_refund_webhook_event(tx_name, event)

	if event_type.startswith("dispute."):
		return _handle_dispute_webhook_event(tx_name, event)

	return {
		"status": "ignored",
		"reason": "unsupported_event",
		"event_id": event_id,
		"event_type": event_type,
	}
