# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import base64
import binascii
import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, get_datetime, get_url, now_datetime

from ism.api.sms import dispatch_sms, get_sms_unconfigured_reason
from ism.comms.email_dispatch import dispatch_transactional_email
from ism.portal.auth import assert_ro_mutating_unlocked
from ism.portal.events import emit_portal_quote_approved_event
from ism.portal.pin_vault import get_ro_pin_customer_state

_MAX_SIGNATURE_BYTES = 2 * 1024 * 1024
_ALLOWED_SIGNATURE_PREFIXES = (
	"data:image/png;base64,",
	"data:image/jpeg;base64,",
	"data:image/webp;base64,",
)
_PORTAL_RATE_LIMIT_PREFIX = "ism:portal_rl"
_PORTAL_RATE_LIMIT_WINDOW_SEC = 60
_PORTAL_RATE_LIMITS = {
	"read": {"ip": 30, "token": 30},
	"submit": {"ip": 15, "token": 10},
}


def _get_portal_client_ip(ip_override: str | None = None) -> str:
	"""Resolve client IP from request context (ignoring untrusted overrides when HTTP request is active)."""
	if getattr(frappe.local, "request", None):
		raw_ip = (
			getattr(frappe.local, "request_ip", None)
			or frappe.get_request_header("X-Forwarded-For")
			or getattr(frappe.local.request, "remote_addr", None)
		)
	else:
		raw_ip = (
			ip_override.strip()
			if isinstance(ip_override, str) and ip_override.strip()
			else getattr(frappe.local, "request_ip", None)
		)
	if isinstance(raw_ip, str) and raw_ip.strip():
		return raw_ip.split(",")[0].strip()
	return "unknown"


def _increment_rate_limit_key(cache_key: str, limit: int, window_sec: int = _PORTAL_RATE_LIMIT_WINDOW_SEC) -> None:
	"""Increment a Redis-backed rate-limit counter and throw TooManyRequestsError when exceeded."""
	cache = frappe.cache()
	current = cint(cache.get_value(cache_key) or 0) + 1
	cache.set_value(cache_key, current, expires_in_sec=window_sec)
	if current > limit:
		frappe.throw(
			_("Too many requests. Please wait a moment and try again."),
			frappe.TooManyRequestsError,
		)


def _enforce_portal_rate_limit(endpoint: str, token: str | None = None, ip_override: str | None = None) -> None:
	"""Enforce per-IP and per-token rate limits on public guest approval endpoints."""
	limits = _PORTAL_RATE_LIMITS[endpoint]
	client_ip = _get_portal_client_ip(ip_override=ip_override)
	_increment_rate_limit_key(f"{_PORTAL_RATE_LIMIT_PREFIX}:{endpoint}:ip:{client_ip}", limits["ip"])
	if isinstance(token, str) and token.strip():
		token_hash = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()[:24]
		_increment_rate_limit_key(f"{_PORTAL_RATE_LIMIT_PREFIX}:{endpoint}:tok:{token_hash}", limits["token"])


def clear_portal_rate_limits() -> None:
	"""Clear guest portal rate-limit keys from cache (for hermetic test runs)."""
	cache = frappe.cache()
	if hasattr(cache, "delete_keys"):
		cache.delete_keys(f"{_PORTAL_RATE_LIMIT_PREFIX}:*")


def _notify_shop_floor_approval(doc, normalized_approvals: dict[str, str], signer_name: str) -> None:
	"""Emit realtime event and create/update a deduplicated Shop Action Item via Seam 5."""
	emit_portal_quote_approved_event(doc, normalized_approvals, signer_name)


def _ensure_approval_token_for_doc(doc) -> tuple[str, str]:
	"""Delegate token creation/reuse to RepairOrder.generate_approval_token without reloading."""
	doc.check_permission("write")
	token_res = doc.generate_approval_token()
	token = doc.approval_token or (token_res.get("approval_token") if isinstance(token_res, dict) else None)
	if not token:
		frappe.throw(_("Unable to generate approval token for Repair Order {0}.").format(doc.name))
	approval_url = f"{get_url()}/quote-approval?token={token}"
	return token, approval_url


def get_or_create_approval_token(repair_order_name: str) -> tuple[str, str]:
	"""Get an active approval token or generate a new one for a Repair Order.

	Returns:
		tuple[str, str]: (token, approval_url)
	"""
	if not isinstance(repair_order_name, str) or not repair_order_name.strip():
		frappe.throw(_("Repair Order ID is required."))
	doc = frappe.get_doc("Repair Order", repair_order_name.strip())
	return _ensure_approval_token_for_doc(doc)


@frappe.whitelist()
def send_approval_request(
	repair_order_name: str,
	channel: str = "sms",
	recipient: str | None = None,
) -> dict:
	"""Dispatch digital approval link via SMS or Email, or generate a shareable URL."""
	normalized_channel = _normalize_dispatch_channel(channel)
	if not isinstance(repair_order_name, str) or not repair_order_name.strip():
		frappe.throw(_("Repair Order ID is required."))
	if recipient is not None and not isinstance(recipient, str):
		frappe.throw(_("Recipient must be a string."))

	doc = frappe.get_doc("Repair Order", repair_order_name.strip())
	_token, approval_url = _ensure_approval_token_for_doc(doc)
	vehicle_display = _format_vehicle_display(doc.vehicle)
	customer_name = doc.customer or _("Customer")
	clean_recipient = recipient.strip() if isinstance(recipient, str) and recipient.strip() else None

	if normalized_channel == "sms":
		return _dispatch_sms_approval(doc, approval_url, vehicle_display, customer_name, clean_recipient)

	if normalized_channel == "email":
		return _dispatch_email_approval(doc, approval_url, vehicle_display, customer_name, clean_recipient)

	return {
		"success": True,
		"channel": "link",
		"requested_channel": "link",
		"recipient": clean_recipient,
		"approval_url": approval_url,
		"delivery_status": "Link Generated",
		"message": _("Approval link generated."),
	}


@frappe.whitelist(allow_guest=True)
def get_public_quote_approval(token: str) -> dict:
	"""Fetch Repair Order details for the public guest approval portal via secure token."""
	if token is not None and not isinstance(token, str):
		frappe.throw(_("Invalid approval token format."))

	_enforce_portal_rate_limit("read", token=token)

	if not token or not token.strip():
		return {"valid": False, "error": _("Invalid or missing approval token.")}

	ro_name = frappe.db.get_value("Repair Order", {"approval_token": token.strip()}, "name")
	if not ro_name:
		return {"valid": False, "error": _("The requested repair estimate could not be found.")}

	doc = frappe.get_doc("Repair Order", ro_name)
	if doc.approval_token_expiry and now_datetime() > get_datetime(doc.approval_token_expiry):
		return {
			"valid": False,
			"error": _("This authorization link has expired. Please contact the service advisor for a new link."),
		}

	services = getattr(doc, "services", [])
	has_pending = any(s.status in ("Draft", "Estimating", "Pending Approval") for s in services)
	already_submitted = bool(
		not has_pending
		and doc.approval_status in ("Approved", "Partially Approved", "Declined")
		and (doc.signature_image or doc.authorized_at)
	)

	services_list = _serialize_portal_services(services)
	settings = frappe.get_single("ISM Settings")
	disclaimer = settings.disclaimer_text or _(
		"I hereby authorize the repair work listed above to be done along with the necessary material. "
		"You and your employees may operate the vehicle for purpose of testing, inspection, or delivery."
	)

	return {
		"valid": True,
		"already_submitted": already_submitted,
		"repair_order": {
			"name": doc.name,
			"posting_date": doc.posting_date,
			"status": doc.status,
			"approval_status": doc.approval_status,
			"customer_name": getattr(doc, "customer_name", None) or doc.customer,
			"customer_concern": doc.customer_concern,
			"odometer_in": doc.odometer_in,
			"is_supplement": int(any(s.get("is_supplement") for s in services_list)),
		},
		"vehicle": _get_vehicle_payload(doc.vehicle),
		"company": _get_company_payload(doc.company),
		"services": services_list,
		"net_total": flt(doc.net_total),
		"tax_rate": flt(getattr(doc, "tax_rate", 0.0)),
		"tax_total": flt(doc.total_taxes_and_charges),
		"grand_total": flt(doc.grand_total),
		"cost_tolerance_percentage": flt(getattr(settings, "cost_tolerance_percentage", 0.0)),
		"cost_tolerance_flat_amount": flt(getattr(settings, "cost_tolerance_flat_amount", 0.0)),
		"disclaimer_text": disclaimer,
		"authorized_by": doc.authorized_by,
		"authorized_at": doc.authorized_at,
		"audit_hash": getattr(doc, "audit_hash", None),
		"approval_logs": _serialize_approval_logs(doc),
	}


def _validate_signature_data(signature_data: str | None, require_signature: bool) -> str | None:
	"""Enforce allowlisted raster data URIs with strict base64 decoding and byte-size cap."""
	if signature_data is None or (isinstance(signature_data, str) and not signature_data.strip()):
		if require_signature:
			frappe.throw(_("Digital signature is required to approve services."))
		return None

	if not isinstance(signature_data, str):
		frappe.throw(_("Digital signature must be a raster image data URI."))

	cleaned = signature_data.strip()
	lower = cleaned.lower()
	matched_prefix = None
	for prefix in _ALLOWED_SIGNATURE_PREFIXES:
		if lower.startswith(prefix):
			matched_prefix = prefix
			break

	if not matched_prefix:
		frappe.throw(_("Unsupported signature format. Allowed raster types: PNG, JPEG, WebP."))

	b64_payload = cleaned[len(matched_prefix) :].strip()
	if not b64_payload:
		frappe.throw(_("Digital signature image payload cannot be empty."))

	max_b64_len = (_MAX_SIGNATURE_BYTES * 4 // 3) + 16
	if len(b64_payload) > max_b64_len:
		frappe.throw(_("Digital signature exceeds maximum allowed size (2 MB)."))

	try:
		decoded = base64.b64decode(b64_payload, validate=True)
	except (binascii.Error, ValueError):
		frappe.throw(_("Digital signature contains invalid base64 data."))

	if not decoded or len(decoded) > _MAX_SIGNATURE_BYTES:
		frappe.throw(_("Digital signature exceeds allowed size bounds."))

	return f"{matched_prefix}{b64_payload}"


def authorize_ro_for_portal(
	ro_doc,
	approvals: dict | list | str,
	signer_name: str,
	signature_data: str | None = None,
	client_ip: str | None = None,
	user_agent: str | None = None,
) -> dict:
	"""Shared portal authorization handler enforcing lifecycle locks, audit trail, and PIN prompt state."""
	if not isinstance(signer_name, str) or not signer_name.strip():
		frappe.throw(_("Signer name is required."))

	assert_ro_mutating_unlocked(getattr(ro_doc, "status", None))

	services = getattr(ro_doc, "services", [])
	has_pending = any(s.status in ("Draft", "Estimating", "Pending Approval") for s in services)
	if (
		not has_pending
		and ro_doc.approval_status in ("Approved", "Partially Approved", "Declined")
		and (ro_doc.signature_image or ro_doc.authorized_at)
	):
		frappe.throw(_("This repair estimate has already been authorized."))

	normalized_approvals = _normalize_approvals_payload(approvals)
	validated_signature = _validate_signature_data(
		signature_data,
		require_signature="Approved" in normalized_approvals.values(),
	)

	prev_portal_flag = getattr(frappe.flags, "_ism_public_approval_portal", False)
	frappe.flags._ism_public_approval_portal = True
	try:
		ro_doc.authorize_jobs(
			approvals=normalized_approvals,
			channel="Remote Web Link",
			signer_name=signer_name.strip(),
			signature_image=validated_signature,
			ip_address=client_ip,
			user_agent=user_agent,
		)
	finally:
		frappe.flags._ism_public_approval_portal = prev_portal_flag

	_notify_shop_floor_approval(ro_doc, normalized_approvals, signer_name)
	pin_state = get_ro_pin_customer_state(ro_doc)

	return {
		"success": True,
		"approval_status": ro_doc.approval_status,
		"order_status": ro_doc.status,
		"audit_hash": getattr(ro_doc, "audit_hash", None),
		"pin_prompt_required": bool(pin_state.get("prompt_required")),
		"message": _("Thank you! Your authorization has been recorded."),
	}


@frappe.whitelist(allow_guest=True)
def submit_customer_approval(
	token: str,
	approvals: dict | list | str,
	signer_name: str,
	signature_data: str | None = None,
	ip_address: str | None = None,
	user_agent: str | None = None,
	**kwargs,
) -> dict:
	"""Public guest endpoint to record digital signature and per-service decisions."""
	kwargs.pop("cmd", None)
	_enforce_portal_rate_limit(
		"submit",
		token=token if isinstance(token, str) else None,
		ip_override=ip_address if isinstance(ip_address, str) else None,
	)
	if not token or not isinstance(token, str) or not token.strip():
		frappe.throw(_("Missing or invalid approval token."))

	if not isinstance(signer_name, str) or not signer_name.strip():
		frappe.throw(_("Signer name is required."))

	locked_rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabRepair Order`
		WHERE approval_token = %s
		FOR UPDATE
		""",
		(token.strip(),),
		as_dict=True,
	)
	if not locked_rows:
		frappe.throw(_("Repair Order not found for token."))

	doc = frappe.get_doc("Repair Order", locked_rows[0].name)
	if doc.approval_token_expiry and now_datetime() > get_datetime(doc.approval_token_expiry):
		frappe.throw(_("Authorization token has expired."))

	if getattr(frappe.local, "request", None):
		client_ip = getattr(frappe.local, "request_ip", None)
		client_ua = frappe.get_request_header("User-Agent")
	else:
		client_ip = ip_address if isinstance(ip_address, str) else getattr(frappe.local, "request_ip", None)
		client_ua = user_agent if isinstance(user_agent, str) else None

	return authorize_ro_for_portal(
		doc,
		approvals=approvals,
		signer_name=signer_name,
		signature_data=signature_data,
		client_ip=client_ip,
		user_agent=client_ua,
	)


@frappe.whitelist(allow_guest=True)
def submit_public_quote_approval(
	token: str,
	approvals: dict | list | str,
	signer_name: str,
	signature_data: str | None = None,
	ip_address: str | None = None,
	user_agent: str | None = None,
	**kwargs,
) -> dict:
	"""Whitelisted alias matching quote_approval.html client caller."""
	kwargs.pop("cmd", None)
	return submit_customer_approval(
		token=token,
		approvals=approvals,
		signer_name=signer_name,
		signature_data=signature_data,
		ip_address=ip_address,
		user_agent=user_agent,
		**kwargs,
	)


def _normalize_dispatch_channel(channel: str | None) -> str:
	"""Normalize and validate approval dispatch channel names."""
	normalized = (channel or "sms").strip().lower()
	if normalized in ("copy link only", "url", "none"):
		normalized = "link"
	if normalized not in ("sms", "email", "link"):
		frappe.throw(
			_("Invalid approval channel: {0}. Supported channels are sms, email, or link.").format(channel)
		)
	return normalized


def _format_vehicle_display(vehicle_name: str | None) -> str:
	"""Build a readable label for notification messages."""
	if not vehicle_name:
		return _("Vehicle")
	v_info = frappe.db.get_value("Repair Vehicle", vehicle_name, ["model", "license_plate"], as_dict=True)
	if not v_info:
		return vehicle_name
	parts = [v_info.model, f"({v_info.license_plate})" if v_info.license_plate else ""]
	return " ".join(p for p in parts if p).strip() or vehicle_name


def _dispatch_sms_approval(
	doc,
	approval_url: str,
	vehicle_display: str,
	customer_name: str,
	recipient: str | None,
) -> dict:
	"""Send approval link over SMS or fall back cleanly to link-only when SMS is unconfigured."""
	explicit_recipient = recipient.strip() if isinstance(recipient, str) and recipient.strip() else None
	raw_phone = (
		explicit_recipient
		if explicit_recipient is not None
		else frappe.db.get_value("Customer", doc.customer, "mobile_no")
	)
	if not raw_phone and getattr(doc, "contact_person", None):
		raw_phone = frappe.db.get_value("Contact", doc.contact_person, "mobile_no") or frappe.db.get_value(
			"Contact", doc.contact_person, "phone"
		)
	phone = raw_phone.strip() if isinstance(raw_phone, str) and raw_phone.strip() else None

	unconfigured_reason = get_sms_unconfigured_reason()
	if unconfigured_reason:
		return {
			"success": True,
			"channel": "link",
			"requested_channel": "sms",
			"recipient": phone,
			"approval_url": approval_url,
			"delivery_status": "Link Only (SMS Unconfigured)",
			"warning": unconfigured_reason,
			"message": _("Approval link generated (SMS not sent: {0}). Share the approval URL directly.").format(
				unconfigured_reason
			),
		}

	if not phone:
		frappe.throw(_("No customer phone number available. Please specify a phone number."))

	sms_text = _("Hello {0}, please review and authorize the repair estimate for your {1}: {2}").format(
		customer_name, vehicle_display, approval_url
	)
	res = dispatch_sms(
		phone,
		sms_text,
		message_type="Approval Link",
		reference_doctype="Repair Order",
		reference_name=doc.name,
	)
	if not res.get("success"):
		if res.get("opted_out"):
			frappe.throw(
				_(
					"Customer has opted out of SMS messages. Please send the approval via Email or use Copy Link Only."
				)
			)
		frappe.throw(res.get("error") or _("Failed to dispatch SMS."))

	return {
		"success": True,
		"channel": "sms",
		"requested_channel": "sms",
		"recipient": phone,
		"approval_url": approval_url,
		"delivery_status": "Sent",
		"message_id": res.get("message_id"),
		"message": _("Approval request sent via SMS to {0}.").format(phone),
	}


def _dispatch_email_approval(
	doc,
	approval_url: str,
	vehicle_display: str,
	customer_name: str,
	recipient: str | None,
) -> dict:
	"""Send approval link via email."""
	explicit_recipient = recipient.strip() if isinstance(recipient, str) and recipient.strip() else None
	raw_email = (
		explicit_recipient
		if explicit_recipient is not None
		else frappe.db.get_value("Customer", doc.customer, "email_id")
	)
	if not raw_email and getattr(doc, "contact_person", None):
		raw_email = frappe.db.get_value("Contact", doc.contact_person, "email_id")
	email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None
	if not email:
		frappe.throw(_("No customer email available. Please specify an email address."))

	safe_vehicle_subject = str(vehicle_display or "").replace("\r", " ").replace("\n", " ")
	safe_vehicle = escape_html(safe_vehicle_subject)
	safe_customer = escape_html(str(customer_name or ""))
	safe_url = escape_html(str(approval_url or ""))

	subject = _("Repair Estimate Authorization - {0}").format(safe_vehicle)
	message = _(
		"<p>Hello {0},</p><p>Please review and authorize your automotive repair estimate for {1} by clicking the link below:</p>"
		"<p><a href='{2}' class='btn btn-primary'>Review & Authorize Estimate</a></p>"
	).format(safe_customer, safe_vehicle, safe_url)

	email_res = dispatch_transactional_email(
		recipient=email,
		subject=subject,
		html_body=message,
		email_type="approval_link",
		reference_doctype="Repair Order",
		reference_name=doc.name,
		bypass_rate_limit=True,
	)
	if not email_res.success:
		frappe.throw(email_res.error or _("Failed to dispatch approval email."))
	return {
		"success": True,
		"channel": "email",
		"requested_channel": "email",
		"recipient": email,
		"approval_url": approval_url,
		"delivery_status": "Sent",
		"message": _("Approval request sent via email to {0}.").format(email),
	}


def _get_vehicle_payload(vehicle_name: str | None) -> dict:
	"""Fetch vehicle metadata for the approval portal."""
	if not vehicle_name:
		return {}
	vehicle = frappe.db.get_value(
		"Repair Vehicle",
		vehicle_name,
		["name", "manufacturer", "model", "model_year", "license_plate", "vin"],
		as_dict=True,
	)
	if not vehicle:
		return {}
	return {
		"make": vehicle.manufacturer or "Tesla",
		"model": vehicle.model,
		"model_year": vehicle.model_year,
		"license_plate": vehicle.license_plate,
		"vin": vehicle.vin,
	}


def _get_company_payload(company_name: str | None) -> dict:
	"""Fetch shop company details for portal header and disclosures."""
	if not company_name:
		return {"company_name": None}
	company = frappe.db.get_value("Company", company_name, ["company_name", "phone_no", "email"], as_dict=True)
	return company or {"company_name": company_name}


def _serialize_portal_services(services: list) -> list[dict]:
	"""Serialize Repair Order Service rows with batched Ontario part disclosure metadata."""
	parsed_parts_by_idx = []
	item_codes = set()

	for service in services:
		raw_parts = getattr(service, "parts", [])
		if isinstance(raw_parts, str):
			try:
				raw_parts = json.loads(raw_parts) if raw_parts else []
			except Exception:
				raw_parts = []
		elif not isinstance(raw_parts, list):
			raw_parts = []

		parsed_parts_by_idx.append(raw_parts)
		for part in raw_parts:
			if isinstance(part, dict) and part.get("item_code"):
				item_codes.add(part["item_code"])

	items_meta = {}
	if item_codes:
		desired_fields = ["part_source", "part_condition", "warranty_period", "oem_part_number"]
		query_fields = ["name"] + [f for f in desired_fields if frappe.db.has_column("Item", f)]
		rows = frappe.get_all(
			"Item",
			filters={"name": ["in", list(item_codes)]},
			fields=query_fields,
		)
		items_meta = {row.name: row for row in rows}

	serialized = []
	for service, raw_parts in zip(services, parsed_parts_by_idx, strict=True):
		enriched_parts = []
		for part in raw_parts:
			if not isinstance(part, dict):
				continue
			code = part.get("item_code", "")
			meta = items_meta.get(code)
			src = part.get("part_source") or (getattr(meta, "part_source", None) if meta else None) or "OEM"
			cond = part.get("part_condition") or (getattr(meta, "part_condition", None) if meta else None) or "New"
			warr = (
				part.get("warranty_period")
				or (getattr(meta, "warranty_period", None) if meta else None)
				or "12 Months / 20,000 km"
			)
			oem_num = (
				part.get("oem_part_number")
				or (getattr(meta, "oem_part_number", None) if meta else None)
				or ""
			)

			enriched_parts.append({
				"item_code": code,
				"item_name": part.get("item_name", code),
				"part_source": src,
				"part_condition": cond,
				"warranty_period": warr,
				"oem_part_number": oem_num,
				"qty": flt(part.get("qty", 1.0)),
				"rate": flt(part.get("rate", 0.0)),
				"amount": flt(part.get("amount", 0.0)),
			})

		category = getattr(service, "category", None) or (
			"Supplement" if cint(service.is_supplement) else "Service"
		)
		serialized.append({
			"name": service.name,
			"idx": service.idx,
			"job_id": service.name,
			"service_title": service.service_title,
			"job_title": service.service_title,
			"category": category,
			"labor_hours": flt(service.labor_hours),
			"labor_rate": flt(service.labor_rate),
			"labor_amount": flt(service.labor_amount),
			"parts": enriched_parts,
			"parts_total": flt(service.parts_total),
			"total_amount": flt(service.total_amount),
			"authorized_amount": flt(service.total_amount) if service.status == "Approved" else 0.0,
			"status": service.status,
			"is_supplement": cint(service.is_supplement),
			"is_locked": service.status in ("Approved", "Declined"),
		})

	return serialized


def _serialize_approval_logs(doc) -> list[dict]:
	"""Return structured audit trail rows for the portal confirmation and history views."""
	logs = [
		{
			"approval_datetime": str(log.approval_datetime),
			"signer_name": log.signer_name,
			"channel": log.channel,
			"authorized_amount": flt(log.authorized_amount),
			"approved_services": log.approved_services,
			"declined_services": log.declined_services,
			"audit_hash": getattr(log, "audit_hash", None),
		}
		for log in getattr(doc, "approval_logs", [])
	]
	if not logs and doc.authorized_at and doc.authorized_by:
		services = getattr(doc, "services", [])
		prior_approved = [s for s in services if s.status == "Approved"]
		prior_declined = [s for s in services if s.status == "Declined"]
		logs.append({
			"approval_datetime": str(doc.authorized_at),
			"signer_name": doc.authorized_by,
			"channel": "Remote Web Link",
			"authorized_amount": flt(sum(flt(s.total_amount) for s in prior_approved), 2),
			"approved_services": ", ".join(
				f"{s.service_title} (${flt(s.total_amount):,.2f})" for s in prior_approved
			)
			or _("None"),
			"declined_services": ", ".join(s.service_title for s in prior_declined) or _("None"),
			"audit_hash": getattr(doc, "audit_hash", None),
		})
	return logs


def _normalize_approvals_payload(approvals: dict | list | str) -> dict[str, str]:
	"""Parse and validate explicit per-service Approved/Declined decisions."""
	parsed = approvals
	if isinstance(parsed, str):
		try:
			parsed = json.loads(parsed)
		except Exception:
			frappe.throw(_("Invalid approvals payload format."))

	if isinstance(parsed, list):
		normalized: dict[str, str] = {}
		for item in parsed:
			if isinstance(item, dict):
				key = item.get("job_id") or item.get("service_row_id") or item.get("name")
				decision = item.get("status") or item.get("decision")
				if key and decision:
					normalized[str(key)] = str(decision).strip()
		parsed = normalized

	if not isinstance(parsed, dict) or not parsed:
		frappe.throw(_("Please select Approve or Decline for at least one service."))

	cleaned: dict[str, str] = {}
	for key, val in parsed.items():
		decision = str(val).strip() if val is not None else ""
		if decision not in ("Approved", "Declined"):
			frappe.throw(
				_("Invalid decision '{0}' for service '{1}'. Must be 'Approved' or 'Declined'.").format(
					val, key
				)
			)
		cleaned[str(key)] = decision

	return cleaned
