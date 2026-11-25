# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Optional Capability Pack step: Website Self-Serve Booking (/book)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from ism.comms.providers import get_sms_unconfigured_reason
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_INFO,
	CHECK_PASS,
	CHECK_WARN,
	PROFILE_PRODUCTION,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_WARNING,
	TIER_OPTIONAL_PACK,
	CheckResult,
	SetupContext,
	StepInspection,
	StepMeta,
	StepVerifyResult,
)
from ism.setup_wizard.settings_helper import SettingsSectionStep


class SelfServeBookingStep(SettingsSectionStep):
	"""Configure and gate-check the hosted /book self-serve booking funnel."""

	meta = StepMeta(
		id="self_serve_booking",
		title="Website Self-Serve Booking (/book)",
		phase="integrations",
		order=130,
		schema_version=1,
		tier=TIER_OPTIONAL_PACK,
		depends_on=(
			"workshop_bays_and_bins",
			"catalog_inspections_suppliers",
			"comms_telnyx_sms",
			"customer_portal",
		),
		owner_domain="scheduling",
		managed_settings_sections=("sb_customer_scheduling_policy", "sb_self_serve_booking"),
		allow_skip=True,
		description=(
			"Configure customer scheduling policies and enable the /book self-serve booking funnel "
			"with a 5-point operational readiness gate."
		),
	)

	def _desk_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"book_url": f"{ctx.site_url}/book",
			"service_packages": f"{ctx.site_url}/app/service-package",
			"ism_settings": f"{ctx.site_url}/app/ism-settings",
		}

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		schema = super().get_schema(ctx)
		schema["desk_urls"] = self._desk_urls(ctx)
		return schema

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "standard_self_serve",
				"label": "Standard Self-Serve Booking Policy",
				"description": (
					"Enables /book with 15-min arrival offset, 24h reschedule cutoff, 2-hour lead time, "
					"30-day horizon, and anti-bot honeypot enabled."
				),
				"payload": {
					"settings": {
						"enable_self_serve_booking": 1,
						"customer_journey_demo_mode": 0,
						"customer_arrival_offset_minutes": 15,
						"reschedule_cutoff_hours": 24,
						"max_self_reschedules": 2,
						"max_concurrent_bookings_per_phone": 2,
						"self_serve_min_lead_time_minutes": 120,
						"self_serve_horizon_days": 30,
						"enable_waiter_preference": 1,
						"self_serve_max_packages_per_booking": 5,
						"self_serve_honeypot_enabled": 1,
					}
				},
			}
		]

	def _evaluate_readiness_gate(
		self,
		settings: Any,
		ctx: SetupContext,
	) -> tuple[list[CheckResult], dict[str, Any]]:
		checks: list[CheckResult] = []

		# Gate 1: SMS gateway configured
		sms_reason = get_sms_unconfigured_reason(settings)
		if not sms_reason:
			checks.append(
				CheckResult(
					code="gate_1_sms_configured",
					level=CHECK_PASS,
					message="Gate 1/5: Outbound SMS gateway is configured for booking OTP verification.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="gate_1_sms_configured",
					level=CHECK_FAIL,
					message=f"Gate 1/5: SMS gateway is not configured ({sms_reason}).",
					remediation="Configure the 'comms_telnyx_sms' step before enabling self-serve booking.",
				)
			)

		# Gate 2: >= 1 Service Package with allow_self_serve_booking == 1
		bookable_packages = (
			frappe.db.count("Service Package", {"allow_self_serve_booking": 1})
			if frappe.db.exists("DocType", "Service Package")
			else 0
		)
		if bookable_packages >= 1:
			checks.append(
				CheckResult(
					code="gate_2_bookable_packages",
					level=CHECK_PASS,
					message=f"Gate 2/5: {bookable_packages} bookable Service Package(s) enabled.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="gate_2_bookable_packages",
					level=CHECK_FAIL,
					message="Gate 2/5: No Service Package has allow_self_serve_booking enabled.",
					remediation="Enable 'Allow Self-Serve Booking' on at least one Service Package.",
				)
			)

		# Gate 3: >= 1 active Service Bay
		active_bays = (
			frappe.db.count("Service Bay", {"is_active": 1})
			if frappe.db.exists("DocType", "Service Bay")
			else 0
		)
		if active_bays >= 1:
			checks.append(
				CheckResult(
					code="gate_3_active_bays",
					level=CHECK_PASS,
					message=f"Gate 3/5: {active_bays} active Service Bay(s) configured.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="gate_3_active_bays",
					level=CHECK_FAIL,
					message="Gate 3/5: No active Service Bay found.",
					remediation="Create and activate at least one Service Bay in workshop_bays_and_bins.",
				)
			)

		# Gate 4: >= 1 active Technician (when enable_technician_capacity == 1)
		tech_capacity_enabled = bool(cint(settings.get("enable_technician_capacity")))
		tech_designation = str(settings.get("technician_designation") or "Technician").strip()
		active_techs = (
			frappe.db.count("Employee", {"designation": tech_designation, "status": "Active"})
			if frappe.db.exists("DocType", "Employee")
			else 0
		)
		if not tech_capacity_enabled:
			checks.append(
				CheckResult(
					code="gate_4_active_technicians",
					level=CHECK_INFO,
					message="Gate 4/5: Technician capacity check bypassed (enable_technician_capacity is off).",
				)
			)
		elif active_techs >= 1:
			checks.append(
				CheckResult(
					code="gate_4_active_technicians",
					level=CHECK_PASS,
					message=(
						f"Gate 4/5: {active_techs} active Technician(s) with designation "
						f"'{tech_designation}'."
					),
				)
			)
		else:
			checks.append(
				CheckResult(
					code="gate_4_active_technicians",
					level=CHECK_FAIL,
					message=(
						f"Gate 4/5: No active Employee found with designation '{tech_designation}' "
						"while technician capacity is enabled."
					),
					remediation=(
						f"Create at least one active Employee with designation '{tech_designation}' "
						"in users_roles_employees."
					),
				)
			)

		# Gate 5: self_serve_default_entry_type set and requires_repair_order == 0
		entry_type_name = str(settings.get("self_serve_default_entry_type") or "").strip()
		entry_type_ok = False
		if not entry_type_name:
			checks.append(
				CheckResult(
					code="gate_5_default_entry_type",
					level=CHECK_FAIL,
					message="Gate 5/5: self_serve_default_entry_type is not set.",
					remediation=(
						"Set self_serve_default_entry_type to a Schedule Entry Type with "
						"requires_repair_order = 0."
					),
				)
			)
		else:
			entry_type = (
				frappe.db.get_value(
					"Schedule Entry Type",
					entry_type_name,
					["name", "requires_repair_order"],
					as_dict=True,
				)
				if frappe.db.exists("DocType", "Schedule Entry Type")
				else None
			)
			if not entry_type:
				checks.append(
					CheckResult(
						code="gate_5_default_entry_type",
						level=CHECK_FAIL,
						message=(
							f"Gate 5/5: Configured Schedule Entry Type '{entry_type_name}' does not exist."
						),
						remediation="Select an existing Schedule Entry Type with requires_repair_order = 0.",
					)
				)
			elif cint(entry_type.get("requires_repair_order")) != 0:
				checks.append(
					CheckResult(
						code="gate_5_default_entry_type",
						level=CHECK_FAIL,
						message=(
							f"Gate 5/5: Schedule Entry Type '{entry_type_name}' has "
							"requires_repair_order=1 and cannot be used for provisional self-serve bookings."
						),
						remediation="Select a Schedule Entry Type with requires_repair_order = 0.",
					)
				)
			else:
				entry_type_ok = True
				checks.append(
					CheckResult(
						code="gate_5_default_entry_type",
						level=CHECK_PASS,
						message=(
							f"Gate 5/5: Default entry type '{entry_type_name}' is valid "
							"(requires_repair_order=0)."
						),
					)
				)

		# Demo mode safety check
		demo_mode = bool(cint(settings.get("customer_journey_demo_mode")))
		if demo_mode:
			if ctx.profile == PROFILE_PRODUCTION:
				checks.append(
					CheckResult(
						code="self_serve_demo_mode_production",
						level=CHECK_FAIL,
						message=(
							"customer_journey_demo_mode is enabled in Production! "
							"Demo mode bypasses SMS OTP and parts-stock guards."
						),
						remediation="Disable customer_journey_demo_mode immediately for Production.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="self_serve_demo_mode_staging",
						level=CHECK_WARN,
						message="customer_journey_demo_mode is enabled (Staging walkthrough mode).",
						remediation="Ensure customer_journey_demo_mode is disabled before Production go-live.",
					)
				)

		counts = {
			"enabled": bool(cint(settings.get("enable_self_serve_booking"))),
			"demo_mode": demo_mode,
			"bookable_packages": bookable_packages,
			"active_bays": active_bays,
			"active_technicians": active_techs,
			"default_entry_type_ok": entry_type_ok,
		}
		return checks, counts

	def inspect(self, ctx: SetupContext) -> StepInspection:
		settings = frappe.get_single("ISM Settings")
		enabled = bool(cint(settings.get("enable_self_serve_booking")))
		gate_checks, counts = self._evaluate_readiness_gate(settings, ctx)

		checks: list[CheckResult] = []
		if enabled:
			checks.append(
				CheckResult(
					code="self_serve_enabled",
					level=CHECK_PASS,
					message="Website self-serve booking (/book) is enabled.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="self_serve_enabled",
					level=CHECK_INFO,
					message=(
						"Website self-serve booking (/book) is currently disabled. "
						"All 5 readiness gates are reported below."
					),
					remediation=(
						"Satisfy all 5 readiness gates and set enable_self_serve_booking=1 "
						"(or skip this optional pack)."
					),
				)
			)
		checks.extend(gate_checks)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)

		if not enabled:
			status = STEP_STATUS_INCOMPLETE
			summary = "Self-serve booking (/book) is disabled; review the 5-point readiness gate to enable."
		elif has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = "Self-serve booking is enabled but one or more readiness gate checks failed."
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = "Self-serve booking is enabled with warnings."
		else:
			status = STEP_STATUS_COMPLETE
			summary = "Self-serve booking (/book) is enabled and passes all 5 readiness gates."

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts=counts,
			desk_urls=self._desk_urls(ctx),
		)

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		settings = frappe.get_single("ISM Settings")
		enabled = bool(cint(settings.get("enable_self_serve_booking")))
		checks, _counts = self._evaluate_readiness_gate(settings, ctx)

		if not enabled:
			checks.insert(
				0,
				CheckResult(
					code="self_serve_enabled",
					level=CHECK_FAIL,
					message="Website self-serve booking (enable_self_serve_booking) is disabled.",
					remediation="Enable enable_self_serve_booking in ISM Settings.",
				),
			)

		# Read-only slot search confirmation
		first_pkg = (
			frappe.db.get_value(
				"Service Package",
				{"allow_self_serve_booking": 1},
				"name",
				order_by="self_serve_sort_order asc, name asc",
			)
			if frappe.db.exists("DocType", "Service Package")
			else None
		)
		horizon_days = min(max(cint(settings.get("self_serve_horizon_days") or 7), 1), 14)

		try:
			from ism.scheduling.self_serve import find_self_serve_slots

			if first_pkg:
				slot_result = find_self_serve_slots(
					package_ids=[first_pkg],
					days_ahead=horizon_days,
				)
			else:
				default_dur = cint(settings.get("default_diagnostic_duration") or 60)
				slot_result = find_self_serve_slots(
					duration_minutes=default_dur,
					days_ahead=horizon_days,
				)
			total_slots = cint(slot_result.get("total_slots") or 0)
		except Exception as exc:
			checks.append(
				CheckResult(
					code="self_serve_slot_search",
					level=CHECK_FAIL,
					message=f"Self-serve slot search failed: {exc}",
					remediation="Verify operating hours, bays, technicians, and holiday list.",
				)
			)
		else:
			if total_slots > 0:
				checks.append(
					CheckResult(
						code="self_serve_slot_search",
						level=CHECK_PASS,
						message=(
							f"Read-only slot search returned {total_slots} open slot(s) "
							f"over the next {horizon_days} day(s)."
						),
					)
				)
			else:
				checks.append(
					CheckResult(
						code="self_serve_slot_search",
						level=CHECK_FAIL,
						message=(
							f"Read-only slot search returned 0 open slots over the next {horizon_days} day(s)."
						),
						remediation=(
							"Check shop operating hours, bay equipment tags, technician capacity, "
							"and holiday list."
						),
					)
				)

		passed = not any(c.level == CHECK_FAIL for c in checks)
		summary = (
			"Self-serve booking 5-point readiness gate and live slot search passed."
			if passed
			else "Self-serve booking verification failed."
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=summary,
			checks=tuple(checks),
		)
