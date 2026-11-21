# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from ism.payments.contracts import (
	SQUARE_CURRENCY,
	SQUARE_IDEMPOTENCY_KEY_MAX_LEN,
	assert_cad_currency,
	cad_to_cents,
)

MAX_AUDIT_TRACE_ENTRIES = 50
_CARD_LAST4_RE = re.compile(r"^\d{4}$")

_SECRET_PATTERNS = (
	# Bearer tokens and Authorization headers
	(re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']+"), r"\1[REDACTED]"),
	(re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"), r"\1[REDACTED]"),
	# Square access / sandbox / production tokens
	(re.compile(r"\b(?:EAAAl|EAAAE|EAAA|sq0atp-|sq0csp-)[A-Za-z0-9_\-]{6,}\b"), "[REDACTED]"),
	# Key-value secret fields
	(
		re.compile(
			r"(?i)\b(access_token|square_access_token|webhook_signature_key|square_webhook_signature_key"
			r"|signature_key|source_id|api_key|secret|password|card_number|pan|cvv|cvc)\b"
			r"(\s*[:=]\s*[\"']?)([^\"'\s,;}\]]+)([\"']?)"
		),
		r"\1\2[REDACTED]\4",
	),
)


def _scrub_audit_text(text: Any, max_len: int = 500) -> str:
	raw = "" if text is None else str(text)
	scrubbed = raw
	for pattern, replacement in _SECRET_PATTERNS:
		scrubbed = pattern.sub(replacement, scrubbed)
	return scrubbed[:max_len]


class ISMPaymentTransaction(Document):
	def before_validate(self):
		if not self.currency:
			self.currency = SQUARE_CURRENCY

		# Keep empty square_payment_id as NULL so MariaDB UNIQUE index allows multiple Initiated rows.
		if isinstance(self.square_payment_id, str):
			stripped = self.square_payment_id.strip()
			self.square_payment_id = stripped or None

		if isinstance(self.idempotency_key, str):
			self.idempotency_key = self.idempotency_key.strip()

		if isinstance(self.card_last4, str):
			self.card_last4 = self.card_last4.strip() or None

		for json_field in ("processed_webhook_event_ids", "refund_ids_json", "webhook_audit_trace"):
			if not self.get(json_field):
				self.set(json_field, "[]")

	def validate(self):
		assert_cad_currency(self.currency, context="ISM Payment Transaction")

		expected_cents = cad_to_cents(self.amount)
		if self.amount_cents is None or self.amount_cents == 0:
			self.amount_cents = expected_cents
		elif int(self.amount_cents) != expected_cents:
			frappe.throw(
				_("Amount (Cents) ({0}) does not match CAD Amount ({1} -> {2} cents).").format(
					self.amount_cents, self.amount, expected_cents
				),
				frappe.ValidationError,
			)

		if not self.idempotency_key:
			frappe.throw(_("Idempotency Key is required."), frappe.ValidationError)
		if len(self.idempotency_key) > SQUARE_IDEMPOTENCY_KEY_MAX_LEN:
			frappe.throw(
				_("Idempotency Key cannot exceed {0} characters.").format(SQUARE_IDEMPOTENCY_KEY_MAX_LEN),
				frappe.ValidationError,
			)
		if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in self.idempotency_key):
			frappe.throw(
				_("Idempotency Key must not contain whitespace or control characters."),
				frappe.ValidationError,
			)

		if self.card_last4 and not _CARD_LAST4_RE.fullmatch(str(self.card_last4)):
			frappe.throw(
				_("Card Last 4 must be empty or exactly 4 digits."),
				frappe.ValidationError,
			)

	def on_trash(self):
		if not self.payment_entry:
			return
		allow_teardown = bool(
			getattr(frappe.flags, "in_test", False)
			or getattr(self.flags, "force", False)
			or getattr(frappe.flags, "force_delete", False)
		)
		if not allow_teardown:
			frappe.throw(
				_(
					"Cannot delete ISM Payment Transaction {0} because it is linked to ERPNext Payment Entry {1}."
				).format(self.name, self.payment_entry),
				frappe.ValidationError,
			)

	def append_audit_entry(
		self,
		source: str,
		event_type: str,
		summary: str,
		event_id: str | None = None,
		status_before: str | None = None,
		status_after: str | None = None,
	) -> dict[str, Any]:
		"""Append a secret-scrubbed audit entry to webhook_audit_trace, capped at 50 entries."""
		try:
			entries = json.loads(self.webhook_audit_trace or "[]")
			if not isinstance(entries, list):
				entries = []
		except (ValueError, TypeError):
			entries = []

		entry: dict[str, Any] = {
			"timestamp": str(now_datetime()),
			"source": _scrub_audit_text(source, max_len=64),
			"event_type": _scrub_audit_text(event_type, max_len=128),
			"event_id": _scrub_audit_text(event_id, max_len=128) if event_id is not None else None,
			"status_before": status_before,
			"status_after": status_after,
			"summary": _scrub_audit_text(summary, max_len=500),
		}
		entries.append(entry)
		if len(entries) > MAX_AUDIT_TRACE_ENTRIES:
			entries = entries[-MAX_AUDIT_TRACE_ENTRIES:]

		self.webhook_audit_trace = json.dumps(entries)
		return entry
