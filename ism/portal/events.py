# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Decoupled Seam 5 shop floor notifications for Project 4 Customer Portal.

NEVER imports ``ism.board``. Emits deduplicated ``Shop Action Item`` records
(``category="General"``, ``source="Agent"``) and ``after_commit=True`` realtime
events so Project 6 (``/board`` / ``/cockpit``) can project alerts softly.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from ism.portal.contracts import (
	PIN_STATUS_OPTED_OUT,
	PIN_STATUS_PROVIDED,
	PIN_STATUS_PURGED,
)

__all__ = [
	"emit_portal_pin_purged_event",
	"emit_portal_pin_submitted_event",
	"emit_portal_quote_approved_event",
	"emit_portal_reschedule_event",
	"emit_portal_schedule_entry_pin_purged_event",
	"emit_portal_schedule_entry_pin_submitted_event",
]


def _upsert_portal_action_item(
	*,
	reference_doctype: str,
	reference_name: str,
	dedup_key: str,
	title: str,
	priority: str,
	details: str,
	match_legacy_unkeyed: bool = False,
) -> str:
	"""Create or update an open Shop Action Item for a Repair Order or Schedule Entry (Seam 5)."""
	existing_name = frappe.db.get_value(
		"Shop Action Item",
		{
			"dedup_key": dedup_key,
			"status": ["in", ["Open", "In Progress"]],
		},
		"name",
	)

	if not existing_name and match_legacy_unkeyed:
		legacy_rows = frappe.get_all(
			"Shop Action Item",
			filters={
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"category": "General",
				"status": ["in", ["Open", "In Progress"]],
			},
			fields=["name", "dedup_key"],
			order_by="modified desc",
			limit=10,
		)
		for row in legacy_rows:
			if not (row.get("dedup_key") or "").strip():
				existing_name = row["name"]
				break

	if existing_name:
		item_doc = frappe.get_doc("Shop Action Item", existing_name)
		item_doc.title = title
		item_doc.category = "General"
		item_doc.priority = priority
		item_doc.source = "Agent"
		item_doc.dedup_key = dedup_key
		item_doc.reference_doctype = reference_doctype
		item_doc.reference_name = reference_name
		item_doc.details = details
		item_doc.save(ignore_permissions=True)
		return item_doc.name

	item_doc = frappe.new_doc("Shop Action Item")
	item_doc.title = title
	item_doc.category = "General"
	item_doc.priority = priority
	item_doc.status = "Open"
	item_doc.source = "Agent"
	item_doc.dedup_key = dedup_key
	item_doc.reference_doctype = reference_doctype
	item_doc.reference_name = reference_name
	item_doc.details = details
	item_doc.insert(ignore_permissions=True)
	return item_doc.name


def _upsert_ro_action_item(
	*,
	ro_name: str,
	dedup_key: str,
	title: str,
	priority: str,
	details: str,
	match_legacy_unkeyed: bool = False,
) -> str:
	"""Create or update an open Shop Action Item for a Repair Order (Seam 5)."""
	return _upsert_portal_action_item(
		reference_doctype="Repair Order",
		reference_name=ro_name,
		dedup_key=dedup_key,
		title=title,
		priority=priority,
		details=details,
		match_legacy_unkeyed=match_legacy_unkeyed,
	)


def emit_portal_quote_approved_event(
	ro_doc: Any,
	normalized_approvals: dict[str, str],
	signer_name: str,
) -> None:
	"""Emit legacy approval realtime, portal realtime, and deduplicated Shop Action Item."""
	frappe.publish_realtime(
		"ism_ro_approval_updated",
		{
			"repair_order": ro_doc.name,
			"status": ro_doc.status,
			"approval_status": ro_doc.approval_status,
			"authorized_by": ro_doc.authorized_by,
			"vehicle": ro_doc.vehicle,
			"customer": ro_doc.customer,
		},
		after_commit=True,
	)

	frappe.publish_realtime(
		"ism_portal_event",
		{
			"event_type": "quote_approved",
			"repair_order": ro_doc.name,
			"approval_status": ro_doc.approval_status,
			"order_status": ro_doc.status,
		},
		after_commit=True,
	)

	decisions = set((normalized_approvals or {}).values())
	has_declined = "Declined" in decisions
	has_approved = "Approved" in decisions
	if ro_doc.approval_status == "Declined" or (has_declined and not has_approved):
		title = _("Customer Declined Estimate for {0}").format(ro_doc.name)
	elif ro_doc.approval_status == "Partially Approved" or (has_approved and has_declined):
		title = _("Customer Partially Approved Estimate for {0}").format(ro_doc.name)
	else:
		title = _("Customer Approved Estimate for {0}").format(ro_doc.name)

	priority = "High" if has_declined else "Medium"
	clean_signer = (signer_name or "").strip() or _("Customer")
	details = _(
		"Customer approval submitted by {0} via portal. Approval Status: {1}. Order Status: {2}."
	).format(clean_signer, ro_doc.approval_status, ro_doc.status)

	_upsert_ro_action_item(
		ro_name=ro_doc.name,
		dedup_key=f"portal:approval:{ro_doc.name}",
		title=title,
		priority=priority,
		details=details,
		match_legacy_unkeyed=True,
	)


def emit_portal_pin_submitted_event(ro_name: str, pin_status: str) -> None:
	"""Upsert a non-sensitive Shop Action Item and emit realtime for PIN-to-Drive status changes.

	Strictly NEVER includes PIN digits in any title, details, or realtime payload.
	"""
	if pin_status == PIN_STATUS_PROVIDED:
		title = _("Customer Provided Vehicle PIN-to-Drive for {0}").format(ro_name)
		details = _(
			"Customer submitted an encrypted PIN-to-Drive via the Customer Portal. "
			"Authorized staff can view it from the Repair Order Workshop menu or Shop Board "
			"while the Repair Order remains active."
		)
	elif pin_status == PIN_STATUS_OPTED_OUT:
		title = _("Customer Opted Out of PIN-to-Drive for {0}").format(ro_name)
		details = _("Customer confirmed that PIN-to-Drive is not enabled on this vehicle.")
	else:
		title = _("Vehicle PIN-to-Drive Status Updated for {0}").format(ro_name)
		details = _("Vehicle PIN-to-Drive status changed to {0}.").format(pin_status)

	_upsert_ro_action_item(
		ro_name=ro_name,
		dedup_key=f"portal:pin:{ro_name}",
		title=title,
		priority="Low",
		details=details,
	)

	frappe.publish_realtime(
		"ism_portal_event",
		{
			"event_type": "pin_updated",
			"repair_order": ro_name,
			"pin_status": pin_status,
		},
		after_commit=True,
	)


def emit_portal_pin_purged_event(ro_name: str, *, reason: str = "Closed / Released") -> None:
	"""Emit non-sensitive realtime notification after a Repair Order PIN is purged."""
	frappe.publish_realtime(
		"ism_portal_event",
		{
			"event_type": "pin_purged",
			"repair_order": ro_name,
			"pin_status": PIN_STATUS_PURGED,
			"reason": reason,
		},
		after_commit=True,
	)


def emit_portal_schedule_entry_pin_submitted_event(se_name: str, pin_status: str) -> None:
	"""Upsert a non-sensitive Shop Action Item and realtime event for Schedule Entry PIN updates.

	Strictly NEVER includes PIN digits in any title, details, or realtime payload.
	"""
	if pin_status == PIN_STATUS_PROVIDED:
		title = _("Customer Provided Vehicle PIN-to-Drive for Appointment {0}").format(se_name)
		details = _(
			"Customer submitted an encrypted PIN-to-Drive for their confirmed appointment. "
			"Authorized staff can view it on the Schedule Entry (or linked Repair Order once checked in)."
		)
	elif pin_status == PIN_STATUS_OPTED_OUT:
		title = _("Customer Opted Out of PIN-to-Drive for Appointment {0}").format(se_name)
		details = _("Customer confirmed that PIN-to-Drive is not enabled on this vehicle.")
	else:
		title = _("Vehicle PIN-to-Drive Status Updated for Appointment {0}").format(se_name)
		details = _("Vehicle PIN-to-Drive status changed to {0}.").format(pin_status)

	_upsert_portal_action_item(
		reference_doctype="Schedule Entry",
		reference_name=se_name,
		dedup_key=f"portal:pin:se:{se_name}",
		title=title,
		priority="Low",
		details=details,
	)

	frappe.publish_realtime(
		"ism_portal_event",
		{
			"event_type": "pin_updated",
			"schedule_entry": se_name,
			"pin_status": pin_status,
		},
		after_commit=True,
	)


def emit_portal_schedule_entry_pin_purged_event(se_name: str, *, reason: str = "Cancelled") -> None:
	"""Emit non-sensitive realtime notification after a Schedule Entry PIN is purged."""
	frappe.publish_realtime(
		"ism_portal_event",
		{
			"event_type": "pin_purged",
			"schedule_entry": se_name,
			"pin_status": PIN_STATUS_PURGED,
			"reason": reason,
		},
		after_commit=True,
	)


def emit_portal_reschedule_event(
	schedule_entry: str,
	repair_order: str | None,
	previous_appointment: dict[str, Any],
	new_appointment: dict[str, Any],
) -> None:
	"""Emit portal realtime event for self-serve reschedules without duplicating P3 Action Items."""
	_ = previous_appointment
	frappe.publish_realtime(
		"ism_portal_event",
		{
			"event_type": "appointment_rescheduled",
			"schedule_entry": schedule_entry,
			"repair_order": repair_order,
			"appointment": new_appointment,
		},
		after_commit=True,
	)
