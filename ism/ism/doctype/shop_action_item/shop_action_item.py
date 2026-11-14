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

	if reference_doctype or reference_name:
		if (reference_doctype and not isinstance(reference_doctype, str)) or (
			reference_name and not isinstance(reference_name, str)
		):
			frappe.throw(_("Invalid reference document identifier."), frappe.ValidationError)
		if (
			reference_doctype
			and reference_name
			and frappe.db.exists("DocType", reference_doctype)
			and frappe.db.exists(reference_doctype, reference_name)
		):
			frappe.has_permission(reference_doctype, "read", doc=reference_name, throw=True)

	doc = frappe.new_doc("Shop Action Item")
	doc.title = title.strip()
	doc.category = category
	doc.priority = priority
	doc.status = "Open"
	doc.reference_doctype = reference_doctype
	doc.reference_name = reference_name
	doc.item_code = item_code
	doc.reported_by = reported_by or frappe.session.user
	doc.details = details
	doc.insert()
	return doc.name


@frappe.whitelist()
def resolve_action_item(name, resolution_notes=None, **kwargs):
	frappe.has_permission("Shop Action Item", "write", throw=True)
	if not name or not isinstance(name, str):
		frappe.throw(_("Valid Shop Action Item name is required."), frappe.ValidationError)
	if not frappe.db.exists("Shop Action Item", name):
		frappe.throw(_("Action Item {0} not found").format(name), frappe.DoesNotExistError)
	doc = frappe.get_doc("Shop Action Item", name)
	doc.check_permission("write")
	return doc.resolve(resolution_notes=resolution_notes)
