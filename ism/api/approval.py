# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import hashlib
import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, get_url, now_datetime, nowdate

from ism.api.sms import dispatch_sms


def get_or_create_approval_token(quotation_name: str) -> tuple[str, str]:
	"""Get existing valid token or generate a new 48-hour approval token for Quotation.

	Returns:
		tuple: (token, approval_url)
	"""
	doc = frappe.get_doc("Quotation", quotation_name)
	doc.check_permission("read")

	now = now_datetime()
	settings = frappe.get_single("ISM Settings")
	expiry_hours = cint(settings.authorization_token_expiry_hours) or 48

	token = doc.custom_approval_token
	expiry = get_datetime(doc.custom_approval_token_expiry) if doc.custom_approval_token_expiry else None

	# If token doesn't exist or is expired, generate a new one
	if not token or not expiry or now > expiry:
		token = frappe.generate_hash(length=32)
		doc.custom_approval_token = token
		doc.custom_approval_token_expiry = now + timedelta(hours=expiry_hours)
		if doc.custom_approval_status == "Draft":
			doc.custom_approval_status = "Pending Approval"
		doc.flags.ignore_validate = True
		doc.flags.ignore_validate_update_after_submit = True
		doc.flags.ignore_links = True
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)

	approval_url = f"{get_url()}/quote-approval?token={token}"
	return token, approval_url


@frappe.whitelist()
def send_approval_request(
	quotation_name: str,
	channel: str = "sms",
	recipient: str | None = None,
) -> dict:
	"""Dispatch digital approval link via SMS or Email, or provide Copy Link URL.

	Args:
		quotation_name (str): ID of Quotation
		channel (str): "sms", "email", or "link"
		recipient (str): Target phone number or email address
	"""
	doc = frappe.get_doc("Quotation", quotation_name)
	doc.check_permission("write")

	token, approval_url = get_or_create_approval_token(quotation_name)

	# Vehicle info
	vehicle_display = doc.custom_vehicle or _("Vehicle")
	if doc.custom_vehicle:
		v_info = frappe.db.get_value("Repair Vehicle", doc.custom_vehicle, ["model", "license_plate"], as_dict=True)
		if v_info:
			parts = [v_info.model, f"({v_info.license_plate})" if v_info.license_plate else ""]
			vehicle_display = " ".join(p for p in parts if p).strip()

	customer_name = doc.customer_name or doc.party_name or _("Customer")

	if channel == "sms":
		phone = recipient or doc.contact_mobile or frappe.db.get_value("Customer", doc.party_name, "mobile_no")
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
		email = recipient or doc.contact_email or frappe.db.get_value("Customer", doc.party_name, "email_id")
		if not email:
			frappe.throw(_("No customer email available. Please specify an email address."))

		subject = _("Repair Estimate Authorization - {0}").format(vehicle_display)
		message = _(
			"<p>Hello {0},</p>"
			"<p>Please review and authorize the recommended repair services for your <b>{1}</b>.</p>"
			"<p><a href='{2}' style='background-color:#007bff;color:white;padding:10px 20px;text-decoration:none;border-radius:4px;display:inline-block;'>Review & Authorize Estimate</a></p>"
			"<p>Or copy and paste this link in your browser: <br>{2}</p>"
			"<p>Thank you,<br>{3}</p>"
		).format(customer_name, vehicle_display, approval_url, doc.company or _("Service Center"))

		frappe.sendmail(
			recipients=[email],
			subject=subject,
			message=message,
			reference_doctype="Quotation",
			reference_name=quotation_name,
		)

		return {
			"success": True,
			"channel": "email",
			"recipient": email,
			"approval_url": approval_url,
			"message": _("Approval request emailed to {0}.").format(email),
		}

	# Fallback: Copy link
	return {
		"success": True,
		"channel": "link",
		"approval_url": approval_url,
		"message": _("Approval URL generated."),
	}


@frappe.whitelist(allow_guest=True)
def get_public_quote_approval(token: str) -> dict:
	"""Fetch structured quote and job information for public mobile review page."""
	if not token or not isinstance(token, str) or not token.strip():
		return {"valid": False, "error": _("Invalid approval token.")}

	quotation_name = frappe.db.get_value("Quotation", {"custom_approval_token": token.strip()})
	if not quotation_name:
		return {"valid": False, "error": _("Estimate not found or link is invalid.")}

	doc = frappe.get_doc("Quotation", quotation_name)
	now = now_datetime()
	expiry = get_datetime(doc.custom_approval_token_expiry) if doc.custom_approval_token_expiry else None

	is_expired = bool(expiry and now > expiry)

	# Check if all jobs are resolved (already submitted)
	jobs = doc.get("custom_jobs") or []
	has_pending = any(j.status in ("Draft", "Pending Approval") for j in jobs)
	already_submitted = len(jobs) > 0 and not has_pending

	if is_expired and not already_submitted:
		return {
			"valid": False,
			"expired": True,
			"error": _("This authorization link expired on {0}. Please contact the service center for an updated estimate.").format(expiry),
		}

	# Vehicle details
	v_info = {}
	if doc.custom_vehicle:
		v_doc = frappe.db.get_value(
			"Repair Vehicle",
			doc.custom_vehicle,
			["vin", "license_plate", "model", "model_year", "manufacturer", "color"],
			as_dict=True,
		)
		if v_doc:
			v_info = v_doc

	# Company details
	company_info = {}
	if doc.company:
		c_doc = frappe.db.get_value("Company", doc.company, ["company_name", "phone_no", "email"], as_dict=True)
		if c_doc:
			company_info = c_doc

	# Build jobs and their linked items
	job_list = []
	for j in jobs:
		# Find items linked to this job
		linked_items = []
		for it in doc.get("items") or []:
			if it.custom_job == j.name:
				linked_items.append({
					"item_code": it.item_code,
					"item_name": it.item_name,
					"description": it.description,
					"qty": flt(it.qty),
					"rate": flt(it.rate),
					"amount": flt(it.amount),
				})

		job_list.append({
			"name": j.name,
			"job_title": j.job_title,
			"job_category": j.job_category,
			"status": j.status,
			"estimated_total": flt(j.estimated_total),
			"authorized_amount": flt(j.authorized_amount),
			"approval_channel": j.approval_channel,
			"authorized_by": j.authorized_by,
			"authorized_at": str(j.authorized_at) if j.authorized_at else None,
			"signature_image": j.signature_image,
			"is_supplement": cint(j.is_supplement),
			"items": linked_items,
		})

	settings = frappe.get_single("ISM Settings")

	return {
		"valid": True,
		"already_submitted": already_submitted,
		"is_expired": is_expired,
		"quotation": {
			"name": doc.name,
			"transaction_date": str(doc.transaction_date),
			"customer_name": doc.customer_name or doc.party_name,
			"grand_total": flt(doc.grand_total),
			"net_total": flt(doc.net_total),
			"total_taxes_and_charges": flt(doc.total_taxes_and_charges),
			"approval_status": doc.custom_approval_status,
			"odometer_in": doc.custom_odometer_in,
			"customer_concern": doc.custom_customer_concern,
			"is_supplement": cint(doc.custom_is_supplement),
		},
		"vehicle": v_info,
		"company": company_info,
		"jobs": job_list,
		"disclaimer_text": settings.disclaimer_text or _(
			"I hereby authorize the repair work and necessary materials as itemized above."
		),
	}


@frappe.whitelist(allow_guest=True)
def submit_public_quote_approval(
	token: str,
	approvals: str | list,
	signature_data: str | None = None,
	signer_name: str | None = None,
) -> dict:
	"""Process customer digital approval submitted via the public mobile web link."""
	if not token or not isinstance(token, str) or not token.strip():
		frappe.throw(_("Invalid token."), frappe.AuthenticationError)

	quotation_name = frappe.db.get_value("Quotation", {"custom_approval_token": token.strip()})
	if not quotation_name:
		frappe.throw(_("Quotation not found for provided token."), frappe.DoesNotExistError)

	doc = frappe.get_doc("Quotation", quotation_name)

	# Validate docstatus - cannot modify cancelled quotation
	if doc.docstatus == 2:
		frappe.throw(_("Quotation {0} is cancelled and cannot be modified.").format(doc.name))

	# Validate that quote still has unresolved jobs (prevent replay / tampering)
	jobs = doc.get("custom_jobs") or []
	if not jobs:
		frappe.throw(_("Quotation has no repair jobs configured."))

	has_pending = any(j.status in ("Draft", "Pending Approval") for j in jobs)
	if not has_pending:
		frappe.throw(_("This estimate has already been authorized. Further online modifications are locked."))

	# Validate expiration
	now = now_datetime()
	expiry = get_datetime(doc.custom_approval_token_expiry) if doc.custom_approval_token_expiry else None
	if expiry and now > expiry:
		frappe.throw(_("This estimate authorization link has expired."))

	# Parse approvals list
	if isinstance(approvals, str):
		try:
			approvals = frappe.parse_json(approvals) or []
		except Exception:
			frappe.throw(_("Invalid approvals payload format."))

	if not isinstance(approvals, list) or not approvals:
		frappe.throw(_("No job authorization choices were provided."))

	signer = (signer_name or "").strip() or (doc.customer_name or doc.party_name)

	# Check if any job is approved
	has_approved_job = any(item.get("status") == "Approved" for item in approvals if isinstance(item, dict))
	file_url = None
	if has_approved_job and signature_data:
		file_url = save_signature_file(doc.doctype, doc.name, signature_data, signer)

	# Client audit info
	client_ip = getattr(frappe.local, "request_ip", None) or "127.0.0.1"
	user_agent = ""
	if getattr(frappe.local, "request", None):
		try:
			user_agent = frappe.get_request_header("User-Agent") or ""
		except Exception:
			user_agent = ""
	audit_payload = f"{token}:{signer}:{now}:{json.dumps(approvals)}"
	audit_hash = hashlib.sha256(audit_payload.encode("utf-8")).hexdigest()

	# Apply approvals to Quotation Job rows
	apply_job_decisions(
		doc=doc,
		decisions=approvals,
		channel="Remote Web Link",
		authorized_by=signer,
		signature_image=file_url,
		audit_hash=audit_hash,
		ip_address=client_ip,
		user_agent=user_agent,
		advisor=None,
	)

	return {
		"success": True,
		"message": _("Your authorization has been recorded. Thank you!"),
		"status": doc.custom_approval_status,
	}


@frappe.whitelist()
def capture_in_person_signature(
	quotation_name: str,
	approvals: str | list,
	signature_data: str,
	signer_name: str,
) -> dict:
	"""Record in-person customer authorization captured on a shop desk/tablet."""
	doc = frappe.get_doc("Quotation", quotation_name)
	doc.check_permission("write")

	if isinstance(approvals, str):
		approvals = frappe.parse_json(approvals) or []

	if not signer_name:
		frappe.throw(_("Signer name is required."))

	file_url = None
	if signature_data:
		file_url = save_signature_file(doc.doctype, doc.name, signature_data, signer_name)

	apply_job_decisions(
		doc=doc,
		decisions=approvals,
		channel="In-Person Signature",
		authorized_by=signer_name.strip(),
		signature_image=file_url,
		advisor=frappe.session.user,
	)

	return {"success": True, "message": _("In-person authorization saved.")}


@frappe.whitelist()
def log_verbal_authorization(
	quotation_name: str,
	approvals: str | list,
	contact_person: str,
	phone_number: str,
	notes: str,
) -> dict:
	"""Record verbal phone authorization logged by a service advisor."""
	doc = frappe.get_doc("Quotation", quotation_name)
	doc.check_permission("write")

	if isinstance(approvals, str):
		approvals = frappe.parse_json(approvals) or []

	if not contact_person or not phone_number:
		frappe.throw(_("Customer contact person and phone number are required for verbal authorization."))

	if not notes or not notes.strip():
		frappe.throw(_("Advisor call notes are required for verbal phone authorization."))

	caller_id = f"{contact_person.strip()} ({phone_number.strip()})"

	apply_job_decisions(
		doc=doc,
		decisions=approvals,
		channel="Verbal Phone Authorization",
		authorized_by=caller_id,
		verbal_notes=notes.strip(),
		advisor=frappe.session.user,
	)

	return {"success": True, "message": _("Verbal phone authorization logged.")}


def apply_job_decisions(
	doc,
	decisions: list[dict],
	channel: str,
	authorized_by: str,
	signature_image: str | None = None,
	audit_hash: str | None = None,
	ip_address: str | None = None,
	user_agent: str | None = None,
	verbal_notes: str | None = None,
	advisor: str | None = None,
):
	"""Apply customer or advisor decisions to Quotation Job rows and sync vehicle/SO records."""
	now = now_datetime()
	decision_map = {}
	for d in decisions:
		if not isinstance(d, dict):
			continue
		status = d.get("status")
		if not status:
			continue
		for key in ("job_id", "name", "job_title", "title"):
			val = d.get(key)
			if val:
				decision_map[val] = status

	jobs = doc.get("custom_jobs") or []
	if not jobs:
		frappe.throw(_("Quotation has no repair jobs configured."))

	approved_count = 0
	declined_count = 0

	for job in jobs:
		decision = decision_map.get(job.name) or decision_map.get(job.job_title)
		if decision in ("Approved", "Declined"):
			job.status = decision
			job.approval_channel = channel
			job.authorized_by = authorized_by
			job.authorized_at = now
			if advisor:
				job.advisor = advisor
			if verbal_notes:
				job.verbal_notes = verbal_notes
			if audit_hash:
				job.audit_hash = audit_hash
			if ip_address:
				job.ip_address = ip_address
			if user_agent:
				job.user_agent = user_agent

			if decision == "Approved":
				job.authorized_amount = flt(job.estimated_total)
				if signature_image:
					job.signature_image = signature_image
				approved_count += 1
			elif decision == "Declined":
				declined_count += 1

	# Determine overall status
	total_jobs = len(jobs)
	if approved_count == total_jobs:
		doc.custom_approval_status = "Customer Approved"
	elif approved_count > 0:
		doc.custom_approval_status = "Partially Approved"
	elif declined_count == total_jobs:
		doc.custom_approval_status = "Declined"

	doc.flags.ignore_validate = True
	doc.flags.ignore_validate_update_after_submit = True
	doc.flags.ignore_links = True
	doc.flags.ignore_mandatory = True
	doc.save(ignore_permissions=True)

	# Sync declined jobs to Repair Vehicle deferred services
	sync_declined_jobs_to_vehicle(doc)

	# If this is a supplement for an active Sales Order, auto-merge approved jobs
	if doc.custom_is_supplement and doc.custom_supplement_for and approved_count > 0:
		merge_supplement_to_sales_order(doc)


def sync_declined_jobs_to_vehicle(quotation_doc):
	"""Sync all declined jobs from Quotation to Repair Vehicle's deferred services table."""
	if not quotation_doc.custom_vehicle:
		return

	declined_jobs = [j for j in (quotation_doc.get("custom_jobs") or []) if j.status == "Declined"]
	if not declined_jobs:
		return

	try:
		vehicle = frappe.get_doc("Repair Vehicle", quotation_doc.custom_vehicle)
		existing_titles = {
			(d.job_title, d.quotation) for d in (vehicle.get("deferred_services") or [])
		}

		added = False
		for job in declined_jobs:
			key = (job.job_title, quotation_doc.name)
			if key not in existing_titles:
				vehicle.append("deferred_services", {
					"job_title": job.job_title,
					"decline_date": nowdate(),
					"quotation": quotation_doc.name,
					"estimated_amount": flt(job.estimated_total),
					"status": "Open Recommendation",
					"decline_reason": job.verbal_notes or _("Declined by customer during estimate authorization"),
				})
				added = True

		if added:
			# Prune any orphaned customer rows if customer record no longer exists
			if vehicle.customers:
				vehicle.customers = [
					c for c in vehicle.customers if c.customer and frappe.db.exists("Customer", c.customer)
				]
			vehicle.flags.ignore_links = True
			vehicle.flags.ignore_validate = True
			vehicle.flags.ignore_mandatory = True
			vehicle.save(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Failed to sync declined jobs to vehicle", message=frappe.get_traceback())


@frappe.whitelist()
def create_supplemental_quotation(sales_order_name: str) -> dict:
	"""Generate a Supplemental Quotation linked to an active Sales Order (Repair Order)."""
	so = frappe.get_doc("Sales Order", sales_order_name)
	so.check_permission("write")

	quote = frappe.new_doc("Quotation")
	quote.quotation_to = "Customer"
	quote.party_name = so.customer
	quote.company = so.company
	quote.currency = so.currency
	quote.conversion_rate = so.conversion_rate or 1.0
	quote.selling_price_list = so.selling_price_list
	quote.price_list_currency = so.price_list_currency or so.currency
	quote.plc_conversion_rate = so.plc_conversion_rate or 1.0
	quote.custom_vehicle = so.custom_vehicle
	quote.custom_vehicle_check_in = so.custom_vehicle_check_in
	quote.custom_odometer_in = so.custom_odometer_in
	quote.custom_is_supplement = 1
	quote.custom_supplement_for = so.name
	quote.custom_customer_concern = _("Supplemental repair work for Repair Order {0}").format(so.name)
	quote.base_grand_total = 0.0
	quote.base_rounded_total = 0.0
	quote.grand_total = 0.0
	quote.rounded_total = 0.0
	quote.flags.ignore_validate = True
	quote.flags.ignore_mandatory = True
	quote.insert(ignore_permissions=True, ignore_mandatory=True)

	return {
		"success": True,
		"quotation": quote.name,
		"message": _("Supplemental Quotation {0} created.").format(quote.name),
	}


def merge_supplement_to_sales_order(supplement_quotation_doc):
	"""Append approved jobs and their linked items from supplement quote into parent Sales Order."""
	so_name = supplement_quotation_doc.custom_supplement_for
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		return

	so = frappe.get_doc("Sales Order", so_name)
	existing_so_jobs = {j.job_title for j in (so.get("custom_jobs") or [])}

	# Map of old job row name -> new SO job row name
	job_id_map = {}

	for q_job in supplement_quotation_doc.get("custom_jobs") or []:
		if q_job.status == "Approved" and q_job.job_title not in existing_so_jobs:
			new_job_id = frappe.generate_hash(length=10)
			new_job = so.append("custom_jobs", {
				"job_title": q_job.job_title,
				"job_category": q_job.job_category,
				"service_package": q_job.service_package,
				"status": "Approved",
				"approval_channel": q_job.approval_channel,
				"estimated_total": q_job.estimated_total,
				"authorized_amount": q_job.authorized_amount,
				"authorized_by": q_job.authorized_by,
				"authorized_at": q_job.authorized_at,
				"advisor": q_job.advisor,
				"signature_image": q_job.signature_image,
				"is_supplement": 1,
			})
			new_job.name = new_job_id
			new_job.set("__islocal", 1)
			job_id_map[q_job.name] = new_job_id
			job_id_map[q_job.job_title] = new_job_id

	# Append items linked to approved jobs
	for it in supplement_quotation_doc.get("items") or []:
		job_ref = job_id_map.get(it.custom_job) or job_id_map.get(it.custom_job_title)
		if job_ref:
			rate = flt(it.rate)
			qty = flt(it.qty)
			amount = flt(it.amount) or (rate * qty)
			conv_rate = flt(so.conversion_rate) or 1.0
			base_amt = amount * conv_rate

			so.append("items", {
				"item_code": it.item_code,
				"item_name": it.item_name,
				"description": it.description,
				"qty": qty,
				"rate": rate,
				"amount": amount,
				"net_rate": rate,
				"net_amount": amount,
				"base_rate": rate * conv_rate,
				"base_amount": base_amt,
				"base_net_rate": rate * conv_rate,
				"base_net_amount": base_amt,
				"uom": it.uom or "Nos",
				"conversion_factor": it.conversion_factor or 1.0,
				"delivery_date": so.delivery_date or frappe.utils.nowdate(),
				"custom_job": job_ref,
				"custom_job_title": it.custom_job_title,
			})

	so.calculate_taxes_and_totals()
	so.flags.ignore_validate = True
	so.flags.ignore_mandatory = True
	so.flags.ignore_validate_update_after_submit = True
	so.save(ignore_permissions=True)


def save_signature_file(doctype: str, docname: str, base64_data: str, signer_name: str) -> str:
	"""Save base64 signature canvas data as a private Frappe File attachment."""
	import base64
	import binascii

	if not isinstance(base64_data, str) or not base64_data.strip():
		frappe.throw(_("Signature data must be a valid base64 image string."))

	# Prevent DOS from oversized payloads (max 2 MB for signature canvas)
	if len(base64_data) > 2 * 1024 * 1024:
		frappe.throw(_("Signature file payload exceeds permitted size limit (2 MB)."))

	# Strip header if present: 'data:image/png;base64,...'
	if "," in base64_data:
		header, encoded = base64_data.split(",", 1)
	else:
		encoded = base64_data

	try:
		content = base64.b64decode(encoded)
	except (binascii.Error, ValueError):
		frappe.throw(_("Invalid base64 signature encoding."))

	clean_name = "".join(c for c in signer_name if c.isalnum())[:30] or "customer"
	filename = f"sig_{clean_name}_{frappe.generate_hash(length=8)}.png"

	file_doc = frappe.get_doc({
		"doctype": "File",
		"file_name": filename,
		"attached_to_doctype": doctype,
		"attached_to_name": docname,
		"content": content,
		"is_private": 1,
	})
	file_doc.insert(ignore_permissions=True)
	return file_doc.file_url
