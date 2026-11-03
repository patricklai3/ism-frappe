# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import time as _time
import frappe
from datetime import time, timedelta
from frappe.utils import add_days, today

PREFIX = "Test "
TEST_PREFIX = "_TEST_ISM_"

EQUIPMENT_TAGS = ["Lift", "Alignment Rack", "HV Battery Station"]

SERVICE_BAYS = {
	f"{PREFIX}Bay 1": {
		"is_active": 1,
		"description": "Primary service bay with 2-post lift",
		"equipment": [{"equipment_tag": "Lift"}]
	},
	f"{PREFIX}Bay 2": {
		"is_active": 1,
		"description": "Alignment bay with lift and rack",
		"equipment": [{"equipment_tag": "Lift"}, {"equipment_tag": "Alignment Rack"}]
	},
	f"{PREFIX}Bay 3": {
		"is_active": 0,
		"description": "Inactive bay",
		"equipment": []
	}
}

SCHEDULE_ENTRY_TYPES = [
	{"name": "Diagnostic", "color": "#3b82f6", "is_service_appointment": 1},
	{"name": "Repair", "color": "#10b981", "is_service_appointment": 1},
	{"name": "Maintenance", "color": "#f59e0b", "is_service_appointment": 1},
	{"name": "Meeting", "color": "#8b5cf6", "is_service_appointment": 0},
	{"name": "Internal Service", "color": "#6b7280", "is_service_appointment": 0},
]

EMPLOYEES = {
	"tech_active_1": {
		"first_name": f"{PREFIX}Tech",
		"last_name": "One",
		"employee_name": f"{PREFIX}Tech One",
		"status": "Active",
		"designation": "Technician",
		"gender": "Male",
		"date_of_birth": "1990-01-01",
		"date_of_joining": "2020-01-01",
	},
	"tech_active_2": {
		"first_name": f"{PREFIX}Tech",
		"last_name": "Two",
		"employee_name": f"{PREFIX}Tech Two",
		"status": "Active",
		"designation": "Technician",
		"gender": "Female",
		"date_of_birth": "1992-02-02",
		"date_of_joining": "2021-01-01",
	},
	"tech_inactive": {
		"first_name": f"{PREFIX}Tech",
		"last_name": "Three",
		"employee_name": f"{PREFIX}Tech Three",
		"status": "Left",
		"designation": "Technician",
		"gender": "Male",
		"date_of_birth": "1988-03-03",
		"date_of_joining": "2019-01-01",
		"relieving_date": "2022-01-01",
	},
}

ITEMS = {
	f"{PREFIX}SERVICE_001": {
		"item_code": f"{PREFIX}SERVICE_001",
		"item_name": "Test Service 001",
		"item_group": "Services",
		"is_stock_item": 0,
		"custom_frt": 1.5,
		"standard_rate": 150.0,
	},
	f"{PREFIX}EST_ITEM_001": {
		"item_code": f"{PREFIX}EST_ITEM_001",
		"item_name": "Test Brake Pad Front",
		"item_group": "Brake",
		"is_stock_item": 0,
		"custom_frt": 1.0,
		"standard_rate": 100.0,
	},
	f"{PREFIX}EST_ITEM_002": {
		"item_code": f"{PREFIX}EST_ITEM_002",
		"item_name": "Test Brake Rotor Rear",
		"item_group": "Brake",
		"is_stock_item": 0,
		"custom_frt": 1.5,
		"standard_rate": 120.0,
	},
	f"{PREFIX}EST_ITEM_TIRE": {
		"item_code": f"{PREFIX}EST_ITEM_TIRE",
		"item_name": "Test Tire Mount & Balance",
		"item_group": "Tire",
		"is_stock_item": 0,
		"custom_frt": 0.5,
		"standard_rate": 50.0,
	},
	f"{PREFIX}EST_ITEM_ELEC": {
		"item_code": f"{PREFIX}EST_ITEM_ELEC",
		"item_name": "Test High Voltage Harness Diagnostic",
		"item_group": "Electrical",
		"is_stock_item": 0,
		"custom_frt": 2.0,
		"standard_rate": 200.0,
	},
}

CUSTOMER = f"{PREFIX}Customer"
VEHICLE_VIN = "5YJSA1E26CF000001"


def get_default_company():
	return frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed"


def setup_all():
	"""Idempotently seeds all standard test master records for capacity scheduling."""
	ensure_company()
	ensure_customer()
	ensure_repair_vehicle()
	ensure_equipment_tags()
	ensure_service_bays()
	ensure_schedule_entry_types()
	emp_map = ensure_employees()
	ensure_items()
	ensure_ism_settings()
	frappe.db.commit()
	return emp_map


def ensure_company():
	company = get_default_company()
	if not frappe.db.exists("Company", company):
		frappe.get_doc({
			"doctype": "Company",
			"company_name": company,
			"default_currency": "USD",
			"country": "United States",
		}).insert(ignore_permissions=True)


def ensure_customer():
	if not frappe.db.exists("Customer", CUSTOMER):
		frappe.get_doc({
			"doctype": "Customer",
			"customer_name": CUSTOMER,
			"customer_type": "Individual",
		}).insert(ignore_permissions=True)


def ensure_repair_vehicle():
	if not frappe.db.exists("DocType", "Repair Vehicle"):
		return
	if not frappe.db.exists("Repair Vehicle", VEHICLE_VIN):
		frappe.get_doc({
			"doctype": "Repair Vehicle",
			"vin": VEHICLE_VIN,
			"license_plate": "TEST-SCHED",
			"customer": CUSTOMER,
		}).insert(ignore_permissions=True)
	else:
		veh = frappe.get_doc("Repair Vehicle", VEHICLE_VIN)
		if veh.customer != CUSTOMER:
			veh.customer = CUSTOMER
			veh.save(ignore_permissions=True)


def ensure_equipment_tags():
	if not frappe.db.exists("DocType", "Equipment Tag"):
		return
	for tag in EQUIPMENT_TAGS:
		if not frappe.db.exists("Equipment Tag", tag):
			try:
				frappe.get_doc({
					"doctype": "Equipment Tag",
					"tag_name": tag,
				}).insert(ignore_permissions=True)
			except Exception:
				pass


def ensure_service_bays():
	if not frappe.db.exists("DocType", "Service Bay"):
		return
	for bay_name, data in SERVICE_BAYS.items():
		if not frappe.db.exists("Service Bay", bay_name):
			try:
				doc = frappe.get_doc({
					"doctype": "Service Bay",
					"bay_name": bay_name,
					"is_active": data["is_active"],
					"description": data["description"],
				})
				if frappe.db.exists("DocType", "Service Bay Equipment") and "equipment" in data:
					for eq in data["equipment"]:
						doc.append("equipment", eq)
				doc.insert(ignore_permissions=True)
			except Exception:
				pass


def ensure_schedule_entry_types():
	if not frappe.db.exists("DocType", "Schedule Entry Type"):
		return
	for item in SCHEDULE_ENTRY_TYPES:
		if not frappe.db.exists("Schedule Entry Type", item["name"]):
			try:
				frappe.get_doc({
					"doctype": "Schedule Entry Type",
					"entry_type_name": item["name"],
					"color": item["color"],
					"is_service_appointment": item.get("is_service_appointment", 1),
				}).insert(ignore_permissions=True)
			except Exception:
				pass


def ensure_employees():
	emp_map = {}
	company = get_default_company()
	for key, data in EMPLOYEES.items():
		existing = frappe.db.get_value("Employee", {"employee_name": data["employee_name"]}, "name")
		if not existing:
			doc_dict = {
				"doctype": "Employee",
				"first_name": data["first_name"],
				"last_name": data["last_name"],
				"employee_name": data["employee_name"],
				"status": data["status"],
				"gender": data["gender"],
				"date_of_birth": data["date_of_birth"],
				"date_of_joining": data["date_of_joining"],
				"company": company,
			}
			if "designation" in data and frappe.db.has_column("Employee", "designation"):
				# Ensure designation exists
				if not frappe.db.exists("Designation", data["designation"]):
					try:
						frappe.get_doc({"doctype": "Designation", "designation_name": data["designation"]}).insert(ignore_permissions=True)
					except Exception:
						pass
				doc_dict["designation"] = data["designation"]
			if "relieving_date" in data:
				doc_dict["relieving_date"] = data["relieving_date"]
			emp = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
			emp_map[key] = emp.name
		else:
			emp_map[key] = existing
	return emp_map


def ensure_items():
	for item_code, data in ITEMS.items():
		if not frappe.db.exists("Item", item_code):
			doc_dict = {
				"doctype": "Item",
				"item_code": item_code,
				"item_name": data["item_name"],
				"item_group": data.get("item_group", "Services"),
				"is_stock_item": data.get("is_stock_item", 0),
				"standard_rate": data.get("standard_rate", 100.0),
			}
			# Ensure Item Group exists
			ig = data.get("item_group")
			if ig and not frappe.db.exists("Item Group", ig):
				try:
					frappe.get_doc({"doctype": "Item Group", "item_group_name": ig, "parent_item_group": "All Item Groups"}).insert(ignore_permissions=True)
				except Exception:
					pass
			if "custom_frt" in data and frappe.db.has_column("Item", "custom_frt"):
				doc_dict["custom_frt"] = data["custom_frt"]
			try:
				frappe.get_doc(doc_dict).insert(ignore_permissions=True)
			except Exception:
				pass
		else:
			if "custom_frt" in data and frappe.db.has_column("Item", "custom_frt"):
				curr = frappe.db.get_value("Item", item_code, "custom_frt")
				if curr != data["custom_frt"]:
					frappe.db.set_value("Item", item_code, "custom_frt", data["custom_frt"], update_modified=False)


def ensure_ism_settings(**kwargs):
	"""Set up standard scheduling and estimation parameters in ISM Settings."""
	if not frappe.db.exists("DocType", "ISM Settings"):
		return None
	try:
		settings = frappe.get_single("ISM Settings")
		fields = {
			"operating_hours_start": "08:00:00",
			"operating_hours_end": "17:00:00",
			"break_start": "12:00:00",
			"break_end": "12:30:00",
			"default_slot_interval": 30,
			"scheduling_horizon_days": 30,
			"enable_technician_capacity": 1,
			"technician_designation": "Technician",
			"estimation_percentile": 80.0,
			"estimation_sigma": 0.30,
			"buffer_padding_minutes": 0,
			"default_diagnostic_duration": 60,
		}
		fields.update(kwargs)
		for k, v in fields.items():
			if hasattr(settings, k):
				setattr(settings, k, v)
		settings.save(ignore_permissions=True)
		return settings
	except Exception:
		return None


def create_test_schedule_entry(repair_order=None, **kwargs):
	"""Helper to create a transactional Schedule Entry record with 3-attempt restart loop on error 1020."""
	if not frappe.db.exists("DocType", "Schedule Entry"):
		return None
	date = kwargs.pop("scheduled_date", today())
	time_val = kwargs.pop("scheduled_time", "09:00:00")
	duration = kwargs.pop("estimated_duration", 60)
	bay = kwargs.pop("service_bay", f"{PREFIX}Bay 1")
	entry_type = kwargs.pop("entry_type", "Diagnostic")
	status = kwargs.pop("status", "Scheduled")

	for attempt in range(3):
		try:
			data = {
				"doctype": "Schedule Entry",
				"scheduled_date": date,
				"scheduled_time": time_val,
				"estimated_duration": duration,
				"service_bay": bay,
				"entry_type": entry_type,
				"status": status,
				**kwargs,
			}
			if repair_order:
				data["repair_order"] = repair_order
			doc = frappe.get_doc(data).insert(ignore_permissions=True)
			return doc
		except Exception as e:
			if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
				_time.sleep(0.05 * (attempt + 1))
				continue
			raise


def create_test_repair_order(customer=None, vehicle=None, **kwargs):
	"""Helper to create a transactional Repair Order record with 3-attempt restart loop on error 1020."""
	cust = customer or CUSTOMER
	veh = vehicle or VEHICLE_VIN
	odometer_in = kwargs.pop("odometer_in", 50000)

	for attempt in range(3):
		try:
			data = {
				"doctype": "Repair Order",
				"customer": cust,
				"vehicle": veh,
				"odometer_in": odometer_in,
				**kwargs,
			}
			doc = frappe.get_doc(data).insert(ignore_permissions=True)
			return doc
		except Exception as e:
			if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
				_time.sleep(0.05 * (attempt + 1))
				continue
			raise


def teardown_transactional():
	"""Purge transactional test records in FK-safe reverse dependency order."""
	# 1. Clean Schedule Entries with test bays, temp bays, or test repair orders
	if frappe.db.exists("DocType", "Schedule Entry"):
		entries = set(
			frappe.get_all("Schedule Entry", filters=[["service_bay", "like", f"{PREFIX}%"]], pluck="name")
			+ frappe.get_all("Schedule Entry", filters=[["service_bay", "like", f"{TEST_PREFIX}%"]], pluck="name")
			+ frappe.get_all("Schedule Entry", filters=[["service_bay", "like", "TEMP%"]], pluck="name")
			+ frappe.get_all("Schedule Entry", filters=[["customer", "in", [CUSTOMER, f"{TEST_PREFIX}Cust%"]]], pluck="name")
		)
		for e in entries:
			try:
				frappe.delete_doc("Schedule Entry", e, force=True, ignore_permissions=True)
			except Exception:
				pass

	# 2. Clean Leave Applications created for test techs
	if frappe.db.exists("DocType", "Leave Application"):
		leaves = frappe.get_all(
			"Leave Application",
			filters=[["employee", "like", f"{PREFIX}%"]],
			pluck="name",
		)
		for l_name in leaves:
			try:
				doc = frappe.get_doc("Leave Application", l_name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Leave Application", l_name, force=True, ignore_permissions=True)
			except Exception:
				pass

	# 3. Clean Repair Orders created for Test Customer or TEST_PREFIX
	if frappe.db.exists("DocType", "Repair Order"):
		ros = frappe.get_all(
			"Repair Order",
			filters=[["customer", "in", [CUSTOMER, f"{TEST_PREFIX}Cust%"]]],
			pluck="name",
		)
		for ro_name in ros:
			try:
				doc = frappe.get_doc("Repair Order", ro_name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Repair Order", ro_name, force=True, ignore_permissions=True)
			except Exception:
				pass

	frappe.db.rollback()
