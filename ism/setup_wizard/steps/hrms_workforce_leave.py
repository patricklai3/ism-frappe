# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Optional Capability Pack step: HRMS Workforce Leave Types, Allocations & Holiday Assignment."""

from __future__ import annotations

import datetime
from typing import Any

import frappe
from frappe.utils import cint, flt

from ism.setup_wizard.base import BaseSetupStep, build_desk_link
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_INFO,
	CHECK_PASS,
	CHECK_WARN,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_WARNING,
	TIER_OPTIONAL_PACK,
	CheckResult,
	SetupContext,
	StepApplyResult,
	StepInspection,
	StepMeta,
	StepVerifyResult,
)

DEFAULT_LEAVE_SPECS: tuple[dict[str, Any], ...] = (
	{"leave_type_name": "Privilege Leave", "max_leaves_allowed": 20, "allocation_days": 15},
	{"leave_type_name": "Sick Leave", "max_leaves_allowed": 15, "allocation_days": 10},
	{"leave_type_name": "Casual Leave", "max_leaves_allowed": 15, "allocation_days": 10},
)


def _current_year_bounds(year: int | None = None) -> tuple[int, str, str]:
	resolved_year = cint(year) if year else datetime.date.today().year
	return resolved_year, f"{resolved_year}-01-01", f"{resolved_year}-12-31"


def _get_active_technicians() -> list[dict[str, Any]]:
	designation = "Technician"
	try:
		settings = frappe.get_single("ISM Settings")
		if getattr(settings, "technician_designation", None):
			designation = str(settings.technician_designation).strip() or "Technician"
	except Exception:
		pass

	if not frappe.db.exists("DocType", "Employee"):
		return []

	return list(
		frappe.get_all(
			"Employee",
			filters={"designation": designation, "status": "Active"},
			fields=["name", "employee_name", "company"],
			order_by="name asc",
		)
		or []
	)


def _resolve_company_holiday_list(ctx: SetupContext, payload_hl: str | None = None) -> str | None:
	if payload_hl and frappe.db.exists("Holiday List", payload_hl):
		return payload_hl

	try:
		settings_hl = frappe.db.get_single_value("ISM Settings", "holiday_list")
		if settings_hl and frappe.db.exists("Holiday List", settings_hl):
			return str(settings_hl)
	except Exception:
		pass

	if ctx.company and frappe.db.exists("Company", ctx.company):
		company_hl = frappe.db.get_value("Company", ctx.company, "default_holiday_list")
		if company_hl and frappe.db.exists("Holiday List", company_hl):
			return str(company_hl)

	return None


class HrmsWorkforceLeaveStep(BaseSetupStep):
	"""Seed HRMS Leave Types, current-year technician Leave Allocations, and Holiday List Assignment."""

	meta = StepMeta(
		id="hrms_workforce_leave",
		title="HRMS Workforce & Leave Allocations",
		phase="workforce",
		order=170,
		schema_version=1,
		tier=TIER_OPTIONAL_PACK,
		depends_on=("users_roles_employees",),
		owner_domain="workforce",
		managed_settings_sections=(),
		allow_skip=True,
		description=(
			"Auto-detected when Frappe HRMS is installed: seeds standard Leave Types, "
			"current-year submitted Leave Allocations for active technicians, and "
			"Company Holiday List Assignment."
		),
	)

	def is_applicable(self, ctx: SetupContext) -> bool:
		return bool(frappe.db.exists("DocType", "Leave Application"))

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		year, _from_dt, _to_dt = _current_year_bounds()
		return {
			"step_id": self.meta.id,
			"meta": self.meta.to_dict(),
			"fields": [
				{
					"fieldname": "year",
					"label": "Allocation Year",
					"fieldtype": "Int",
					"default": year,
					"reqd": False,
					"read_only": False,
					"is_secret": False,
				},
				{
					"fieldname": "leave_types",
					"label": "Leave Types & Default Allocation Days",
					"fieldtype": "JSON",
					"default": [dict(spec) for spec in DEFAULT_LEAVE_SPECS],
					"reqd": False,
					"read_only": False,
					"is_secret": False,
				},
				{
					"fieldname": "holiday_list",
					"label": "Holiday List (for Company Holiday List Assignment)",
					"fieldtype": "Link",
					"options": "Holiday List",
					"default": _resolve_company_holiday_list(ctx),
					"reqd": False,
					"read_only": False,
					"is_secret": False,
				},
			],
			"secret_fields": [],
			"presets": self.get_presets(ctx),
			"desk_urls": {
				"leave_types": build_desk_link("Leave Type", site_url=ctx.site_url),
				"leave_allocations": build_desk_link("Leave Allocation", site_url=ctx.site_url),
			},
		}

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		year, _from_dt, _to_dt = _current_year_bounds()
		return [
			{
				"id": "ontario_standard_leave",
				"label": "Ontario Shop Standard Leave Package",
				"description": (
					"Seeds Privilege Leave (15d), Sick Leave (10d), and Casual Leave (10d) "
					"allocations for all active technicians."
				),
				"payload": {
					"year": year,
					"leave_types": [dict(spec) for spec in DEFAULT_LEAVE_SPECS],
				},
			}
		]

	def _inspect_leave_state(
		self,
		ctx: SetupContext,
		year: int | None = None,
	) -> tuple[list[CheckResult], dict[str, Any]]:
		resolved_year, from_dt, to_dt = _current_year_bounds(year)
		required_types = [spec["leave_type_name"] for spec in DEFAULT_LEAVE_SPECS]
		missing_types = [
			lt for lt in required_types if not frappe.db.exists("Leave Type", lt)
		]

		checks: list[CheckResult] = []
		if not missing_types:
			checks.append(
				CheckResult(
					code="hrms_leave_types",
					level=CHECK_PASS,
					message=f"All standard Leave Types exist ({', '.join(required_types)}).",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="hrms_leave_types",
					level=CHECK_FAIL,
					message=f"Missing Leave Type(s): {', '.join(missing_types)}.",
					remediation="Apply hrms_workforce_leave to seed standard Leave Types.",
				)
			)

		active_techs = _get_active_technicians()
		missing_alloc_techs: list[str] = []
		allocated_count = 0

		if not active_techs:
			checks.append(
				CheckResult(
					code="hrms_technician_allocations",
					level=CHECK_WARN,
					message="No active Technician employees found to verify Leave Allocations.",
					remediation="Add active Technician employees in users_roles_employees, then apply this step.",
				)
			)
		else:
			for tech in active_techs:
				tech_allocs = frappe.db.count(
					"Leave Allocation",
					{
						"employee": tech["name"],
						"from_date": ("<=", to_dt),
						"to_date": (">=", from_dt),
						"docstatus": 1,
					},
				)
				if tech_allocs > 0:
					allocated_count += 1
				else:
					missing_alloc_techs.append(tech["name"])

			if not missing_alloc_techs:
				checks.append(
					CheckResult(
						code="hrms_technician_allocations",
						level=CHECK_PASS,
						message=(
							f"All {len(active_techs)} active technician(s) have submitted "
							f"{resolved_year} Leave Allocations."
						),
					)
				)
			else:
				checks.append(
					CheckResult(
						code="hrms_technician_allocations",
						level=CHECK_FAIL,
						message=(
							f"{len(missing_alloc_techs)} of {len(active_techs)} active technician(s) "
							f"lack submitted {resolved_year} Leave Allocations: "
							f"{', '.join(missing_alloc_techs[:5])}."
						),
						remediation="Apply hrms_workforce_leave to submit current-year Leave Allocations.",
					)
				)

		has_hla_doctype = bool(frappe.db.exists("DocType", "Holiday List Assignment"))
		hla_assigned = False
		if has_hla_doctype and ctx.company:
			hla_assigned = bool(
				frappe.db.exists(
					"Holiday List Assignment",
					{
						"applicable_for": "Company",
						"assigned_to": ctx.company,
						"docstatus": 1,
					},
				)
			)
			if hla_assigned:
				checks.append(
					CheckResult(
						code="hrms_holiday_list_assignment",
						level=CHECK_PASS,
						message=f"Submitted Company Holiday List Assignment exists for '{ctx.company}'.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="hrms_holiday_list_assignment",
						level=CHECK_WARN,
						message=f"No submitted Company Holiday List Assignment found for '{ctx.company}'.",
						remediation=(
							"Apply hrms_workforce_leave (with a configured Holiday List) to create "
							"and submit a Company Holiday List Assignment."
						),
					)
				)
		elif has_hla_doctype:
			checks.append(
				CheckResult(
					code="hrms_holiday_list_assignment",
					level=CHECK_INFO,
					message="No active Company resolved; skipped Holiday List Assignment check.",
				)
			)

		counts = {
			"year": resolved_year,
			"missing_leave_types": len(missing_types),
			"active_technicians": len(active_techs),
			"technicians_with_allocations": allocated_count,
			"holiday_list_assigned": hla_assigned,
		}
		return checks, counts

	def inspect(self, ctx: SetupContext) -> StepInspection:
		checks, counts = self._inspect_leave_state(ctx)
		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)

		if has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = "HRMS Leave Types or current-year technician Leave Allocations are incomplete."
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = "HRMS leave configuration has warnings."
		else:
			status = STEP_STATUS_COMPLETE
			summary = (
				f"HRMS Leave Types and {counts['year']} technician Leave Allocations are configured."
			)

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts=counts,
			desk_urls={
				"leave_types": build_desk_link("Leave Type", site_url=ctx.site_url),
				"leave_allocations": build_desk_link("Leave Allocation", site_url=ctx.site_url),
			},
		)

	def apply(
		self,
		payload: dict[str, Any],
		ctx: SetupContext,
		*,
		dry_run: bool = False,
	) -> StepApplyResult:
		resolved_year, from_dt, to_dt = _current_year_bounds(payload.get("year"))
		raw_specs = payload.get("leave_types")
		if isinstance(raw_specs, list) and raw_specs:
			leave_specs = [
				{
					"leave_type_name": str(item.get("leave_type_name") or item.get("name") or "").strip(),
					"max_leaves_allowed": flt(item.get("max_leaves_allowed") or 20),
					"allocation_days": flt(item.get("allocation_days") or 10),
				}
				for item in raw_specs
				if isinstance(item, dict) and str(item.get("leave_type_name") or item.get("name") or "").strip()
			]
		else:
			leave_specs = [dict(spec) for spec in DEFAULT_LEAVE_SPECS]

		created: list[str] = []
		updated: list[str] = []
		unchanged: list[str] = []
		warnings: list[str] = []

		# 1. Seed missing Leave Types
		for spec in leave_specs:
			lt_name = spec["leave_type_name"]
			if frappe.db.exists("Leave Type", lt_name):
				unchanged.append(f"Leave Type: {lt_name}")
			else:
				doc = frappe.get_doc({
					"doctype": "Leave Type",
					"leave_type_name": lt_name,
					"max_leaves_allowed": spec["max_leaves_allowed"],
					"is_lwp": 0,
					"include_holiday": 0,
					"allow_negative": 0,
				})
				doc.insert(ignore_permissions=True)
				created.append(f"Leave Type: {lt_name}")

		# 2. Submit missing current-year Leave Allocations for active technicians
		active_techs = _get_active_technicians()
		if not active_techs:
			warnings.append("No active Technician employees found; no Leave Allocations created.")
		elif frappe.db.exists("DocType", "Leave Allocation"):
			for tech in active_techs:
				emp_id = tech["name"]
				emp_company = tech.get("company") or ctx.company
				for spec in leave_specs:
					lt_name = spec["leave_type_name"]
					alloc_days = flt(spec["allocation_days"])
					if alloc_days <= 0:
						continue
					exists = frappe.db.exists(
						"Leave Allocation",
						{
							"employee": emp_id,
							"leave_type": lt_name,
							"from_date": ("<=", to_dt),
							"to_date": (">=", from_dt),
							"docstatus": 1,
						},
					)
					label = f"Leave Allocation: {emp_id} / {lt_name} ({resolved_year})"
					if exists:
						unchanged.append(label)
					else:
						alloc = frappe.get_doc({
							"doctype": "Leave Allocation",
							"employee": emp_id,
							"leave_type": lt_name,
							"from_date": from_dt,
							"to_date": to_dt,
							"new_leaves_allocated": alloc_days,
							"company": emp_company,
						})
						alloc.insert(ignore_permissions=True)
						alloc.submit()
						created.append(label)

		# 3. Create & submit Company Holiday List Assignment if missing
		if frappe.db.exists("DocType", "Holiday List Assignment") and ctx.company:
			hl_name = _resolve_company_holiday_list(ctx, payload.get("holiday_list"))
			if not hl_name:
				warnings.append(
					"No Holiday List configured on ISM Settings or Company; skipped Holiday List Assignment."
				)
			else:
				existing_hla = frappe.db.exists(
					"Holiday List Assignment",
					{
						"applicable_for": "Company",
						"assigned_to": ctx.company,
						"holiday_list": hl_name,
						"docstatus": 1,
					},
				)
				hla_label = f"Holiday List Assignment: {ctx.company} -> {hl_name}"
				if existing_hla:
					unchanged.append(hla_label)
				else:
					hla = frappe.get_doc({
						"doctype": "Holiday List Assignment",
						"applicable_for": "Company",
						"assigned_to": ctx.company,
						"holiday_list": hl_name,
						"from_date": from_dt,
					})
					hla.insert(ignore_permissions=True)
					hla.submit()
					created.append(hla_label)

		return StepApplyResult(
			step_id=self.meta.id,
			dry_run=dry_run,
			applied=bool(created or updated),
			created=tuple(created),
			updated=tuple(updated),
			unchanged=tuple(unchanged),
			warnings=tuple(warnings),
		)

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		opts = dict(options) if isinstance(options, dict) else {}
		checks, counts = self._inspect_leave_state(ctx, year=opts.get("year"))
		passed = not any(c.level == CHECK_FAIL for c in checks)
		summary = (
			f"HRMS verification passed: all {counts['active_technicians']} active technician(s) "
			f"have {counts['year']} Leave Allocations."
			if passed
			else f"HRMS verification failed for {counts['year']} Leave Allocations."
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=summary,
			checks=tuple(checks),
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		configured_types: list[dict[str, Any]] = []
		default_days_map = {
			spec["leave_type_name"]: spec["allocation_days"] for spec in DEFAULT_LEAVE_SPECS
		}
		if frappe.db.exists("DocType", "Leave Type"):
			for spec in DEFAULT_LEAVE_SPECS:
				lt_name = spec["leave_type_name"]
				row = frappe.db.get_value(
					"Leave Type",
					lt_name,
					["leave_type_name", "max_leaves_allowed"],
					as_dict=True,
				)
				if row:
					configured_types.append({
						"leave_type_name": row["leave_type_name"],
						"max_leaves_allowed": flt(row.get("max_leaves_allowed") or spec["max_leaves_allowed"]),
						"allocation_days": default_days_map[lt_name],
					})

		if not configured_types:
			configured_types = [dict(spec) for spec in DEFAULT_LEAVE_SPECS]

		return {
			"leave_types": configured_types,
			"default_allocation_days": {
				item["leave_type_name"]: item["allocation_days"] for item in configured_types
			},
		}
