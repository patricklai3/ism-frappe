# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, get_url, now_datetime

from ism.api.sms import dispatch_sms, get_sms_unconfigured_reason


def get_or_create_approval_token(repair_order_name: str) -> tuple[str, str]:
	"""Get an active approval token or generate a new one for a Repair Order.

	Returns:
		tuple[str, str]: (token, approval_url)
	"""
	doc = frappe.get_doc("Repair Order", repair_order_name)
	doc.check_permission("write")

	now = now_datetime()
	settings = frappe.get_single("ISM Settings")
	expiry_hours = cint(settings.authorization_token_expiry_hours) or 48

	token = doc.approval_token
	expiry = get_datetime(doc.approval_token_expiry) if doc.approval_token_expiry else None

	# Advance uncommunicated draft/estimating services upon dispatching for authorization
	doc_changed = False
	services = getattr(doc, "services", [])
	for service in services:
		if service.status in ("Draft", "Estimating"):
			service.status = "Pending Approval"
			doc_changed = True
			if service.name and not str(service.name).startswith("new-"):
				frappe.db.set_value(
					"Repair Order Service",
					service.name,
					"status",
					"Pending Approval",
					update_modified=False,
				)

	if not token or not expiry or now > expiry:
		token = frappe.generate_hash(length=32)
		doc.approval_token = token
		doc.approval_token_expiry = now + timedelta(hours=expiry_hours)
		doc_changed = True

	if doc_changed:
		doc.approval_status = _derive_approval_status(services)
		doc.flags.ignore_validate = True
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)

	approval_url = f"{get_url()}/quote-approval?token={token}"
	return token, approval_url


@frappe.whitelist()
def send_approval_request(
	repair_order_name: str,
	channel: str = "sms",
	recipient: str | None = None,
) -> dict:
	"""Dispatch digital approval link via SMS or Email, or generate a shareable URL."""
	normalized_channel = _normalize_dispatch_channel(channel)
	doc = frappe.get_doc("Repair Order", repair_order_name)
	doc.check_permission("write")

	_token, approval_url = get_or_create_approval_token(repair_order_name)
	vehicle_display = _format_vehicle_display(doc.vehicle)
	customer_name = doc.customer or _("Customer")

	if normalized_channel == "sms":
		return _dispatch_sms_approval(doc, approval_url, vehicle_display, customer_name, recipient)

	if normalized_channel == "email":
		return _dispatch_email_approval(doc, approval_url, vehicle_display, customer_name, recipient)

	return {
		"success": True,
		"channel": "link",
		"requested_channel": "link",
		"recipient": recipient,
		"approval_url": approval_url,
		"delivery_status": "Link Generated",
		"message": _("Approval link generated."),
	}


@frappe.whitelist(allow_guest=True)
def get_public_quote_approval(token: str) -> dict:
	"""Fetch Repair Order details for the public guest approval portal via secure token."""
	if token is not None and not isinstance(token, str):
		frappe.throw(_("Invalid approval token format."))

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
		"signature_image": doc.signature_image,
		"audit_hash": getattr(doc, "audit_hash", None),
		"approval_logs": _serialize_approval_logs(doc),
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
	if not token or not isinstance(token, str) or not token.strip():
		frappe.throw(_("Missing or invalid approval token."))

	if not isinstance(signer_name, str) or not signer_name.strip():
		frappe.throw(_("Signer name is required."))

	ro_name = frappe.db.get_value("Repair Order", {"approval_token": token.strip()}, "name")
	if not ro_name:
		frappe.throw(_("Repair Order not found for token."))

	doc = frappe.get_doc("Repair Order", ro_name)
	if doc.approval_token_expiry and now_datetime() > get_datetime(doc.approval_token_expiry):
		frappe.throw(_("Authorization token has expired."))

	services = getattr(doc, "services", [])
	has_pending = any(s.status in ("Draft", "Estimating", "Pending Approval") for s in services)
	if not has_pending and doc.approval_status in ("Approved", "Partially Approved", "Declined") and (
		doc.signature_image or doc.authorized_at
	):
		frappe.throw(_("This repair estimate has already been authorized."))

	normalized_approvals = _normalize_approvals_payload(approvals)
	if "Approved" in normalized_approvals.values() and (
		not isinstance(signature_data, str) or not signature_data.strip()
	):
		frappe.throw(_("Digital signature is required to approve services."))

	client_ip = ip_address if ip_address is not None else getattr(frappe.local, "request_ip", None)
	client_ua = user_agent
	if client_ua is None and getattr(frappe.local, "request", None):
		client_ua = frappe.get_request_header("User-Agent")

	doc.authorize_jobs(
		approvals=normalized_approvals,
		channel="Remote Web Link",
		signer_name=signer_name.strip(),
		signature_image=signature_data,
		ip_address=client_ip,
		user_agent=client_ua,
	)

	return {
		"success": True,
		"approval_status": doc.approval_status,
		"order_status": doc.status,
		"audit_hash": getattr(doc, "audit_hash", None),
		"message": _("Thank you! Your authorization has been recorded."),
	}


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


def _derive_approval_status(services: list) -> str:
	"""Compute Repair Order approval_status from current service row statuses."""
	has_pending = any(s.status in ("Draft", "Estimating", "Pending Approval") for s in services)
	has_approved = any(s.status == "Approved" for s in services)
	has_declined = any(s.status == "Declined" for s in services)

	if has_approved and has_pending:
		return "Partially Approved"
	if has_pending or not services:
		return "Pending Approval"
	if has_approved:
		return "Approved"
	if has_declined:
		return "Declined"
	return "Pending Approval"


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
	raw_phone = recipient if recipient is not None else frappe.db.get_value("Customer", doc.customer, "mobile_no")
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
	res = dispatch_sms(phone, sms_text)
	if not res.get("success"):
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
	raw_email = recipient if recipient is not None else frappe.db.get_value("Customer", doc.customer, "email_id")
	if not raw_email and getattr(doc, "contact_person", None):
		raw_email = frappe.db.get_value("Contact", doc.contact_person, "email_id")
	email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None
	if not email:
		frappe.throw(_("No customer email available. Please specify an email address."))

	subject = _("Repair Estimate Authorization - {0}").format(vehicle_display)
	message = _(
		"<p>Hello {0},</p><p>Please review and authorize your automotive repair estimate by clicking the link below:</p>"
		"<p><a href='{1}' class='btn btn-primary'>Review & Authorize Estimate</a></p>"
	).format(customer_name, approval_url)

	frappe.sendmail(recipients=[email], subject=subject, message=message, now=True)
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
	for service, raw_parts in zip(services, parsed_parts_by_idx):
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
			"signature_image": log.signature_image,
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
			"signature_image": doc.signature_image,
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
