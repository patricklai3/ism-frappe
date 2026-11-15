# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe

from ism.setup import backfill_service_package_models, cleanup_legacy_custom_fields

PERM_FIELDS = (
	"permlevel",
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"share",
	"print",
	"email",
	"select",
	"if_owner",
)


def execute():
	"""Run one-time legacy cleanup, ISM Settings backfill, and Custom DocPerm repair."""
	cleanup_legacy_custom_fields()
	backfill_service_package_models()
	_normalize_ism_settings_fields()
	_repair_partial_custom_docperms()


def _normalize_ism_settings_fields():
	"""Migrate deprecated sms_gateway_provider values and initialize default_labor_rate."""
	provider = frappe.db.get_single_value("ISM Settings", "sms_gateway_provider")
	if provider == "Twilio":
		frappe.db.set_single_value("ISM Settings", "sms_gateway_provider", "Disabled")

	if not frappe.db.get_single_value("ISM Settings", "default_labor_rate"):
		frappe.db.set_single_value("ISM Settings", "default_labor_rate", 140.0)

	frappe.clear_cache(doctype="ISM Settings")


def _repair_partial_custom_docperms():
	"""Remove leftover MCP test DocPerms and backfill standard DocPerms where shadowed."""
	cleared_doctypes = set(
		frappe.get_all("Custom DocPerm", filters={"role": "MCP Desk Tester"}, pluck="parent")
	)
	if cleared_doctypes:
		frappe.db.delete("Custom DocPerm", {"role": "MCP Desk Tester"})

	remaining_doctypes = set(frappe.get_all("Custom DocPerm", pluck="parent"))
	for dt in remaining_doctypes:
		has_std_sysmgr = frappe.db.exists("DocPerm", {"parent": dt, "role": "System Manager"})
		has_custom_sysmgr = frappe.db.exists("Custom DocPerm", {"parent": dt, "role": "System Manager"})
		if not has_std_sysmgr or has_custom_sysmgr:
			continue

		existing_pairs = {
			(row.role, int(row.permlevel or 0), int(row.if_owner or 0))
			for row in frappe.get_all(
				"Custom DocPerm",
				filters={"parent": dt},
				fields=["role", "permlevel", "if_owner"],
			)
		}
		docperms = frappe.get_all(
			"DocPerm",
			filters={"parent": dt},
			fields=["role", *PERM_FIELDS],
		)
		for perm in docperms:
			key = (perm.role, int(perm.permlevel or 0), int(perm.if_owner or 0))
			if key in existing_pairs:
				continue
			payload = {
				"doctype": "Custom DocPerm",
				"parent": dt,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": perm.role,
			}
			for fieldname in PERM_FIELDS:
				payload[fieldname] = perm.get(fieldname)
			frappe.get_doc(payload).insert(ignore_permissions=True)

		cleared_doctypes.add(dt)

	for dt in cleared_doctypes:
		frappe.clear_cache(doctype=dt)
