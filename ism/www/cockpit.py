# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frappe web page controller for the ISM Shop OS Executive & Dispatch Cockpit (/cockpit)."""

from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe import _

no_cache = 1

ALLOWED_COCKPIT_ROLES: frozenset[str] = frozenset(
	{
		"ISM Manager",
		"ISM Service Advisor",
		"System Manager",
	}
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _safe_json_for_script(payload: Any) -> str:
	"""Serialize JSON safely for inline <script> embedding."""
	return (
		json.dumps(payload, default=str, ensure_ascii=False)
		.replace("<", "\\u003c")
		.replace(">", "\\u003e")
		.replace("&", "\\u0026")
	)


def _sanitize_date(raw: Any) -> str:
	val = str(raw or "").strip()
	return val if _DATE_RE.match(val) else ""


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False

	user = frappe.session.user
	if not user or user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/cockpit"
		raise frappe.Redirect

	roles = sorted(set(frappe.get_roles(user)))
	if user != "Administrator" and not (set(roles) & ALLOWED_COCKPIT_ROLES):
		frappe.local.flags.redirect_location = "/board"
		raise frappe.Redirect

	full_name = frappe.utils.get_fullname(user) or user
	initial_date = _sanitize_date(frappe.form_dict.get("date"))

	initial_snapshot = None
	try:
		from ism.board import cockpit_view

		initial_snapshot = cockpit_view.get_cockpit_view(date=initial_date or None)
	except Exception:
		frappe.log_error(
			title="ISM Cockpit Initial Snapshot Error",
			message=frappe.get_traceback(),
		)
		initial_snapshot = None

	socketio_port = frappe.conf.get("socketio_port")
	boot_payload = {
		"csrf_token": frappe.sessions.get_csrf_token(),
		"user": user,
		"full_name": full_name,
		"roles": roles,
		"initial_date": initial_date,
		"site_name": getattr(frappe.local, "site", ""),
		"socketio_port": socketio_port,
		"realtime": {
			"socketio_port": socketio_port,
			"site_name": getattr(frappe.local, "site", ""),
			"events": [
				"ism_board_thread_summary",
				"ism_board_projection",
				"ism_board_event",
				"ism_board_ai_status",
			],
		},
		"initial_snapshot": initial_snapshot,
	}

	context.title = _("ISM Shop OS — Cockpit")
	context.user_full_name = full_name
	context.user_role_label = (
		"Manager"
		if ("ISM Manager" in roles or "System Manager" in roles or user == "Administrator")
		else "Service Advisor"
	)
	context.cockpit_boot_json = _safe_json_for_script(boot_payload)
	return context
