# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Pure HTTPS Square Payments API v2 adapter and deterministic offline Mock harness."""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

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
	SQUARE_IDEMPOTENCY_KEY_MAX_LEN,
	SQUARE_SDK_SCRIPT_URLS,
	SquareSdkBootConfig,
	assert_cad_currency,
	normalize_idempotency_key,
)

SQUARE_REFERENCE_ID_MAX_LEN = 40
SQUARE_NOTE_MAX_LEN = 500
SQUARE_HTTP_TIMEOUT: tuple[int, int] = (5, 25)
_REDACTED = "[REDACTED]"

_ENV_TO_MODE = {
	SQUARE_ENV_DISABLED: "disabled",
	SQUARE_ENV_SANDBOX: "sandbox",
	SQUARE_ENV_PRODUCTION: "production",
	SQUARE_ENV_MOCK: "mock",
}

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _clean_optional_str(value: Any) -> str | None:
	if value is None:
		return None
	cleaned = str(value).strip()
	return cleaned or None


def _redact_text(text: str, secrets: tuple[str | None, ...] = ()) -> str:
	result = str(text or "")
	for secret in secrets:
		if secret:
			result = result.replace(secret, _REDACTED)
	return result


def get_square_settings_snapshot() -> dict[str, Any]:
	"""Read ISM Settings Square config (with get_password for secrets) for internal server use only."""
	settings = frappe.get_single("ISM Settings")
	environment = _clean_optional_str(settings.get("square_environment")) or SQUARE_ENV_DISABLED
	currency = (_clean_optional_str(settings.get("square_currency")) or SQUARE_CURRENCY).upper()
	access_token = _clean_optional_str(
		settings.get_password("square_access_token", raise_exception=False)
	)
	webhook_signature_key = _clean_optional_str(
		settings.get_password("square_webhook_signature_key", raise_exception=False)
	)

	return {
		"environment": environment,
		"application_id": _clean_optional_str(settings.get("square_application_id")),
		"location_id": _clean_optional_str(settings.get("square_location_id")),
		"currency": currency,
		"access_token": access_token,
		"webhook_signature_key": webhook_signature_key,
		"webhook_notification_url": _clean_optional_str(settings.get("square_webhook_notification_url")),
		"mode_of_payment": _clean_optional_str(settings.get("square_mode_of_payment")) or SQUARE_DEFAULT_MOP,
		"bank_account": _clean_optional_str(settings.get("square_bank_account")),
	}


def get_sdk_boot_config() -> SquareSdkBootConfig:
	"""Return strictly non-secret Square Web Payments SDK boot parameters for portal/frontend clients.

	Keys: enabled, environment, mode ('disabled'|'sandbox'|'production'|'mock'),
	application_id, location_id, currency ('CAD'), sdk_url.
	NEVER includes square_access_token or square_webhook_signature_key.
	"""
	snapshot = get_square_settings_snapshot()
	environment = snapshot["environment"]
	mode = _ENV_TO_MODE.get(environment, "disabled")
	application_id = snapshot["application_id"]
	location_id = snapshot["location_id"]

	if mode == "disabled":
		return SquareSdkBootConfig(
			enabled=False,
			environment=SQUARE_ENV_DISABLED,
			mode="disabled",
			application_id=None,
			location_id=None,
			currency=SQUARE_CURRENCY,
			sdk_url=None,
		)

	if mode == "mock":
		return SquareSdkBootConfig(
			enabled=True,
			environment=SQUARE_ENV_MOCK,
			mode="mock",
			application_id=application_id,
			location_id=location_id,
			currency=SQUARE_CURRENCY,
			sdk_url=None,
		)

	sdk_url = SQUARE_SDK_SCRIPT_URLS.get(environment)
	enabled = bool(application_id and location_id)
	return SquareSdkBootConfig(
		enabled=enabled,
		environment=environment,
		mode=mode,
		application_id=application_id,
		location_id=location_id,
		currency=SQUARE_CURRENCY,
		sdk_url=sdk_url,
	)


class SquareApiError(Exception):
	"""Structured Square gateway error with code, category, detail, status_code, and retryable flag."""

	def __init__(
		self,
		message: str | None = None,
		*,
		code: str = "UNKNOWN_ERROR",
		category: str = "API_ERROR",
		detail: str = "",
		status_code: int | None = None,
		retryable: bool = False,
	) -> None:
		self.code = str(code or "UNKNOWN_ERROR")
		self.category = str(category or "API_ERROR")
		self.detail = str(detail or "")
		self.status_code = status_code
		self.retryable = bool(retryable)
		resolved_message = message or self.detail or f"Square API error ({self.code})"
		super().__init__(resolved_message)


class SquareClient:
	"""Pure HTTPS requests adapter for Square Payments API v2 (CAD-only)."""

	def __init__(
		self,
		*,
		access_token: str,
		location_id: str,
		environment: str = SQUARE_ENV_SANDBOX,
		timeout: tuple[int, int] = SQUARE_HTTP_TIMEOUT,
		session: requests.Session | None = None,
	) -> None:
		if environment != SQUARE_ENV_MOCK:
			if environment not in SQUARE_API_BASE_URLS:
				raise frappe.ValidationError(f"Unsupported Square API environment: {environment!r}.")
			if not (access_token or "").strip():
				raise frappe.ValidationError("Square Access Token is required.")
			if not (location_id or "").strip():
				raise frappe.ValidationError("Square Location ID is required.")

		self.access_token = (access_token or "").strip()
		self.location_id = (location_id or "").strip()
		self.environment = environment
		self.timeout = timeout
		self._session = session
		self._base_url = SQUARE_API_BASE_URLS.get(environment, "")

	def _headers(self) -> dict[str, str]:
		return {
			"Authorization": f"Bearer {self.access_token}",
			"Square-Version": SQUARE_API_VERSION,
			"Content-Type": "application/json",
		}

	def _redacted_headers(self) -> dict[str, str]:
		return {
			"Authorization": f"Bearer {_REDACTED}",
			"Square-Version": SQUARE_API_VERSION,
			"Content-Type": "application/json",
		}

	def _validate_create_payment_args(
		self,
		*,
		source_id: str,
		amount_cents: int,
		idempotency_key: str,
		reference_id: str,
		note: str,
		buyer_email_address: str | None = None,
	) -> tuple[str, int, str, str, str, str | None]:
		if not isinstance(source_id, str) or not source_id.strip():
			raise frappe.ValidationError("Square payment token (source_id) is required.")
		clean_source_id = source_id.strip()

		if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
			raise frappe.ValidationError("Payment amount_cents must be a positive integer.")

		if not isinstance(idempotency_key, str) or not idempotency_key.strip():
			raise frappe.ValidationError("Idempotency key is required.")
		clean_idempotency_key = normalize_idempotency_key(idempotency_key)
		if len(clean_idempotency_key) > SQUARE_IDEMPOTENCY_KEY_MAX_LEN:
			raise frappe.ValidationError(
				f"Idempotency key exceeds Square's {SQUARE_IDEMPOTENCY_KEY_MAX_LEN}-character limit."
			)

		if not isinstance(reference_id, str) or not reference_id.strip():
			raise frappe.ValidationError("Reference ID is required.")
		clean_reference_id = reference_id.strip()
		if len(clean_reference_id) > SQUARE_REFERENCE_ID_MAX_LEN:
			raise frappe.ValidationError(
				f"Reference ID exceeds Square's {SQUARE_REFERENCE_ID_MAX_LEN}-character limit."
			)

		clean_note = str(note or "")[:SQUARE_NOTE_MAX_LEN]
		clean_email = _clean_optional_str(buyer_email_address)
		return (
			clean_source_id,
			amount_cents,
			clean_idempotency_key,
			clean_reference_id,
			clean_note,
			clean_email,
		)

	def _handle_response(
		self,
		response: requests.Response,
		*,
		redact_values: tuple[str | None, ...] = (),
	) -> dict[str, Any]:
		status_code = getattr(response, "status_code", None)
		try:
			body = response.json()
		except ValueError:
			raw_detail = _redact_text(getattr(response, "text", "")[:300], redact_values)
			retryable = status_code in _RETRYABLE_STATUS_CODES
			raise SquareApiError(
				f"Square returned non-JSON response (HTTP {status_code}).",
				code="INVALID_RESPONSE",
				category="API_ERROR",
				detail=raw_detail,
				status_code=status_code,
				retryable=retryable,
			)

		if not isinstance(body, dict):
			raise SquareApiError(
				f"Square returned unexpected JSON payload (HTTP {status_code}).",
				code="INVALID_RESPONSE",
				category="API_ERROR",
				detail="",
				status_code=status_code,
				retryable=status_code in _RETRYABLE_STATUS_CODES,
			)

		if status_code is not None and status_code >= 400:
			errors = body.get("errors")
			first_err = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
			code = str(first_err.get("code") or f"HTTP_{status_code}")
			category = str(first_err.get("category") or "API_ERROR")
			raw_detail = str(first_err.get("detail") or f"Square API request failed with HTTP {status_code}.")
			safe_detail = _redact_text(raw_detail, redact_values)
			retryable = status_code in _RETRYABLE_STATUS_CODES
			frappe.logger("ism.payments").warning(
				"Square API error status=%s code=%s category=%s detail=%s headers=%s",
				status_code,
				code,
				category,
				safe_detail,
				self._redacted_headers(),
			)
			raise SquareApiError(
				safe_detail,
				code=code,
				category=category,
				detail=safe_detail,
				status_code=status_code,
				retryable=retryable,
			)

		payment = body.get("payment")
		if not isinstance(payment, dict):
			raise SquareApiError(
				"Square response missing payment object.",
				code="INVALID_RESPONSE",
				category="API_ERROR",
				detail="Square response missing payment object.",
				status_code=status_code,
				retryable=False,
			)
		return payment

	def create_payment(
		self,
		*,
		source_id: str,
		amount_cents: int,
		idempotency_key: str,
		reference_id: str,
		note: str,
		buyer_email_address: str | None = None,
	) -> dict[str, Any]:
		"""Execute POST /v2/payments in CAD with autocomplete=True and explicit (5, 25) timeout."""
		(
			clean_source_id,
			clean_amount_cents,
			clean_idempotency_key,
			clean_reference_id,
			clean_note,
			clean_email,
		) = self._validate_create_payment_args(
			source_id=source_id,
			amount_cents=amount_cents,
			idempotency_key=idempotency_key,
			reference_id=reference_id,
			note=note,
			buyer_email_address=buyer_email_address,
		)

		payload: dict[str, Any] = {
			"source_id": clean_source_id,
			"idempotency_key": clean_idempotency_key,
			"amount_money": {
				"amount": clean_amount_cents,
				"currency": SQUARE_CURRENCY,
			},
			"location_id": self.location_id,
			"autocomplete": True,
			"reference_id": clean_reference_id,
			"note": clean_note,
		}
		if clean_email:
			payload["buyer_email_address"] = clean_email

		url = f"{self._base_url}/v2/payments"
		post_fn = self._session.post if self._session is not None else requests.post
		response = post_fn(
			url,
			json=payload,
			headers=self._headers(),
			timeout=self.timeout,
		)
		return self._handle_response(
			response,
			redact_values=(self.access_token, f"Bearer {self.access_token}", clean_source_id),
		)

	def get_payment(self, square_payment_id: str) -> dict[str, Any]:
		"""Fetch GET /v2/payments/{payment_id} for reconciliation/re-verification."""
		if not isinstance(square_payment_id, str) or not square_payment_id.strip():
			raise frappe.ValidationError("Square payment ID is required.")
		clean_id = square_payment_id.strip()
		quoted_id = quote(clean_id, safe="")
		url = f"{self._base_url}/v2/payments/{quoted_id}"
		get_fn = self._session.get if self._session is not None else requests.get
		response = get_fn(
			url,
			headers=self._headers(),
			timeout=self.timeout,
		)
		return self._handle_response(
			response,
			redact_values=(self.access_token, f"Bearer {self.access_token}"),
		)


class MockSquareClient(SquareClient):
	"""Deterministic offline Square adapter enabled when square_environment == 'Mock (Deterministic Test)'.

	Deterministic source_id fixtures:
	  - 'mock_tok_ok' (or any 'mock_tok_ok_*'): returns COMPLETED payment with VISA **** 4242, deterministic sq_mock_<hash16> ID
	  - 'mock_tok_pending': returns PENDING payment (settled later via webhook replay in tests)
	  - 'mock_tok_decline': raises SquareApiError(code='CARD_DECLINED', retryable=False, status_code=402)
	  - 'mock_tok_timeout': raises requests.Timeout (simulates network drop after/during charge)
	  - 'mock_tok_mismatch': returns COMPLETED payment with amount_money.amount = amount_cents - 100
	Idempotency memory: repeated create_payment calls with the same idempotency_key return the exact same Square payment payload.
	"""

	_shared_idempotency_store: dict[str, dict[str, Any]] = {}
	_shared_payment_store: dict[str, dict[str, Any]] = {}

	def __init__(
		self,
		*,
		location_id: str = "LMOCK00000001",
		isolated: bool = False,
	) -> None:
		super().__init__(
			access_token="mock_access_token",
			location_id=location_id or "LMOCK00000001",
			environment=SQUARE_ENV_MOCK,
		)
		if isolated:
			self._idempotency_store: dict[str, dict[str, Any]] = {}
			self._payment_store: dict[str, dict[str, Any]] = {}
		else:
			self._idempotency_store = self._shared_idempotency_store
			self._payment_store = self._shared_payment_store

	@classmethod
	def reset_store(cls) -> None:
		"""Clear shared mock idempotency and payment memory (for test isolation)."""
		cls._shared_idempotency_store.clear()
		cls._shared_payment_store.clear()

	def create_payment(
		self,
		*,
		source_id: str,
		amount_cents: int,
		idempotency_key: str,
		reference_id: str,
		note: str,
		buyer_email_address: str | None = None,
	) -> dict[str, Any]:
		(
			clean_source_id,
			clean_amount_cents,
			clean_idempotency_key,
			clean_reference_id,
			clean_note,
			clean_email,
		) = self._validate_create_payment_args(
			source_id=source_id,
			amount_cents=amount_cents,
			idempotency_key=idempotency_key,
			reference_id=reference_id,
			note=note,
			buyer_email_address=buyer_email_address,
		)

		if clean_idempotency_key in self._idempotency_store:
			return copy.deepcopy(self._idempotency_store[clean_idempotency_key])

		if clean_source_id == "mock_tok_decline":
			raise SquareApiError(
				"Card was declined.",
				code="CARD_DECLINED",
				category="PAYMENT_METHOD_ERROR",
				detail="Authorization error: 'CARD_DECLINED'",
				status_code=402,
				retryable=False,
			)

		if clean_source_id == "mock_tok_timeout":
			raise requests.Timeout("Mock Square API timeout")

		if clean_source_id == "mock_tok_ok" or clean_source_id.startswith("mock_tok_ok_"):
			status = "COMPLETED"
			reported_amount_cents = clean_amount_cents
		elif clean_source_id == "mock_tok_pending":
			status = "PENDING"
			reported_amount_cents = clean_amount_cents
		elif clean_source_id == "mock_tok_mismatch":
			status = "COMPLETED"
			reported_amount_cents = clean_amount_cents - 100
		else:
			raise SquareApiError(
				"Unsupported mock payment token.",
				code="INVALID_CARD",
				category="INVALID_REQUEST_ERROR",
				detail="Unsupported mock payment token.",
				status_code=400,
				retryable=False,
			)

		digest = hashlib.sha256(
			f"{clean_idempotency_key}:{clean_reference_id}:{clean_amount_cents}".encode()
		).hexdigest()[:16]
		payment_id = f"sq_mock_{digest}"
		timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

		payment: dict[str, Any] = {
			"id": payment_id,
			"status": status,
			"amount_money": {
				"amount": reported_amount_cents,
				"currency": SQUARE_CURRENCY,
			},
			"total_money": {
				"amount": reported_amount_cents,
				"currency": SQUARE_CURRENCY,
			},
			"location_id": self.location_id,
			"reference_id": clean_reference_id,
			"note": clean_note,
			"source_type": "CARD",
			"card_details": {
				"status": "CAPTURED" if status == "COMPLETED" else "AUTHORIZED",
				"card": {
					"card_brand": "VISA",
					"last_4": "4242",
				},
			},
			"receipt_number": f"MK{digest[:6].upper()}",
			"receipt_url": f"https://squareupsandbox.com/receipt/preview/{payment_id}",
			"created_at": timestamp,
			"updated_at": timestamp,
		}
		if clean_email:
			payment["buyer_email_address"] = clean_email

		self._idempotency_store[clean_idempotency_key] = copy.deepcopy(payment)
		self._payment_store[payment_id] = copy.deepcopy(payment)
		return payment

	def get_payment(self, square_payment_id: str) -> dict[str, Any]:
		if not isinstance(square_payment_id, str) or not square_payment_id.strip():
			raise frappe.ValidationError("Square payment ID is required.")
		clean_id = square_payment_id.strip()
		stored = self._payment_store.get(clean_id)
		if stored is None:
			raise SquareApiError(
				f"Square mock payment {clean_id} not found.",
				code="NOT_FOUND",
				category="INVALID_REQUEST_ERROR",
				detail="Payment not found.",
				status_code=404,
				retryable=False,
			)
		return copy.deepcopy(stored)


def get_square_client(settings_snapshot: dict[str, Any] | None = None) -> SquareClient:
	"""Factory returning MockSquareClient or live HTTPS SquareClient; raises ValidationError if Disabled."""
	snapshot = settings_snapshot if settings_snapshot is not None else get_square_settings_snapshot()
	environment = _clean_optional_str(snapshot.get("environment")) or SQUARE_ENV_DISABLED
	assert_cad_currency(snapshot.get("currency") or SQUARE_CURRENCY, context="ISM Settings Square")

	if environment == SQUARE_ENV_DISABLED:
		raise frappe.ValidationError("Square online payments are disabled.")

	if environment == SQUARE_ENV_MOCK:
		return MockSquareClient(location_id=_clean_optional_str(snapshot.get("location_id")) or "LMOCK00000001")

	if environment in (SQUARE_ENV_SANDBOX, SQUARE_ENV_PRODUCTION):
		access_token = _clean_optional_str(snapshot.get("access_token"))
		location_id = _clean_optional_str(snapshot.get("location_id"))
		if not access_token:
			raise frappe.ValidationError(f"Square {environment} mode requires Square Access Token.")
		if not location_id:
			raise frappe.ValidationError(f"Square {environment} mode requires Square Location ID.")
		return SquareClient(
			access_token=access_token,
			location_id=location_id,
			environment=environment,
		)

	raise frappe.ValidationError(f"Unsupported Square environment: {environment!r}.")
