# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class ShopActionItem(Document):
	def validate(self):
		if self.status == "Resolved":
			if not self.resolved_by:
				self.resolved_by = frappe.session.user
			if not self.resolved_at:
				self.resolved_at = now_datetime()
		elif self.status in ["Open", "In Progress"]:
			self.resolved_by = None
			self.resolved_at = None

	@frappe.whitelist()
	def resolve(self, resolution_notes=None):
		self.check_permission("write")
		if resolution_notes is not None and not isinstance(resolution_notes, str):
			frappe.throw(_("Resolution Notes must be a string."), frappe.ValidationError)
		self.status = "Resolved"
		if resolution_notes:
			self.resolution_notes = resolution_notes
		self.resolved_by = frappe.session.user
		self.resolved_at = now_datetime()
		self.save()
		return {"status": "success", "message": _("Action item marked as Resolved.")}


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
	):
		if val is not None and not isinstance(val, str):
			frappe.throw(_("{0} must be a string.").format(field_label), frappe.ValidationError)

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

	doc = frappe.new_doc("Shop Action Item")
	doc.title = title.strip()
	doc.category = category.strip() if isinstance(category, str) and category.strip() else "Shop Floor Discrepancy"
	doc.priority = priority.strip() if isinstance(priority, str) and priority.strip() else "High"
	doc.status = "Open"
	doc.reference_doctype = ref_dt or None
	doc.reference_name = ref_name or None
	doc.item_code = item_code.strip() if isinstance(item_code, str) and item_code.strip() else None
	doc.reported_by = resolved_reported_by
	doc.details = details.strip() if isinstance(details, str) and details.strip() else None
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
