# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Layer 3 World Profiles for repeatable ISM evaluations and agent testing."""

from __future__ import annotations

import datetime as _dt
import random
from typing import Any, Optional

import frappe
from frappe.core.doctype.user.user import generate_keys
from frappe.utils import flt

from ism.testing.builder import SimContext, _normalize_anchor_date, seed_historical_telemetry
from ism.testing.catalog import (
	CANONICAL_COMPANY,
	CANONICAL_MAIN_WAREHOUSE,
	CATALOG_SEED,
	_PART_SPECS,
	get_master_manifest,
	seed_catalog_manifest,
	seed_inventory_and_parts,
)
from ism.testing.manifest import SimManifest

VALID_PROFILES: tuple[str, ...] = ("clean_master", "eval_standard", "swarm_crunch_day")

_ACTIVE_BAY_HANDLES: tuple[str, ...] = ("main_lift", "alignment", "hv_diagnostics", "tire_quick")
_DAY_CAPACITY_MINUTES = 510


def _parse_commit_flag(commit: Any) -> bool:
	if isinstance(commit, str):
		return commit.strip().lower() in ("1", "true", "yes", "on")
	return bool(commit)


def _ensure_canonical_bay_states(manifest: SimManifest) -> None:
	"""Ensure Layer 1 bays reflect their canonical active/offline states."""
	for handle in _ACTIVE_BAY_HANDLES:
		bay_name = manifest.bays.get(handle)
		if bay_name and frappe.db.exists("Service Bay", bay_name):
			if int(frappe.db.get_value("Service Bay", bay_name, "is_active") or 0) != 1:
				frappe.db.set_value("Service Bay", bay_name, "is_active", 1)
	offline_bay = manifest.bays.get("offline")
	if offline_bay and frappe.db.exists("Service Bay", offline_bay):
		if int(frappe.db.get_value("Service Bay", offline_bay, "is_active") or 0) != 0:
			frappe.db.set_value("Service Bay", offline_bay, "is_active", 0)


def _ensure_layer1_manifest(anchor_date: Optional[str] = None, force_seed: bool = False) -> SimManifest:
	"""Return a Layer 1 SimManifest, seeding canonical masters if absent or requested."""
	if force_seed or not frappe.db.exists("Company", CANONICAL_COMPANY):
		return seed_catalog_manifest(seed=CATALOG_SEED, anchor_date=anchor_date, commit=False)
	manifest = get_master_manifest(seed=CATALOG_SEED, anchor_date=anchor_date)
	_ensure_canonical_bay_states(manifest)
	return manifest


def _cancel_and_delete_doc(doctype: str, name: str) -> None:
	"""Cancel (if submitted) and forcefully delete a Layer 3 document."""
	if not frappe.db.exists(doctype, name):
		return
	doc = frappe.get_doc(doctype, name)
	if getattr(doc, "docstatus", 0) == 1:
		doc.cancel()
	frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)


def _find_preserved_historical_ros(anchor_date: str, max_count: int = 30) -> set[str]:
	"""Return up to `max_count` closed historical telemetry ROs in T-60d..T-1d to preserve."""
	anchor_dt = _normalize_anchor_date(anchor_date)
	start_str = (anchor_dt - _dt.timedelta(days=60)).isoformat()
	end_str = (anchor_dt - _dt.timedelta(days=1)).isoformat()
	candidates = frappe.get_all(
		"Repair Order",
		filters={
			"company": CANONICAL_COMPANY,
			"status": "Closed / Released",
			"posting_date": ["between", [start_str, end_str]],
			"sales_invoice": ["is", "not set"],
		},
		order_by="name asc",
		pluck="name",
		limit=int(max_count),
	)
	return set(candidates)


def _resync_opening_inventory_if_drifted(manifest: SimManifest) -> None:
	"""Reconcile Main Shop Stores Bin quantities back to canonical Layer 1 bands if drifted."""
	main_wh = manifest.warehouses.get("main_stores") or CANONICAL_MAIN_WAREHOUSE
	drifted = False
	for spec in _PART_SPECS:
		item_code = spec["item_code"]
		if not frappe.db.exists("Item", item_code):
			drifted = True
			break
		current_qty = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": main_wh}, "actual_qty"))
		if abs(current_qty - float(spec["stock_qty"])) > 1e-6:
			drifted = True
			break
	if drifted:
		seed_inventory_and_parts(manifest, random.Random(CATALOG_SEED))


def _clear_rq_queues() -> None:
	"""Empty stale RQ background queues on non-worker bench slots to prevent QueueOverloaded."""
	if getattr(frappe.flags, "in_test", False):
		return
	try:
		from frappe.utils.background_jobs import get_queues

		for queue in get_queues():
			queue.empty()
	except Exception:
		pass


def _reset_layer3_world(manifest: SimManifest, anchor_date: str, *, keep_historical: bool = True) -> None:
	"""Cleanly tear down prior Layer 3 transactional docs, restore bays, and resync opening stock."""
	_clear_rq_queues()
	preserved_ros = _find_preserved_historical_ros(anchor_date) if keep_historical else set()

	ro_filters: dict[str, Any] = {"name": ["not in", list(preserved_ros)]} if preserved_ros else {}
	target_ros = frappe.get_all("Repair Order", filters=ro_filters, pluck="name")

	for ro_name in target_ros:
		if frappe.db.get_value("Repair Order", ro_name, "sales_invoice"):
			frappe.db.set_value("Repair Order", ro_name, "sales_invoice", None, update_modified=False)

	if frappe.db.exists("DocType", "Vehicle Deferred Service"):
		if preserved_ros:
			frappe.db.sql(
				"DELETE FROM `tabVehicle Deferred Service` "
				"WHERE COALESCE(repair_order, '') != '' AND repair_order NOT IN %s",
				(tuple(preserved_ros),),
			)
		else:
			frappe.db.sql(
				"DELETE FROM `tabVehicle Deferred Service` WHERE COALESCE(repair_order, '') != ''"
			)

	for doctype in (
		"Schedule Entry",
		"Shop Action Item",
		"Payment Entry",
		"Purchase Receipt",
		"Purchase Invoice",
		"Sales Invoice",
		"Stock Entry",
		"Purchase Order",
		"Request for Quotation",
		"Leave Application",
	):
		if not frappe.db.exists("DocType", doctype):
			continue
		for name in frappe.get_all(doctype, pluck="name"):
			_cancel_and_delete_doc(doctype, name)

	for ro_name in target_ros:
		_cancel_and_delete_doc("Repair Order", ro_name)

	_ensure_canonical_bay_states(manifest)
	_resync_opening_inventory_if_drifted(manifest)


def _ensure_historical_telemetry(ctx: SimContext, n_closed_ros: int = 30) -> list[str]:
	"""Idempotently ensure at least `n_closed_ros` closed historical ROs exist across T-60d..T-1d."""
	target_count = int(n_closed_ros)
	existing = frappe.get_all(
		"Repair Order",
		filters={
			"company": ctx.manifest.company,
			"status": "Closed / Released",
			"posting_date": ["between", [ctx.day(-60), ctx.day(-1)]],
		},
		order_by="name asc",
		pluck="name",
		limit=target_count,
	)

	if len(existing) >= target_count:
		selected = list(existing[:target_count])
		for idx, ro_name in enumerate(selected, start=1):
			ctx.manifest.register("ros", f"hist_{idx}", ro_name)
		ctx.manifest.metadata["historical_ros"] = list(selected)
		return selected

	if not existing:
		return seed_historical_telemetry(ctx, n_closed_ros=target_count)

	remaining = target_count - len(existing)
	extra = seed_historical_telemetry(ctx, n_closed_ros=remaining)
	combined = list(existing) + list(extra)
	for idx, ro_name in enumerate(combined, start=1):
		ctx.manifest.register("ros", f"hist_{idx}", ro_name)
	ctx.manifest.metadata["historical_ros"] = list(combined)
	return combined


def _ensure_submitted_leave(
	ctx: SimContext,
	tech_handle: str,
	leave_date: str,
	leave_type_handle: str = "privilege",
	handle: str = "elena_tomorrow",
) -> str:
	"""Create and submit an approved Leave Application on `leave_date`, reusing if already present."""
	emp_id = ctx.resolve("techs", tech_handle)
	existing_name = frappe.db.get_value(
		"Leave Application",
		{
			"employee": emp_id,
			"docstatus": 1,
			"status": "Approved",
			"from_date": ["<=", leave_date],
			"to_date": [">=", leave_date],
		},
		"name",
	)
	if existing_name:
		ctx.manifest.register("leave_applications", handle, existing_name)
		return existing_name

	leave_doc = frappe.get_doc({
		"doctype": "Leave Application",
		"employee": emp_id,
		"leave_type": ctx.resolve("leave_types", leave_type_handle),
		"from_date": leave_date,
		"to_date": leave_date,
		"status": "Approved",
		"company": ctx.manifest.company,
	})
	leave_doc.insert(ignore_permissions=True)
	leave_doc.submit()
	ctx.track(leave_doc)
	ctx.manifest.register("leave_applications", handle, leave_doc.name)
	return leave_doc.name


def _build_eval_standard(ctx: SimContext) -> SimManifest:
	"""Populate the `eval_standard` evaluation scenario."""
	m = ctx.manifest
	target_date = ctx.next_open_day(1)
	m.metadata["target_date"] = target_date
	m.metadata["historical_ro_count"] = 30

	_ensure_historical_telemetry(ctx, n_closed_ros=30)
	_ensure_submitted_leave(ctx, "elena", target_date, "privilege", "elena_tomorrow")

	# Primary conflict booking on target_date 09:00:00 (90m) in main_lift with marcus
	(
		ctx.ro("conflict_ro")
		.for_customer("fleet_1", "veh_fleet_1", odometer_in=112420, concern="Scheduled brake fluid thermal service.")
		.with_package("brake_fluid_flush")
		.with_schedule(
			date=target_date,
			start_time="09:00:00",
			duration_minutes=90,
			bay="main_lift",
			technician="marcus",
			entry_type="Repair",
			status="Scheduled",
			handle="conflict_main_lift",
		)
		.with_time_log(technician="marcus", hours=0.5, notes="Pre-inspection and fluid setup")
		.build("scheduled_in_progress")
	)

	# Secondary scheduled booking on target_date 10:00:00 (60m) in hv_diagnostics with noah
	(
		ctx.ro("hv_scheduled_ro")
		.for_customer("fleet_1", "veh_fleet_2", odometer_in=87910, concern="HV isolation resistance check.")
		.with_package("hv_isolation_diagnostic")
		.with_schedule(
			date=target_date,
			start_time="10:00:00",
			duration_minutes=60,
			bay="hv_diagnostics",
			technician="noah",
			entry_type="Diagnostic",
			status="Scheduled",
			handle="hv_morning",
		)
		.with_time_log(technician="noah", hours=0.5, notes="HV isolation test lead connection")
		.build("scheduled_in_progress")
	)

	# Two unscheduled authorized_in_stock ROs
	(
		ctx.ro("unscheduled_ro")
		.for_customer("retail_1", "veh_model3_liam", odometer_in=48250, concern="Cabin HEPA filter and tire rotation.")
		.with_package("cabin_filter_service")
		.with_package("tire_rotation_balance")
		.build("authorized_in_stock")
	)

	(
		ctx.ro("unscheduled_secondary_ro")
		.for_customer("retail_2", "veh_modely_sophia", odometer_in=34120, concern="DOT 4 brake fluid flush.")
		.with_package("brake_fluid_flush")
		.build("authorized_in_stock")
	)

	# Open PO + submitted Bank Draft advance PE (waiting_on_parts, unreceived)
	(
		ctx.ro("open_po_ro")
		.for_customer("retail_3", "veh_models_owen", odometer_in=27680, concern="Pyro fuse fault code replacement.")
		.with_package("hv_pyro_fuse_replace")
		.with_procurement(
			order_mode="Purchase Order",
			supplier="tesla_oem",
			payment_method="Bank Draft",
			reference_no=f"BD-EVAL-{ctx.seed}",
			po_handle="eval_open_po",
			pe_handle="eval_open_po_advance",
		)
		.build("waiting_on_parts")
	)

	# Zero-stock RFQ waiting_on_parts RO
	(
		ctx.ro("zero_stock_ro")
		.for_customer(
			"household_primary",
			"veh_modelx_household",
			odometer_in=52440,
			concern="Rear camber link bushing play on LH side.",
		)
		.with_package("rear_subframe_camber_sym")
		.with_procurement(
			order_mode="Request for Quotation",
			rfq_handle="eval_open_rfq",
		)
		.build("waiting_on_parts")
	)

	return m


def _build_swarm_crunch_day(ctx: SimContext) -> SimManifest:
	"""Populate the `swarm_crunch_day` high-utilization multi-agent stress profile."""
	m = ctx.manifest
	target_date = ctx.next_open_day(1)
	m.metadata["target_date"] = target_date
	m.metadata["historical_ro_count"] = 30

	_ensure_historical_telemetry(ctx, n_closed_ros=30)

	# 4 active bays x 435 min scheduled / 510 min open capacity = 85.29% utilization.
	# Each bay pairs with one dedicated on-duty technician (4 active techs) to avoid pool exhaustion.
	schedule_plan: tuple[dict[str, Any], ...] = (
		# Bay 1 - Main Lift (marcus): 120 + 120 + 195 = 435m
		{
			"ro_handle": "crunch_fifo_winner",
			"se_handle": "crunch_main_1",
			"customer": "retail_2",
			"vehicle": "veh_modely_sophia",
			"odometer_in": 34200,
			"packages": ("front_brake_overhaul",),
			"bay": "main_lift",
			"tech": "marcus",
			"start_time": "08:00:00",
			"duration": 120,
			"is_fleet": False,
		},
		{
			"ro_handle": "crunch_fleet_1",
			"se_handle": "crunch_main_2",
			"customer": "fleet_1",
			"vehicle": "veh_fleet_1",
			"odometer_in": 112450,
			"packages": ("brake_fluid_flush",),
			"bay": "main_lift",
			"tech": "marcus",
			"start_time": "10:00:00",
			"duration": 120,
			"is_fleet": True,
			"supplement": {
				"title": "Replace Seized Front Caliper Slide Pins & Hardware",
				"labor_hours": 1.5,
				"notes": "Caliper guide pins seized; exceeds 10% / $20 cost tolerance.",
			},
			"action_item": {
				"handle": "crunch_over_tol_1",
				"title": "Over-tolerance caliper hardware supplement requires authorization",
				"details": "Supplemental caliper slide pin replacement ($210.00) exceeds cost tolerance.",
			},
		},
		{
			"ro_handle": "crunch_main_pm",
			"se_handle": "crunch_main_3",
			"customer": "retail_1",
			"vehicle": "veh_model3_liam",
			"odometer_in": 48300,
			"packages": ("brake_fluid_flush", "cabin_filter_service"),
			"bay": "main_lift",
			"tech": "marcus",
			"start_time": "12:30:00",
			"duration": 195,
			"is_fleet": False,
		},
		# Bay 2 - Alignment & Suspension (elena): 120 + 120 + 195 = 435m
		{
			"ro_handle": "crunch_fleet_2",
			"se_handle": "crunch_align_1",
			"customer": "fleet_1",
			"vehicle": "veh_fleet_2",
			"odometer_in": 87940,
			"packages": ("four_wheel_alignment", "cabin_filter_service"),
			"bay": "alignment",
			"tech": "elena",
			"start_time": "08:00:00",
			"duration": 120,
			"is_fleet": True,
		},
		{
			"ro_handle": "crunch_align_supp",
			"se_handle": "crunch_align_2",
			"customer": "retail_3",
			"vehicle": "veh_models_owen",
			"odometer_in": 27700,
			"packages": ("four_wheel_alignment",),
			"bay": "alignment",
			"tech": "elena",
			"start_time": "10:00:00",
			"duration": 120,
			"is_fleet": False,
			"supplement": {
				"title": "Rear Camber Eccentric Bolt Extraction & Replacement",
				"labor_hours": 1.75,
				"notes": "Seized eccentric camber bolt requires torch extraction and replacement.",
			},
			"action_item": {
				"handle": "crunch_over_tol_2",
				"title": "Seized camber eccentric bolt supplement exceeds tolerance",
				"details": "Supplemental extraction labor ($245.00) exceeds 10% / $20 tolerance.",
			},
		},
		{
			"ro_handle": "crunch_fleet_3",
			"se_handle": "crunch_align_3",
			"customer": "fleet_1",
			"vehicle": "veh_fleet_3",
			"odometer_in": 64340,
			"packages": ("four_wheel_alignment", "brake_fluid_flush"),
			"bay": "alignment",
			"tech": "elena",
			"start_time": "12:30:00",
			"duration": 195,
			"is_fleet": True,
		},
		# Bay 3 - HV & Diagnostics (noah): 120 + 120 + 195 = 435m
		{
			"ro_handle": "crunch_hv_1",
			"se_handle": "crunch_hv_1",
			"customer": "household_primary",
			"vehicle": "veh_modelx_household",
			"odometer_in": 52460,
			"packages": ("hv_isolation_diagnostic", "brake_fluid_flush"),
			"bay": "hv_diagnostics",
			"tech": "noah",
			"start_time": "08:00:00",
			"duration": 120,
			"is_fleet": False,
		},
		{
			"ro_handle": "crunch_fleet_4",
			"se_handle": "crunch_hv_2",
			"customer": "fleet_1",
			"vehicle": "veh_fleet_4",
			"odometer_in": 94180,
			"packages": ("hv_isolation_diagnostic", "cabin_filter_service"),
			"bay": "hv_diagnostics",
			"tech": "noah",
			"start_time": "10:00:00",
			"duration": 120,
			"is_fleet": True,
		},
		{
			"ro_handle": "crunch_hv_2",
			"se_handle": "crunch_hv_3",
			"customer": "household_primary",
			"vehicle": "veh_model3_household",
			"odometer_in": 19840,
			"packages": ("hv_isolation_diagnostic", "brake_fluid_flush"),
			"bay": "hv_diagnostics",
			"tech": "noah",
			"start_time": "12:30:00",
			"duration": 195,
			"is_fleet": False,
		},
		# Bay 4 - Tire & Quick Service (devon): 120 + 120 + 195 = 435m
		{
			"ro_handle": "crunch_fleet_5",
			"se_handle": "crunch_tire_1",
			"customer": "fleet_1",
			"vehicle": "veh_fleet_5",
			"odometer_in": 51230,
			"packages": ("tire_rotation_balance", "wiper_blade_service"),
			"bay": "tire_quick",
			"tech": "devon",
			"start_time": "08:00:00",
			"duration": 120,
			"is_fleet": True,
		},
		{
			"ro_handle": "crunch_tire_2",
			"se_handle": "crunch_tire_2",
			"customer": "retail_1",
			"vehicle": "veh_model3_liam",
			"odometer_in": 48320,
			"packages": ("tire_rotation_balance", "cabin_filter_service"),
			"bay": "tire_quick",
			"tech": "devon",
			"start_time": "10:00:00",
			"duration": 120,
			"is_fleet": False,
		},
		{
			"ro_handle": "crunch_tire_3",
			"se_handle": "crunch_tire_3",
			"customer": "retail_2",
			"vehicle": "veh_modely_sophia",
			"odometer_in": 34220,
			"packages": ("tire_rotation_balance", "wiper_blade_service"),
			"bay": "tire_quick",
			"tech": "devon",
			"start_time": "12:30:00",
			"duration": 195,
			"is_fleet": False,
		},
	)

	fleet_ros: list[str] = []
	over_tolerance_ros: list[str] = []
	total_scheduled_minutes = 0

	for slot in schedule_plan:
		builder = ctx.ro(slot["ro_handle"]).for_customer(
			slot["customer"],
			slot["vehicle"],
			odometer_in=slot["odometer_in"],
			concern=f"Swarm crunch day scheduled service ({', '.join(slot['packages'])}).",
		)
		for pkg in slot["packages"]:
			builder.with_package(pkg)
		builder.with_schedule(
			date=target_date,
			start_time=slot["start_time"],
			duration_minutes=slot["duration"],
			bay=slot["bay"],
			technician=slot["tech"],
			entry_type="Repair",
			status="In Progress",
			handle=slot["se_handle"],
		)
		builder.with_time_log(technician=slot["tech"], hours=1.0, notes="Crunch day bay execution")
		if slot.get("supplement"):
			supp = slot["supplement"]
			builder.with_supplement(
				service_title=supp["title"],
				labor_hours=supp["labor_hours"],
				notes=supp["notes"],
				approve=False,
			)
		if slot.get("action_item"):
			act = slot["action_item"]
			builder.with_action_item(
				title=act["title"],
				category="Shop Floor Discrepancy",
				priority="High",
				details=act["details"],
				handle=act["handle"],
			)
		ro = builder.build("scheduled_in_progress")
		total_scheduled_minutes += int(slot["duration"])
		if slot.get("is_fleet"):
			fleet_ros.append(ro.name)
		if slot.get("supplement"):
			over_tolerance_ros.append(ro.name)

	# Tight-band FIFO loser: front_rotor_oem (qty=2) was claimed by crunch_fifo_winner.
	ro_loser = (
		ctx.ro("crunch_fifo_loser")
		.for_customer(
			"retail_3",
			"veh_models_owen",
			odometer_in=27720,
			concern="Front brake rotor and pad overhaul (FIFO contention loser).",
		)
		.with_package("front_brake_overhaul")
		.build("authorized_in_stock")
	)

	total_capacity_minutes = len(_ACTIVE_BAY_HANDLES) * _DAY_CAPACITY_MINUTES
	m.metadata["bay_utilization_pct"] = round((total_scheduled_minutes / total_capacity_minutes) * 100.0, 2)
	m.metadata["scheduled_minutes_total"] = total_scheduled_minutes
	m.metadata["bay_capacity_minutes"] = total_capacity_minutes
	m.metadata["fifo_winner_ro"] = m.resolve("ros.crunch_fifo_winner")
	m.metadata["fifo_loser_ro"] = ro_loser.name
	m.metadata["fleet_ros"] = fleet_ros
	m.metadata["over_tolerance_ros"] = over_tolerance_ros
	return m


def build_profile(
	profile: str = "eval_standard",
	*,
	seed: int = 42,
	anchor_date: Optional[str] = None,
	commit: bool = False,
) -> SimManifest:
	"""Build and return a populated SimManifest for one of `VALID_PROFILES`."""
	if profile not in VALID_PROFILES:
		raise ValueError(f"Unsupported profile '{profile}'. Expected one of {VALID_PROFILES}.")

	commit_flag = _parse_commit_flag(commit)
	resolved_anchor = _normalize_anchor_date(anchor_date).isoformat()
	base_manifest = _ensure_layer1_manifest(anchor_date=resolved_anchor, force_seed=False)
	_reset_layer3_world(
		base_manifest,
		resolved_anchor,
		keep_historical=(profile != "clean_master"),
	)
	if profile == "clean_master":
		base_manifest = _ensure_layer1_manifest(anchor_date=resolved_anchor, force_seed=True)
	base_manifest.profile = profile
	ctx = SimContext(seed=int(seed), anchor_date=base_manifest.anchor_date, manifest=base_manifest)

	if profile == "eval_standard":
		_build_eval_standard(ctx)
	elif profile == "swarm_crunch_day":
		_build_swarm_crunch_day(ctx)

	if commit_flag:
		frappe.db.commit()
		_clear_rq_queues()
	return ctx.manifest


def seed_profile(
	profile: str = "eval_standard",
	seed: int = 42,
	anchor_date: Optional[str] = None,
	commit: bool = True,
) -> dict[str, Any]:
	"""Bench-executable entry point returning `SimManifest.to_dict()` without spurious stdout."""
	manifest = build_profile(
		profile=profile,
		seed=int(seed),
		anchor_date=anchor_date,
		commit=_parse_commit_flag(commit),
	)
	return manifest.to_dict()


def seed_clean_master() -> dict[str, Any]:
	"""Seed canonical Layer 1 masters only and return `SimManifest.to_dict()`."""
	return seed_profile("clean_master", commit=True)


def seed_eval_standard() -> dict[str, Any]:
	"""Seed the `eval_standard` profile and return `SimManifest.to_dict()`."""
	return seed_profile("eval_standard", commit=True)


def seed_swarm_crunch_day() -> dict[str, Any]:
	"""Seed the `swarm_crunch_day` profile and return `SimManifest.to_dict()`."""
	return seed_profile("swarm_crunch_day", commit=True)


def provision_agent_api_keys(user: str = "manager@lightspeedev.ca") -> dict[str, Any]:
	"""Generate and commit Frappe API keys for `user` and return connection credentials."""
	if not frappe.db.exists("User", user):
		frappe.throw(frappe._("User '{0}' does not exist.").format(user), frappe.DoesNotExistError)

	_clear_rq_queues()
	keys = generate_keys(user)
	api_key = keys.get("api_key") or frappe.db.get_value("User", user, "api_key")
	api_secret = keys.get("api_secret")
	frappe.db.commit()
	_clear_rq_queues()

	return {
		"user": user,
		"api_key": api_key,
		"api_secret": api_secret,
		"company": CANONICAL_COMPANY,
	}
