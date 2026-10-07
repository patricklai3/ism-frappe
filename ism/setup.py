import json
import os
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.modules.export_file import export_to_files

AUTOMOTIVE_CUSTOM_FIELDS = {
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
			"insert_after": "order_type",
		},
		{
			"fieldname": "custom_sb_jobs",
			"label": "Automotive Repair Jobs",
			"fieldtype": "Section Break",
			"insert_after": "custom_customer_concern",
		},
		{
			"fieldname": "custom_jobs",
			"label": "Repair Jobs",
			"fieldtype": "Table",
			"options": "Quotation Job",
			"allow_on_submit": 1,
			"insert_after": "custom_sb_jobs",
		},
		{
			"fieldname": "custom_cb_approval",
			"fieldtype": "Column Break",
			"insert_after": "custom_jobs",
		},
		{
			"fieldname": "custom_approval_status",
			"label": "Customer Approval Status",
			"fieldtype": "Select",
			"options": "Draft\nPending Approval\nCustomer Approved\nPartially Approved\nDeclined",
			"default": "Draft",
			"allow_on_submit": 1,
			"insert_after": "custom_cb_approval",
		},
		{
			"fieldname": "custom_approval_token_expiry",
			"label": "Approval Token Expiry",
			"fieldtype": "Datetime",
			"read_only": 1,
			"allow_on_submit": 1,
			"insert_after": "custom_approval_status",
		},
		{
			"fieldname": "custom_cb_supplement",
			"fieldtype": "Column Break",
			"insert_after": "custom_approval_token_expiry",
		},
		{
			"fieldname": "custom_is_supplement",
			"label": "Is Supplement",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "custom_cb_supplement",
		},
		{
			"fieldname": "custom_supplement_for",
			"label": "Supplement For",
			"fieldtype": "Link",
			"options": "Sales Order",
			"insert_after": "custom_is_supplement",
		},
		{
			"fieldname": "custom_approval_token",
			"label": "Approval Token",
			"fieldtype": "Data",
			"hidden": 1,
			"read_only": 1,
			"search_index": 1,
			"allow_on_submit": 1,
			"insert_after": "custom_supplement_for",
		},
	],
	"Quotation Item": [
		{
			"fieldname": "custom_job",
			"label": "Job Row ID",
			"fieldtype": "Data",
			"hidden": 1,
		},
		{
			"fieldname": "custom_job_title",
			"label": "Job Title",
			"fieldtype": "Data",
			"in_list_view": 1,
			"insert_after": "item_code",
		},
	],
	"Sales Order": [
		{
			"fieldname": "custom_vehicle",
			"label": "Vehicle",
			"fieldtype": "Link",
			"options": "Repair Vehicle",
			"insert_after": "customer",
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
			"fieldname": "custom_sb_jobs",
			"label": "Authorized Repair Jobs",
			"fieldtype": "Section Break",
			"insert_after": "delivery_date",
		},
		{
			"fieldname": "custom_jobs",
			"label": "Repair Jobs",
			"fieldtype": "Table",
			"options": "Quotation Job",
			"insert_after": "custom_sb_jobs",
			"allow_on_submit": 1,
		},
	],
	"Sales Order Item": [
		{
			"fieldname": "custom_job",
			"label": "Job Row ID",
			"fieldtype": "Data",
			"hidden": 1,
		},
		{
			"fieldname": "custom_job_title",
			"label": "Job Title",
			"fieldtype": "Data",
			"in_list_view": 1,
			"insert_after": "item_code",
		},
	],
	"Sales Invoice": [
		{
			"fieldname": "custom_vehicle",
			"label": "Vehicle",
			"fieldtype": "Link",
			"options": "Repair Vehicle",
			"insert_after": "customer",
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
			"fieldname": "custom_sb_jobs",
			"label": "Authorized Repair Jobs",
			"fieldtype": "Section Break",
			"insert_after": "due_date",
		},
		{
			"fieldname": "custom_jobs",
			"label": "Repair Jobs",
			"fieldtype": "Table",
			"options": "Quotation Job",
			"insert_after": "custom_sb_jobs",
			"allow_on_submit": 1,
		},
	],
	"Sales Invoice Item": [
		{
			"fieldname": "custom_job",
			"label": "Job Row ID",
			"fieldtype": "Data",
			"hidden": 1,
		},
		{
			"fieldname": "custom_job_title",
			"label": "Job Title",
			"fieldtype": "Data",
			"in_list_view": 1,
			"insert_after": "item_code",
		},
	],
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
	setup_default_ism_settings()
	sync_workspace()


def setup_custom_fields():
	"""Provision automotive custom fields on standard ERPNext DocTypes."""
	create_custom_fields(AUTOMOTIVE_CUSTOM_FIELDS, update=True)


def setup_default_ism_settings():
	"""Initialize default ISM Settings single document."""
	if not frappe.db.exists("DocType", "ISM Settings"):
		return

	settings = frappe.get_single("ISM Settings")
	if not settings.authorization_token_expiry_hours:
		settings.authorization_token_expiry_hours = 48
	if not settings.cost_tolerance_percentage:
		settings.cost_tolerance_percentage = 10.0
	if not settings.cost_tolerance_flat_amount:
		settings.cost_tolerance_flat_amount = 20.0
	settings.save(ignore_permissions=True)


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


def purge_all_test_data(verbose=True):
	"""Purge residual test master and transactional records from the database."""
	from ism.tests.cleanup import cleanup_all_test_data
	cleanup_all_test_data(verbose=verbose)
