# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import hashlib
import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, get_url, now_datetime, nowdate

from ism.api.sms import dispatch_sms


def get_or_create_approval_token(repair_order_name: str) -> tuple[str, str]:
	"""Get existing valid token or generate a new 48-hour approval token for Repair Order.

	Returns:
		tuple: (token, approval_url)
	"""
	doc = frappe.get_doc("Repair Order", repair_order_name)
	doc.check_permission("read")

	now = now_datetime()
	settings = frappe.get_single("ISM Settings")
	expiry_hours = cint(settings.authorization_token_expiry_hours) or 48

	token = doc.approval_token
	expiry = get_datetime(doc.approval_token_expiry) if doc.approval_token_expiry else None

	# Advance uncommunicated draft/estimating services to Pending Approval upon requesting authorization
	services_updated = False
	for s in getattr(doc, "services", []):
		if s.status in ["Draft", "Estimating"]:
			s.status = "Pending Approval"
			services_updated = True
			if s.name and not str(s.name).startswith("new-"):
				frappe.db.set_value("Repair Order Service", s.name, "status", "Pending Approval", update_modified=False)

	if not token or not expiry or now > expiry:
		token = frappe.generate_hash(length=32)
		doc.approval_token = token
		doc.approval_token_expiry = now + timedelta(hours=expiry_hours)
		services_updated = True

	if services_updated:
		has_approved = any(s.status == "Approved" for s in getattr(doc, "services", []))
		if has_approved:
			doc.approval_status = "Partially Approved"
		else:
			doc.approval_status = "Pending Approval"
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
	"""Dispatch digital approval link via SMS or Email, or provide Copy Link URL."""
	doc = frappe.get_doc("Repair Order", repair_order_name)
	doc.check_permission("write")

	token, approval_url = get_or_create_approval_token(repair_order_name)

	vehicle_display = doc.vehicle or _("Vehicle")
	if doc.vehicle:
		v_info = frappe.db.get_value("Repair Vehicle", doc.vehicle, ["model", "license_plate"], as_dict=True)
		if v_info:
			parts = [v_info.model, f"({v_info.license_plate})" if v_info.license_plate else ""]
			vehicle_display = " ".join(p for p in parts if p).strip()

	customer_name = doc.customer or _("Customer")

	if channel == "sms":
		phone = recipient or frappe.db.get_value("Customer", doc.customer, "mobile_no")
		if not phone:
			frappe.throw(_("No customer phone number available. Please specify a phone number."))

		sms_text = _(
			"Hello {0}, please review and authorize the repair estimate for your {1}: {2}"
		).format(customer_name, vehicle_display, approval_url)

		res = dispatch_sms(phone, sms_text)
		if not res.get("success"):
			frappe.throw(res.get("error") or _("Failed to dispatch SMS."))

		return {
			"success": True,
			"channel": "sms",
			"recipient": phone,
			"approval_url": approval_url,
			"message": _("Approval request sent via SMS to {0}.").format(phone),
		}

	elif channel == "email":
		email = recipient or frappe.db.get_value("Customer", doc.customer, "email_id")
		if not email:
			frappe.throw(_("No customer email available. Please specify an email address."))

		subject = _("Repair Estimate Authorization - {0}").format(vehicle_display)
		message = _(
			"<p>Hello {0},</p><p>Please review and authorize your automotive repair estimate by clicking the link below:</p><p><a href='{1}' class='btn btn-primary'>Review & Authorize Estimate</a></p>"
		).format(customer_name, approval_url)

		frappe.sendmail(
			recipients=[email],
			subject=subject,
			message=message,
			now=True,
		)
		return {
			"success": True,
			"channel": "email",
			"recipient": email,
			"approval_url": approval_url,
			"message": _("Approval request sent via email to {0}.").format(email),
		}

	return {
		"success": True,
		"channel": "link",
		"approval_url": approval_url,
		"message": _("Approval link generated."),
	}


@frappe.whitelist(allow_guest=True)
def get_public_quote_approval(token: str) -> dict:
	"""Fetch Repair Order details for public guest approval portal via secure token."""
	if not token or not isinstance(token, str) or not token.strip():
		return {"valid": False, "error": _("Invalid or missing approval token.")}

	ro_name = frappe.db.get_value("Repair Order", {"approval_token": token.strip()}, "name")
	if not ro_name:
		return {"valid": False, "error": _("The requested repair estimate could not be found.")}

	doc = frappe.get_doc("Repair Order", ro_name)

	# Verify expiry
	if doc.approval_token_expiry and now_datetime() > get_datetime(doc.approval_token_expiry):
		return {"valid": False, "error": _("This authorization link has expired. Please contact the service advisor for a new link.")}

	# Check if already authorized
	has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in getattr(doc, "services", []))
	already_submitted = not has_pending and doc.approval_status in ["Approved", "Partially Approved", "Declined"] and doc.signature_image is not None

	# Vehicle info
	vehicle_data = {}
	if doc.vehicle:
		v = frappe.db.get_value(
			"Repair Vehicle",
			doc.vehicle,
			["name", "manufacturer", "model", "model_year", "license_plate", "vin"],
			as_dict=True,
		)
		if v:
			vehicle_data = {
				"make": v.manufacturer or "Tesla",
				"model": v.model,
				"model_year": v.model_year,
				"license_plate": v.license_plate,
				"vin": v.vin,
			}

	# Company details
	company_data = {"company_name": doc.company}
	if doc.company:
		c = frappe.db.get_value("Company", doc.company, ["company_name", "phone_no", "email"], as_dict=True)
		if c:
			company_data = c

	# Build service list from Repair Order services with nested parts
	services_list = []
	for s in getattr(doc, "services", []):
		parts = getattr(s, "parts", [])
		if isinstance(parts, str):
			try:
				parts = json.loads(parts) if parts else []
			except Exception:
				parts = []
		elif not isinstance(parts, list):
			parts = []

		linked_items = []
		for p in parts:
			ic = p.get("item_code", "")
			src = p.get("part_source")
			cond = p.get("part_condition")
			warr = p.get("warranty_period")
			oem_num = p.get("oem_part_number")
			if ic and (not src or not cond or not warr):
				it_info = frappe.db.get_value("Item", ic, ["part_source", "part_condition", "warranty_period", "oem_part_number"], as_dict=True)
				if it_info:
					src = src or it_info.part_source or "OEM"
					cond = cond or it_info.part_condition or "New"
					warr = warr or it_info.warranty_period or "12 Months / 20,000 km"
					oem_num = oem_num or it_info.oem_part_number or ""

			linked_items.append({
				"item_code": ic,
				"item_name": p.get("item_name", ic),
				"item_type": "Stock Item",
				"part_source": src or "OEM",
				"part_condition": cond or "New",
				"warranty_period": warr or "12 Months / 20,000 km",
				"oem_part_number": oem_num or "",
				"qty": flt(p.get("qty", 1.0)),
				"rate": flt(p.get("rate", 0.0)),
				"amount": flt(p.get("amount", 0.0)),
			})

		services_list.append({
			"name": s.name,
			"idx": s.idx,
			"job_id": s.name,
			"job_title": s.service_title,
			"service_title": s.service_title,
			"job_category": s.category,
			"category": s.category,
			"estimated_total": flt(s.total_amount),
			"authorized_amount": flt(s.total_amount) if s.status == "Approved" else 0.0,
			"labor_hours": flt(s.labor_hours),
			"labor_rate": flt(s.labor_rate),
			"labor_amount": flt(s.labor_amount),
			"parts": parts,
			"parts_total": flt(s.parts_total),
			"total_amount": flt(s.total_amount),
			"status": s.status,
			"is_supplement": cint(s.is_supplement),
			"is_locked": s.status in ["Approved", "Declined"],
			"items": linked_items,
		})

	# Backward compatibility fallback if legacy jobs still exist
	if not services_list and hasattr(doc, "jobs") and doc.jobs:
		for job in doc.jobs:
			linked_items = [
				{
					"item_code": it.item_code,
					"item_name": it.item_name,
					"item_type": getattr(it, "item_type", "Stock Item"),
					"qty": flt(it.qty),
					"rate": flt(it.rate),
					"amount": flt(it.amount),
				}
				for it in getattr(doc, "items", [])
				if getattr(it, "job_id", None) == job.job_id
			]
			services_list.append({
				"name": job.name,
				"idx": getattr(job, "idx", 1),
				"job_id": getattr(job, "job_id", job.name),
				"job_title": job.job_title,
				"service_title": job.job_title,
				"job_category": getattr(job, "job_category", "Mechanical"),
				"category": getattr(job, "job_category", "Mechanical"),
				"estimated_total": flt(job.estimated_total),
				"authorized_amount": flt(getattr(job, "authorized_amount", 0.0)),
				"status": job.status,
				"is_supplement": cint(getattr(job, "is_supplement", 0)),
				"items": linked_items,
			})


	settings = frappe.get_single("ISM Settings")
	disclaimer = settings.disclaimer_text or _(
		"I hereby authorize the repair work listed above to be done along with the necessary material. You and your employees may operate the vehicle for purpose of testing, inspection, or delivery."
	)

	approval_logs = [
		{
			"approval_datetime": str(log.approval_datetime),
			"signer_name": log.signer_name,
			"channel": log.channel,
			"authorized_amount": flt(log.authorized_amount),
			"approved_services": log.approved_services,
			"declined_services": log.declined_services,
			"signature_image": log.signature_image,
		}
		for log in getattr(doc, "approval_logs", [])
	]
	if not approval_logs and doc.authorized_at and doc.authorized_by:
		prior_approved = [s for s in getattr(doc, "services", []) if s.status == "Approved"]
		prior_declined = [s for s in getattr(doc, "services", []) if s.status == "Declined"]
		approval_logs.append({
			"approval_datetime": str(doc.authorized_at),
			"signer_name": doc.authorized_by,
			"channel": "Remote Web Link",
			"authorized_amount": flt(sum(flt(s.total_amount) for s in prior_approved), 2),
			"approved_services": ", ".join(f"{s.service_title} (${flt(s.total_amount):,.2f})" for s in prior_approved) or _("None"),
			"declined_services": ", ".join(s.service_title for s in prior_declined) or _("None"),
			"signature_image": doc.signature_image,
		})

	return {
		"valid": True,
		"already_submitted": already_submitted,
		"quotation": {
			"name": doc.name,
			"transaction_date": doc.posting_date,
			"status": doc.status,
			"customer_name": getattr(doc, "customer_name", None) or doc.customer,
			"customer_concern": doc.customer_concern,
			"is_supplement": cint(doc.is_supplement) if hasattr(doc, "is_supplement") else int(any(s.get("is_supplement") for s in services_list)),
		},
		"repair_order": {
			"name": doc.name,
			"posting_date": doc.posting_date,
			"status": doc.status,
			"customer_name": getattr(doc, "customer_name", None) or doc.customer,
			"customer_concern": doc.customer_concern,
			"is_supplement": cint(doc.is_supplement) if hasattr(doc, "is_supplement") else int(any(s.get("is_supplement") for s in services_list)),
		},
		"vehicle": vehicle_data,
		"company": company_data,
		"services": services_list,
		"jobs": services_list,
		"net_total": flt(doc.net_total),
		"tax_total": flt(doc.total_taxes_and_charges),
		"grand_total": flt(doc.grand_total),
		"disclaimer": disclaimer,
		"authorized_by": doc.authorized_by,
		"authorized_at": doc.authorized_at,
		"signature_image": doc.signature_image,
		"approval_logs": approval_logs,
	}


@frappe.whitelist(allow_guest=True)
def submit_customer_approval(
	token: str,
	approvals: dict | list | str,
	signer_name: str,
	signature_data: str,
	ip_address: str | None = None,
	user_agent: str | None = None,
	**kwargs,
) -> dict:
	"""Public guest endpoint to record digital signature and per-job decisions."""
	kwargs.pop("cmd", None)
	if not token or not isinstance(token, str) or not token.strip():
		frappe.throw(_("Missing or invalid approval token."))

	ro_name = frappe.db.get_value("Repair Order", {"approval_token": token.strip()}, "name")
	if not ro_name:
		frappe.throw(_("Repair Order not found for token."))

	doc = frappe.get_doc("Repair Order", ro_name)

	if doc.approval_token_expiry and now_datetime() > get_datetime(doc.approval_token_expiry):
		frappe.throw(_("Authorization token has expired."))

	has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in getattr(doc, "services", []))
	if not has_pending and doc.approval_status in ["Approved", "Partially Approved"] and doc.signature_image:
		frappe.throw(_("This repair estimate has already been authorized."))

	# Normalize approvals if passed as list of objects from web portal
	if isinstance(approvals, str):
		try:
			approvals = json.loads(approvals)
		except Exception:
			approvals = {}

	if isinstance(approvals, list):
		approvals_dict = {}
		for item in approvals:
			if isinstance(item, dict):
				key = item.get("job_id") or item.get("service_row_id") or item.get("name")
				status = item.get("status") or item.get("decision")
				if key and status:
					approvals_dict[str(key)] = status
		approvals = approvals_dict

	doc.authorize_jobs(
		approvals=approvals,
		channel="Remote Web Link",
		signer_name=signer_name,
		signature_image=signature_data,
		ip_address=ip_address,
		user_agent=user_agent,
	)

	return {"success": True, "message": _("Thank you! Your authorization has been recorded.")}


@frappe.whitelist(allow_guest=True)
def submit_public_quote_approval(
	token: str,
	approvals: dict | list | str,
	signer_name: str,
	signature_data: str,
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
