# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

from ism.api.agent import (
	add_deferred_services,
	add_supplement,
	amend_quote,
	apply_inspection_template,
	book_appointment,
	check_parts_stock,
	checkout_repair_order,
	create_quote,
	create_ro_from_appointment,
	create_shop_action_item,
	decode_vehicle_vin,
	duplicate_service_for_opposite_side,
	estimate_duration,
	fetch_tesla_manual,
	find_available_slots,
	get_check_in_inspection,
	get_component_alternates,
	get_deferred_services,
	get_inspection_template,
	get_shop_floor_status,
	get_tech_overview,
	get_unscheduled_services,
	get_vehicle_intelligence,
	import_supplier_catalog,
	import_tesla_procedure,
	link_vehicle_customer,
	list_inspection_templates,
	list_shop_action_items,
	receive_purchase_order,
	record_check_in_inspection,
	reschedule_appointment,
	resolve_shop_action_item,
	search_vehicles,
	send_quote_for_approval,
	submit_procurement_orders,
	toggle_service_completion,
	update_appointment_status,
	update_job_status,
)
from ism.ism.doctype.repair_order.repair_order import _get_default_labor_rate
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

		# Verify compact default response envelope
		self.assertNotIn("summary_card", res)
		self.assertNotIn("summary_markdown", res)
		self.assertNotIn("repair_order", res)
		self.assertNotIn("parts_html", res["data"]["services"][0])
		self.assertIn("urls", res)
		self.assertIn("/app/repair-order/", res["urls"]["desk"])
		self.assertIn("/printview?", res["urls"]["print"])

		# Verify opt-in summary_card works when requested
		res_summary = create_quote(
			customer_name=f"{PREFIX}Agent Cust 1b",
			vehicle_identifier="5YJ3E1EBXNF999009",
			services=[{"service_title": "Brake Inspection", "labor_hours": 1.0, "labor_rate": 140.0}],
			include_summary=True,
		)
		self.track("Repair Order", res_summary["data"]["name"])
		self.assertIn("summary_card", res_summary)
		self.assertIn("Brake Inspection", res_summary["summary_card"])

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
		"""Verify find_available_slots returns compact slot recommendations by default."""
		res = find_available_slots(duration_minutes=60, start_date=today(), days_ahead=2)
		self.assertNotIn("summary_card", res)
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

			# Verify realtime event emitted once by ScheduleEntry.on_update (not duplicated)
			mock_realtime.assert_called()
			calendar_calls = [c for c in mock_realtime.call_args_list if c.args and c.args[0] == "ism_calendar_update"]
			self.assertEqual(len(calendar_calls), 1)

	def test_send_quote_for_approval_generates_token(self):
		"""Verify send_quote_for_approval generates a secure token and enforces email presence."""
		res = create_quote(
			customer_name=CUSTOMER,
			vehicle_identifier=VEHICLE_VIN,
			services=[{"service_title": "Service Auth", "labor_hours": 1.0}],
		)
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		auth_res = send_quote_for_approval(repair_order=ro_name, communication_medium="link")
		self.assertTrue(auth_res["success"])
		self.assertIn("/quote-approval?token=", auth_res["urls"]["approval"])
		self.assertTrue(len(auth_res["data"]["token"]) >= 16)

		with self.assertRaises(frappe.ValidationError):
			send_quote_for_approval(repair_order=ro_name, communication_medium="email")

	def test_import_supplier_catalog(self):
		"""Verify bulk importing parts via CSV content links supplier and formats compact metrics."""
		csv_data = "Part Number,Part Description,Price\nAGY-PAD-001,Front Ceramic Pads,85.00\nAGY-ROT-002,Vented Brake Rotor,120.00"
		res = import_supplier_catalog(raw_text=csv_data, supplier=f"{PREFIX}Supplier", include_summary=True)
		self.assertTrue(res["success"])
		self.assertEqual(res["data"]["total_created"], 2)
		self.assertEqual(res["data"]["supplier"], f"{PREFIX}Supplier")
		self.assertIn("summary_card", res)
		self.assertIn("Created Items", res["summary_card"])

	def test_import_tesla_procedure(self):
		"""Verify importing a Tesla procedure attaches parts and upserts cleanly."""
		res = import_tesla_procedure(
			procedure_title="Replace Front Damper Assembly",
			correction_code="1001-0102",
			labor_hours=2.5,
			model="Model Y",
			parts=[{"part_name": "Front Damper Bolt", "part_number": "1044341-00-Z", "qty": 2}],
		)
		self.assertTrue(res["success"])
		pkg_name = res["data"]["name"]
		self.track("Service Package", pkg_name)

		pkg = frappe.get_doc("Service Package", pkg_name)
		self.assertEqual(pkg.applicable_model, "Model Y")
		self.assertAlmostEqual(pkg.standard_labor_hours, 2.5, places=1)
		self.assertGreaterEqual(len(pkg.items), 1)

		# Re-importing with same correction code updates existing Service Package
		res2 = import_tesla_procedure(
			procedure_title="Replace Front Damper Assembly",
			correction_code="1001-0102",
			labor_hours=3.0,
			model="Model Y",
		)
		self.assertEqual(res2["data"]["name"], pkg_name)

	def test_get_shop_floor_status(self):
		"""Verify get_shop_floor_status returns aggregated floor metrics."""
		res = get_shop_floor_status(date=today())
		self.assertTrue(res["success"])
		self.assertIn("summary", res["data"])
		self.assertIn("active_bays", res["data"]["summary"])
		self.assertIn("active_technicians", res["data"]["summary"])

	def test_update_job_status_authorization_lock(self):
		"""Verify update_job_status enforces auth lock and uses manual_parts_hold on hold."""
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

		# Holding for parts sets manual_parts_hold without setting has_shop_floor_override
		hold_res = update_job_status(repair_order=ro_name, action="hold")
		self.assertEqual(hold_res["data"]["status"], "Waiting on Parts")
		ro.reload()
		self.assertEqual(int(ro.manual_parts_hold or 0), 1)
		self.assertEqual(int(ro.has_shop_floor_override or 0), 0)

		# Completing job succeeds and clears manual_parts_hold
		comp_res = update_job_status(repair_order=ro_name, action="complete", odometer_out=10500)
		self.assertEqual(comp_res["data"]["status"], "Work Completed")
		ro.reload()
		self.assertEqual(int(ro.manual_parts_hold or 0), 0)

	def test_create_quote_plate_vehicle_customer_contact_and_default_rate(self):
		"""Verify license-plate vehicle creation, customer contact persistence, and default labor rate."""
		res = create_quote(
			customer_name=f"{PREFIX}Plate Cust",
			vehicle_identifier="ABCD-789",
			customer_phone="416-555-0199",
			customer_email="platecust@example.com",
			services=[{"service_title": "Wheel Torque Check", "labor_hours": 1.0}],
		)
		self.assertTrue(res["success"])
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(ro.services[0].labor_rate, _get_default_labor_rate())
		veh = frappe.get_doc("Repair Vehicle", ro.vehicle)
		self.assertEqual(veh.license_plate, "ABCD-789")
		self.assertTrue(veh.vin.startswith("PLATE"))
		cust = frappe.get_doc("Customer", ro.customer)
		self.assertEqual(cust.mobile_no, "416-555-0199")
		self.assertEqual(cust.email_id, "platecust@example.com")

	def test_extended_facade_endpoints(self):
		"""Verify check_parts_stock, submit_procurement_orders (draft), tech overview, and unscheduled services."""
		res = create_quote(
			customer_name=CUSTOMER,
			vehicle_identifier=VEHICLE_VIN,
			services=[{"service_title": "Stock & Tech Check", "labor_hours": 1.0}],
		)
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		stock_res = check_parts_stock(repair_order=ro_name)
		self.assertTrue(stock_res["success"])
		self.assertIn("all_available", stock_res["data"])

		tech_res = get_tech_overview(date=today())
		self.assertTrue(tech_res["success"])
		self.assertIn("total_scheduled_hours", tech_res["data"])

		unsched_res = get_unscheduled_services(repair_order=ro_name)
		self.assertTrue(unsched_res["success"])
		self.assertGreaterEqual(unsched_res["data"]["unscheduled_count"], 1)

		with patch("ism.api.agent.fetch_and_parse_tesla_procedure", return_value={"title": "Mock Proc"}) as mock_fetch:
			manual_res = fetch_tesla_manual("https://service.tesla.com/docs/mock.html")
			self.assertTrue(manual_res["success"])
			self.assertEqual(manual_res["data"]["title"], "Mock Proc")
			mock_fetch.assert_called_once()

		with patch("ism.api.agent._submit_procurement_orders_impl", return_value={"purchase_orders": ["PO-TEST-1"], "rfqs": []}) as mock_po:
			proc_res = submit_procurement_orders(
				repair_order=ro_name,
				parts_data=[{"item_code": "OEM-123", "qty": 1, "supplier": "Tesla"}],
			)
			self.assertTrue(proc_res["success"])
			self.assertFalse(proc_res["data"]["submitted"])
			self.assertEqual(proc_res["data"]["purchase_orders"], ["PO-TEST-1"])
			self.assertFalse(mock_po.call_args.kwargs["submit"])

	def test_updated_capabilities_workflow(self):
		"""End-to-end smoke test covering Chunk 1 compact envelopes, wrappers, and hold isolation."""
		# 1. Compact intake with license plate, customer contact, and server default labor rate
		quote_res = create_quote(
			customer_name=f"{PREFIX}Workflow Cust",
			vehicle_identifier="WORK-123",
			customer_phone="416-555-0142",
			customer_email="workflow@example.com",
			services=[{"service_title": "Brake Inspection", "labor_hours": 1.0}],
		)
		self.assertTrue(quote_res["success"])
		self.assertNotIn("summary_card", quote_res)
		self.assertNotIn("summary_markdown", quote_res)
		self.assertNotIn("parts_html", quote_res["data"]["services"][0])
		self.assertIn("/app/repair-order/", quote_res["urls"]["desk"])
		self.assertIn("/printview?", quote_res["urls"]["print"])

		ro_name = quote_res["data"]["name"]
		self.track("Repair Order", ro_name)
		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(ro.services[0].labor_rate, _get_default_labor_rate())

		cust = frappe.get_doc("Customer", ro.customer)
		self.assertEqual(cust.mobile_no, "416-555-0142")
		self.assertEqual(cust.email_id, "workflow@example.com")
		veh = frappe.get_doc("Repair Vehicle", ro.vehicle)
		self.assertEqual(veh.license_plate, "WORK-123")
		self.assertTrue(veh.vin.startswith("PLATE"))

		# 2. Amend quote and verify curated unscheduled and parts stock wrappers
		amend_res = amend_quote(
			repair_order=ro_name,
			add_services=[{"service_title": "Caliper Service", "labor_hours": 1.5}],
		)
		self.assertEqual(amend_res["data"]["total_services"], 2)

		unsched_res = get_unscheduled_services(repair_order=ro_name)
		self.assertEqual(unsched_res["data"]["unscheduled_count"], 2)

		stock_res = check_parts_stock(repair_order=ro_name)
		self.assertIn("all_available", stock_res["data"])

		# 3. Approval link generation via unified approval module
		auth_res = send_quote_for_approval(repair_order=ro_name, communication_medium="link")
		self.assertIn("/quote-approval?token=", auth_res["urls"]["approval"])
		self.assertTrue(auth_res["data"]["token"])

		# 4. Approve RO, place on manual parts hold, and verify draft-first procurement
		ro.reload()
		ro.approval_status = "Approved"
		ro.save(ignore_permissions=True)

		hold_res = update_job_status(repair_order=ro_name, action="hold", notes="Waiting for caliper")
		self.assertEqual(hold_res["data"]["status"], "Waiting on Parts")
		ro.reload()
		self.assertEqual(int(ro.manual_parts_hold or 0), 1)
		self.assertEqual(int(ro.has_shop_floor_override or 0), 0)

		with patch("ism.api.agent._submit_procurement_orders_impl", return_value={"purchase_orders": ["PO-WF-1"], "rfqs": []}) as mock_po:
			proc_res = submit_procurement_orders(
				repair_order=ro_name,
				parts_data=[{"item_code": "OEM-CALIPER", "qty": 1, "supplier": "Tesla"}],
			)
			self.assertFalse(proc_res["data"]["submitted"])
			self.assertFalse(mock_po.call_args.kwargs["submit"])

		# 5. Opt-in summary card on shop floor status
		floor_res = get_shop_floor_status(date=today(), include_summary=True)
		self.assertIn("summary_card", floor_res)
		self.assertIn("Shop Floor Status", floor_res["summary_card"])

	def test_accelerations_and_live_bug_fixes(self):
		"""Verify Reman normalization, deficit calculation, slot IDs, PO receiving, and checkout."""
		cat = import_supplier_catalog(
			raw_text="ACC-CAL-01 | Rear Reman Caliper | OEM | Reman | 320.00 | 210.00 | 0 | A | 1188611-00-R",
			supplier=f"{PREFIX}Parts Co",
			default_source="OEM",
			default_condition="Reman",
		)
		created_item = cat["data"]["created"][0]
		self.assertEqual(created_item["part_condition"], "Reconditioned")
		self.assertEqual(created_item["item_code"], "OEM-ACC-CAL-01-RCN")

		q = create_quote(
			customer_name=f"{PREFIX}Fast Flow Cust",
			vehicle_identifier="FAST-888",
			services=[{
				"service_title": "Caliper Replace",
				"labor_hours": 1.0,
				"parts": [{"item_code": "ACC-CAL-01", "qty": 1, "rate": 320.0}],
			}],
			odometer_in=12000,
		)
		ro_name = q["data"]["name"]
		self.track("Repair Order", ro_name)
		self.assertEqual(q["data"]["license_plate"], "FAST-888")

		with self.assertRaises(frappe.ValidationError):
			amend_quote(
				repair_order=ro_name,
				add_services=[{"service_title": "Bad Part", "parts": [{"item_code": "NO-SUCH-SKU-999", "qty": 1}]}],
			)

		stock_before = check_parts_stock(repair_order=ro_name)
		self.assertFalse(stock_before["data"]["all_available"])
		self.assertGreaterEqual(stock_before["data"]["deficit_count"], 1)
		part_info = stock_before["data"]["items"]["OEM-ACC-CAL-01-RCN"]
		self.assertIn("Store", part_info["warehouse"])
		self.assertEqual(part_info["deficit"], 1.0)

		slots = find_available_slots(repair_order=ro_name, start_date=today(), days_ahead=2)
		if slots["data"]["slots"]:
			self.assertIn("available_bay_ids", slots["data"]["slots"][0])

		auth = send_quote_for_approval(repair_order=ro_name, communication_medium="link")
		self.assertGreaterEqual(len(auth["data"]["services"]), 1)

		ro = frappe.get_doc("Repair Order", ro_name)
		ro.approval_status = "Approved"
		ro.save(ignore_permissions=True)

		book_res = book_appointment(repair_order=ro_name, scheduled_date=today(), start_time="10:00:00", service_bay=f"{PREFIX}Bay 1")
		se_name = book_res["data"]["name"]
		self.track("Schedule Entry", se_name)

		po_res = submit_procurement_orders(
			repair_order=ro_name,
			parts_data=[{"item_code": "OEM-ACC-CAL-01-RCN", "qty": 1, "supplier": f"{PREFIX}Parts Co"}],
			submit=False,
		)
		po_name = po_res["data"]["purchase_orders"][0]
		self.track("Purchase Order", po_name)
		po_doc = frappe.get_doc("Purchase Order", po_name)
		self.assertAlmostEqual(po_doc.items[0].rate, 210.0, places=2)

		with self.assertRaises(frappe.ValidationError):
			receive_purchase_order(purchase_order=po_name, submit_po=False)

		rcv_res = receive_purchase_order(purchase_order=po_name, submit_po=True)
		self.assertTrue(rcv_res["success"])
		self.track("Purchase Receipt", rcv_res["data"]["purchase_receipt"])
		self.assertEqual(rcv_res["data"]["per_received"], 100.0)

		update_job_status(repair_order=ro_name, action="complete", odometer_out=12050, notes="Done")
		with self.assertRaises(frappe.ValidationError):
			checkout_repair_order(repair_order=ro_name, confirm=False)

		chk = checkout_repair_order(repair_order=ro_name, payment_method="Debit / Interac", odometer_out=12050, confirm=True)
		self.assertTrue(chk["success"])
		self.assertEqual(chk["data"]["status"], "Closed / Released")
		self.assertEqual(chk["data"]["payment_status"], "Paid")
		if chk["data"].get("sales_invoice"):
			self.track("Sales Invoice", chk["data"]["sales_invoice"])
		if chk["data"].get("payment_entry"):
			self.track("Payment Entry", chk["data"]["payment_entry"])
		self.assertEqual(frappe.db.get_value("Schedule Entry", se_name, "status"), "Completed")

	def test_chunk2_floor_supplements_deferred_and_action_items(self):
		"""Verify deferred service discovery/intake, opposite-side duplication, supplements, service completion, bay park, and shop action items."""
		veh_doc = frappe.get_doc("Repair Vehicle", VEHICLE_VIN)
		veh_doc.append(
			"deferred_services",
			{
				"job_title": "Cabin Air Filter Replacement",
				"estimated_amount": 95.0,
				"status": "Open Recommendation",
				"decline_reason": "Recommended during previous inspection",
			},
		)
		veh_doc.save(ignore_permissions=True)

		# 1. Deferred discovery standalone + surfaced automatically inside create_quote
		def_res = get_deferred_services(vehicle=VEHICLE_VIN)
		self.assertTrue(def_res["success"])
		self.assertGreaterEqual(def_res["data"]["deferred_count"], 1)

		q_res = create_quote(
			customer_name=CUSTOMER,
			vehicle_identifier=VEHICLE_VIN,
			notes="Front suspension rattle",
			services=[{"service_title": "Front Left Control Arm Replacement", "labor_hours": 1.5, "labor_rate": 140.0}],
		)
		self.assertTrue(q_res["success"])
		ro_name = q_res["data"]["name"]
		self.track("Repair Order", ro_name)
		self.assertGreaterEqual(q_res["data"].get("deferred_count", 0), 1)
		self.assertIn("deferred_services", q_res["data"])

		# 2. Add deferred service onto RO
		add_def = add_deferred_services(repair_order=ro_name, deferred_items=["Cabin Air Filter Replacement"])
		self.assertTrue(add_def["success"])
		self.assertGreaterEqual(len(add_def["data"]["services"]), 2)

		# 3. Duplicate Left service for Right side
		first_row_id = q_res["data"]["services"][0]["name"]
		dup_res = duplicate_service_for_opposite_side(repair_order=ro_name, service_row_id=first_row_id)
		self.assertTrue(dup_res["success"])
		svc_titles = [s["service_title"] for s in dup_res["data"]["services"]]
		self.assertTrue(any("Right" in title for title in svc_titles), svc_titles)

		# 4. Approve RO and exercise floor actions: toggle completion, supplement, park
		ro_doc = frappe.get_doc("Repair Order", ro_name)
		ro_doc.approval_status = "Approved"
		ro_doc.status = "Authorized - In Progress"
		ro_doc.save(ignore_permissions=True)

		tog = toggle_service_completion(repair_order=ro_name, service_row_id=first_row_id, is_completed=True)
		self.assertTrue(tog["success"])
		self.assertEqual(tog["data"]["services"][0]["is_completed"], 1)
		self.assertGreater(tog["data"]["progress_percent"], 0)

		sup = add_supplement(
			repair_order=ro_name,
			service_title="Subframe Alignment Bolt Kit",
			notes="Seized during tear-down",
			labor_hours=0.5,
		)
		self.assertTrue(sup["success"])
		self.assertTrue(any(s.get("is_supplement") for s in sup["data"]["services"]))

		park = update_job_status(repair_order=ro_name, action="park", notes="Waiting on alignment rack")
		self.assertTrue(park["success"])
		self.assertEqual(park["data"]["vehicle_location"], "In Customer Lot")

		# 5. Shop Action Items lifecycle
		created = create_shop_action_item(
			title=f"{PREFIX} Rust-seized rear knuckle",
			category="Shop Floor Discrepancy",
			priority="High",
			reference_doctype="Repair Order",
			reference_name=ro_name,
			details="Needs torch extraction",
		)
		self.assertTrue(created["success"])
		item_name = created["data"]["name"]
		self.track("Shop Action Item", item_name)

		listed = list_shop_action_items(status="Open")
		self.assertTrue(listed["success"])
		self.assertTrue(any(row["name"] == item_name for row in listed["data"]["items"]))

		resolved = resolve_shop_action_item(action_item=item_name, resolution_notes="Extracted safely")
		self.assertTrue(resolved["success"])
		self.assertEqual(resolved["data"]["status"], "Resolved")

	def test_live_simulation_fixes_package_expansion_sms_fallback_and_batch_toggle(self):
		"""Verify Service Package expansion, SMS link fallback, start bay location, Repair entry_type, and batch toggle."""
		part = frappe.get_doc({
			"doctype": "Item",
			"item_code": f"{PREFIX}LINK-PART-01",
			"item_name": "Test Compliance Link",
			"item_group": "Products",
			"is_stock_item": 0,
			"standard_rate": 190.0,
		}).insert(ignore_permissions=True)
		self.track("Item", part.name)

		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": f"{PREFIX}Lateral Link LH (Remove and Replace)",
			"correction_code": "31-03-45-02",
			"is_symmetrical": 1,
			"standard_labor_hours": 0.9,
			"hourly_rate": 135.0,
			"items": [{"item_code": part.name, "qty": 1, "rate": 190.0, "side": "LH"}],
		}).insert(ignore_permissions=True)
		self.track("Service Package", pkg.name)

		# 1. Quote by package title expands FRT hours, correction code, position, and parts; explicit override wins
		q = create_quote(
			customer_name=CUSTOMER,
			vehicle_identifier=VEHICLE_VIN,
			services=[
				pkg.package_name,
				{"service_package": pkg.name, "service_title": "Custom Override Link", "labor_hours": 1.4},
			],
		)
		self.assertTrue(q["success"])
		ro_name = q["data"]["name"]
		self.track("Repair Order", ro_name)

		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(len(ro.services), 2)
		self.assertEqual(ro.services[0].service_package, pkg.name)
		self.assertAlmostEqual(ro.services[0].labor_hours, 0.9, places=2)
		self.assertAlmostEqual(ro.services[0].parts_total, 190.0, places=2)
		self.assertEqual(ro.services[0].correction_code, "31-03-45-02")
		self.assertEqual(ro.services[0].service_position, "LH")
		self.assertAlmostEqual(ro.services[1].labor_hours, 1.4, places=2)
		self.assertAlmostEqual(ro.services[1].parts_total, 190.0, places=2)

		stock = check_parts_stock(repair_order=ro_name)
		self.assertIn("1 unique part SKU.", stock["message"])

		# 2. SMS unconfigured falls back cleanly to generating the 48h approval link
		settings = frappe.get_single("ISM Settings")
		prev_provider = getattr(settings, "sms_gateway_provider", None)
		prev_endpoint = getattr(settings, "sms_gateway_endpoint", None)
		settings.sms_gateway_provider = "Disabled"
		settings.sms_gateway_endpoint = ""
		settings.save(ignore_permissions=True)
		try:
			auth = send_quote_for_approval(repair_order=ro_name, communication_medium="sms")
			self.assertTrue(auth["success"])
			self.assertEqual(auth["data"]["channel"], "link")
			self.assertEqual(auth["data"]["delivery_status"], "Link Only (SMS Unconfigured)")
			self.assertIn("/quote-approval?token=", auth["urls"]["approval"])
		finally:
			settings.sms_gateway_provider = prev_provider
			settings.sms_gateway_endpoint = prev_endpoint
			settings.save(ignore_permissions=True)

		# 3. Approve, book Repair entry_type, start into bay, and batch-toggle both rows in one call
		ro.reload()
		ro.approval_status = "Approved"
		for svc in ro.services:
			svc.status = "Approved"
		ro.save(ignore_permissions=True)

		book_res = book_appointment(
			repair_order=ro_name,
			scheduled_date=today(),
			start_time="11:00:00",
			service_bay=f"{PREFIX}Bay 1",
		)
		self.track("Schedule Entry", book_res["data"]["name"])
		self.assertEqual(book_res["data"]["entry_type"], "Repair")

		start_res = update_job_status(repair_order=ro_name, action="start")
		self.assertEqual(start_res["data"]["status"], "Authorized - In Progress")
		self.assertEqual(start_res["data"]["vehicle_location"], "In Bay / Being Serviced")

		row_ids = [s.name for s in ro.services]
		batch_tog = toggle_service_completion(repair_order=ro_name, service_row_ids=row_ids, is_completed=True)
		self.assertTrue(batch_tog["success"])
		self.assertEqual(len(batch_tog["data"]["updated_rows"]), 2)
		self.assertTrue(all(s["is_completed"] == 1 for s in batch_tog["data"]["services"]))

	def test_chunk3_scheduling_lifecycle_and_estimation_endpoints(self):
		"""Verify reschedule, status transitions (Confirmed/No Show), create RO from entry, compact slots, and estimate_duration."""
		target_day = add_days(today(), 2)
		q = create_quote(
			customer_name=CUSTOMER,
			vehicle_identifier=VEHICLE_VIN,
			services=[{"service_title": "Suspension Noise Check", "labor_hours": 1.0}],
		)
		ro_name = q["data"]["name"]
		self.track("Repair Order", ro_name)

		est = estimate_duration(repair_order=ro_name, include_summary=True)
		self.assertTrue(est["success"])
		self.assertGreater(est["data"]["total_duration_minutes"], 0)
		self.assertIn("summary_card", est)

		slots_compact = find_available_slots(repair_order=ro_name, start_date=target_day, days_ahead=2)
		self.assertIn("resources", slots_compact["data"])
		if slots_compact["data"]["slots"]:
			self.assertIn("available_bay_ids", slots_compact["data"]["slots"][0])
			self.assertNotIn("available_technician_ids", slots_compact["data"]["slots"][0])

		slots_full = find_available_slots(
			repair_order=ro_name,
			start_date=target_day,
			days_ahead=2,
			include_resources=True,
		)
		if slots_full["data"]["slots"] and slots_full["data"]["slots"][0].get("available_technicians") is not None:
			self.assertIn("available_technician_ids", slots_full["data"]["slots"][0])

		with patch("frappe.publish_realtime") as mock_realtime:
			booked = book_appointment(
				repair_order=ro_name,
				scheduled_date=target_day,
				start_time="09:00:00",
				service_bay=f"{PREFIX}Bay 1",
			)
		calendar_calls = [c for c in mock_realtime.call_args_list if c.args and c.args[0] == "ism_calendar_update"]
		self.assertEqual(len(calendar_calls), 1)
		se_name = booked["data"]["name"]
		self.track("Schedule Entry", se_name)

		# Child row synced without firing full RO validate again
		ro_doc = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(len(ro_doc.schedule_entries), 1)
		self.assertEqual(ro_doc.schedule_entries[0].schedule_entry, se_name)

		confirmed = update_appointment_status(schedule_entry=se_name, status="Confirmed")
		self.assertEqual(confirmed["data"]["status"], "Confirmed")

		rescheduled = reschedule_appointment(
			schedule_entry=se_name,
			start_time="10:30:00",
			notes="Moved back 90 min",
			include_summary=True,
		)
		self.assertTrue(rescheduled["success"])
		self.assertTrue(str(rescheduled["data"]["scheduled_time"]).startswith("10:30"))
		self.assertIn("summary_card", rescheduled)

		no_show = update_appointment_status(schedule_entry=se_name, status="No Show")
		self.assertEqual(no_show["data"]["status"], "No Show")

		# Pre-intake Diagnostic SE converts cleanly into a Checked In RO
		pre_se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"customer": CUSTOMER,
			"repair_vehicle": VEHICLE_VIN,
			"scheduled_date": target_day,
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"service_bay": f"{PREFIX}Bay 2",
			"notes": "Walk-in vibration check",
			"status": "Scheduled",
		}).insert(ignore_permissions=True)
		self.track("Schedule Entry", pre_se.name)

		created_ro = create_ro_from_appointment(schedule_entry=pre_se.name)
		self.assertTrue(created_ro["success"])
		new_ro_name = created_ro["data"]["repair_order"]
		self.track("Repair Order", new_ro_name)
		self.assertEqual(frappe.db.get_value("Schedule Entry", pre_se.name, "repair_order"), new_ro_name)

		# Holiday dates must be skipped by find_available_slots
		with patch(
			"ism.scheduling.capacity.ShopCapacityWindow._load_holidays",
			return_value={target_day: "Shop Holiday"},
		):
			holiday_slots = find_available_slots(duration_minutes=60, start_date=target_day, days_ahead=2)
		self.assertIn(target_day, holiday_slots["data"].get("skipped_holidays", []))
		self.assertTrue(all(s["date"] != target_day for s in holiday_slots["data"]["slots"]))

	def test_asap_past_slot_skipping_least_scarce_bays_and_provisional_booking(self):
		"""Verify future slot cutoff, full-day slots, least-scarce bay order, provisional book, and status breakdown."""
		from datetime import datetime
		from ism.api.technician_availability import get_daily_technician_overview

		# Fixed future date avoids midnight test edge cases while verifying min_start_time + non-truncated slots
		future_day = add_days(today(), 4)
		midmorning = find_available_slots(
			duration_minutes=30,
			start_date=future_day,
			days_ahead=1,
			equipment_tags=["Lift"],
			min_start_time="09:45:00",
			max_slots=15,
		)
		returned_times = [s["start_time"] for s in midmorning["data"]["slots"]]
		self.assertGreater(len(returned_times), 3)
		self.assertEqual(returned_times[0], "10:00")
		self.assertNotIn("08:00", returned_times)
		self.assertEqual(midmorning["data"]["slots"][0]["available_bay_ids"], [f"{PREFIX}Bay 1", f"{PREFIX}Bay 2"])

		# Automatic today cutoff skips elapsed slots in now_datetime()
		with patch("ism.api.agent.now_datetime", return_value=datetime.strptime(f"{today()} 11:10:00", "%Y-%m-%d %H:%M:%S")):
			asap_today = find_available_slots(duration_minutes=30, start_date=today(), days_ahead=1)
		self.assertTrue(asap_today["data"]["slots"])
		self.assertGreaterEqual(asap_today["data"]["slots"][0]["start_time"], "11:30")

		# Provisional standalone Diagnostic booking succeeds without Repair Order
		tech_ids = list(midmorning["data"]["resources"]["technicians"].keys())
		tech_id = tech_ids[-1]
		walkin = book_appointment(
			scheduled_date=future_day,
			start_time="10:00:00",
			service_bay=f"{PREFIX}Bay 1",
			technician=tech_id,
			entry_type="Diagnostic",
			estimated_duration=30,
			provisional_customer_name="ASAP Walk-In",
			provisional_vehicle_info="Model 3",
			include_summary=True,
		)
		self.assertTrue(walkin["success"])
		se_name = walkin["data"]["name"]
		self.track("Schedule Entry", se_name)
		self.assertEqual(walkin["data"]["estimated_duration"], 30)
		self.assertIn("ASAP Walk-In", walkin["summary_card"])

		# Completed and No Show do not count as active allocations, and Completed vs remaining tech load is split
		update_appointment_status(schedule_entry=se_name, status="Completed")
		floor = get_shop_floor_status(date=future_day)
		self.assertEqual(floor["data"]["summary"]["completed_appointments"], 1)
		self.assertEqual(floor["data"]["summary"]["active_allocations"], 0)

		overview = get_daily_technician_overview(future_day)
		t2_row = next(r for r in overview["technicians"] if r["employee"] == tech_id)
		self.assertEqual(t2_row["completed_minutes"], 30)
		self.assertEqual(t2_row["remaining_minutes"], 0)

	def test_default_labor_rate_reads_ism_settings(self):
		"""Verify _get_default_labor_rate reads Single DocType ISM Settings.default_labor_rate (140.0 and custom)."""
		prev_rate = frappe.db.get_single_value("ISM Settings", "default_labor_rate")
		try:
			frappe.db.set_single_value("ISM Settings", "default_labor_rate", 140.0)
			frappe.clear_cache(doctype="ISM Settings")
			self.assertAlmostEqual(_get_default_labor_rate(), 140.0, places=2)

			frappe.db.set_single_value("ISM Settings", "default_labor_rate", 165.0)
			frappe.clear_cache(doctype="ISM Settings")
			self.assertAlmostEqual(_get_default_labor_rate(), 165.0, places=2)

			q = create_quote(
				customer_name=CUSTOMER,
				vehicle_identifier=VEHICLE_VIN,
				services=[{"service_title": "Custom Rate Verification", "labor_hours": 1.0}],
			)
			self.track("Repair Order", q["data"]["name"])
			self.assertAlmostEqual(q["data"]["services"][0]["labor_rate"], 165.0, places=2)
		finally:
			frappe.db.set_single_value("ISM Settings", "default_labor_rate", prev_rate or 140.0)
			frappe.clear_cache(doctype="ISM Settings")

	def test_structured_import_supplier_catalog_and_component_alternates(self):
		"""Verify structured items catalog ingestion, supplier linking, and component alternate lookup."""
		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": f"{PREFIX}Front Control Arm Package",
			"applicable_model": "Model 3",
			"standard_labor_hours": 1.2,
		}).insert(ignore_permissions=True)
		self.track("Service Package", pkg.name)

		oem_pn = "1044341-00-D"
		structured_items = [
			{
				"part_number": oem_pn,
				"part_name": "Front Lower Control Arm",
				"part_source": "OEM",
				"part_condition": "New",
				"unit_rate": 245.0,
				"valuation_rate": 170.0,
				"qty": 2,
				"oem_part_number": oem_pn,
				"slot_number": 1,
				"is_default": 1,
			},
			{
				"part_number": "AFT-LCA-01",
				"part_name": "Aftermarket Lower Control Arm",
				"part_source": "Aftermarket",
				"part_condition": "Reman",
				"unit_rate": 160.0,
				"valuation_rate": 100.0,
				"qty": 1,
				"oem_part_number": oem_pn,
				"slot_number": 1,
				"is_default": 0,
			},
		]
		res = import_supplier_catalog(
			items=json.dumps(structured_items),
			supplier=f"{PREFIX}Structured Supplier",
			service_package=pkg.name,
			include_summary=True,
		)
		self.assertTrue(res["success"])
		self.assertEqual(res["data"]["total_created"], 2)
		self.assertEqual(res["data"]["total_errors"], 0)
		self.assertIn("summary_card", res)

		oem_code = res["data"]["created"][0]["item_code"]
		aft_code = res["data"]["created"][1]["item_code"]
		self.track("Item", oem_code)
		self.track("Item", aft_code)
		self.assertEqual(res["data"]["created"][1]["part_condition"], "Reconditioned")

		alt_res = get_component_alternates(
			service_package=pkg.name,
			component_slot="Slot 1",
			oem_part_number=oem_pn,
			include_summary=True,
		)
		self.assertTrue(alt_res["success"])
		self.assertGreaterEqual(alt_res["data"]["count"], 2)
		alt_codes = {a["item_code"] for a in alt_res["data"]["alternates"]}
		self.assertIn(oem_code, alt_codes)
		self.assertIn(aft_code, alt_codes)
		self.assertIn("Component Alternates", alt_res["summary_card"])

	def test_tesla_on_conflict_force_and_fetch_compatibility(self):
		"""Verify fetch_tesla_manual vehicle forwarding and import_tesla_procedure on_conflict/force policies."""
		# VEHICLE_VIN is a 2012 Model S ("5YJSA1E26CF000001")
		q = create_quote(
			customer_name=CUSTOMER,
			vehicle_identifier=VEHICLE_VIN,
			services=[{"service_title": "Initial Inspection", "labor_hours": 0.5}],
		)
		ro_name = q["data"]["name"]
		self.track("Repair Order", ro_name)

		with patch("ism.api.agent.fetch_and_parse_tesla_procedure", return_value={"title": "Mock Proc", "compatibility": {"is_compatible": True}}) as mock_fetch:
			manual_res = fetch_tesla_manual(
				"https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-TEST.html",
				repair_order=ro_name,
				vehicle=VEHICLE_VIN,
				include_summary=True,
			)
			self.assertTrue(manual_res["success"])
			mock_fetch.assert_called_once_with(
				"https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-TEST.html",
				repair_order=ro_name,
				vehicle=VEHICLE_VIN,
			)
			self.assertIn("summary_card", manual_res)

		model3_url = "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-FACADE-01.html"
		proc_payload = {
			"title": "Front Upper Control Arm LH (Remove and Replace)",
			"correction_code": "31012102",
			"rh_correction_code": "31012202",
			"is_symmetrical": 1,
			"frt": 1.1,
			"applicable_model": "Model 3",
			"tesla_manual_url": model3_url,
		}

		# Model 3 procedure on Model S RO must raise ValidationError unless force=True
		with self.assertRaises(frappe.ValidationError):
			import_tesla_procedure(repair_order=ro_name, data=proc_payload, force=False)

		added = import_tesla_procedure(
			repair_order=ro_name,
			data=proc_payload,
			on_conflict="skip",
			force=True,
			side="Both",
			advisor_notes="Customer reported clunk",
			include_summary=True,
		)
		self.assertTrue(added["success"])
		self.assertEqual(added["data"]["action"], "added")
		self.assertEqual(added["data"]["count_added"], 2)
		self.assertTrue(added["data"]["warnings"])
		self.assertIn("summary_card", added)
		if added["data"].get("service_package"):
			self.track("Service Package", added["data"]["service_package"])

		skipped = import_tesla_procedure(
			repair_order=ro_name,
			data=proc_payload,
			on_conflict="skip",
			force=True,
			side="LH",
		)
		self.assertEqual(skipped["data"]["action"], "skipped")
		self.assertEqual(skipped["data"]["count_skipped"], 1)

		with self.assertRaises(frappe.DuplicateEntryError):
			import_tesla_procedure(
				repair_order=ro_name,
				data=proc_payload,
				on_conflict="error",
				force=True,
				side="LH",
			)

		overwritten = import_tesla_procedure(
			repair_order=ro_name,
			data={**proc_payload, "frt": 1.4},
			on_conflict="overwrite",
			force=True,
			side="LH",
			advisor_notes="Updated FRT",
		)
		self.assertEqual(overwritten["data"]["action"], "updated")
		self.assertEqual(overwritten["data"]["count_updated"], 1)

	def test_vehicle_intelligence_facade_endpoints(self):
		"""Verify decode_vehicle_vin, search_vehicles, get_vehicle_intelligence, and link_vehicle_customer."""
		decoded = decode_vehicle_vin("5YJ3E1EBXNF999001", include_summary=True)
		self.assertTrue(decoded["success"])
		self.assertEqual(decoded["data"]["model"], "Model 3")
		self.assertEqual(decoded["data"]["model_year"], "2022")
		self.assertIn("recommended_manual_url", decoded["data"])
		self.assertIn("summary_card", decoded)

		bad_vin = decode_vehicle_vin("INVALIDVIN")
		self.assertFalse(bad_vin["success"])
		self.assertTrue(bad_vin.get("error"))

		co_owner = self.create_test_customer(f"{PREFIX}CoOwner")
		veh = self.create_test_vehicle(vin="5YJ3E1EBXNF888123", customer=CUSTOMER, license_plate="FAC-888")

		linked = link_vehicle_customer(
			vehicle=veh.name,
			customer=co_owner.name,
			is_primary=False,
			relationship="Fleet Driver",
			include_summary=True,
		)
		self.assertTrue(linked["success"])
		self.assertEqual(linked["data"]["primary_customer"], CUSTOMER)
		owner_ids = {o["customer"] for o in linked["data"]["owners"]}
		self.assertIn(co_owner.name, owner_ids)
		self.assertIn("summary_card", linked)

		searched = search_vehicles(query="FAC-888", customer=co_owner.name, model="Model 3")
		self.assertTrue(searched["success"])
		self.assertEqual(searched["data"]["count"], 1)
		self.assertEqual(searched["data"]["vehicles"][0]["name"], veh.name)

		intel = get_vehicle_intelligence(vehicle=veh.name, history_limit=10, include_summary=True)
		self.assertTrue(intel["success"])
		self.assertEqual(intel["data"]["vehicle"]["name"], veh.name)
		self.assertEqual(intel["data"]["vin_decode"]["model"], "Model 3")
		self.assertGreaterEqual(len(intel["data"]["owners"]), 2)
		self.assertIn("deferred_services", intel["data"])
		self.assertIn("summary_card", intel)

	def test_approval_hardening_and_checkin_inspection_facade(self):
		"""Verify send_quote_for_approval SMS message_id/warning propagation and check-in inspection endpoints."""
		cust = self.create_test_customer(f"{PREFIX}InspectCust", mobile_no="416-555-0188")
		q = create_quote(
			customer_name=cust.name,
			vehicle_identifier=VEHICLE_VIN,
			services=[{"service_title": "Walkaround & Brake Check", "labor_hours": 1.0}],
		)
		ro_name = q["data"]["name"]
		self.track("Repair Order", ro_name)

		with patch(
			"ism.api.approval.get_sms_unconfigured_reason",
			return_value=None,
		), patch(
			"ism.api.approval.dispatch_sms",
			return_value={"success": True, "message_id": "sms-msg-42"},
		):
			sms_res = send_quote_for_approval(
				repair_order=ro_name,
				communication_medium="sms",
				include_summary=True,
			)
		self.assertTrue(sms_res["success"])
		self.assertEqual(sms_res["data"]["channel"], "sms")
		self.assertEqual(sms_res["data"]["requested_channel"], "sms")
		self.assertEqual(sms_res["data"]["delivery_status"], "Sent")
		self.assertEqual(sms_res["data"]["message_id"], "sms-msg-42")
		self.assertIn("sms-msg-42", sms_res["summary_card"])

		with patch(
			"ism.api.approval.get_sms_unconfigured_reason",
			return_value="SMS gateway is Disabled",
		):
			fallback_res = send_quote_for_approval(repair_order=ro_name, communication_medium="sms")
		self.assertTrue(fallback_res["success"])
		self.assertEqual(fallback_res["data"]["channel"], "link")
		self.assertEqual(fallback_res["data"]["requested_channel"], "sms")
		self.assertEqual(fallback_res["data"]["delivery_status"], "Link Only (SMS Unconfigured)")
		self.assertTrue(fallback_res["data"].get("warning"))

		tpl = frappe.get_doc({
			"doctype": "Inspection Template",
			"template_name": f"{PREFIX}Facade Walkaround",
			"is_active": 1,
			"items": [
				{"inspection_description": "Front Windshield & Glass", "category": "Exterior"},
				{"inspection_description": "Tire Tread & Pressure", "category": "Tires"},
			],
		}).insert(ignore_permissions=True)
		self.track("Inspection Template", tpl.name)

		tpl_list = list_inspection_templates(include_inactive=False)
		self.assertTrue(tpl_list["success"])
		self.assertTrue(any(t["name"] == tpl.name for t in tpl_list["data"]["templates"]))

		tpl_detail = get_inspection_template(template_name=tpl.name)
		self.assertTrue(tpl_detail["success"])
		self.assertEqual(len(tpl_detail["data"]["items"]), 2)

		applied = apply_inspection_template(
			repair_order=ro_name,
			template_name=tpl.name,
			replace_existing=True,
			include_summary=True,
		)
		self.assertTrue(applied["success"])
		self.assertEqual(applied["data"]["added_count"], 2)
		self.assertIn("summary_card", applied)

		recorded = record_check_in_inspection(
			repair_order=ro_name,
			items=[
				{"inspection_description": "Front Windshield & Glass", "status": "ok", "notes": "No chips"},
				{"inspection_description": "Tire Tread & Pressure", "status": "warning", "notes": "Rear low 34 PSI"},
			],
			odometer_in=15250,
			customer_concern="TPMS alert on dash",
			include_summary=True,
		)
		self.assertTrue(recorded["success"])
		self.assertEqual(recorded["data"]["updated_count"], 2)
		self.assertEqual(recorded["data"]["odometer_in"], 15250)
		self.assertEqual(recorded["data"]["summary"]["pass"], 1)
		self.assertEqual(recorded["data"]["summary"]["attention"], 1)
		self.assertTrue(recorded["data"]["summary"]["all_completed"])
		self.assertIn("summary_card", recorded)

		fetched = get_check_in_inspection(repair_order=ro_name, include_summary=True)
		self.assertTrue(fetched["success"])
		self.assertEqual(fetched["data"]["customer_concern"], "TPMS alert on dash")
		self.assertEqual(fetched["data"]["summary"]["total"], 2)
		self.assertIn("summary_card", fetched)

	def test_whitelisted_permissions_and_type_confusion_guards(self):
		"""Verify Guest isolation, type-confusion ValidationErrors, and Guest token approval flow."""
		from ism.api.approval import submit_customer_approval
		from ism.api.estimation_service import estimate_repair_order, get_total_estimate
		from ism.api.scheduling import allocate_schedule_entry, get_available_slots
		from ism.api.scheduling_views import get_calendar_events as get_calendar_view_events
		from ism.api.technician_availability import get_daily_technician_overview
		from ism.ism.doctype.repair_order.repair_order import get_service_package_details
		from ism.ism.doctype.shop_action_item.shop_action_item import (
			create_shop_action_item as create_action_item_doc,
		)

		cust = self.create_test_customer(f"{PREFIX}SecCust", mobile_no="416-555-0199")
		q = create_quote(
			customer_name=cust.name,
			vehicle_identifier=VEHICLE_VIN,
			services=[{"service_title": "Security Guard Brake Check", "labor_hours": 1.0}],
		)
		ro_name = q["data"]["name"]
		self.track("Repair Order", ro_name)
		ro = frappe.get_doc("Repair Order", ro_name)
		svc_row_name = ro.services[0].name

		approval_res = send_quote_for_approval(repair_order=ro_name, communication_medium="link")
		approval_token = approval_res["data"]["token"]

		prev_user = frappe.session.user
		try:
			frappe.set_user("Guest")

			guest_calls = [
				lambda: create_quote(customer_name=cust.name, vehicle_identifier=VEHICLE_VIN),
				lambda: amend_quote(repair_order=ro_name, notes="Guest edit"),
				lambda: find_available_slots(target_date=today()),
				lambda: book_appointment(repair_order=ro_name, scheduled_date=add_days(today(), 1), start_time="10:00:00"),
				lambda: send_quote_for_approval(repair_order=ro_name, communication_medium="link"),
				lambda: import_supplier_catalog(items=[{"item_code": "GUEST-TEST", "item_name": "Guest Test"}]),
				lambda: get_component_alternates(oem_part_number="TEST-OEM"),
				lambda: import_tesla_procedure(procedure_title="Guest Proc"),
				lambda: get_shop_floor_status(),
				lambda: update_job_status(repair_order=ro_name, action="start"),
				lambda: check_parts_stock(repair_order=ro_name),
				lambda: submit_procurement_orders(repair_order=ro_name, parts_data=[]),
				lambda: fetch_tesla_manual(url="https://service.tesla.com/docs/Model3/ServiceManual/test"),
				lambda: get_tech_overview(),
				lambda: get_unscheduled_services(repair_order=ro_name),
				lambda: receive_purchase_order(purchase_order="PO-NONE"),
				lambda: checkout_repair_order(repair_order=ro_name, confirm=True),
				lambda: toggle_service_completion(repair_order=ro_name, service_row_id=svc_row_name),
				lambda: add_supplement(repair_order=ro_name, service_title="Guest Supp"),
				lambda: duplicate_service_for_opposite_side(repair_order=ro_name, service_row_id=svc_row_name),
				lambda: get_deferred_services(repair_order=ro_name),
				lambda: add_deferred_services(repair_order=ro_name, deferred_items=[]),
				lambda: list_shop_action_items(),
				lambda: create_shop_action_item(title="Guest Escalation"),
				lambda: resolve_shop_action_item(action_item="SAI-NONE"),
				lambda: reschedule_appointment(schedule_entry="SE-NONE", scheduled_date=today()),
				lambda: update_appointment_status(schedule_entry="SE-NONE", status="Confirmed"),
				lambda: create_ro_from_appointment(schedule_entry="SE-NONE"),
				lambda: estimate_duration(repair_order=ro_name),
				lambda: decode_vehicle_vin(vin=VEHICLE_VIN),
				lambda: search_vehicles(query="Model 3"),
				lambda: get_vehicle_intelligence(vehicle=VEHICLE_VIN),
				lambda: link_vehicle_customer(vehicle=VEHICLE_VIN, customer=cust.name),
				lambda: list_inspection_templates(),
				lambda: get_inspection_template(),
				lambda: apply_inspection_template(repair_order=ro_name),
				lambda: get_check_in_inspection(repair_order=ro_name),
				lambda: record_check_in_inspection(repair_order=ro_name, odometer_in=12345),
				lambda: get_available_slots(date=today(), duration_minutes=60),
				lambda: allocate_schedule_entry(
					repair_order=ro_name,
					entry_type="Diagnostic",
					scheduled_date=add_days(today(), 1),
					scheduled_time="11:00:00",
					estimated_duration=60,
				),
				lambda: get_calendar_view_events(today(), add_days(today(), 1)),
				lambda: get_daily_technician_overview(today()),
				lambda: estimate_repair_order(ro_name),
				lambda: get_total_estimate([]),
				lambda: create_action_item_doc(title="Direct Guest Escalation"),
				lambda: ro.check_parts_stock_availability(),
				lambda: ro.generate_approval_token(),
				lambda: ro.authorize_jobs(approvals={svc_row_name: "Approved"}, signer_name="Direct Guest"),
				lambda: ro.toggle_service_completion(service_row_id=svc_row_name, is_completed=1),
			]
			for fn in guest_calls:
				with self.assertRaises(frappe.PermissionError):
					fn()

			portal_res = submit_customer_approval(
				token=approval_token,
				approvals={svc_row_name: "Approved"},
				signer_name="Guest Customer",
				signature_data="data:image/png;base64,iVBORw0KGgo=",
			)
			self.assertTrue(portal_res["success"])
			self.assertEqual(portal_res["approval_status"], "Approved")
		finally:
			frappe.set_user(prev_user)

		ro.reload()
		self.assertEqual(ro.approval_status, "Approved")

		type_confusion_calls = [
			lambda: create_quote(customer_name={"!=": ""}, vehicle_identifier=VEHICLE_VIN),
			lambda: get_deferred_services(vehicle={"!=": ""}),
			lambda: list_shop_action_items(status={"!=": ""}),
			lambda: search_vehicles(customer={"!=": ""}),
			lambda: decode_vehicle_vin(vin={"!=": ""}),
			lambda: get_total_estimate([{"item_code": {"!=": ""}, "qty": 1}]),
			lambda: get_service_package_details(service_package={"!=": ""}),
			lambda: ro.add_service_package(service_package={"!=": ""}),
		]
		for fn in type_confusion_calls:
			with self.assertRaises(frappe.ValidationError):
				fn()
