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
	"""Ensure the ISM Workspace and Workspace Sidebar exist and match stage-based configuration."""
	workspace_file = frappe.get_app_path("ism", "ism", "workspace", "ism", "ism.json")
	if os.path.exists(workspace_file):
		with open(workspace_file, "r") as f:
			data = json.load(f)

		if not frappe.db.exists("Workspace", workspace_name):
			workspace = frappe.get_doc(data)
			workspace.insert(ignore_permissions=True)
		else:
			workspace = frappe.get_doc("Workspace", workspace_name)
			workspace.content = data.get("content")
			workspace.shortcuts = []
			for sc in data.get("shortcuts", []):
				workspace.append("shortcuts", sc)
			workspace.links = []
			for lnk in data.get("links", []):
				workspace.append("links", lnk)
			workspace.save(ignore_permissions=True)

	sidebar_file = frappe.get_app_path("ism", "workspace_sidebar", "ism.json")
	if os.path.exists(sidebar_file):
		with open(sidebar_file, "r") as f:
			data = json.load(f)

		if not frappe.db.exists("Workspace Sidebar", workspace_name):
			sidebar = frappe.get_doc(data)
			sidebar.insert(ignore_permissions=True)
		else:
			sidebar = frappe.get_doc("Workspace Sidebar", workspace_name)
			sidebar.items = []
			for it in data.get("items", []):
				sidebar.append("items", it)
			sidebar.save(ignore_permissions=True)



def sync_doctype_to_workspace(doc, method=None):
	"""Hook triggered on DocType after_insert and on_trash."""
	if doc.module == "ISM" and not doc.istable:
		sync_workspace()
