# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from ism.api.approval import get_public_quote_approval


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
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

	context.token = token
	context.approval_data = res
	context.title = _("Repair Estimate Authorization")
