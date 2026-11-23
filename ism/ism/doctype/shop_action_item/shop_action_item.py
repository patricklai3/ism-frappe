# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now_datetime

from ism.ism.doctype.shop_action_item.clearing import CLEARING_CONDITION_TYPES

_VALID_SOURCES = ("Manual", "Agent", "Board AI", "Rule Snooze")


def _normalize_json_field(value, field_label):
	if value is None:
		return None
	if isinstance(value, (dict, list)):
		return json.dumps(value)
	if isinstance(value, str):
		cleaned = value.strip()
		if not cleaned:
			return None
		try:
			json.loads(cleaned)
		except Exception:
			frappe.throw(_("{0} must be valid JSON.").format(field_label), frappe.ValidationError)
		return cleaned
	frappe.throw(_("{0} must be a JSON string or object.").format(field_label), frappe.ValidationError)


class ShopActionItem(Document):
	def validate(self):
		if self.status in ("Resolved", "Dismissed"):
			if not self.resolved_by:
				self.resolved_by = frappe.session.user
			if not self.resolved_at:
				self.resolved_at = now_datetime()
		elif self.status in ["Open", "In Progress"]:
			self.resolved_by = None
			self.resolved_at = None
			if hasattr(self, "auto_cleared"):
				self.auto_cleared = 0

		if self.source and self.source not in _VALID_SOURCES:
			frappe.throw(
				_("Invalid Action Item source: {0}.").format(self.source),
				frappe.ValidationError,
			)

		if hasattr(self, "clearing_condition_type"):
			ctype = (self.clearing_condition_type or "").strip() or "manual"
			if ctype not in CLEARING_CONDITION_TYPES:
				frappe.throw(
					_("Invalid Clearing Condition Type: {0}.").format(ctype),
					frappe.ValidationError,
				)
			self.clearing_condition_type = ctype

		if hasattr(self, "action_payload_json") and self.action_payload_json is not None:
			self.action_payload_json = _normalize_json_field(
				self.action_payload_json, "Action Payload JSON"
			)

		if hasattr(self, "clearing_condition_json") and self.clearing_condition_json is not None:
			self.clearing_condition_json = _normalize_json_field(
				self.clearing_condition_json, "Clearing Condition JSON"
			)

	@frappe.whitelist()
	def resolve(self, resolution_notes=None):
		self.check_permission("write")
		if resolution_notes is not None and not isinstance(resolution_notes, str):
			frappe.throw(_("Resolution Notes must be a string."), frappe.ValidationError)
		if self.status == "Resolved":
			return {
				"status": "success",
				"already_resolved": True,
				"auto_cleared": cint(getattr(self, "auto_cleared", 0)),
				"resolved_by": self.resolved_by,
				"resolved_at": self.resolved_at,
				"message": _("Action item is already Resolved."),
			}
		self.status = "Resolved"
		if resolution_notes:
			self.resolution_notes = resolution_notes
		self.resolved_by = frappe.session.user
		self.resolved_at = now_datetime()
		self.save()
		return {
			"status": "success",
			"already_resolved": False,
			"auto_cleared": cint(getattr(self, "auto_cleared", 0)),
			"resolved_by": self.resolved_by,
			"resolved_at": self.resolved_at,
			"message": _("Action item marked as Resolved."),
		}


@frappe.whitelist()
def create_shop_action_item(
	title,
	category="Shop Floor Discrepancy",
	priority="High",
	reference_doctype=None,
	reference_name=None,
	item_code=None,
	details=None,
	reported_by=None,
	dedup_key=None,
	source=None,
	snoozed_until=None,
	board_event=None,
	action_payload_json=None,
	clearing_condition_type=None,
	clearing_condition_json=None,
	**kwargs,
):
	frappe.has_permission("Shop Action Item", "create", throw=True)
	if not title or not isinstance(title, str) or not title.strip():
		frappe.throw(_("Action Item title is required."), frappe.ValidationError)

	for field_label, val in (
		("Category", category),
		("Priority", priority),
		("Reference DocType", reference_doctype),
		("Reference Name", reference_name),
		("Item Code", item_code),
		("Details", details),
		("Reported By", reported_by),
		("Deduplication Key", dedup_key),
		("Source", source),
		("Board Event", board_event),
		("Clearing Condition Type", clearing_condition_type),
	):
		if val is not None and not isinstance(val, str):
			frappe.throw(_("{0} must be a string.").format(field_label), frappe.ValidationError)

	clean_source = source.strip() if isinstance(source, str) and source.strip() else None
	if clean_source and clean_source not in _VALID_SOURCES:
		frappe.throw(_("Invalid Action Item source: {0}.").format(clean_source), frappe.ValidationError)

	clean_clearing_type = (
		clearing_condition_type.strip()
		if isinstance(clearing_condition_type, str) and clearing_condition_type.strip()
		else None
	)
	if clean_clearing_type and clean_clearing_type not in CLEARING_CONDITION_TYPES:
		frappe.throw(
			_("Invalid Clearing Condition Type: {0}.").format(clean_clearing_type),
			frappe.ValidationError,
		)

	clean_board_event = (
		board_event.strip() if isinstance(board_event, str) and board_event.strip() else None
	)
	clean_action_payload = _normalize_json_field(action_payload_json, "Action Payload JSON")
	clean_clearing_json = _normalize_json_field(clearing_condition_json, "Clearing Condition JSON")

	clean_dedup_key = dedup_key.strip() if isinstance(dedup_key, str) and dedup_key.strip() else None

	ref_dt = reference_doctype.strip() if isinstance(reference_doctype, str) else ""
	ref_name = reference_name.strip() if isinstance(reference_name, str) else ""
	if bool(ref_dt) != bool(ref_name):
		frappe.throw(
			_("Reference DocType and Reference Name must both be provided."),
			frappe.ValidationError,
		)
	if ref_dt and ref_name:
		if not frappe.db.exists("DocType", ref_dt):
			frappe.throw(_("Invalid Reference DocType: {0}").format(ref_dt), frappe.ValidationError)
		if not frappe.db.exists(ref_dt, ref_name):
			frappe.throw(
				_("{0} {1} not found.").format(ref_dt, ref_name),
				frappe.DoesNotExistError,
			)
		frappe.has_permission(ref_dt, "read", doc=ref_name, throw=True)

	resolved_reported_by = frappe.session.user
	if isinstance(reported_by, str) and reported_by.strip():
		candidate_reporter = reported_by.strip()
		if candidate_reporter != frappe.session.user:
			if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
				frappe.throw(
					_("Cannot specify a different Reported By user."),
					frappe.PermissionError,
				)
			resolved_reported_by = candidate_reporter

	clean_title = title.strip()
	clean_category = category.strip() if isinstance(category, str) and category.strip() else "Shop Floor Discrepancy"
	clean_priority = priority.strip() if isinstance(priority, str) and priority.strip() else "High"
	clean_item_code = item_code.strip() if isinstance(item_code, str) and item_code.strip() else None
	clean_details = details.strip() if isinstance(details, str) and details.strip() else None

	if clean_dedup_key:
		existing_name = frappe.db.get_value(
			"Shop Action Item",
			{"dedup_key": clean_dedup_key, "status": ["in", ["Open", "In Progress"]]},
			"name",
		)
		if existing_name:
			doc = frappe.get_doc("Shop Action Item", existing_name)
			doc.check_permission("write")
			doc.title = clean_title
			doc.category = clean_category
			doc.priority = clean_priority
			doc.reference_doctype = ref_dt or None
			doc.reference_name = ref_name or None
			doc.item_code = clean_item_code
			doc.details = clean_details
			if clean_source:
				doc.source = clean_source
			if snoozed_until is not None:
				doc.snoozed_until = snoozed_until or None
			if clean_board_event is not None:
				doc.board_event = clean_board_event
			if action_payload_json is not None:
				doc.action_payload_json = clean_action_payload
			if clean_clearing_type is not None:
				doc.clearing_condition_type = clean_clearing_type
			if clearing_condition_json is not None:
				doc.clearing_condition_json = clean_clearing_json
			doc.save()
			return doc.name

	doc = frappe.new_doc("Shop Action Item")
	doc.title = clean_title
	doc.category = clean_category
	doc.priority = clean_priority
	doc.status = "Open"
	doc.source = clean_source or "Manual"
	doc.reference_doctype = ref_dt or None
	doc.reference_name = ref_name or None
	doc.item_code = clean_item_code
	doc.dedup_key = clean_dedup_key
	doc.board_event = clean_board_event
	doc.snoozed_until = snoozed_until or None
	doc.reported_by = resolved_reported_by
	doc.details = clean_details
	doc.action_payload_json = clean_action_payload
	doc.clearing_condition_type = clean_clearing_type or "manual"
	doc.clearing_condition_json = clean_clearing_json
	doc.insert()
	return doc.name


@frappe.whitelist()
def resolve_action_item(name, resolution_notes=None, **kwargs):
	frappe.has_permission("Shop Action Item", "write", throw=True)
	if not name or not isinstance(name, str) or not name.strip():
		frappe.throw(_("Valid Shop Action Item name is required."), frappe.ValidationError)
	if resolution_notes is not None and not isinstance(resolution_notes, str):
		frappe.throw(_("Resolution Notes must be a string."), frappe.ValidationError)
	name = name.strip()
	if not frappe.db.exists("Shop Action Item", name):
		frappe.throw(_("Action Item {0} not found").format(name), frappe.DoesNotExistError)
	doc = frappe.get_doc("Shop Action Item", name)
	doc.check_permission("write")
	return doc.resolve(resolution_notes=resolution_notes)
