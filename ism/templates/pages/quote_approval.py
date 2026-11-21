# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _

from ism.api.approval import get_public_quote_approval
from ism.portal.auth import exchange_ro_approval_token, get_public_portal_branding

no_cache = 1


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
	context.full_width = True
	raw_token = frappe.form_dict.get("token")
	token = raw_token.strip() if isinstance(raw_token, str) else ""

	try:
		res = get_public_quote_approval(token)
	except frappe.ValidationError as exc:
		res = {"valid": False, "error": str(exc) or _("Invalid or missing approval token.")}
	except Exception:
		frappe.log_error(
			title="Quote Approval Portal Load Error",
			message=frappe.get_traceback(),
		)
		res = {"valid": False, "error": _("Unable to load repair estimate authorization at this time.")}

	csrf_token = frappe.sessions.get_csrf_token()
	branding = get_public_portal_branding()

	authenticated = False
	active_ro: str | None = None
	token_error: str | None = None

	if res.get("valid") and token:
		ro_info = res.get("repair_order") or {}
		active_ro = ro_info.get("name") or None
		try:
			exchange = exchange_ro_approval_token(token, set_response_cookie=True)
			if exchange.get("valid"):
				authenticated = True
				active_ro = exchange.get("repair_order") or active_ro
				if not getattr(frappe.flags, "in_test", False):
					frappe.db.commit()
			else:
				token_error = exchange.get("error") or _("This estimate link is invalid or has expired.")
		except (frappe.ValidationError, frappe.TooManyRequestsError) as exc:
			token_error = str(exc) or _("This estimate link is invalid or has expired.")
		except Exception:
			frappe.log_error(
				title="Quote Approval Session Exchange Error",
				message=frappe.get_traceback(),
			)
			token_error = _("Unable to start your portal session right now.")
	else:
		token_error = res.get("error") or _("This estimate link is invalid or has expired.")

	context.token = token
	context.approval_data = res
	context.csrf_token = csrf_token
	context.branding = branding
	context.portal_boot = {
		"authenticated": authenticated,
		"shop": branding,
		"token_error": token_error,
		"should_strip_query_token": bool(token),
		"initial_section": "quote",
		"active_ro": active_ro if authenticated else None,
		"active_schedule_entry": None,
		"csrf_token": csrf_token,
		"entry_route": "quote_approval",
	}
	context.title = _("Repair Estimate Authorization")
