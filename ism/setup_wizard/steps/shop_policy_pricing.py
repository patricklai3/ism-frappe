# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Step 40: Shop operating hours, technician capacity, duration estimation, pricing, and approval policy."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt, to_timedelta

from ism.setup_wizard.base import build_desk_link
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_PASS,
	CHECK_WARN,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_WARNING,
	TIER_CORE,
	CheckResult,
	SetupContext,
	StepInspection,
	StepMeta,
)
from ism.setup_wizard.settings_helper import SettingsSectionStep


class ShopPolicyPricingStep(SettingsSectionStep):
	meta = StepMeta(
		id="shop_policy_pricing",
		title="Shop Hours, Capacity, Pricing & Approvals",
		phase="policy",
		order=40,
		schema_version=1,
		tier=TIER_CORE,
		depends_on=("users_roles_employees",),
		owner_domain="ism",
		managed_settings_sections=(
			"sb_scheduling",
			"sb_capacity",
			"sb_estimation",
			"sb_pricing",
			"sb_approval_settings",
		),
		allow_skip=False,
		description=(
			"Configure shop operating hours, slot interval, holiday list, technician capacity designation, "
			"duration estimation tuning, default hourly labor rate, shop supplies, and customer approval policy."
		),
	)

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "ontario_ev_shop_policy",
				"label": "Ontario EV Shop Standard Policy (08:00-17:00, $150/hr)",
				"description": (
					"Standard 8am-5pm operating hours with 12:00-12:30 lunch break, 30-min slots, "
					"$150/hr labor rate, 5% shop supplies ($50 cap), and 10% / $20 approval tolerance."
				),
				"payload": {
					"settings": {
						"operating_hours_start": "08:00:00",
						"operating_hours_end": "17:00:00",
						"break_start": "12:00:00",
						"break_end": "12:30:00",
						"default_slot_interval": 30,
						"scheduling_horizon_days": 30,
						"technician_designation": "Technician",
						"enable_technician_capacity": 1,
						"estimation_percentile": 80.0,
						"estimation_sigma": 0.30,
						"buffer_padding_minutes": 0,
						"default_diagnostic_duration": 60,
						"default_labor_rate": 150.0,
						"shop_supplies_percentage": 5.0,
						"shop_supplies_cap": 50.0,
						"authorization_token_expiry_hours": 48,
						"cost_tolerance_percentage": 10.0,
						"cost_tolerance_flat_amount": 20.0,
					}
				},
			}
		]

	def inspect(self, ctx: SetupContext) -> StepInspection:
		settings = frappe.get_single("ISM Settings")
		checks: list[CheckResult] = []

		# 1. operating_hours_start < operating_hours_end
		op_start = settings.get("operating_hours_start")
		op_end = settings.get("operating_hours_end")
		if not op_start or not op_end:
			checks.append(
				CheckResult(
					code="operating_hours",
					level=CHECK_FAIL,
					message="Both operating_hours_start and operating_hours_end must be set.",
					remediation="Set valid shop opening and closing times in ISM Settings.",
				)
			)
		else:
			try:
				start_td = to_timedelta(str(op_start))
				end_td = to_timedelta(str(op_end))
				if start_td < end_td:
					checks.append(
						CheckResult(
							code="operating_hours",
							level=CHECK_PASS,
							message=f"Operating hours configured: {op_start} to {op_end}.",
						)
					)
				else:
					checks.append(
						CheckResult(
							code="operating_hours",
							level=CHECK_FAIL,
							message=f"operating_hours_start ({op_start}) must be earlier than operating_hours_end ({op_end}).",
							remediation="Correct shop operating hours so start is before end.",
						)
					)
			except Exception as exc:
				checks.append(
					CheckResult(
						code="operating_hours",
						level=CHECK_FAIL,
						message=f"Invalid operating hours format: {exc}",
						remediation="Use HH:MM:SS time values for operating_hours_start and operating_hours_end.",
					)
				)

		# 2. default_labor_rate > 0
		labor_rate = flt(settings.get("default_labor_rate") or 0)
		if labor_rate > 0:
			checks.append(
				CheckResult(
					code="default_labor_rate",
					level=CHECK_PASS,
					message=f"Default hourly labor rate is ${labor_rate:.2f}/hr.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="default_labor_rate",
					level=CHECK_FAIL,
					message=f"default_labor_rate must be greater than 0 (currently {labor_rate}).",
					remediation="Set a positive default_labor_rate in ISM Settings.",
				)
			)

		# 3. technician_designation set & exists as Designation
		tech_desig = (settings.get("technician_designation") or "").strip()
		if tech_desig and frappe.db.exists("Designation", tech_desig):
			checks.append(
				CheckResult(
					code="technician_designation",
					level=CHECK_PASS,
					message=f"Technician designation '{tech_desig}' is set and exists.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="technician_designation",
					level=CHECK_FAIL,
					message=(
						f"Technician designation '{tech_desig}' does not exist in Designation."
						if tech_desig
						else "technician_designation is not set in ISM Settings."
					),
					remediation="Set technician_designation to an existing Designation (e.g. 'Technician').",
				)
			)

		# 4. holiday_list set
		holiday_list = (settings.get("holiday_list") or "").strip()
		if holiday_list and frappe.db.exists("Holiday List", holiday_list):
			checks.append(
				CheckResult(
					code="holiday_list",
					level=CHECK_PASS,
					message=f"Holiday List '{holiday_list}' is configured.",
				)
			)
		elif holiday_list:
			checks.append(
				CheckResult(
					code="holiday_list",
					level=CHECK_FAIL,
					message=f"Configured Holiday List '{holiday_list}' does not exist.",
					remediation="Select an existing Holiday List in ISM Settings.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="holiday_list",
					level=CHECK_WARN,
					message="holiday_list is not set in ISM Settings.",
					remediation="Link a Holiday List in ISM Settings so shop holidays are excluded from scheduling.",
				)
			)

		# 5. disclaimer_text non-empty
		disclaimer = (settings.get("disclaimer_text") or "").strip()
		if disclaimer:
			checks.append(
				CheckResult(
					code="disclaimer_text",
					level=CHECK_PASS,
					message="Customer legal disclaimer text is configured.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="disclaimer_text",
					level=CHECK_FAIL,
					message="disclaimer_text is empty in ISM Settings.",
					remediation="Provide Ontario repair authorization legal disclaimer text in ISM Settings.",
				)
			)

		# 6. cost_tolerance_percentage & cost_tolerance_flat_amount
		tol_pct = settings.get("cost_tolerance_percentage")
		tol_flat = settings.get("cost_tolerance_flat_amount")
		if tol_pct is None or tol_flat is None:
			checks.append(
				CheckResult(
					code="cost_tolerance",
					level=CHECK_FAIL,
					message="Both cost_tolerance_percentage and cost_tolerance_flat_amount must be set.",
					remediation="Configure Ontario repair cost tolerance thresholds (e.g. 10% and $20).",
				)
			)
		elif flt(tol_pct) < 0 or flt(tol_flat) < 0:
			checks.append(
				CheckResult(
					code="cost_tolerance",
					level=CHECK_FAIL,
					message="Cost tolerance values cannot be negative.",
					remediation="Set non-negative values for cost_tolerance_percentage and cost_tolerance_flat_amount.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="cost_tolerance",
					level=CHECK_PASS,
					message=f"Cost tolerance configured: {flt(tol_pct):.1f}% / ${flt(tol_flat):.2f} cap.",
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)
		if has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = "Shop scheduling, pricing, or approval policy has invalid/missing fields."
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = "Shop policy and pricing configured with minor recommendations."
		else:
			status = STEP_STATUS_COMPLETE
			summary = (
				f"Shop policy ready: {op_start}-{op_end}, ${labor_rate:.2f}/hr, "
				f"designation '{tech_desig}'."
			)

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			desk_urls={
				"ism_settings": build_desk_link("ISM Settings", site_url=ctx.site_url),
				"holiday_list": build_desk_link("Holiday List", holiday_list or None, site_url=ctx.site_url),
			},
		)
