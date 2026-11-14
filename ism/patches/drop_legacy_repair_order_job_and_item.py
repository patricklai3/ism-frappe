import frappe


def _migrate_user_tech_to_employee(table: str, column: str):
	if not frappe.db.table_exists(table) or not frappe.db.has_column(table.replace("tab", "", 1), column):
		return

	rows = frappe.db.sql(
		f"""
		SELECT DISTINCT `{column}` AS tech_val
		FROM `{table}`
		WHERE `{column}` IS NOT NULL AND `{column}` != ''
		""",
		as_dict=True,
	)
	for row in rows:
		tech_val = row.tech_val
		if frappe.db.exists("Employee", tech_val):
			continue
		emp_name = frappe.db.get_value("Employee", {"user_id": tech_val}, "name")
		if emp_name:
			frappe.db.sql(
				f"UPDATE `{table}` SET `{column}` = %s WHERE `{column}` = %s",
				(emp_name, tech_val),
			)


def execute():
	for doctype_name, table_name in (
		("Repair Order Job", "tabRepair Order Job"),
		("Repair Order Item", "tabRepair Order Item"),
	):
		if frappe.db.exists("DocType", doctype_name):
			frappe.delete_doc("DocType", doctype_name, force=True, ignore_permissions=True)
		frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `{table_name}`")

	_migrate_user_tech_to_employee("tabRepair Order Time Log", "technician")
	_migrate_user_tech_to_employee("tabRepair Order Service", "assigned_technician")
