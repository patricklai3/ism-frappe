import json
import os
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.modules.export_file import export_to_files

QUOTATION_CUSTOM_FIELDS = {
	"Quotation": [
		{
			"fieldname": "custom_vehicle",
			"label": "Vehicle",
			"fieldtype": "Link",
			"options": "Repair Vehicle",
			"insert_after": "party_name",
			"in_standard_filter": 1,
		},
		{
			"fieldname": "custom_vehicle_check_in",
			"label": "Vehicle Check-in",
			"fieldtype": "Link",
			"options": "Vehicle Check-in",
			"insert_after": "custom_vehicle",
		},
		{
			"fieldname": "custom_odometer_in",
			"label": "Odometer In",
			"fieldtype": "Int",
			"insert_after": "custom_vehicle_check_in",
		},
		{
			"fieldname": "custom_customer_concern",
			"label": "Customer Concern",
			"fieldtype": "Small Text",
			"insert_after": "custom_odometer_in",
		},
	]
}

STANDARD_INSPECTION_ITEMS = [
	{"inspection_description": "Manufacturing Certification Label"},
	{"inspection_description": "Front Left Corner"},
	{"inspection_description": "Front Right Corner"},
	{"inspection_description": "Rear Left Corner"},
	{"inspection_description": "Rear Right Corner"},
	{"inspection_description": "Interior (Front Seats)"},
	{"inspection_description": "Interior (Rear Seats)"},
	{"inspection_description": "Dashboard Mileage"},
	{"inspection_description": "Service Mode Alert Page"},
]


def after_migrate():
	"""Run migrations and setup tasks for ISM."""
	setup_custom_fields()
	create_standard_inspection_template()
	sync_workspace()


def setup_custom_fields():
	"""Provision automotive custom fields on standard ERPNext DocTypes."""
	create_custom_fields(QUOTATION_CUSTOM_FIELDS, update=True)


def create_standard_inspection_template():
	"""Provision standard 9-point vehicle walkaround inspection checklist."""
	if not frappe.db.exists("DocType", "Inspection Template"):
		return

	if not frappe.db.exists("Inspection Template", "Standard"):
		doc = frappe.get_doc({
			"doctype": "Inspection Template",
			"template_name": "Standard",
			"items": STANDARD_INSPECTION_ITEMS,
		})
		doc.insert(ignore_permissions=True)


def sync_workspace(workspace_name="ISM"):
	"""Ensure the ISM Workspace and Workspace Sidebar exist and include all module DocTypes."""
	if not frappe.db.exists("Workspace", workspace_name):
		from frappe.model.sync import sync_for
		sync_for("ism")

	if not frappe.db.exists("Workspace", workspace_name):
		return

	ism_doctypes = frappe.get_all(
		"DocType",
		filters={"module": "ISM", "istable": 0},
		pluck="name",
		order_by="name asc",
	)

	workspace = frappe.get_doc("Workspace", workspace_name)
	existing_links = {link.link_to for link in workspace.links if link.type == "Link" and link.link_to}

	card_break_idx = None
	for idx, link in enumerate(workspace.links):
		if link.type == "Card Break" and link.label == "ISM DocTypes":
			card_break_idx = idx
			break

	if card_break_idx is None:
		workspace.append(
			"links",
			{
				"label": "ISM DocTypes",
				"type": "Card Break",
				"link_count": 0,
				"hidden": 0,
				"onboard": 0,
			},
		)
		card_break_idx = len(workspace.links) - 1

	for dt in ism_doctypes:
		if dt not in existing_links:
			workspace.append(
				"links",
				{
					"label": dt,
					"type": "Link",
					"link_type": "DocType",
					"link_to": dt,
					"onboard": 0,
					"hidden": 0,
				},
			)

	ism_card_links = [
		link for link in workspace.links
		if link.type == "Link" and link.link_to in ism_doctypes
	]
	workspace.links[card_break_idx].link_count = len(ism_card_links)
	workspace.save(ignore_permissions=True)

	if frappe.db.exists("Workspace Sidebar", workspace_name):
		sidebar = frappe.get_doc("Workspace Sidebar", workspace_name)
		sidebar_links = {item.link_to for item in sidebar.items if item.link_to}

		has_ism_section = any(
			item.type == "Section Break" and item.label == "ISM DocTypes"
			for item in sidebar.items
		)
		if not has_ism_section and ism_doctypes:
			sidebar.append(
				"items",
				{
					"label": "ISM DocTypes",
					"type": "Section Break",
					"child": 0,
					"collapsible": 1,
					"show_arrow": 0,
				},
			)

		for dt in ism_doctypes:
			if dt not in sidebar_links:
				sidebar.append(
					"items",
					{
						"label": dt,
						"type": "Link",
						"link_type": "DocType",
						"link_to": dt,
						"child": 1,
						"collapsible": 1,
						"icon": "file",
						"show_arrow": 0,
					},
				)

		sidebar.save(ignore_permissions=True)

	if frappe.conf.developer_mode:
		try:
			export_to_files(record_list=[["Workspace", workspace_name]])
		except Exception:
			pass


def sync_doctype_to_workspace(doc, method=None):
	"""Hook triggered on DocType after_insert and on_trash."""
	if doc.module == "ISM" and not doc.istable:
		sync_workspace()
