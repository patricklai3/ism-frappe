# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Step 30: Staff users, role profiles, technician isolation guard, Employee links, and Holiday List."""

from __future__ import annotations

from datetime import date
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, today

from ism.permissions import MANAGER_ROLES, get_employee_for_user
from ism.setup_wizard.base import BaseSetupStep, build_desk_link, normalize_records_list
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
	StepApplyResult,
	StepInspection,
	StepMeta,
)
from ism.setup_wizard.presets import STAFF_ROLE_BUNDLES

ROLE_PROFILE_MAP: tuple[tuple[str, str], ...] = (
	("ISM Shop Manager", "Shop Manager"),
	("ISM Service Advisor", "Service Advisor"),
	("ISM Parts Specialist", "Parts Specialist"),
	("ISM Floor Technician", "Floor Technician"),
	("ISM Agent Service Account", "Agent OS Service User"),
)

_PASSWORD_KEYS = frozenset({"password", "new_password", "passwd", "pwd"})


def _get_roles_with_ro_create_permission() -> set[str]:
	"""Return all Role names that currently hold Repair Order create permission."""
	if not frappe.db.exists("DocType", "Repair Order"):
		return set()

	roles: set[str] = set()
	if frappe.db.exists("DocType", "Custom DocPerm"):
		custom_rows = frappe.get_all(
			"Custom DocPerm",
			filters={"parent": "Repair Order", "permlevel": 0},
			fields=["role", "create"],
		)
		if custom_rows:
			for row in custom_rows:
				if cint(row.get("create")) and row.get("role"):
					roles.add(str(row["role"]))
			return roles

	try:
		meta = frappe.get_meta("Repair Order")
		for perm in meta.permissions or []:
			if cint(getattr(perm, "permlevel", 0)) == 0 and cint(getattr(perm, "create", 0)):
				role_name = getattr(perm, "role", None)
				if role_name:
					roles.add(str(role_name))
	except Exception:
		pass
	return roles


def _get_conflicting_technician_roles(user_roles: set[str], ro_create_roles: set[str]) -> list[str]:
	forbidden = set(MANAGER_ROLES) | set(ro_create_roles)
	return sorted(user_roles & forbidden)


def _resolve_bundle(raw_bundle: str) -> tuple[str, dict[str, Any]]:
	cleaned = (raw_bundle or "").strip().lower()
	for canonical_name, spec in STAFF_ROLE_BUNDLES.items():
		if cleaned == canonical_name.lower():
			return canonical_name, spec
	available = ", ".join(STAFF_ROLE_BUNDLES.keys())
	frappe.throw(
		_("Unknown staff role bundle '{0}'. Valid bundles: {1}.").format(raw_bundle, available),
		frappe.ValidationError,
	)


def _ensure_designation(designation_name: str) -> bool:
	if frappe.db.exists("Designation", designation_name):
		return False
	frappe.get_doc({
		"doctype": "Designation",
		"designation_name": designation_name,
	}).insert(ignore_permissions=True)
	return True


def _ensure_holiday_list(
	requested: Any,
	company: str | None,
) -> tuple[str | None, bool, list[str]]:
	"""Create a current + next year Holiday List when requested and link blank ISM/Company fields."""
	updated_links: list[str] = []
	if not requested:
		return None, False, updated_links

	current_year = date.fromisoformat(today()).year
	if isinstance(requested, dict):
		hl_name = str(
			requested.get("holiday_list_name")
			or requested.get("name")
			or f"Ontario Shop Holidays {current_year}-{current_year + 1}"
		).strip()
		from_date = str(requested.get("from_date") or f"{current_year}-01-01").strip()
		to_date = str(requested.get("to_date") or f"{current_year + 1}-12-31").strip()
	elif isinstance(requested, str) and requested.strip():
		hl_name = requested.strip()
		from_date = f"{current_year}-01-01"
		to_date = f"{current_year + 1}-12-31"
	else:
		hl_name = f"Ontario Shop Holidays {current_year}-{current_year + 1}"
		from_date = f"{current_year}-01-01"
		to_date = f"{current_year + 1}-12-31"

	created_hl = False
	if not frappe.db.exists("Holiday List", hl_name):
		hl_doc = frappe.get_doc({
			"doctype": "Holiday List",
			"holiday_list_name": hl_name,
			"from_date": from_date,
			"to_date": to_date,
		})
		hl_doc.insert(ignore_permissions=True)
		created_hl = True

	# Link ISM Settings.holiday_list if currently blank
	ism_settings = frappe.get_single("ISM Settings")
	if not (ism_settings.get("holiday_list") or "").strip():
		ism_settings.holiday_list = hl_name
		ism_settings.save(ignore_permissions=True)
		updated_links.append(f"ISM Settings.holiday_list={hl_name}")

	# Link Company.default_holiday_list if currently blank
	if company and frappe.db.exists("Company", company):
		cur_comp_hl = (frappe.db.get_value("Company", company, "default_holiday_list") or "").strip()
		if not cur_comp_hl:
			frappe.db.set_value("Company", company, "default_holiday_list", hl_name)
			updated_links.append(f"Company({company}).default_holiday_list={hl_name}")

	return hl_name, created_hl, updated_links


def _ensure_employee_for_staff(
	email: str,
	first_name: str,
	last_name: str,
	company: str | None,
	designation: str | None,
	holiday_list: str | None,
) -> tuple[str, str]:
	"""Idempotently create or link an active Employee for email. Returns (status, employee_name)."""
	existing_name = frappe.db.get_value("Employee", {"user_id": email}, "name")
	if existing_name:
		emp = frappe.get_doc("Employee", existing_name)
		dirty = False
		if (emp.status or "") != "Active":
			emp.status = "Active"
			dirty = True
		if designation and (emp.designation or "") != designation:
			emp.designation = designation
			dirty = True
		if holiday_list and not (emp.holiday_list or "").strip():
			emp.holiday_list = holiday_list
			dirty = True
		if dirty:
			emp.save(ignore_permissions=True)
			return "updated", emp.name
		return "unchanged", emp.name

	if not company:
		frappe.throw(
			_("Cannot create Employee for '{0}': no active Company configured.").format(email),
			frappe.ValidationError,
		)

	emp_data: dict[str, Any] = {
		"doctype": "Employee",
		"first_name": first_name or email.split("@")[0],
		"last_name": last_name or "",
		"user_id": email,
		"company": company,
		"status": "Active",
		"gender": frappe.db.get_value("Gender", {}, "name") or "Prefer not to say",
		"date_of_birth": "1990-01-01",
		"date_of_joining": today(),
	}
	if designation:
		emp_data["designation"] = designation
	if holiday_list:
		emp_data["holiday_list"] = holiday_list

	emp = frappe.get_doc(emp_data)
	emp.insert(ignore_permissions=True)
	return "created", emp.name


class UsersRolesEmployeesStep(BaseSetupStep):
	meta = StepMeta(
		id="users_roles_employees",
		title="Users, Role Profiles & Technicians",
		phase="workforce",
		order=30,
		schema_version=1,
		tier=TIER_CORE,
		depends_on=("erpnext_foundation",),
		owner_domain="ism",
		managed_settings_sections=(),
		allow_skip=False,
		description=(
			"Seed ISM Role Profiles, additively assign staff bundles without overwriting existing "
			"ERPNext roles, enforce strict Floor Technician isolation & Employee linking, and configure Holiday List."
		),
	)

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "seed_role_profiles_and_holidays",
				"label": "Seed ISM Role Profiles & Current/Next Year Holiday List",
				"description": (
					"Creates the 5 standard ISM Role Profiles, ensures Technician Designation exists, "
					"and creates a 2-year Holiday List linked to ISM Settings and Company (if blank)."
				),
				"payload": {
					"seed_role_profiles": True,
					"holiday_list": True,
				},
			}
		]

	def _desk_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"user": build_desk_link("User", site_url=ctx.site_url),
			"role_profile": build_desk_link("Role Profile", site_url=ctx.site_url),
			"employee": build_desk_link("Employee", site_url=ctx.site_url),
			"designation": build_desk_link("Designation", site_url=ctx.site_url),
			"holiday_list": build_desk_link("Holiday List", site_url=ctx.site_url),
		}

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		tech_desig = (
			frappe.db.get_single_value("ISM Settings", "technician_designation") or "Technician"
		).strip()
		holiday_list = (frappe.db.get_single_value("ISM Settings", "holiday_list") or "").strip()
		return {
			"step_id": self.meta.id,
			"meta": self.meta.to_dict(),
			"fields": [
				{
					"fieldname": "seed_role_profiles",
					"label": "Seed ISM Role Profiles",
					"fieldtype": "Check",
					"default": 1,
					"reqd": False,
					"read_only": False,
					"is_secret": False,
				},
				{
					"fieldname": "staff",
					"label": "Staff Roster",
					"fieldtype": "JSON",
					"default": [],
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"description": (
						"List/JSON/CSV of {email, first_name, last_name, bundle "
						"('Shop Manager'|'Service Advisor'|'Parts Specialist'|'Floor Technician'), "
						"create_employee, send_welcome_email}. Never pass plaintext passwords."
					),
				},
				{
					"fieldname": "holiday_list",
					"label": "Holiday List",
					"fieldtype": "Link",
					"options": "Holiday List",
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"value": holiday_list,
					"description": "Pass True, a Holiday List name, or {holiday_list_name, from_date, to_date}.",
				},
				{
					"fieldname": "technician_designation",
					"label": "Technician Designation",
					"fieldtype": "Data",
					"default": "Technician",
					"reqd": False,
					"read_only": True,
					"is_secret": False,
					"value": tech_desig,
				},
			],
			"role_bundles": STAFF_ROLE_BUNDLES,
			"secret_fields": [],
			"presets": self.get_presets(ctx),
			"desk_urls": self._desk_urls(ctx),
		}

	def inspect(self, ctx: SetupContext) -> StepInspection:
		checks: list[CheckResult] = []
		tech_designation = (
			frappe.db.get_single_value("ISM Settings", "technician_designation") or "Technician"
		).strip()
		ro_create_roles = _get_roles_with_ro_create_permission()

		# 1. >= 1 enabled ISM Manager user
		manager_users = frappe.db.sql_list(
			"""
			SELECT DISTINCT u.`name`
			FROM `tabUser` u
			INNER JOIN `tabHas Role` hr
				ON hr.`parent` = u.`name`
			   AND hr.`parenttype` = 'User'
			WHERE u.`enabled` = 1
			  AND u.`name` NOT IN ('Guest')
			  AND hr.`role` = 'ISM Manager'
			"""
		) or []
		if manager_users:
			checks.append(
				CheckResult(
					code="ism_manager_count",
					level=CHECK_PASS,
					message=f"Found {len(manager_users)} enabled ISM Manager user(s).",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="ism_manager_count",
					level=CHECK_FAIL,
					message="No enabled User holds the 'ISM Manager' role.",
					remediation="Assign at least one Shop Manager user via this step or Desk > User.",
				)
			)

		# 2. Technician Designation exists
		has_desig = bool(tech_designation and frappe.db.exists("Designation", tech_designation))
		checks.append(
			CheckResult(
				code="technician_designation",
				level=CHECK_PASS if has_desig else CHECK_FAIL,
				message=(
					f"Designation '{tech_designation}' exists."
					if has_desig
					else f"Technician Designation '{tech_designation}' does not exist."
				),
				remediation=None if has_desig else "Apply this step to seed the Technician Designation.",
			)
		)

		# 3. >= 1 active Employee with designation == technician_designation
		active_tech_employees = (
			frappe.get_all(
				"Employee",
				filters={"status": "Active", "designation": tech_designation},
				pluck="name",
			)
			if frappe.db.exists("DocType", "Employee")
			else []
		)
		if active_tech_employees:
			checks.append(
				CheckResult(
					code="active_technician_employees",
					level=CHECK_PASS,
					message=f"Found {len(active_tech_employees)} active Employee(s) with designation '{tech_designation}'.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="active_technician_employees",
					level=CHECK_FAIL,
					message=f"No active Employee found with designation '{tech_designation}'.",
					remediation="Add at least one Floor Technician via this step or create an active Technician Employee.",
				)
			)

		# 4. Every enabled ISM Technician user has linked active Employee AND zero conflicting roles
		tech_users = frappe.db.sql_list(
			"""
			SELECT DISTINCT u.`name`
			FROM `tabUser` u
			INNER JOIN `tabHas Role` hr
				ON hr.`parent` = u.`name`
			   AND hr.`parenttype` = 'User'
			WHERE u.`enabled` = 1
			  AND u.`name` NOT IN ('Administrator', 'Guest')
			  AND hr.`role` = 'ISM Technician'
			"""
		) or []

		unlinked_techs: list[str] = []
		conflicting_techs: list[str] = []
		for user_id in tech_users:
			emp_name = get_employee_for_user(user_id)
			emp_status = (
				frappe.db.get_value("Employee", emp_name, "status") if emp_name else None
			)
			if not emp_name or emp_status != "Active":
				unlinked_techs.append(user_id)

			user_roles = set(frappe.get_roles(user_id) or [])
			conflicts = _get_conflicting_technician_roles(user_roles, ro_create_roles)
			if conflicts:
				conflicting_techs.append(f"{user_id} ({', '.join(conflicts)})")

		if unlinked_techs:
			checks.append(
				CheckResult(
					code="technician_employee_links",
					level=CHECK_FAIL,
					message=f"ISM Technician user(s) missing an active linked Employee: {', '.join(unlinked_techs)}.",
					remediation="Link each ISM Technician user to an active Employee via Employee.user_id.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="technician_employee_links",
					level=CHECK_PASS,
					message=(
						f"All {len(tech_users)} enabled ISM Technician user(s) have active linked Employee records."
						if tech_users
						else "No enabled ISM Technician users to audit for Employee links."
					),
				)
			)

		if conflicting_techs:
			checks.append(
				CheckResult(
					code="technician_role_isolation",
					level=CHECK_FAIL,
					message=(
						"ISM Technician isolation violation — technician user(s) also hold manager/RO-create roles: "
						f"{'; '.join(conflicting_techs)}."
					),
					remediation="Remove manager/advisor/RO-create roles from Floor Technician accounts (or apply with strip_manager_roles=True).",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="technician_role_isolation",
					level=CHECK_PASS,
					message="All enabled ISM Technician users satisfy strict role isolation.",
				)
			)

		# 5. ISM Settings.holiday_list
		holiday_list = (frappe.db.get_single_value("ISM Settings", "holiday_list") or "").strip()
		if holiday_list and frappe.db.exists("Holiday List", holiday_list):
			checks.append(
				CheckResult(
					code="holiday_list",
					level=CHECK_PASS,
					message=f"ISM Settings.holiday_list is '{holiday_list}'.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="holiday_list",
					level=CHECK_WARN,
					message="ISM Settings.holiday_list is not configured.",
					remediation="Create and link a Holiday List so scheduling skips statutory shop holidays.",
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)
		if has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = "Workforce roles, technician Employees, or isolation checks require attention."
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = "Workforce configured with minor recommendations (e.g. Holiday List)."
		else:
			status = STEP_STATUS_COMPLETE
			summary = (
				f"Workforce ready: {len(manager_users)} ISM Manager(s), "
				f"{len(active_tech_employees)} active Technician Employee(s)."
			)

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"ism_managers": len(manager_users),
				"ism_technician_users": len(tech_users),
				"active_technician_employees": len(active_tech_employees),
			},
			desk_urls=self._desk_urls(ctx),
		)

	def apply(
		self,
		payload: dict[str, Any],
		ctx: SetupContext,
		*,
		dry_run: bool = False,
	) -> StepApplyResult:
		# Strictly reject plaintext passwords anywhere at top-level
		for key in _PASSWORD_KEYS:
			if payload.get(key) not in (None, ""):
				frappe.throw(
					_("Plaintext password field '{0}' is not accepted by users_roles_employees. Set passwords via Frappe Desk or welcome email.").format(
						key
					),
					frappe.ValidationError,
				)

		created: list[str] = []
		updated: list[str] = []
		unchanged: list[str] = []
		warnings: list[str] = []
		user_desk_links: dict[str, str] = {}

		tech_designation = (
			frappe.db.get_single_value("ISM Settings", "technician_designation") or "Technician"
		).strip()
		if _ensure_designation(tech_designation):
			created.append(f"Designation: {tech_designation}")
		else:
			unchanged.append(f"Designation: {tech_designation}")

		# 1. Optional Role Profiles
		if cint(payload.get("seed_role_profiles")):
			existing_roles_set = set(frappe.get_all("Role", pluck="name"))
			for profile_name, bundle_key in ROLE_PROFILE_MAP:
				if frappe.db.exists("Role Profile", profile_name):
					unchanged.append(f"Role Profile: {profile_name}")
					continue
				bundle_roles = [
					r for r in STAFF_ROLE_BUNDLES[bundle_key]["roles"] if r in existing_roles_set
				]
				rp = frappe.get_doc({
					"doctype": "Role Profile",
					"role_profile": profile_name,
					"roles": [{"role": r} for r in bundle_roles],
				})
				rp.insert(ignore_permissions=True)
				created.append(f"Role Profile: {profile_name}")

		# 2. Optional Holiday List
		holiday_list_name: str | None = (
			frappe.db.get_single_value("ISM Settings", "holiday_list") or ""
		).strip() or None
		if payload.get("holiday_list"):
			hl_name, created_hl, hl_links = _ensure_holiday_list(
				payload["holiday_list"], ctx.company
			)
			if hl_name:
				holiday_list_name = hl_name
				if created_hl:
					created.append(f"Holiday List: {hl_name}")
				else:
					unchanged.append(f"Holiday List: {hl_name}")
			updated.extend(hl_links)

		# 3. Staff roster provisioning
		raw_staff = payload.get("staff")
		if raw_staff:
			staff_records = normalize_records_list(raw_staff)
			existing_roles_set = set(frappe.get_all("Role", pluck="name"))
			ro_create_roles = _get_roles_with_ro_create_permission()
			global_strip = bool(cint(payload.get("strip_manager_roles")))

			for rec in staff_records:
				for pkey in _PASSWORD_KEYS:
					if rec.get(pkey) not in (None, ""):
						frappe.throw(
							_("Plaintext password field '{0}' is not allowed in staff payload.").format(
								pkey
							),
							frappe.ValidationError,
						)

				email = str(rec.get("email") or "").strip().lower()
				if not email or "@" not in email:
					frappe.throw(
						_("Each staff record must provide a valid 'email' address."),
						frappe.ValidationError,
					)

				bundle_name, bundle_spec = _resolve_bundle(str(rec.get("bundle") or ""))
				first_name = str(rec.get("first_name") or email.split("@")[0]).strip()
				last_name = str(rec.get("last_name") or "").strip()
				send_welcome = cint(rec.get("send_welcome_email", 0))
				is_floor_tech = bundle_name == "Floor Technician"
				strip_mgr = global_strip or bool(cint(rec.get("strip_manager_roles")))

				target_roles = [r for r in bundle_spec["roles"] if r in existing_roles_set]
				user_exists = bool(frappe.db.exists("User", email))

				if not user_exists:
					user_doc = frappe.get_doc({
						"doctype": "User",
						"email": email,
						"first_name": first_name,
						"last_name": last_name,
						"enabled": 1,
						"send_welcome_email": send_welcome,
						"user_type": "System User",
						"roles": [{"role": r} for r in target_roles],
					})
					user_doc.insert(ignore_permissions=True)
					created.append(f"User: {email} ({bundle_name})")
				else:
					user_doc = frappe.get_doc("User", email)
					current_roles = {r.role for r in (user_doc.roles or []) if r.role}
					user_dirty = False

					if is_floor_tech:
						conflicts = _get_conflicting_technician_roles(current_roles, ro_create_roles)
						if conflicts and not strip_mgr:
							frappe.throw(
								_(
									"Refused to assign Floor Technician bundle to '{0}': user holds conflicting "
									"manager/RO-create role(s): {1}. Pass strip_manager_roles=True to strip them explicitly."
								).format(email, ", ".join(conflicts)),
								frappe.ValidationError,
							)
						if conflicts and strip_mgr:
							conflict_set = set(conflicts)
							user_doc.set(
								"roles",
								[row for row in (user_doc.roles or []) if row.role not in conflict_set],
							)
							current_roles -= conflict_set
							user_dirty = True
							warnings.append(
								f"Stripped conflicting role(s) {conflicts} from Floor Technician '{email}'."
							)

					# Strictly additive role append (preserves all existing non-conflicting ERPNext roles)
					added_roles: list[str] = []
					for role_name in target_roles:
						if role_name not in current_roles:
							user_doc.append("roles", {"role": role_name})
							current_roles.add(role_name)
							added_roles.append(role_name)
							user_dirty = True

					if cint(user_doc.enabled) != 1:
						user_doc.enabled = 1
						user_dirty = True

					if user_dirty:
						user_doc.save(ignore_permissions=True)
						updated.append(f"User: {email} (+{', '.join(added_roles) or 'updated'})")
					else:
						unchanged.append(f"User: {email}")

				user_desk_links[email] = build_desk_link("User", email, site_url=ctx.site_url)

				# Employee creation/linking (always for Floor Technician, optional for others)
				should_create_emp = is_floor_tech or bool(cint(rec.get("create_employee")))
				if should_create_emp:
					emp_desig = tech_designation if is_floor_tech else rec.get("designation")
					emp_status, emp_name = _ensure_employee_for_staff(
						email=email,
						first_name=first_name,
						last_name=last_name,
						company=ctx.company,
						designation=emp_desig,
						holiday_list=holiday_list_name,
					)
					if emp_status == "created":
						created.append(f"Employee: {emp_name} ({email})")
					elif emp_status == "updated":
						updated.append(f"Employee: {emp_name} ({email})")
					else:
						unchanged.append(f"Employee: {emp_name} ({email})")

		handoff = {"user_desk_links": user_desk_links} if user_desk_links else None
		return StepApplyResult(
			step_id=self.meta.id,
			dry_run=dry_run,
			applied=bool(created or updated),
			created=tuple(created),
			updated=tuple(updated),
			unchanged=tuple(unchanged),
			warnings=tuple(warnings),
			handoff=handoff,
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		holiday_list = (frappe.db.get_single_value("ISM Settings", "holiday_list") or "").strip()
		has_profiles = any(
			frappe.db.exists("Role Profile", profile_name) for profile_name, _ in ROLE_PROFILE_MAP
		)

		ism_role_users = frappe.db.sql(
			"""
			SELECT DISTINCT u.`name` AS email, u.`first_name`, u.`last_name`
			FROM `tabUser` u
			INNER JOIN `tabHas Role` hr
				ON hr.`parent` = u.`name`
			   AND hr.`parenttype` = 'User'
			WHERE u.`enabled` = 1
			  AND u.`name` NOT IN ('Administrator', 'Guest')
			  AND hr.`role` IN ('ISM Manager', 'ISM Service Advisor', 'ISM Technician')
			ORDER BY u.`name` ASC
			""",
			as_dict=True,
		)

		staff_export: list[dict[str, Any]] = []
		for row in ism_role_users:
			email = row["email"]
			roles = set(frappe.get_roles(email) or [])
			if "ISM Manager" in roles:
				bundle = "Shop Manager"
			elif "ISM Service Advisor" in roles:
				bundle = "Parts Specialist" if "Stock Manager" in roles else "Service Advisor"
			else:
				bundle = "Floor Technician"
			staff_export.append({
				"email": email,
				"first_name": row.get("first_name") or "",
				"last_name": row.get("last_name") or "",
				"bundle": bundle,
				"create_employee": bool(get_employee_for_user(email)),
			})

		return {
			"seed_role_profiles": has_profiles,
			"holiday_list": holiday_list,
			"staff": staff_export,
		}
