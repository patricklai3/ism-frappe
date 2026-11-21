# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frozen cross-wave contracts, constants, and pure validation helpers for ISM Square CAD Payments."""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, TypedDict

import frappe

# ---------------------------------------------------------------------------
# Frozen Constants
# ---------------------------------------------------------------------------

SQUARE_CURRENCY = "CAD"
SQUARE_API_VERSION = "2025-01-23"
SQUARE_IDEMPOTENCY_KEY_MAX_LEN = 45

SQUARE_ENV_DISABLED = "Disabled"
SQUARE_ENV_SANDBOX = "Sandbox"
SQUARE_ENV_PRODUCTION = "Production"
SQUARE_ENV_MOCK = "Mock (Deterministic Test)"
SQUARE_ENVIRONMENTS = (
	SQUARE_ENV_DISABLED,
	SQUARE_ENV_SANDBOX,
	SQUARE_ENV_PRODUCTION,
	SQUARE_ENV_MOCK,
)

SQUARE_API_BASE_URLS = {
	SQUARE_ENV_SANDBOX: "https://connect.squareupsandbox.com",
	SQUARE_ENV_PRODUCTION: "https://connect.squareup.com",
}

SQUARE_SDK_SCRIPT_URLS = {
	SQUARE_ENV_SANDBOX: "https://sandbox.web.squarecdn.com/v1/square.js",
	SQUARE_ENV_PRODUCTION: "https://web.squarecdn.com/v1/square.js",
}

SQUARE_DEFAULT_MOP = "Square"
SQUARE_DEFAULT_BANK_ACCOUNT_LABEL = "Square"

INTENT_MANUAL_DEPOSIT = "Manual Deposit"
INTENT_INVOICE_BALANCE = "Invoice Balance"
PAYMENT_INTENTS = (INTENT_MANUAL_DEPOSIT, INTENT_INVOICE_BALANCE)

TX_STATUS_INITIATED = "Initiated"
TX_STATUS_PENDING = "Pending"
TX_STATUS_COMPLETED = "Completed"
TX_STATUS_POSTED = "Posted"
TX_STATUS_FAILED = "Failed"
TX_STATUS_VOIDED = "Voided"
TX_STATUS_PARTIALLY_REFUNDED = "Partially Refunded"
TX_STATUS_REFUNDED = "Refunded"
TX_STATUS_RECONCILIATION_ERROR = "Reconciliation Error"

TX_STATUSES = (
	TX_STATUS_INITIATED,
	TX_STATUS_PENDING,
	TX_STATUS_COMPLETED,
	TX_STATUS_POSTED,
	TX_STATUS_FAILED,
	TX_STATUS_VOIDED,
	TX_STATUS_PARTIALLY_REFUNDED,
	TX_STATUS_REFUNDED,
	TX_STATUS_RECONCILIATION_ERROR,
)

IN_FLIGHT_TX_STATUSES = (TX_STATUS_INITIATED, TX_STATUS_PENDING)

DEPOSIT_REQ_NONE = "None"
DEPOSIT_REQ_REQUESTED = "Requested"
DEPOSIT_REQ_PAID = "Paid"
DEPOSIT_REQ_CANCELLED = "Cancelled"
DEPOSIT_REQUEST_STATUSES = (
	DEPOSIT_REQ_NONE,
	DEPOSIT_REQ_REQUESTED,
	DEPOSIT_REQ_PAID,
	DEPOSIT_REQ_CANCELLED,
)

STAFF_PAYMENT_ROLES = frozenset({"System Manager", "ISM Manager", "ISM Service Advisor"})

PROHIBITED_PCI_FIELD_NAMES = frozenset({
	"card_number",
	"pan",
	"cvv",
	"cvc",
	"security_code",
	"exp_month",
	"exp_year",
	"expiry",
	"expiration_date",
	"card_data",
})

WEBHOOK_SUPPORTED_EVENTS = frozenset({
	"payment.created",
	"payment.updated",
	"refund.created",
	"refund.updated",
	"dispute.created",
	"dispute.state.updated",
})

_CENT_QUANT = Decimal("0.01")


# ---------------------------------------------------------------------------
# TypedDict Contracts (Frozen at Checkpoint M1)
# ---------------------------------------------------------------------------


class SquareSdkBootConfig(TypedDict):
	enabled: bool
	environment: str
	mode: str
	application_id: str | None
	location_id: str | None
	currency: str
	sdk_url: str | None


class SquareAccountingConfig(TypedDict):
	company: str
	mode_of_payment: str
	bank_account: str
	currency: str


class PaymentStateSummary(TypedDict, total=False):
	repair_order: str
	currency: str
	payment_status: str
	grand_total: float
	deposit_amount: float
	paid_amount: float
	outstanding_amount: float
	sales_invoice: str | None
	invoice_submitted: bool
	invoice_grand_total: float
	invoice_outstanding_amount: float
	deposit_request: dict[str, Any]
	payable_intent: str | None
	payable_amount: float
	payable_amount_cents: int
	has_in_flight_payment: bool
	portal_payment_badge: str
	online_payments_enabled: bool
	transactions: list[dict[str, Any]]


class SquareChargeResult(TypedDict, total=False):
	transaction: str
	repair_order: str
	sales_invoice: str | None
	payment_intent: str
	status: str
	erpnext_sync_status: str
	amount: float
	amount_cents: int
	currency: str
	idempotency_key: str
	square_payment_id: str | None
	square_status: str | None
	payment_entry: str | None
	paid_to_account: str | None
	card_brand: str | None
	card_last4: str | None
	receipt_url: str | None
	receipt_number: str | None
	posted_at: str | None
	idempotent_replay: bool
	error_message: str | None


class WebhookEventNormalized(TypedDict, total=False):
	event_id: str
	event_type: str
	merchant_id: str | None
	created_at: str | None
	square_payment_id: str | None
	reference_id: str | None
	payment_status: str | None
	amount_cents: int | None
	currency: str | None
	receipt_url: str | None
	receipt_number: str | None
	card_brand: str | None
	card_last4: str | None
	refund_id: str | None
	refund_status: str | None
	refund_amount_cents: int | None
	dispute_id: str | None
	dispute_state: str | None
	sanitized_summary: str
	ignored: bool


# ---------------------------------------------------------------------------
# Pure Utility Functions
# ---------------------------------------------------------------------------


def cad_to_cents(amount: float | int | str | Decimal) -> int:
	"""Convert a positive CAD dollar amount to integer cents using Decimal ROUND_HALF_UP.

	Rejects booleans, None, NaN, Infinity, non-numeric values, and amounts <= 0.
	"""
	if isinstance(amount, bool) or amount is None:
		raise frappe.ValidationError("Payment amount must be a positive CAD number.")

	try:
		raw = amount.strip() if isinstance(amount, str) else str(amount)
		dec = Decimal(raw)
	except (InvalidOperation, ValueError, TypeError):
		raise frappe.ValidationError(f"Invalid CAD payment amount: {amount!r}.")

	if not dec.is_finite():
		raise frappe.ValidationError("Payment amount must be a finite CAD number.")

	if dec <= 0:
		raise frappe.ValidationError("Payment amount must be greater than zero.")

	quantized = dec.quantize(_CENT_QUANT, rounding=ROUND_HALF_UP)
	cents = int(quantized * 100)
	if cents <= 0:
		raise frappe.ValidationError("Payment amount must be at least $0.01 CAD.")
	return cents


def cents_to_cad(cents: int) -> float:
	"""Convert non-negative integer CAD cents to a 2-decimal CAD float."""
	if isinstance(cents, bool) or not isinstance(cents, int):
		raise frappe.ValidationError("Cents value must be an integer.")
	if cents < 0:
		raise frappe.ValidationError("Cents value cannot be negative.")
	return float((Decimal(cents) / Decimal(100)).quantize(_CENT_QUANT, rounding=ROUND_HALF_UP))


def normalize_idempotency_key(raw_key: str | None) -> str:
	"""Validate or generate a Square-compliant idempotency key (1..45 printable non-whitespace chars)."""
	if raw_key is None:
		return f"ism-{uuid.uuid4().hex}"

	if not isinstance(raw_key, str):
		raise frappe.ValidationError("Idempotency key must be a string.")

	key = raw_key.strip()
	if not key:
		return f"ism-{uuid.uuid4().hex}"

	if len(key) > SQUARE_IDEMPOTENCY_KEY_MAX_LEN:
		raise frappe.ValidationError(
			f"Idempotency key exceeds Square's {SQUARE_IDEMPOTENCY_KEY_MAX_LEN}-character limit."
		)

	if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in key):
		raise frappe.ValidationError("Idempotency key must not contain whitespace or control characters.")

	return key


def assert_cad_currency(currency: str | None, context: str = "Payment") -> str:
	"""Enforce CAD-only currency across Square payments, settings, and ERPNext accounts."""
	normalized = str(currency or "").strip().upper()
	if normalized != SQUARE_CURRENCY:
		raise frappe.ValidationError(
			f"{context} currency must be {SQUARE_CURRENCY} (received {currency!r})."
		)
	return SQUARE_CURRENCY


def _scan_pci_fields(obj: Any) -> None:
	if isinstance(obj, dict):
		for key, value in obj.items():
			normalized_key = str(key).strip().lower()
			if normalized_key in PROHIBITED_PCI_FIELD_NAMES:
				raise frappe.ValidationError(
					f"Prohibited raw card field '{key}' is not accepted by ISM. "
					"Card data must be tokenized client-side via Square Web Payments SDK."
				)
			_scan_pci_fields(value)
	elif isinstance(obj, list | tuple):
		for item in obj:
			_scan_pci_fields(item)


def assert_no_raw_card_fields(payload: dict[str, Any]) -> None:
	"""Recursively reject any payload containing raw PCI cardholder field names."""
	if payload is None:
		return
	if not isinstance(payload, dict):
		raise frappe.ValidationError("Payment payload must be a dictionary.")
	_scan_pci_fields(payload)


def build_square_deposit_remark(ro_name: str, square_payment_id: str) -> str:
	"""Build canonical ERPNext Payment Entry remark for Square RO advances (auto-discovered by billing.py)."""
	return f"Square online deposit for Repair Order {ro_name} [RO:{ro_name}] [SQ:{square_payment_id}]"


def build_square_invoice_remark(ro_name: str, si_name: str, square_payment_id: str) -> str:
	"""Build canonical ERPNext Payment Entry remark for Square Sales Invoice balance payments."""
	return f"Square online invoice payment for {si_name} [RO:{ro_name}] [SQ:{square_payment_id}]"
