# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, cint, flt, today

from ism.ism.doctype.repair_order.repair_order import make_repair_order
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
		# Create test items
		labor_item = self.create_test_item(item_code="LABOR-BRAKE", is_stock_item=0, standard_rate=120)
		part_item = self.create_test_item(item_code="PART-PADS", is_stock_item=0, standard_rate=80)

		# Create service package
		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": "Front Brake Renewal",
			"standard_labor_hours": 1.5,
			"items": [
				{"item_code": labor_item.name, "is_labor": 1, "qty": 1.5, "rate": 120},
				{"item_code": part_item.name, "is_labor": 0, "qty": 1, "rate": 80},
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

		# Add service package
		ro.add_service_package(pkg.name)
		ro.reload()

		self.assertEqual(len(ro.jobs), 1)
		self.assertEqual(ro.jobs[0].job_title, "Front Brake Renewal")
		self.assertEqual(len(ro.items), 2)

		# 1.5 * 120 = 180 (labor) + 80 (part) = 260
		expected_net = 260.0
		self.assertEqual(flt(ro.jobs[0].estimated_total), expected_net)
		self.assertEqual(flt(ro.net_total), expected_net)

		# Tax calculation: 13% Ontario HST -> 260 * 0.13 = 33.80, grand_total = 293.80
		expected_tax = flt(expected_net * 0.13, 2)
		expected_grand = flt(expected_net + expected_tax, 2)
		self.assertEqual(flt(ro.total_taxes_and_charges), expected_tax)
		self.assertEqual(flt(ro.grand_total), expected_grand)

	def test_bay_pickup_parking_and_job_completion(self):
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 54000,
			"jobs": [
				{
					"job_id": "JOB-1",
					"job_title": "Brake Service",
					"status": "Approved",
					"estimated_total": 200,
					"authorized_amount": 200,
				}
			],
			"items": [
				{
					"job_id": "JOB-1",
					"item_code": self.create_test_item("TEST-PART-1", is_stock_item=0, standard_rate=200).name,
					"item_name": "Test Part 1",
					"qty": 1,
					"rate": 200,
					"amount": 200,
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

		# 2. Complete Job
		ro.toggle_job_completion("JOB-1", 1)
		ro.reload()
		self.assertEqual(ro.jobs[0].is_completed, 1)
		self.assertIsNotNone(ro.jobs[0].completed_at)
		self.assertEqual(ro.progress_percent, 100.0)

		# 3. Park Vehicle back in Lot
		ro.park_vehicle()
		ro.reload()
		self.assertEqual(ro.vehicle_location, "Completed / Parked in Lot")
		self.assertEqual(ro.status, "Work Completed")
		self.assertIsNotNone(ro.parked_at)

	def test_customer_authorization_and_deferred_services(self):
		part1 = self.create_test_item("TEST-BRAKES", is_stock_item=0, standard_rate=300)
		part2 = self.create_test_item("TEST-TRANS", is_stock_item=0, standard_rate=250)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 54000,
			"jobs": [
				{"job_id": "JOB-1", "job_title": "Brake Replacement", "status": "Draft"},
				{"job_id": "JOB-2", "job_title": "Transmission Flush", "status": "Draft"},
			],
			"items": [
				{"job_id": "JOB-1", "item_code": part1.name, "item_name": "Brakes", "qty": 1, "rate": 300, "amount": 300},
				{"job_id": "JOB-2", "item_code": part2.name, "item_name": "Flush", "qty": 1, "rate": 250, "amount": 250},
			],
		})
		ro.insert()
		self.track(ro)

		# Approve Job 1, Decline Job 2
		ro.authorize_jobs(
			approvals={"JOB-1": "Approved", "JOB-2": "Declined"},
			channel="Remote Web Link",
			signer_name="John Customer",
		)
		ro.reload()

		self.assertEqual(ro.approval_status, "Partially Approved")
		self.assertEqual(ro.jobs[0].status, "Approved")
		self.assertEqual(ro.jobs[1].status, "Declined")
		self.assertEqual(flt(ro.net_total), 300.0)
		self.assertIsNotNone(ro.audit_hash)

		# Verify declined job logged into Vehicle Deferred Service
		deferred = frappe.get_all(
			"Vehicle Deferred Service",
			filters={"parent": self.vehicle.name, "job_title": "Transmission Flush"},
		)
		self.assertEqual(len(deferred), 1)

	def test_process_payment_and_release(self):
		part = self.create_test_item("TEST-OIL-PKG", is_stock_item=0, standard_rate=100)

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 54000,
			"jobs": [
				{"job_id": "JOB-1", "job_title": "Oil Service", "status": "Approved"}
			],
			"items": [
				{"job_id": "JOB-1", "item_code": part.name, "item_name": "Oil Service", "qty": 1, "rate": 100, "amount": 100}
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
		self.assertEqual(ro.status, "Billed / Paid")
		self.assertEqual(ro.payment_status, "Paid")
		self.assertEqual(ro.vehicle_released, 1)
		self.assertEqual(ro.vehicle_location, "Released to Customer")
		self.assertTrue(bool(ro.sales_invoice))

		# Verify background Sales Invoice exists and is submitted
		si = frappe.get_doc("Sales Invoice", ro.sales_invoice)
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(flt(si.outstanding_amount), 0.0)
		self.assertEqual(si.update_stock, 1)

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
