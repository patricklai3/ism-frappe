# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frappe web page controller for the 3-Layer Technician Execution Workbench (/tech)."""

from __future__ import annotations

import json
from urllib.parse import quote, unquote

import frappe
from frappe import _

from ism.api import tech as tech_api
from ism.board.gateways import resolve_actor_role
from ism.permissions import (
	get_employee_for_user,
	is_ro_visible_to_user,
	is_schedule_entry_assigned_to_user,
)
from ism.utils.web_security import apply_page_security_headers, compute_asset_version

no_cache = 1

_TECH_ASSET_PATHS: tuple[str, ...] = (
	"board/vendor/socket.io.min.js",
	"board/board.css",
	"board/api.js",
	"board/realtime.js",
	"board/capture.js",
	"board/cards.js",
	"tech/tech.css",
	"tech/api.js",
	"tech/app.js",
)


def _build_login_redirect() -> str:
	target = "/tech"
	try:
		req = getattr(frappe, "request", None)
		if req is not None:
			full_path = getattr(req, "full_path", "") or getattr(req, "path", "") or "/tech"
			full_path = full_path.rstrip("?")
			if full_path.startswith("/tech"):
				target = full_path
	except Exception:
		target = "/tech"
	return f"/login?redirect-to={quote(target, safe='')}"


def _clean_param(key: str) -> str:
	raw = None
	try:
		form_dict = getattr(frappe, "form_dict", None)
		if form_dict is not None:
			raw = form_dict.get(key)
	except Exception:
		raw = None
	if not raw:
		try:
			req = getattr(frappe, "request", None)
			args = getattr(req, "args", None) if req is not None else None
			if args is not None:
				raw = args.get(key)
		except Exception:
			raw = None
	if not raw:
		return ""
	return unquote(str(raw)).strip()


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
	context.full_width = True
	context.title = _("ISM Technician Workbench")
	apply_page_security_headers(context)
	context.asset_version = compute_asset_version(_TECH_ASSET_PATHS)

	caller_user = frappe.session.user
	if not caller_user or caller_user == "Guest":
		frappe.local.flags.redirect_location = _build_login_redirect()
		raise frappe.Redirect

	try:
		actor_role = resolve_actor_role(caller_user)
	except frappe.PermissionError as exc:
		context.authorized = False
		context.error_message = str(exc) or _(
			"You do not have permission to access the ISM Technician Workbench."
		)
		context.can_manage = False
		context.can_access_cockpit = False
		context.user_full_name = frappe.utils.get_fullname(caller_user) or caller_user
		context.actor_role = "UNAUTHORIZED"
		context.tech_id = None
		context.employee_linked = False
		context.boot_json = "{}"
		return

	can_manage = actor_role == "MANAGER"
	can_access_cockpit = actor_role == "MANAGER"
	tech_id = get_employee_for_user(caller_user)
	employee_linked = bool(tech_id)
	user_full_name = frappe.utils.get_fullname(caller_user) or caller_user
	csrf_token = frappe.sessions.get_csrf_token()

	raw_ro = _clean_param("ro") or _clean_param("ro_name") or _clean_param("repair_order")
	raw_se = _clean_param("se") or _clean_param("schedule_entry")
	raw_view = _clean_param("view").lower()

	initial_kind: str | None = None
	initial_id: str | None = None
	initial_view: str = "list"
	initial_error: str | None = None

	if raw_ro:
		if is_ro_visible_to_user(raw_ro, user=caller_user):
			initial_kind = "REPAIR_ORDER"
			initial_id = raw_ro
			initial_view = "board" if raw_view == "board" else "detail"
		else:
			initial_error = _(
				"Repair Order {0} is not assigned to you or is not yet ready for bay execution."
			).format(raw_ro)
	elif raw_se:
		if not is_schedule_entry_assigned_to_user(raw_se, user=caller_user):
			initial_error = _(
				"You do not have permission to access Schedule Entry {0}."
			).format(raw_se)
		else:
			linked_ro = frappe.db.get_value("Schedule Entry", raw_se, "repair_order")
			if linked_ro and is_ro_visible_to_user(linked_ro, user=caller_user):
				frappe.local.flags.redirect_location = f"/tech?ro={quote(str(linked_ro), safe='')}"
				raise frappe.Redirect
			initial_kind = "SCHEDULE_ENTRY"
			initial_id = raw_se
			initial_view = "detail"

	initial_jobs = []
	active_count = 0
	completed_count = 0
	try:
		listing = tech_api.list_my_jobs(include_completed=False, limit=100)
		initial_jobs = listing.get("jobs") or []
		active_count = int(listing.get("active_count") or 0)
		completed_count = int(listing.get("completed_count") or 0)
	except Exception:
		frappe.log_error(
			title="Tech Workbench Boot Job List Error",
			message=frappe.get_traceback(),
		)

	boot_data = {
		"user": caller_user,
		"full_name": user_full_name,
		"actor_role": actor_role,
		"can_manage": can_manage,
		"can_access_cockpit": can_access_cockpit,
		"tech_id": tech_id,
		"employee_linked": employee_linked,
		"csrf_token": csrf_token,
		"sitename": getattr(frappe.local, "site", None) or "",
		"socketio_port": frappe.conf.get("socketio_port") or 9000,
		"initial_kind": initial_kind,
		"initial_id": initial_id,
		"initial_view": initial_view,
		"initial_error": initial_error,
		"initial_jobs": initial_jobs,
		"active_count": active_count,
		"completed_count": completed_count,
	}

	context.authorized = True
	context.actor_role = actor_role
	context.can_manage = can_manage
	context.can_access_cockpit = can_access_cockpit
	context.user_full_name = user_full_name
	context.tech_id = tech_id
	context.employee_linked = employee_linked
	context.csrf_token = csrf_token
	context.boot_json = json.dumps(boot_data, default=str).replace("</", "<\\/")
