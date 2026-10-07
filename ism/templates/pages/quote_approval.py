# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from ism.api.approval import get_public_quote_approval


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
	token = frappe.form_dict.get("token") or ""

	res = get_public_quote_approval(token)
	context.token = token
	context.approval_data = res
	context.title = _("Repair Estimate Authorization")
