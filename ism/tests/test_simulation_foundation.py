# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Stage 1 Gate Test Suite verifying Layer 1 Master Catalog, Layer 2 SimBuilder, and SimManifest."""

from __future__ import annotations

import json

import frappe
from frappe.utils import flt

from ism.api.estimation_service import estimate_repair_order, get_estimate
from ism.api.parts import get_component_alternates
from ism.api.parts_stock import get_default_warehouse
from ism.api.technician_availability import get_active_technicians
from ism.testing import (
	SimManifest,
	compute_catalog_part_price,
	seed_all,
	seed_historical_telemetry,
)
from ism.tests.base import ISMTestCase


class TestSimulationFoundation(ISMTestCase):
	"""End-to-end verification of clean site masters, SimBuilder lifecycle stages, and SimManifest."""

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_clean_reinstall_hygiene_and_accounting_defaults(self):
		"""Clean site holds only canonical Lightspeed EV masters, 13% HST, price lists, and 6 payment modes."""
		companies = frappe.get_all("Company", pluck="name")
		self.assertEqual(companies, ["Lightspeed EV"])
		self.assertEqual(frappe.db.get_value("Company", "Lightspeed EV", "default_currency"), "CAD")
		self.assertEqual(frappe.db.get_value("Company", "Lightspeed EV", "country"), "Canada")

		self.assertEqual(frappe.get_all("Service Bay", filters={"name": ["like", "Test %"]}, pluck="name"), [])
		self.assertEqual(frappe.get_all("Employee", filters={"employee_name": ["like", "Test %"]}, pluck="name"), [])
		self.assertEqual(frappe.get_all("Customer", filters={"name": ["like", "Test %"]}, pluck="name"), [])
		self.assertFalse(hasattr(ISMTestCase, "create_test_quotation"))

		manifest = self.get_manifest()
		self.assertTrue(frappe.db.exists("Price List", manifest.resolve("price_lists.selling")))
		self.assertTrue(frappe.db.exists("Price List", manifest.resolve("price_lists.buying")))
		self.assertEqual(frappe.db.get_value("Price List", "Standard Selling", "currency"), "CAD")

		sales_tax = manifest.resolve("tax_templates.sales_hst_13")
		purchase_tax = manifest.resolve("tax_templates.purchase_hst_13")
		self.assertEqual(sales_tax, "13% HST - LEV")
		self.assertEqual(purchase_tax, "13% HST - LEV")
		self.assertTrue(frappe.db.exists("Sales Taxes and Charges Template", sales_tax))
		self.assertTrue(frappe.db.exists("Purchase Taxes and Charges Template", purchase_tax))
		self.assertTrue(frappe.db.exists("Tax Rule", {"company": "Lightspeed EV", "tax_type": "Sales"}))
		self.assertTrue(frappe.db.exists("Tax Rule", {"company": "Lightspeed EV", "tax_type": "Purchase"}))

		for mop in ("Debit / Interac", "Credit Card", "E-Transfer", "Cash", "Bank Draft", "Wire Transfer"):
			self.assertEqual(int(frappe.db.get_value("Mode of Payment", mop, "enabled") or 0), 1)
			acct = frappe.db.get_value(
				"Mode of Payment Account",
				{"parent": mop, "company": "Lightspeed EV"},
				"default_account",
			)
			self.assertTrue(acct and frappe.db.exists("Account", acct), f"Missing GL account on {mop}")

		settings = frappe.get_single("ISM Settings")
		self.assertEqual(str(settings.operating_hours_start), "08:00:00")
		self.assertEqual(str(settings.operating_hours_end), "17:00:00")
		self.assertEqual(str(settings.break_start), "12:00:00")
		self.assertEqual(str(settings.break_end), "12:30:00")
		self.assertEqual(int(settings.default_slot_interval), 30)
		self.assertAlmostEqual(flt(settings.estimation_sigma), 0.30, places=2)
		self.assertAlmostEqual(flt(settings.default_labor_rate), 140.0, places=2)
		self.assertAlmostEqual(flt(settings.cost_tolerance_percentage), 10.0, places=2)
		self.assertAlmostEqual(flt(settings.cost_tolerance_flat_amount), 20.0, places=2)
		self.assertAlmostEqual(flt(settings.shop_supplies_percentage), 0.0, places=2)

	def test_workforce_rbac_and_real_hrms_leave_submission(self):
		"""Seeded personas enforce permlevel=1 locks, active techs filter Left, and real Leave submits."""
		manifest = self.get_manifest()
		for handle in ("manager", "advisor", "parts", "tech"):
			self.assertTrue(frappe.db.exists("User", manifest.resolve(f"users.{handle}")))

		ctx = self.sim_context()
		ro = ctx.ro("rbac_ro").for_customer("retail_1", "veh_model3_liam").build("draft_estimate")

		frappe.set_user(manifest.resolve("users.tech"))
		ro_tech = frappe.get_doc("Repair Order", ro.name)
		self.assertIn(1, ro_tech.get_permlevel_access("read"))
		self.assertNotIn(1, ro_tech.get_permlevel_access("write"))
		self.assertFalse(ro_tech.has_permlevel_access_to("discount_amount", permission_type="write"))

		frappe.set_user(manifest.resolve("users.advisor"))
		ro_adv = frappe.get_doc("Repair Order", ro.name)
		self.assertIn(1, ro_adv.get_permlevel_access("write"))
		self.assertTrue(ro_adv.has_permlevel_access_to("discount_amount", permission_type="write"))

		frappe.set_user("Administrator")
		active_techs = get_active_technicians()
		active_names = {t["employee_name"] for t in active_techs}
		self.assertEqual(active_names, {"Marcus Vance", "Elena Rostova", "Devon Brooks", "Noah Patel"})

		# Submit a real Leave Application without ignore_validate to verify Holiday List + Leave Allocation.
		elena_id = manifest.resolve("techs.elena")
		leave_date = ctx.next_open_day(1)
		leave = frappe.get_doc({
			"doctype": "Leave Application",
			"employee": elena_id,
			"leave_type": manifest.resolve("leave_types.privilege"),
			"from_date": leave_date,
			"to_date": leave_date,
			"status": "Approved",
			"company": manifest.company,
		})
		leave.insert(ignore_permissions=True)
		leave.submit()
		self.track(leave)

		day_techs = {t["employee"]: t for t in get_active_technicians(date=leave_date)}
		self.assertTrue(day_techs[elena_id]["on_leave"])
		self.assertFalse(day_techs[manifest.resolve("techs.marcus")]["on_leave"])

	def test_shop_floor_inventory_bands_and_pricing_rules(self):
		"""Shop floor equipment/bays, warehouses, suppliers, stock bands, and PARTS_PRICING math match."""
		manifest = self.get_manifest()
		self.assertEqual(len(manifest.equipment_tags), 6)
		self.assertEqual(len(manifest.bays), 5)
		self.assertEqual(int(frappe.db.get_value("Service Bay", manifest.resolve("bays.offline"), "is_active")), 0)
		self.assertEqual(len(manifest.schedule_entry_types), 5)

		main_wh = manifest.resolve("warehouses.main_stores")
		self.assertEqual(get_default_warehouse(manifest.company), main_wh)
		self.assertEqual(len(manifest.warehouses), 3)
		self.assertEqual(len(manifest.suppliers), 4)

		abundant_qty = flt(
			frappe.db.get_value(
				"Bin",
				{"item_code": manifest.resolve("items.dot4_brake_fluid"), "warehouse": main_wh},
				"actual_qty",
			)
		)
		tight_qty = flt(
			frappe.db.get_value(
				"Bin",
				{"item_code": manifest.resolve("items.front_rotor_oem"), "warehouse": main_wh},
				"actual_qty",
			)
		)
		zero_qty = flt(
			frappe.db.get_value(
				"Bin",
				{"item_code": manifest.resolve("items.hv_pyro_fuse_oem"), "warehouse": main_wh},
				"actual_qty",
			)
		)
		self.assertGreaterEqual(abundant_qty, 16.0)
		self.assertEqual(tight_qty, 2.0)
		self.assertEqual(zero_qty, 0.0)

		# Verify PARTS_PRICING.md baseline calculation parity
		self.assertEqual(compute_catalog_part_price(40.0, 200.0), 140.0)
		self.assertEqual(compute_catalog_part_price(90.0, 100.0), 112.5)
		self.assertEqual(compute_catalog_part_price(4.0, 10.0), 10.0)
		self.assertEqual(compute_catalog_part_price(9.0, 10.0), 14.0)
		self.assertEqual(compute_catalog_part_price(1.0, 3.0), 6.0)
		self.assertEqual(compute_catalog_part_price(25.0, None), 35.0)

	def test_nested_packages_customers_vehicles_and_deferred_work(self):
		"""Symmetrical LH/RH, multi-slot alternates, flat/mobile packages, multi-owner & fleet VINs work."""
		manifest = self.get_manifest()
		ctx = self.sim_context()

		# Multi-owner household & commercial fleet checks
		hh_veh = frappe.get_doc("Repair Vehicle", manifest.resolve("vehicles.veh_modelx_household"))
		self.assertEqual(int(hh_veh.is_valid_vin), 1)
		self.assertEqual(len(hh_veh.customers), 2)
		self.assertEqual(
			{r.customer for r in hh_veh.customers},
			{manifest.resolve("customers.household_primary"), manifest.resolve("customers.household_secondary")},
		)

		fleet_vehs = frappe.get_all(
			"Repair Vehicle",
			filters={"customer": manifest.resolve("customers.fleet_1")},
			fields=["name", "is_valid_vin", "model"],
		)
		self.assertEqual(len(fleet_vehs), 5)
		self.assertTrue(all(int(v.is_valid_vin) == 1 for v in fleet_vehs))

		liam_veh = frappe.get_doc("Repair Vehicle", manifest.resolve("vehicles.veh_model3_liam"))
		self.assertTrue(any(int(d.safety_hazard) == 1 and d.status == "Open Recommendation" for d in liam_veh.deferred_services))

		# Symmetrical package single-side + opposite-side duplication + side='both'
		ro_sym = ctx.ro("sym_single").for_customer("retail_1", "veh_model3_liam").with_package("symmetrical_fore_link").build("draft_estimate")
		self.assertEqual(len(ro_sym.services), 1)
		self.assertEqual(ro_sym.services[0].service_position, "LH")
		self.assertEqual(ro_sym.services[0].correction_code, "31011802")

		ro_sym.duplicate_service_for_opposite_side(service_row_id=ro_sym.services[0].name)
		ro_sym.reload()
		self.assertEqual(len(ro_sym.services), 2)
		self.assertEqual(ro_sym.services[1].service_position, "RH")
		self.assertEqual(ro_sym.services[1].correction_code, "31011902")

		# Multi-slot overhaul & alternates lookup
		ro_both = (
			ctx.ro("sym_both")
			.for_customer("retail_2", "veh_modely_sophia")
			.with_package("front_brake_overhaul")
			.with_package("four_wheel_alignment")
			.with_package("cabin_filter_service")
			.build("draft_estimate")
		)
		self.assertEqual(len(ro_both.services), 3)
		self.assertEqual(int(ro_both.services[1].is_flat_rate), 1)
		self.assertAlmostEqual(flt(ro_both.services[1].labor_amount), 169.0, places=2)
		self.assertEqual(int(ro_both.services[2].is_mobile_capable), 1)

		alts = get_component_alternates(
			service_package=manifest.resolve("packages.front_brake_overhaul"),
			slot_number=2,
		)
		alt_codes = {row.get("item_code") for row in (alts or [])}
		self.assertIn(manifest.resolve("items.front_rotor_oem"), alt_codes)
		self.assertIn(manifest.resolve("items.front_rotor_aft"), alt_codes)

	def test_sim_builder_lifecycle_stages_and_fifo_contention(self):
		"""SimBuilder transitions through all RO stages, FIFO contention, procurement, and billing."""
		ctx = self.sim_context(seed=77, anchor_date="2026-11-18")
		self.assertEqual(ctx.today, "2026-11-18")
		self.assertEqual(ctx.tomorrow, "2026-11-19")
		self.assertEqual(ctx.rel_date("T-7d"), "2026-11-11")
		self.assertEqual(ctx.rel_date("T+3d"), "2026-11-21")

		ro_chk = ctx.ro("ro_chk").for_customer("retail_1", "veh_model3_liam").with_inspection("walkaround").build("checked_in")
		self.assertEqual(ro_chk.status, "Checked In")
		self.assertGreaterEqual(len(ro_chk.inspection_items), 9)

		ro_pend = ctx.ro("ro_pend").for_customer("retail_1", "veh_model3_liam").with_package("wiper_blade_service").build("pending_authorization")
		self.assertEqual(ro_pend.status, "Pending Authorization")
		self.assertTrue(bool(ro_pend.approval_token))
		self.assertTrue(bool(ro_pend.approval_token_expiry))

		# Tight-band FIFO contention (front_rotor_oem has qty=2; overhaul consumes qty=2)
		ro_fifo_1 = (
			ctx.ro("fifo_winner")
			.for_customer("retail_2", "veh_modely_sophia")
			.with_package("front_brake_overhaul")
			.build("authorized_in_stock")
		)
		self.assertEqual(ro_fifo_1.status, "Authorized - In Progress")

		ro_fifo_2 = (
			ctx.ro("fifo_loser")
			.for_customer("fleet_1", "veh_fleet_1")
			.with_package("front_brake_overhaul")
			.build("authorized_in_stock")
		)
		self.assertEqual(ro_fifo_2.status, "Waiting on Parts")

		# Receive via Purchase Invoice (update_stock=True) to clear Waiting on Parts before subsequent FIFO demand
		ro_recv = (
			ctx.ro("ro_recv")
			.for_customer("retail_1", "veh_model3_liam")
			.with_package("hv_pyro_fuse_replace")
			.with_procurement(receive_via_pi=True, bill_no="INV-STAGE1-RECV", pi_handle="recv_pi")
			.with_schedule(date=ctx.day(3), start_time="10:00:00", bay="hv_diagnostics", technician="noah")
			.build("scheduled_in_progress")
		)
		self.assertEqual(ro_recv.status, "Authorized - In Progress")
		self.assertEqual(int(frappe.db.get_value("Purchase Invoice", ctx.manifest.resolve("pis.recv_pi"), "docstatus")), 1)

		# Waiting on Parts + PO + Bank Draft advance PE + RFQ variant
		ro_wop = (
			ctx.ro("ro_wop")
			.for_customer("retail_3", "veh_models_owen")
			.with_package("hv_pyro_fuse_replace")
			.with_procurement(
				order_mode="Purchase Order",
				payment_method="Bank Draft",
				reference_no="BD-STAGE1-01",
				po_handle="wop_po",
				pe_handle="wop_pe",
			)
			.build("waiting_on_parts")
		)
		self.assertEqual(ro_wop.status, "Waiting on Parts")
		self.assertEqual(int(frappe.db.get_value("Purchase Order", ctx.manifest.resolve("pos.wop_po"), "docstatus")), 1)
		self.assertEqual(int(frappe.db.get_value("Payment Entry", ctx.manifest.resolve("pes.wop_pe"), "docstatus")), 1)

		ro_rfq = (
			ctx.ro("ro_rfq")
			.for_customer("household_primary", "veh_modelx_household")
			.with_package("rear_subframe_camber_sym")
			.with_procurement(order_mode="Request for Quotation", rfq_handle="wop_rfq")
			.build("waiting_on_parts")
		)
		self.assertEqual(ro_rfq.status, "Waiting on Parts")
		self.assertEqual(int(frappe.db.get_value("Request for Quotation", ctx.manifest.resolve("rfqs.wop_rfq"), "docstatus")), 1)

		# Partial approval with safety-hazard deferred sync
		ro_part = (
			ctx.ro("ro_part")
			.for_customer("retail_2", "veh_modely_sophia")
			.with_package("cabin_filter_service")
			.with_package("four_wheel_alignment")
			.with_partial_approval(approved_indices=(1,), declined_indices=(2,), safety_hazard_declines=(2,))
			.build("partially_approved_with_deferred")
		)
		self.assertEqual(ro_part.approval_status, "Partially Approved")
		veh_sophia = frappe.get_doc("Repair Vehicle", ctx.manifest.resolve("vehicles.veh_modely_sophia"))
		deferred_row = next((d for d in veh_sophia.deferred_services if d.repair_order == ro_part.name), None)
		self.assertIsNotNone(deferred_row)
		self.assertEqual(int(deferred_row.safety_hazard), 1)

		# Scheduled in progress + time log + supplement + action item
		ro_wip = (
			ctx.ro("ro_wip")
			.for_customer("fleet_1", "veh_fleet_2")
			.with_package("brake_fluid_flush")
			.with_schedule(date="tomorrow", start_time="09:00:00", bay="main_lift", technician="marcus", handle="wip_se")
			.with_time_log(technician="marcus", hours=0.75)
			.with_supplement("Caliper Slide Pin Lubrication", labor_hours=0.5, approve=False)
			.with_action_item("Bleeder screw seized on caliper", handle="wip_act")
			.build("scheduled_in_progress")
		)
		self.assertEqual(ro_wip.status, "Authorized - In Progress")
		self.assertEqual(ro_wip.vehicle_location, "In Bay / Being Serviced")
		self.assertEqual(len(ro_wip.time_logs), 1)
		self.assertEqual(len(ro_wip.services), 2)
		self.assertTrue(frappe.db.exists("Schedule Entry", ctx.manifest.resolve("schedule_entries.wip_se")))
		self.assertTrue(frappe.db.exists("Shop Action Item", ctx.manifest.resolve("action_items.wip_act")))

		# Full lifecycle through closed_released with upfront deposit + checkout
		ro_closed = (
			ctx.ro("ro_closed")
			.for_customer("fleet_1", "veh_fleet_3", odometer_in=64310)
			.with_package("tire_rotation_balance")
			.with_deposit(30.0, payment_method="Debit / Interac", reference_no="DEP-STAGE1", handle="closed_dep")
			.with_schedule(date=ctx.day(2), start_time="10:30:00", bay="tire_quick", technician="devon")
			.with_checkout(payment_method="Credit Card", odometer_out=64325, si_handle="closed_si", pe_handle="closed_pe")
			.build("closed_released")
		)
		self.assertEqual(ro_closed.status, "Closed / Released")
		self.assertEqual(ro_closed.payment_status, "Paid")
		self.assertEqual(int(ro_closed.vehicle_released), 1)
		self.assertEqual(int(ro_closed.odometer_out), 64325)
		si = frappe.get_doc("Sales Invoice", ctx.manifest.resolve("sis.closed_si"))
		self.assertEqual(si.docstatus, 1)
		self.assertAlmostEqual(flt(si.outstanding_amount), 0.0, places=2)

	def test_historical_telemetry_empirical_estimation_manifest_roundtrip_and_idempotency(self):
		"""Historical telemetry feeds empirical P80, SimManifest round-trips JSON, and seed_all is idempotent."""
		ctx = self.sim_context(seed=99, anchor_date="2026-11-18")
		hist_ros = seed_historical_telemetry(ctx, n_closed_ros=30)
		self.assertEqual(len(hist_ros), 30)

		sample_hist = frappe.get_doc("Repair Order", hist_ros[0])
		self.assertEqual(sample_hist.status, "Closed / Released")
		self.assertGreater(flt(sample_hist.services[0].actual_duration_hours), 0.0)

		pkg_name = ctx.manifest.resolve("packages.cabin_filter_service")
		emp_res = get_estimate(
			item_code=ctx.manifest.resolve("items.cabin_hepa_filter_oem"),
			service_package=pkg_name,
			empirical=True,
		)
		self.assertTrue(emp_res.get("empirical_used"))
		self.assertGreaterEqual(int(emp_res.get("sample_count", 0)), 3)

		ro_probe = (
			ctx.ro("emp_probe")
			.for_customer("retail_1", "veh_model3_liam")
			.with_package("cabin_filter_service")
			.build("draft_estimate")
		)
		ro_analytic = estimate_repair_order(ro_probe.name)
		ro_empirical = estimate_repair_order(ro_probe.name, empirical=True)
		self.assertNotIn("empirical_used", ro_analytic)
		self.assertTrue(ro_empirical.get("empirical_used"))
		self.assertGreaterEqual(ro_empirical["empirical_sample_counts"].get(pkg_name, 0), 3)

		# SimManifest JSON round-trip and interpolation
		raw_json = ctx.manifest.to_json()
		restored = SimManifest.from_json(raw_json)
		self.assertEqual(restored.resolve("ros.emp_probe"), ro_probe.name)
		self.assertEqual(
			restored.interpolate("Inspect {{ros.emp_probe}} in {manifest:bays.main_lift}"),
			f"Inspect {ro_probe.name} in Bay 1 - Main Lift",
		)

		# Idempotency check on seed_all(commit=False)
		main_wh = ctx.manifest.resolve("warehouses.main_stores")
		rotor_item = ctx.manifest.resolve("items.front_rotor_oem")
		before_qty = flt(frappe.db.get_value("Bin", {"item_code": rotor_item, "warehouse": main_wh}, "actual_qty"))
		second = seed_all(commit=False)
		after_qty = flt(frappe.db.get_value("Bin", {"item_code": rotor_item, "warehouse": main_wh}, "actual_qty"))
		self.assertEqual(before_qty, after_qty)
		self.assertEqual(second["company"], "Lightspeed EV")
		self.assertEqual(len(second["packages"]), 10)
