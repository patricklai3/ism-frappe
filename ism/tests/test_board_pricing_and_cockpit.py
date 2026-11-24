# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import importlib
import json
from unittest.mock import patch

import frappe
from frappe.utils import today

from ism.board.cockpit_view import (
	build_cockpit_view,
	get_cockpit_view,
)
from ism.tests.base import ISMTestCase


class TestBoardPricingAndCockpit(ISMTestCase):
	"""Unit and integration tests for active-RO /cockpit view engine (server-side pricing removed)."""

	def test_deleted_pricing_module_does_not_exist(self):
		with self.assertRaises(ModuleNotFoundError):
			importlib.import_module("ism.board.pricing")

	def _sample_snapshot(self) -> dict:
		return {
			"success": True,
			"urls": {"repair_orders": "/app/repair-order", "calendar": "/app/schedule-entry"},
			"data": {
				"date": "2026-06-15",
				"next_open_date": "2026-06-16",
				"server_now": "2026-06-15 10:30:00",
				"settings": {
					"operating_hours_start": "08:00:00",
					"operating_hours_end": "17:00:00",
					"break_start": "12:00:00",
					"break_end": "13:00:00",
				},
				"bays": [
					{"name": "Bay 1", "bay_name": "Bay 1", "equipment_tags": ["Lift"]},
					{"name": "Bay 2", "bay_name": "Bay 2", "equipment_tags": ["Alignment Rack"]},
				],
				"technician_overview": {
					"technicians": [
						{
							"employee": "TECH-1",
							"employee_name": "Alex Tech",
							"on_leave": False,
							"utilization_pct": 50.0,
						},
					]
				},
				"next_open_technicians": [
					{"employee": "TECH-1", "employee_name": "Alex Tech", "on_leave": False},
				],
				"schedule_entries": [
					{
						"name": "SE-EARLY",
						"entry_type": "Repair",
						"status": "Scheduled",
						"scheduled_date": "2026-06-15",
						"scheduled_time": "08:00:00",
						# Seam 4 test: even when customer_arrival_time is 07:45, swimlane MUST use T (08:00)
						"customer_arrival_time": "07:45:00",
						"estimated_duration": 60,
						"p80_duration_minutes": 60,
						"repair_order": "RO-COLD",
						"service_bay": "Bay 2",
						"assigned_technician": "TECH-1",
						"vehicle": "2021 Model S",
						"desk_url": "/app/schedule-entry/SE-EARLY",
					},
					{
						"name": "SE-001",
						"entry_type": "Repair",
						"status": "In Progress",
						"scheduled_date": "2026-06-15",
						"scheduled_time": "09:00:00",
						"customer_arrival_time": "08:45:00",
						"estimated_duration": 120,
						"p80_duration_minutes": 180,
						"repair_order": "RO-DEADLIFT",
						"service_bay": "Bay 1",
						"assigned_technician": "TECH-1",
						"vehicle": "2022 Model Y",
						"desk_url": "/app/schedule-entry/SE-001",
					},
					{
						"name": "SE-002",
						"entry_type": "Repair",
						"status": "Scheduled",
						"scheduled_date": "2026-06-15",
						"scheduled_time": "11:30:00",
						"estimated_duration": 60,
						"p80_duration_minutes": 60,
						"repair_order": "RO-NEXT",
						"service_bay": "Bay 1",
						"assigned_technician": "TECH-1",
						"vehicle": "2023 Model 3",
						"desk_url": "/app/schedule-entry/SE-002",
					},
				],
				"active_repair_orders": [
					{
						"name": "RO-DEADLIFT",
						"status": "Waiting on Parts",
						"approval_status": "Approved",
						"vehicle_location": "In Bay / Being Serviced",
						"picked_up_at": "2026-06-15 09:00:00",
						"modified": "2026-06-15 09:15:00",
						"customer": "Cust A",
						"vehicle": "2022 Model Y",
						"grand_total": 850.0,
						"parts_summary": {
							"has_uncovered_deficit": True,
							"deficit_items": [{"item_code": "PART-1", "uncovered_qty": 1}],
						},
						"desk_url": "/app/repair-order/RO-DEADLIFT",
					},
					{
						"name": "RO-COLD",
						"status": "Pending Authorization",
						"approval_status": "Pending Approval",
						"approval_sent_at": "2026-06-15 07:00:00",
						"modified": "2026-06-15 07:00:00",
						"customer": "Cust B",
						"vehicle": "2021 Model S",
						"grand_total": 1200.0,
						"parts_summary": {},
						"desk_url": "/app/repair-order/RO-COLD",
					},
					{
						"name": "RO-UNSLOTTED",
						"status": "Authorized - In Progress",
						"approval_status": "Approved",
						"vehicle_location": "In Bay / Being Serviced",
						"customer": "Cust C",
						"vehicle": "2024 Model X",
						"grand_total": 400.0,
						"parts_summary": {},
						"desk_url": "/app/repair-order/RO-UNSLOTTED",
					},
				],
				"action_items": [
					{
						"name": "ACT-2026-00042",
						"title": "Add front left upper control arm supplement",
						"description": "Tech reported cracked bushing on front left upper control arm.",
						"category": "Quote / Supplement",
						"priority": "High",
						"status": "Open",
						"source": "Board AI",
						"board_event": "BE-2026-00009",
						"reference_doctype": "Repair Order",
						"reference_name": "RO-DEADLIFT",
						"clearing_condition_type": "ro_has_supplement_or_service",
						"clearing_condition_json": json.dumps(
							{
								"baseline_service_count": 1,
								"description": "When supplement or service is added / sent for approval",
							}
						),
						"auto_cleared": 0,
						"creation": "2026-06-15 09:45:00",
						"desk_url": "/app/shop-action-item/ACT-2026-00042",
					}
				],
				"health_snapshots": {},
			},
		}

	def test_build_cockpit_view_dead_lift_cold_quote_internal_time_and_no_threads(self):
		snap = self._sample_snapshot()
		view = build_cockpit_view(snap, thread_map={})

		rules = {it["rule"]: it for it in view["action_queue"]}
		self.assertIn("dead_lift", rules)
		self.assertEqual(rules["dead_lift"]["priority"], "Critical")
		self.assertEqual(rules["dead_lift"]["thread_key"], "RO:RO-DEADLIFT")
		self.assertEqual(rules["dead_lift"]["board_url"], "/board?thread=RO:RO-DEADLIFT")
		self.assertIsNone(rules["dead_lift"]["board_status"])
		self.assertEqual(rules["dead_lift"]["open_action_count"], 0)
		self.assertNotIn("open_approval_count", rules["dead_lift"])
		self.assertNotIn("open_clarification_count", rules["dead_lift"])

		self.assertIn("quote_cold", rules)
		self.assertEqual(rules["quote_cold"]["age_minutes"], 210)
		self.assertEqual(rules["quote_cold"]["thread_key"], "RO:RO-COLD")

		self.assertIn("parts_deficit", rules)
		self.assertEqual(rules["parts_deficit"]["priority"], "Critical")

		# 7 core gauges always present
		self.assertEqual(len(view["gauges"]), 7)

		# Seam 4 Internal Time Invariant: SE-EARLY MUST start at 08:00 (T), NOT 07:45 (T - offset)
		bay2_blocks = view["swimlane"]["bay_lanes"][1]["blocks"]
		se_early = next(b for b in bay2_blocks if b["name"] == "SE-EARLY")
		self.assertEqual(se_early["start_clock"], "08:00")
		self.assertEqual(se_early["end_clock"], "09:00")

		# SE-001 MUST start at 09:00 (T), NOT 08:45
		bay1_blocks = view["swimlane"]["bay_lanes"][0]["blocks"]
		se1 = next(b for b in bay1_blocks if b["name"] == "SE-001")
		self.assertEqual(se1["start_clock"], "09:00")
		self.assertEqual(se1["end_clock"], "11:00")
		self.assertEqual(se1["state"], "dead_lift")
		self.assertEqual(se1["thread_key"], "RO:RO-DEADLIFT")
		self.assertEqual(se1["board_url"], "/board?thread=RO:RO-DEADLIFT")

		# SE-001 p80 (180m -> 12:00) overlaps SE-002 (11:30) on Bay 1 & TECH-1 -> spillover risk = 1
		spill_gauge = next(g for g in view["gauges"] if g["key"] == "spillover_risk")
		self.assertEqual(spill_gauge["value"], "1 at risk")

		# Unslotted bay RO enrichment
		unslotted = view["swimlane"]["unslotted_bay_ros"]
		self.assertEqual(len(unslotted), 1)
		self.assertEqual(unslotted[0]["thread_key"], "RO:RO-UNSLOTTED")
		self.assertEqual(unslotted[0]["board_url"], "/board?thread=RO:RO-UNSLOTTED")

	def test_build_cockpit_view_with_active_ro_enrichment_and_rule8_board_ai_item(self):
		snap = self._sample_snapshot()
		thread_map = {
			"RO:RO-DEADLIFT": {
				"thread_key": "RO:RO-DEADLIFT",
				"title": "RO-DEADLIFT · 2022 Model Y",
				"repair_order": "RO-DEADLIFT",
				"board_status": "NEEDS_ACTION",
				"blocked_reason": "1 open action item",
				"blocked_since": "2026-06-15 09:45:00",
				"open_action_count": 1,
			},
		}
		view = build_cockpit_view(snap, thread_map=thread_map)

		dead_lift_item = next(it for it in view["action_queue"] if it["id"] == "dead_lift:RO-DEADLIFT")
		self.assertEqual(dead_lift_item["board_status"], "NEEDS_ACTION")
		self.assertEqual(dead_lift_item["open_action_count"], 1)
		self.assertNotIn("open_approval_count", dead_lift_item)
		self.assertNotIn("open_clarification_count", dead_lift_item)

		# No duplicate board_decision:* entries should exist alongside Board AI Shop Action Items
		board_decisions = [
			it
			for it in view["action_queue"]
			if it.get("rule") == "board_decision" or str(it.get("id", "")).startswith("board_decision:")
		]
		self.assertEqual(board_decisions, [])

		# Rule 8 surfaces the Board AI Shop Action Item with canonical ACT ID & clearing condition metadata
		synced_items = [it for it in view["action_queue"] if it.get("action_item_name") == "ACT-2026-00042"]
		self.assertEqual(len(synced_items), 1)
		ai_item = synced_items[0]
		self.assertEqual(ai_item["source"], "Board AI")
		self.assertEqual(ai_item["board_event"], "BE-2026-00009")
		self.assertEqual(ai_item["clearing_condition_type"], "ro_has_supplement_or_service")
		self.assertIn("supplement", (ai_item.get("clearing_condition_description") or "").lower())
		self.assertEqual(ai_item["open_action_count"], 1)

		se1 = next(b for b in view["swimlane"]["bay_lanes"][0]["blocks"] if b["name"] == "SE-001")
		self.assertEqual(se1["board_status"], "NEEDS_ACTION")
		self.assertEqual(se1["open_action_count"], 1)
		self.assertNotIn("open_approval_count", se1)
		self.assertNotIn("open_clarification_count", se1)

	def test_get_cockpit_view_live_and_absent_thread_table_safe(self):
		with patch.object(frappe.db, "table_exists", return_value=False):
			view = get_cockpit_view(date=today())
		self.assertIn("gauges", view)
		self.assertEqual(len(view["gauges"]), 7)
		self.assertIn("swimlane", view)
		self.assertIn("action_queue", view)

	def test_cockpit_action_items_and_swimlane_blocks_vehicle_display(self):
		"""Cockpit derived action items & swimlane blocks include vehicle_info and human-friendly display (not raw VIN)."""
		from ism.ism.doctype.repair_vehicle.vin_decoder import compute_vin_check_digit

		cust = self.create_test_customer()
		template = "7SAYGDEE0PF334455"  # 2023 Model Y
		vin = f"{template[:8]}{compute_vin_check_digit(template)}{template[9:]}"
		veh = self.create_test_vehicle(
			vin=vin,
			customer=cust.name,
			color="Ultra Red",
			license_plate="ckt 555",
		)
		snap = self._sample_snapshot()
		snap["data"]["active_repair_orders"][0]["vehicle"] = veh.name
		snap["data"]["schedule_entries"][0]["vehicle"] = veh.name

		view = build_cockpit_view(snap)
		expected = "2023 Ultra Red Model Y (CKT 555)"

		dead_lift = next(it for it in view["action_queue"] if it["id"] == "dead_lift:RO-DEADLIFT")
		self.assertEqual(dead_lift["vehicle_display"], expected)
		self.assertEqual(dead_lift["vehicle_info"]["color"], "Ultra Red")
		self.assertEqual(dead_lift["vehicle_info"]["license_plate"], "CKT 555")
		self.assertIn(expected, dead_lift["subtitle"])
		self.assertNotIn(vin, dead_lift["subtitle"])

		se1 = next(b for b in view["swimlane"]["bay_lanes"][0]["blocks"] if b["name"] == "SE-001")
		self.assertEqual(se1["vehicle_display"], expected)
		self.assertIn(expected, se1["sublabel"])
		self.assertNotIn(vin, se1["sublabel"])
		self.assertEqual(se1["vehicle_info"]["display"], expected)
		self.assertEqual(se1["vehicle_info"]["compact_display"], "Ultra Red Model Y")
