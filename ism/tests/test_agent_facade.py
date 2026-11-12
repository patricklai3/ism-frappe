# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import today

from ism.api.agent import (
	amend_quote,
	book_appointment,
	create_quote,
	find_available_slots,
	get_shop_floor_status,
	import_supplier_catalog,
	import_tesla_procedure,
	send_quote_for_approval,
	update_job_status,
)
from ism.tests.base import ISMTestCase
from ism.tests.test_fixtures import CUSTOMER, PREFIX, VEHICLE_VIN, ensure_ism_settings, setup_all, teardown_transactional


class TestAgentFacade(ISMTestCase):
	"""Comprehensive test suite for the Curated Agent Facade API layer."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		setup_all()

	@classmethod
	def tearDownClass(cls):
		teardown_transactional()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		ensure_ism_settings()

	def test_create_quote_basic_flow(self):
		"""Verify create_quote creates a Draft Estimate Repair Order with exact 13% tax math and summary card."""
		res = create_quote(
			customer_name=f"{PREFIX}Agent Cust 1",
			vehicle_identifier="5YJ3E1EBXNF999001",
			services=[
				{"service_title": "Front Brake Pad Replacement", "labor_hours": 1.5, "labor_rate": 140.0},
				{"service_title": "Cabin Air Filter Service", "labor_hours": 0.5, "labor_rate": 140.0},
			],
			notes="Customer reports squeaking noise on light braking.",
		)

		self.assertTrue(res["success"])
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		# Verify DB state
		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(ro.status, "Draft Estimate")
		self.assertEqual(ro.approval_status, "Estimating")
		self.assertEqual(len(ro.services), 2)

		# Verify deterministic financial calculations:
		# Labor: (1.5 * 140) + (0.5 * 140) = 210 + 70 = 280.00
		# Tax: 280 * 0.13 = 36.40
		# Total: 316.40
		self.assertAlmostEqual(ro.net_total, 280.0, places=2)
		self.assertAlmostEqual(ro.total_taxes_and_charges, 36.4, places=2)
		self.assertAlmostEqual(ro.grand_total, 316.4, places=2)

		# Verify response envelope & rich summary card
		self.assertIn(f"#{ro.name}", res["summary_card"])
		self.assertIn("Front Brake Pad Replacement", res["summary_card"])
		self.assertIn("316.40", res["summary_card"])
		self.assertIn("urls", res)
		self.assertIn("/app/repair-order/", res["urls"]["desk"])
		self.assertIn("/printview?", res["urls"]["print"])

	def test_create_quote_default_inspection_fallback(self):
		"""Verify create_quote provides a default inspection service when services list is empty."""
		res = create_quote(
			customer_name=f"{PREFIX}Agent Cust 2",
			vehicle_identifier="5YJ3E1EBXNF999002",
			services=[],
			notes="General check-in inspection",
		)
		self.assertTrue(res["success"])
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(len(ro.services), 1)
		self.assertIn("Inspection", ro.services[0].service_title)

	def test_amend_quote_safely_modifies_lines(self):
		"""Verify amend_quote can add and remove service rows on an unapproved Repair Order."""
		res = create_quote(
			customer_name=f"{PREFIX}Agent Cust 3",
			vehicle_identifier="5YJ3E1EBXNF999003",
			services=[
				{"service_title": "Initial Service A", "labor_hours": 1.0, "labor_rate": 100.0},
				{"service_title": "Initial Service B", "labor_hours": 2.0, "labor_rate": 100.0},
			],
		)
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		# Amend: remove first service (idx 0), add a new service
		amend_res = amend_quote(
			repair_order=ro_name,
			remove_service_indices=[0],
			add_services=[{"service_title": "Replacement Service C", "labor_hours": 1.5, "labor_rate": 100.0}],
			notes="Updated scope after inspection",
		)
		self.assertTrue(amend_res["success"])

		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(len(ro.services), 2)
		titles = [s.service_title for s in ro.services]
		self.assertNotIn("Initial Service A", titles)
		self.assertIn("Initial Service B", titles)
		self.assertIn("Replacement Service C", titles)

	def test_amend_quote_blocked_on_approved_order(self):
		"""Verify amend_quote strictly rejects modifications if the quote has already been approved."""
		res = create_quote(
			customer_name=f"{PREFIX}Agent Cust 4",
			vehicle_identifier="5YJ3E1EBXNF999004",
			services=[{"service_title": "Service A", "labor_hours": 1.0}],
		)
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		ro = frappe.get_doc("Repair Order", ro_name)
		ro.approval_status = "Approved"
		ro.save(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			amend_quote(repair_order=ro_name, add_services=[{"service_title": "Service B"}])

	def test_find_available_slots(self):
		"""Verify find_available_slots returns valid slot recommendations."""
		res = find_available_slots(duration_minutes=60, start_date=today(), days_ahead=2)
		self.assertIn("summary_card", res)
		self.assertIn("duration_minutes", res["data"])
		self.assertEqual(res["data"]["duration_minutes"], 60)

	def test_book_appointment_and_realtime_broadcast(self):
		"""Verify book_appointment allocates a Schedule Entry and emits a realtime event."""
		res = create_quote(
			customer_name=CUSTOMER,
			vehicle_identifier=VEHICLE_VIN,
			services=[{"service_title": "Service Book", "labor_hours": 1.0}],
		)
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		with patch("frappe.publish_realtime") as mock_realtime:
			book_res = book_appointment(
				repair_order=ro_name,
				scheduled_date=today(),
				start_time="09:00:00",
				service_bay=f"{PREFIX}Bay 1",
			)
			self.assertTrue(book_res["success"])
			se_name = book_res["data"]["name"]
			self.track("Schedule Entry", se_name)

			# Verify realtime event emitted
			mock_realtime.assert_called()
			event_name = mock_realtime.call_args[0][0]
			self.assertEqual(event_name, "ism_calendar_update")

	def test_send_quote_for_approval_generates_token(self):
		"""Verify send_quote_for_approval generates a secure token and URL."""
		res = create_quote(
			customer_name=CUSTOMER,
			vehicle_identifier=VEHICLE_VIN,
			services=[{"service_title": "Service Auth", "labor_hours": 1.0}],
		)
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		auth_res = send_quote_for_approval(repair_order=ro_name, communication_medium="email")
		self.assertTrue(auth_res["success"])
		self.assertIn("/quote-approval?token=", auth_res["urls"]["approval"])
		self.assertTrue(len(auth_res["data"]["token"]) >= 16)

	def test_import_supplier_catalog(self):
		"""Verify bulk importing parts via CSV content."""
		csv_data = "Part Number,Part Description,Price\nAGY-PAD-001,Front Ceramic Pads,85.00\nAGY-ROT-002,Vented Brake Rotor,120.00"
		res = import_supplier_catalog(raw_text=csv_data)
		self.assertTrue(res["success"])
		self.assertIn("summary_card", res)
		self.assertIn("Created Items", res["summary_card"])

	def test_import_tesla_procedure(self):
		"""Verify importing a Tesla procedure creates a Service Package."""
		res = import_tesla_procedure(
			procedure_title="Replace Front Damper Assembly",
			correction_code="1001-0102",
			labor_hours=2.5,
			model="Model Y",
		)
		self.assertTrue(res["success"])
		pkg_name = res["data"]["name"]
		self.track("Service Package", pkg_name)

		pkg = frappe.get_doc("Service Package", pkg_name)
		self.assertEqual(pkg.applicable_model, "Model Y")
		self.assertAlmostEqual(pkg.standard_labor_hours, 2.5, places=1)

	def test_get_shop_floor_status(self):
		"""Verify get_shop_floor_status returns aggregated floor metrics."""
		res = get_shop_floor_status(date=today())
		self.assertTrue(res["success"])
		self.assertIn("Active Bays", res["summary_card"])
		self.assertIn("bays_count", res["data"])
		self.assertIn("technicians_on_duty", res["data"])

	def test_update_job_status_authorization_lock(self):
		"""Verify update_job_status strictly blocks starting an unapproved Repair Order."""
		res = create_quote(
			customer_name=CUSTOMER,
			vehicle_identifier=VEHICLE_VIN,
			services=[{"service_title": "Lock Test", "labor_hours": 1.0}],
		)
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		# Attempting to start work on an unapproved RO must raise ValidationError
		with self.assertRaises(frappe.ValidationError):
			update_job_status(repair_order=ro_name, action="start")

		# Once approved, starting work succeeds
		ro = frappe.get_doc("Repair Order", ro_name)
		ro.approval_status = "Approved"
		ro.save(ignore_permissions=True)

		start_res = update_job_status(repair_order=ro_name, action="start")
		self.assertTrue(start_res["success"])
		self.assertEqual(start_res["data"]["status"], "Authorized - In Progress")

		# Holding for parts succeeds
		hold_res = update_job_status(repair_order=ro_name, action="hold")
		self.assertEqual(hold_res["data"]["status"], "Waiting on Parts")

		# Completing job succeeds
		comp_res = update_job_status(repair_order=ro_name, action="complete", odometer_out=10500)
		self.assertEqual(comp_res["data"]["status"], "Work Completed")
