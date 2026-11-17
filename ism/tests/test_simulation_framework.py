# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Stage 2 Gate Test Suite verifying World Profiles (profiles.py) and Monte Carlo (monte_carlo.py)."""

from __future__ import annotations

import json

import frappe
from frappe.utils import flt

from ism.scheduling.capacity import NON_BLOCKING_ENTRY_STATUSES
from ism.scheduling.entry_queries import build_holiday_checker, entry_datetimes, intervals_overlap
from ism.scheduling.time_utils import get_shop_settings, normalize_time
from ism.testing import (
	SimManifest,
	VALID_PROFILES,
	build_profile,
	monte_carlo,
	provision_agent_api_keys,
	seed_monte_carlo,
	seed_profile,
)
from ism.testing.profiles import _ensure_historical_telemetry, _ensure_submitted_leave
from ism.tests.base import ISMTestCase


class TestSimulationFramework(ISMTestCase):
	"""Verify Layer 3 profiles, API key provisioning, Monte Carlo determinism, and invariants."""

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_clean_master_profile_and_invalid_profile_guard(self):
		"""clean_master returns only Layer 1 masters and rejects unknown profile names."""
		self.assertEqual(VALID_PROFILES, ("clean_master", "eval_standard", "swarm_crunch_day"))

		manifest = build_profile("clean_master", seed=42, anchor_date="2026-11-18", commit=False)
		self.assertEqual(manifest.profile, "clean_master")
		self.assertEqual(manifest.company, "Lightspeed EV")
		self.assertEqual(len(manifest.bays), 5)
		self.assertEqual(len(manifest.packages), 10)
		self.assertEqual(len(manifest.vehicles), 10)
		self.assertEqual(manifest.ros, {})
		self.assertEqual(manifest.schedule_entries, {})
		self.assertEqual(manifest.leave_applications, {})

		payload = seed_profile("clean_master", seed=42, anchor_date="2026-11-18", commit=False)
		self.assertIsInstance(payload, dict)
		self.assertEqual(payload["profile"], "clean_master")
		self.assertTrue(json.dumps(payload))

		with self.assertRaises(ValueError):
			build_profile("nonexistent_profile", commit=False)

	def test_eval_standard_profile_handles_metadata_and_idempotency(self):
		"""eval_standard creates all locked handles, metadata, telemetry, leave, conflicts, and procurement."""
		manifest = build_profile("eval_standard", seed=42, anchor_date="2026-11-18", commit=False)
		self.assertEqual(manifest.profile, "eval_standard")
		self.assertEqual(manifest.metadata["target_date"], "2026-11-19")
		self.assertEqual(manifest.metadata["historical_ro_count"], 30)
		self.assertEqual(len(manifest.metadata["historical_ros"]), 30)

		# Leave application for Elena on target_date
		leave_name = manifest.resolve("leave_applications.elena_tomorrow")
		leave_doc = frappe.get_doc("Leave Application", leave_name)
		self.assertEqual(leave_doc.docstatus, 1)
		self.assertEqual(leave_doc.status, "Approved")
		self.assertEqual(leave_doc.employee, manifest.resolve("techs.elena"))
		self.assertEqual(str(leave_doc.from_date), "2026-11-19")

		# Primary conflict booking
		conflict_se = frappe.get_doc("Schedule Entry", manifest.resolve("schedule_entries.conflict_main_lift"))
		self.assertEqual(conflict_se.repair_order, manifest.resolve("ros.conflict_ro"))
		self.assertEqual(conflict_se.service_bay, manifest.resolve("bays.main_lift"))
		self.assertEqual(conflict_se.assigned_technician, manifest.resolve("techs.marcus"))
		self.assertEqual(str(conflict_se.scheduled_date), "2026-11-19")
		self.assertEqual(normalize_time(conflict_se.scheduled_time), "09:00:00")
		self.assertEqual(int(conflict_se.estimated_duration), 90)

		# Secondary HV scheduled booking
		hv_se = frappe.get_doc("Schedule Entry", manifest.resolve("schedule_entries.hv_morning"))
		self.assertEqual(hv_se.repair_order, manifest.resolve("ros.hv_scheduled_ro"))
		self.assertEqual(hv_se.service_bay, manifest.resolve("bays.hv_diagnostics"))
		self.assertEqual(hv_se.assigned_technician, manifest.resolve("techs.noah"))
		self.assertEqual(normalize_time(hv_se.scheduled_time), "10:00:00")
		self.assertEqual(int(hv_se.estimated_duration), 60)

		# Two unscheduled authorized_in_stock ROs
		for ro_handle, expected_cust, expected_veh, svc_count in (
			("unscheduled_ro", "retail_1", "veh_model3_liam", 2),
			("unscheduled_secondary_ro", "retail_2", "veh_modely_sophia", 1),
		):
			ro_doc = frappe.get_doc("Repair Order", manifest.resolve(f"ros.{ro_handle}"))
			self.assertEqual(ro_doc.status, "Authorized - In Progress")
			self.assertEqual(ro_doc.customer, manifest.resolve(f"customers.{expected_cust}"))
			self.assertEqual(ro_doc.vehicle, manifest.resolve(f"vehicles.{expected_veh}"))
			self.assertEqual(len(ro_doc.services), svc_count)
			self.assertEqual(
				frappe.db.count("Schedule Entry", {"repair_order": ro_doc.name}),
				0,
			)

		# Open PO + Bank Draft advance PE (waiting_on_parts, unreceived)
		open_po_ro = frappe.get_doc("Repair Order", manifest.resolve("ros.open_po_ro"))
		self.assertEqual(open_po_ro.status, "Waiting on Parts")
		open_po = frappe.get_doc("Purchase Order", manifest.resolve("pos.eval_open_po"))
		self.assertEqual(open_po.docstatus, 1)
		self.assertAlmostEqual(flt(open_po.per_received), 0.0, places=2)
		advance_pe = frappe.get_doc("Payment Entry", manifest.resolve("pes.eval_open_po_advance"))
		self.assertEqual(advance_pe.docstatus, 1)
		self.assertEqual(advance_pe.mode_of_payment, "Bank Draft")

		# Zero-stock RFQ waiting_on_parts RO
		zero_ro = frappe.get_doc("Repair Order", manifest.resolve("ros.zero_stock_ro"))
		self.assertEqual(zero_ro.status, "Waiting on Parts")
		open_rfq = frappe.get_doc("Request for Quotation", manifest.resolve("rfqs.eval_open_rfq"))
		self.assertEqual(open_rfq.docstatus, 1)

		# Idempotency on warm DB for historical telemetry and leave reuse
		ctx = self.sim_context(seed=42, anchor_date="2026-11-18")
		reused_hist = _ensure_historical_telemetry(ctx, n_closed_ros=30)
		self.assertEqual(reused_hist, manifest.metadata["historical_ros"])
		reused_leave = _ensure_submitted_leave(ctx, "elena", "2026-11-19", "privilege", "elena_tomorrow")
		self.assertEqual(reused_leave, leave_name)

	def test_provision_agent_api_keys_commits_credentials(self):
		"""provision_agent_api_keys generates valid key/secret on a clean transaction without leaking ROs."""
		frappe.db.rollback()
		creds = provision_agent_api_keys("manager@lightspeedev.ca")
		self.assertEqual(creds["user"], "manager@lightspeedev.ca")
		self.assertEqual(creds["company"], "Lightspeed EV")
		self.assertTrue(bool(creds["api_key"]))
		self.assertTrue(bool(creds["api_secret"]))
		self.assertEqual(frappe.db.get_value("User", "manager@lightspeedev.ca", "api_key"), creds["api_key"])

	def test_swarm_crunch_day_utilization_fifo_supplements_and_fleet(self):
		"""swarm_crunch_day hits ~85% bay utilization, FIFO winner/loser, over-tolerance supplements, and fleet."""
		manifest = build_profile("swarm_crunch_day", seed=42, anchor_date="2026-11-18", commit=False)
		self.assertEqual(manifest.profile, "swarm_crunch_day")
		target_date = manifest.metadata["target_date"]
		self.assertEqual(target_date, "2026-11-19")

		util_pct = flt(manifest.metadata["bay_utilization_pct"])
		self.assertGreaterEqual(util_pct, 80.0)
		self.assertLessEqual(util_pct, 90.0)

		# Verify each active bay has 435 scheduled minutes on target_date
		for bay_handle in ("main_lift", "alignment", "hv_diagnostics", "tire_quick"):
			bay_name = manifest.resolve(f"bays.{bay_handle}")
			entries = frappe.get_all(
				"Schedule Entry",
				filters={
					"service_bay": bay_name,
					"scheduled_date": target_date,
					"status": ["not in", list(NON_BLOCKING_ENTRY_STATUSES)],
				},
				fields=["name", "estimated_duration"],
			)
			self.assertEqual(sum(int(e.estimated_duration or 0) for e in entries), 435)

		# Competing FIFO winner vs loser
		winner_ro = frappe.get_doc("Repair Order", manifest.resolve("ros.crunch_fifo_winner"))
		loser_ro = frappe.get_doc("Repair Order", manifest.resolve("ros.crunch_fifo_loser"))
		self.assertEqual(winner_ro.status, "Authorized - In Progress")
		self.assertEqual(loser_ro.status, "Waiting on Parts")

		# Unapproved over-tolerance supplements (> max(10%, $20)) + Shop Action Items
		over_tol_ros = manifest.metadata["over_tolerance_ros"]
		self.assertGreaterEqual(len(over_tol_ros), 2)
		for ro_name in over_tol_ros:
			ro_doc = frappe.get_doc("Repair Order", ro_name)
			unapproved_supps = [s for s in ro_doc.services if int(s.is_supplement or 0) == 1 and s.status != "Approved"]
			self.assertTrue(unapproved_supps, f"{ro_name} missing unapproved supplement")
			approved_base = sum(flt(s.total_amount) for s in ro_doc.services if s.status == "Approved")
			supp_total = sum(flt(s.total_amount) for s in unapproved_supps)
			self.assertGreater(supp_total, max(approved_base * 0.10, 20.0))
			self.assertTrue(
				frappe.db.exists("Shop Action Item", {"reference_doctype": "Repair Order", "reference_name": ro_name})
			)

		# Commercial fleet batch intake across veh_fleet_1..5
		fleet_ros = manifest.metadata["fleet_ros"]
		self.assertEqual(len(fleet_ros), 5)
		fleet_vehicles = {frappe.db.get_value("Repair Order", r, "vehicle") for r in fleet_ros}
		expected_fleet_vehicles = {manifest.resolve(f"vehicles.veh_fleet_{i}") for i in range(1, 6)}
		self.assertEqual(fleet_vehicles, expected_fleet_vehicles)

	def test_monte_carlo_determinism_across_rollbacks(self):
		"""Same seed produces identical logical SimManifest structure and counts across rollbacks."""
		m1 = monte_carlo(seed=7, anchor_date="2026-11-18", n_arrivals=10, commit=False)
		s1 = m1.metadata["monte_carlo_summary"]
		snapshot_1 = {
			"profile": m1.profile,
			"seed": m1.seed,
			"anchor_date": m1.anchor_date,
			"ros_keys": list(m1.ros.keys()),
			"se_keys": list(m1.schedule_entries.keys()),
			"po_keys": list(m1.pos.keys()),
			"rfq_keys": list(m1.rfqs.keys()),
			"si_keys": list(m1.sis.keys()),
			"pe_keys": list(m1.pes.keys()),
			"act_keys": list(m1.action_items.keys()),
			"leave_keys": list(m1.leave_applications.keys()),
			"target_date": s1["target_date"],
			"n_arrivals": s1["n_arrivals"],
			"entity_counts": dict(s1["entity_counts"]),
			"conflict_count": len(s1["conflict_ros"]),
			"wop_count": len(s1["waiting_on_parts_ros"]),
			"unsched_count": len(s1["unscheduled_ros"]),
			"over_tol_count": len(s1["over_tolerance_ros"]),
			"partial_count": len(s1["partially_approved_ros"]),
			"closed_count": len(s1["closed_ros"]),
			"walk_in_count": len(s1["walk_in_ros"]),
			"outage_bays": list(s1["outage_bays"]),
			"techs_on_leave": list(s1["techs_on_leave"]),
		}

		frappe.db.rollback()

		m2 = monte_carlo(seed=7, anchor_date="2026-11-18", n_arrivals=10, commit=False)
		s2 = m2.metadata["monte_carlo_summary"]
		snapshot_2 = {
			"profile": m2.profile,
			"seed": m2.seed,
			"anchor_date": m2.anchor_date,
			"ros_keys": list(m2.ros.keys()),
			"se_keys": list(m2.schedule_entries.keys()),
			"po_keys": list(m2.pos.keys()),
			"rfq_keys": list(m2.rfqs.keys()),
			"si_keys": list(m2.sis.keys()),
			"pe_keys": list(m2.pes.keys()),
			"act_keys": list(m2.action_items.keys()),
			"leave_keys": list(m2.leave_applications.keys()),
			"target_date": s2["target_date"],
			"n_arrivals": s2["n_arrivals"],
			"entity_counts": dict(s2["entity_counts"]),
			"conflict_count": len(s2["conflict_ros"]),
			"wop_count": len(s2["waiting_on_parts_ros"]),
			"unsched_count": len(s2["unscheduled_ros"]),
			"over_tol_count": len(s2["over_tolerance_ros"]),
			"partial_count": len(s2["partially_approved_ros"]),
			"closed_count": len(s2["closed_ros"]),
			"walk_in_count": len(s2["walk_in_ros"]),
			"outage_bays": list(s2["outage_bays"]),
			"techs_on_leave": list(s2["techs_on_leave"]),
		}

		self.assertEqual(snapshot_1, snapshot_2)

		seed_dict = seed_monte_carlo(seed=3, anchor_date="2026-11-18", commit=False, n_arrivals=5)
		self.assertEqual(seed_dict["profile"], "monte_carlo")
		self.assertEqual(seed_dict["metadata"]["monte_carlo_summary"]["n_arrivals"], 5)

	def test_monte_carlo_invariant_safety_across_seeds_1_to_10(self):
		"""Seeds 1..10 uphold bay non-overlap, bay restoration, waiting/over-tolerance invariants, and JSON roundtrip."""
		settings = get_shop_settings()
		holiday_fn = build_holiday_checker(settings.get("holiday_list"), "2026-10-01")

		for seed in range(1, 11):
			frappe.db.rollback()
			m = monte_carlo(seed=seed, anchor_date="2026-11-18", n_arrivals=10, commit=False)
			roundtrip = SimManifest.from_json(m.to_json())
			self.assertEqual(roundtrip.seed, seed)

			summary = m.metadata["monte_carlo_summary"]
			for required_key in (
				"target_date",
				"conflict_ros",
				"waiting_on_parts_ros",
				"unscheduled_ros",
				"over_tolerance_ros",
				"entity_counts",
			):
				self.assertIn(required_key, summary)

			self.assertEqual(summary["entity_counts"]["ros"], 10)
			self.assertEqual(len(m.ros), 10)

			# Canonical bay is_active states must be safely restored
			for bay_handle in ("main_lift", "alignment", "hv_diagnostics", "tire_quick"):
				self.assertEqual(int(frappe.db.get_value("Service Bay", m.resolve(f"bays.{bay_handle}"), "is_active")), 1)
			self.assertEqual(int(frappe.db.get_value("Service Bay", m.resolve("bays.offline"), "is_active")), 0)

			# No overlapping Schedule Entries in the same Service Bay (checked-out ROs complete their SE)
			by_bay: dict[str, list[tuple]] = {}
			closed_set = set(summary["closed_ros"])
			for se_name in m.schedule_entries.values():
				se = frappe.get_doc("Schedule Entry", se_name)
				if se.repair_order in closed_set:
					self.assertEqual(se.status, "Completed")
				else:
					self.assertNotIn(se.status, NON_BLOCKING_ENTRY_STATUSES)
				span = entry_datetimes(
					{
						"scheduled_date": se.scheduled_date,
						"scheduled_time": se.scheduled_time,
						"estimated_duration": se.estimated_duration,
						"completion_date": se.completion_date,
						"completion_time": se.completion_time,
					},
					is_holiday_fn=holiday_fn,
				)
				bay_spans = by_bay.setdefault(se.service_bay, [])
				for prev_start, prev_end in bay_spans:
					self.assertFalse(
						intervals_overlap(span[0], span[1], prev_start, prev_end),
						f"Seed {seed}: overlapping Schedule Entries in {se.service_bay}",
					)
				bay_spans.append(span)

			# Waiting on Parts invariant
			for ro_name in summary["waiting_on_parts_ros"]:
				self.assertEqual(frappe.db.get_value("Repair Order", ro_name, "status"), "Waiting on Parts")

			# Over-tolerance invariant: pending service + Shop Action Item
			for ro_name in summary["over_tolerance_ros"]:
				ro_doc = frappe.get_doc("Repair Order", ro_name)
				self.assertTrue(
					any(s.status in ("Estimating", "Draft", "Pending Approval") for s in ro_doc.services),
					f"Seed {seed}: over_tolerance_ro {ro_name} missing pending service",
				)
				self.assertTrue(
					frappe.db.exists("Shop Action Item", {"reference_doctype": "Repair Order", "reference_name": ro_name}),
					f"Seed {seed}: over_tolerance_ro {ro_name} missing Shop Action Item",
				)

			# Closed Sales Invoice invariant
			for si_name in m.sis.values():
				si_doc = frappe.get_doc("Sales Invoice", si_name)
				self.assertEqual(si_doc.docstatus, 1)
				self.assertGreaterEqual(flt(si_doc.outstanding_amount), -0.01)

	def test_warm_db_sequential_profile_and_monte_carlo_resets(self):
		"""Sequential profile and Monte Carlo builds on a warm DB cleanly reset prior Layer 3 state."""
		m_eval_1 = build_profile("eval_standard", seed=42, anchor_date="2026-11-18", commit=False)
		self.assertEqual(len(m_eval_1.metadata["historical_ros"]), 30)

		m_crunch = build_profile("swarm_crunch_day", seed=42, anchor_date="2026-11-18", commit=False)
		self.assertEqual(frappe.db.count("Leave Application", {"docstatus": 1}), 0)
		self.assertEqual(frappe.db.get_value("Repair Order", m_crunch.resolve("ros.crunch_fifo_winner"), "status"), "Authorized - In Progress")
		self.assertEqual(frappe.db.get_value("Repair Order", m_crunch.resolve("ros.crunch_fifo_loser"), "status"), "Waiting on Parts")

		m_eval_2 = build_profile("eval_standard", seed=42, anchor_date="2026-11-18", commit=False)
		self.assertEqual(len(m_eval_2.metadata["historical_ros"]), 30)
		self.assertEqual(frappe.db.count("Schedule Entry"), 2)

		m_mc1 = monte_carlo(seed=1, anchor_date="2026-11-18", n_arrivals=8, commit=False)
		self.assertEqual(len(m_mc1.ros), 8)
		m_mc2 = monte_carlo(seed=2, anchor_date="2026-11-18", n_arrivals=8, commit=False)
		self.assertEqual(len(m_mc2.ros), 8)
		self.assertEqual(frappe.db.count("Repair Order", {"name": ["not in", m_eval_2.metadata["historical_ros"]]}), 8)

		m_clean = build_profile("clean_master", seed=42, anchor_date="2026-11-18", commit=False)
		self.assertEqual(m_clean.profile, "clean_master")
		self.assertEqual(frappe.db.count("Repair Order"), 0)
		self.assertEqual(frappe.db.count("Schedule Entry"), 0)
		self.assertEqual(frappe.db.count("Leave Application"), 0)
		self.assertEqual(
			flt(frappe.db.get_value("Bin", {"item_code": m_clean.resolve("items.front_rotor_oem"), "warehouse": m_clean.resolve("warehouses.main_stores")}, "actual_qty")),
			2.0,
		)
