import frappe


def _migrate_user_tech_to_employee(table: str, column: str):
	doctype = table[3:] if table.startswith("tab") else table
	if not frappe.db.table_exists(table) or not frappe.db.has_column(doctype, column):
		return

	tbl = frappe.qb.DocType(doctype)
	col = tbl[column]
	rows = (
		frappe.qb.from_(tbl)
		.select(col.as_("tech_val"))
		.distinct()
		.where(col.isnotnull() & (col != ""))
		.run(as_dict=True)
	)
	for row in rows:
		tech_val = row.tech_val
		if frappe.db.exists("Employee", tech_val):
			continue
		emp_name = frappe.db.get_value("Employee", {"user_id": tech_val}, "name")
		if emp_name:
			frappe.qb.update(tbl).set(col, emp_name).where(col == tech_val).run()


def execute():
	_migrate_user_tech_to_employee("tabRepair Order Time Log", "technician")
	_migrate_user_tech_to_employee("tabRepair Order Service", "assigned_technician")

	for doctype_name, table_name in (
		("Repair Order Job", "tabRepair Order Job"),
		("Repair Order Item", "tabRepair Order Item"),
	):
		if frappe.db.exists("DocType", doctype_name):
			frappe.delete_doc("DocType", doctype_name, force=True, ignore_permissions=True)
		frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `{table_name}`")
