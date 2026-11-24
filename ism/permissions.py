# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Centralized technician assignment, RO readiness filtering, Desk permission hooks, and Socket.IO recipient resolution."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.permissions import get_role_permissions
from frappe.utils import cint

from ism.board.contracts import parse_ro_from_thread_key

MANAGER_ROLES: frozenset[str] = frozenset(
	{"System Manager", "ISM Manager", "ISM Service Advisor"}
)

TECH_VISIBLE_RO_STATUSES: tuple[str, ...] = (
	"Authorized - In Progress",
	"Work Completed",
	"Billed / Paid",
	"Closed / Released",
)

TECH_VISIBLE_APPROVAL_STATUSES: tuple[str, ...] = (
	"Approved",
	"Partially Approved",
)

_INACTIVE_SCHEDULE_STATUSES: tuple[str, ...] = ("Cancelled", "No Show")


def _resolve_user(user: str | None = None) -> str:
	return (user or getattr(frappe.session, "user", None) or "").strip()


def _extract_docname(doc_or_name: Any) -> str | None:
	if isinstance(doc_or_name, str):
		cleaned = doc_or_name.strip()
		return cleaned or None
	name = getattr(doc_or_name, "name", None)
	if isinstance(name, str) and name.strip():
		return name.strip()
	return None


def is_manager_or_advisor(user: str | None = None) -> bool:
	"""Return True for Administrator or users holding any MANAGER_ROLES role."""
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False
	if target_user == "Administrator":
		return True
	roles = set(frappe.get_roles(target_user) or [])
	return bool(roles & MANAGER_ROLES)


def is_technician_scoped_user(user: str | None = None) -> bool:
	"""Return True iff authenticated, not Administrator/MANAGER_ROLES, and has ISM Technician or non-manager RO read perm."""
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False
	if is_manager_or_advisor(target_user):
		return False

	roles = set(frappe.get_roles(target_user) or [])
	if "ISM Technician" in roles:
		return True

	# Check DocPerm role permissions directly (never invokes controller has_permission hooks).
	try:
		if frappe.db.exists("DocType", "Repair Order"):
			role_perms = get_role_permissions(frappe.get_meta("Repair Order"), user=target_user)
			if role_perms.get("read"):
				return True
	except Exception:
		return False

	return False


def get_employee_for_user(user: str | None = None) -> str | None:
	"""Resolve Employee record linked to target_user via Employee.user_id."""
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return None
	if not frappe.db.exists("DocType", "Employee"):
		return None

	active_emp = frappe.db.get_value(
		"Employee",
		{"user_id": target_user, "status": "Active"},
		"name",
	)
	if active_emp:
		return active_emp

	return frappe.db.get_value("Employee", {"user_id": target_user}, "name") or None


def _is_ro_tech_ready(ro_row: Any) -> bool:
	"""Return True iff RO approval_status, status, and manual_parts_hold meet technician visibility criteria."""
	if not ro_row:
		return False
	if isinstance(ro_row, dict):
		approval_status = ro_row.get("approval_status")
		status = ro_row.get("status")
		manual_parts_hold = ro_row.get("manual_parts_hold")
	else:
		approval_status = getattr(ro_row, "approval_status", None)
		status = getattr(ro_row, "status", None)
		manual_parts_hold = getattr(ro_row, "manual_parts_hold", 0)

	return (
		approval_status in TECH_VISIBLE_APPROVAL_STATUSES
		and status in TECH_VISIBLE_RO_STATUSES
		and cint(manual_parts_hold or 0) == 0
	)


def _get_assigned_employees_for_ro(ro_name: str) -> set[str]:
	"""Return Employee names assigned to ro_name via active Schedule Entry or non-Declined Repair Order Service."""
	clean_ro = (ro_name or "").strip()
	if not clean_ro:
		return set()

	employees: set[str] = set()

	if frappe.db.exists("DocType", "Schedule Entry"):
		se_emps = frappe.db.sql_list(
			"""
			SELECT DISTINCT `assigned_technician`
			FROM `tabSchedule Entry`
			WHERE `repair_order` = %(ro)s
			  AND IFNULL(`assigned_technician`, '') != ''
			  AND IFNULL(`status`, '') NOT IN %(inactive)s
			""",
			{"ro": clean_ro, "inactive": _INACTIVE_SCHEDULE_STATUSES},
		)
		employees.update(e.strip() for e in (se_emps or []) if isinstance(e, str) and e.strip())

	if frappe.db.exists("DocType", "Repair Order Service"):
		svc_emps = frappe.db.sql_list(
			"""
			SELECT DISTINCT `assigned_technician`
			FROM `tabRepair Order Service`
			WHERE `parent` = %(ro)s
			  AND `parenttype` = 'Repair Order'
			  AND IFNULL(`assigned_technician`, '') != ''
			  AND IFNULL(`status`, '') != 'Declined'
			""",
			{"ro": clean_ro},
		)
		employees.update(e.strip() for e in (svc_emps or []) if isinstance(e, str) and e.strip())

	return employees


def get_eligible_ro_names_for_employee(employee: str | None) -> set[str]:
	"""Return set of Repair Order names assigned to employee that are approved and parts-ready."""
	if not isinstance(employee, str) or not employee.strip():
		return set()
	emp = employee.strip()
	if not frappe.db.exists("DocType", "Repair Order"):
		return set()

	rows = frappe.db.sql_list(
		"""
		SELECT ro.`name`
		FROM `tabRepair Order` ro
		WHERE ro.`approval_status` IN %(approval_statuses)s
		  AND ro.`status` IN %(statuses)s
		  AND IFNULL(ro.`manual_parts_hold`, 0) = 0
		  AND (
			  EXISTS (
				  SELECT 1
				  FROM `tabSchedule Entry` se
				  WHERE se.`repair_order` = ro.`name`
					AND se.`assigned_technician` = %(emp)s
					AND IFNULL(se.`status`, '') NOT IN %(inactive)s
			  )
			  OR EXISTS (
				  SELECT 1
				  FROM `tabRepair Order Service` ros
				  WHERE ros.`parent` = ro.`name`
					AND ros.`parenttype` = 'Repair Order'
					AND ros.`assigned_technician` = %(emp)s
					AND IFNULL(ros.`status`, '') != 'Declined'
			  )
		  )
		""",
		{
			"emp": emp,
			"approval_statuses": TECH_VISIBLE_APPROVAL_STATUSES,
			"statuses": TECH_VISIBLE_RO_STATUSES,
			"inactive": _INACTIVE_SCHEDULE_STATUSES,
		},
	)
	return set(rows or [])


def is_ro_visible_to_user(ro_name: str | None, user: str | None = None) -> bool:
	"""Return True iff target_user is allowed to view/access ro_name."""
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False

	clean_ro = _extract_docname(ro_name)
	if not clean_ro:
		return False

	if is_manager_or_advisor(target_user):
		return bool(frappe.db.exists("Repair Order", clean_ro))

	if not is_technician_scoped_user(target_user):
		return False

	emp = get_employee_for_user(target_user)
	if not emp:
		return False

	ro_row = frappe.db.get_value(
		"Repair Order",
		clean_ro,
		["approval_status", "status", "manual_parts_hold"],
		as_dict=True,
	)
	if not _is_ro_tech_ready(ro_row):
		return False

	return emp in _get_assigned_employees_for_ro(clean_ro)


def assert_can_access_ro(ro_name: str | None, user: str | None = None) -> None:
	"""Enforce Repair Order visibility for target_user, raising frappe.PermissionError on denial."""
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		frappe.throw(_("Authentication is required."), frappe.PermissionError)

	if is_manager_or_advisor(target_user):
		return

	if not is_ro_visible_to_user(ro_name, target_user):
		frappe.throw(
			_("You do not have permission to access Repair Order {0}.").format(ro_name or ""),
			frappe.PermissionError,
		)


def is_schedule_entry_assigned_to_user(se_name: str | None, user: str | None = None) -> bool:
	"""Return True iff target_user is a manager/advisor (for existing SE) or the assigned technician."""
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False

	clean_se = _extract_docname(se_name)
	if not clean_se:
		return False

	if is_manager_or_advisor(target_user):
		return bool(frappe.db.exists("Schedule Entry", clean_se))

	if not is_technician_scoped_user(target_user):
		return False

	emp = get_employee_for_user(target_user)
	if not emp:
		return False

	assigned_tech = frappe.db.get_value("Schedule Entry", clean_se, "assigned_technician")
	return bool(assigned_tech and assigned_tech == emp)


def assert_can_access_schedule_entry(se_name: str | None, user: str | None = None) -> None:
	"""Enforce Schedule Entry access for target_user, raising frappe.PermissionError on denial."""
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		frappe.throw(_("Authentication is required."), frappe.PermissionError)

	if is_manager_or_advisor(target_user):
		return

	if not is_schedule_entry_assigned_to_user(se_name, target_user):
		frappe.throw(
			_("You do not have permission to access Schedule Entry {0}.").format(se_name or ""),
			frappe.PermissionError,
		)


def _get_enabled_manager_users() -> set[str]:
	"""Return all enabled User IDs with any MANAGER_ROLES role, plus Administrator."""
	users = set(
		frappe.db.sql_list(
			"""
			SELECT DISTINCT u.`name`
			FROM `tabUser` u
			INNER JOIN `tabHas Role` hr
				ON hr.`parent` = u.`name`
			   AND hr.`parenttype` = 'User'
			WHERE u.`enabled` = 1
			  AND u.`name` != 'Guest'
			  AND hr.`role` IN %(roles)s
			""",
			{"roles": tuple(MANAGER_ROLES)},
		)
		or []
	)
	users.add("Administrator")
	return users


def _get_enabled_users_for_employees(employees: set[str] | list[str] | tuple[str, ...]) -> set[str]:
	"""Return enabled User IDs linked to any Employee in employees."""
	clean_emps = sorted({e.strip() for e in employees if isinstance(e, str) and e.strip()})
	if not clean_emps or not frappe.db.exists("DocType", "Employee"):
		return set()

	return set(
		frappe.db.sql_list(
			"""
			SELECT DISTINCT e.`user_id`
			FROM `tabEmployee` e
			INNER JOIN `tabUser` u
				ON u.`name` = e.`user_id`
			WHERE e.`name` IN %(emps)s
			  AND IFNULL(e.`user_id`, '') != ''
			  AND u.`enabled` = 1
			  AND u.`name` != 'Guest'
			""",
			{"emps": tuple(clean_emps)},
		)
		or []
	)


def get_realtime_recipients_for_ro(ro_name: str | None) -> list[str]:
	"""Return enabled manager/advisor users (+ Administrator) plus assigned tech user_ids ONLY when RO is tech-visible."""
	recipients = _get_enabled_manager_users()
	clean_ro = _extract_docname(ro_name)
	if clean_ro:
		ro_row = frappe.db.get_value(
			"Repair Order",
			clean_ro,
			["approval_status", "status", "manual_parts_hold"],
			as_dict=True,
		)
		if _is_ro_tech_ready(ro_row):
			assigned_emps = _get_assigned_employees_for_ro(clean_ro)
			recipients.update(_get_enabled_users_for_employees(assigned_emps))

	return sorted(recipients)


def get_realtime_recipients_for_schedule_entry(se_name: str | None) -> list[str]:
	"""Return enabled manager/advisor users (+ Administrator) plus assigned technician user_id for a Schedule Entry."""
	recipients = _get_enabled_manager_users()
	clean_se = _extract_docname(se_name)
	if clean_se:
		assigned_tech = frappe.db.get_value("Schedule Entry", clean_se, "assigned_technician")
		if assigned_tech:
			recipients.update(_get_enabled_users_for_employees({assigned_tech}))

	return sorted(recipients)


# ---------------------------------------------------------------------------
# Frappe Permission Hooks (permission_query_conditions & has_permission)
# ---------------------------------------------------------------------------


def _ro_eligible_sql(ro_alias: str, emp: str) -> str:
	"""Build escaped SQL predicate matching technician-eligible Repair Order rows."""
	esc_emp = frappe.db.escape(emp)
	statuses_sql = ", ".join(frappe.db.escape(s) for s in TECH_VISIBLE_RO_STATUSES)
	approvals_sql = ", ".join(frappe.db.escape(s) for s in TECH_VISIBLE_APPROVAL_STATUSES)
	inactive_sql = ", ".join(frappe.db.escape(s) for s in _INACTIVE_SCHEDULE_STATUSES)
	return (
		f"({ro_alias}.`approval_status` IN ({approvals_sql}) "
		f"AND {ro_alias}.`status` IN ({statuses_sql}) "
		f"AND IFNULL({ro_alias}.`manual_parts_hold`, 0) = 0 "
		f"AND ("
		f"EXISTS (SELECT 1 FROM `tabSchedule Entry` _se "
		f"WHERE _se.`repair_order` = {ro_alias}.`name` "
		f"AND _se.`assigned_technician` = {esc_emp} "
		f"AND IFNULL(_se.`status`, '') NOT IN ({inactive_sql})) "
		f"OR EXISTS (SELECT 1 FROM `tabRepair Order Service` _ros "
		f"WHERE _ros.`parent` = {ro_alias}.`name` "
		f"AND _ros.`parenttype` = 'Repair Order' "
		f"AND _ros.`assigned_technician` = {esc_emp} "
		f"AND IFNULL(_ros.`status`, '') != 'Declined')"
		f"))"
	)


def _get_scoped_employee_for_query(user: str | None) -> tuple[bool, str | None]:
	"""Return (should_scope, employee_or_None) for permission_query_conditions hooks."""
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False, None
	if is_manager_or_advisor(target_user):
		return False, None
	if not is_technician_scoped_user(target_user):
		return False, None
	return True, get_employee_for_user(target_user)


def repair_order_permission_query_conditions(user: str | None = None) -> str:
	should_scope, emp = _get_scoped_employee_for_query(user)
	if not should_scope:
		return ""
	if not emp:
		return "1=0"
	return _ro_eligible_sql("`tabRepair Order`", emp)


def repair_order_has_permission(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	_ = ptype
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False
	if is_manager_or_advisor(target_user) or not is_technician_scoped_user(target_user):
		return True
	if doc is None:
		return True

	ro_name = _extract_docname(doc)
	return bool(ro_name and is_ro_visible_to_user(ro_name, target_user))


def schedule_entry_permission_query_conditions(user: str | None = None) -> str:
	should_scope, emp = _get_scoped_employee_for_query(user)
	if not should_scope:
		return ""
	if not emp:
		return "1=0"
	return f"`tabSchedule Entry`.`assigned_technician` = {frappe.db.escape(emp)}"


def schedule_entry_has_permission(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	_ = ptype
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False
	if is_manager_or_advisor(target_user) or not is_technician_scoped_user(target_user):
		return True
	if doc is None:
		return True

	se_name = _extract_docname(doc)
	return bool(se_name and is_schedule_entry_assigned_to_user(se_name, target_user))


def shop_board_thread_permission_query_conditions(user: str | None = None) -> str:
	should_scope, emp = _get_scoped_employee_for_query(user)
	if not should_scope:
		return ""
	if not emp:
		return "1=0"
	return (
		"EXISTS (SELECT 1 FROM `tabRepair Order` _ro "
		"WHERE _ro.`name` = `tabShop Board Thread`.`repair_order` "
		f"AND {_ro_eligible_sql('_ro', emp)})"
	)


def shop_board_thread_has_permission(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	_ = ptype
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False
	if is_manager_or_advisor(target_user) or not is_technician_scoped_user(target_user):
		return True
	if doc is None:
		return True

	if isinstance(doc, str):
		ro_name = frappe.db.get_value("Shop Board Thread", doc, "repair_order") or parse_ro_from_thread_key(doc)
	else:
		ro_name = getattr(doc, "repair_order", None) or parse_ro_from_thread_key(
			getattr(doc, "thread_key", None) or getattr(doc, "name", None)
		)

	return bool(ro_name and is_ro_visible_to_user(ro_name, target_user))


def shop_board_event_permission_query_conditions(user: str | None = None) -> str:
	should_scope, emp = _get_scoped_employee_for_query(user)
	if not should_scope:
		return ""
	if not emp:
		return "1=0"
	return (
		"EXISTS (SELECT 1 FROM `tabRepair Order` _ro "
		"WHERE (_ro.`name` = `tabShop Board Event`.`repair_order` "
		"OR (IFNULL(`tabShop Board Event`.`repair_order`, '') = '' "
		"AND CONCAT('RO:', _ro.`name`) = `tabShop Board Event`.`thread`)) "
		f"AND {_ro_eligible_sql('_ro', emp)})"
	)


def shop_board_event_has_permission(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	_ = ptype
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False
	if is_manager_or_advisor(target_user) or not is_technician_scoped_user(target_user):
		return True
	if doc is None:
		return True

	if isinstance(doc, str):
		ev = frappe.db.get_value("Shop Board Event", doc, ["repair_order", "thread"], as_dict=True)
		ro_name = (
			(ev.get("repair_order") or parse_ro_from_thread_key(ev.get("thread")))
			if ev
			else None
		)
	else:
		ro_name = getattr(doc, "repair_order", None) or parse_ro_from_thread_key(
			getattr(doc, "thread", None)
		)

	return bool(ro_name and is_ro_visible_to_user(ro_name, target_user))


def shop_action_item_permission_query_conditions(user: str | None = None) -> str:
	should_scope, emp = _get_scoped_employee_for_query(user)
	if not should_scope:
		return ""
	if not emp:
		return "1=0"
	return (
		"(`tabShop Action Item`.`reference_doctype` = 'Repair Order' "
		"AND EXISTS (SELECT 1 FROM `tabRepair Order` _ro "
		"WHERE _ro.`name` = `tabShop Action Item`.`reference_name` "
		f"AND {_ro_eligible_sql('_ro', emp)}))"
	)


def shop_action_item_has_permission(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	_ = ptype
	target_user = _resolve_user(user)
	if not target_user or target_user == "Guest":
		return False
	if is_manager_or_advisor(target_user) or not is_technician_scoped_user(target_user):
		return True
	if doc is None:
		return True

	if isinstance(doc, str):
		row = frappe.db.get_value(
			"Shop Action Item",
			doc,
			["reference_doctype", "reference_name"],
			as_dict=True,
		)
		ref_dt = row.get("reference_doctype") if row else None
		ref_name = row.get("reference_name") if row else None
	else:
		ref_dt = getattr(doc, "reference_doctype", None)
		ref_name = getattr(doc, "reference_name", None)

	return bool(ref_dt == "Repair Order" and ref_name and is_ro_visible_to_user(ref_name, target_user))


__all__ = [
	"MANAGER_ROLES",
	"TECH_VISIBLE_APPROVAL_STATUSES",
	"TECH_VISIBLE_RO_STATUSES",
	"assert_can_access_ro",
	"assert_can_access_schedule_entry",
	"get_eligible_ro_names_for_employee",
	"get_employee_for_user",
	"get_realtime_recipients_for_ro",
	"get_realtime_recipients_for_schedule_entry",
	"is_manager_or_advisor",
	"is_ro_visible_to_user",
	"is_schedule_entry_assigned_to_user",
	"is_technician_scoped_user",
	"repair_order_has_permission",
	"repair_order_permission_query_conditions",
	"schedule_entry_has_permission",
	"schedule_entry_permission_query_conditions",
	"shop_action_item_has_permission",
	"shop_action_item_permission_query_conditions",
	"shop_board_event_has_permission",
	"shop_board_event_permission_query_conditions",
	"shop_board_thread_has_permission",
	"shop_board_thread_permission_query_conditions",
]
