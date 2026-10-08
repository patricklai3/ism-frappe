# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt, today

from ism.ism.doctype.repair_order.repair_order import (
	get_service_package_details,
	make_repair_order,
)
from ism.tests.base import ISMTestCase


class TestRepairOrder(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.customer = self.create_test_customer(customer_name="Test Repair Customer")
		self.vehicle = self.create_test_vehicle(
			vin="5YJSA1E26CF000001",
			license_plate="REPAIR-01",
			customer=self.customer.name,
		)

	def test_repair_order_creation_and_naming(self):
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 54000,
			"customer_concern": "Brake squeak and routine oil service",
		})
		ro.insert()
		self.track(ro)

		self.assertTrue(ro.name.startswith("RO-"))
		self.assertEqual(ro.status, "Draft Estimate")
		self.assertEqual(ro.vehicle_location, "In Customer Lot")
		self.assertEqual(ro.odometer_in, 54000)

	def test_user_creation_permission_and_in_create_flag(self):
		for dt in ["Vehicle Check-in", "Repair Order", "Repair Vehicle", "Service Package"]:
			meta = frappe.get_meta(dt)
			self.assertEqual(
				cint(getattr(meta, "in_create", 0)),
				0,
				f"DocType {dt} has in_create=1 ('User Cannot Create'), which hides the '+ Add' button on List Views.",
			)
			self.assertTrue(
				frappe.has_permission(dt, "create", user="Administrator"),
				f"Administrator should have create permission on {dt}",
			)

	def test_service_package_expansion_and_totals(self):
		# Create test part items
		part1 = self.create_test_item(item_code="PART-PADS-1", is_stock_item=0, standard_rate=80)
		part2 = self.create_test_item(item_code="PART-FLUID-1", is_stock_item=0, standard_rate=20)

		# Create service package with labor hours, rate, and nested parts
		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": "Front Brake Renewal",
			"standard_labor_hours": 1.5,
			"hourly_rate": 120.0,
			"items": [
				{"item_code": part1.name, "qty": 1, "rate": 80},
				{"item_code": part2.name, "qty": 2, "rate": 20},
			],
		})
		pkg.insert()
		self.track(pkg)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 54000,
		})
		ro.insert()
		self.track(ro)

		# Add service package to Repair Order
		ro.add_service_package(pkg.name)
		ro.reload()

		self.assertEqual(len(ro.services), 1)
		service = ro.services[0]
		self.assertEqual(service.service_title, "Front Brake Renewal")
		self.assertEqual(service.service_package, pkg.name)
		self.assertEqual(flt(service.labor_hours), 1.5)
		self.assertEqual(flt(service.labor_rate), 120.0)
		self.assertEqual(flt(service.labor_amount), 180.0)

		# Parts: 1 * 80 + 2 * 20 = 120.0
		self.assertEqual(flt(service.parts_total), 120.0)
		parts_data = json.loads(service.parts)
		self.assertEqual(len(parts_data), 2)
		self.assertEqual(parts_data[0]["item_code"], part1.name)
		self.assertEqual(flt(parts_data[0]["amount"]), 80.0)
		self.assertEqual(parts_data[1]["item_code"], part2.name)
		self.assertEqual(flt(parts_data[1]["amount"]), 40.0)

		# Service total: 180 (labor) + 120 (parts) = 300.0
		self.assertEqual(flt(service.total_amount), 300.0)
		self.assertEqual(flt(ro.net_total), 300.0)

		# Tax calculation: 13% Ontario HST -> 300 * 0.13 = 39.00, grand_total = 339.00
		expected_tax = flt(300.0 * 0.13, 2)
		expected_grand = flt(300.0 + expected_tax, 2)
		self.assertEqual(flt(ro.total_taxes_and_charges), expected_tax)
		self.assertEqual(flt(ro.grand_total), expected_grand)

	def test_get_service_package_details_rpc(self):
		part = self.create_test_item(item_code="PART-FILT-1", is_stock_item=0, standard_rate=35)
		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": "Engine Oil & Filter Change",
			"category": "Maintenance",
			"standard_labor_hours": 0.5,
			"hourly_rate": 120.0,
			"items": [
				{"item_code": part.name, "qty": 1, "rate": 35},
			],
		})
		pkg.insert()
		self.track(pkg)

		details = get_service_package_details(pkg.name)
		self.assertEqual(details["package_name"], "Engine Oil & Filter Change")
		self.assertEqual(details["category"], "Maintenance")
		self.assertEqual(flt(details["standard_labor_hours"]), 0.5)
		self.assertEqual(flt(details["hourly_rate"]), 120.0)
		self.assertEqual(flt(details["service_rate"]), 60.0)
		self.assertEqual(len(details["parts"]), 1)
		self.assertEqual(flt(details["parts_total"]), 35.0)

	def test_save_service_as_package(self):
		"""Advisors can create a custom one-off service with nested parts and save it as a reusable Service Package."""
		part1 = self.create_test_item("TEST-PLUG-1", is_stock_item=0, standard_rate=25)
		part2 = self.create_test_item("TEST-COIL-1", is_stock_item=0, standard_rate=85)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 75000,
			"services": [
				{
					"service_title": "Custom Ignition Tune-Up",
					"category": "Mechanical",
					"labor_hours": 2.0,
					"labor_rate": 120.0,
					"parts": json.dumps([
						{"item_code": part1.name, "item_name": "Spark Plug", "qty": 4, "rate": 25, "amount": 100},
						{"item_code": part2.name, "item_name": "Ignition Coil", "qty": 1, "rate": 85, "amount": 85},
					]),
					"advisor_notes": "Replaces plugs and cylinder 3 coil pack",
				}
			],
		})
		ro.insert()
		self.track(ro)

		service_row = ro.services[0]
		res = ro.save_service_as_package(
			service_row_id=service_row.name,
			package_name="Ignition Overhaul Package",
		)
		self.assertEqual(res.get("status"), "success")
		self.assertEqual(res.get("package_name"), "Ignition Overhaul Package")

		# Verify Service Package created in DB
		self.assertTrue(frappe.db.exists("Service Package", "Ignition Overhaul Package"))
		pkg_doc = frappe.get_doc("Service Package", "Ignition Overhaul Package")
		self.track(pkg_doc)
		self.assertEqual(flt(pkg_doc.standard_labor_hours), 2.0)
		self.assertEqual(flt(pkg_doc.hourly_rate), 120.0)
		self.assertEqual(len(pkg_doc.items), 2)
		self.assertEqual(pkg_doc.items[0].item_code, part1.name)
		self.assertEqual(flt(pkg_doc.items[0].qty), 4)
		self.assertEqual(flt(pkg_doc.items[1].rate), 85)

		# Verify service row updated with linked package
		ro.reload()
		self.assertEqual(ro.services[0].service_package, "Ignition Overhaul Package")

	def test_bay_pickup_parking_and_service_completion(self):
		part = self.create_test_item("TEST-PART-BRAKE", is_stock_item=0, standard_rate=200)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 54000,
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Brake Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 120.0,
					"parts": json.dumps([
						{"item_code": part.name, "item_name": "Brake Rotor", "qty": 1, "rate": 200, "amount": 200}
					]),
				}
			],
		})
		ro.insert()
		self.track(ro)

		# 1. Pick Up Vehicle
		ro.pick_up_vehicle()
		ro.reload()
		self.assertEqual(ro.vehicle_location, "In Bay / Being Serviced")
		self.assertEqual(ro.status, "Authorized - In Progress")
		self.assertIsNotNone(ro.picked_up_at)
		self.assertEqual(ro.progress_percent, 0.0)

		# 2. Complete Service
		service_row = ro.services[0]
		ro.toggle_service_completion(service_row.name, 1)
		ro.reload()
		self.assertEqual(ro.services[0].is_completed, 1)
		self.assertIsNotNone(ro.services[0].completed_at)
		self.assertEqual(ro.progress_percent, 100.0)

		# 3. Park Vehicle back in Lot
		ro.park_vehicle()
		ro.reload()
		self.assertEqual(ro.vehicle_location, "Completed / Parked in Lot")
		self.assertEqual(ro.status, "Work Completed")
		self.assertIsNotNone(ro.parked_at)

	def test_customer_authorization_and_deferred_services(self):
		part1 = self.create_test_item("TEST-BRAKES-SVC", is_stock_item=0, standard_rate=300)
		part2 = self.create_test_item("TEST-TRANS-FLUID", is_stock_item=0, standard_rate=250)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 54000,
			"services": [
				{
					"service_title": "Brake Replacement",
					"status": "Draft",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"parts": json.dumps([{"item_code": part1.name, "item_name": "Brakes", "qty": 1, "rate": 300, "amount": 300}]),
				},
				{
					"service_title": "Transmission Flush",
					"status": "Draft",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"parts": json.dumps([{"item_code": part2.name, "item_name": "Flush Fluid", "qty": 1, "rate": 250, "amount": 250}]),
				},
			],
		})
		ro.insert()
		self.track(ro)

		# Approve Service 1, Decline Service 2
		ro.authorize_jobs(
			approvals={"1": "Approved", "2": "Declined"},
			channel="Remote Web Link",
			signer_name="John Customer",
		)
		ro.reload()

		self.assertEqual(ro.approval_status, "Partially Approved")
		self.assertEqual(ro.services[0].status, "Approved")
		self.assertEqual(ro.services[1].status, "Declined")
		# Approved service total: 100 labor + 300 part = 400.0
		self.assertEqual(flt(ro.net_total), 400.0)
		self.assertIsNotNone(ro.audit_hash)

		# Verify declined service logged into Vehicle Deferred Service
		deferred = frappe.get_all(
			"Vehicle Deferred Service",
			filters={"parent": self.vehicle.name, "job_title": "Transmission Flush"},
		)
		self.assertEqual(len(deferred), 1)

	def test_process_payment_and_release(self):
		part = self.create_test_item("TEST-OIL-FILTER", is_stock_item=0, standard_rate=50)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 54000,
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Oil Service",
					"status": "Approved",
					"labor_hours": 0.5,
					"labor_rate": 100.0,
					"parts": json.dumps([
						{"item_code": part.name, "item_name": "Oil Filter", "qty": 1, "rate": 50, "amount": 50}
					]),
				}
			],
		})
		ro.insert()
		self.track(ro)

		# Process payment and release
		res = ro.process_payment_and_release(
			payment_method="Cash",
			odometer_out=54020,
		)
		ro.reload()

		self.assertEqual(res.get("status"), "success")
		self.assertEqual(ro.docstatus, 1)  # Submitted
		self.assertEqual(ro.status, "Closed / Released")
		self.assertEqual(ro.payment_status, "Paid")
		self.assertEqual(ro.vehicle_released, 1)
		self.assertEqual(ro.vehicle_location, "Released to Customer")
		self.assertTrue(bool(ro.sales_invoice))

		# Verify background Sales Invoice exists and is submitted
		si = frappe.get_doc("Sales Invoice", ro.sales_invoice)
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(flt(si.outstanding_amount), 0.0)
		self.assertEqual(si.update_stock, 1)

		# Verify items on Sales Invoice contain both labor and nested parts
		item_codes = [item.item_code for item in si.items]
		self.assertIn(part.name, item_codes)

		# Verify vehicle specs updated
		veh = frappe.get_doc("Repair Vehicle", self.vehicle.name)
		self.assertEqual(veh.last_odometer, 54020)
		self.assertEqual(str(veh.last_service_date), today())

	def test_odometer_out_validation(self):
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 54000,
			"odometer_out": 53900,  # Lower than in
		})
		self.assertRaises(frappe.ValidationError, ro.insert)

	def test_make_repair_order_from_check_in(self):
		check_in = frappe.get_doc({
			"doctype": "Vehicle Check-in",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"intake_mileage": 54100,
			"fuel_level": "3/4",
			"customer_concern": "Engine vibration at idle",
		})
		check_in.insert()
		self.track(check_in)

		ro = make_repair_order(check_in.name)
		self.assertEqual(ro.customer, self.customer.name)
		self.assertEqual(ro.vehicle, self.vehicle.name)
		self.assertEqual(ro.vehicle_check_in, check_in.name)
		self.assertEqual(ro.odometer_in, 54100)
		self.assertEqual(ro.customer_concern, "Engine vibration at idle")
		self.assertEqual(ro.status, "Draft Estimate")
		self.assertEqual(ro.vehicle_location, "In Customer Lot")

	def test_submitted_repair_order_dynamic_lifecycle_and_supplements(self):
		part1 = self.create_test_item("TEST-BRAKE-PAD-X", is_stock_item=0, standard_rate=150)
		part2 = self.create_test_item("TEST-ROTOR-X", is_stock_item=0, standard_rate=200)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 60000,
			"customer_concern": "Brake grind and inspection",
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Brake Service",
					"status": "Approved",
					"labor_hours": 2.0,
					"labor_rate": 120.0,
					"parts": json.dumps([
						{"item_code": part1.name, "item_name": "Brake Pad", "qty": 1, "rate": 150, "amount": 150}
					]),
				}
			],
		})
		ro.insert()
		self.track(ro)

		# 1. User submits Repair Order at intake for audit trail
		ro.submit()
		self.assertEqual(ro.docstatus, 1)

		# 2. Bay execution on submitted document
		ro.pick_up_vehicle()
		ro.reload()
		self.assertEqual(ro.vehicle_location, "In Bay / Being Serviced")
		self.assertEqual(ro.status, "Authorized - In Progress")
		self.assertIsNotNone(ro.picked_up_at)

		# 3. Technician toggles completion on submitted document
		service_row = ro.services[0]
		ro.toggle_service_completion(service_row_id=service_row.name, is_completed=1)
		ro.reload()
		self.assertEqual(ro.services[0].is_completed, 1)
		self.assertIsNotNone(ro.services[0].completed_at)
		self.assertEqual(ro.progress_percent, 100.0)

		# 4. Discovered teardown issue -> Add supplement on submitted document
		supp_res = ro.add_supplement(
			service_title="Replace Warped Rotor",
			category="Mechanical",
			labor_hours=1.0,
			labor_rate=120.0,
			parts=[{"item_code": part2.name, "qty": 1, "rate": 200}],
			notes="Rotor thickness below discard spec",
		)
		ro.reload()
		self.assertEqual(len(ro.services), 2)
		self.assertEqual(ro.services[1].is_supplement, 1)
		self.assertEqual(ro.services[1].status, "Pending Approval")
		# Only service 0 is approved so far, so 1 of 1 approved services is completed = 100%
		self.assertEqual(ro.progress_percent, 100.0)

		# 5. Authorize supplement
		ro.authorize_jobs(
			approvals={"1": "Approved", "2": "Approved"},
			channel="In-Person Signature",
			signer_name="Test Customer",
		)
		ro.reload()
		self.assertEqual(ro.services[1].status, "Approved")
		# Now 1 of 2 approved services is completed = 50%
		self.assertEqual(ro.progress_percent, 50.0)

		# Complete supplement service
		ro.toggle_service_completion(service_row_id=ro.services[1].name, is_completed=1)
		ro.reload()
		self.assertEqual(ro.progress_percent, 100.0)

		# 6. Park vehicle back in lot
		ro.park_vehicle()
		ro.reload()
		self.assertEqual(ro.vehicle_location, "Completed / Parked in Lot")
		self.assertEqual(ro.status, "Work Completed")

		# 7. Finalize payment and vehicle release
		release_res = ro.process_payment_and_release(
			payment_method="Cash",
			odometer_out=60015,
		)
		ro.reload()
		self.assertEqual(release_res.get("status"), "success")
		self.assertEqual(ro.status, "Closed / Released")
		self.assertEqual(ro.vehicle_released, 1)
		self.assertEqual(ro.vehicle_location, "Released to Customer")
		self.assertIsNotNone(ro.sales_invoice)

	def test_unapproved_pickup_and_status_progression_blocked(self):
		"""Verify that starting work, pulling car into bay, or advancing status without authorization is strictly blocked."""
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 70000,
			"customer_concern": "Check engine light",
			"services": [
				{
					"service_title": "Diagnostic Scan",
					"status": "Pending Approval",
					"labor_hours": 1.0,
					"labor_rate": 150.0,
				}
			],
		})
		ro.insert()
		self.track(ro)

		# 1. Calling pick_up_vehicle without customer approval must throw ValidationError
		with self.assertRaises(frappe.ValidationError):
			ro.pick_up_vehicle()

		# 2. Directly setting vehicle_location to In Bay without customer approval must throw ValidationError
		ro.vehicle_location = "In Bay / Being Serviced"
		with self.assertRaises(frappe.ValidationError):
			ro.save()
		ro.reload()

		# 3. Directly advancing status to Authorized - In Progress without customer approval must throw ValidationError
		ro.status = "Authorized - In Progress"
		with self.assertRaises(frappe.ValidationError):
			ro.save()
		ro.reload()

	def test_unapproved_service_completion_blocked(self):
		"""Verify that services cannot be marked completed unless customer has approved them."""
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 72000,
			"customer_concern": "Suspension noise",
			"services": [
				{
					"service_title": "Strut Replacement",
					"status": "Pending Approval",
					"labor_hours": 2.0,
					"labor_rate": 120.0,
				}
			],
		})
		ro.insert()
		self.track(ro)

		# 1. toggle_service_completion must throw ValidationError on unapproved service
		service_row = ro.services[0]
		with self.assertRaises(frappe.ValidationError):
			ro.toggle_service_completion(service_row.name, 1)

		# 2. Saving doc with is_completed=1 on unapproved service must throw ValidationError
		ro.services[0].is_completed = 1
		with self.assertRaises(frappe.ValidationError):
			ro.save()

	def test_module_level_authorize_jobs_and_guards(self):
		"""Verify module-level authorize_jobs endpoint works, syncs services, and unblocks execution."""
		from ism.ism.doctype.repair_order.repair_order import authorize_jobs

		item = self.create_test_item("TEST-ALIGN-P", is_stock_item=0, standard_rate=120)
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 80000,
			"services": [
				{
					"service_title": "Wheel Alignment",
					"status": "Pending Approval",
					"labor_hours": 1.0,
					"labor_rate": 120.0,
					"parts": json.dumps([{"item_code": item.name, "item_name": "Alignment Shim", "qty": 1, "rate": 20}]),
				}
			],
		})
		ro.insert()
		self.track(ro)

		fake_signature = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
		res = authorize_jobs(
			docname=ro.name,
			approvals={"1": "Approved"},
			channel="In-Person Signature",
			signer_name="Alice Smith",
			signature_image=fake_signature,
		)
		self.assertEqual(res.get("status"), "success")
		self.assertEqual(res.get("approval_status"), "Approved")

		ro.reload()
		self.assertEqual(ro.approval_status, "Approved")
		self.assertEqual(ro.services[0].status, "Approved")
		self.assertEqual(ro.status, "Authorized - In Progress")
		self.assertEqual(ro.authorized_by, "Alice Smith")
		self.assertIsNotNone(ro.audit_hash)
		self.assertTrue(ro.signature_image.startswith("/files/sig_") or ro.signature_image.startswith("/private/files/sig_"))

		# Bay pickup and service completion should now succeed
		pickup_res = ro.pick_up_vehicle()
		self.assertEqual(pickup_res.get("status"), "success")
		self.assertEqual(ro.vehicle_location, "In Bay / Being Serviced")

		comp_res = ro.toggle_service_completion(ro.services[0].name, 1)
		self.assertEqual(comp_res.get("status"), "success")
		self.assertEqual(ro.progress_percent, 100.0)

	def test_run_doc_method_authorize_jobs_with_extra_kwargs(self):
		"""Verify that run_method works with extra keyword arguments like docname and numeric string IDs."""
		item1 = self.create_test_item("TEST-P1-X", is_stock_item=0, standard_rate=50)
		item2 = self.create_test_item("TEST-P2-X", is_stock_item=0, standard_rate=75)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 85000,
			"services": [
				{
					"service_title": "Brake Job",
					"status": "Pending Approval",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"parts": json.dumps([{"item_code": item1.name, "qty": 1, "rate": 50}]),
				},
				{
					"service_title": "Tire Rotation",
					"status": "Pending Approval",
					"labor_hours": 0.5,
					"labor_rate": 100.0,
					"parts": json.dumps([{"item_code": item2.name, "qty": 1, "rate": 75}]),
				},
			],
		})
		ro.insert()
		self.track(ro)

		fake_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

		# Call via ro.run_method with docname in kwargs (simulating Frappe's run_doc_method handler)
		res = ro.run_method(
			"authorize_jobs",
			docname=ro.name,
			approvals={"1": "Approved", "2": "Approved"},
			channel="In-Person Signature",
			signer_name="Prestiga-Biz",
			signature_image=fake_sig,
		)
		self.assertEqual(res.get("status"), "success")
		self.assertEqual(res.get("approval_status"), "Approved")

		ro.reload()
		self.assertEqual(ro.approval_status, "Approved")
		self.assertEqual(ro.services[0].status, "Approved")
		self.assertEqual(ro.services[1].status, "Approved")
		self.assertEqual(ro.status, "Authorized - In Progress")
		self.assertEqual(ro.authorized_by, "Prestiga-Biz")
		self.assertIsNotNone(ro.audit_hash)

	def test_manual_advisor_approval_and_cascade(self):
		"""Verify that advisor manually setting approval_status to 'Approved' on submitted doc cascades to services."""
		item = self.create_test_item("TEST-P5-X", is_stock_item=0, standard_rate=120)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 30000,
			"status": "Pending Authorization",
			"approval_status": "Draft",
			"services": [
				{
					"service_title": "Engine Diagnostics",
					"status": "Draft",
					"labor_hours": 1.0,
					"labor_rate": 120.0,
					"parts": json.dumps([{"item_code": item.name, "qty": 1, "rate": 120}]),
				},
			],
		})
		ro.insert()
		ro.submit()
		self.track(ro)

		# Advisor manually updates header approval_status to Approved and status to Authorized - In Progress
		ro.approval_status = "Approved"
		ro.status = "Authorized - In Progress"
		ro.save(ignore_permissions=True)

		ro.reload()
		self.assertEqual(ro.status, "Authorized - In Progress")
		self.assertEqual(ro.approval_status, "Approved")
		self.assertEqual(ro.services[0].status, "Approved")

		# Technician can work and pick up vehicle
		ro.pick_up_vehicle()
		ro.reload()
		self.assertEqual(ro.vehicle_location, "In Bay / Being Serviced")

	def test_authorize_jobs_with_wrapped_dict_and_json_string(self):
		"""Verify authorize_jobs unwraps wrapped dict and parses json string approvals correctly."""
		item = self.create_test_item("TEST-P6-X", is_stock_item=0, standard_rate=150)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 45000,
			"status": "Draft Estimate",
			"approval_status": "Pending Approval",
			"services": [
				{
					"service_title": "Transmission Fluid Exchange",
					"status": "Draft",
					"labor_hours": 1.5,
					"labor_rate": 120.0,
					"parts": json.dumps([{"item_code": item.name, "qty": 4, "rate": 25}]),
				},
			],
		})
		ro.insert()
		ro.submit()
		self.track(ro)

		fake_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

		# Simulate handler passing the entire request payload as a wrapped dictionary in the first parameter
		wrapped_payload = {
			"docname": ro.name,
			"approvals": json.dumps({"1": "Approved", "JOB-1": "Approved"}),
			"channel": "In-Person Signature",
			"signer_name": "Prestiga-Biz",
			"signature_image": fake_sig,
			"notes": "Customer signed on shop tablet",
		}
		res = ro.authorize_jobs(wrapped_payload)
		self.assertEqual(res.get("status"), "success")
		self.assertEqual(res.get("approval_status"), "Approved")

		ro.reload()
		self.assertEqual(ro.approval_status, "Approved")
		self.assertEqual(ro.status, "Authorized - In Progress")
		self.assertEqual(ro.authorized_by, "Prestiga-Biz")
		self.assertEqual(ro.verbal_notes, "Customer signed on shop tablet")
		self.assertEqual(ro.services[0].status, "Approved")
		self.assertIsNotNone(ro.audit_hash)

	def test_method_signatures_do_not_contain_args_parameter(self):
		"""Verify that no whitelisted controller methods contain 'args' in their parameters,
		which would cause Frappe's run_doc_method to fail keyword unpacking."""
		import inspect
		from ism.ism.doctype.repair_order.repair_order import RepairOrder

		methods_to_check = [
			"authorize_jobs",
			"pick_up_vehicle",
			"park_vehicle",
			"toggle_service_completion",
			"toggle_job_completion",
			"generate_approval_token",
			"add_service_package",
			"add_supplement",
			"save_service_as_package",
			"process_payment_and_release",
			"create_material_request",
		]

		for method_name in methods_to_check:
			fn = getattr(RepairOrder, method_name, None)
			self.assertIsNotNone(fn, f"Method {method_name} should exist on RepairOrder")
			params = list(inspect.signature(fn).parameters.keys())
			self.assertNotIn("args", params, f"Method {method_name} should not have 'args' parameter (e.g. *args)")

	def test_run_doc_method_pick_up_and_park_vehicle_handler(self):
		"""Verify that run_doc_method handler executes pick_up_vehicle and park_vehicle cleanly when called from desk."""
		from frappe.handler import run_doc_method
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		builder = EnvironBuilder(method="POST", data={})
		frappe.local.request = Request(builder.get_environ())

		item = self.create_test_item("TEST-P7-X", is_stock_item=0, standard_rate=100)
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 55000,
			"status": "Authorized - In Progress",
			"approval_status": "Approved",
			"services": [
				{
					"service_title": "Brake Flush",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"parts": json.dumps([{"item_code": item.name, "qty": 1, "rate": 20}]),
				},
			],
		})
		ro.insert()
		ro.submit()
		self.track(ro)

		# 1. Simulate client calling frm.call({ doc: frm.doc, method: "pick_up_vehicle" })
		frappe.response = frappe._dict()
		frappe.response.docs = []
		run_doc_method("pick_up_vehicle", docs=ro.as_json())
		self.assertEqual(frappe.response.message.get("status"), "success")
		self.assertEqual(frappe.response.message.get("vehicle_location"), "In Bay / Being Serviced")

		ro.reload()
		self.assertEqual(ro.vehicle_location, "In Bay / Being Serviced")

		# 2. Simulate client calling frm.call({ doc: frm.doc, method: "park_vehicle" })
		frappe.response = frappe._dict()
		frappe.response.docs = []
		run_doc_method("park_vehicle", docs=ro.as_json())
		self.assertEqual(frappe.response.message.get("status"), "success")
		self.assertEqual(frappe.response.message.get("vehicle_location"), "Completed / Parked in Lot")

		ro.reload()
		self.assertEqual(ro.vehicle_location, "Completed / Parked in Lot")
		self.assertEqual(ro.status, "Work Completed")

