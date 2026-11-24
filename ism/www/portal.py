# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frappe web page controller for the Unified Customer Portal (/portal)."""

from __future__ import annotations

import frappe
from frappe import _

from ism.portal.auth import (
	exchange_booking_token,
	exchange_ro_approval_token,
	get_active_portal_session,
	get_public_portal_branding,
)
from ism.utils.web_security import (
	PORTAL_ASSET_PATHS,
	apply_page_security_headers,
	compute_asset_version,
)

no_cache = 1

_INVALID_LINK_SIGNIN_MSG = _(
	"This link is invalid or has expired. Sign in below with your mobile phone number."
)


def _clean_query_str(key: str) -> str:
	raw_val = frappe.form_dict.get(key)
	return raw_val.strip() if isinstance(raw_val, str) else ""


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
	context.full_width = True
	context.title = _("Customer Portal")
	apply_page_security_headers(context)
	context.asset_version = compute_asset_version(PORTAL_ASSET_PATHS)

	csrf_token = frappe.sessions.get_csrf_token()
	branding = get_public_portal_branding()

	token = _clean_query_str("token")
	booking_token = _clean_query_str("booking_token")
	requested_ro = _clean_query_str("ro") or None
	requested_se = _clean_query_str("schedule_entry") or None

	authenticated = False
	token_error: str | None = None
	should_strip_query_token = False
	active_ro: str | None = None
	active_schedule_entry: str | None = None

	if token:
		should_strip_query_token = True
		try:
			exchange = exchange_ro_approval_token(token, set_response_cookie=True)
			if exchange.get("valid"):
				authenticated = True
				active_ro = exchange.get("repair_order")
				if not getattr(frappe.flags, "in_test", False):
					frappe.db.commit()
			else:
				token_error = _INVALID_LINK_SIGNIN_MSG
		except (frappe.ValidationError, frappe.TooManyRequestsError) as exc:
			token_error = str(exc) or _INVALID_LINK_SIGNIN_MSG
		except Exception:
			frappe.log_error(
				title="Customer Portal RO Token Exchange Error",
				message=frappe.get_traceback(),
			)
			token_error = _INVALID_LINK_SIGNIN_MSG
	elif booking_token:
		should_strip_query_token = True
		try:
			exchange = exchange_booking_token(booking_token, set_response_cookie=True)
			if exchange.get("valid"):
				authenticated = True
				active_schedule_entry = exchange.get("schedule_entry")
				active_ro = exchange.get("repair_order")
				if not getattr(frappe.flags, "in_test", False):
					frappe.db.commit()
			else:
				token_error = _INVALID_LINK_SIGNIN_MSG
		except (frappe.ValidationError, frappe.TooManyRequestsError) as exc:
			token_error = str(exc) or _INVALID_LINK_SIGNIN_MSG
		except Exception:
			frappe.log_error(
				title="Customer Portal Booking Token Exchange Error",
				message=frappe.get_traceback(),
			)
			token_error = _INVALID_LINK_SIGNIN_MSG
	else:
		session = get_active_portal_session(touch=True)
		if session:
			authenticated = True
			active_ro = requested_ro or getattr(session, "repair_order", None)
			active_schedule_entry = requested_se or getattr(session, "schedule_entry", None)

	portal_boot = {
		"authenticated": authenticated,
		"demo_mode": bool(branding.get("demo_mode")),
		"shop": branding,
		"token_error": token_error,
		"should_strip_query_token": should_strip_query_token,
		"initial_section": None,
		"active_ro": active_ro if authenticated else None,
		"active_schedule_entry": active_schedule_entry if authenticated else None,
		"csrf_token": csrf_token,
		"entry_route": "portal",
	}

	context.csrf_token = csrf_token
	context.branding = branding
	context.portal_boot = portal_boot
