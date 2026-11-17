# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import base64
import binascii
import hashlib
import json
import re
import secrets
from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, now_datetime

_RASTER_SIGNATURE_RE = re.compile(
	r"^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=_\s-]+)$",
	re.IGNORECASE,
)
_SAFE_SIGNATURE_PATH_RE = re.compile(r"^/(?:private/)?files/[^\s<>\"']+$")
_MAX_SIGNATURE_BYTES = 2 * 1024 * 1024
_VALID_APPROVAL_CHANNELS = {
	"Remote Web Link",
	"In-Person Digital",
	"In-Person Signature",
	"Phone Verbal",
	"Verbal Approval",
	"Written Work Order",
	"Advisor Manual",
}
_VALID_DECISIONS = ("Approved", "Declined")


def is_public_approval_portal_request() -> bool:
	"""Return True when executing inside a token-verified guest approval portal call."""
	return bool(getattr(frappe.flags, "_ism_public_approval_portal", False))


def _get_token_expiry_hours() -> int:
	"""Read authorization token TTL (hours) from ISM Settings with safe positive fallback."""
	try:
		val = cint(frappe.db.get_single_value("ISM Settings", "authorization_token_expiry_hours"))
		if val > 0:
			return min(val, 24 * 30)
	except Exception:
		pass
	return 48


def _match_service_decision(service, approvals) -> Optional[str]:
	"""
	Match customer approval decision from approvals dict against a service by:
	- service.name (child row name, e.g. '0t4pqp90cs')
	- service.idx (1, 2, ...)
	- str(service.idx)
	- service.service_title
	- service.service_package
	"""
	if not approvals or not isinstance(approvals, dict):
		return None

	idx_str = str(service.idx or "")
	name_str = str(service.name or "")
	title_str = str(service.service_title or "").strip()
	pkg_str = str(service.service_package or "").strip()

	keys = [
		name_str,
		idx_str,
		title_str,
		title_str.lower(),
		pkg_str,
		pkg_str.lower(),
		f"Service {idx_str}",
		f"service-{idx_str}",
		f"JOB-{idx_str}",
	]

	for k in keys:
		if k:
			if k in approvals:
				return approvals[k]
			if str(k) in approvals:
				return approvals[str(k)]
	return None


def _validate_and_save_signature(ro, signature_image: Any, is_public_portal: bool) -> Optional[str]:
	"""Validate raster signature data URI (saving as private File) or safe file path."""
	if not signature_image:
		return None
	if not isinstance(signature_image, str):
		frappe.throw(_("Signature image must be a string."), frappe.ValidationError)

	candidate = signature_image.strip()
	if not candidate:
		return None

	if candidate.lower().startswith("data:"):
		match = _RASTER_SIGNATURE_RE.match(candidate)
		if not match:
			frappe.throw(
				_("Invalid signature image format. Only PNG, JPEG, or WebP base64 data URIs are supported."),
				frappe.ValidationError,
			)
		img_ext = "jpg" if match.group(1).lower() == "jpeg" else match.group(1).lower()
		b64_payload = re.sub(r"\s+", "", match.group(2))
		max_b64_len = (_MAX_SIGNATURE_BYTES * 4 // 3) + 16
		if len(b64_payload) > max_b64_len:
			frappe.throw(_("Digital signature exceeds maximum allowed size."), frappe.ValidationError)

		try:
			decoded = base64.b64decode(b64_payload, validate=is_public_portal)
		except (binascii.Error, ValueError):
			frappe.throw(_("Digital signature contains invalid base64 data."), frappe.ValidationError)

		if not decoded or len(decoded) > _MAX_SIGNATURE_BYTES:
			frappe.throw(_("Digital signature data is empty or exceeds size bounds."), frappe.ValidationError)

		from frappe.utils.file_manager import save_file

		file_doc = save_file(
			fname=f"sig_{ro.name}_{int(now_datetime().timestamp())}.{img_ext}",
			content=candidate,
			dt=ro.doctype,
			dn=ro.name,
			decode=True,
			is_private=1,
			df="signature_image",
		)
		return file_doc.file_url

	if ".." in candidate or not _SAFE_SIGNATURE_PATH_RE.match(candidate):
		frappe.throw(_("Invalid signature image reference."), frappe.ValidationError)
	return candidate


def _resolve_audit_client_metadata(kwargs: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
	"""Derive client IP and User-Agent from request context when handling HTTP traffic."""
	req = getattr(frappe.local, "request", None)
	if req is not None:
		client_ip = getattr(frappe.local, "request_ip", None) or getattr(req, "remote_addr", None)
		client_ua = frappe.get_request_header("User-Agent")
	else:
		raw_ip = kwargs.get("ip_address")
		raw_ua = kwargs.get("user_agent")
		client_ip = raw_ip if isinstance(raw_ip, str) else getattr(frappe.local, "request_ip", None)
		client_ua = raw_ua if isinstance(raw_ua, str) else None
	if isinstance(client_ua, str):
		client_ua = client_ua[:255]
	return client_ip, client_ua


def sync_approval_status_to_services(ro) -> None:
	"""Sync service row statuses with header approval_status and recompute parts waiting status."""
	has_approved = any(s.status == "Approved" for s in ro.services)
	has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in ro.services)

	if not ro.approval_status or ro.approval_status == "Draft":
		if not has_approved:
			ro.approval_status = "Estimating"

	for s in ro.services:
		if s.status == "Draft":
			s.status = "Estimating"

	if has_approved and has_pending and not ro.has_value_changed("approval_status"):
		ro.approval_status = "Partially Approved"
		return

	if ro.approval_status == "Approved":
		for s in ro.services:
			if s.status != "Declined":
				s.status = "Approved"
	elif ro.approval_status == "Partially Approved" and not has_approved and ro.services:
		for s in ro.services:
			if s.status not in ["Declined", "Estimating", "Draft", "Pending Approval"]:
				s.status = "Approved"
				break

	ro._update_parts_waiting_status()


def _get_cost_tolerance_limits() -> tuple[float, float]:
	"""Read cost tolerance percentage and flat cap from ISM Settings."""
	try:
		settings = frappe.get_cached_doc("ISM Settings")
		pct = max(0.0, flt(getattr(settings, "cost_tolerance_percentage", 0.0)))
		flat = max(0.0, flt(getattr(settings, "cost_tolerance_flat_amount", 0.0)))
		return pct, flat
	except Exception:
		return 0.0, 0.0


def _compute_max_allowed_amount(authorized_amount: float, pct: float, flat: float) -> float:
	"""Compute maximum allowed total_amount before customer re-authorization is required."""
	base = max(0.0, flt(authorized_amount))
	pct_allowance = base * (pct / 100.0) if pct > 0 else None
	flat_allowance = flat if flat > 0 else None
	if pct_allowance is not None and flat_allowance is not None:
		overrun = min(pct_allowance, flat_allowance)
	elif pct_allowance is not None:
		overrun = pct_allowance
	elif flat_allowance is not None:
		overrun = flat_allowance
	else:
		overrun = 0.0
	return flt(base + overrun, 2)


def _should_enforce_cost_tolerance(ro, service) -> bool:
	"""Return True when an Approved service row has a locked estimate to enforce against."""
	if service.status != "Approved":
		return False
	if flt(service.total_amount, 2) <= 0:
		return False

	raw_auth = getattr(service, "authorized_amount", None)
	if raw_auth is not None and flt(raw_auth, 2) > 0:
		return True

	# Zero-cost warranty/comp job explicitly authorized at $0 whose total later increased.
	# Legacy rows (already Approved with total_amount > 0 and authorized_amount == 0) and
	# direct/manual approvals without authorize_repair_order_jobs remain exempt.
	before = service.get_doc_before_save() if hasattr(service, "get_doc_before_save") else None
	return bool(
		before is not None
		and before.status == "Approved"
		and flt(before.total_amount, 2) == 0.0
		and (getattr(ro, "authorized_at", None) or getattr(ro, "approval_logs", None))
	)


def validate_authorization_guards(ro) -> None:
	"""Enforce authorization blocks before moving beyond draft/pending, entering bay, or completing work."""
	demoted = False
	pct, flat = None, None
	for s in ro.services:
		if _should_enforce_cost_tolerance(ro, s):
			if pct is None:
				pct, flat = _get_cost_tolerance_limits()
			max_allowed = _compute_max_allowed_amount(flt(s.authorized_amount, 2), pct, flat)
			if flt(s.total_amount, 2) > max_allowed:
				s.status = "Pending Approval"
				demoted = True

	if demoted:
		ro.approval_status = _derive_approval_status(ro.services)
		ro.total_billed_hours = sum(flt(s.labor_hours) for s in ro.services if s.status == "Approved")
		if ro.approval_status == "Pending Approval" and ro.status in (
			"Authorized - In Progress",
			"Waiting on Parts",
		):
			ro.status = "Pending Authorization"
		if hasattr(ro, "_calculate_taxes_and_totals"):
			ro._calculate_taxes_and_totals()
		if hasattr(ro, "_update_parts_waiting_status"):
			ro._update_parts_waiting_status()

	has_approved = any(s.status == "Approved" for s in ro.services)
	has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in ro.services)
	if has_pending and ro.approval_status == "Approved":
		ro.approval_status = "Partially Approved" if has_approved else "Pending Approval"

	if ro.approval_status == "Approved":
		if not ro.services:
			has_approved = True
		else:
			for s in ro.services:
				if s.status != "Declined":
					s.status = "Approved"
					has_approved = True
	elif ro.approval_status == "Partially Approved" and not has_approved and ro.services:
		for s in ro.services:
			if s.status not in ["Declined", "Estimating", "Draft", "Pending Approval"]:
				s.status = "Approved"
				has_approved = True
				break

	restricted_statuses = (
		"Authorized - In Progress",
		"Waiting on Parts",
		"Work Completed",
		"Billed / Paid",
		"Closed / Released",
	)
	if ro.status in restricted_statuses and not has_approved:
		frappe.throw(
			_(
				"Cannot advance Repair Order to '{0}' without customer authorization. "
				"Please obtain customer approval before proceeding with repair work."
			).format(ro.status)
		)

	restricted_locations = (
		"In Bay / Being Serviced",
		"Completed / Parked in Lot",
		"Released to Customer",
	)
	if ro.vehicle_location in restricted_locations and not has_approved:
		frappe.throw(
			_(
				"Vehicle cannot be moved to '{0}' without customer authorization on file. "
				"Please obtain customer approval first."
			).format(ro.vehicle_location)
		)

	for s in ro.services:
		if cint(s.is_completed) and s.status != "Approved":
			frappe.throw(
				_(
					"Service '{0}' cannot be completed because its status is '{1}'. "
					"Only Approved services can be completed by technicians."
				).format(s.service_title, s.status)
			)


def _derive_approval_status(services) -> str:
	"""Derive Repair Order approval_status consistently from service row states."""
	approved = [s for s in services if s.status == "Approved"]
	declined = [s for s in services if s.status == "Declined"]
	pending = [s for s in services if s.status in ["Draft", "Estimating", "Pending Approval"]]

	if approved and not pending and not declined:
		return "Approved"
	if approved:
		return "Partially Approved"
	if declined and not pending:
		return "Declined"
	return "Pending Approval"


def issue_approval_token(ro) -> Dict[str, Any]:
	"""Generate token for remote customer signature portal and save atomically via ro.save()."""
	ro.check_permission("write")
	ro.approval_token = secrets.token_urlsafe(32)
	ro.approval_token_expiry = add_to_date(now_datetime(), hours=_get_token_expiry_hours())

	for s in ro.services:
		if s.status in ["Draft", "Estimating"]:
			s.status = "Pending Approval"

	ro.approval_status = _derive_approval_status(ro.services)
	ro.save()
	return {"approval_token": ro.approval_token, "expiry": ro.approval_token_expiry}


def authorize_repair_order_jobs(
	ro,
	approvals=None,
	channel="Remote Web Link",
	signer_name=None,
	signature_image=None,
	notes=None,
	**kwargs,
) -> Dict[str, Any]:
	"""Apply per-service customer decisions (Approved/Declined) and audit evidence atomically."""
	is_public_portal = is_public_approval_portal_request()
	if not getattr(ro.flags, "ignore_permissions", False) and not is_public_portal:
		ro.check_permission("write")

	confirm_all = cint(kwargs.get("confirm_all", 0))
	if isinstance(approvals, dict) and (
		"approvals" in approvals
		or "channel" in approvals
		or "signer_name" in approvals
		or "confirm_all" in approvals
	):
		channel = approvals.get("channel") or channel
		signer_name = approvals.get("signer_name") or signer_name
		signature_image = approvals.get("signature_image") or signature_image
		notes = approvals.get("notes") or notes
		confirm_all = cint(approvals.get("confirm_all", confirm_all))
		approvals = approvals.get("approvals")

	if isinstance(approvals, str):
		try:
			approvals = json.loads(approvals)
		except Exception:
			frappe.throw(_("Invalid approvals JSON payload."), frappe.ValidationError)

	if not approvals:
		if not confirm_all or is_public_portal:
			frappe.throw(
				_("Explicit per-service approval decisions are required."),
				frappe.ValidationError,
			)
	elif not isinstance(approvals, dict):
		frappe.throw(_("Approvals payload must be a JSON object mapping services to decisions."), frappe.ValidationError)
	else:
		invalid_decisions = [v for v in approvals.values() if v not in _VALID_DECISIONS]
		if invalid_decisions:
			frappe.throw(
				_("Invalid approval decision '{0}'. Must be Approved or Declined.").format(invalid_decisions[0]),
				frappe.ValidationError,
			)

	if signature_image is not None and signature_image != "":
		saved_signature = _validate_and_save_signature(ro, signature_image, is_public_portal=is_public_portal)
	else:
		saved_signature = None

	if hasattr(ro, "_calculate_service_totals"):
		ro._calculate_service_totals()

	newly_approved = []
	newly_declined = []
	newly_authorized_amount = 0.0

	if not approvals:
		for s in ro.services:
			if s.status != "Declined":
				if s.status != "Approved":
					newly_approved.append(s)
					newly_authorized_amount += flt(s.total_amount)
					s.authorized_amount = flt(s.total_amount, 2)
				s.status = "Approved"
	else:
		for s in ro.services:
			if s.status in ["Approved", "Declined"]:
				continue

			decision = _match_service_decision(s, approvals)
			if decision is None and len(ro.services) == 1 and len(approvals) == 1:
				decision = next(iter(approvals.values()))

			if decision in _VALID_DECISIONS:
				s.status = decision
				if decision == "Approved":
					s.authorized_amount = flt(s.total_amount, 2)
					newly_approved.append(s)
					newly_authorized_amount += flt(s.total_amount)
				elif decision == "Declined":
					newly_declined.append(s)
					s.is_completed = 0
					s.completed_at = None
					s.completed_by = None

	ro.approval_status = _derive_approval_status(ro.services)
	ro._update_parts_waiting_status()

	current_timestamp = now_datetime()
	if (not getattr(ro, "approval_logs", None) or len(ro.approval_logs) == 0) and ro.authorized_at and ro.authorized_by:
		prior_approved = [s for s in ro.services if s.status == "Approved" and s not in newly_approved]
		if prior_approved:
			ro.append("approval_logs", {
				"approval_datetime": ro.authorized_at,
				"signer_name": ro.authorized_by,
				"channel": "Remote Web Link",
				"authorized_amount": flt(sum(flt(s.total_amount) for s in prior_approved), 2),
				"approved_services": ", ".join(
					f"{s.service_title} (${flt(s.total_amount):,.2f})" for s in prior_approved
				),
				"declined_services": ", ".join(
					s.service_title for s in ro.services if s.status == "Declined" and s not in newly_declined
				)
				or _("None"),
				"signature_image": ro.signature_image,
				"notes": _("Initial customer authorization on file"),
			})

	if newly_approved or newly_declined:
		if signer_name is not None and not isinstance(signer_name, str):
			frappe.throw(_("Signer name must be a string."), frappe.ValidationError)
		if notes is not None and not isinstance(notes, str):
			frappe.throw(_("Notes must be a string."), frappe.ValidationError)

		ro.authorized_by = (signer_name.strip() if isinstance(signer_name, str) and signer_name.strip() else None) or frappe.session.user
		ro.authorized_at = current_timestamp
		if saved_signature is not None:
			ro.signature_image = saved_signature
		if notes:
			ro.verbal_notes = notes

		audit_payload = f"{ro.name}|{ro.authorized_by}|{ro.authorized_at}|{json.dumps(approvals or {}, sort_keys=True)}"
		ro.audit_hash = hashlib.sha256(audit_payload.encode()).hexdigest()

		approved_summary = (
			", ".join(f"{s.service_title} (${flt(s.total_amount):,.2f})" for s in newly_approved) or _("None")
		)
		declined_summary = ", ".join(s.service_title for s in newly_declined) or _("None")
		channel_to_record = channel if channel in _VALID_APPROVAL_CHANNELS else "In-Person Signature"
		client_ip, client_ua = _resolve_audit_client_metadata(kwargs)

		ro.append("approval_logs", {
			"approval_datetime": current_timestamp,
			"signer_name": ro.authorized_by,
			"channel": channel_to_record,
			"authorized_amount": flt(newly_authorized_amount, 2),
			"approved_services": approved_summary,
			"declined_services": declined_summary,
			"signature_image": ro.signature_image,
			"ip_address": client_ip,
			"user_agent": client_ua,
			"notes": notes or ro.verbal_notes,
			"audit_hash": ro.audit_hash,
		})

	ro.save(ignore_permissions=is_public_portal)
	return {
		"status": "success",
		"approval_status": ro.approval_status,
		"order_status": ro.status,
	}
