# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from unittest.mock import patch

import frappe
from frappe.utils import flt
from ism.tests.base import ISMTestCase, TEST_PREFIX
from ism.api.agent_procurement import _attach_package_parts
from ism.api.parts_catalog import _upsert_part_row_on_package_doc
from ism.api.parts import (
	_get_items_stock_qty_map,
	format_item_code,
	quick_create_part,
	bulk_import_parts,
	get_component_alternates,
	get_default_warehouse,
	link_part_to_service_packages,
	search_service_packages,
	quick_create_service_package,
	parse_slot_number,
	format_slot_label,
	has_lhrh_designation,
	resolve_part_side,
	ensure_supplier_and_link_to_item,
	get_repair_order_procurement_data,
	submit_procurement_orders,
	create_purchase_payment,
	create_purchase_invoice,
	get_item_procurement_info,
	get_repair_order_committed_qty,
	get_repair_order_committed_qty_batch,
	get_item_available_stock,
	get_items_available_stock_batch,
	get_repair_order_parts_stock,
)
from ism.ism.doctype.repair_order.repair_order import (
	get_service_package_details,
	on_stock_update_event,
)


class TestPartsManagement(ISMTestCase):
	"""Test suite for Parts Management, compliance fields, quick creation, and Service Package compatibility."""

	def test_format_item_code(self):
		"""Verify standard [Source]-[PartNumber]-[Condition] formatting."""
		self.assertEqual(format_item_code("OEM", "1044321-00-J", "New"), "OEM-1044321-00-J-NEW")
		self.assertEqual(format_item_code("OEM", "1044321-00-J", "Used"), "OEM-1044321-00-J-USED")
		self.assertEqual(format_item_code("OEM", "1044321-00-J", "Reconditioned"), "OEM-1044321-00-J-RCN")
		self.assertEqual(format_item_code("Aftermarket", "RK623000", "New"), "AFT-RK623000-NEW")
		self.assertEqual(format_item_code("AFT", "RK623000", "Used"), "AFT-RK623000-USED")

	def test_quick_create_part(self):
		"""Verify quick_create_part creates Item with stock defaults and compliance fields."""
		part_num = f"{TEST_PREFIX}P101"
		res = quick_create_part(
			part_number=part_num,
			part_name="Test Upper Control Arm",
			part_source="OEM",
			part_condition="New",
			unit_rate=195.0,
			valuation_rate=120.0,
			warranty_period="12 Months / 20,000 km",
		)

		expected_code = f"OEM-{part_num}-NEW"
		self.assertEqual(res["item_code"], expected_code)
		self.track(frappe.get_doc("Item", expected_code))

		item = frappe.get_doc("Item", expected_code)
		self.assertEqual(item.is_stock_item, 1)
		self.assertEqual(item.is_vehicle_part, 1)
		self.assertEqual(item.part_source, "OEM")
		self.assertEqual(item.part_condition, "New")
		self.assertEqual(item.oem_part_number, part_num)
		self.assertEqual(flt(item.standard_rate), 195.0)
		self.assertEqual(flt(item.valuation_rate), 120.0)
		self.assertEqual(item.warranty_period, "12 Months / 20,000 km")

	def test_bulk_import_parts(self):
		"""Verify bulk TSV import parses lines and creates multiple items."""
		p1 = f"{TEST_PREFIX}BLK1"
		p2 = f"{TEST_PREFIX}BLK2"
		raw_tsv = f"{p1}\tBulk Part 1\tOEM\tNew\t150.0\t90.0\t1\n{p2}\tBulk Part 2\tAFT\tUsed\t75.0\t40.0\t2"

		res = bulk_import_parts(raw_tsv)
		self.assertEqual(res["total_created"], 2)

		code1 = f"OEM-{p1}-NEW"
		code2 = f"AFT-{p2}-USED"
		self.track(frappe.get_doc("Item", code1))
		self.track(frappe.get_doc("Item", code2))

		it1 = frappe.get_doc("Item", code1)
		self.assertEqual(it1.part_source, "OEM")
		self.assertEqual(it1.part_condition, "New")
		self.assertEqual(flt(it1.standard_rate), 150.0)

		it2 = frappe.get_doc("Item", code2)
		self.assertEqual(it2.part_source, "Aftermarket")
		self.assertEqual(it2.part_condition, "Used")
		self.assertEqual(flt(it2.standard_rate), 75.0)

	def test_get_component_alternates(self):
		"""Verify retrieving compatible variants (New vs Used vs Aftermarket) sharing OEM part number."""
		canonical_oem = f"{TEST_PREFIX}CANONICAL"
		p_new = quick_create_part(
			part_number=canonical_oem,
			part_name="Canonical Control Arm (New)",
			part_source="OEM",
			part_condition="New",
			unit_rate=200.0,
		)
		p_used = quick_create_part(
			part_number=canonical_oem,
			part_name="Canonical Control Arm (Used)",
			part_source="OEM",
			part_condition="Used",
			unit_rate=95.0,
		)
		self.track(frappe.get_doc("Item", p_new["item_code"]))
		self.track(frappe.get_doc("Item", p_used["item_code"]))

		alternates = get_component_alternates(oem_part_number=canonical_oem)
		item_codes = [a["item_code"] for a in alternates]
		self.assertIn(p_new["item_code"], item_codes)
		self.assertIn(p_used["item_code"], item_codes)

	def test_service_package_component_slot_and_alternates(self):
		"""Verify Service Package uses component slots, populates default item into RO, and supports alternates."""
		base_num = f"{TEST_PREFIX}PKG_PART"
		item_def = quick_create_part(
			part_number=base_num,
			part_name="Control Arm LH (OEM New)",
			part_source="OEM",
			part_condition="New",
			unit_rate=210.0,
		)
		item_alt = quick_create_part(
			part_number=base_num,
			part_name="Control Arm LH (OEM Used)",
			part_source="OEM",
			part_condition="Used",
			unit_rate=105.0,
		)
		self.track(frappe.get_doc("Item", item_def["item_code"]))
		self.track(frappe.get_doc("Item", item_alt["item_code"]))

		# Create Service Package with component slot
		pkg_name = f"{TEST_PREFIX}Suspension Service"
		if frappe.db.exists("Service Package", pkg_name):
			frappe.delete_doc("Service Package", pkg_name, force=True, ignore_permissions=True)

		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": pkg_name,
			"standard_labor_hours": 1.5,
			"hourly_rate": 120.0,
			"items": [
				{
					"component_slot": "Front Upper Control Arm LH",
					"item_code": item_def["item_code"],
					"item_name": item_def["item_name"],
					"is_default": 1,
					"qty": 1.0,
					"rate": 210.0,
				},
				{
					"component_slot": "Front Upper Control Arm LH",
					"item_code": item_alt["item_code"],
					"item_name": item_alt["item_name"],
					"is_default": 0,
					"qty": 1.0,
					"rate": 105.0,
				},
			],
		}).insert(ignore_permissions=True)
		self.track(pkg)

		# Fetch package details as loaded into a Repair Order
		details = get_service_package_details(pkg_name)
		self.assertEqual(len(details["parts"]), 1, "Only the default variant should be loaded initially")
		self.assertEqual(details["parts"][0]["item_code"], item_def["item_code"])
		self.assertEqual(details["parts"][0]["part_source"], "OEM")
		self.assertEqual(details["parts"][0]["part_condition"], "New")

	def test_sales_invoice_compliance_description(self):
		"""Verify Sales Invoice generated from Repair Order includes legal compliance tag in description."""
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(vin="5YJ3E1EBXNF999999", customer=customer.name)

		p_code = quick_create_part(
			part_number=f"{TEST_PREFIX}LEG_PART",
			part_name="Brake Caliper",
			part_source="OEM",
			part_condition="Reconditioned",
			unit_rate=320.0,
			warranty_period="24 Months / 40,000 km",
		)["item_code"]
		self.track(frappe.get_doc("Item", p_code))

		company = frappe.defaults.get_user_default("Company") or "I"
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": customer.name,
			"vehicle": vehicle.name,
			"company": company,
			"odometer_in": 50000,
			"status": "Authorized - In Progress",
			"services": [
				{
					"service_title": "Brake Caliper Overhaul",
					"labor_hours": 2.0,
					"labor_rate": 120.0,
					"labor_amount": 240.0,
					"status": "Approved",
					"parts": json.dumps([
						{
							"component_slot": "Brake Caliper",
							"item_code": p_code,
							"item_name": "Brake Caliper",
							"part_source": "OEM",
							"part_condition": "Reconditioned",
							"warranty_period": "24 Months / 40,000 km",
							"qty": 1.0,
							"rate": 320.0,
							"amount": 320.0,
						}
					]),
					"parts_total": 320.0,
					"total_amount": 560.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro)

		# Temporarily allow negative stock for test environment
		orig_neg = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		try:
			# Call process_payment_and_release to generate Sales Invoice
			res = ro.process_payment_and_release(
				payment_method="Debit / Interac",
				paid_amount=560.0 * 1.13,
				odometer_out=50005,
			)

			si_name = res.get("sales_invoice")
			self.assertTrue(si_name)
			self.track(frappe.get_doc("Sales Invoice", si_name))

			si = frappe.get_doc("Sales Invoice", si_name)
			part_row = next((it for it in si.items if it.item_code == p_code), None)
			self.assertIsNotNone(part_row)
			self.assertIn("[OEM - Reconditioned", part_row.description)
			self.assertIn("24 Months / 40,000 km", part_row.description)
		finally:
			frappe.db.set_single_value("Stock Settings", "allow_negative_stock", orig_neg)

	def test_get_default_warehouse_company_filtering(self):
		"""Verify get_default_warehouse respects the specified company."""
		company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed EV"
		wh = get_default_warehouse(company)
		self.assertTrue(wh)
		wh_doc = frappe.get_doc("Warehouse", wh)
		self.assertEqual(wh_doc.company, company)
		self.assertEqual(wh_doc.is_group, 0)

	def test_procurement_warehouse_resilience(self):
		"""Verify submit_procurement_orders handles stale / invalid warehouses on RO parts smoothly."""
		company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed EV"
		customer = self.create_test_customer(company=company)
		vehicle = self.create_test_vehicle(customer=customer.name)

		p_code = quick_create_part(
			part_number=f"{TEST_PREFIX}MR01",
			part_name="Test Resilient Arm",
			unit_rate=150.0,
		)["item_code"]
		self.track(frappe.get_doc("Item", p_code))

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": customer.name,
			"vehicle": vehicle.name,
			"company": company,
			"odometer_in": 30000,
			"status": "Authorized - In Progress",
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Suspension Service",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"status": "Approved",
					"parts": json.dumps([
						{
							"item_code": p_code,
							"item_name": "Test Resilient Arm",
							"qty": 2.0,
							"uom": "Nos",
							"rate": 150.0,
							"amount": 300.0,
							"warehouse": "Stores - NONEXISTENT_INVALID",
						}
					]),
					"parts_total": 300.0,
					"total_amount": 400.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro)

		proc_data = get_repair_order_procurement_data(ro.name)
		self.assertEqual(len(proc_data["parts"]), 1)
		res = submit_procurement_orders(
			repair_order=ro.name,
			parts_data=[{"item_code": p_code, "qty": 2.0, "supplier": f"{TEST_PREFIX}Resilient Sup", "order_mode": "Purchase Order"}],
			submit=False,
		)
		self.track("Supplier", f"{TEST_PREFIX}Resilient Sup")
		self.assertEqual(len(res["purchase_orders"]), 1)
		po = frappe.get_doc("Purchase Order", res["purchase_orders"][0])
		self.track(po)
		self.assertEqual(len(po.items), 1)
		self.assertEqual(po.items[0].item_code, p_code)
		self.assertEqual(flt(po.items[0].qty), 2.0)
		self.assertNotEqual(po.items[0].warehouse, "Stores - NONEXISTENT_INVALID")
		self.assertTrue(frappe.db.exists("Warehouse", {"name": po.items[0].warehouse, "company": company, "is_group": 0}))

	def test_link_part_to_multiple_service_packages(self):
		"""Verify linking a single part to multiple Service Packages (e.g. Model 3 LH and Model Y LH)."""
		pkg1 = quick_create_service_package(package_name=f"{TEST_PREFIX}Pkg_M3_LH")
		pkg2 = quick_create_service_package(package_name=f"{TEST_PREFIX}Pkg_MY_LH")
		self.track(frappe.get_doc("Service Package", pkg1["name"]))
		self.track(frappe.get_doc("Service Package", pkg2["name"]))

		part = quick_create_part(
			part_number=f"{TEST_PREFIX}UCA_MULTI",
			part_name="Multi Upper Control Arm",
			unit_rate=185.0,
		)
		self.track(frappe.get_doc("Item", part["item_code"]))

		res = link_part_to_service_packages(
			item_code=part["item_code"],
			service_packages=[pkg1["name"], pkg2["name"]],
			component_slot="Front Upper Control Arm",
			qty=1.0,
			rate=185.0,
		)
		self.assertEqual(res["status"], "success")
		self.assertEqual(res["linked_count"], 2)

		pkg1_doc = frappe.get_doc("Service Package", pkg1["name"])
		pkg2_doc = frappe.get_doc("Service Package", pkg2["name"])

		self.assertIn(part["item_code"], [it.item_code for it in pkg1_doc.items])
		self.assertIn(part["item_code"], [it.item_code for it in pkg2_doc.items])

	def test_auto_sync_repair_order_parts_to_service_package(self):
		"""Verify RO save does not mutate catalog Service Packages, while save_service_as_package explicitly persists parts."""
		pkg = quick_create_service_package(package_name=f"{TEST_PREFIX}Pkg_AutoSync")
		self.track(frappe.get_doc("Service Package", pkg["name"]))

		part = quick_create_part(
			part_number=f"{TEST_PREFIX}SYNC_PART",
			part_name="Sync Test Part",
			unit_rate=220.0,
			part_source="OEM",
			part_condition="New",
		)
		self.track(frappe.get_doc("Item", part["item_code"]))

		# Verify initially not in package
		pkg_doc = frappe.get_doc("Service Package", pkg["name"])
		self.assertNotIn(part["item_code"], [it.item_code for it in pkg_doc.items])

		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)
		company = frappe.defaults.get_user_default("Company") or "Lightspeed EV"

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": customer.name,
			"vehicle": vehicle.name,
			"company": company,
			"odometer_in": 25000,
			"status": "Draft Estimate",
			"services": [
				{
					"service_package": pkg["name"],
					"service_title": pkg["package_name"],
					"labor_hours": 1.0,
					"labor_rate": 120.0,
					"labor_amount": 120.0,
					"parts": json.dumps([
						{
							"component_slot": "Control Arm Assembly",
							"item_code": part["item_code"],
							"item_name": "Sync Test Part",
							"qty": 1.0,
							"rate": 220.0,
							"amount": 220.0,
						}
					]),
					"parts_total": 220.0,
					"total_amount": 340.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro)

		# Saving RO must NOT mutate the source catalog Service Package
		pkg_doc.reload()
		self.assertNotIn(part["item_code"], [it.item_code for it in pkg_doc.items])

		# Explicit save_service_as_package intentionally creates a new catalog package with the parts
		saved_pkg_res = ro.save_service_as_package(
			service_row_id=ro.services[0].name,
			package_name=f"{TEST_PREFIX}Pkg_Saved_{frappe.generate_hash(length=4)}",
		)
		saved_pkg = frappe.get_doc("Service Package", saved_pkg_res["package_name"])
		self.track(saved_pkg)
		synced_item = next((it for it in saved_pkg.items if it.item_code == part["item_code"]), None)
		self.assertIsNotNone(synced_item, "Part must be persisted when explicitly saving Service Package")
		self.assertEqual(flt(synced_item.qty), 1.0)
		self.assertEqual(flt(synced_item.rate), 220.0)
		self.assertEqual(synced_item.part_source, "OEM")
		self.assertEqual(synced_item.part_condition, "New")

	def test_search_and_quick_create_service_package(self):
		"""Verify searching and quick creating Service Packages with vehicle model support."""
		unique_code = f"CORR-{frappe.generate_hash(length=6).upper()}"
		pkg = quick_create_service_package(
			package_name=f"{TEST_PREFIX}SearchPkg_{unique_code}",
			correction_code=unique_code,
			applicable_model="Model 3",
			applicable_generation="Model 3 Jun 2017 - Dec 2023",
			standard_labor_hours=1.8,
			hourly_rate=135.0,
		)
		self.track(frappe.get_doc("Service Package", pkg["name"]))

		# Verify applicable_model saved on doc
		pkg_doc = frappe.get_doc("Service Package", pkg["name"])
		self.assertEqual(pkg_doc.applicable_model, "Model 3")
		self.assertEqual(pkg_doc.applicable_generation, "Model 3 Jun 2017 - Dec 2023")

		# Search by correction code
		res1 = search_service_packages(query=unique_code)
		matched1 = next((p for p in res1 if p["name"] == pkg["name"]), None)
		self.assertIsNotNone(matched1)
		self.assertEqual(matched1["applicable_model"], "Model 3")

		# Search by name fragment
		res2 = search_service_packages(query=f"SearchPkg_{unique_code}")
		matched2 = next((p for p in res2 if p["name"] == pkg["name"]), None)
		self.assertIsNotNone(matched2)
		self.assertEqual(matched2["applicable_model"], "Model 3")

		# Search by Model name
		res3 = search_service_packages(query="Model 3")
		self.assertTrue(any(p["name"] == pkg["name"] for p in res3))

		# Test get_service_package_details returns applicable_model
		details = get_service_package_details(service_package=pkg["name"])
		self.assertEqual(details["applicable_model"], "Model 3")

	def test_upsert_service_package_persists_model(self):
		"""Verify _upsert_service_package extracts and stores applicable_model from Tesla manual URL."""
		from ism.api.tesla import _upsert_service_package

		manual_url = "https://service.tesla.com/docs/ModelY/ServiceManual/en-us/GUID-ABCD-1234.html"
		proc_data = {
			"procedure_name": f"{TEST_PREFIX}Model Y Front Bumper Cover",
			"correction_code": f"CORR-{frappe.generate_hash(length=5).upper()}",
			"frt": 1.5,
			"parts": [],
		}

		pkg = _upsert_service_package(proc_data, manual_url)
		self.track(pkg)
		self.assertEqual(pkg.applicable_model, "Model Y")
		self.assertEqual(pkg.applicable_generation, "Model Y Jan 2020 - Jan 2025")

	def test_backfill_service_package_models(self):
		"""Verify backfill script automatically populates applicable_model from existing tesla_manual_url."""
		from ism.setup import backfill_service_package_models

		test_url = "https://service.tesla.com/docs/ModelS/ServiceManual/en-us/GUID-MS-0001.html"
		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": f"{TEST_PREFIX}Model S Drive Unit",
			"correction_code": f"CORR-{frappe.generate_hash(length=5).upper()}",
			"tesla_manual_url": test_url,
		}).insert(ignore_permissions=True)
		self.track(pkg)

		# Clear model fields to simulate pre-migration state
		frappe.db.set_value("Service Package", pkg.name, {
			"applicable_model": None,
			"applicable_generation": None,
		}, update_modified=False)

		# Run backfill
		backfilled = backfill_service_package_models()
		self.assertGreaterEqual(backfilled, 1)

		pkg.reload()
		self.assertEqual(pkg.applicable_model, "Model S")

	def test_parse_and_format_slot_number(self):
		"""Verify parsing integers and slot labels, formatting standard labels."""
		self.assertEqual(parse_slot_number(1), 1)
		self.assertEqual(parse_slot_number("1"), 1)
		self.assertEqual(parse_slot_number("Slot 2"), 2)
		self.assertEqual(parse_slot_number("slot 3"), 3)
		self.assertEqual(parse_slot_number("Unassigned"), 0)
		self.assertEqual(parse_slot_number("0"), 0)
		self.assertEqual(parse_slot_number(""), 0)
		self.assertEqual(parse_slot_number(None), 0)

		self.assertEqual(format_slot_label(1), "Slot 1")
		self.assertEqual(format_slot_label(2), "Slot 2")
		self.assertEqual(format_slot_label(0), "Unassigned")
		self.assertEqual(format_slot_label(-1), "Unassigned")

	def test_link_part_to_service_packages_multi_service_slots(self):
		"""Verify multi-service link: primary gets chosen slot, secondary gets unassigned."""
		pkg1 = quick_create_service_package(
			package_name=f"{TEST_PREFIX}Model3_Pair_{frappe.generate_hash(length=4)}",
		)
		pkg2 = quick_create_service_package(
			package_name=f"{TEST_PREFIX}ModelY_Pair_{frappe.generate_hash(length=4)}",
		)
		self.track(frappe.get_doc("Service Package", pkg1["name"]))
		self.track(frappe.get_doc("Service Package", pkg2["name"]))

		part = quick_create_part(
			part_number=f"{TEST_PREFIX}MULTI_SLOT_P1",
			part_name="Universal Control Arm",
			slot_number=2,
			service_packages=[pkg1["name"], pkg2["name"]],
		)
		self.track(frappe.get_doc("Item", part["item_code"]))

		# Primary package pkg1 must have slot_number = 2
		doc1 = frappe.get_doc("Service Package", pkg1["name"])
		row1 = next((r for r in doc1.items if r.item_code == part["item_code"]), None)
		self.assertIsNotNone(row1)
		self.assertEqual(row1.slot_number, 2)
		self.assertEqual(row1.component_slot, "Slot 2")

		# Secondary package pkg2 must have slot_number = 0 (Unassigned)
		doc2 = frappe.get_doc("Service Package", pkg2["name"])
		row2 = next((r for r in doc2.items if r.item_code == part["item_code"]), None)
		self.assertIsNotNone(row2)
		self.assertEqual(row2.slot_number, 0)
		self.assertEqual(row2.component_slot, "Unassigned")

	def test_repair_order_sync_permanently_assigns_unassigned_part(self):
		"""Verify that using an unassigned part in a Repair Order permanently assigns it in the Service Package."""
		pkg = quick_create_service_package(
			package_name=f"{TEST_PREFIX}RO_SyncPkg_{frappe.generate_hash(length=4)}",
		)
		pkg_name = pkg["name"]
		self.track(frappe.get_doc("Service Package", pkg_name))

		part = quick_create_part(
			part_number=f"{TEST_PREFIX}RO_SYNC_P1",
			part_name="Bushing to Assign via RO",
			slot_number=0,
			service_package=pkg_name,
		)
		self.track(frappe.get_doc("Item", part["item_code"]))

		# Verify initially unassigned
		pkg_doc = frappe.get_doc("Service Package", pkg_name)
		self.assertEqual(pkg_doc.items[0].slot_number, 0)
		self.assertEqual(pkg_doc.items[0].component_slot, "Unassigned")

		# Create Repair Order using this service and assign part to Slot 2
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)
		company = frappe.defaults.get_user_default("Company") or "Lightspeed EV"

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": customer.name,
			"vehicle": vehicle.name,
			"company": company,
			"odometer_in": 15000,
			"status": "Draft Estimate",
			"services": [
				{
					"service_package": pkg_name,
					"service_title": pkg_doc.package_name,
					"labor_hours": 1.0,
					"labor_rate": 120.0,
					"labor_amount": 120.0,
					"parts": json.dumps([{
						"slot_number": 2,
						"component_slot": "Slot 2",
						"item_code": part["item_code"],
						"item_name": part["item_name"],
						"qty": 1.0,
						"rate": 150.0,
						"amount": 150.0,
					}]),
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro)

		# Verify RO-level slot assignment does not mutate the underlying Service Package catalog template
		pkg_doc.reload()
		self.assertEqual(pkg_doc.items[0].slot_number, 0)
		self.assertEqual(pkg_doc.items[0].component_slot, "Unassigned")
		ro_parts = json.loads(ro.services[0].parts)
		self.assertEqual(ro_parts[0]["component_slot"], "Slot 2")

	def test_get_service_package_details_excludes_unassigned_from_default_parts(self):
		"""Verify get_service_package_details excludes unassigned parts from active parts list."""
		pkg = quick_create_service_package(
			package_name=f"{TEST_PREFIX}DetailsPkg_{frappe.generate_hash(length=4)}",
		)
		pkg_name = pkg["name"]
		self.track(frappe.get_doc("Service Package", pkg_name))

		# Slot 1 part
		p_assigned = quick_create_part(
			part_number=f"{TEST_PREFIX}ASSIGNED_P1",
			part_name="Assigned Slot 1 Part",
			slot_number=1,
			service_package=pkg_name,
		)
		self.track(frappe.get_doc("Item", p_assigned["item_code"]))

		# Unassigned part
		p_unassigned = quick_create_part(
			part_number=f"{TEST_PREFIX}UNASSIGNED_P2",
			part_name="Unassigned Compatible Part",
			slot_number=0,
			service_package=pkg_name,
		)
		self.track(frappe.get_doc("Item", p_unassigned["item_code"]))

		details = get_service_package_details(service_package=pkg_name)
		active_parts = details["parts"]
		unassigned_parts = details.get("unassigned_parts", [])

		self.assertEqual(len(active_parts), 1)
		self.assertEqual(active_parts[0]["item_code"], p_assigned["item_code"])
		self.assertEqual(active_parts[0]["slot_number"], 1)

		self.assertEqual(len(unassigned_parts), 1)
		self.assertEqual(unassigned_parts[0]["item_code"], p_unassigned["item_code"])
		self.assertEqual(unassigned_parts[0]["slot_number"], 0)

	def test_has_lhrh_designation(self):
		"""Verify LH/RH detection correctly identifies Left Hand/Right Hand but ignores driver/passenger side."""
		self.assertTrue(has_lhrh_designation("Hub - Front - LH (Dual Motor) (Remove and Replace)"))
		self.assertTrue(has_lhrh_designation("Door - Front - LH (Remove and Replace)"))
		self.assertTrue(has_lhrh_designation("Mirror - Side - RH"))
		self.assertTrue(has_lhrh_designation("Front Door - Left Hand"))
		self.assertTrue(has_lhrh_designation("Front Door - Right Hand"))

		# Driver side / passenger side must NOT be treated as LH/RH designation
		self.assertFalse(has_lhrh_designation("Cabin Air Filter (Replace)"))
		self.assertFalse(has_lhrh_designation("Driver's Seat Motor (Remove and Replace)"))
		self.assertFalse(has_lhrh_designation("Wiper Blade - Driver Side"))
		self.assertFalse(has_lhrh_designation("Passenger's Side Mirror Glass"))
		self.assertFalse(has_lhrh_designation("Left and Right Wheel Bearing and Hub Assembly Set 2 Piece"))
		self.assertFalse(has_lhrh_designation("Steering Rack (Left Hand Drive)"))

	def test_resolve_part_side(self):
		"""Verify resolve_part_side normalizes side and never returns 'None'."""
		# Never return "None"
		self.assertEqual(resolve_part_side(side="None"), "Both")
		self.assertEqual(resolve_part_side(side=None), "Both")
		self.assertEqual(resolve_part_side(side=""), "Both")

		# Non-LHRH services always resolve to "Both"
		self.assertEqual(resolve_part_side(side="None", service_package="Cabin Air Filter (Replace)"), "Both")
		self.assertEqual(resolve_part_side(side="LH", service_package="Cabin Air Filter (Replace)"), "Both")
		self.assertEqual(resolve_part_side(side="None", service_package="Driver's Seat Track (Replace)"), "Both")

		# LHRH service
		lh_pkg = "Hub - Front - LH (Dual Motor) (Remove and Replace)"
		self.assertEqual(resolve_part_side(side="LH", service_package=lh_pkg), "LH")
		self.assertEqual(resolve_part_side(side="None", service_package=lh_pkg), "Both")
		self.assertEqual(resolve_part_side(side="Both", service_package=lh_pkg), "Both")

		# Part explicit side takes precedence on LHRH service
		self.assertEqual(resolve_part_side(side="Both", service_package=lh_pkg, part_name="Wheel Bearing LH"), "LH")
		self.assertEqual(resolve_part_side(side="Both", service_package=lh_pkg, part_name="Wheel Bearing RH"), "RH")
		self.assertEqual(resolve_part_side(side="None", service_package=lh_pkg, part_name="Left and Right Wheel Bearing Set"), "Both")

	def test_quick_create_part_with_none_side_on_non_lhrh_service(self):
		"""Verify importing a part with side='None' on a non-LHRH service does not crash and defaults to 'Both'."""
		pkg = quick_create_service_package(
			package_name=f"{TEST_PREFIX}Cabin_Filter_{frappe.generate_hash(length=4)}",
		)
		pkg_name = pkg["name"]
		self.track(frappe.get_doc("Service Package", pkg_name))

		# Import part with side="None"
		res = quick_create_part(
			part_number=f"{TEST_PREFIX}CABIN_FILT_01",
			part_name="High Efficiency Cabin Filter Element",
			service_package=pkg_name,
			side="None",
		)
		self.track(frappe.get_doc("Item", res["item_code"]))
		self.assertEqual(res["side"], "Both")

		pkg_doc = frappe.get_doc("Service Package", pkg_name)
		self.assertEqual(len(pkg_doc.items), 1)
		self.assertEqual(pkg_doc.items[0].side, "Both")

	def _get_company(self):
		return frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed EV"

	def test_ensure_supplier_and_link_to_item(self):
		"""Verify ensure_supplier_and_link_to_item creates supplier and populates Item.supplier_items child table."""
		sup_name = f"{TEST_PREFIX}Supplier_{frappe.generate_hash(length=6)}"
		part = quick_create_part(
			part_number=f"{TEST_PREFIX}SP_TEST_01",
			part_name="Supplier Link Test Part",
			unit_rate=125.0,
		)
		p_code = part["item_code"]
		self.track(frappe.get_doc("Item", p_code))

		# Link new supplier
		res_sup = ensure_supplier_and_link_to_item(sup_name, p_code, supplier_part_no="VEND-XYZ-01")
		self.assertEqual(res_sup, sup_name)
		self.assertTrue(frappe.db.exists("Supplier", sup_name))
		self.track(frappe.get_doc("Supplier", sup_name))

		item_doc = frappe.get_doc("Item", p_code)
		self.assertTrue(any(row.supplier == sup_name and row.supplier_part_no == "VEND-XYZ-01" for row in item_doc.supplier_items))

		# Calling again should not duplicate child table row
		ensure_supplier_and_link_to_item(sup_name, p_code)
		item_doc.reload()
		supplier_rows = [r for r in item_doc.supplier_items if r.supplier == sup_name]
		self.assertEqual(len(supplier_rows), 1)

	def test_submit_procurement_orders_po_and_rfq(self):
		"""Verify direct creation and submission of Purchase Orders (grouped by supplier) and RFQs."""
		company = self._get_company()
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)

		p1 = quick_create_part(part_number=f"{TEST_PREFIX}PO_P1", part_name="PO Part 1", unit_rate=80.0)
		p2 = quick_create_part(part_number=f"{TEST_PREFIX}PO_P2", part_name="PO Part 2", unit_rate=120.0)
		p3 = quick_create_part(part_number=f"{TEST_PREFIX}RFQ_P3", part_name="RFQ High Value Part", unit_rate=1500.0)
		self.track(frappe.get_doc("Item", p1["item_code"]))
		self.track(frappe.get_doc("Item", p2["item_code"]))
		self.track(frappe.get_doc("Item", p3["item_code"]))

		sup_a = f"{TEST_PREFIX}Sup_A_{frappe.generate_hash(length=4)}"
		sup_b = f"{TEST_PREFIX}Sup_B_{frappe.generate_hash(length=4)}"

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": vehicle.name,
			"odometer_in": 12000,
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Suspension & Sensor Service",
					"status": "Approved",
					"labor_hours": 2.0,
					"labor_rate": 120.0,
					"labor_amount": 240.0,
					"parts": json.dumps([
						{"item_code": p1["item_code"], "item_name": "PO Part 1", "qty": 1.0, "rate": 80.0},
						{"item_code": p2["item_code"], "item_name": "PO Part 2", "qty": 2.0, "rate": 120.0},
						{"item_code": p3["item_code"], "item_name": "RFQ Part 3", "qty": 1.0, "rate": 1500.0},
					]),
					"parts_total": 1820.0,
					"total_amount": 2060.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro)

		# Check procurement data extraction
		proc_data = get_repair_order_procurement_data(ro.name)
		self.assertEqual(len(proc_data["parts"]), 3)

		parts_payload = [
			{"item_code": p1["item_code"], "qty": 1.0, "rate": 80.0, "supplier": sup_a, "order_mode": "Purchase Order"},
			{"item_code": p2["item_code"], "qty": 2.0, "rate": 120.0, "supplier": sup_a, "order_mode": "Purchase Order"},
			{"item_code": p3["item_code"], "qty": 1.0, "rate": 1500.0, "supplier": sup_b, "order_mode": "Request for Quotation"},
		]

		res = submit_procurement_orders(ro.name, parts_payload)
		self.track("Supplier", sup_a)
		self.track("Supplier", sup_b)
		self.assertEqual(res["status"], "success")
		self.assertEqual(len(res["purchase_orders"]), 1)
		self.assertEqual(len(res["rfqs"]), 1)

		po_name = res["purchase_orders"][0]
		po = frappe.get_doc("Purchase Order", po_name)
		self.track(po)
		self.assertEqual(po.docstatus, 1)  # Immediately submitted
		self.assertEqual(po.supplier, sup_a)
		self.assertEqual(len(po.items), 2)
		self.assertEqual(getattr(po, "repair_order", None), ro.name)
		self.assertTrue(po.taxes_and_charges and ("13" in po.taxes_and_charges or "HST" in po.taxes_and_charges))
		self.assertAlmostEqual(flt(po.net_total), 320.0, places=2)
		self.assertAlmostEqual(flt(po.total_taxes_and_charges), 41.60, places=2)
		self.assertAlmostEqual(flt(po.grand_total), 361.60, places=2)

		rfq_name = res["rfqs"][0]
		rfq = frappe.get_doc("Request for Quotation", rfq_name)
		self.track(rfq)
		self.assertEqual(rfq.docstatus, 1)  # Immediately submitted
		self.assertEqual(len(rfq.suppliers), 1)
		self.assertEqual(rfq.suppliers[0].supplier, sup_b)
		self.assertEqual(len(rfq.items), 1)
		self.assertEqual(getattr(rfq, "repair_order", None), ro.name)

		# Verify suppliers were linked into Item's supplier_items child table
		p1_doc = frappe.get_doc("Item", p1["item_code"])
		p3_doc = frappe.get_doc("Item", p3["item_code"])
		self.assertTrue(any(row.supplier == sup_a for row in p1_doc.supplier_items))
		self.assertTrue(any(row.supplier == sup_b for row in p3_doc.supplier_items))

	def test_waiting_on_parts_status_assignment_and_override(self):
		"""Verify zero-stock parts automatically set 'Waiting on Parts', block pickup, reject override, and unblock on stock receipt."""
		company = self._get_company()
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)

		p = quick_create_part(part_number=f"{TEST_PREFIX}ZERO_STOCK_P", part_name="Zero Stock Part", unit_rate=200.0)
		p_code = p["item_code"]
		self.track(frappe.get_doc("Item", p_code))

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": vehicle.name,
			"odometer_in": 15000,
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Heavy Mechanical Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": p_code, "item_name": "Zero Stock Part", "qty": 1.0, "rate": 200.0}
					]),
					"parts_total": 200.0,
					"total_amount": 300.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro)

		# Since p_code has zero stock, status must automatically be "Waiting on Parts"
		self.assertEqual(ro.status, "Waiting on Parts")

		# Direct pick_up_vehicle must be strictly blocked when stock is missing
		with self.assertRaises(frappe.ValidationError):
			ro.pick_up_vehicle()

		# Still in Waiting on Parts and Not in bay
		ro.reload()
		self.assertEqual(ro.status, "Waiting on Parts")
		self.assertNotEqual(ro.vehicle_location, "In Bay / Being Serviced")

		# Once parts are received into inventory, status transitions and pick-up succeeds
		wh = get_default_warehouse(company)
		se = frappe.get_doc({
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": company,
			"items": [
				{
					"item_code": p_code,
					"qty": 2.0,
					"t_warehouse": wh,
					"basic_rate": 200.0,
				}
			],
		}).insert(ignore_permissions=True)
		se.submit()
		self.track(se)

		on_stock_update_event(se)
		ro.reload()
		self.assertEqual(ro.status, "Authorized - In Progress")

		# Now vehicle pick-up into bay succeeds
		pickup_res = ro.pick_up_vehicle()
		self.assertEqual(pickup_res["status"], "success")
		self.assertEqual(ro.vehicle_location, "In Bay / Being Serviced")

	def test_on_stock_update_event_clears_waiting_on_parts(self):
		"""Verify on_stock_update_event clears 'Waiting on Parts' when parts become available."""
		company = self._get_company()
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)

		# Create an RO with zero-stock part
		p = quick_create_part(part_number=f"{TEST_PREFIX}STOCK_ARRIVE_P", part_name="Arriving Part", unit_rate=95.0)
		p_code = p["item_code"]
		self.track(frappe.get_doc("Item", p_code))

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": vehicle.name,
			"odometer_in": 18000,
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Brake Replacement",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": p_code, "item_name": "Arriving Part", "qty": 2.0, "rate": 95.0}
					]),
					"parts_total": 190.0,
					"total_amount": 290.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro)
		self.assertEqual(ro.status, "Waiting on Parts")

		# Create a Stock Entry to add stock
		wh = get_default_warehouse(company)
		se = frappe.get_doc({
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": company,
			"items": [
				{
					"item_code": p_code,
					"qty": 5.0,
					"t_warehouse": wh,
					"basic_rate": 95.0,
				}
			],
		}).insert(ignore_permissions=True)
		se.submit()
		self.track(se)

		# Trigger stock update event
		on_stock_update_event(se)

		ro.reload()
		self.assertEqual(ro.status, "Authorized - In Progress")

	def test_purchase_invoice_with_update_stock_clears_waiting_on_parts(self):
		"""Verify submitting a Purchase Invoice with update_stock=1 triggers hook and clears 'Waiting on Parts'."""
		company = self._get_company()
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)

		p = quick_create_part(part_number=f"{TEST_PREFIX}PINV_P", part_name="PINV Arriving Part", unit_rate=60.0)
		p_code = p["item_code"]
		self.track(frappe.get_doc("Item", p_code))

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": vehicle.name,
			"odometer_in": 19000,
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Cooling Valve Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": p_code, "item_name": "PINV Arriving Part", "qty": 1.0, "rate": 60.0}
					]),
					"parts_total": 60.0,
					"total_amount": 160.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro)
		self.assertEqual(ro.status, "Waiting on Parts")

		# Create supplier & Purchase Invoice with update_stock=1
		sup_name = f"{TEST_PREFIX}PINV_Sup_{frappe.generate_hash(length=4)}"
		ensure_supplier_and_link_to_item(sup_name, p_code)
		self.track(frappe.get_doc("Supplier", sup_name))

		self.assertTrue(get_default_warehouse(company))
		pinv = frappe.get_doc({
			"doctype": "Purchase Invoice",
			"company": company,
			"supplier": sup_name,
			"update_stock": 1,
			"items": [
				{
					"item_code": p_code,
					"qty": 2.0,
					"rate": 60.0,
					"expense_account": frappe.db.get_value("Company", company, "default_expense_account") or "Cost of Goods Sold - L",
					"cost_center": frappe.db.get_value("Company", company, "cost_center") or "Main - L",
				}
			],
		}).insert(ignore_permissions=True)
		pinv.submit()
		self.track(pinv)

		ro.reload()
		self.assertEqual(ro.status, "Authorized - In Progress")

	def test_stock_allocation_across_repair_orders(self):
		"""Verify that stock committed to an earlier active approved RO is deducted from available stock for later ROs."""
		company = self._get_company()
		customer = self.create_test_customer()
		vehicle1 = self.create_test_vehicle(customer=customer.name)
		vehicle2 = self.create_test_vehicle(customer=customer.name)

		p = quick_create_part(part_number=f"{TEST_PREFIX}ALLOC_P", part_name="Allocated Widget", unit_rate=80.0)
		p_code = p["item_code"]
		self.track(frappe.get_doc("Item", p_code))
		wh = get_default_warehouse(company)

		# 1. Receive exactly 1 unit into warehouse
		se1 = frappe.get_doc({
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": company,
			"items": [
				{
					"item_code": p_code,
					"qty": 1.0,
					"t_warehouse": wh,
					"basic_rate": 80.0,
				}
			],
		}).insert(ignore_permissions=True)
		se1.submit()
		self.track(se1)

		# 2. Create first Repair Order RO1 and approve it
		ro1 = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": vehicle1.name,
			"odometer_in": 10000,
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Primary Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": p_code, "item_name": "Allocated Widget", "qty": 1.0, "rate": 80.0, "warehouse": wh}
					]),
					"parts_total": 80.0,
					"total_amount": 180.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro1)

		# RO1 should be in progress because 1 unit is available and allocated to it
		self.assertEqual(ro1.status, "Authorized - In Progress")
		stock_ro1 = get_item_available_stock(p_code, wh, ro1.name)
		self.assertEqual(stock_ro1["available_qty"], 1.0)
		self.assertEqual(stock_ro1["committed_qty"], 0.0)

		# 3. Create second Repair Order RO2 (initially in Estimating)
		ro2 = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": vehicle2.name,
			"odometer_in": 12000,
			"approval_status": "Estimating",
			"services": [
				{
					"service_title": "Secondary Service",
					"status": "Estimating",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": p_code, "item_name": "Allocated Widget", "qty": 1.0, "rate": 80.0, "warehouse": wh}
					]),
					"parts_total": 80.0,
					"total_amount": 180.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro2)

		# For RO2, the single unit is already committed to RO1, so available is 0!
		stock_ro2 = get_item_available_stock(p_code, wh, ro2.name)
		self.assertEqual(stock_ro2["available_qty"], 0.0)
		self.assertEqual(stock_ro2["committed_qty"], 1.0)
		self.assertEqual(len(stock_ro2["committed_orders"]), 1)
		self.assertEqual(stock_ro2["committed_orders"][0]["repair_order"], ro1.name)

		# 4. When customer approves RO2, it must transition to 'Waiting on Parts'!
		ro2.approval_status = "Approved"
		ro2.save(ignore_permissions=True)
		self.assertEqual(ro2.status, "Waiting on Parts")

		# Check procurement data for RO2
		proc_data = get_repair_order_procurement_data(ro2.name)
		self.assertEqual(len(proc_data["parts"]), 1)
		self.assertEqual(proc_data["parts"][0]["is_deficit"], 1)
		self.assertEqual(proc_data["parts"][0]["stock_qty"], 0.0)
		self.assertEqual(proc_data["parts"][0]["committed_qty"], 1.0)

		# 5. Receive 1 more unit into stock (total actual = 2.0)
		se2 = frappe.get_doc({
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": company,
			"items": [
				{
					"item_code": p_code,
					"qty": 1.0,
					"t_warehouse": wh,
					"basic_rate": 80.0,
				}
			],
		}).insert(ignore_permissions=True)
		se2.submit()
		self.track(se2)

		# Trigger stock arrival hook
		on_stock_update_event(se2)

		# RO2 should now automatically advance to 'Authorized - In Progress'!
		ro2.reload()
		self.assertEqual(ro2.status, "Authorized - In Progress")

		# And stock check for RO2 now reports 1 available
		stock_ro2_after = get_item_available_stock(p_code, wh, ro2.name)
		self.assertEqual(stock_ro2_after["available_qty"], 1.0)

	def test_get_repair_order_parts_stock_endpoint(self):
		"""Verify get_repair_order_parts_stock endpoint returns live stock mapping for parts in an RO."""
		company = self._get_company()
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)

		p1 = quick_create_part(part_number=f"{TEST_PREFIX}EP1", part_name="Endpoint Part 1", unit_rate=25.0)
		p2 = quick_create_part(part_number=f"{TEST_PREFIX}EP2", part_name="Endpoint Part 2", unit_rate=35.0)
		self.track(frappe.get_doc("Item", p1["item_code"]))
		self.track(frappe.get_doc("Item", p2["item_code"]))

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": vehicle.name,
			"odometer_in": 15000,
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Dual Part Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": p1["item_code"], "qty": 1.0, "rate": 25.0},
						{"item_code": p2["item_code"], "qty": 2.0, "rate": 35.0},
					]),
					"parts_total": 95.0,
					"total_amount": 195.0,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(ro)

		res = get_repair_order_parts_stock(ro_name=ro.name)
		self.assertIn(p1["item_code"], res)
		self.assertIn(p2["item_code"], res)
		self.assertIn("available_qty", res[p1["item_code"]])
		self.assertIn("committed_qty", res[p1["item_code"]])

	def test_default_13_percent_tax_profiles_and_rules(self):
		"""Verify 13% HST Sales & Purchase templates, item tax template, and Tax Rules are created and set as default."""
		from ism.setup import (
			setup_default_taxes_and_payments,
			get_or_create_default_purchase_tax_template,
			get_or_create_default_sales_tax_template,
		)
		setup_default_taxes_and_payments()

		company = self._get_company()

		# Sales Template
		s_tmpl = get_or_create_default_sales_tax_template(company)
		self.assertTrue(s_tmpl)
		self.assertIn("13", s_tmpl)
		self.assertEqual(frappe.db.get_value("Sales Taxes and Charges Template", s_tmpl, "is_default"), 1)

		# Purchase Template
		p_tmpl = get_or_create_default_purchase_tax_template(company)
		self.assertTrue(p_tmpl)
		self.assertIn("13", p_tmpl)
		self.assertEqual(frappe.db.get_value("Purchase Taxes and Charges Template", p_tmpl, "is_default"), 1)

		# Tax Rules
		sales_rules = frappe.get_all(
			"Tax Rule",
			filters={"company": company, "tax_type": "Sales", "customer": ["in", ["", None]]},
		)
		self.assertTrue(len(sales_rules) > 0)

		purchase_rules = frappe.get_all(
			"Tax Rule",
			filters={"company": company, "tax_type": "Purchase", "supplier": ["in", ["", None]]},
		)
		self.assertTrue(len(purchase_rules) > 0)

		# Accounts Settings
		self.assertEqual(
			frappe.db.get_single_value("Accounts Settings", "add_taxes_from_taxes_and_charges_template"),
			1,
		)

	def test_existing_company_tax_setup_preserved_without_override(self):
		"""Verify existing company tax templates, Tax Rules, and Accounts Settings are preserved untouched."""
		from ism.setup import (
			get_or_create_default_purchase_tax_template,
			get_or_create_default_sales_tax_template,
			setup_default_taxes_and_payments,
		)

		company = self._get_company()
		abbr = frappe.db.get_value("Company", company, "abbr") or company
		default_hst_name = f"13% HST - {abbr}"
		tax_account = (
			frappe.db.get_value("Account", {"company": company, "account_type": "Tax", "is_group": 0}, "name")
			or frappe.db.get_value("Account", {"company": company, "root_type": "Liability", "is_group": 0}, "name")
			or frappe.db.get_value("Account", {"company": company, "is_group": 0}, "name")
		)
		self.assertTrue(tax_account)

		savepoint = "test_preserve_existing_taxes"
		frappe.db.savepoint(savepoint)
		try:
			for dt_name in ("Sales Taxes and Charges Template", "Purchase Taxes and Charges Template"):
				tbl = frappe.qb.DocType(dt_name)
				frappe.qb.update(tbl).set(tbl.disabled, 1).set(tbl.is_default, 0).where(
					tbl.company == company
				).run()

			frappe.db.delete("Tax Rule", {"company": company})

			had_hst_sales = bool(frappe.db.exists("Sales Taxes and Charges Template", default_hst_name))
			had_hst_purchase = bool(frappe.db.exists("Purchase Taxes and Charges Template", default_hst_name))

			custom_sales_tmpl = frappe.get_doc({
				"doctype": "Sales Taxes and Charges Template",
				"title": f"{TEST_PREFIX}Acct HST Sales",
				"company": company,
				"is_default": 1,
				"disabled": 0,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": tax_account,
						"rate": 13.0,
						"description": "Custom HST Sales @ 13.0%",
					}
				],
			}).insert(ignore_permissions=True)

			custom_purchase_tmpl = frappe.get_doc({
				"doctype": "Purchase Taxes and Charges Template",
				"title": f"{TEST_PREFIX}Acct HST ITC",
				"company": company,
				"is_default": 1,
				"disabled": 0,
				"taxes": [
					{
						"category": "Total",
						"add_deduct_tax": "Add",
						"charge_type": "On Net Total",
						"account_head": tax_account,
						"rate": 13.0,
						"description": "Custom HST ITC @ 13.0%",
					}
				],
			}).insert(ignore_permissions=True)

			custom_rule = frappe.get_doc({
				"doctype": "Tax Rule",
				"tax_type": "Sales",
				"company": company,
				"sales_tax_template": custom_sales_tmpl.name,
				"priority": 1,
				"from_date": "2020-01-01",
				"use_for_shopping_cart": 0,
			}).insert(ignore_permissions=True)

			frappe.db.set_single_value("Accounts Settings", "add_taxes_from_taxes_and_charges_template", 0)

			setup_default_taxes_and_payments(only_if_missing=False)
			s_res = get_or_create_default_sales_tax_template(company)
			p_res = get_or_create_default_purchase_tax_template(company)

			self.assertEqual(s_res, custom_sales_tmpl.name)
			self.assertEqual(p_res, custom_purchase_tmpl.name)
			self.assertEqual(
				frappe.db.get_value("Sales Taxes and Charges Template", custom_sales_tmpl.name, "is_default"),
				1,
			)
			self.assertEqual(
				frappe.db.get_value("Purchase Taxes and Charges Template", custom_purchase_tmpl.name, "is_default"),
				1,
			)

			if had_hst_sales:
				self.assertEqual(
					frappe.db.get_value("Sales Taxes and Charges Template", default_hst_name, "disabled"),
					1,
				)
				self.assertEqual(
					frappe.db.get_value("Sales Taxes and Charges Template", default_hst_name, "is_default"),
					0,
				)
			else:
				self.assertFalse(frappe.db.exists("Sales Taxes and Charges Template", default_hst_name))

			if had_hst_purchase:
				self.assertEqual(
					frappe.db.get_value("Purchase Taxes and Charges Template", default_hst_name, "disabled"),
					1,
				)
				self.assertEqual(
					frappe.db.get_value("Purchase Taxes and Charges Template", default_hst_name, "is_default"),
					0,
				)
			else:
				self.assertFalse(frappe.db.exists("Purchase Taxes and Charges Template", default_hst_name))

			self.assertEqual(
				frappe.db.get_value("Tax Rule", custom_rule.name, "sales_tax_template"),
				custom_sales_tmpl.name,
			)
			self.assertEqual(
				frappe.db.get_single_value("Accounts Settings", "add_taxes_from_taxes_and_charges_template"),
				0,
			)
		finally:
			frappe.db.rollback(save_point=savepoint)
			frappe.clear_cache(doctype="Accounts Settings")

	def test_bulk_import_parts_structured_json(self):
		"""Verify bulk_import_parts accepts structured list[dict] and JSON string payloads with aliases, supplier linking, and error reporting."""
		pkg = quick_create_service_package(
			package_name=f"{TEST_PREFIX}StructImpPkg_{frappe.generate_hash(length=4)}",
		)
		pkg_name = pkg["name"]
		self.track(frappe.get_doc("Service Package", pkg_name))

		sup_name = f"{TEST_PREFIX}CatalogSup_{frappe.generate_hash(length=4)}"
		p1 = f"{TEST_PREFIX}STR1"
		p2 = f"{TEST_PREFIX}STR2"
		p3 = f"{TEST_PREFIX}STR3"

		payload = [
			{
				"part_number": p1,
				"part_name": "Front Brake Caliper Reman",
				"part_source": "OEM",
				"part_condition": "Reman",
				"unit_rate": 180.0,
				"valuation_rate": 110.0,
				"qty": 3,
				"revision": "B",
				"oem_part_number": "1044621-00-B",
			},
			{
				"sku": p2,
				"description": "Aftermarket Rotor Kit",
				"source": "AFT",
				"condition": "New",
				"rate": 95.5,
				"cost": 55.0,
				"quantity": 2,
				"supplier_part_no": "SUP-ROT-02",
			},
			{
				"part_name": "Missing Part Number Row",
			},
		]

		res = bulk_import_parts(
			items=payload,
			service_package=pkg_name,
			supplier=sup_name,
		)
		self.track(frappe.get_doc("Supplier", sup_name))
		self.assertEqual(res["total_created"], 2)
		self.assertEqual(len(res["errors"]), 1)
		self.assertEqual(res["errors"][0]["index"], 3)

		code1 = f"OEM-{p1}-RCN"
		code2 = f"AFT-{p2}-NEW"
		it1 = frappe.get_doc("Item", code1)
		it2 = frappe.get_doc("Item", code2)
		self.track(it1)
		self.track(it2)

		self.assertEqual(it1.part_source, "OEM")
		self.assertEqual(it1.part_condition, "Reconditioned")
		self.assertEqual(it1.revision, "B")
		self.assertEqual(flt(it1.standard_rate), 180.0)
		self.assertEqual(it2.part_source, "Aftermarket")
		self.assertEqual(flt(it2.valuation_rate), 55.0)
		self.assertIn(sup_name, [r.supplier for r in it2.supplier_items])
		sup_row = next(r for r in it2.supplier_items if r.supplier == sup_name)
		self.assertEqual(sup_row.supplier_part_no, "SUP-ROT-02")

		pkg_doc = frappe.get_doc("Service Package", pkg_name)
		pkg_row1 = next(r for r in pkg_doc.items if r.item_code == code1)
		pkg_row2 = next(r for r in pkg_doc.items if r.item_code == code2)
		self.assertEqual(flt(pkg_row1.qty), 3.0)
		self.assertEqual(pkg_row1.is_default, 1)
		self.assertEqual(flt(pkg_row2.qty), 2.0)

		# Also verify raw JSON string input works identically
		json_res = bulk_import_parts(
			items=json.dumps([
				{
					"part_number": p3,
					"part_name": "JSON String Part",
					"part_source": "OEM",
					"part_condition": "Used",
					"unit_rate": 60.0,
				}
			])
		)
		self.assertEqual(json_res["total_created"], 1)
		code3 = f"OEM-{p3}-USED"
		self.track(frappe.get_doc("Item", code3))
		self.assertEqual(json_res["created"][0]["item_code"], code3)
		self.assertEqual(json_res["created"][0]["part_condition"], "Used")

	def test_fifo_batch_allocation_parity_and_warehouse_isolation(self):
		"""Verify batched FIFO stock calculations match single-item functions, preserve FIFO ordering, and isolate warehouses."""
		company = self._get_company()
		customer = self.create_test_customer()
		v1 = self.create_test_vehicle(customer=customer.name)
		v2 = self.create_test_vehicle(customer=customer.name)
		v3 = self.create_test_vehicle(customer=customer.name)

		p1 = quick_create_part(part_number=f"{TEST_PREFIX}BATCH_P1", part_name="Batch Part 1", unit_rate=50.0)
		p2 = quick_create_part(part_number=f"{TEST_PREFIX}BATCH_P2", part_name="Batch Part 2", unit_rate=70.0)
		c1, c2 = p1["item_code"], p2["item_code"]
		self.track(frappe.get_doc("Item", c1))
		self.track(frappe.get_doc("Item", c2))

		wh1 = get_default_warehouse(company)
		parent_wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 1}, "name")
		wh2_doc = frappe.get_doc({
			"doctype": "Warehouse",
			"warehouse_name": f"{TEST_PREFIX}AltWH_{frappe.generate_hash(length=4)}",
			"company": company,
			"is_group": 0,
			**({"parent_warehouse": parent_wh} if parent_wh else {}),
		}).insert(ignore_permissions=True)
		self.track(wh2_doc)
		wh2 = wh2_doc.name

		for item_code, qty, target_wh in ((c1, 5.0, wh1), (c1, 10.0, wh2), (c2, 4.0, wh1)):
			se = frappe.get_doc({
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": company,
				"items": [{"item_code": item_code, "qty": qty, "t_warehouse": target_wh, "basic_rate": 50.0}],
			}).insert(ignore_permissions=True)
			se.submit()
			self.track(se)

		ro1 = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": v1.name,
			"odometer_in": 10000,
			"approval_status": "Partially Approved",
			"services": [
				{
					"service_title": "RO1 Approved Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": c1, "qty": 2.0, "rate": 50.0, "warehouse": wh1},
						{"item_code": c2, "qty": 1.0, "rate": 70.0, "warehouse": wh1},
					]),
					"parts_total": 170.0,
					"total_amount": 270.0,
				},
				{
					"service_title": "RO1 Unapproved Estimating Service",
					"status": "Estimating",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": c1, "qty": 99.0, "rate": 50.0, "warehouse": wh1},
					]),
					"parts_total": 4950.0,
					"total_amount": 5050.0,
				},
			],
		}).insert(ignore_permissions=True)
		self.track(ro1)

		ro2 = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": v2.name,
			"odometer_in": 12000,
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "RO2 Approved Dual WH Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": c1, "qty": 1.0, "rate": 50.0, "warehouse": wh1},
						{"item_code": c1, "qty": 2.0, "rate": 50.0, "warehouse": wh2},
					]),
					"parts_total": 150.0,
					"total_amount": 250.0,
				},
			],
		}).insert(ignore_permissions=True)
		self.track(ro2)

		ro_est = frappe.get_doc({
			"doctype": "Repair Order",
			"company": company,
			"customer": customer.name,
			"vehicle": v3.name,
			"odometer_in": 14000,
			"approval_status": "Estimating",
			"services": [
				{
					"service_title": "RO3 Estimating Service",
					"status": "Estimating",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"labor_amount": 100.0,
					"parts": json.dumps([
						{"item_code": c1, "qty": 1.0, "rate": 50.0},
						{"item_code": c2, "qty": 1.0, "rate": 70.0},
					]),
					"parts_total": 120.0,
					"total_amount": 220.0,
				},
			],
		}).insert(ignore_permissions=True)
		self.track(ro_est)

		# Ensure unapproved Estimating RO commits nothing to prior RO2, and RO2 only sees RO1
		ro2_batch = get_items_available_stock_batch([c1, c2], wh1, ro2.name)
		self.assertEqual(ro2_batch[c1]["committed_qty"], 2.0)
		self.assertEqual(ro2_batch[c1]["available_qty"], 3.0)
		self.assertEqual([o["repair_order"] for o in ro2_batch[c1]["committed_orders"]], [ro1.name])

		# Estimating RO sees RO1 + RO2 strictly ordered FIFO for wh1
		est_batch = get_items_available_stock_batch([c1, c2], wh1, ro_est.name)
		self.assertEqual(est_batch[c1]["actual_qty"], 5.0)
		self.assertEqual(est_batch[c1]["committed_qty"], 3.0)
		self.assertEqual(est_batch[c1]["available_qty"], 2.0)
		self.assertEqual([o["repair_order"] for o in est_batch[c1]["committed_orders"]], [ro1.name, ro2.name])
		self.assertEqual(est_batch[c2]["actual_qty"], 4.0)
		self.assertEqual(est_batch[c2]["committed_qty"], 1.0)
		self.assertEqual(est_batch[c2]["available_qty"], 3.0)

		# Parity check against single-item helpers and whitelisted endpoint
		ro_parts_stock = get_repair_order_parts_stock(ro_name=ro_est.name, items=[c1, c2], company=company)
		committed_batch = get_repair_order_committed_qty_batch([c1, c2], wh1, ro_est.name)
		for code in (c1, c2):
			single = get_item_available_stock(code, wh1, ro_est.name)
			single_committed = get_repair_order_committed_qty(code, wh1, ro_est.name)
			self.assertEqual(est_batch[code], single)
			self.assertEqual(ro_parts_stock[code], single)
			self.assertEqual(committed_batch[code], single_committed)

		# Warehouse 2 isolation check
		wh2_batch = get_items_available_stock_batch([c1], wh2, ro_est.name)
		self.assertEqual(wh2_batch[c1]["actual_qty"], 10.0)
		self.assertEqual(wh2_batch[c1]["committed_qty"], 2.0)
		self.assertEqual(wh2_batch[c1]["available_qty"], 8.0)

	def test_service_package_does_not_create_or_delete_items(self):
		"""Verify ServicePackage create, update, and delete never create, overwrite, or delete Item records."""
		pkg_name = f"{TEST_PREFIX}NoItemSvc_{frappe.generate_hash(length=4)}"
		quick_create_service_package(package_name=pkg_name)
		pkg = frappe.get_doc("Service Package", pkg_name)
		self.track(pkg)
		self.assertFalse(frappe.db.exists("Item", pkg_name))

		pkg.service_rate = 250.0
		pkg.save(ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Item", pkg_name))

		# A pre-existing Item sharing the same name must neither be overwritten nor deleted on trash.
		existing_item_name = f"{TEST_PREFIX}KeepItem_{frappe.generate_hash(length=4)}"
		item_group = "Services" if frappe.db.exists("Item Group", "Services") else (
			frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
		)
		stock_uom = frappe.db.get_value("UOM", {"name": ["in", ["Nos", "Unit", "Hour"]]}, "name") or "Nos"
		existing_item = frappe.get_doc({
			"doctype": "Item",
			"item_code": existing_item_name,
			"item_name": "Original Preexisting Item",
			"item_group": item_group,
			"is_stock_item": 0,
			"stock_uom": stock_uom,
			"standard_rate": 77.0,
			"description": "Original description",
		}).insert(ignore_permissions=True)
		self.track(existing_item)

		quick_create_service_package(package_name=existing_item_name)
		self.assertTrue(frappe.db.exists("Service Package", existing_item_name))

		existing_item.reload()
		self.assertEqual(existing_item.item_name, "Original Preexisting Item")
		self.assertEqual(float(existing_item.standard_rate), 77.0)

		frappe.delete_doc("Service Package", existing_item_name, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Service Package", existing_item_name))
		self.assertTrue(frappe.db.exists("Item", existing_item_name))

	def test_parts_endpoints_enforce_permissions_and_input_types(self):
		"""Verify whitelisted parts APIs reject unprivileged users and malformed filter dicts."""
		with self.assertRaises(frappe.ValidationError):
			get_component_alternates(item_code={"!=": ""})
		with self.assertRaises(frappe.ValidationError):
			search_service_packages(query={"!=": ""})
		with self.assertRaises(frappe.ValidationError):
			get_repair_order_procurement_data(docname={"!=": ""})

		orig_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			calls = (
				lambda: quick_create_part(part_number="GUEST-PART"),
				lambda: bulk_import_parts(items=[{"part_number": "GUEST-BLK"}]),
				lambda: get_component_alternates(oem_part_number="1044321-00-J"),
				lambda: link_part_to_service_packages("GUEST-ITEM", ["GUEST-PKG"]),
				lambda: search_service_packages(query=""),
				lambda: quick_create_service_package(package_name="GUEST-PKG"),
				lambda: ensure_supplier_and_link_to_item("GUEST-SUP"),
				lambda: get_repair_order_parts_stock(items=["GUEST-ITEM"]),
				lambda: get_item_procurement_info("GUEST-ITEM"),
				lambda: get_repair_order_procurement_data("GUEST-RO"),
				lambda: submit_procurement_orders("GUEST-RO", [{"item_code": "GUEST-ITEM", "supplier": "S"}]),
				lambda: create_purchase_payment(purchase_order="GUEST-PO", payment_method="Cash"),
				lambda: create_purchase_invoice(supplier="GUEST-SUP", parts_data=[{"item_code": "GUEST-ITEM", "qty": 1}]),
				lambda: get_default_warehouse(),
			)
			for call in calls:
				with self.assertRaises(frappe.PermissionError):
					call()
		finally:
			frappe.set_user(orig_user)

	def test_get_items_stock_qty_map_logs_and_reraises_on_db_error(self):
		"""Verify _get_items_stock_qty_map logs and re-raises DB query errors instead of returning empty dict."""
		with (
			patch("ism.api.parts_stock.frappe.get_all", side_effect=RuntimeError("Simulated Bin query failure")),
			patch("frappe.log_error") as mock_log,
		):
			with self.assertRaises(RuntimeError):
				_get_items_stock_qty_map(["ERR-ITEM-001"])
			mock_log.assert_called_once()

	def test_zero_dollar_package_part_rate_preserved(self):
		"""Verify explicit $0.00 part rates are not clobbered by Item.standard_rate across package helpers and alternates."""
		item = self.create_test_item(
			item_code=f"{TEST_PREFIX}ZERO_RATE_ITEM",
			is_stock_item=0,
			standard_rate=95.0,
		)
		pkg_name = f"{TEST_PREFIX}Zero Rate Pkg"
		quick_create_service_package(package_name=pkg_name)
		self.track("Service Package", pkg_name)

		pkg = frappe.get_doc("Service Package", pkg_name)
		_attach_package_parts(pkg, [{"item_code": item.name, "qty": 1, "rate": 0.0, "slot_number": 1}])
		pkg.save(ignore_permissions=True)
		pkg.reload()
		self.assertEqual(len(pkg.items), 1)
		self.assertEqual(flt(pkg.items[0].rate), 0.0)

		pkg.items[0].rate = 45.0
		_upsert_part_row_on_package_doc(pkg, item, slot_number=1, qty=2, rate=0.0)
		pkg.save(ignore_permissions=True)
		pkg.reload()
		self.assertEqual(flt(pkg.items[0].rate), 0.0)

		pkg.items[0].rate = 45.0
		pkg.save(ignore_permissions=True)
		link_part_to_service_packages(item.name, [pkg.name], slot_number=1, qty=1, rate=0.0)
		pkg.reload()
		self.assertEqual(flt(pkg.items[0].rate), 0.0)

		alts = get_component_alternates(service_package=pkg.name, component_slot="Slot 1")
		match = next(a for a in alts if a["item_code"] == item.name)
		self.assertEqual(flt(match["rate"]), 0.0)










