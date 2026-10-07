# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe


def cleanup_all_test_data():
	"""Purge residual test master and transactional records from the database."""
	# 1. Clean Quotations linked to test records
	test_qtns = frappe.get_all(
		"Quotation",
		filters=[["party_name", "like", "%Test%"]],
		pluck="name",
	)
	for qtn in test_qtns:
		try:
			doc = frappe.get_doc("Quotation", qtn)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Quotation", qtn, force=True, ignore_permissions=True)
		except Exception:
			pass

	# 2. Clean test Vehicle Check-ins
	test_vcis = frappe.get_all(
		"Vehicle Check-in",
		filters=[["customer", "like", "%Test%"]],
		pluck="name",
	)
	for vci in test_vcis:
		try:
			frappe.delete_doc("Vehicle Check-in", vci, force=True, ignore_permissions=True)
		except Exception:
			pass

	# 3. Clean test Repair Vehicles
	test_vins = ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456"]
	for vin in test_vins:
		if frappe.db.exists("Repair Vehicle", vin):
			try:
				frappe.delete_doc("Repair Vehicle", vin, force=True, ignore_permissions=True)
			except Exception:
				pass

	# 4. Clean test Customers
	test_customer_patterns = ["_Test%", "%Customer%", "_Husband%", "_Wife%", "_Spouse%"]
	for pat in test_customer_patterns:
		custs = frappe.get_all("Customer", filters=[["name", "like", pat]], pluck="name")
		for cust in custs:
			if cust == "Prestiga-Biz":
				continue
			# Check if customer has actual non-test transactions
			so_count = frappe.db.count("Sales Order", {"customer": cust})
			si_count = frappe.db.count("Sales Invoice", {"customer": cust})
			if so_count == 0 and si_count == 0:
				try:
					frappe.delete_doc("Customer", cust, force=True, ignore_permissions=True)
				except Exception:
					pass

	# 5. Clean test Items
	if frappe.db.exists("Item", "_Test Labor Item"):
		try:
			frappe.delete_doc("Item", "_Test Labor Item", force=True, ignore_permissions=True)
		except Exception:
			pass

	frappe.db.commit()
