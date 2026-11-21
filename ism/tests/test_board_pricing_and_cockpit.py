# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import today

from ism.board.cockpit_view import (
	build_cockpit_view,
	get_cockpit_view,
)
from ism.board.pricing import (
	ServerPricingConfig,
	build_tesla_part_search_url,
	calculate_part_price,
	load_server_pricing_config,
)
from ism.tests.base import ISMTestCase


class TestBoardPricingAndCockpit(ISMTestCase):
	"""Unit and integration tests for server-side parts pricing and /cockpit view engine."""

	def test_default_config_matches_parts_pricing_json(self):
		canonical_defaults = {
			"currency": "CAD",
			"tesla_search_url_template": "https://parts.tesla.com/en-CA/find-part?searchTerm={query}",
			"baseline_discount_pct": 30.0,
			"condition_discount_overrides": {"New": None, "Used": None, "Reconditioned": None},
			"small_ticket_max_tesla_price": 20.0,
			"small_ticket_discount_pct": 0.0,
			"min_sell_price": 5.0,
			"min_cost_markup_pct": 25.0,
			"min_cost_margin_dollars": 5.0,
			"fallback_cost_markup_pct": 40.0,
			"rounding_increment": 0.01,
		}
		rules_path = Path(__file__).resolve().parents[3] / "ism-agent-os" / "rules" / "parts_pricing.json"
		expected = (
			json.loads(rules_path.read_text(encoding="utf-8"))
			if rules_path.exists()
			else canonical_defaults
		)
		cfg = ServerPricingConfig()
		cfg.validate()
		self.assertEqual(cfg.currency, expected["currency"])
		self.assertEqual(cfg.tesla_search_url_template, expected["tesla_search_url_template"])
		self.assertEqual(cfg.baseline_discount_pct, expected["baseline_discount_pct"])
		self.assertEqual(cfg.condition_discount_overrides, expected["condition_discount_overrides"])
		self.assertEqual(cfg.small_ticket_max_tesla_price, expected["small_ticket_max_tesla_price"])
		self.assertEqual(cfg.small_ticket_discount_pct, expected["small_ticket_discount_pct"])
		self.assertEqual(cfg.min_sell_price, expected["min_sell_price"])
		self.assertEqual(cfg.min_cost_markup_pct, expected["min_cost_markup_pct"])
		self.assertEqual(cfg.min_cost_margin_dollars, expected["min_cost_margin_dollars"])
		self.assertEqual(cfg.fallback_cost_markup_pct, expected["fallback_cost_markup_pct"])
		self.assertEqual(cfg.rounding_increment, expected["rounding_increment"])

	def test_load_server_pricing_config_defensive_getattr(self):
		# Unseeded / partial settings object falls back cleanly to defaults
		partial = SimpleNamespace(parts_pricing_rounding_increment=0.0, parts_pricing_baseline_discount_pct=None)
		cfg = load_server_pricing_config(partial)
		self.assertEqual(cfg.baseline_discount_pct, 30.0)
		self.assertEqual(cfg.rounding_increment, 0.01)

		custom = SimpleNamespace(
			currency="CAD",
			parts_pricing_baseline_discount_pct=25.0,
			parts_pricing_small_ticket_max_tesla_price=15.0,
			parts_pricing_small_ticket_discount_pct=0.0,
			parts_pricing_min_sell_price=6.0,
			parts_pricing_min_cost_markup_pct=20.0,
			parts_pricing_min_cost_margin_dollars=4.0,
			parts_pricing_fallback_cost_markup_pct=35.0,
			parts_pricing_rounding_increment=0.05,
			parts_pricing_condition_overrides_json='{"New": null, "Used": 50.0, "Reconditioned": 40.0}',
			parts_pricing_tesla_search_url_template="https://parts.tesla.com/en-CA/find-part?searchTerm={query}",
		)
		cfg2 = load_server_pricing_config(custom)
		self.assertEqual(cfg2.baseline_discount_pct, 25.0)
		self.assertEqual(cfg2.condition_discount_overrides["Used"], 50.0)
		self.assertEqual(cfg2.rounding_increment, 0.05)

	def test_pricing_config_validation_bounds(self):
		with self.assertRaises(ValueError):
			ServerPricingConfig(baseline_discount_pct=-1.0).validate()
		with self.assertRaises(ValueError):
			ServerPricingConfig(baseline_discount_pct=100.0).validate()
		with self.assertRaises(ValueError):
			ServerPricingConfig(condition_discount_overrides={"Used": 105.0}).validate()
		with self.assertRaises(ValueError):
			ServerPricingConfig(min_sell_price=-0.5).validate()
		with self.assertRaises(ValueError):
			ServerPricingConfig(rounding_increment=0.0).validate()
		with self.assertRaises(ValueError):
			ServerPricingConfig(tesla_search_url_template="https://parts.tesla.com/").validate()
		with self.assertRaises(ValueError):
			build_tesla_part_search_url("   ", ServerPricingConfig())

	def test_calculate_part_price_all_four_regimes_and_bounds(self):
		cfg = ServerPricingConfig()

		# Regime 1: Manual override
		res1 = calculate_part_price(
			tesla_oem_price=100.0,
			cost=50.0,
			override_rate=123.456,
			part_number="1044321-00-G",
			config=cfg,
		)
		self.assertTrue(res1["internal_only"])
		self.assertEqual(res1["rule_applied"], "manual_override")
		self.assertEqual(res1["pricing_regime"], "manual_override")
		self.assertEqual(res1["sell_price"], 123.46)
		self.assertTrue(res1["above_tesla"])
		self.assertEqual(res1["margin_dollars"], 73.46)
		self.assertIn("1044321-00-G", res1["tesla_search_url"])

		# Regime 2a: Small ticket match (<= $20, 0% discount)
		res2a = calculate_part_price(tesla_oem_price=15.0, cost=8.0, config=cfg)
		self.assertEqual(res2a["rule_applied"], "small_ticket_match")
		self.assertEqual(res2a["sell_price"], 15.0)

		# Regime 2b: Baseline offset (30% off Tesla OEM)
		res2b = calculate_part_price(tesla_oem_price=100.0, cost=40.0, config=cfg)
		self.assertEqual(res2b["rule_applied"], "baseline_offset")
		self.assertEqual(res2b["sell_price"], 70.0)
		self.assertFalse(res2b["above_tesla"])

		# Regime 2c: Condition override
		cfg_used = ServerPricingConfig(
			condition_discount_overrides={"New": None, "Used": 50.0, "Reconditioned": 40.0}
		)
		res2c = calculate_part_price(
			tesla_oem_price=200.0,
			cost=60.0,
			part_condition="Used",
			config=cfg_used,
		)
		self.assertEqual(res2c["rule_applied"], "condition_override")
		self.assertEqual(res2c["sell_price"], 100.0)

		# Regime 2d: Min sell floor ($5)
		res2d = calculate_part_price(tesla_oem_price=3.0, cost=None, config=cfg)
		self.assertEqual(res2d["rule_applied"], "min_sell_floor")
		self.assertEqual(res2d["sell_price"], 5.0)
		self.assertTrue(res2d["above_tesla"])

		# Regime 2e: Cost floor (cost * 1.25 > tesla * 0.70)
		res2e = calculate_part_price(tesla_oem_price=100.0, cost=65.0, config=cfg)
		self.assertEqual(res2e["rule_applied"], "cost_floor")
		self.assertEqual(res2e["sell_price"], 81.25)

		# Regime 3: Cost-plus fallback (40% markup when Tesla OEM price unavailable)
		res3 = calculate_part_price(tesla_oem_price=None, cost=100.0, config=cfg)
		self.assertEqual(res3["rule_applied"], "cost_plus_fallback")
		self.assertEqual(res3["sell_price"], 140.0)

		# Regime 4: Rounding increment parity (0.05)
		cfg_round = ServerPricingConfig(rounding_increment=0.05)
		res4 = calculate_part_price(tesla_oem_price=99.99, cost=30.0, config=cfg_round)
		self.assertEqual(res4["sell_price"], 70.0)

		# Negative / missing inputs
		with self.assertRaises(ValueError):
			calculate_part_price(tesla_oem_price=-10.0, config=cfg)
		with self.assertRaises(ValueError):
			calculate_part_price(cost=-1.0, config=cfg)
		with self.assertRaises(ValueError):
			calculate_part_price(override_rate=-5.0, config=cfg)
		with self.assertRaises(ValueError):
			calculate_part_price(tesla_oem_price=None, cost=None, config=cfg)

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
						{"employee": "TECH-1", "employee_name": "Alex Tech", "on_leave": False, "utilization_pct": 50.0},
					]
				},
				"next_open_technicians": [
					{"employee": "TECH-1", "employee_name": "Alex Tech", "on_leave": False},
				],
				"schedule_entries": [
					{
						"name": "SE-001",
						"entry_type": "Repair",
						"status": "In Progress",
						"scheduled_date": "2026-06-15",
						"scheduled_time": "09:00:00",
						# Seam 4 test: even if customer_arrival_time is present, swimlane MUST use T (09:00)
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
				"action_items": [],
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
		self.assertEqual(rules["dead_lift"]["open_approval_count"], 0)

		self.assertIn("quote_cold", rules)
		self.assertEqual(rules["quote_cold"]["age_minutes"], 210)
		self.assertEqual(rules["quote_cold"]["thread_key"], "RO:RO-COLD")

		self.assertIn("parts_deficit", rules)
		self.assertEqual(rules["parts_deficit"]["priority"], "Critical")

		# 7 core gauges always present
		self.assertEqual(len(view["gauges"]), 7)

		# Seam 4 Internal Time Invariant: SE-001 MUST start at 09:00 (T), NOT 08:45 (T - offset)
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

	def test_build_cockpit_view_with_shop_board_thread_enrichment(self):
		snap = self._sample_snapshot()
		thread_map = {
			"RO:RO-DEADLIFT": {
				"thread_key": "RO:RO-DEADLIFT",
				"title": "RO-DEADLIFT · 2022 Model Y",
				"repair_order": "RO-DEADLIFT",
				"board_status": "BLOCKED_DECISION",
				"blocked_reason": "Awaiting PO submit approval",
				"blocked_since": "2026-06-15 09:45:00",
				"open_approval_count": 1,
				"open_clarification_count": 0,
			},
			"SHOP:INTAKE": {
				"thread_key": "SHOP:INTAKE",
				"title": "Front Desk Intake & Booking",
				"repair_order": None,
				"board_status": "BLOCKED_DECISION",
				"blocked_reason": "Clarify VIN",
				"blocked_since": "2026-06-15 10:00:00",
				"open_approval_count": 0,
				"open_clarification_count": 1,
			},
		}
		view = build_cockpit_view(snap, thread_map=thread_map)

		dead_lift_item = next(it for it in view["action_queue"] if it["id"] == "dead_lift:RO-DEADLIFT")
		self.assertEqual(dead_lift_item["board_status"], "BLOCKED_DECISION")
		self.assertEqual(dead_lift_item["open_approval_count"], 1)

		board_decisions = [it for it in view["action_queue"] if it["rule"] == "board_decision"]
		self.assertEqual(len(board_decisions), 2)
		intake_dec = next(it for it in board_decisions if it["thread_key"] == "SHOP:INTAKE")
		self.assertEqual(intake_dec["board_url"], "/board?thread=SHOP:INTAKE")
		self.assertEqual(intake_dec["open_clarification_count"], 1)

		se1 = next(b for b in view["swimlane"]["bay_lanes"][0]["blocks"] if b["name"] == "SE-001")
		self.assertEqual(se1["board_status"], "BLOCKED_DECISION")
		self.assertEqual(se1["open_approval_count"], 1)

	def test_get_cockpit_view_live_and_absent_thread_table_safe(self):
		with patch.object(frappe.db, "table_exists", return_value=False):
			view = get_cockpit_view(date=today())
		self.assertIn("gauges", view)
		self.assertEqual(len(view["gauges"]), 7)
		self.assertIn("swimlane", view)
		self.assertIn("action_queue", view)
