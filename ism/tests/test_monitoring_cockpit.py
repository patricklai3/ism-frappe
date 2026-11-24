# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import today

from ism.api.agent import (
	book_appointment,
	checkout_repair_order,
	create_quote,
	create_shop_action_item,
	get_cockpit_snapshot,
	get_pnl_digest_inputs,
	import_supplier_catalog,
	list_shop_action_items,
	publish_health_snapshot,
	send_quote_for_approval,
	submit_procurement_orders,
	sync_action_item_state,
	update_job_status,
)
from ism.scheduling.time_utils import invalidate_shop_settings_cache
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestMonitoringCockpit(ISMTestCase):
	"""Integration tests for monitoring cockpit snapshots, P&L digest, synced action state, and health scores."""

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

	def _create_user_with_role(self, prefix: str, role: str) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.com"
		frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": prefix,
			"last_name": "Tester",
			"enabled": 1,
			"user_type": "System User",
			"send_welcome_email": 0,
			"roles": [{"role": role}],
		}).insert(ignore_permissions=True)
		self.track("User", email)
		return email

	def test_health_snapshot_permissions_bounds_and_delta(self):
		"""Verify Shop Health Snapshot validation, score delta, role split, and Guest isolation."""
		first = publish_health_snapshot(
			metric_key=f"{TEST_PREFIX}pnl_health",
			score=72,
			verdict="Slightly soft ELR",
			period_date=today(),
			report_url="/app/query-report/Profit%20and%20Loss%20Statement",
			details={"elr": 132.0},
		)
		self.assertTrue(first["success"])
		self.track("Shop Health Snapshot", first["data"]["name"])
		self.assertEqual(first["data"]["grade"], "Watch")
		self.assertIsNone(first["data"]["delta"])

		second = publish_health_snapshot(
			metric_key=f"{TEST_PREFIX}pnl_health",
			score=84,
			verdict="Recovered margin and ELR",
			period_date=today(),
		)
		self.assertTrue(second["success"])
		self.track("Shop Health Snapshot", second["data"]["name"])
		self.assertEqual(second["data"]["grade"], "Healthy")
		self.assertEqual(second["data"]["previous_score"], 72)
		self.assertEqual(second["data"]["delta"], 12)

		for bad_score in (-1, 101, True, "abc"):
			with self.assertRaises(frappe.ValidationError):
				publish_health_snapshot(
					metric_key=f"{TEST_PREFIX}bad",
					score=bad_score,
					verdict="Invalid score",
				)

		advisor = self._create_user_with_role("test_mon_adv", "ISM Service Advisor")
		tech = self._create_user_with_role("test_mon_tech", "ISM Technician")
		prev_user = frappe.session.user
		try:
			frappe.set_user(advisor)
			adv_res = publish_health_snapshot(
				metric_key=f"{TEST_PREFIX}adv_metric",
				score=81,
				verdict="Advisor can publish snapshot",
			)
			self.assertTrue(adv_res["success"])
			self.track("Shop Health Snapshot", adv_res["data"]["name"])

			frappe.set_user(tech)
			self.assertTrue(frappe.has_permission("Shop Health Snapshot", "read"))
			with self.assertRaises(frappe.PermissionError):
				publish_health_snapshot(
					metric_key=f"{TEST_PREFIX}tech_metric",
					score=50,
					verdict="Tech cannot publish",
				)

			frappe.set_user("Guest")
			for fn in (
				lambda: get_cockpit_snapshot(),
				lambda: get_pnl_digest_inputs(),
				lambda: publish_health_snapshot(metric_key="k", score=50, verdict="v"),
				lambda: sync_action_item_state(dedup_key="k", action="snooze"),
			):
				with self.assertRaises(frappe.PermissionError):
					fn()
		finally:
			frappe.set_user(prev_user)

	def test_action_item_dedup_upsert_sync_and_type_guards(self):
		"""Verify idempotent dedup_key upsert, snooze/dismiss/reopen transitions, and ValidationError guards."""
		d_key = f"{TEST_PREFIX}pnl_anomaly:2026-11-17"
		r1 = create_shop_action_item(
			title=f"{TEST_PREFIX}Margin Drop Alert",
			category="Analytics Threshold",
			priority="Medium",
			dedup_key=d_key,
			source="Agent",
		)
		self.assertTrue(r1["success"])
		sai_name = r1["data"]["name"]
		self.track("Shop Action Item", sai_name)
		self.assertEqual(r1["data"]["dedup_key"], d_key)
		self.assertEqual(r1["data"]["source"], "Agent")

		# Second call with same dedup_key upserts existing Open item instead of creating a duplicate
		r2 = create_shop_action_item(
			title=f"{TEST_PREFIX}Margin Drop Escalated",
			category="Analytics Threshold",
			priority="Critical",
			dedup_key=d_key,
			source="Agent",
		)
		self.assertEqual(r2["data"]["name"], sai_name)
		self.assertEqual(r2["data"]["priority"], "Critical")

		snooze_res = sync_action_item_state(
			dedup_key=f"{TEST_PREFIX}quote_needed:RO-999",
			action="snooze",
			snooze_minutes=90,
			title=f"{TEST_PREFIX}Snoozed Quote",
		)
		self.assertTrue(snooze_res["success"])
		self.track("Shop Action Item", snooze_res["data"]["name"])
		self.assertEqual(snooze_res["data"]["source"], "Rule Snooze")
		self.assertTrue(bool(snooze_res["data"]["snoozed_until"]))

		dismiss_res = sync_action_item_state(
			dedup_key=d_key,
			action="dismiss",
			notes="Reviewed by manager",
		)
		self.assertEqual(dismiss_res["data"]["status"], "Dismissed")

		reopen_res = sync_action_item_state(dedup_key=d_key, action="reopen")
		self.assertEqual(reopen_res["data"]["status"], "Open")
		self.assertIsNone(reopen_res["data"]["snoozed_until"])

		listed = list_shop_action_items(status="Open", limit=50)
		matching = [it for it in listed["data"]["items"] if it["name"] == sai_name]
		self.assertEqual(len(matching), 1)
		self.assertEqual(matching[0]["dedup_key"], d_key)

		for fn in (
			lambda: get_cockpit_snapshot(date={"!=": ""}),
			lambda: get_pnl_digest_inputs(from_date={"!=": ""}),
			lambda: publish_health_snapshot(metric_key={"!=": ""}, score=50, verdict="v"),
			lambda: sync_action_item_state(dedup_key={"!=": ""}, action="snooze"),
			lambda: sync_action_item_state(dedup_key="x", action="invalid_action"),
		):
			with self.assertRaises(frappe.ValidationError):
				fn()

	def test_cockpit_snapshot_live_ro_and_po_deficit_clearing(self):
		"""Verify get_cockpit_snapshot batches RO states, FIFO deficits, and open PO coverage accurately."""
		sup_name = f"{TEST_PREFIX}Cockpit Parts Sup"
		cat = import_supplier_catalog(
			raw_text="CKP-PAD-01 | Cockpit Brake Pad Set | OEM | New | 180.00 | 95.00 | 0 | A | 1044621-00-A",
			supplier=sup_name,
			default_source="OEM",
			default_condition="New",
		)
		self.track("Supplier", sup_name)
		item_code = cat["data"]["created"][0]["item_code"]
		self.track("Item", item_code)

		q = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=43100,
			services=[{
				"service_title": "Cockpit Pad Replacement",
				"labor_hours": 1.0,
				"parts": [{"item_code": item_code, "qty": 1.0, "rate": 180.0}],
			}],
		)
		ro_name = q["data"]["name"]
		self.track("Repair Order", ro_name)

		snap_draft = get_cockpit_snapshot(date=today())
		self.assertTrue(snap_draft["success"])
		self.assertTrue(snap_draft["data"]["bays"])
		ro_entry_1 = next(r for r in snap_draft["data"]["active_repair_orders"] if r["name"] == ro_name)
		self.assertEqual(ro_entry_1["status"], "Draft Estimate")

		send_quote_for_approval(repair_order=ro_name, communication_medium="link")
		ro = frappe.get_doc("Repair Order", ro_name)
		ro.authorize_jobs(approvals={ro.services[0].name: "Approved"}, signer_name="Cockpit Tester")
		ro.reload()
		self.assertEqual(ro.status, "Waiting on Parts")

		book_res = book_appointment(
			repair_order=ro_name,
			scheduled_date=today(),
			start_time="10:00:00",
			service_bay=self.manifest.resolve("bays.main_lift"),
		)
		self.track("Schedule Entry", book_res["data"]["name"])

		snap_deficit = get_cockpit_snapshot(date=today())
		ro_entry_2 = next(r for r in snap_deficit["data"]["active_repair_orders"] if r["name"] == ro_name)
		self.assertTrue(ro_entry_2["parts_summary"]["has_stock_deficit"])
		self.assertTrue(ro_entry_2["parts_summary"]["has_uncovered_deficit"])
		self.assertGreater(ro_entry_2["p80_duration_minutes"], 0)
		self.assertTrue(any(se["name"] == book_res["data"]["name"] for se in snap_deficit["data"]["schedule_entries"]))

		# Submitting a covering Purchase Order eliminates the uncovered deficit
		po_res = submit_procurement_orders(
			repair_order=ro_name,
			parts_data=[{"item_code": item_code, "qty": 1.0, "supplier": sup_name}],
			submit=True,
		)
		for po_name in po_res["data"]["purchase_orders"]:
			self.track("Purchase Order", po_name)

		snap_covered = get_cockpit_snapshot(date=today())
		ro_entry_3 = next(r for r in snap_covered["data"]["active_repair_orders"] if r["name"] == ro_name)
		self.assertTrue(ro_entry_3["parts_summary"]["has_stock_deficit"])
		self.assertFalse(ro_entry_3["parts_summary"]["has_uncovered_deficit"])
		self.assertEqual(ro_entry_3["parts_summary"]["deficit_items"][0]["open_po_qty"], 1.0)

	def test_pnl_digest_inputs_computes_billed_elr_and_unbilled_wip(self):
		"""Verify get_pnl_digest_inputs summarizes submitted Sales Invoices and unbilled Work Completed ROs."""
		q1 = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=44000,
			services=[{"service_title": "Alignment Check", "labor_hours": 1.5, "labor_rate": 160.0}],
		)
		ro1_name = q1["data"]["name"]
		self.track("Repair Order", ro1_name)
		ro1 = frappe.get_doc("Repair Order", ro1_name)
		ro1.authorize_jobs(approvals={ro1.services[0].name: "Approved"}, signer_name="Billing Tester")
		update_job_status(repair_order=ro1_name, action="complete", odometer_out=44010)
		chk = checkout_repair_order(
			repair_order=ro1_name,
			payment_method="Cash",
			odometer_out=44010,
			confirm=True,
		)
		if chk["data"].get("sales_invoice"):
			self.track("Sales Invoice", chk["data"]["sales_invoice"])
		if chk["data"].get("payment_entry"):
			self.track("Payment Entry", chk["data"]["payment_entry"])

		q2 = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=44020,
			services=[{"service_title": "Brake Fluid Flush", "labor_hours": 1.0, "labor_rate": 150.0}],
		)
		ro2_name = q2["data"]["name"]
		self.track("Repair Order", ro2_name)
		ro2 = frappe.get_doc("Repair Order", ro2_name)
		ro2.authorize_jobs(approvals={ro2.services[0].name: "Approved"}, signer_name="WIP Tester")
		update_job_status(repair_order=ro2_name, action="complete", odometer_out=44025)

		digest = get_pnl_digest_inputs(from_date=today(), to_date=today())
		self.assertTrue(digest["success"])
		data = digest["data"]
		self.assertGreaterEqual(data["invoice_count"], 1)
		self.assertGreaterEqual(data["labor_revenue"], 240.0)
		self.assertGreaterEqual(data["billed_labor_hours"], 1.5)
		self.assertGreater(data["effective_labor_rate"], 0.0)
		self.assertGreaterEqual(data["unbilled_completed_count"], 1)
		self.assertGreater(data["unbilled_completed_total"], 0.0)

	def test_cockpit_snapshot_excludes_billed_ro_from_fifo_parts(self):
		"""Verify Billed / Paid and stock-invoiced ROs do not double-deduct FIFO stock for active ROs."""
		from ism.api.parts_stock import _resolve_default_warehouse

		sup_name = f"{TEST_PREFIX}FIFO Billed Sup"
		cat = import_supplier_catalog(
			raw_text="CKP-FIFO-01 | Cockpit FIFO Rotor | OEM | New | 220.00 | 110.00 | 0 | A | 1044622-00-A",
			supplier=sup_name,
			default_source="OEM",
			default_condition="New",
		)
		self.track("Supplier", sup_name)
		item_code = cat["data"]["created"][0]["item_code"]
		self.track("Item", item_code)

		q1 = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=45100,
			services=[{
				"service_title": "FIFO Rotor Install 1",
				"labor_hours": 1.0,
				"parts": [{"item_code": item_code, "qty": 1.0, "rate": 220.0}],
			}],
		)
		ro1_name = q1["data"]["name"]
		self.track("Repair Order", ro1_name)
		ro1 = frappe.get_doc("Repair Order", ro1_name)
		wh = _resolve_default_warehouse(ro1.company)

		se = frappe.get_doc({
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": ro1.company,
			"items": [{
				"item_code": item_code,
				"qty": 2.0,
				"t_warehouse": wh,
				"basic_rate": 110.0,
			}],
		}).insert(ignore_permissions=True)
		se.submit()
		self.track("Stock Entry", se.name)

		ro1.authorize_jobs(approvals={ro1.services[0].name: "Approved"}, signer_name="FIFO Tester 1")
		update_job_status(repair_order=ro1_name, action="complete", odometer_out=45105)
		chk = checkout_repair_order(
			repair_order=ro1_name,
			payment_method="Cash",
			odometer_out=45105,
			confirm=True,
		)
		si_name = chk["data"].get("sales_invoice")
		if si_name:
			self.track("Sales Invoice", si_name)
		if chk["data"].get("payment_entry"):
			self.track("Payment Entry", chk["data"]["payment_entry"])

		# Simulate RO1 sitting in Billed / Paid status after stock was already deducted by SI (1 unit left in Bin)
		frappe.db.set_value("Repair Order", ro1_name, "status", "Billed / Paid")

		q2 = create_quote(
			customer_name=self.customer,
			vehicle_identifier=self.vehicle_vin,
			odometer_in=45120,
			services=[{
				"service_title": "FIFO Rotor Install 2",
				"labor_hours": 1.0,
				"parts": [{"item_code": item_code, "qty": 1.0, "rate": 220.0}],
			}],
		)
		ro2_name = q2["data"]["name"]
		self.track("Repair Order", ro2_name)
		ro2 = frappe.get_doc("Repair Order", ro2_name)
		ro2.authorize_jobs(approvals={ro2.services[0].name: "Approved"}, signer_name="FIFO Tester 2")

		# Active RO2 needs 1 unit and Bin has 1 unit left; Billed / Paid RO1 must not consume it again
		snap = get_cockpit_snapshot(date=today())
		ro2_entry = next(r for r in snap["data"]["active_repair_orders"] if r["name"] == ro2_name)
		self.assertFalse(ro2_entry["parts_summary"]["has_stock_deficit"])
		self.assertFalse(ro2_entry["parts_summary"]["has_uncovered_deficit"])
		self.assertTrue(ro2_entry["parts_summary"]["all_parts_ready"])

		# Even without a linked sales_invoice field, Billed / Paid status alone must be excluded from FIFO
		frappe.db.set_value("Repair Order", ro1_name, "sales_invoice", None)
		snap_no_si = get_cockpit_snapshot(date=today())
		ro2_entry_2 = next(r for r in snap_no_si["data"]["active_repair_orders"] if r["name"] == ro2_name)
		self.assertFalse(ro2_entry_2["parts_summary"]["has_stock_deficit"])
		self.assertTrue(ro2_entry_2["parts_summary"]["all_parts_ready"])

		# And an active-status RO with a submitted update_stock=1 Sales Invoice must not double-deduct either
		frappe.db.set_value("Repair Order", ro1_name, {"status": "Work Completed", "sales_invoice": si_name})
		snap_invoiced_active = get_cockpit_snapshot(date=today())
		ro2_entry_3 = next(r for r in snap_invoiced_active["data"]["active_repair_orders"] if r["name"] == ro2_name)
		self.assertFalse(ro2_entry_3["parts_summary"]["has_stock_deficit"])
		self.assertTrue(ro2_entry_3["parts_summary"]["all_parts_ready"])
