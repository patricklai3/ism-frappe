# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frappe web page controller for the Shop OS Blackboard (/board)."""

from __future__ import annotations

import json
from urllib.parse import quote, unquote

import frappe
from frappe import _

from ism.api import board as board_api
from ism.board.gateways import resolve_actor_role
from ism.board.projections import resolve_thread_key
from ism.utils.web_security import apply_page_security_headers, compute_asset_version

no_cache = 1

_BOARD_ASSET_PATHS: tuple[str, ...] = (
	"board/vendor/socket.io.min.js",
	"board/board.css",
	"board/api.js",
	"board/realtime.js",
	"board/capture.js",
	"board/cards.js",
	"board/app.js",
)


def _build_login_redirect() -> str:
	target = "/board"
	try:
		req = getattr(frappe, "request", None)
		if req is not None:
			full_path = getattr(req, "full_path", "") or getattr(req, "path", "") or "/board"
			full_path = full_path.rstrip("?")
			if full_path.startswith("/board"):
				target = full_path
	except Exception:
		target = "/board"
	return f"/login?redirect-to={quote(target, safe='')}"


def _resolve_initial_thread_key() -> tuple[str | None, str | None]:
	raw_thread = (
		frappe.form_dict.get("thread_key")
		or frappe.form_dict.get("thread")
		or ""
	)
	raw_ro = (
		frappe.form_dict.get("ro")
		or frappe.form_dict.get("repair_order")
		or ""
	)

	thread_val = unquote(str(raw_thread)).strip() if raw_thread else None
	ro_val = unquote(str(raw_ro)).strip() if raw_ro else None
	if not thread_val and not ro_val:
		return None, None

	try:
		canonical = resolve_thread_key(thread_key=thread_val, repair_order=ro_val)
		return canonical, None
	except Exception as exc:
		return None, str(exc)


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
	context.full_width = True
	context.title = _("ISM Shop Floor Blackboard")
	apply_page_security_headers(context)
	context.asset_version = compute_asset_version(_BOARD_ASSET_PATHS)

	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = _build_login_redirect()
		raise frappe.Redirect

	try:
		actor_role = resolve_actor_role(frappe.session.user)
	except frappe.PermissionError as exc:
		context.authorized = False
		context.error_message = str(exc) or _(
			"You do not have permission to access the ISM Shop Floor Blackboard."
		)
		context.can_access_cockpit = False
		context.user_full_name = frappe.utils.get_fullname(frappe.session.user) or frappe.session.user
		context.actor_role = "UNAUTHORIZED"
		context.boot_json = "{}"
		return

	can_manage = actor_role == "MANAGER"
	can_access_cockpit = actor_role == "MANAGER"
	tech_id = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")
	user_full_name = frappe.utils.get_fullname(frappe.session.user) or frappe.session.user
	csrf_token = frappe.sessions.get_csrf_token()

	initial_thread_key, initial_thread_error = _resolve_initial_thread_key()

	initial_threads = []
	try:
		listing = board_api.list_board_threads(status="OPEN", limit=100)
		initial_threads = listing.get("threads") or []
	except Exception:
		frappe.log_error(
			title="Shop Board Boot Thread List Error",
			message=frappe.get_traceback(),
		)

	initial_action_feed = []
	try:
		feed_fn = getattr(board_api, "list_board_action_feed", None)
		if callable(feed_fn):
			feed_res = feed_fn(status="Open", limit=50)
			initial_action_feed = feed_res.get("items") or []
	except Exception:
		frappe.log_error(
			title="Shop Board Boot Action Feed Error",
			message=frappe.get_traceback(),
		)

	boot_data = {
		"user": frappe.session.user,
		"full_name": user_full_name,
		"actor_role": actor_role,
		"can_manage": can_manage,
		"can_approve": can_manage,
		"can_access_cockpit": can_access_cockpit,
		"tech_id": tech_id,
		"csrf_token": csrf_token,
		"sitename": getattr(frappe.local, "site", None) or "",
		"socketio_port": frappe.conf.get("socketio_port") or 9000,
		"initial_thread_key": initial_thread_key,
		"initial_thread_error": initial_thread_error,
		"initial_threads": initial_threads,
		"initial_action_feed": initial_action_feed,
	}

	context.authorized = True
	context.actor_role = actor_role
	context.can_manage = can_manage
	context.can_approve = can_manage
	context.can_access_cockpit = can_access_cockpit
	context.user_full_name = user_full_name
	context.tech_id = tech_id
	context.csrf_token = csrf_token
	context.boot_json = json.dumps(boot_data, default=str).replace("</", "<\\/")
