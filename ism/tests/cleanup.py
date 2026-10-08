# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe


def cleanup_all_test_data(verbose: bool = True):
	"""Purge residual test master and transactional records from the database."""
	def log(msg):
		if verbose:
			print(msg)

	# 1. Clean Sales Invoices linked to test records
	test_invoices = frappe.get_all(
		"Sales Invoice",
		filters=[["customer", "like", "_Test%"]],
		pluck="name",
	) + frappe.get_all(
		"Sales Invoice",
		filters=[["customer", "like", "_TEST%"]],
		pluck="name",
	)
	for inv in set(test_invoices):
		try:
			doc = frappe.get_doc("Sales Invoice", inv)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Sales Invoice", inv, force=True, ignore_permissions=True)
			log(f"Deleted test Sales Invoice: {inv}")
		except Exception as e:
			log(f"Failed deleting test Sales Invoice {inv}: {e}")

	# 2. Clean Sales Orders linked to test records
	test_sos = frappe.get_all(
		"Sales Order",
		filters=[["customer", "like", "_Test%"]],
		pluck="name",
	) + frappe.get_all(
		"Sales Order",
		filters=[["customer", "like", "_TEST%"]],
		pluck="name",
	)
	for so in set(test_sos):
		try:
			doc = frappe.get_doc("Sales Order", so)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Sales Order", so, force=True, ignore_permissions=True)
			log(f"Deleted test Sales Order: {so}")
		except Exception as e:
			log(f"Failed deleting test Sales Order {so}: {e}")

	# 3. Clean Quotations linked to test records
	test_qtns = frappe.get_all(
		"Quotation",
		filters=[["party_name", "like", "_Test%"]],
		pluck="name",
	) + frappe.get_all(
		"Quotation",
		filters=[["party_name", "like", "_TEST%"]],
		pluck="name",
	)
	for qtn in set(test_qtns):
		try:
			doc = frappe.get_doc("Quotation", qtn)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Quotation", qtn, force=True, ignore_permissions=True)
			log(f"Deleted test Quotation: {qtn}")
		except Exception as e:
			log(f"Failed deleting test Quotation {qtn}: {e}")

	# 4. Clean test Vehicle Check-ins
	test_vcis = frappe.get_all(
		"Vehicle Check-in",
		filters=[["customer", "like", "_Test%"]],
		pluck="name",
	) + frappe.get_all(
		"Vehicle Check-in",
		filters=[["customer", "like", "_TEST%"]],
		pluck="name",
	)
	for vci in set(test_vcis):
		try:
			frappe.delete_doc("Vehicle Check-in", vci, force=True, ignore_permissions=True)
			log(f"Deleted test Vehicle Check-in: {vci}")
		except Exception as e:
			log(f"Failed deleting test Vehicle Check-in {vci}: {e}")

	# 5. Clean test Repair Vehicles
	test_vins = ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456"]
	vins_by_cust = frappe.get_all(
		"Repair Vehicle",
		filters=[["customer", "like", "_Test%"]],
		pluck="name",
	) + frappe.get_all(
		"Repair Vehicle",
		filters=[["customer", "like", "_TEST%"]],
		pluck="name",
	)
	for vin in set(test_vins + vins_by_cust):
		if frappe.db.exists("Repair Vehicle", vin):
			try:
				frappe.delete_doc("Repair Vehicle", vin, force=True, ignore_permissions=True)
				log(f"Deleted test Repair Vehicle: {vin}")
			except Exception as e:
				log(f"Failed deleting test Repair Vehicle {vin}: {e}")

	# 6. Clean test Customers
	test_customer_patterns = ["_TEST%", "_Test%", "_Husband%", "_Wife%", "_Spouse%"]
	for pat in test_customer_patterns:
		custs = frappe.get_all("Customer", filters=[["name", "like", pat]], pluck="name")
		for cust in custs:
			if cust == "Prestiga-Biz":
				continue
			so_count = frappe.db.count("Sales Order", {"customer": cust})
			si_count = frappe.db.count("Sales Invoice", {"customer": cust})
			if so_count == 0 and si_count == 0:
				try:
					frappe.db.delete("Repair Vehicle Customer", {"customer": cust})
					frappe.delete_doc("Customer", cust, force=True, ignore_permissions=True)
					log(f"Deleted test Customer: {cust}")
				except Exception as e:
					log(f"Failed deleting test Customer {cust}: {e}")

	# Clean any remaining orphaned Repair Vehicle Customer child rows
	try:
		frappe.db.sql("""
			DELETE rvc FROM `tabRepair Vehicle Customer` rvc
			LEFT JOIN `tabCustomer` c ON rvc.customer = c.name
			WHERE c.name IS NULL
		""")
	except Exception:
		pass

	# 7. Clean test Service Packages
	test_packages = frappe.get_all(
		"Service Package",
		filters=[["name", "like", "_TEST%"]],
		pluck="name",
	) + frappe.get_all(
		"Service Package",
		filters=[["name", "like", "_Test%"]],
		pluck="name",
	)
	for pkg in set(test_packages):
		try:
			frappe.delete_doc("Service Package", pkg, force=True, ignore_permissions=True)
			log(f"Deleted test Service Package: {pkg}")
		except Exception as e:
			log(f"Failed deleting test Service Package {pkg}: {e}")

	# 8. Clean test Items
	test_items = frappe.get_all(
		"Item",
		filters=[["item_code", "like", "_TEST%"]],
		pluck="name",
	) + frappe.get_all(
		"Item",
		filters=[["item_code", "like", "_Test%"]],
		pluck="name",
	)
	for item in set(test_items):
		try:
			frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
			log(f"Deleted test Item: {item}")
		except Exception as e:
			log(f"Failed deleting test Item {item}: {e}")

	if frappe.db.exists("Item", "_Test Labor Item"):
		try:
			frappe.delete_doc("Item", "_Test Labor Item", force=True, ignore_permissions=True)
		except Exception:
			pass

	# 9. Clean signature files and test attachments
	test_files = frappe.get_all(
		"File",
		filters=[["attached_to_name", "like", "_TEST%"]],
		pluck="name",
	) + frappe.get_all(
		"File",
		filters=[["attached_to_name", "like", "_Test%"]],
		pluck="name",
	)
	for f in set(test_files):
		try:
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
			log(f"Deleted test File: {f}")
		except Exception as e:
			log(f"Failed deleting test File {f}: {e}")

	# 10. Clean test Repair Orders
	test_ros = frappe.get_all(
		"Repair Order",
		filters=[["customer", "like", "_Test%"]],
		pluck="name",
	) + frappe.get_all(
		"Repair Order",
		filters=[["customer", "like", "_TEST%"]],
		pluck="name",
	)
	for ro in set(test_ros):
		try:
			doc = frappe.get_doc("Repair Order", ro)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Repair Order", ro, force=True, ignore_permissions=True)
			log(f"Deleted test Repair Order: {ro}")
		except Exception as e:
			log(f"Failed deleting test Repair Order {ro}: {e}")

	frappe.db.commit()
	log("Cleanup of test data completed successfully.")
