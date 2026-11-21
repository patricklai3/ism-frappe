# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Whitelisted REST API surface for Project 4 Expanded Customer Portal (Dual-Auth Visit Hub).

All customer endpoints live under ``/api/method/ism.api.portal.<name>``.
Security guarantees:
- Unauthenticated bootstrap returns strictly public branding (ZERO PII).
- Every authenticated portal read/write requires an active ``ism_portal_session`` cookie
  and enforces strict scope ownership (raising uniform ``frappe.PermissionError`` on IDOR).
- Mutating endpoints require ``POST`` (with Frappe CSRF protection).
- Never imports ``ism.board`` (Seam 5 decoupling).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint
from werkzeug.wrappers import Response

from ism.api.approval import authorize_ro_for_portal
from ism.api.sms import dispatch_sms, get_sms_unconfigured_reason
from ism.comms.contracts import get_client_ip
from ism.payments.contracts import assert_no_raw_card_fields
from ism.payments.deposit_requests import get_ro_payment_state
from ism.payments.erpnext_bridge import execute_tokenized_payment
from ism.payments.square_client import get_sdk_boot_config
from ism.portal.auth import (
	assert_ro_mutating_unlocked,
	assert_session_owns_ro,
	assert_session_owns_schedule_entry,
	exchange_booking_token,
	exchange_ro_approval_token,
	get_active_portal_session,
	get_public_portal_branding,
	request_portal_login_otp,
	require_portal_session,
	resolve_customer_phone_e164,
	sign_out_portal_session,
	verify_portal_login_otp,
)
from ism.portal.events import emit_portal_reschedule_event
from ism.portal.invoices import (
	build_invoice_download_headers,
	email_sales_invoice_copy,
	get_ro_invoice_summary,
	render_sales_invoice_pdf,
)
from ism.portal.pin_vault import (
	get_ro_pin_for_staff,
	opt_out_ro_pin,
	submit_ro_pin,
)
from ism.portal.visit_view import (
	_to_plain_value,
	build_portal_hub_payload,
)
from ism.scheduling.self_serve import (
	evaluate_reschedule_eligibility,
	find_self_serve_slots,
	get_required_tags_for_entry,
	reschedule_self_serve_appointment,
)

_DISALLOWED_AMOUNT_KEYS = frozenset({
	"amount",
	"amount_cents",
	"amount_money",
	"total",
	"grand_total",
	"paid_amount",
	"deposit_amount",
	"outstanding_amount",
})


def _get_user_agent() -> str | None:
	if not getattr(frappe.local, "request", None):
		return None
	try:
		ua = frappe.get_request_header("User-Agent")
	except Exception:
		headers = getattr(frappe.local.request, "headers", None)
		ua = headers.get("User-Agent") if headers else None
	return str(ua).strip()[:255] if isinstance(ua, str) and ua.strip() else None


def _coerce_bool_flag(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, int | float):
		return bool(value)
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return False


def _coerce_expected_amount_cents(value: Any) -> int | None:
	if value is None:
		return None
	if isinstance(value, bool):
		frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)
	if isinstance(value, int):
		if value <= 0:
			frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)
		return value
	if isinstance(value, str):
		cleaned = value.strip()
		if not cleaned:
			return None
		if not cleaned.isdigit():
			frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)
		parsed = int(cleaned)
		if parsed <= 0:
			frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)
		return parsed
	frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)


def _reject_pci_and_amount_overrides(kwargs: dict[str, Any]) -> None:
	assert_no_raw_card_fields(kwargs)

	form_dict = getattr(frappe.local, "form_dict", None)
	form_snapshot = dict(form_dict) if isinstance(form_dict, dict) else {}
	if form_snapshot:
		assert_no_raw_card_fields(form_snapshot)

	for source in (kwargs, form_snapshot):
		for key, value in source.items():
			if str(key).strip().lower() in _DISALLOWED_AMOUNT_KEYS and value is not None:
				frappe.throw(
					_(
						"Client-supplied payment amount overrides are not accepted. "
						"Amount is computed server-side."
					),
					frappe.ValidationError,
				)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_portal_bootstrap(
	active_ro: str | None = None,
	active_schedule_entry: str | None = None,
) -> dict[str, Any]:
	"""Return strictly public shop branding when unauthenticated (ZERO PII) or full hub when signed in."""
	session = get_active_portal_session(touch=True)
	if session is None:
		return {
			"authenticated": False,
			"shop": get_public_portal_branding(),
		}

	clean_ro = active_ro if isinstance(active_ro, str) and active_ro.strip() else None
	clean_se = (
		active_schedule_entry
		if isinstance(active_schedule_entry, str) and active_schedule_entry.strip()
		else None
	)
	hub = build_portal_hub_payload(
		session,
		active_ro=clean_ro,
		active_schedule_entry=clean_se,
	)
	return {
		"authenticated": True,
		"hub": hub,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def exchange_portal_link(
	token: str | None = None,
	booking_token: str | None = None,
) -> dict[str, Any]:
	"""Path 1 (Zero OTP Friction): Exchange RO approval token or booking token into persistent session cookie."""
	client_ip = get_client_ip()
	user_agent = _get_user_agent()

	if isinstance(token, str) and token.strip():
		result = exchange_ro_approval_token(
			token,
			client_ip=client_ip,
			user_agent=user_agent,
			set_response_cookie=True,
		)
		if not result.get("valid"):
			return {
				"valid": False,
				"error": result.get("error") or _("This portal link is invalid or has expired."),
			}
		hub = build_portal_hub_payload(result["session"], active_ro=result.get("repair_order"))
		return {"valid": True, "hub": hub}

	if isinstance(booking_token, str) and booking_token.strip():
		result = exchange_booking_token(
			booking_token,
			client_ip=client_ip,
			user_agent=user_agent,
			set_response_cookie=True,
		)
		if not result.get("valid"):
			return {
				"valid": False,
				"error": result.get("error") or _("This portal link is invalid or has expired."),
			}
		converted_ro = result.get("repair_order") or None
		hub = build_portal_hub_payload(
			result["session"],
			active_ro=converted_ro,
			active_schedule_entry=None if converted_ro else result.get("schedule_entry"),
		)
		return {"valid": True, "hub": hub}

	# Consume link-exchange rate limit bucket uniformly on missing/invalid parameters.
	result = exchange_ro_approval_token(
		"",
		client_ip=client_ip,
		user_agent=user_agent,
		set_response_cookie=False,
	)
	return {
		"valid": False,
		"error": result.get("error") or _("This portal link is invalid or has expired."),
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_login_otp(phone: str) -> dict[str, Any]:
	"""Path 2 (SMS-only, non-enumerating): Request a 6-digit SMS login OTP."""
	return request_portal_login_otp(
		phone,
		client_ip=get_client_ip(),
		user_agent=_get_user_agent(),
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def verify_login_otp(
	challenge_id: str,
	code: str,
	phone: str | None = None,
	upgrade_existing_session: int | bool | str = 0,
) -> dict[str, Any]:
	"""Verify SMS OTP, issue or upgrade persistent ism_portal_session cookie, and return hub payload."""
	result = verify_portal_login_otp(
		challenge_id,
		code,
		raw_phone=phone,
		client_ip=get_client_ip(),
		user_agent=_get_user_agent(),
		upgrade_existing_session=_coerce_bool_flag(upgrade_existing_session),
		set_response_cookie=True,
	)
	if not result.get("verified"):
		payload: dict[str, Any] = {
			"verified": False,
			"error": result.get("error") or _("Invalid or expired verification code."),
		}
		if result.get("error_code"):
			payload["error_code"] = result["error_code"]
		if result.get("locked_until"):
			payload["locked_until"] = result["locked_until"]
		return payload

	hub = build_portal_hub_payload(result["session"])
	return {
		"verified": True,
		"hub": hub,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def sign_out() -> dict[str, Any]:
	"""Revoke active server-side portal session and clear ism_portal_session cookie."""
	sign_out_portal_session(reason="Customer signed out on device")
	return {"signed_out": True}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_portal_hub(
	active_ro: str | None = None,
	active_schedule_entry: str | None = None,
) -> dict[str, Any]:
	"""Return authenticated Visit Hub payload after enforcing strict scope ownership."""
	session = require_portal_session(touch=True)
	return build_portal_hub_payload(
		session,
		active_ro=active_ro,
		active_schedule_entry=active_schedule_entry,
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def submit_portal_quote_approval(
	repair_order: str,
	approvals: dict | list | str,
	signer_name: str,
	signature_data: str | None = None,
) -> dict[str, Any]:
	"""Record customer quote approval under active portal session + RO scope + unlocked lifecycle."""
	session = require_portal_session(touch=True)
	assert_session_owns_ro(session, repair_order)

	ro_name = repair_order.strip()
	locked_rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabRepair Order`
		WHERE name = %s
		FOR UPDATE
		""",
		(ro_name,),
		as_dict=True,
	)
	if not locked_rows:
		frappe.throw(_("You do not have permission to access this visit."), frappe.PermissionError)

	ro_doc = frappe.get_doc("Repair Order", ro_name)
	assert_ro_mutating_unlocked(getattr(ro_doc, "status", None))

	auth_result = authorize_ro_for_portal(
		ro_doc,
		approvals=approvals,
		signer_name=signer_name,
		signature_data=signature_data,
		client_ip=get_client_ip(),
		user_agent=_get_user_agent(),
	)
	hub = build_portal_hub_payload(session, active_ro=ro_name)
	return {
		"success": True,
		"approval_status": auth_result["approval_status"],
		"order_status": auth_result["order_status"],
		"audit_hash": auth_result["audit_hash"],
		"pin_prompt_required": bool(auth_result["pin_prompt_required"]),
		"message": auth_result["message"],
		"hub": hub,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def submit_pin_to_drive(repair_order: str, pin: str) -> dict[str, Any]:
	"""Submit or update a 4-digit encrypted PIN-to-Drive under active portal session (NEVER echoes PIN)."""
	session = require_portal_session(touch=True)
	assert_session_owns_ro(session, repair_order)

	ro_name = repair_order.strip()
	pin_state = submit_ro_pin(ro_name, pin, actor_label="Customer Portal")
	hub = build_portal_hub_payload(session, active_ro=ro_name)
	return {
		"success": True,
		"pin_to_drive": pin_state,
		"hub": hub,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def opt_out_pin_to_drive(repair_order: str) -> dict[str, Any]:
	"""Record customer 1-tap 'No PIN-to-Drive enabled' opt-out under active portal session."""
	session = require_portal_session(touch=True)
	assert_session_owns_ro(session, repair_order)

	ro_name = repair_order.strip()
	pin_state = opt_out_ro_pin(ro_name, actor_label="Customer Portal")
	hub = build_portal_hub_payload(session, active_ro=ro_name)
	return {
		"success": True,
		"pin_to_drive": pin_state,
		"hub": hub,
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_reschedule_options(
	schedule_entry: str,
	start_date: str | None = None,
	days_ahead: int | str = 7,
) -> dict[str, Any]:
	"""Return eligibility and sanitized customer drop-off reschedule slots ONLY (zero internal times/bays/techs)."""
	session = require_portal_session(touch=True)
	assert_session_owns_schedule_entry(session, schedule_entry)

	se_name = schedule_entry.strip()
	se_doc = frappe.get_doc("Schedule Entry", se_name)
	eligibility = _to_plain_value(evaluate_reschedule_eligibility(se_doc))

	if not eligibility.get("allowed"):
		return {
			"schedule_entry": se_name,
			"eligibility": eligibility,
			"slots_by_date": {},
			"slots": [],
		}

	duration_minutes = cint(getattr(se_doc, "estimated_duration", None) or 0) or None
	required_tags = get_required_tags_for_entry(se_doc)
	parsed_days_ahead = cint(days_ahead) if days_ahead not in (None, "") else 7
	search_result = find_self_serve_slots(
		duration_minutes=duration_minutes,
		required_tags=required_tags,
		start_date=start_date,
		days_ahead=parsed_days_ahead,
		exclude_entry=se_name,
	)

	current_appt = eligibility.get("current_appointment") or {}
	curr_date = current_appt.get("appointment_date")
	curr_time = current_appt.get("customer_arrival_time")

	slots_by_date: dict[str, list[dict[str, Any]]] = {}
	flat_slots: list[dict[str, Any]] = []

	for day_group in search_result.get("days", []):
		iso_date = str(day_group["date"])
		parsed_dt = date.fromisoformat(iso_date)
		display_date = f"{parsed_dt.strftime('%a, %b')} {parsed_dt.day}, {parsed_dt.year}"
		day_items: list[dict[str, Any]] = []
		for raw_slot in day_group.get("slots", []):
			arrival_time = str(raw_slot["customer_arrival_time"])
			if iso_date == curr_date and arrival_time == curr_time:
				continue
			slot_item = {
				"date": iso_date,
				"display_date": display_date,
				"customer_arrival_time": arrival_time,
				"customer_arrival_hhmm": str(raw_slot.get("customer_arrival_hhmm") or arrival_time[:5]),
				"display_time": str(raw_slot["display_time"]),
				"available": True,
			}
			day_items.append(slot_item)
			flat_slots.append(slot_item)
		if day_items:
			slots_by_date[iso_date] = day_items

	return {
		"schedule_entry": se_name,
		"eligibility": eligibility,
		"slots_by_date": slots_by_date,
		"slots": flat_slots,
	}


def _send_reschedule_confirmation_sms(
	*,
	schedule_entry: str,
	customer: str | None,
	provisional_phone: str | None,
	session_phone: str | None,
	new_appointment: dict[str, Any],
) -> bool:
	"""Dispatch updated reschedule confirmation SMS displaying ONLY P3 customer drop-off time."""
	if get_sms_unconfigured_reason():
		return False

	target_phone = (
		resolve_customer_phone_e164(customer, fallback_phone=provisional_phone)
		or session_phone
	)
	if not target_phone:
		return False

	shop_name = (get_public_portal_branding().get("company_name") or "").strip() or "ISM Automotive"
	display_dt = (new_appointment or {}).get("display_datetime") or (
		f"{(new_appointment or {}).get('display_date', '')} at {(new_appointment or {}).get('display_time', '')}"
	).strip()
	sms_body = _(
		"{0}: Your appointment has been rescheduled to {1}. Reply STOP to opt out."
	).format(shop_name, display_dt)

	try:
		res = dispatch_sms(
			target_phone,
			sms_body,
			message_type="Appointment Reschedule",
			reference_doctype="Schedule Entry",
			reference_name=schedule_entry,
		)
		return bool(res.get("success"))
	except Exception:
		frappe.log_error(
			title=f"Portal Reschedule SMS Failed ({schedule_entry})",
			message=frappe.get_traceback(),
		)
		return False


@frappe.whitelist(allow_guest=True, methods=["POST"])
def reschedule_appointment(
	schedule_entry: str,
	target_date: str,
	customer_arrival_time: str,
	reason: str | None = None,
) -> dict[str, Any]:
	"""Atomically reschedule a customer appointment via P3, emit Seam 5 realtime, and dispatch P1 SMS."""
	session = require_portal_session(touch=True)
	assert_session_owns_schedule_entry(session, schedule_entry)

	se_name = schedule_entry.strip()
	# ScheduleEntry.validate() checks bay availability via ism.api.scheduling.check_bay_availability(),
	# which asserts Desk read permission on Schedule Entry. Scope ownership has already been strictly
	# enforced above, so elevate only for the atomic P3 reschedule call and restore immediately.
	prev_user = getattr(frappe.session, "user", None)
	if prev_user == "Guest":
		frappe.set_user("Administrator")
	try:
		result = reschedule_self_serve_appointment(
			se_name,
			target_date=target_date,
			customer_arrival_time=customer_arrival_time,
			reason=reason,
			emit_notification=True,
		)
	finally:
		if prev_user and frappe.session.user != prev_user:
			frappe.set_user(prev_user)

	se_meta = (
		frappe.db.get_value(
			"Schedule Entry",
			se_name,
			["repair_order", "customer", "provisional_phone"],
			as_dict=True,
		)
		or {}
	)
	linked_ro = (se_meta.get("repair_order") or "").strip() or None
	new_appointment = _to_plain_value(result["appointment"])
	previous_appointment = _to_plain_value(result["previous_appointment"])

	emit_portal_reschedule_event(
		schedule_entry=se_name,
		repair_order=linked_ro,
		previous_appointment=previous_appointment,
		new_appointment=new_appointment,
	)

	sms_sent = _send_reschedule_confirmation_sms(
		schedule_entry=se_name,
		customer=se_meta.get("customer"),
		provisional_phone=se_meta.get("provisional_phone"),
		session_phone=session.phone_e164,
		new_appointment=new_appointment,
	)

	hub = build_portal_hub_payload(
		session,
		active_ro=linked_ro,
		active_schedule_entry=None if linked_ro else se_name,
	)
	return {
		"success": True,
		"schedule_entry": se_name,
		"previous_appointment": previous_appointment,
		"appointment": new_appointment,
		"reschedule_count": result["reschedule_count"],
		"remaining_reschedules": result["remaining_reschedules"],
		"sms_sent": sms_sent,
		"hub": hub,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def submit_portal_payment(
	repair_order: str,
	payment_intent: str,
	source_id: str,
	idempotency_key: str | None = None,
	expected_amount_cents: int | str | None = None,
	buyer_email: str | None = None,
	**kwargs,
) -> dict[str, Any]:
	"""Execute CAD Square tokenized deposit or invoice balance payment via P2 under active portal session."""
	kwargs.pop("cmd", None)
	_reject_pci_and_amount_overrides(kwargs)

	session = require_portal_session(touch=True)
	assert_session_owns_ro(session, repair_order)

	coerced_expected_cents = _coerce_expected_amount_cents(expected_amount_cents)
	ro_name = repair_order.strip()

	charge_result = _to_plain_value(
		execute_tokenized_payment(
			repair_order=ro_name,
			payment_intent=payment_intent,
			source_id=source_id,
			idempotency_key=idempotency_key,
			expected_amount_cents=coerced_expected_cents,
			buyer_email=buyer_email,
			initiated_by="Customer Portal",
			client_ip=get_client_ip(),
		)
	)

	ro_doc = frappe.get_doc("Repair Order", ro_name)
	payment_block = {
		"sdk_config": _to_plain_value(get_sdk_boot_config()),
		"state": _to_plain_value(get_ro_payment_state(ro_name)),
	}
	invoice_block = _to_plain_value(get_ro_invoice_summary(ro_doc))
	hub = build_portal_hub_payload(session, active_ro=ro_name)

	return {
		"success": charge_result.get("status") not in ("Failed", "Voided"),
		**charge_result,
		"charge": charge_result,
		"payment": payment_block,
		"invoice": invoice_block,
		"hub": hub,
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def download_invoice_pdf(repair_order: str) -> Response:
	"""Stream official Sales Invoice PDF (works for BOTH unpaid & paid submitted Sales Invoices)."""
	session = require_portal_session(touch=True)
	assert_session_owns_ro(session, repair_order)

	ro_name = repair_order.strip()
	filename, pdf_bytes = render_sales_invoice_pdf(ro_name)
	headers = build_invoice_download_headers(filename)

	if not hasattr(frappe.local, "response") or frappe.local.response is None:
		frappe.local.response = frappe._dict()

	frappe.local.response["type"] = "download"
	frappe.local.response["filename"] = filename
	frappe.local.response["filecontent"] = pdf_bytes
	frappe.local.response["display_content_as"] = "attachment"
	frappe.local.response["content_type"] = "application/pdf"
	frappe.local.response["headers"] = headers

	return Response(
		pdf_bytes,
		status=200,
		headers=headers,
		mimetype="application/pdf",
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def email_invoice_copy(
	repair_order: str,
	recipient_email: str | None = None,
) -> dict[str, Any]:
	"""Email official Sales Invoice PDF copy (unpaid or paid) via Project 1 with strict rate limiting."""
	session = require_portal_session(touch=True)
	assert_session_owns_ro(session, repair_order)

	ro_name = repair_order.strip()
	target_email: Any
	if isinstance(recipient_email, str) and not recipient_email.strip():
		target_email = None
	else:
		target_email = recipient_email

	res = email_sales_invoice_copy(
		ro_name,
		recipient_email=target_email,
		client_ip=get_client_ip(),
	)
	return {
		**res,
		"sent": bool(res.get("success")),
		"error": None if res.get("success") else res.get("message"),
	}


@frappe.whitelist(allow_guest=False, methods=["POST"])
def reveal_ro_pin(repair_order: str) -> dict[str, Any]:
	"""Staff-only convenience wrapper delegating to ism.portal.pin_vault.get_ro_pin_for_staff."""
	return get_ro_pin_for_staff(repair_order, channel="Desk")
