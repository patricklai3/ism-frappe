# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import frappe
from frappe.utils import flt
from ism.tests.base import ISMTestCase, TEST_PREFIX
from ism.api.parts import (
	format_item_code,
	quick_create_part,
	bulk_import_parts,
	get_component_alternates,
	save_part_to_service_package,
	get_default_warehouse,
	link_part_to_service_packages,
	search_service_packages,
	quick_create_service_package,
	parse_slot_number,
	format_slot_label,
	get_service_package_slots,
	assign_part_to_slot,
)
from ism.ism.doctype.repair_order.repair_order import get_service_package_details


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

	def test_create_material_request_warehouse_resilience(self):
		"""Verify create_material_request handles stale / invalid warehouses (e.g. Stores - ISM) smoothly."""
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

		# create_material_request should sanitize the invalid warehouse and create MR successfully
		res = ro.create_material_request()
		self.assertIn("material_request", res)
		mr_name = res["material_request"]
		mr = frappe.get_doc("Material Request", mr_name)
		self.track(mr)

		self.assertEqual(len(mr.items), 1)
		self.assertEqual(mr.items[0].item_code, p_code)
		self.assertEqual(flt(mr.items[0].qty), 2.0)
		self.assertNotEqual(mr.items[0].warehouse, "Stores - NONEXISTENT_INVALID")
		self.assertTrue(frappe.db.exists("Warehouse", {"name": mr.items[0].warehouse, "company": company, "is_group": 0}))

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
		"""Verify parts added to a service on a Repair Order are automatically synced to the Service Package on save."""
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

		# Reload Service Package and verify the part has been automatically cataloged
		pkg_doc.reload()
		synced_item = next((it for it in pkg_doc.items if it.item_code == part["item_code"]), None)
		self.assertIsNotNone(synced_item, "Part must be auto-synced into Service Package items")
		self.assertEqual(synced_item.slot_number, 1)
		self.assertEqual(synced_item.component_slot, "Slot 1")
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

	def test_get_service_package_slots(self):
		"""Verify get_service_package_slots returns grouped slots with live part previews."""
		pkg = quick_create_service_package(
			package_name=f"{TEST_PREFIX}SlotsPkg_{frappe.generate_hash(length=4)}",
		)
		pkg_name = pkg["name"]
		self.track(frappe.get_doc("Service Package", pkg_name))

		# Add part in slot 1 (LH)
		p1 = quick_create_part(
			part_number=f"{TEST_PREFIX}SLOT1_P1",
			part_name="Left Upper Control Arm",
			slot_number=1,
			service_package=pkg_name,
		)
		self.track(frappe.get_doc("Item", p1["item_code"]))

		# Add another part (variant) in slot 1 (LH aftermarket)
		p2 = quick_create_part(
			part_number=f"{TEST_PREFIX}SLOT1_P2",
			part_name="Left Upper Control Arm Aftermarket",
			part_source="Aftermarket",
			slot_number=1,
			service_package=pkg_name,
		)
		self.track(frappe.get_doc("Item", p2["item_code"]))

		# Add part in slot 2 (RH)
		p3 = quick_create_part(
			part_number=f"{TEST_PREFIX}SLOT2_P1",
			part_name="Right Upper Control Arm",
			slot_number=2,
			service_package=pkg_name,
		)
		self.track(frappe.get_doc("Item", p3["item_code"]))

		slots_data = get_service_package_slots(pkg_name)
		self.assertEqual(len(slots_data["slots"]), 2)
		self.assertEqual(slots_data["slots"][0]["slot_number"], 1)
		self.assertEqual(slots_data["slots"][0]["slot_label"], "Slot 1")
		self.assertEqual(len(slots_data["slots"][0]["parts"]), 2)

		self.assertEqual(slots_data["slots"][1]["slot_number"], 2)
		self.assertEqual(slots_data["slots"][1]["slot_label"], "Slot 2")
		self.assertEqual(len(slots_data["slots"][1]["parts"]), 1)

		self.assertEqual(slots_data["next_slot_number"], 3)

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

	def test_assign_part_to_slot_permanently(self):
		"""Verify assign_part_to_slot permanently sets slot_number and component_slot."""
		pkg = quick_create_service_package(
			package_name=f"{TEST_PREFIX}AssignSlotPkg_{frappe.generate_hash(length=4)}",
		)
		pkg_name = pkg["name"]
		self.track(frappe.get_doc("Service Package", pkg_name))

		# Create an unassigned part
		part = quick_create_part(
			part_number=f"{TEST_PREFIX}UNASSIGNED_P1",
			part_name="Unassigned Bushing",
			slot_number=0,
			service_package=pkg_name,
		)
		self.track(frappe.get_doc("Item", part["item_code"]))

		# Verify initially in unassigned
		slots_before = get_service_package_slots(pkg_name)
		self.assertEqual(len(slots_before["unassigned"]), 1)
		self.assertEqual(slots_before["unassigned"][0]["item_code"], part["item_code"])

		# Permanently assign to Slot 1
		assign_res = assign_part_to_slot(pkg_name, part["item_code"], 1)
		self.assertEqual(assign_res["status"], "success")
		self.assertEqual(assign_res["slot_number"], 1)
		self.assertEqual(assign_res["slot_label"], "Slot 1")

		# Verify now in Slot 1 and unassigned is empty
		slots_after = get_service_package_slots(pkg_name)
		self.assertEqual(len(slots_after["unassigned"]), 0)
		self.assertEqual(len(slots_after["slots"]), 1)
		self.assertEqual(slots_after["slots"][0]["slot_number"], 1)
		self.assertEqual(slots_after["slots"][0]["parts"][0]["item_code"], part["item_code"])

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

		# Now check Service Package: part must have been permanently assigned to Slot 2!
		pkg_doc.reload()
		self.assertEqual(pkg_doc.items[0].slot_number, 2)
		self.assertEqual(pkg_doc.items[0].component_slot, "Slot 2")

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



