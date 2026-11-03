# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe

TEST_PREFIX = "_TEST_ISM_"
TEST_VINS = ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456", "5YJSA1E26CF000001"]

PRESERVED_CUSTOMERS = {"Prestiga-Biz"}
PRESERVED_VEHICLES = {"LRW3F7FS2NC5xxxxx", "LRW3F7ET2RC1xxxxx"}
PRESERVED_REPAIR_ORDERS = {"RO-2026-00067", "RO-2026-00105"}
PRESERVED_SALES_INVOICES = {"ACC-SINV-2026-00046", "ACC-SINV-2026-00047"}
PRESERVED_PAYMENT_ENTRIES = {"ACC-PAY-2026-00043", "ACC-PAY-2026-00044"}

TEST_CUST_PREFIXES = ("_TEST", "_Test", "Test", "_Husband", "_Wife", "_Spouse")
TEST_ITEM_PREFIXES = ("_TEST", "_Test", "TEST-", "PART-")
STANDARD_SCHEDULE_ENTRY_TYPES = {
	"Diagnostic",
	"Repair",
	"Maintenance",
	"Meeting",
	"Internal Service",
}


def cleanup_all_test_data(verbose: bool = True):
	"""Purge residual test master and transactional records from the database in reverse FK dependency order."""
	def log(msg):
		if verbose:
			print(msg)

	test_customers = _find_test_customers()

	# Ensure test customers exist temporarily if referenced by orphaned transactions
	# so ERPNext's cancel handlers do not fail with 'Could not find Party'
	stub_customers_created = _ensure_stub_customers_for_orphaned_transactions(test_customers)

	# 1. Clean test Payment Entries
	_cleanup_payment_entries(test_customers, log)

	# 2. Clean test Sales Invoices
	_cleanup_sales_invoices(test_customers, log)

	# 3. Clean test Sales Orders
	_cleanup_sales_orders(test_customers, log)

	# 4. Clean test Quotations
	_cleanup_quotations(test_customers, log)

	# 5. Clean test Schedule Entries (MUST precede Repair Orders and Service Bays)
	_cleanup_schedule_entries(test_customers, log)

	# 6. Clean test Repair Orders
	_cleanup_repair_orders(test_customers, log)

	# 7. Clean test Vehicle Check-ins
	_cleanup_vehicle_checkins(test_customers, log)

	# 8. Clean test Repair Vehicles
	_cleanup_repair_vehicles(test_customers, log)

	# 9. Clean child customer links and Customers
	_cleanup_customers(test_customers, stub_customers_created, log)

	# 10. Clean test Service Bays
	_cleanup_service_bays(log)

	# 11. Clean test Schedule Entry Types
	_cleanup_schedule_entry_types(log)

	# 12. Clean test Service Packages
	_cleanup_service_packages(log)

	# 13. Clean test Items
	_cleanup_items(log)

	# 14. Clean test Files and signatures
	_cleanup_files(log)

	frappe.db.commit()
	log("Cleanup of test data completed successfully.")


def _find_test_customers():
	all_custs = frappe.get_all("Customer", pluck="name")
	test_custs = set()
	for c in all_custs:
		if c in PRESERVED_CUSTOMERS:
			continue
		if any(c.startswith(p) for p in TEST_CUST_PREFIXES) or "Test" in c or "TEST" in c:
			test_custs.add(c)
	return test_custs


def _ensure_stub_customers_for_orphaned_transactions(known_test_customers):
	"""Recreate missing party customers temporarily to permit transaction cancellation."""
	stubs = set()
	for dt, party_field in [("Sales Invoice", "customer"), ("Payment Entry", "party"), ("Repair Order", "customer")]:
		records = frappe.get_all(dt, fields=[party_field])
		for r in records:
			party = r.get(party_field)
			if not party or party in PRESERVED_CUSTOMERS:
				continue
			if any(party.startswith(p) for p in TEST_CUST_PREFIXES) or "Test" in party or "TEST" in party:
				known_test_customers.add(party)
				if not frappe.db.exists("Customer", party):
					try:
						frappe.get_doc({
							"doctype": "Customer",
							"customer_name": party,
							"customer_type": "Individual",
						}).insert(ignore_permissions=True)
						stubs.add(party)
					except Exception:
						pass
	return stubs


def _cleanup_payment_entries(test_customers, log):
	all_pes = frappe.get_all("Payment Entry", fields=["name", "party", "docstatus"])
	for pe in all_pes:
		if pe.name in PRESERVED_PAYMENT_ENTRIES or pe.party in PRESERVED_CUSTOMERS:
			continue
		is_test = pe.party in test_customers or any(pe.party.startswith(p) for p in TEST_CUST_PREFIXES) if pe.party else False
		if is_test:
			try:
				doc = frappe.get_doc("Payment Entry", pe.name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Payment Entry", pe.name, force=True, ignore_permissions=True)
				log(f"Deleted test Payment Entry: {pe.name}")
			except Exception as e:
				log(f"Failed deleting test Payment Entry {pe.name}: {e}")


def _cleanup_sales_invoices(test_customers, log):
	all_sis = frappe.get_all("Sales Invoice", fields=["name", "customer", "docstatus"])
	for si in all_sis:
		if si.name in PRESERVED_SALES_INVOICES or si.customer in PRESERVED_CUSTOMERS:
			continue
		is_test = si.customer in test_customers or any(si.customer.startswith(p) for p in TEST_CUST_PREFIXES) if si.customer else False
		if is_test:
			try:
				# Cancel any linked Payment Entry first
				pe_refs = frappe.get_all(
					"Payment Entry Reference",
					filters={"reference_doctype": "Sales Invoice", "reference_name": si.name},
					pluck="parent",
				)
				for pe_name in set(pe_refs):
					if pe_name not in PRESERVED_PAYMENT_ENTRIES and frappe.db.exists("Payment Entry", pe_name):
						pe_doc = frappe.get_doc("Payment Entry", pe_name)
						if pe_doc.docstatus == 1:
							pe_doc.cancel()
						frappe.delete_doc("Payment Entry", pe_name, force=True, ignore_permissions=True)
				doc = frappe.get_doc("Sales Invoice", si.name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Sales Invoice", si.name, force=True, ignore_permissions=True)
				log(f"Deleted test Sales Invoice: {si.name}")
			except Exception as e:
				log(f"Failed deleting test Sales Invoice {si.name}: {e}")


def _cleanup_sales_orders(test_customers, log):
	all_sos = frappe.get_all("Sales Order", fields=["name", "customer", "docstatus"])
	for so in all_sos:
		if so.customer in PRESERVED_CUSTOMERS:
			continue
		if so.customer in test_customers or (so.customer and any(so.customer.startswith(p) for p in TEST_CUST_PREFIXES)):
			try:
				doc = frappe.get_doc("Sales Order", so.name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
				log(f"Deleted test Sales Order: {so.name}")
			except Exception as e:
				log(f"Failed deleting test Sales Order {so.name}: {e}")


def _cleanup_quotations(test_customers, log):
	fields = ["name", "party_name", "docstatus"]
	has_custom_vehicle = frappe.db.has_column("Quotation", "custom_vehicle")
	if has_custom_vehicle:
		fields.append("custom_vehicle")
	all_qtns = frappe.get_all("Quotation", fields=fields)
	for qtn in all_qtns:
		if qtn.party_name in PRESERVED_CUSTOMERS:
			continue
		is_test = qtn.party_name in test_customers or (has_custom_vehicle and qtn.get("custom_vehicle") in TEST_VINS)
		if is_test or (qtn.party_name and any(qtn.party_name.startswith(p) for p in TEST_CUST_PREFIXES)):
			try:
				doc = frappe.get_doc("Quotation", qtn.name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Quotation", qtn.name, force=True, ignore_permissions=True)
				log(f"Deleted test Quotation: {qtn.name}")
			except Exception as e:
				log(f"Failed deleting test Quotation {qtn.name}: {e}")


def _cleanup_schedule_entries(test_customers, log):
	"""Purge residual test Schedule Entry records before parent Repair Orders and Bays are deleted."""
	if not frappe.db.exists("DocType", "Schedule Entry"):
		return

	fields = [
		"name",
		"repair_order",
		"customer",
		"repair_vehicle",
		"service_bay",
		"entry_type",
		"assigned_technician",
		"provisional_customer_name",
		"provisional_vehicle_info",
		"title",
		"docstatus",
	]
	all_ses = frappe.get_all("Schedule Entry", fields=fields)

	for se in all_ses:
		# Preserve records explicitly linked to preserved master entities
		if se.repair_order and se.repair_order in PRESERVED_REPAIR_ORDERS:
			continue
		if se.customer and se.customer in PRESERVED_CUSTOMERS:
			continue
		if se.repair_vehicle and se.repair_vehicle in PRESERVED_VEHICLES:
			continue

		is_test = False

		# 1. Linked Repair Order checks
		if se.repair_order:
			if not frappe.db.exists("Repair Order", se.repair_order):
				is_test = True
			else:
				ro_data = frappe.db.get_value(
					"Repair Order",
					se.repair_order,
					["customer", "vehicle"],
					as_dict=True,
				)
				if ro_data:
					ro_cust = ro_data.get("customer") or ""
					ro_veh = ro_data.get("vehicle") or ""
					if (
						ro_cust in test_customers
						or any(ro_cust.startswith(p) for p in TEST_CUST_PREFIXES)
						or "Test" in ro_cust
						or "TEST" in ro_cust
						or ro_veh in TEST_VINS
					):
						is_test = True

		# 2. Linked Customer checks
		if se.customer:
			if (
				se.customer in test_customers
				or any(se.customer.startswith(p) for p in TEST_CUST_PREFIXES)
				or "Test" in se.customer
				or "TEST" in se.customer
				or not frappe.db.exists("Customer", se.customer)
			):
				is_test = True

		# 3. Linked Repair Vehicle checks
		if se.repair_vehicle:
			if (
				se.repair_vehicle in TEST_VINS
				or any(se.repair_vehicle.startswith(p) for p in ("_TEST", "TEST", "Test"))
				or not frappe.db.exists("Repair Vehicle", se.repair_vehicle)
			):
				is_test = True

		# 4. Linked Service Bay checks
		if se.service_bay:
			if (
				any(se.service_bay.startswith(p) for p in ("_TEST", "TEST", "Test Unit Bay", "STRESS"))
				or not frappe.db.exists("Service Bay", se.service_bay)
			):
				is_test = True

		# 5. Entry Type checks
		if se.entry_type:
			if (
				any(se.entry_type.startswith(p) for p in ("_TEST", "TEST", "Test", "STRESS"))
				or (se.entry_type not in STANDARD_SCHEDULE_ENTRY_TYPES and not frappe.db.exists("Schedule Entry Type", se.entry_type))
			):
				is_test = True

		# 6. Assigned Technician checks
		if se.assigned_technician:
			if any(se.assigned_technician.startswith(p) for p in ("_TEST", "TEST", "Test", "STRESS")):
				is_test = True

		# 7. Provisional Customer / Vehicle Information checks
		if se.provisional_customer_name:
			if (
				any(se.provisional_customer_name.startswith(p) for p in TEST_CUST_PREFIXES)
				or se.provisional_customer_name in ("Alice Walker", "Provisional Client")
				or "Test" in se.provisional_customer_name
				or "TEST" in se.provisional_customer_name
			):
				is_test = True

		if se.provisional_vehicle_info:
			if any(p in se.provisional_vehicle_info for p in ("Test", "TEST", "Model Y Red", "TEST-INTAKE")):
				is_test = True

		# 8. Title checks
		if se.title:
			if (
				any(se.title.startswith(p) for p in TEST_CUST_PREFIXES)
				or "Alice Walker" in se.title
				or "Provisional Client" in se.title
				or se.title.startswith("Guest")
			):
				is_test = True

		# 9. Standalone unlinked test entries with no RO and no Customer
		if not se.repair_order and not se.customer:
			is_test = True

		if is_test:
			try:
				doc = frappe.get_doc("Schedule Entry", se.name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Schedule Entry", se.name, force=True, ignore_permissions=True)
				log(f"Deleted test Schedule Entry: {se.name}")
			except Exception as e:
				log(f"Failed deleting test Schedule Entry {se.name}: {e}")


def _cleanup_repair_orders(test_customers, log):
	all_ros = frappe.get_all("Repair Order", fields=["name", "customer", "vehicle", "sales_invoice", "docstatus"])
	for ro in all_ros:
		if ro.name in PRESERVED_REPAIR_ORDERS or ro.customer in PRESERVED_CUSTOMERS:
			continue
		is_test = (
			ro.customer in test_customers
			or ro.vehicle in TEST_VINS
			or (ro.customer and any(ro.customer.startswith(p) for p in TEST_CUST_PREFIXES))
		)
		if is_test:
			try:
				# Clean any remaining linked Schedule Entries before RO deletion
				if frappe.db.exists("DocType", "Schedule Entry"):
					linked_entries = frappe.get_all(
						"Schedule Entry",
						filters={"repair_order": ro.name},
						pluck="name",
					)
					for se_name in linked_entries:
						try:
							frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
						except Exception:
							pass

				doc = frappe.get_doc("Repair Order", ro.name)
				if doc.sales_invoice and frappe.db.exists("Sales Invoice", doc.sales_invoice):
					si_doc = frappe.get_doc("Sales Invoice", doc.sales_invoice)
					if si_doc.name not in PRESERVED_SALES_INVOICES:
						pe_refs = frappe.get_all(
							"Payment Entry Reference",
							filters={"reference_doctype": "Sales Invoice", "reference_name": si_doc.name},
							pluck="parent",
						)
						for pe_name in set(pe_refs):
							if pe_name not in PRESERVED_PAYMENT_ENTRIES and frappe.db.exists("Payment Entry", pe_name):
								pe_doc = frappe.get_doc("Payment Entry", pe_name)
								if pe_doc.docstatus == 1:
									pe_doc.cancel()
								frappe.delete_doc("Payment Entry", pe_name, force=True, ignore_permissions=True)
						if si_doc.docstatus == 1:
							si_doc.cancel()
						frappe.delete_doc("Sales Invoice", si_doc.name, force=True, ignore_permissions=True)
					doc.db_set("sales_invoice", None)
					doc.reload()
				elif doc.sales_invoice:
					doc.db_set("sales_invoice", None)
					doc.reload()

				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Repair Order", ro.name, force=True, ignore_permissions=True)
				log(f"Deleted test Repair Order: {ro.name}")
			except Exception as e:
				log(f"Failed deleting test Repair Order {ro.name}: {e}")


def _cleanup_vehicle_checkins(test_customers, log):
	if not frappe.db.exists("DocType", "Vehicle Check-in"):
		return
	all_vcis = frappe.get_all("Vehicle Check-in", fields=["name", "customer", "vehicle"])
	for vci in all_vcis:
		if vci.customer in PRESERVED_CUSTOMERS:
			continue
		if vci.customer in test_customers or vci.vehicle in TEST_VINS:
			try:
				frappe.delete_doc("Vehicle Check-in", vci.name, force=True, ignore_permissions=True)
				log(f"Deleted test Vehicle Check-in: {vci.name}")
			except Exception as e:
				log(f"Failed deleting test Vehicle Check-in {vci.name}: {e}")


def _cleanup_repair_vehicles(test_customers, log):
	for vin in TEST_VINS:
		if vin in PRESERVED_VEHICLES:
			continue
		if frappe.db.exists("Repair Vehicle", vin):
			try:
				frappe.delete_doc("Repair Vehicle", vin, force=True, ignore_permissions=True)
				log(f"Deleted test Repair Vehicle: {vin}")
			except Exception as e:
				log(f"Failed deleting test Repair Vehicle {vin}: {e}")

	all_vehs = frappe.get_all("Repair Vehicle", fields=["name", "customer", "license_plate"])
	for v in all_vehs:
		if v.name in PRESERVED_VEHICLES or v.customer in PRESERVED_CUSTOMERS:
			continue
		if v.customer in test_customers or v.license_plate in ["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"]:
			try:
				frappe.delete_doc("Repair Vehicle", v.name, force=True, ignore_permissions=True)
				log(f"Deleted test Repair Vehicle: {v.name}")
			except Exception as e:
				log(f"Failed deleting test Repair Vehicle {v.name}: {e}")


def _cleanup_customers(test_customers, stub_customers_created, log):
	all_to_clean = test_customers | stub_customers_created
	for cust in all_to_clean:
		if cust in PRESERVED_CUSTOMERS:
			continue
		try:
			# Delete referencing Schedule Entries first
			if frappe.db.exists("DocType", "Schedule Entry"):
				for se_name in frappe.get_all("Schedule Entry", filters={"customer": cust}, pluck="name"):
					try:
						frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
					except Exception:
						pass
			frappe.db.delete("Repair Vehicle Customer", {"customer": cust})
			if frappe.db.exists("Customer", cust):
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


def _cleanup_service_bays(log):
	"""Purge residual test Service Bay records after Schedule Entries have been cleaned."""
	if not frappe.db.exists("DocType", "Service Bay"):
		return
	all_bays = frappe.get_all("Service Bay", fields=["name", "bay_name"])
	for bay in all_bays:
		name = bay.name or bay.bay_name or ""
		if name in ("Test Bay 1", "Test Bay 2", "Test Bay 3"):
			continue
		if (
			any(name.startswith(p) for p in ("_TEST", "TEST-", "Test Unit Bay", "STRESS"))
			or "Test" in name
			or "TEST" in name
		):
			try:
				frappe.delete_doc("Service Bay", bay.name, force=True, ignore_permissions=True)
				log(f"Deleted test Service Bay: {bay.name}")
			except Exception as e:
				log(f"Failed deleting test Service Bay {bay.name}: {e}")


def _cleanup_schedule_entry_types(log):
	"""Purge residual custom Schedule Entry Types after referencing Schedule Entries have been cleaned."""
	if not frappe.db.exists("DocType", "Schedule Entry Type"):
		return
	types = frappe.get_all("Schedule Entry Type", pluck="name")
	for t in types:
		if t in STANDARD_SCHEDULE_ENTRY_TYPES:
			continue
		if any(t.startswith(p) for p in ("_TEST", "TEST", "Test", "STRESS")) or "Test" in t or "TEST" in t:
			try:
				frappe.delete_doc("Schedule Entry Type", t, force=True, ignore_permissions=True)
				log(f"Deleted test Schedule Entry Type: {t}")
			except Exception as e:
				log(f"Failed deleting test Schedule Entry Type {t}: {e}")


def _cleanup_service_packages(log):
	known_packages = [
		"Front Brake Renewal",
		"Engine Oil & Filter Change",
		"Ignition Overhaul Package",
		"test",
		"Door - Front - LH (Remove and Replace)",
	]
	packages = frappe.get_all("Service Package", pluck="name")
	for pkg in packages:
		if pkg in known_packages or any(pkg.startswith(p) for p in TEST_CUST_PREFIXES):
			try:
				frappe.delete_doc("Service Package", pkg, force=True, ignore_permissions=True)
				log(f"Deleted test Service Package: {pkg}")
			except Exception as e:
				log(f"Failed deleting test Service Package {pkg}: {e}")


def _cleanup_items(log):
	known_items = [
		"test",
		"_Test Labor Item",
		"Door - Front - LH (Remove and Replace)",
	]
	items = frappe.get_all("Item", pluck="name")
	for item in items:
		if item == "LABOR-STD":
			continue
		if item in known_items or any(item.startswith(p) for p in TEST_ITEM_PREFIXES):
			try:
				frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
				log(f"Deleted test Item: {item}")
			except Exception as e:
				log(f"Failed deleting test Item {item}: {e}")


def _cleanup_files(log):
	test_files = frappe.get_all(
		"File",
		filters=[["file_name", "like", "%sig_%"]],
		pluck="name",
	) + frappe.get_all(
		"File",
		filters=[["attached_to_name", "like", f"{TEST_PREFIX}%"]],
		pluck="name",
	)
	for f in set(test_files):
		try:
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
			log(f"Deleted test File: {f}")
		except Exception as e:
			log(f"Failed deleting test File {f}: {e}")
