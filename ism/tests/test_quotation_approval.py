# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import timedelta
import frappe
from frappe.utils import flt, now_datetime

from ism.api.approval import (
	capture_in_person_signature,
	create_supplemental_quotation,
	get_or_create_approval_token,
	get_public_quote_approval,
	log_verbal_authorization,
	submit_public_quote_approval,
)
from ism.api.quotation import make_sales_order
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestQuotationApproval(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.vin = "5YJ3E1EBXNF123456"
		self.customer = self.create_test_customer(f"{TEST_PREFIX}Auth_Cust")
		self.vehicle = self.create_test_vehicle(
			vin=self.vin,
			customer=self.customer.name,
			license_plate="TEST-AUTH",
		)
		self.check_in = self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=self.customer.name,
			intake_mileage=15000,
		)
		self.item_1 = self.get_or_create_test_item(f"{TEST_PREFIX}Brakes")
		self.item_2 = self.get_or_create_test_item(f"{TEST_PREFIX}Flush")

	def create_quote_with_jobs(self, submit=True):
		"""Helper to create Quotation with two distinct jobs and linked items."""
		q = frappe.get_doc({
			"doctype": "Quotation",
			"quotation_to": "Customer",
			"party_name": self.customer.name,
			"custom_vehicle": self.vehicle.name,
			"custom_vehicle_check_in": self.check_in.name,
			"custom_jobs": [
				{
					"job_title": "Front Brake Service",
					"job_category": "Mechanical",
					"status": "Draft",
				},
				{
					"job_title": "Transmission Fluid Flush",
					"job_category": "Maintenance",
					"status": "Draft",
				},
			],
			"items": [
				{
					"item_code": self.item_1,
					"qty": 2,
					"rate": 150.0,
					"custom_job_title": "Front Brake Service",
				},
				{
					"item_code": self.item_2,
					"qty": 1,
					"rate": 120.0,
					"custom_job_title": "Transmission Fluid Flush",
				},
			],
		})
		q.insert(ignore_permissions=True)
		self.track(q)
		if submit:
			q.submit()
		return q

	def test_job_estimated_total_calculation(self):
		"""Ensure job.estimated_total is automatically computed from linked items."""
		q = self.create_quote_with_jobs(submit=False)
		q.reload()
		self.assertEqual(q.custom_jobs[0].estimated_total, 300.0)
		self.assertEqual(q.custom_jobs[1].estimated_total, 120.0)

	def test_token_generation_and_expiration(self):
		"""Test secure token generation and 48-hour expiration checking."""
		q = self.create_quote_with_jobs()
		token, url = get_or_create_approval_token(q.name)
		self.assertTrue(len(token) >= 32)
		self.assertIn(token, url)

		# Test public view API
		data = get_public_quote_approval(token)
		self.assertTrue(data.get("valid"))
		self.assertEqual(len(data.get("jobs")), 2)

		# Simulate expiration
		q.reload()
		q.custom_approval_token_expiry = now_datetime() - timedelta(hours=1)
		q.save(ignore_permissions=True)

		expired_data = get_public_quote_approval(token)
		self.assertFalse(expired_data.get("valid"))
		self.assertTrue(expired_data.get("expired"))

	def test_partial_remote_approval_and_deferred_sync(self):
		"""TEST-AUTH-01 & TEST-AUTH-04: Test partial approval and deferred services sync."""
		q = self.create_quote_with_jobs()
		token, _ = get_or_create_approval_token(q.name)

		job1 = q.custom_jobs[0]
		job2 = q.custom_jobs[1]

		# Mock simple base64 1x1 png signature
		sample_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

		res = submit_public_quote_approval(
			token=token,
			approvals=[
				{"job_id": job1.name, "status": "Approved"},
				{"job_id": job2.name, "status": "Declined"},
			],
			signature_data=sample_sig,
			signer_name="Jane Doe",
		)
		self.assertTrue(res.get("success"))

		q.reload()
		self.assertEqual(q.custom_jobs[0].status, "Approved")
		self.assertEqual(q.custom_jobs[0].authorized_amount, 300.0)
		self.assertEqual(q.custom_jobs[0].approval_channel, "Remote Web Link")
		self.assertEqual(q.custom_jobs[1].status, "Declined")
		self.assertEqual(q.custom_approval_status, "Partially Approved")

		# Verify declined job is synced to Vehicle deferred services
		vehicle = frappe.get_doc("Repair Vehicle", self.vehicle.name)
		deferred = [d for d in vehicle.deferred_services if d.job_title == "Transmission Fluid Flush"]
		self.assertEqual(len(deferred), 1)
		self.assertEqual(deferred[0].quotation, q.name)
		self.assertEqual(deferred[0].estimated_amount, 120.0)

	def test_conversion_blocked_with_unresolved_jobs(self):
		"""TEST-AUTH-02: Conversion to Sales Order must fail if any job is unresolved."""
		q = self.create_quote_with_jobs()
		# Job status is Pending Approval on submitted quote
		self.assertRaises(frappe.ValidationError, make_sales_order, q.name)

	def test_approved_only_sales_order_transfer(self):
		"""TEST-AUTH-03: Only items belonging to approved jobs transfer to Sales Order."""
		q = self.create_quote_with_jobs()
		job1 = q.custom_jobs[0]
		job2 = q.custom_jobs[1]

		# Approve job1, decline job2
		capture_in_person_signature(
			quotation_name=q.name,
			approvals=[
				{"name": job1.name, "status": "Approved"},
				{"name": job2.name, "status": "Declined"},
			],
			signature_data="",
			signer_name="John In-Person",
		)
		q.reload()

		so = make_sales_order(q.name)
		so.delivery_date = frappe.utils.nowdate()
		so.insert(ignore_permissions=True)
		self.track(so)

		# Sales Order jobs should only have Job 1
		self.assertEqual(len(so.custom_jobs), 1)
		self.assertEqual(so.custom_jobs[0].job_title, "Front Brake Service")

		# Sales Order items should only have item_1
		self.assertEqual(len(so.items), 1)
		self.assertEqual(so.items[0].item_code, self.item_1)
		self.assertEqual(so.items[0].amount, 300.0)

	def test_verbal_phone_authorization(self):
		"""TEST-AUTH-11: Log verbal authorization with caller name, phone and notes."""
		q = self.create_quote_with_jobs()
		job1 = q.custom_jobs[0]
		job2 = q.custom_jobs[1]

		res = log_verbal_authorization(
			quotation_name=q.name,
			approvals=[
				{"name": job1.name, "status": "Approved"},
				{"name": job2.name, "status": "Approved"},
			],
			contact_person="Bob Customer",
			phone_number="555-987-6543",
			notes="Customer agreed over phone call at 2:30pm to proceed with all items.",
		)
		self.assertTrue(res.get("success"))

		q.reload()
		self.assertEqual(q.custom_jobs[0].status, "Approved")
		self.assertEqual(q.custom_jobs[0].approval_channel, "Verbal Phone Authorization")
		self.assertIn("Bob Customer", q.custom_jobs[0].authorized_by)
		self.assertEqual(q.custom_approval_status, "Customer Approved")

	def test_supplement_creation_and_auto_merge(self):
		"""TEST-AUTH-05 & TEST-AUTH-06: Create supplement and auto-append to active Sales Order."""
		q = self.create_quote_with_jobs()
		job1 = q.custom_jobs[0]
		job2 = q.custom_jobs[1]

		# Approve job1 only
		capture_in_person_signature(
			quotation_name=q.name,
			approvals=[
				{"name": job1.name, "status": "Approved"},
				{"name": job2.name, "status": "Declined"},
			],
			signature_data="",
			signer_name="Initial Client",
		)
		q.reload()

		so = make_sales_order(q.name)
		so.delivery_date = frappe.utils.nowdate()
		so.insert(ignore_permissions=True)
		so.submit()
		self.track(so)

		# Create Supplement from active Sales Order
		supp_res = create_supplemental_quotation(so.name)
		supp_name = supp_res.get("quotation")
		supp_quote = frappe.get_doc("Quotation", supp_name)
		self.track(supp_quote)

		self.assertEqual(supp_quote.custom_is_supplement, 1)
		self.assertEqual(supp_quote.custom_supplement_for, so.name)

		# Add supplemental job (e.g. Broken Caliper)
		caliper_item = self.get_or_create_test_item(f"{TEST_PREFIX}Caliper")
		supp_quote.append("custom_jobs", {
			"job_title": "Brake Caliper Replacement",
			"job_category": "Mechanical",
			"status": "Draft",
		})
		supp_quote.append("items", {
			"item_code": caliper_item,
			"qty": 1,
			"rate": 220.0,
			"custom_job_title": "Brake Caliper Replacement",
		})
		supp_quote.save(ignore_permissions=True)
		supp_quote.submit()
		supp_quote.reload()
		caliper_job_id = supp_quote.custom_jobs[0].name

		# Customer authorizes supplement
		log_verbal_authorization(
			quotation_name=supp_quote.name,
			approvals=[{"name": caliper_job_id, "status": "Approved"}],
			contact_person="Client on Bay Phone",
			phone_number="555-1234",
			notes="Authorized caliper after tech inspection.",
		)

		# Check that parent Sales Order was automatically updated with new job and item!
		so.reload()
		so_job_titles = [j.job_title for j in so.custom_jobs]
		self.assertIn("Brake Caliper Replacement", so_job_titles)
		so_item_codes = [it.item_code for it in so.items]
		self.assertIn(caliper_item, so_item_codes)
		self.assertEqual(flt(so.net_total), 520.0)

	def test_invoice_cost_tolerance_validation(self):
		"""TEST-AUTH-09 & TEST-AUTH-10: Test cost tolerance buffer enforcement on Sales Invoice."""
		from ism.api.sales_invoice import validate_sales_invoice_authorization

		# Set ISM Settings tolerance: 10% and $20 cap
		settings = frappe.get_single("ISM Settings")
		settings.cost_tolerance_percentage = 10.0
		settings.cost_tolerance_flat_amount = 20.0
		settings.save(ignore_permissions=True)

		inv = frappe.get_doc({
			"doctype": "Sales Invoice",
			"customer": self.customer.name,
			"custom_vehicle": self.vehicle.name,
			"custom_jobs": [
				{
					"name": "JOB-BRAKES",
					"job_title": "Front Brake Job",
					"status": "Approved",
					"authorized_amount": 100.0,
				}
			],
			"items": [
				{
					"item_code": self.item_1,
					"qty": 1,
					"rate": 105.0,  # $5 increase (5% within 10% / $20 buffer)
					"amount": 105.0,
					"custom_job": "JOB-BRAKES",
					"custom_job_title": "Front Brake Job",
				}
			],
		})

		# Case 1: Within tolerance -> Passes
		validate_sales_invoice_authorization(inv)

		# Case 2: Exceeding tolerance -> $130 ($30 increase > $10 buffer) -> Fails
		inv.items[0].rate = 130.0
		inv.items[0].amount = 130.0
		self.assertRaises(frappe.ValidationError, validate_sales_invoice_authorization, inv)

		# Case 3: Unauthorized item with no job link -> Fails
		inv.items[0].rate = 100.0
		inv.items[0].amount = 100.0
		inv.items[0].custom_job = None
		inv.items[0].custom_job_title = None
		self.assertRaises(frappe.ValidationError, validate_sales_invoice_authorization, inv)

	def test_service_package_auto_sync_and_items_population(self):
		"""Test Service Package auto-item sync, job title defaulting, and items auto-population on quote."""
		from ism.api.quotation import get_service_package_details

		pkg_name = f"{TEST_PREFIX}Pkg_{frappe.generate_hash(length=6)}"
		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": pkg_name,
			"category": "Maintenance",
			"standard_labor_hours": 1.5,
			"hourly_rate": 100.0,
			"items": [
				{
					"item_code": self.item_1,
					"qty": 2.0,
					"rate": 50.0,
				}
			],
		})
		pkg.insert(ignore_permissions=True)
		self.track(pkg)

		# 1. Verify that ERPNext non-stock Item was automatically generated
		self.assertTrue(frappe.db.exists("Item", pkg_name))
		item_doc = frappe.get_doc("Item", pkg_name)
		self.track(item_doc)
		self.assertEqual(item_doc.is_stock_item, 0)
		self.assertEqual(item_doc.is_sales_item, 1)
		self.assertEqual(flt(item_doc.standard_rate), 150.0)

		# 2. Test get_service_package_details API
		details = get_service_package_details(pkg_name)
		self.assertEqual(details["package_name"], pkg_name)
		self.assertEqual(details["service_rate"], 150.0)
		self.assertEqual(len(details["items"]), 1)
		self.assertEqual(details["items"][0]["item_code"], self.item_1)

		# 3. Create Quotation selecting ONLY the service_package, leaving items empty
		q = frappe.get_doc({
			"doctype": "Quotation",
			"party_name": self.customer.name,
			"quotation_to": "Customer",
			"custom_vehicle": self.vehicle.name,
			"custom_vehicle_check_in": self.check_in.name,
			"custom_jobs": [
				{
					"service_package": pkg.name,
					"status": "Draft",
				}
			],
			"items": [],  # Empty items - should auto-expand from service package!
		})
		q.save(ignore_permissions=True)
		self.track(q)
		q.reload()

		# Job title should default to package name
		self.assertEqual(q.custom_jobs[0].job_title, pkg_name)

		# Items should be automatically expanded with 2 rows (1 labor service + 1 part)
		self.assertEqual(len(q.items), 2)
		item_codes = [it.item_code for it in q.items]
		self.assertIn(pkg_name, item_codes)
		self.assertIn(self.item_1, item_codes)

		# Job estimated total should equal labor ($150) + parts ($100) = $250
		self.assertEqual(flt(q.custom_jobs[0].estimated_total), 250.0)
		self.assertEqual(flt(q.net_total), 250.0)

	def test_quotation_submission_with_unresolved_jobs_succeeds(self):
		"""Verify Quotation submits cleanly with unresolved jobs before presenting to customer."""
		q = self.create_quote_with_jobs(submit=False)
		self.assertEqual(q.docstatus, 0)
		self.assertEqual(q.custom_approval_status, "Draft")
		self.assertEqual(q.custom_jobs[0].status, "Draft")

		# Submit estimate - must succeed and transition to Pending Approval
		q.submit()
		q.reload()
		self.assertEqual(q.docstatus, 1)
		self.assertEqual(q.custom_approval_status, "Pending Approval")
		self.assertEqual(q.custom_jobs[0].status, "Pending Approval")

		# Conversion to Sales Order is blocked until customer approves
		self.assertRaises(frappe.ValidationError, make_sales_order, q.name)

		# Customer approves jobs
		capture_in_person_signature(
			quotation_name=q.name,
			approvals=[
				{"name": q.custom_jobs[0].name, "status": "Approved"},
				{"name": q.custom_jobs[1].name, "status": "Approved"},
			],
			signature_data="",
			signer_name="Happy Customer",
		)
		q.reload()
		self.assertEqual(q.custom_approval_status, "Customer Approved")

		# Sales order conversion now succeeds
		so = make_sales_order(q.name)
		self.assertEqual(len(so.custom_jobs), 2)

