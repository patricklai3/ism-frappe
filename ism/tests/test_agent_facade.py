# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

from ism.api.agent import (
	_resolve_bay_for_booking,
	add_deferred_services,
	add_supplement,
	amend_quote,
	apply_inspection_template,
	book_appointment,
	check_parts_stock,
	checkout_repair_order,
	create_purchase_invoice,
	create_purchase_payment,
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
	intake_and_find_slots,
	link_vehicle_customer,
	list_inspection_templates,
	list_shop_action_items,
	receive_purchase_order,
	record_check_in_inspection,
	record_ro_deposit,
	reschedule_appointment,
	resolve_shop_action_item,
	search_vehicles,
	send_quote_for_approval,
	submit_procurement_orders,
	toggle_service_completion,
	update_appointment_status,
	update_job_status,
)
from ism.api.scheduling import resolve_service_bay_identifier
from ism.ism.doctype.repair_order.repair_order import _get_default_labor_rate
from ism.scheduling.time_utils import invalidate_shop_settings_cache
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestAgentFacade(ISMTestCase):
	"""Comprehensive test suite for the Curated Agent Facade API layer."""

	def setUp(self):
		super().setUp()
		self.manifest = self.get_manifest()
		self.customer = self.manifest.resolve("customers.retail_1")
		self.vehicle_vin = self.manifest.resolve("vehicles.veh_model3_liam")
		frappe.db.set_single_value("ISM Settings", "holiday_list", None)
		invalidate_shop_settings_cache()

	def tearDown(self):
		try:
			super().tearDown()
		finally:
			invalidate_shop_settings_cache()

	def _track_quote_ro(self, res):
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)
		ro_meta = frappe.db.get_value("Repair Order", ro_name, ["customer", "vehicle"], as_dict=True)
		if ro_meta:
			if ro_meta.vehicle and ro_meta.vehicle != self.vehicle_vin:
				self.track("Repair Vehicle", ro_meta.vehicle)
			if ro_meta.customer and ro_meta.customer != self.customer:
				self.track("Customer", ro_meta.customer)
		return ro_name

	def test_create_quote_basic_flow(self):
		"""Verify create_quote creates a Draft Estimate Repair Order with exact 13% tax math and summary card."""
		res = create_quote(
			customer_name=f"{TEST_PREFIX}Agent Cust 1",
			vehicle_identifier="5YJ3E1EBXNF999001",
			vehicle_color="Pearl White",
			license_plate="AGY 001",
			odometer_in=41200,
			services=[
				{"service_title": "Front Brake Pad Replacement", "labor_hours": 1.5, "labor_rate": 140.0},
				{"service_title": "Cabin Air Filter Service", "labor_hours": 0.5, "labor_rate": 140.0},
			],
			notes="Customer reports squeaking noise on light braking.",
		)

		self.assertTrue(res["success"])
		self.assertIn("Pearl White", res["data"].get("vehicle_display", ""))
		self.assertIn("(AGY 001)", res["data"].get("vehicle_display", ""))
		ro_name = self._track_quote_ro(res)

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
			customer_name=f"{TEST_PREFIX}Agent Cust 1b",
			vehicle_identifier="5YJ3E1EBXNF999009",
			vehicle_color="Midnight Silver",
			license_plate="AGY 009",
			odometer_in=41300,
			services=[{"service_title": "Brake Inspection", "labor_hours": 1.0, "labor_rate": 140.0}],
			include_summary=True,
		)
		self._track_quote_ro(res_summary)
		self.assertIn("summary_card", res_summary)
		self.assertIn("Brake Inspection", res_summary["summary_card"])

	def test_create_quote_default_inspection_fallback(self):
		"""Verify create_quote provides a default inspection service when services list is empty."""
		res = create_quote(
			customer_name=f"{TEST_PREFIX}Agent Cust 2",
			vehicle_identifier="5YJ3E1EBXNF999002",
			vehicle_color="Deep Blue",
			license_plate="AGY 002",
			odometer_in=41400,
			services=[],
			notes="General check-in inspection",
		)
		self.assertTrue(res["success"])
		ro_name = self._track_quote_ro(res)

		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(len(ro.services), 1)
		self.assertIn("Inspection", ro.services[0].service_title)

	def test_amend_quote_safely_modifies_lines(self):
		"""Verify amend_quote can add and remove service rows on an unapproved Repair Order."""
		res = create_quote(
			customer_name=f"{TEST_PREFIX}Agent Cust 3",
			vehicle_identifier="5YJ3E1EBXNF999003",
			vehicle_color="Solid Black",
			license_plate="AGY 003",
			odometer_in=41500,
			services=[
				{"service_title": "Initial Service A", "labor_hours": 1.0, "labor_rate": 100.0},
				{"service_title": "Initial Service B", "labor_hours": 2.0, "labor_rate": 100.0},
			],
		)
		ro_name = self._track_quote_ro(res)

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
			customer_name=f"{TEST_PREFIX}Agent Cust 4",
			vehicle_identifier="5YJ3E1EBXNF999004",
			vehicle_color="Red Multi-Coat",
			license_plate="AGY 004",
			odometer_in=41600,
			services=[{"service_title": "Service A", "labor_hours": 1.0}],
		)
		ro_name = self._track_quote_ro(res)

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
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=41700,
			services=[{"service_title": "Service Book", "labor_hours": 1.0}],
		)
		ro_name = res["data"]["name"]
		self.track("Repair Order", ro_name)

		with patch("frappe.publish_realtime") as mock_realtime:
			book_res = book_appointment(
				repair_order=ro_name,
				scheduled_date=today(),
				start_time="09:00:00",
				service_bay=self.manifest.resolve("bays.main_lift"),
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
		no_email_cust = self.create_test_customer(f"{TEST_PREFIX}NoEmailCust")
		no_email_veh = self.create_test_vehicle(customer=no_email_cust.name)
		res = create_quote(
			customer_name=no_email_cust.name,
			vehicle_identifier=no_email_veh.name,
			odometer_in=41800,
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
		res = import_supplier_catalog(raw_text=csv_data, supplier=f"{TEST_PREFIX}Supplier", include_summary=True)
		self.assertTrue(res["success"])
		self.track("Supplier", f"{TEST_PREFIX}Supplier")
		for item in res["data"].get("created", []):
			item_code = item.get("item_code") if isinstance(item, dict) else item
			if item_code:
				self.track("Item", item_code)
		self.assertEqual(res["data"]["total_created"], 2)
		self.assertEqual(res["data"]["supplier"], f"{TEST_PREFIX}Supplier")
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
		for row in pkg.items:
			if row.item_code:
				self.track("Item", row.item_code)
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
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=10000,
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
			customer_name=f"{TEST_PREFIX}Plate Cust",
			vehicle_identifier="ABCD-789",
			vehicle_color="Pearl White",
			customer_phone="416-555-0199",
			customer_email="platecust@example.com",
			odometer_in=41900,
			services=[{"service_title": "Wheel Torque Check", "labor_hours": 1.0}],
		)
		self.assertTrue(res["success"])
		ro_name = self._track_quote_ro(res)

		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(ro.services[0].labor_rate, _get_default_labor_rate())
		veh = frappe.get_doc("Repair Vehicle", ro.vehicle)
		self.assertEqual(veh.license_plate, "ABCD-789")
		self.assertTrue(veh.vin.startswith("PLATE"))
		cust = frappe.get_doc("Customer", ro.customer)
		self.assertEqual(cust.mobile_no, "416-555-0199")
		self.assertEqual(cust.email_id, "platecust@example.com")

	def test_extended_facade_endpoints(self):
		"""Verify check_parts_stock, submit_procurement_orders (submit default + draft override), tech overview, and unscheduled services."""
		res = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=42000,
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
			self.assertTrue(proc_res["data"]["submitted"])
			self.assertEqual(proc_res["data"]["purchase_orders"], ["PO-TEST-1"])
			self.assertTrue(mock_po.call_args.kwargs["submit"])

		with patch("ism.api.agent._submit_procurement_orders_impl", return_value={"purchase_orders": ["PO-DRAFT-1"], "rfqs": []}) as mock_po_draft:
			draft_res = submit_procurement_orders(
				repair_order=ro_name,
				parts_data=[{"item_code": "OEM-123", "qty": 1, "supplier": "Tesla"}],
				submit=False,
			)
			self.assertFalse(draft_res["data"]["submitted"])
			self.assertFalse(mock_po_draft.call_args.kwargs["submit"])

	def test_updated_capabilities_workflow(self):
		"""End-to-end smoke test covering Chunk 1 compact envelopes, wrappers, and hold isolation."""
		# 1. Compact intake with license plate, customer contact, and server default labor rate
		quote_res = create_quote(
			customer_name=f"{TEST_PREFIX}Workflow Cust",
			vehicle_identifier="WORK-123",
			vehicle_color="Midnight Silver",
			customer_phone="416-555-0142",
			customer_email="workflow@example.com",
			odometer_in=42100,
			services=[{"service_title": "Brake Inspection", "labor_hours": 1.0}],
		)
		self.assertTrue(quote_res["success"])
		self.assertNotIn("summary_card", quote_res)
		self.assertNotIn("summary_markdown", quote_res)
		self.assertNotIn("parts_html", quote_res["data"]["services"][0])
		self.assertIn("/app/repair-order/", quote_res["urls"]["desk"])
		self.assertIn("/printview?", quote_res["urls"]["print"])

		ro_name = self._track_quote_ro(quote_res)
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

		# 4. Approve RO, place on manual parts hold, and verify default-submit procurement
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
			self.assertTrue(proc_res["data"]["submitted"])
			self.assertTrue(mock_po.call_args.kwargs["submit"])

		# 5. Opt-in summary card on shop floor status
		floor_res = get_shop_floor_status(date=today(), include_summary=True)
		self.assertIn("summary_card", floor_res)
		self.assertIn("Shop Floor Status", floor_res["summary_card"])

	def test_accelerations_and_live_bug_fixes(self):
		"""Verify Reman normalization, deficit calculation, slot IDs, PO receiving, and checkout."""
		cat = import_supplier_catalog(
			raw_text="ACC-CAL-01 | Rear Reman Caliper | OEM | Reman | 320.00 | 210.00 | 0 | A | 1188611-00-R",
			supplier=f"{TEST_PREFIX}Parts Co",
			default_source="OEM",
			default_condition="Reman",
		)
		self.track("Supplier", f"{TEST_PREFIX}Parts Co")
		created_item = cat["data"]["created"][0]
		self.track("Item", created_item["item_code"])
		self.assertEqual(created_item["part_condition"], "Reconditioned")
		self.assertEqual(created_item["item_code"], "OEM-ACC-CAL-01-RCN")

		q = create_quote(
			customer_name=f"{TEST_PREFIX}Fast Flow Cust",
			vehicle_identifier="FAST-888",
			vehicle_color="Deep Blue",
			services=[{
				"service_title": "Caliper Replace",
				"labor_hours": 1.0,
				"parts": [{"item_code": "ACC-CAL-01", "qty": 1, "rate": 320.0}],
			}],
			odometer_in=12000,
		)
		ro_name = self._track_quote_ro(q)
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
		self.assertTrue(slots["data"]["slots"])
		self.assertIn("available_bay_ids", slots["data"]["slots"][0])

		auth = send_quote_for_approval(repair_order=ro_name, communication_medium="link")
		self.assertGreaterEqual(len(auth["data"]["services"]), 1)

		ro = frappe.get_doc("Repair Order", ro_name)
		ro.approval_status = "Approved"
		ro.save(ignore_permissions=True)

		book_res = book_appointment(
			repair_order=ro_name,
			scheduled_date=today(),
			start_time="10:00:00",
			service_bay=self.manifest.resolve("bays.main_lift"),
		)
		se_name = book_res["data"]["name"]
		self.track("Schedule Entry", se_name)

		po_res = submit_procurement_orders(
			repair_order=ro_name,
			parts_data=[{"item_code": "OEM-ACC-CAL-01-RCN", "qty": 1, "supplier": f"{TEST_PREFIX}Parts Co"}],
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
			checkout_repair_order(
				repair_order=ro_name,
				payment_method="Debit / Interac",
				odometer_out=12050,
				confirm=False,
			)

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
		veh_doc = frappe.get_doc("Repair Vehicle", self.vehicle_vin)
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
		def_res = get_deferred_services(vehicle=self.vehicle_vin)
		self.assertTrue(def_res["success"])
		self.assertGreaterEqual(def_res["data"]["deferred_count"], 1)

		q_res = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=42200,
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
			title=f"{TEST_PREFIX} Rust-seized rear knuckle",
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
			"item_code": f"{TEST_PREFIX}LINK-PART-01",
			"item_name": "Lateral Compliance Link",
			"item_group": "Products",
			"is_stock_item": 0,
			"standard_rate": 190.0,
		}).insert(ignore_permissions=True)
		self.track("Item", part.name)

		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": f"{TEST_PREFIX}Lateral Link LH (Remove and Replace)",
			"correction_code": "31-03-45-02",
			"is_symmetrical": 1,
			"standard_labor_hours": 0.9,
			"hourly_rate": 135.0,
			"items": [{"item_code": part.name, "qty": 1, "rate": 190.0, "side": "LH"}],
		}).insert(ignore_permissions=True)
		self.track("Service Package", pkg.name)

		# 1. Quote by package title expands FRT hours, correction code, position, and parts; explicit override wins
		q = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=42300,
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
			service_bay=self.manifest.resolve("bays.main_lift"),
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
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=42400,
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
		self.assertTrue(slots_compact["data"]["slots"])
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
				service_bay=self.manifest.resolve("bays.main_lift"),
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
			"customer": self.customer,
			"repair_vehicle": self.vehicle_vin,
			"scheduled_date": target_day,
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"service_bay": self.manifest.resolve("bays.alignment"),
			"notes": "Walk-in vibration check",
			"status": "Scheduled",
		}).insert(ignore_permissions=True)
		self.track("Schedule Entry", pre_se.name)

		created_ro = create_ro_from_appointment(schedule_entry=pre_se.name, odometer_in=42450)
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
		available_bays = midmorning["data"]["slots"][0]["available_bay_ids"]
		self.assertEqual(
			available_bays,
			[
				self.manifest.resolve("bays.main_lift"),
				self.manifest.resolve("bays.tire_quick"),
				self.manifest.resolve("bays.alignment"),
				self.manifest.resolve("bays.hv_diagnostics"),
			],
		)
		self.assertNotIn(self.manifest.resolve("bays.offline"), available_bays)

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
			service_bay=self.manifest.resolve("bays.main_lift"),
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
				customer_name=self.customer,
				vehicle_identifier=self.vehicle_vin,
				odometer_in=42500,
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
			"package_name": f"{TEST_PREFIX}Front Control Arm Package",
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
			supplier=f"{TEST_PREFIX}Structured Supplier",
			service_package=pkg.name,
			include_summary=True,
		)
		self.assertTrue(res["success"])
		self.track("Supplier", f"{TEST_PREFIX}Structured Supplier")
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
		model_s_customer = self.manifest.resolve("customers.retail_3")
		model_s_vin = self.manifest.resolve("vehicles.veh_models_owen")
		q = create_quote(
			customer_name=model_s_customer,
			vehicle_identifier=model_s_vin,
			odometer_in=42600,
			services=[{"service_title": "Initial Inspection", "labor_hours": 0.5}],
		)
		ro_name = q["data"]["name"]
		self.track("Repair Order", ro_name)

		with patch("ism.api.agent.fetch_and_parse_tesla_procedure", return_value={"title": "Mock Proc", "compatibility": {"is_compatible": True}}) as mock_fetch:
			manual_res = fetch_tesla_manual(
				"https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-TEST.html",
				repair_order=ro_name,
				vehicle=model_s_vin,
				include_summary=True,
			)
			self.assertTrue(manual_res["success"])
			mock_fetch.assert_called_once_with(
				"https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-TEST.html",
				repair_order=ro_name,
				vehicle=model_s_vin,
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

		co_owner = self.create_test_customer(f"{TEST_PREFIX}CoOwner")
		veh = self.create_test_vehicle(vin="5YJ3E1EBXNF888123", customer=self.customer, license_plate="FAC-888")

		linked = link_vehicle_customer(
			vehicle=veh.name,
			customer=co_owner.name,
			is_primary=False,
			relationship="Fleet Driver",
			include_summary=True,
		)
		self.assertTrue(linked["success"])
		self.assertEqual(linked["data"]["primary_customer"], self.customer)
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
		cust = self.create_test_customer(f"{TEST_PREFIX}InspectCust", mobile_no="416-555-0188")
		q = create_quote(
			customer_name=cust.name,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=42700,
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
			"template_name": f"{TEST_PREFIX}Facade Walkaround",
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

		cust = self.create_test_customer(f"{TEST_PREFIX}SecCust", mobile_no="416-555-0199")
		q = create_quote(
			customer_name=cust.name,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=42800,
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
				lambda: create_quote(customer_name=cust.name, vehicle_identifier=self.vehicle_vin),
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
				lambda: decode_vehicle_vin(vin=self.vehicle_vin),
				lambda: search_vehicles(query="Model 3"),
				lambda: get_vehicle_intelligence(vehicle=self.vehicle_vin),
				lambda: link_vehicle_customer(vehicle=self.vehicle_vin, customer=cust.name),
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
			lambda: create_quote(customer_name={"!=": ""}, vehicle_identifier=self.vehicle_vin),
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

	def test_resource_resolution_relative_time_and_conflict_feedback(self):
		"""Verify display-name bay/tech resolution, strict ISO dates/times, and structured conflict feedback."""
		bay_1 = self.manifest.resolve("bays.main_lift")
		bay_2 = self.manifest.resolve("bays.alignment")
		tomorrow_date = add_days(today(), 1)

		tech_id = self.manifest.resolve("techs.marcus")
		tech_name = frappe.db.get_value("Employee", tech_id, "employee_name")
		self.assertTrue(tech_id and tech_name)

		with self.assertRaises(frappe.ValidationError):
			find_available_slots(start_date="tomorrow", days_ahead=1, duration_minutes=60)
		with self.assertRaises(frappe.ValidationError):
			book_appointment(
				scheduled_date=tomorrow_date,
				start_time="9am",
				service_bay=bay_1,
				provisional_customer_name=f"{TEST_PREFIX}InvalidTime",
			)

		slots_res = find_available_slots(
			start_date=tomorrow_date,
			days_ahead=1,
			duration_minutes=60,
			service_bay=bay_1,
			technician=tech_name,
			min_start_time="09:00:00",
		)
		self.assertTrue(slots_res["success"])
		self.assertEqual(slots_res["data"]["service_bay"], bay_1)
		self.assertEqual(slots_res["data"]["technician"], tech_id)
		self.assertTrue(slots_res["data"]["slots"])
		first_slot = slots_res["data"]["slots"][0]
		self.assertEqual(first_slot["matched_technician"], tech_id)
		self.assertIn(tech_id, first_slot["available_technician_ids"])

		ro_res = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=42900,
			services=[{"service_title": "Brake Inspection", "labor_hours": 1.0}],
		)
		ro_name = ro_res["data"]["name"]
		self.track("Repair Order", ro_name)

		book1 = book_appointment(
			repair_order=ro_name,
			scheduled_date=tomorrow_date,
			start_time="09:00:00",
			service_bay=bay_1,
			technician=tech_name,
		)
		self.assertTrue(book1["success"])
		se1_name = book1["data"]["name"]
		self.track("Schedule Entry", se1_name)

		# Conflicting booking on same bay & time returns structured conflict feedback instead of raising
		book_conflict = book_appointment(
			scheduled_date=tomorrow_date,
			start_time="09:00:00",
			service_bay=bay_1,
			provisional_customer_name=f"{TEST_PREFIX}WalkIn",
		)
		self.assertFalse(book_conflict["success"])
		self.assertEqual(book_conflict["data"]["status"], "conflict")
		self.assertEqual(book_conflict["data"]["conflict_type"], "bay_conflict")
		self.assertEqual(book_conflict["data"]["resources"]["service_bay"], bay_1)
		self.assertIn("conflicting_entry", book_conflict["urls"])
		self.assertTrue(book_conflict["data"]["alternative_slots"])

		# Book a second entry on bay_2 at 14:30:00, then reschedule into bay_1 at 09:00:00 to trigger conflict
		book2 = book_appointment(
			scheduled_date=tomorrow_date,
			start_time="14:30:00",
			service_bay=bay_2,
			provisional_customer_name=f"{TEST_PREFIX}WalkIn2",
		)
		self.assertTrue(book2["success"])
		se2_name = book2["data"]["name"]
		self.track("Schedule Entry", se2_name)

		resched_conflict = reschedule_appointment(
			schedule_entry=se2_name,
			scheduled_date=tomorrow_date,
			start_time="09:00:00",
			service_bay=bay_1,
		)
		self.assertFalse(resched_conflict["success"])
		self.assertEqual(resched_conflict["data"]["status"], "conflict")
		self.assertEqual(resched_conflict["data"]["conflict_type"], "bay_conflict")
		self.assertTrue(resched_conflict["data"]["alternative_slots"])

	def test_intake_and_find_slots_composite(self):
		"""Verify intake_and_find_slots creates a Draft Estimate RO and returns candidate slots in one call."""
		res = intake_and_find_slots(
			customer_name=f"{TEST_PREFIX}Composite Cust",
			vehicle_identifier="5YJ3E1EBXNF999099",
			vehicle_color="Pearl White",
			license_plate="CMP 099",
			odometer_in=43000,
			services=[{"service_title": "Cabin Air Filter Replacement", "labor_hours": 1.0}],
			notes="Musty AC smell",
			start_date=add_days(today(), 1),
			days_ahead=2,
			max_slots=5,
			include_summary=True,
		)
		self.assertTrue(res["success"])
		self._track_quote_ro(res)
		self.assertEqual(res["data"]["repair_order"]["status"], "Draft Estimate")
		self.assertGreaterEqual(res["data"]["duration_minutes"], 60)
		self.assertIsInstance(res["data"]["slots"], list)
		self.assertIn("calendar", res["urls"])
		self.assertIn("summary_card", res)

	def test_service_row_aliases_and_defaults(self):
		"""Verify _build_service_row normalizes intuitive title/hours aliases in create_quote and amend_quote."""
		res = create_quote(
			customer_name=f"{TEST_PREFIX}Alias Cust",
			vehicle_identifier="5YJ3E1EBXNF999077",
			vehicle_color="Solid Black",
			license_plate="ALS 077",
			odometer_in=43100,
			services=[
				{"service_name": "Cabin Air Filter Replacement", "hours": 0.5},
				{"service": "Wheel Torque Check", "frt": 0.75},
				{"operation": "HV Check", "custom_frt": 1.25},
				{},
			],
		)
		self.assertTrue(res["success"])
		ro_name = self._track_quote_ro(res)

		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(len(ro.services), 4)
		self.assertEqual(ro.services[0].service_title, "Cabin Air Filter Replacement")
		self.assertAlmostEqual(ro.services[0].labor_hours, 0.5, places=2)
		self.assertEqual(ro.services[1].service_title, "Wheel Torque Check")
		self.assertAlmostEqual(ro.services[1].labor_hours, 0.75, places=2)
		self.assertEqual(ro.services[2].service_title, "HV Check")
		self.assertAlmostEqual(ro.services[2].labor_hours, 1.25, places=2)
		self.assertEqual(ro.services[3].service_title, "Service Operation")
		self.assertAlmostEqual(ro.services[3].labor_hours, 1.0, places=2)

		amend_res = amend_quote(
			repair_order=ro_name,
			remove_service_indices=[3],
			add_services=[{"service_name": "Wiper Blade Replacement", "hours": 0.25}],
		)
		self.assertTrue(amend_res["success"])
		ro.reload()
		self.assertEqual(len(ro.services), 4)
		self.assertEqual(ro.services[3].service_title, "Wiper Blade Replacement")
		self.assertAlmostEqual(ro.services[3].labor_hours, 0.25, places=2)

	def test_intake_and_find_slots_preserves_filters_and_tech_match(self):
		"""Verify intake_and_find_slots preserves service_bay/technician and exposes matched_technician on slots."""
		bay_1 = self.manifest.resolve("bays.main_lift")
		tech_id = self.manifest.resolve("techs.marcus")
		tech_name = frappe.db.get_value("Employee", tech_id, "employee_name")
		self.assertTrue(tech_id and tech_name)

		res = intake_and_find_slots(
			customer_name=f"{TEST_PREFIX}Composite Filter Cust",
			vehicle_identifier="5YJ3E1EBXNF999078",
			vehicle_color="Stealth Grey",
			license_plate="FLT 078",
			odometer_in=43200,
			services=[{"service_name": "Brake Fluid Flush", "hours": 1.0}],
			start_date=add_days(today(), 1),
			days_ahead=1,
			service_bay=bay_1,
			technician=tech_name,
			max_slots=3,
		)
		self.assertTrue(res["success"])
		self._track_quote_ro(res)
		self.assertEqual(res["data"]["service_bay"], bay_1)
		self.assertEqual(res["data"]["technician"], tech_id)
		self.assertTrue(res["data"]["slots"])
		for slot in res["data"]["slots"]:
			self.assertEqual(slot["matched_technician"], tech_id)
			self.assertIn(tech_id, slot["available_technician_ids"])

	def test_ambiguous_service_bay_error_includes_equipment_tags(self):
		"""Verify resolve_service_bay_identifier lists matching active bays with their equipment tags."""
		bay_1 = self.manifest.resolve("bays.main_lift")
		bay_2 = self.manifest.resolve("bays.alignment")
		bay_offline = self.manifest.resolve("bays.offline")
		with self.assertRaises(frappe.ValidationError) as ctx:
			resolve_service_bay_identifier("Bay")
		msg = str(ctx.exception)
		self.assertIn("Ambiguous Service Bay", msg)
		self.assertIn(f"{bay_1} [Lift]", msg)
		self.assertIn(f"{bay_2} [ADAS Calibration Target, Alignment Rack, Lift]", msg)
		self.assertNotIn(bay_offline, msg)

	def test_book_appointment_ambiguous_bay_annotates_availability(self):
		"""Verify book_appointment annotates Ambiguous Service Bay options with [Available]/[Busy]."""
		bay_1 = self.manifest.resolve("bays.main_lift")
		bay_2 = self.manifest.resolve("bays.alignment")
		tomorrow_date = add_days(today(), 1)
		booked = book_appointment(
			scheduled_date=tomorrow_date,
			start_time="10:00:00",
			service_bay=bay_1,
			entry_type="Diagnostic",
			estimated_duration=60,
			provisional_customer_name=f"{TEST_PREFIX}WalkIn Busy",
		)
		self.assertTrue(booked["success"])
		self.track("Schedule Entry", booked["data"]["name"])

		with self.assertRaises(frappe.ValidationError) as ctx:
			book_appointment(
				scheduled_date=tomorrow_date,
				start_time="10:00:00",
				service_bay="Bay",
				entry_type="Diagnostic",
				provisional_customer_name=f"{TEST_PREFIX}WalkIn Ambiguous",
			)
		msg = str(ctx.exception)
		self.assertIn("Ambiguous Service Bay", msg)
		self.assertIn(f"{bay_1} [Lift] [Busy]", msg)
		self.assertIn(f"{bay_2} [ADAS Calibration Target, Alignment Rack, Lift] [Available]", msg)

		# Missing or unparseable date/time falls back cleanly to standard Ambiguous Service Bay error
		with self.assertRaises(frappe.ValidationError) as empty_ctx:
			book_appointment(
				scheduled_date="",
				start_time="",
				service_bay="Bay",
				entry_type="Diagnostic",
			)
		empty_msg = str(empty_ctx.exception)
		self.assertIn("Ambiguous Service Bay", empty_msg)
		self.assertIn(f"{bay_1} [Lift]", empty_msg)

		with self.assertRaises(frappe.ValidationError) as fallback_ctx:
			_resolve_bay_for_booking(
				"Bay",
				scheduled_date="not-a-date",
				start_time="not-a-time",
				estimated_duration=None,
				repair_order=None,
			)
		fallback_msg = str(fallback_ctx.exception)
		self.assertIn("Ambiguous Service Bay", fallback_msg)
		self.assertIn(f"{bay_1} [Lift]", fallback_msg)

	def test_required_field_enforcement_across_workflows(self):
		"""Verify odometer_in, scope, odometer_out, and payment_method cannot be silently defaulted."""
		with self.assertRaises(frappe.MandatoryError):
			create_quote(
				customer_name=self.customer,
				vehicle_identifier=self.vehicle_vin,
				services=[{"service_title": "Suspension Diagnostic", "labor_hours": 1.0}],
			)

		with self.assertRaises(frappe.MandatoryError):
			create_quote(
				customer_name=self.customer,
				vehicle_identifier=self.vehicle_vin,
				odometer_in=45000,
			)

		with self.assertRaises(frappe.ValidationError):
			create_quote(
				customer_name=self.customer,
				vehicle_identifier=self.vehicle_vin,
				odometer_in=-5,
				notes="Negative odometer should fail",
			)

		q = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=45000,
			services=[{"service_title": "Lateral Link Diagnostic", "labor_hours": 1.0}],
		)
		ro_name = q["data"]["name"]
		self.track("Repair Order", ro_name)
		self.assertEqual(frappe.db.get_value("Repair Order", ro_name, "odometer_in"), 45000)

		ro = frappe.get_doc("Repair Order", ro_name)
		ro.approval_status = "Approved"
		ro.save(ignore_permissions=True)

		with self.assertRaises(frappe.MandatoryError):
			update_job_status(repair_order=ro_name, action="complete")

		with self.assertRaises(frappe.ValidationError):
			update_job_status(repair_order=ro_name, action="complete", odometer_out=44999)

		update_job_status(repair_order=ro_name, action="complete", odometer_out=45012)

		with self.assertRaises(frappe.MandatoryError):
			checkout_repair_order(repair_order=ro_name, odometer_out=45012, confirm=True)

		pre_se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"customer": self.customer,
			"repair_vehicle": self.vehicle_vin,
			"scheduled_date": add_days(today(), 3),
			"scheduled_time": "10:00:00",
			"estimated_duration": 60,
			"service_bay": self.manifest.resolve("bays.main_lift"),
			"notes": "Missing odometer intake check",
			"status": "Scheduled",
		}).insert(ignore_permissions=True)
		self.track("Schedule Entry", pre_se.name)

		with self.assertRaises(frappe.MandatoryError):
			create_ro_from_appointment(schedule_entry=pre_se.name)

	def test_standalone_procurement_advance_payment_and_invoice_scenarios(self):
		"""Verify standalone POs, auto-submit, PO advance payments, Scenario A (PI update_stock=0 -> PR), and Scenario B (PI update_stock=1)."""
		from frappe.utils import flt
		from ism.api.parts import quick_create_part

		sup = f"{TEST_PREFIX}InvSup_{frappe.generate_hash(length=5)}"
		p_a = quick_create_part(
			part_number=f"{TEST_PREFIX}INV_A",
			part_name="Lower Control Arm Frontward LH",
			unit_rate=220.0,
			valuation_rate=150.0,
		)
		p_b = quick_create_part(
			part_number=f"{TEST_PREFIX}INV_B",
			part_name="Lower Control Arm Frontward RH",
			unit_rate=240.0,
			valuation_rate=160.0,
		)
		self.track("Item", p_a["item_code"])
		self.track("Item", p_b["item_code"])

		# 1. Standalone PO with prepaid payment in a single dispatch (no Repair Order required)
		prepaid_res = submit_procurement_orders(
			parts_data=[{"item_code": p_a["item_code"], "qty": 1, "rate": 150.0, "supplier": sup}],
			payment_method="Debit / Interac",
		)
		self.assertTrue(prepaid_res["success"])
		self.assertIsNone(prepaid_res["data"]["repair_order"])
		self.assertTrue(prepaid_res["data"]["submitted"])
		po_a_name = prepaid_res["data"]["purchase_orders"][0]
		pe_a_name = prepaid_res["data"]["payment_entries"][0]
		self.track("Purchase Order", po_a_name)
		self.track("Payment Entry", pe_a_name)
		self.track("Supplier", sup)
		self.assertIn("/app/purchase-order/", prepaid_res["urls"]["purchase_order"])
		self.assertIn("/app/payment-entry/", prepaid_res["urls"]["payment_entry"])

		po_a = frappe.get_doc("Purchase Order", po_a_name)
		self.assertEqual(po_a.docstatus, 1)
		self.assertIsNone(getattr(po_a, "repair_order", None))
		self.assertAlmostEqual(flt(po_a.grand_total), 169.50, places=2)
		self.assertAlmostEqual(flt(po_a.advance_paid), 169.50, places=2)

		pe_a = frappe.get_doc("Payment Entry", pe_a_name)
		self.assertEqual(pe_a.docstatus, 1)
		self.assertEqual(pe_a.payment_type, "Pay")
		self.assertEqual(pe_a.party_type, "Supplier")

		# 2. Scenario A: Invoice arrives before part (update_stock=0) -> advance attached -> PR later updates stock
		pi_a_res = create_purchase_invoice(
			purchase_order=po_a_name,
			update_stock=False,
			bill_no="SUP-INV-A1",
		)
		self.assertTrue(pi_a_res["success"])
		pi_a_name = pi_a_res["data"]["purchase_invoice"]
		self.track("Purchase Invoice", pi_a_name)
		self.assertEqual(pi_a_res["data"]["docstatus"], 1)
		self.assertEqual(pi_a_res["data"]["update_stock"], 0)
		self.assertAlmostEqual(flt(pi_a_res["data"]["total_advance"]), 169.50, places=2)
		self.assertAlmostEqual(flt(pi_a_res["data"]["outstanding_amount"]), 0.0, places=2)
		self.assertEqual(len(pi_a_res["data"]["advances"]), 1)
		self.assertEqual(pi_a_res["data"]["advances"][0]["reference_name"], pe_a_name)

		wh = po_a.items[0].warehouse
		stock_before_pr = flt(
			frappe.db.get_value("Bin", {"item_code": p_a["item_code"], "warehouse": wh}, "actual_qty") or 0.0
		)

		pr_a_res = receive_purchase_order(purchase_order=po_a_name)
		self.assertTrue(pr_a_res["success"])
		pr_a_name = pr_a_res["data"]["purchase_receipt"]
		self.track("Purchase Receipt", pr_a_name)
		self.assertEqual(pr_a_res["data"]["purchase_invoice"], pi_a_name)
		self.assertEqual(pr_a_res["data"]["per_received"], 100.0)

		stock_after_pr = flt(
			frappe.db.get_value("Bin", {"item_code": p_a["item_code"], "warehouse": wh}, "actual_qty") or 0.0
		)
		self.assertAlmostEqual(stock_after_pr - stock_before_pr, 1.0, places=2)

		pi_a_doc = frappe.get_doc("Purchase Invoice", pi_a_name)
		pr_a_doc = frappe.get_doc("Purchase Receipt", pr_a_name)
		self.assertAlmostEqual(flt(pi_a_doc.per_received), 100.0, places=2)
		self.assertAlmostEqual(flt(pr_a_doc.per_billed), 100.0, places=2)

		# 3. Scenario B: Standalone PO -> separate Bank advance Payment Entry (fallback reference_no) -> PI with update_stock=1
		po_b_res = submit_procurement_orders(
			parts_data=[{"item_code": p_b["item_code"], "qty": 2, "rate": 100.0, "supplier": sup}],
		)
		po_b_name = po_b_res["data"]["purchase_orders"][0]
		self.track("Purchase Order", po_b_name)

		pe_b_res = create_purchase_payment(
			purchase_order=po_b_name,
			payment_method="Credit Card",
			paid_amount=100.0,
		)
		pe_b_name = pe_b_res["data"]["payment_entry"]
		self.track("Payment Entry", pe_b_name)
		self.assertEqual(pe_b_res["data"]["docstatus"], 1)
		self.assertAlmostEqual(flt(pe_b_res["data"]["paid_amount"]), 100.0, places=2)

		stock_before_pi1 = flt(
			frappe.db.get_value("Bin", {"item_code": p_b["item_code"], "warehouse": wh}, "actual_qty") or 0.0
		)
		pi_b_res = create_purchase_invoice(
			purchase_order=po_b_name,
			update_stock=True,
			bill_no="SUP-INV-B1",
		)
		pi_b_name = pi_b_res["data"]["purchase_invoice"]
		self.track("Purchase Invoice", pi_b_name)
		self.assertEqual(pi_b_res["data"]["docstatus"], 1)
		self.assertEqual(pi_b_res["data"]["update_stock"], 1)
		self.assertAlmostEqual(flt(pi_b_res["data"]["grand_total"]), 226.0, places=2)
		self.assertAlmostEqual(flt(pi_b_res["data"]["total_advance"]), 100.0, places=2)
		self.assertAlmostEqual(flt(pi_b_res["data"]["outstanding_amount"]), 126.0, places=2)
		self.assertAlmostEqual(flt(pi_b_res["data"]["per_received"]), 100.0, places=2)
		self.assertAlmostEqual(flt(pi_b_res["data"]["per_billed"]), 100.0, places=2)

		stock_after_pi1 = flt(
			frappe.db.get_value("Bin", {"item_code": p_b["item_code"], "warehouse": wh}, "actual_qty") or 0.0
		)
		self.assertAlmostEqual(stock_after_pi1 - stock_before_pi1, 2.0, places=2)

		with self.assertRaises(frappe.ValidationError):
			receive_purchase_order(purchase_order=po_b_name)

		# Pay remaining balance directly against the Purchase Invoice
		pe_b2_res = create_purchase_payment(purchase_invoice=pi_b_name, payment_method="Cash")
		self.track("Payment Entry", pe_b2_res["data"]["payment_entry"])
		self.assertAlmostEqual(flt(pe_b2_res["data"]["paid_amount"]), 126.0, places=2)
		pi_b_doc = frappe.get_doc("Purchase Invoice", pi_b_name)
		self.assertAlmostEqual(flt(pi_b_doc.outstanding_amount), 0.0, places=2)

		# 4. Standalone Purchase Invoice without PO (update_stock=0 -> receive via Purchase Invoice)
		pi_c_res = create_purchase_invoice(
			supplier=sup,
			parts_data=[{"item_code": p_a["item_code"], "qty": 1, "rate": 140.0}],
			update_stock=False,
			bill_no="SUP-INV-C1",
		)
		pi_c_name = pi_c_res["data"]["purchase_invoice"]
		self.track("Purchase Invoice", pi_c_name)
		pr_c_res = receive_purchase_order(purchase_invoice=pi_c_name)
		self.track("Purchase Receipt", pr_c_res["data"]["purchase_receipt"])
		self.assertEqual(pr_c_res["data"]["purchase_invoice"], pi_c_name)
		self.assertEqual(pr_c_res["data"]["per_received"], 100.0)

	def test_shop_action_item_permissions_reporter_spoof_and_reference_checks(self):
		"""Verify Shop Action Item removes Role: All access, blocks reported_by spoofing, and checks reference read perms."""
		from ism.ism.doctype.shop_action_item.shop_action_item import (
			create_shop_action_item as create_action_item_doc,
		)

		no_role_email = "_test_ism_sai_norole@example.com"
		maint_email = "_test_ism_sai_maint@example.com"

		for email, roles in ((no_role_email, []), (maint_email, [{"role": "Maintenance User"}])):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
			frappe.get_doc({
				"doctype": "User",
				"email": email,
				"first_name": "SAI",
				"last_name": "Tester",
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": roles,
			}).insert(ignore_permissions=True)
			self.addCleanup(
				lambda u=email: frappe.db.exists("User", u)
				and frappe.delete_doc("User", u, force=True, ignore_permissions=True)
			)

		allowed_cust = self.create_test_customer(f"{TEST_PREFIX}SAI Allowed Cust")
		blocked_cust = self.create_test_customer(f"{TEST_PREFIX}SAI Blocked Cust")
		blocked_veh = self.create_test_vehicle(customer=blocked_cust.name)
		blocked_ro = self.create_test_repair_order(customer=blocked_cust.name, vehicle=blocked_veh.name)

		user_perm = frappe.get_doc({
			"doctype": "User Permission",
			"user": maint_email,
			"allow": "Customer",
			"for_value": allowed_cust.name,
			"apply_to_all_doctypes": 1,
		}).insert(ignore_permissions=True)
		self.addCleanup(
			lambda p=user_perm.name: frappe.db.exists("User Permission", p)
			and frappe.delete_doc("User Permission", p, force=True, ignore_permissions=True)
		)

		seed_res = create_shop_action_item(title=f"{TEST_PREFIX}Seed SAI Item")
		self.assertTrue(seed_res["success"])
		seed_name = seed_res["data"]["name"]
		self.track("Shop Action Item", seed_name)

		orig_user = frappe.session.user
		try:
			frappe.set_user(no_role_email)
			self.assertFalse(frappe.has_permission("Shop Action Item", "read"))
			for fn in (
				lambda: create_shop_action_item(title="No Role Escalation"),
				lambda: list_shop_action_items(),
				lambda: resolve_shop_action_item(action_item=seed_name, resolution_notes="Should fail"),
				lambda: frappe.get_doc("Shop Action Item", seed_name).check_permission("read"),
			):
				with self.assertRaises(frappe.PermissionError):
					fn()

			frappe.set_user(maint_email)
			created = create_shop_action_item(title=f"{TEST_PREFIX}Maint SAI Item")
			self.assertTrue(created["success"])
			sai_name = created["data"]["name"]
			self.track("Shop Action Item", sai_name)

			listed = list_shop_action_items(status="Open")
			self.assertTrue(any(row["name"] == sai_name for row in listed["data"]["items"]))

			resolved = resolve_shop_action_item(action_item=sai_name, resolution_notes="Resolved by tech")
			self.assertTrue(resolved["success"])
			self.assertEqual(resolved["data"]["status"], "Resolved")

			with self.assertRaises(frappe.PermissionError):
				create_action_item_doc(title=f"{TEST_PREFIX}Spoof Reporter", reported_by="Administrator")

			with self.assertRaises(frappe.PermissionError):
				create_shop_action_item(
					title=f"{TEST_PREFIX}Unauthorized Ref",
					reference_doctype="Repair Order",
					reference_name=blocked_ro.name,
				)

			with self.assertRaises(frappe.PermissionError):
				create_action_item_doc(
					title=f"{TEST_PREFIX}Unauthorized Ref Doc",
					reference_doctype="Repair Order",
					reference_name=blocked_ro.name,
				)
		finally:
			frappe.set_user(orig_user)

	def test_reschedule_appointment_clear_bay(self):
		"""Verify reschedule_appointment(..., clear_bay=True) clears the assigned service bay."""
		bay_1 = self.manifest.resolve("bays.main_lift")
		booked = book_appointment(
			scheduled_date=add_days(today(), 1),
			start_time="15:00:00",
			service_bay=bay_1,
			entry_type="Diagnostic",
			estimated_duration=60,
			provisional_customer_name=f"{TEST_PREFIX}Clear Bay WalkIn",
		)
		self.assertTrue(booked["success"])
		se_name = booked["data"]["name"]
		self.track("Schedule Entry", se_name)
		self.assertEqual(booked["data"]["service_bay"], bay_1)

		cleared = reschedule_appointment(schedule_entry=se_name, clear_bay=True)
		self.assertTrue(cleared["success"])
		self.assertFalse(cleared["data"].get("service_bay"))
		self.assertFalse(frappe.db.get_value("Schedule Entry", se_name, "service_bay"))

	def test_amend_quote_in_place_update_services_and_fees(self):
		"""Verify amend_quote supports in-place row updates, order fees/discounts, and row guardrails."""
		res = create_quote(
			customer_name=f"{TEST_PREFIX}Amend InPlace Cust",
			vehicle_identifier="5YJ3E1EBXNF999111",
			vehicle_color="Pearl White",
			license_plate="AMD 111",
			odometer_in=42000,
			services=[
				{"service_title": "Brake Fluid Flush", "labor_hours": 1.0, "labor_rate": 140.0},
				{"service_title": "Cabin Filter", "labor_hours": 0.5, "labor_rate": 140.0},
			],
		)
		ro_name = self._track_quote_ro(res)
		row_1_name = res["data"]["services"][0]["name"]
		row_2_name = res["data"]["services"][1]["name"]

		# Update row 1 by service_row_id and row 2 by 1-based idx, plus order-level fees/discount
		upd = amend_quote(
			repair_order=ro_name,
			update_services=[
				{
					"service_row_id": row_1_name,
					"job_title": "Brake Fluid Flush (DOT 4)",
					"labor_hours": 1.5,
					"labor_rate": 150.0,
					"discount_amount": 15.0,
					"notes": "Customer requested synthetic DOT 4",
				},
				{
					"idx": 2,
					"labor_hours": 0.75,
				},
			],
			discount_amount=20.0,
			shop_supplies_amount=12.5,
			environmental_fee_amount=4.0,
		)
		self.assertTrue(upd["success"])
		ro = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(ro.services[0].name, row_1_name)
		self.assertEqual(ro.services[0].service_title, "Brake Fluid Flush (DOT 4)")
		self.assertEqual(ro.services[0].labor_hours, 1.5)
		self.assertEqual(ro.services[0].labor_rate, 150.0)
		self.assertEqual(ro.services[1].name, row_2_name)
		self.assertEqual(ro.services[1].labor_hours, 0.75)

		# Guardrails: missing selector, ambiguous selector, out-of-range idx, negative discount
		with self.assertRaises(frappe.ValidationError):
			amend_quote(repair_order=ro_name, update_services=[{"labor_hours": 2.0}])
		with self.assertRaises(frappe.ValidationError):
			amend_quote(repair_order=ro_name, update_services=[{"service_row_id": row_1_name, "idx": 1, "labor_hours": 2.0}])
		with self.assertRaises(frappe.ValidationError):
			amend_quote(repair_order=ro_name, update_services=[{"idx": 99, "labor_hours": 2.0}])
		with self.assertRaises(frappe.ValidationError):
			amend_quote(repair_order=ro_name, discount_amount=-5.0)

		# Guardrail: reject in-place edit on row with approval_status == "Approved" or status == "Approved"
		ro_approved_row = frappe.get_doc("Repair Order", ro_name)
		ro_approved_row.services[0].approval_status = "Approved"
		with patch("ism.api.agent_quotes._get_ro_doc", return_value=ro_approved_row):
			with self.assertRaises(frappe.ValidationError):
				amend_quote(repair_order=ro_name, update_services=[{"service_row_id": row_1_name, "labor_hours": 3.0}])

		# Also test row.status == "Approved" on Partially Approved RO (row 2 unapproved can be updated)
		frappe.db.set_value("Repair Order Service", row_1_name, "status", "Approved")
		frappe.db.set_value("Repair Order", ro_name, "approval_status", "Partially Approved")
		with self.assertRaises(frappe.ValidationError):
			amend_quote(repair_order=ro_name, update_services=[{"service_row_id": row_1_name, "labor_hours": 3.0}])
		with self.assertRaises(frappe.ValidationError):
			amend_quote(repair_order=ro_name, remove_service_indices=[1])
		part_upd = amend_quote(
			repair_order=ro_name,
			update_services=[{"service_row_id": row_2_name, "labor_hours": 1.25}],
		)
		self.assertTrue(part_upd["success"])
		self.assertEqual(frappe.get_doc("Repair Order", ro_name).services[1].labor_hours, 1.25)

	def test_record_ro_deposit_facade_and_validation(self):
		"""Verify record_ro_deposit validates inputs, delegates to RepairOrder.record_deposit, and returns envelope."""
		res = create_quote(
			customer_name=f"{TEST_PREFIX}Deposit Cust",
			vehicle_identifier="5YJ3E1EBXNF999112",
			vehicle_color="Deep Blue",
			license_plate="DEP 112",
			odometer_in=43000,
			services=[{"service_title": "Battery Inspection", "labor_hours": 1.0, "labor_rate": 140.0}],
		)
		ro_name = self._track_quote_ro(res)

		with self.assertRaises(frappe.ValidationError):
			record_ro_deposit(repair_order=ro_name, amount=-10)
		with self.assertRaises(frappe.ValidationError):
			record_ro_deposit(repair_order=ro_name, amount=0)

		with patch(
			"ism.ism.doctype.repair_order.repair_order.RepairOrder.record_deposit",
			create=True,
			return_value={"payment_entry": "ACC-PAY-TEST-001", "deposit_amount": 100.0},
		) as mock_dep:
			dep_res = record_ro_deposit(
				repair_order=ro_name,
				amount=100.0,
				payment_method="Visa",
				reference_no="AUTH-123",
				include_summary=True,
			)
		mock_dep.assert_called_once_with(amount=100.0, payment_method="Visa", reference_no="AUTH-123")
		self.assertTrue(dep_res["success"])
		self.assertEqual(dep_res["data"]["repair_order"], ro_name)
		self.assertEqual(dep_res["data"]["amount"], 100.0)
		self.assertEqual(dep_res["data"]["deposit_amount"], 100.0)
		self.assertEqual(dep_res["data"]["payment_method"], "Visa")
		self.assertEqual(dep_res["data"]["reference_no"], "AUTH-123")
		self.assertEqual(dep_res["data"]["payment_entry"], "ACC-PAY-TEST-001")
		self.assertIn("summary_card", dep_res)

	def test_checkout_repair_order_split_payments_facade(self):
		"""Verify checkout_repair_order validates and forwards split-tender payments."""
		res = create_quote(
			customer_name=f"{TEST_PREFIX}Split Checkout Cust",
			vehicle_identifier="5YJ3E1EBXNF999113",
			vehicle_color="Midnight Silver",
			license_plate="SPL 113",
			odometer_in=44000,
			services=[{"service_title": "Tire Rotation", "labor_hours": 0.5, "labor_rate": 140.0}],
		)
		ro_name = self._track_quote_ro(res)

		with self.assertRaises(frappe.ValidationError):
			checkout_repair_order(repair_order=ro_name, payments=[], odometer_out=44005, confirm=True)
		with self.assertRaises(frappe.ValidationError):
			checkout_repair_order(
				repair_order=ro_name,
				payments=[{"payment_method": "Visa", "amount": -50}],
				odometer_out=44005,
				confirm=True,
			)

		tenders = [
			{"payment_method": "Visa", "amount": 50.0, "reference_no": "V-10"},
			{"payment_method": "Cash", "amount": 29.10},
		]
		with patch(
			"ism.ism.doctype.repair_order.repair_order.RepairOrder.process_payment_and_release",
			return_value={
				"payment_entry": "ACC-PAY-SPLIT-1",
				"payment_entries": ["ACC-PAY-SPLIT-1", "ACC-PAY-SPLIT-2"],
				"deposit_allocated": 25.0,
			},
		) as mock_rel:
			chk_res = checkout_repair_order(
				repair_order=ro_name,
				payments=json.dumps(tenders),
				odometer_out=44005,
				confirm=True,
			)
		self.assertTrue(chk_res["success"])
		self.assertEqual(chk_res["data"]["payment_entry"], "ACC-PAY-SPLIT-1")
		self.assertEqual(chk_res["data"]["payment_entries"], ["ACC-PAY-SPLIT-1", "ACC-PAY-SPLIT-2"])
		self.assertEqual(chk_res["data"]["deposit_allocated"], 25.0)
		mock_rel.assert_called_once()
		call_kwargs = mock_rel.call_args.kwargs
		self.assertEqual(call_kwargs["odometer_out"], 44005)
		self.assertEqual(
			call_kwargs["payments"],
			[
				{"payment_method": "Visa", "amount": 50.0, "reference_no": "V-10"},
				{"payment_method": "Cash", "amount": 29.1},
			],
		)

	def test_procurement_skips_item_write_when_user_lacks_item_write_permission(self):
		"""Verify submit_procurement_orders creates POs when user lacks Item write permission."""
		part_code = f"{TEST_PREFIX}NO_ITEM_WRITE_PART"
		if not frappe.db.exists("Item", part_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": part_code,
				"item_name": "No Item Write Test Part",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"valuation_rate": 45.0,
			}).insert(ignore_permissions=True)
			self.track(item)

		sup_name = f"{TEST_PREFIX}NoWriteSup"
		if not frappe.db.exists("Supplier", sup_name):
			sup = frappe.get_doc({
				"doctype": "Supplier",
				"supplier_name": sup_name,
				"supplier_group": frappe.db.get_value("Supplier Group", {}, "name") or "All Supplier Groups",
			}).insert(ignore_permissions=True)
			self.track(sup)

		user_email = f"{TEST_PREFIX.lower()}mgr_proc@example.com"
		if not frappe.db.exists("User", user_email):
			user = frappe.get_doc({
				"doctype": "User",
				"email": user_email,
				"first_name": "Proc",
				"last_name": "Manager",
				"enabled": 1,
				"send_welcome_email": 0,
				"roles": [
					{"role": "ISM Manager"},
					{"role": "Stock User"},
					{"role": "Purchase User"},
				],
			}).insert(ignore_permissions=True)
			self.track(user)

		ro_res = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=45100,
			services=[{"service_title": "Brake Pad Service", "labor_hours": 1.0, "labor_rate": 140.0}],
		)
		ro_name = self._track_quote_ro(ro_res)

		prev_user = frappe.session.user
		try:
			frappe.set_user(user_email)
			self.assertFalse(frappe.get_doc("Item", part_code).has_permission("write"))
			proc_res = submit_procurement_orders(
				repair_order=ro_name,
				parts_data=json.dumps([
					{"item_code": part_code, "qty": 2, "rate": 50.0, "supplier": sup_name}
				]),
				submit=True,
			)
		finally:
			frappe.set_user(prev_user)

		self.assertTrue(proc_res["success"])
		pos = proc_res["data"].get("purchase_orders") or []
		self.assertEqual(len(pos), 1)
		for po_name in pos:
			self.track("Purchase Order", po_name)

	def test_partially_approved_ro_surfaces_declined_deferred_service(self):
		"""Verify get_deferred_services(repair_order=...) surfaces declined lines synced from that RO."""
		q_res = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=45200,
			services=[
				{"service_title": "Approved Brake Service", "labor_hours": 1.0, "labor_rate": 140.0},
				{"service_title": "Declined Suspension Flush", "labor_hours": 1.0, "labor_rate": 150.0},
			],
		)
		ro_name = self._track_quote_ro(q_res)
		ro_doc = frappe.get_doc("Repair Order", ro_name)
		ro_doc.services[0].status = "Approved"
		ro_doc.services[1].status = "Declined"
		ro_doc.approval_status = "Partially Approved"
		ro_doc.status = "Authorized - In Progress"
		ro_doc.save(ignore_permissions=True)

		def_res = get_deferred_services(repair_order=ro_name)
		self.assertTrue(def_res["success"])
		titles = [d["job_title"] for d in def_res["data"]["deferred_services"]]
		self.assertIn("Declined Suspension Flush", titles)
		self.assertNotIn("Approved Brake Service", titles)

	def test_get_shop_floor_status_includes_schedule_entries(self):
		"""Verify get_shop_floor_status surfaces active schedule_entries and omits Cancelled ones."""
		target_day = add_days(today(), 4)
		bay_name = self.manifest.resolve("bays.main_lift")
		active_se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"status": "Scheduled",
			"scheduled_date": target_day,
			"scheduled_time": "09:30:00",
			"estimated_duration": 60,
			"service_bay": bay_name,
			"provisional_customer_name": f"{TEST_PREFIX}Floor Status Active",
		}).insert(ignore_permissions=True)
		self.track(active_se)

		cancelled_se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"status": "Cancelled",
			"scheduled_date": target_day,
			"scheduled_time": "11:00:00",
			"estimated_duration": 60,
			"service_bay": bay_name,
			"provisional_customer_name": f"{TEST_PREFIX}Floor Status Cancelled",
		}).insert(ignore_permissions=True)
		self.track(cancelled_se)

		res = get_shop_floor_status(date=target_day)
		self.assertTrue(res["success"])
		entries = res["data"].get("schedule_entries") or []
		by_name = {e["name"]: e for e in entries}
		self.assertIn(active_se.name, by_name)
		self.assertNotIn(cancelled_se.name, by_name)
		row = by_name[active_se.name]
		self.assertEqual(row["service_bay"], bay_name)
		self.assertEqual(row["scheduled_time"], "09:30:00")
		self.assertEqual(row["estimated_duration"], 60)
		self.assertTrue(row["end_time"])
		self.assertEqual(row["status"], "Scheduled")

	def test_provisional_walkin_book_and_convert_to_ro_resolves_customer_and_vehicle(self):
		"""Verify provisional walk-in booking resolves Customer and Repair Vehicle when converted to RO."""
		target_day = add_days(today(), 5)
		sophia_cust = self.manifest.resolve("customers.retail_2")
		sophia_veh = self.manifest.resolve("vehicles.veh_modely_sophia")
		bay_name = self.manifest.resolve("bays.tire_quick")

		book_res = book_appointment(
			scheduled_date=target_day,
			start_time="14:30:00",
			service_bay=bay_name,
			entry_type="Diagnostic",
			estimated_duration=60,
			provisional_customer_name="sophia desjardins",
			provisional_vehicle_info="2024 Tesla Model Y",
		)
		self.assertTrue(book_res["success"])
		se_name = book_res["data"]["name"]
		self.track("Schedule Entry", se_name)

		conv_res = create_ro_from_appointment(schedule_entry=se_name, odometer_in=61200)
		self.assertTrue(conv_res["success"])
		ro_name = conv_res["data"]["repair_order"]
		self.track("Repair Order", ro_name)

		ro_doc = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(ro_doc.customer, sophia_cust)
		self.assertEqual(ro_doc.vehicle, sophia_veh)
		self.assertEqual(ro_doc.odometer_in, 61200)

		se_doc = frappe.get_doc("Schedule Entry", se_name)
		self.assertEqual(se_doc.customer, sophia_cust)
		self.assertEqual(se_doc.repair_vehicle, sophia_veh)
		self.assertEqual(se_doc.repair_order, ro_name)

		# Unknown provisional customer still fails cleanly with ValidationError
		unknown_se = book_appointment(
			scheduled_date=target_day,
			start_time="15:30:00",
			service_bay=bay_name,
			entry_type="Diagnostic",
			estimated_duration=60,
			provisional_customer_name=f"{TEST_PREFIX}Nonexistent Walkin Person",
			provisional_vehicle_info="2024 Tesla Model Y",
		)
		self.assertTrue(unknown_se["success"])
		self.track("Schedule Entry", unknown_se["data"]["name"])
		with self.assertRaises(frappe.ValidationError):
			create_ro_from_appointment(schedule_entry=unknown_se["data"]["name"], odometer_in=10000)

		# Multi-vehicle household customer resolves specific model from provisional_vehicle_info
		hannah_cust = self.manifest.resolve("customers.household_primary")
		hannah_modelx = self.manifest.resolve("vehicles.veh_modelx_household")
		hh_se = book_appointment(
			scheduled_date=target_day,
			start_time="10:00:00",
			service_bay=bay_name,
			entry_type="Diagnostic",
			estimated_duration=60,
			provisional_customer_name="Hannah Kowalski",
			provisional_vehicle_info="Tesla Model X",
		)
		self.assertTrue(hh_se["success"])
		self.track("Schedule Entry", hh_se["data"]["name"])
		hh_ro = create_ro_from_appointment(schedule_entry=hh_se["data"]["name"], odometer_in=52600)
		self.assertTrue(hh_ro["success"])
		self.track("Repair Order", hh_ro["data"]["repair_order"])
		self.assertEqual(frappe.db.get_value("Repair Order", hh_ro["data"]["repair_order"], "customer"), hannah_cust)
		self.assertEqual(frappe.db.get_value("Repair Order", hh_ro["data"]["repair_order"], "vehicle"), hannah_modelx)

	def test_add_deferred_services_resolves_row_id_and_title(self):
		"""Verify add_deferred_services resolves Vehicle Deferred Service and Declined ROS row IDs as well as job_title."""
		hannah_cust = self.manifest.resolve("customers.household_primary")
		hannah_veh = self.manifest.resolve("vehicles.veh_modelx_household")

		veh_doc = frappe.get_doc("Repair Vehicle", hannah_veh)
		vds_row = veh_doc.append(
			"deferred_services",
			{
				"job_title": f"{TEST_PREFIX}Rear Brake Pad Replacement (3mm Remaining)",
				"estimated_amount": 325.0,
				"status": "Open Recommendation",
				"safety_hazard": 1,
				"decline_reason": "Deferred for next visit",
			},
		)
		veh_doc.save(ignore_permissions=True)
		vds_row_id = vds_row.name

		# Also create a prior RO with a Declined Repair Order Service row
		prior_ro = create_quote(
			customer_name=hannah_cust,
			vehicle_identifier=hannah_veh,
			odometer_in=52300,
			services=[
				{"service_title": f"{TEST_PREFIX}Approved Inspection", "labor_hours": 0.5, "labor_rate": 140.0},
				{"service_title": f"{TEST_PREFIX}Declined Coolant Flush", "labor_hours": 1.0, "labor_rate": 155.0},
			],
		)
		prior_ro_name = self._track_quote_ro(prior_ro)
		prior_ro_doc = frappe.get_doc("Repair Order", prior_ro_name)
		prior_ro_doc.services[0].status = "Approved"
		prior_ro_doc.services[1].status = "Declined"
		prior_ro_doc.approval_status = "Partially Approved"
		prior_ro_doc.status = "Authorized - In Progress"
		prior_ro_doc.save(ignore_permissions=True)
		declined_ros_row_id = prior_ro_doc.services[1].name

		# Also add a title-matched deferred recommendation
		veh_doc.reload()
		veh_doc.append(
			"deferred_services",
			{
				"job_title": f"{TEST_PREFIX}Cabin Air Filter Deferred",
				"estimated_amount": 85.0,
				"status": "Open Recommendation",
			},
		)
		veh_doc.save(ignore_permissions=True)

		target_q = create_quote(
			customer_name=hannah_cust,
			vehicle_identifier=hannah_veh,
			odometer_in=52450,
			services=[{"service_title": "Front Wiper Blade Set Replacement", "labor_hours": 0.5, "labor_rate": 140.0}],
		)
		target_ro_name = self._track_quote_ro(target_q)

		# 1. Add by Vehicle Deferred Service row ID
		res1 = add_deferred_services(repair_order=target_ro_name, deferred_items=[vds_row_id])
		self.assertTrue(res1["success"])
		self.assertEqual(res1["data"]["added_count"], 1)
		self.assertEqual(res1["data"]["added_services"], [f"{TEST_PREFIX}Rear Brake Pad Replacement (3mm Remaining)"])
		titles1 = [s["service_title"] for s in res1["data"]["services"]]
		self.assertNotIn(vds_row_id, titles1)
		self.assertIn(f"{TEST_PREFIX}Rear Brake Pad Replacement (3mm Remaining)", titles1)

		# 2. Add by Declined Repair Order Service row ID
		res2 = add_deferred_services(repair_order=target_ro_name, deferred_items=[declined_ros_row_id])
		self.assertTrue(res2["success"])
		self.assertEqual(res2["data"]["added_count"], 1)
		self.assertEqual(res2["data"]["added_services"], [f"{TEST_PREFIX}Declined Coolant Flush"])
		titles2 = [s["service_title"] for s in res2["data"]["services"]]
		self.assertNotIn(declined_ros_row_id, titles2)
		self.assertIn(f"{TEST_PREFIX}Declined Coolant Flush", titles2)

		# 3. Add by job_title string
		res3 = add_deferred_services(
			repair_order=target_ro_name,
			deferred_items=[f"{TEST_PREFIX}Cabin Air Filter Deferred"],
		)
		self.assertTrue(res3["success"])
		self.assertEqual(res3["data"]["added_count"], 1)
		self.assertIn(f"{TEST_PREFIX}Cabin Air Filter Deferred", [s["service_title"] for s in res3["data"]["services"]])

	def test_generation_scoped_import_and_quote_disambiguation(self):
		"""Verify Agent Facade imports generation-scoped packages cleanly and create_quote picks the vehicle's generation."""
		shared_title = f"{TEST_PREFIX}High Voltage Pyrotechnic Fuse"
		imp_old = import_tesla_procedure(
			procedure_title=shared_title,
			correction_code="17019900",
			labor_hours=1.1,
			model="Model 3",
			applicable_generation="Model 3 Jun 2017 - Dec 2023",
		)
		imp_new = import_tesla_procedure(
			procedure_title=shared_title,
			correction_code="17019900",
			labor_hours=1.7,
			model="Model 3",
			applicable_generation="Model 3 Jan 2024",
		)
		self.assertTrue(imp_old["success"])
		self.assertTrue(imp_new["success"])
		self.assertNotEqual(imp_old["data"]["name"], imp_new["data"]["name"])
		self.track("Service Package", imp_old["data"]["name"])
		self.track("Service Package", imp_new["data"]["name"])
		self.assertEqual(imp_new["data"]["applicable_generation"], "Model 3 Jan 2024")
		self.assertTrue(imp_new["data"]["generation_compatibility"])

		highland_veh = self.create_test_vehicle(
			vin="5YJ3E1EA9RF333444",
			customer=self.customer,
			license_plate="HIGH-24",
			model="Model 3",
			model_year="2024",
			manufactured_month="04/24",
		)
		quote_res = create_quote(
			customer_name=self.customer,
			vehicle_identifier=highland_veh.name,
			odometer_in=1500,
			services=[shared_title],
		)
		ro_name = self._track_quote_ro(quote_res)
		ro_doc = frappe.get_doc("Repair Order", ro_name)
		self.assertEqual(ro_doc.services[0].service_package, imp_new["data"]["name"])
		self.assertEqual(ro_doc.services[0].applicable_generation, "Model 3 Jan 2024")

	def test_find_available_slots_derives_equipment_tags_from_repair_order(self):
		"""Verify find_available_slots auto-derives required equipment tags from RO packages and honors overrides."""
		align_pkg_name = self.manifest.resolve("packages.four_wheel_alignment")
		align_bay = self.manifest.resolve("bays.alignment")
		main_lift_bay = self.manifest.resolve("bays.main_lift")

		quote_res = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=25000,
			services=[align_pkg_name],
		)
		ro_name = self._track_quote_ro(quote_res)
		self.assertEqual(quote_res["data"]["required_equipment_tags"], ["Alignment Rack", "Lift"])

		target_day = add_days(today(), 5)
		derived_slots = find_available_slots(
			repair_order=ro_name,
			start_date=target_day,
			days_ahead=1,
			duration_minutes=60,
			max_slots=5,
		)
		self.assertTrue(derived_slots["data"]["slots"])
		for slot in derived_slots["data"]["slots"]:
			self.assertEqual(slot["available_bay_ids"], [align_bay])

		# Explicit equipment_tags override ignores derived RO tags
		override_slots = find_available_slots(
			repair_order=ro_name,
			start_date=target_day,
			days_ahead=1,
			duration_minutes=60,
			equipment_tags=["Lift"],
			max_slots=5,
		)
		self.assertTrue(override_slots["data"]["slots"])
		self.assertIn(main_lift_bay, override_slots["data"]["slots"][0]["available_bay_ids"])

		# Explicit empty list disables tag filtering
		unfiltered_slots = find_available_slots(
			repair_order=ro_name,
			start_date=target_day,
			days_ahead=1,
			duration_minutes=60,
			equipment_tags=[],
			max_slots=5,
		)
		self.assertTrue(unfiltered_slots["data"]["slots"])
		self.assertIn(main_lift_bay, unfiltered_slots["data"]["slots"][0]["available_bay_ids"])

