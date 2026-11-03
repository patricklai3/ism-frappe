import frappe

frappe.init("development.localhost", sites_path="/workspace/development/frappe-bench/sites")
frappe.connect()

print("=== TABLES IN MARIADB ===")
tables = [
	"tabService Bay",
	"tabEquipment Tag",
	"tabService Bay Equipment",
	"tabSchedule Entry Type",
	"tabSchedule Entry",
]
for t in tables:
	exists = frappe.db.sql(f"SHOW TABLES LIKE '{t}'")
	print(f"Table '{t}': {'EXISTS' if exists else 'MISSING'}")

print("\n=== SEEDED EQUIPMENT TAGS ===")
tags = frappe.get_all("Equipment Tag", fields=["name", "tag_name", "description"])
for tag in tags:
	print(f"Tag: {tag.name} -> {tag.description}")

print("\n=== SEEDED SCHEDULE ENTRY TYPES ===")
entry_types = frappe.get_all(
	"Schedule Entry Type",
	fields=["name", "color", "requires_repair_order", "description"],
)
for et in entry_types:
	print(f"Type: {et.name} | color: {et.color} | req_ro: {et.requires_repair_order} | desc: {et.description}")

print("\n=== ISM SETTINGS CONSOLIDATED FIELDS ===")
settings = frappe.get_single("ISM Settings")
fields_to_check = [
	"operating_hours_start",
	"operating_hours_end",
	"break_start",
	"break_end",
	"default_slot_interval",
	"scheduling_horizon_days",
	"holiday_list",
	"technician_designation",
	"enable_technician_capacity",
	"estimation_percentile",
	"estimation_sigma",
	"buffer_padding_minutes",
	"default_diagnostic_duration",
]
for f in fields_to_check:
	print(f"{f} = {getattr(settings, f, 'FIELD_NOT_FOUND')}")

print("\n=== ISM ROLES ===")
roles = frappe.get_all("Role", filters={"name": "ISM Manager"}, fields=["name", "desk_access"])
print(f"ISM Manager Role: {roles}")
