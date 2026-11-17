# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Layer 3 Deterministic Monte Carlo Simulation Generator."""

from __future__ import annotations

import datetime as _dt
import random
from typing import Any, Optional

import frappe
from frappe.utils import flt

from ism.api.scheduling import get_available_bays
from ism.api.technician_availability import get_technician_pool_availability
from ism.ism.doctype.shop_action_item.shop_action_item import create_shop_action_item
from ism.testing.builder import SimContext, _normalize_anchor_date
from ism.testing.catalog import PACKAGE_EQUIPMENT_TAGS
from ism.testing.manifest import SimManifest
from ism.testing.profiles import (
	_ensure_layer1_manifest,
	_ensure_submitted_leave,
	_parse_commit_flag,
	_reset_layer3_world,
)

_ACTIVE_TECH_HANDLES: tuple[str, ...] = ("marcus", "elena", "devon", "noah")
_ACTIVE_BAY_HANDLES: tuple[str, ...] = ("main_lift", "alignment", "hv_diagnostics", "tire_quick")
_ALL_BAY_HANDLES: tuple[str, ...] = ("main_lift", "alignment", "hv_diagnostics", "tire_quick", "offline")

_CUSTOMER_VEHICLE_POOL: tuple[tuple[str, str, int], ...] = (
	("retail_1", "veh_model3_liam", 48200),
	("retail_2", "veh_modely_sophia", 34100),
	("retail_3", "veh_models_owen", 27650),
	("household_primary", "veh_modelx_household", 52400),
	("household_secondary", "veh_model3_household", 19800),
	("fleet_1", "veh_fleet_1", 112400),
	("fleet_1", "veh_fleet_2", 87900),
	("fleet_1", "veh_fleet_3", 64300),
	("fleet_1", "veh_fleet_4", 94150),
	("fleet_1", "veh_fleet_5", 51200),
)

_ABUNDANT_PACKAGES: tuple[str, ...] = (
	"cabin_filter_service",
	"wiper_blade_service",
	"tire_rotation_balance",
	"four_wheel_alignment",
	"brake_fluid_flush",
	"hv_isolation_diagnostic",
)
_TIGHT_PACKAGES: tuple[str, ...] = ("front_brake_overhaul", "symmetrical_fore_link")
_ZERO_STOCK_PACKAGES: tuple[str, ...] = ("hv_pyro_fuse_replace", "rear_subframe_camber_sym")
_SLOT_TIMES: tuple[str, ...] = (
	"08:00:00",
	"09:00:00",
	"10:00:00",
	"11:00:00",
	"12:30:00",
	"13:30:00",
	"14:30:00",
	"15:30:00",
)


def _required_tags_for_packages(packages: list[str]) -> list[str]:
	tags: list[str] = ["Lift"]
	for pkg in packages:
		for tag in PACKAGE_EQUIPMENT_TAGS.get(pkg, ()):
			if tag not in tags:
				tags.append(tag)
	return tags


def _snapshot_bay_states(manifest: SimManifest) -> dict[str, int]:
	states: dict[str, int] = {}
	for handle in _ALL_BAY_HANDLES:
		bay_name = manifest.bays.get(handle)
		if bay_name and frappe.db.exists("Service Bay", bay_name):
			states[bay_name] = int(frappe.db.get_value("Service Bay", bay_name, "is_active") or 0)
	return states


def _restore_bay_states(states: dict[str, int]) -> None:
	for bay_name, is_active in states.items():
		if frappe.db.exists("Service Bay", bay_name):
			current = int(frappe.db.get_value("Service Bay", bay_name, "is_active") or 0)
			if current != int(is_active):
				frappe.db.set_value("Service Bay", bay_name, "is_active", int(is_active))


def _record_deposit_if_eligible(
	ctx: SimContext,
	ro: Any,
	rng: random.Random,
	seed: int,
	arrival_idx: int,
) -> None:
	"""Record an upfront deposit strictly below ro.grand_total so checkout balance stays positive."""
	grand_total = flt(ro.grand_total, 2)
	if grand_total < 30.0:
		return
	dep_amt = flt(round(max(15.0, min(60.0, grand_total * 0.30)), 2), 2)
	if dep_amt >= grand_total:
		return
	mop = rng.choice(("Debit / Interac", "Credit Card", "E-Transfer"))
	res = ro.record_deposit(
		amount=dep_amt,
		payment_method=mop,
		reference_no=f"DEP-MC-{seed}-{arrival_idx}",
	)
	pe_name = res.get("payment_entry")
	if pe_name:
		pe_handle = ctx.manifest.next_handle("pes", "pe")
		ctx.manifest.register("pes", pe_handle, pe_name)
		ctx.track("Payment Entry", pe_name)
	ro.reload()


def _find_slot_and_conflict(
	ctx: SimContext,
	target_date: str,
	packages: list[str],
	rng: random.Random,
	techs_on_leave: list[str],
	occupied_tech_slots: set[tuple[str, str]],
) -> tuple[Optional[str], Optional[str], Optional[str], bool]:
	"""Choose a valid bay/time slot on target_date and detect bay/tech conflicts."""
	req_tags = _required_tags_for_packages(packages)
	candidate_slots = list(_SLOT_TIMES)
	rng.shuffle(candidate_slots)

	had_conflict = False
	chosen_bay: Optional[str] = None
	chosen_time: Optional[str] = None

	for idx, slot_time in enumerate(candidate_slots):
		avail_bays = get_available_bays(
			date=target_date,
			start_time=slot_time,
			duration_minutes=60,
			required_tags=req_tags,
		)
		pool = get_technician_pool_availability(
			date=target_date,
			start_time=slot_time,
			duration_minutes=60,
		)
		if avail_bays and pool.get("is_available"):
			# Pick deterministically among currently free capable bays; note if first random slot was congested.
			sorted_bays = sorted(avail_bays, key=lambda b: b["bay_name"])
			bay_idx = rng.randrange(len(sorted_bays))
			chosen_bay = sorted_bays[bay_idx]["bay_name"]
			chosen_time = slot_time
			if idx > 0:
				had_conflict = True
			break
		had_conflict = True

	if not chosen_bay or not chosen_time:
		return None, None, None, had_conflict

	on_duty_handles = [t for t in _ACTIVE_TECH_HANDLES if t not in techs_on_leave]
	free_handles = [t for t in on_duty_handles if (t, chosen_time) not in occupied_tech_slots]

	if techs_on_leave and rng.random() < 0.30:
		chosen_tech_handle = rng.choice(techs_on_leave)
		had_conflict = True
	elif free_handles:
		chosen_tech_handle = rng.choice(free_handles)
	elif on_duty_handles:
		chosen_tech_handle = rng.choice(on_duty_handles)
		had_conflict = True
	else:
		chosen_tech_handle = rng.choice(_ACTIVE_TECH_HANDLES)
		had_conflict = True

	occupied_tech_slots.add((chosen_tech_handle, chosen_time))
	return chosen_bay, chosen_time, chosen_tech_handle, had_conflict


def _apply_supplement_or_breach(
	ctx: SimContext,
	ro: Any,
	arrival_idx: int,
	wants_supplement: bool,
	wants_breach: bool,
	rng: random.Random,
) -> bool:
	"""Apply supplement / cost-tolerance breach; return True if RO is held over tolerance."""
	if not wants_supplement and not wants_breach:
		return False

	m = ctx.manifest
	if wants_supplement and not wants_breach:
		ro.add_supplement(
			service_title=f"Minor Hardware & Clip Replacement #{arrival_idx}",
			labor_hours=0.25,
			notes="Approved minor supplement within tolerance.",
		)
		ro.reload()
		last_row = ro.services[-1]
		ro.authorize_jobs(
			approvals={str(last_row.idx): "Approved"},
			channel="Phone / Verbal",
			signer_name=ro.customer,
			notes="Customer verbally approved minor supplement.",
		)
		ro.reload()
		return False

	# Over-tolerance breach (> max(10%, $20)) -> either direct row overrun or unapproved supplement + Action Item.
	non_flat_approved = [s for s in ro.services if s.status == "Approved" and not int(s.is_flat_rate or 0)]
	if not wants_supplement and len(ro.services) >= 2 and non_flat_approved:
		target_row = non_flat_approved[0]
		target_row.labor_hours = flt(target_row.labor_hours) + 1.0
		ro.save(ignore_permissions=True)
		ro.reload()
		act_title = f"Cost tolerance overrun on {ro.name}: {target_row.service_title}"
		act_details = "Additional labor hours pushed approved job above 10% / $20 cost tolerance."
	else:
		supp_hours = round(rng.uniform(1.5, 2.2), 2)
		ro.add_supplement(
			service_title=f"Over-Tolerance Discovery Repair #{arrival_idx}",
			labor_hours=supp_hours,
			notes="Unapproved supplemental work exceeding 10% / $20 threshold.",
		)
		ro.reload()
		act_title = f"Unapproved over-tolerance supplement on {ro.name}"
		act_details = f"Supplemental estimate ({supp_hours}h) exceeds max(10%, $20) cost tolerance."

	act_name = create_shop_action_item(
		title=act_title,
		category="Shop Floor Discrepancy",
		priority="High",
		reference_doctype="Repair Order",
		reference_name=ro.name,
		details=act_details,
	)
	act_handle = m.next_handle("action_items", "mc_action")
	m.register("action_items", act_handle, act_name)
	ctx.track("Shop Action Item", act_name)
	return True


def _checkout_ro(
	ctx: SimContext,
	ro: Any,
	odo_in: int,
	wants_split_tender: bool,
	rng: random.Random,
	seed: int,
	arrival_idx: int,
) -> None:
	"""Complete services, park vehicle, and check out with single or split tender."""
	for s in ro.services:
		if s.status == "Approved" and not s.is_completed:
			ro.toggle_service_completion(service_row_id=s.name, is_completed=1)
	ro.reload()
	ro.park_vehicle()
	ro.reload()

	remaining = flt(ro.outstanding_amount, 2)
	odo_out = int(odo_in) + 12

	if wants_split_tender and remaining >= 2.0:
		part1 = flt(round(remaining * 0.55, 2), 2)
		part2 = flt(remaining - part1, 2)
		payments = [
			{
				"payment_method": "Debit / Interac",
				"amount": part1,
				"reference_no": f"MC-SPLIT-A-{seed}-{arrival_idx}",
			},
			{
				"payment_method": "Credit Card",
				"amount": part2,
				"reference_no": f"MC-SPLIT-B-{seed}-{arrival_idx}",
			},
		]
		res = ro.process_payment_and_release(odometer_out=odo_out, payments=payments)
	else:
		mop = rng.choice(("Debit / Interac", "Credit Card", "E-Transfer", "Cash"))
		res = ro.process_payment_and_release(payment_method=mop, odometer_out=odo_out)

	si_name = res.get("sales_invoice")
	if si_name:
		si_handle = ctx.manifest.next_handle("sis", "mc_si")
		ctx.manifest.register("sis", si_handle, si_name)
		ctx.track("Sales Invoice", si_name)
	for pe_name in (res.get("payment_entries") or []):
		pe_handle = ctx.manifest.next_handle("pes", "mc_pe")
		ctx.manifest.register("pes", pe_handle, pe_name)
		ctx.track("Payment Entry", pe_name)
	ro.reload()


def monte_carlo(
	seed: int,
	*,
	anchor_date: Optional[str] = None,
	n_arrivals: Optional[int] = None,
	walk_in_prob: float = 0.35,
	stockout_prob: float = 0.25,
	supplement_prob: float = 0.30,
	tolerance_breach_prob: float = 0.40,
	tech_leave_prob: float = 0.20,
	bay_outage_prob: float = 0.15,
	partial_decline_prob: float = 0.20,
	deposit_prob: float = 0.35,
	split_tender_prob: float = 0.30,
	commit: bool = False,
) -> SimManifest:
	"""Generate a deterministic seeded simulation world exercising ISM edge conditions."""
	resolved_seed = int(seed)
	rng = random.Random(resolved_seed)
	resolved_anchor = _normalize_anchor_date(anchor_date).isoformat()
	base_manifest = _ensure_layer1_manifest(anchor_date=resolved_anchor, force_seed=False)
	_reset_layer3_world(base_manifest, resolved_anchor, keep_historical=True)
	base_manifest.profile = "monte_carlo"
	ctx = SimContext(seed=resolved_seed, anchor_date=base_manifest.anchor_date, manifest=base_manifest)
	m = ctx.manifest

	target_date = ctx.next_open_day(1)
	resolved_arrivals = int(n_arrivals) if n_arrivals is not None else rng.randint(8, 12)

	techs_on_leave: list[str] = []
	for tech_handle in _ACTIVE_TECH_HANDLES:
		if rng.random() < float(tech_leave_prob) and len(techs_on_leave) < 2:
			lt_handle = rng.choice(("privilege", "sick", "casual"))
			_ensure_submitted_leave(
				ctx,
				tech_handle,
				target_date,
				leave_type_handle=lt_handle,
				handle=f"mc_{tech_handle}_leave",
			)
			techs_on_leave.append(tech_handle)

	original_bay_states = _snapshot_bay_states(m)
	outage_bays: list[str] = []

	conflict_ros: list[str] = []
	waiting_on_parts_ros: list[str] = []
	unscheduled_ros: list[str] = []
	over_tolerance_ros: list[str] = []
	partially_approved_ros: list[str] = []
	closed_ros: list[str] = []
	walk_in_ros: list[str] = []
	occupied_tech_slots: set[tuple[str, str]] = set()

	try:
		for bay_handle in _ACTIVE_BAY_HANDLES:
			if rng.random() < float(bay_outage_prob) and len(outage_bays) < 2:
				bay_name = m.bays.get(bay_handle)
				if bay_name and frappe.db.exists("Service Bay", bay_name):
					frappe.db.set_value("Service Bay", bay_name, "is_active", 0)
					outage_bays.append(bay_handle)

		for idx in range(resolved_arrivals):
			arrival_num = idx + 1
			cust_handle, veh_handle, base_odo = rng.choice(_CUSTOMER_VEHICLE_POOL)
			odo_in = base_odo + arrival_num * 15
			ro_handle = f"mc_ro_{arrival_num}"

			is_walk_in = rng.random() < float(walk_in_prob)
			wants_stockout = rng.random() < float(stockout_prob)
			wants_partial_decline = rng.random() < float(partial_decline_prob)
			wants_deposit = rng.random() < float(deposit_prob)
			wants_supplement = rng.random() < float(supplement_prob)
			wants_breach = rng.random() < float(tolerance_breach_prob)
			wants_split_tender = rng.random() < float(split_tender_prob)
			wants_checkout = rng.random() < 0.45

			is_zero_stock = False
			if wants_stockout:
				if rng.random() < 0.55:
					packages = [rng.choice(_ZERO_STOCK_PACKAGES)]
					is_zero_stock = True
				else:
					packages = [rng.choice(_TIGHT_PACKAGES)]
			else:
				first_pkg = rng.choice(_ABUNDANT_PACKAGES)
				packages = [first_pkg]
				if wants_partial_decline:
					other_choices = [p for p in _ABUNDANT_PACKAGES if p != first_pkg]
					packages.append(rng.choice(other_choices))

			if is_zero_stock:
				builder = ctx.ro(ro_handle).for_customer(
					cust_handle,
					veh_handle,
					odometer_in=odo_in,
					concern=f"Monte Carlo zero-stock service arrival #{arrival_num}.",
				)
				builder.with_package(packages[0])
				if packages[0] == "hv_pyro_fuse_replace":
					builder.with_procurement(
						order_mode="Purchase Order",
						supplier="tesla_oem",
						payment_method="Bank Draft",
						reference_no=f"BD-MC-{resolved_seed}-{arrival_num}",
					)
				else:
					builder.with_procurement(order_mode="Request for Quotation")
				ro = builder.build("waiting_on_parts")
				if wants_deposit:
					_record_deposit_if_eligible(ctx, ro, rng, resolved_seed, arrival_num)
				waiting_on_parts_ros.append(ro.name)
				unscheduled_ros.append(ro.name)
				continue

			if wants_partial_decline and len(packages) >= 2:
				builder = ctx.ro(ro_handle).for_customer(
					cust_handle,
					veh_handle,
					odometer_in=odo_in,
					concern=f"Monte Carlo multi-service recommendation #{arrival_num}.",
				)
				for pkg in packages:
					builder.with_package(pkg)
				ro = builder.with_partial_approval(
					approved_indices=(1,),
					declined_indices=(2,),
					safety_hazard_declines=(2,),
				).build("partially_approved_with_deferred")
				if wants_deposit:
					_record_deposit_if_eligible(ctx, ro, rng, resolved_seed, arrival_num)
				partially_approved_ros.append(ro.name)
				unscheduled_ros.append(ro.name)
				continue

			builder = ctx.ro(ro_handle).for_customer(
				cust_handle,
				veh_handle,
				odometer_in=odo_in,
				concern=f"Monte Carlo service arrival #{arrival_num} ({packages[0]}).",
			)
			for pkg in packages:
				builder.with_package(pkg)
			ro = builder.build("authorized_in_stock")
			if wants_deposit:
				_record_deposit_if_eligible(ctx, ro, rng, resolved_seed, arrival_num)

			if ro.status == "Waiting on Parts":
				waiting_on_parts_ros.append(ro.name)
				unscheduled_ros.append(ro.name)
				continue

			if is_walk_in:
				walk_in_ros.append(ro.name)
				unscheduled_ros.append(ro.name)
				continue

			bay_name, slot_time, tech_handle, had_conflict = _find_slot_and_conflict(
				ctx=ctx,
				target_date=target_date,
				packages=packages,
				rng=rng,
				techs_on_leave=techs_on_leave,
				occupied_tech_slots=occupied_tech_slots,
			)
			if not bay_name or not slot_time or not tech_handle:
				unscheduled_ros.append(ro.name)
				continue

			tech_name = ctx.resolve("techs", tech_handle)
			se = frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Repair",
				"status": "In Progress",
				"scheduled_date": target_date,
				"scheduled_time": slot_time,
				"estimated_duration": 60,
				"repair_order": ro.name,
				"service_bay": bay_name,
				"assigned_technician": tech_name,
			}).insert(ignore_permissions=True)
			m.register("schedule_entries", f"mc_se_{arrival_num}", se.name)
			ctx.track(se)

			if had_conflict:
				conflict_ros.append(ro.name)

			ro.reload()
			ro.pick_up_vehicle()
			ro.reload()

			start_dt = _dt.datetime.fromisoformat(f"{target_date} {slot_time}")
			end_dt = start_dt + _dt.timedelta(hours=1)
			ro.append("time_logs", {
				"technician": tech_name,
				"from_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
				"to_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
				"time_in_hours": 1.0,
				"notes": f"Monte Carlo bay execution #{arrival_num}",
			})
			ro.save(ignore_permissions=True)
			ro.reload()

			is_over_tolerance = _apply_supplement_or_breach(
				ctx=ctx,
				ro=ro,
				arrival_idx=arrival_num,
				wants_supplement=wants_supplement,
				wants_breach=wants_breach,
				rng=rng,
			)
			if is_over_tolerance:
				over_tolerance_ros.append(ro.name)
				continue

			if wants_checkout:
				_checkout_ro(
					ctx=ctx,
					ro=ro,
					odo_in=odo_in,
					wants_split_tender=wants_split_tender,
					rng=rng,
					seed=resolved_seed,
					arrival_idx=arrival_num,
				)
				closed_ros.append(ro.name)
	finally:
		_restore_bay_states(original_bay_states)

	m.metadata["target_date"] = target_date
	m.metadata["bay_outages"] = list(outage_bays)
	m.metadata["monte_carlo_summary"] = {
		"seed": resolved_seed,
		"target_date": target_date,
		"n_arrivals": resolved_arrivals,
		"conflict_ros": conflict_ros,
		"waiting_on_parts_ros": waiting_on_parts_ros,
		"unscheduled_ros": unscheduled_ros,
		"over_tolerance_ros": over_tolerance_ros,
		"partially_approved_ros": partially_approved_ros,
		"closed_ros": closed_ros,
		"walk_in_ros": walk_in_ros,
		"outage_bays": outage_bays,
		"techs_on_leave": techs_on_leave,
		"entity_counts": {
			"ros": len(m.ros),
			"schedule_entries": len(m.schedule_entries),
			"pos": len(m.pos),
			"rfqs": len(m.rfqs),
			"pis": len(m.pis),
			"sis": len(m.sis),
			"pes": len(m.pes),
			"action_items": len(m.action_items),
			"leave_applications": len(m.leave_applications),
		},
	}

	if _parse_commit_flag(commit):
		frappe.db.commit()
	return m


def seed_monte_carlo(
	seed: int = 1,
	anchor_date: Optional[str] = None,
	commit: bool = True,
	**params: Any,
) -> dict[str, Any]:
	"""Bench-executable Monte Carlo wrapper returning `SimManifest.to_dict()`."""
	parsed_params: dict[str, Any] = dict(params)
	if "n_arrivals" in parsed_params and parsed_params["n_arrivals"] is not None:
		parsed_params["n_arrivals"] = int(parsed_params["n_arrivals"])
	for prob_key in (
		"walk_in_prob",
		"stockout_prob",
		"supplement_prob",
		"tolerance_breach_prob",
		"tech_leave_prob",
		"bay_outage_prob",
		"partial_decline_prob",
		"deposit_prob",
		"split_tender_prob",
	):
		if prob_key in parsed_params and parsed_params[prob_key] is not None:
			parsed_params[prob_key] = float(parsed_params[prob_key])

	manifest = monte_carlo(
		seed=int(seed),
		anchor_date=anchor_date,
		commit=_parse_commit_flag(commit),
		**parsed_params,
	)
	return manifest.to_dict()
