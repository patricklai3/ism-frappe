import json
import os
import frappe
from frappe.modules.export_file import export_to_files


def sync_workspace(workspace_name="ISM"):
	"""Ensure the ISM Workspace and Workspace Sidebar exist and include all module DocTypes."""
	if not frappe.db.exists("Workspace", workspace_name):
		# Let Frappe sync standard items first if during migrate
		from frappe.model.sync import sync_for
		sync_for("ism")

	if not frappe.db.exists("Workspace", workspace_name):
		return

	# 1. Fetch custom DocTypes belonging to ISM module (excluding child tables)
	ism_doctypes = frappe.get_all(
		"DocType",
		filters={"module": "ISM", "istable": 0},
		pluck="name",
		order_by="name asc",
	)

	# 2. Update Workspace links
	workspace = frappe.get_doc("Workspace", workspace_name)
	existing_links = {link.link_to for link in workspace.links if link.type == "Link" and link.link_to}

	card_break_idx = None
	for idx, link in enumerate(workspace.links):
		if link.type == "Card Break" and link.label == "ISM DocTypes":
			card_break_idx = idx
			break

	if card_break_idx is None:
		workspace.append(
			"links",
			{
				"label": "ISM DocTypes",
				"type": "Card Break",
				"link_count": 0,
				"hidden": 0,
				"onboard": 0,
			},
		)
		card_break_idx = len(workspace.links) - 1

	added_count = 0
	for dt in ism_doctypes:
		if dt not in existing_links:
			workspace.append(
				"links",
				{
					"label": dt,
					"type": "Link",
					"link_type": "DocType",
					"link_to": dt,
					"onboard": 0,
					"hidden": 0,
				},
			)
			added_count += 1

	# Recalculate link count for ISM DocTypes card
	ism_card_links = [
		link for link in workspace.links
		if link.type == "Link" and link.link_to in ism_doctypes
	]
	workspace.links[card_break_idx].link_count = len(ism_card_links)

	workspace.save(ignore_permissions=True)

	# 3. Update Workspace Sidebar
	if frappe.db.exists("Workspace Sidebar", workspace_name):
		sidebar = frappe.get_doc("Workspace Sidebar", workspace_name)
		sidebar_links = {item.link_to for item in sidebar.items if item.link_to}

		has_ism_section = any(
			item.type == "Section Break" and item.label == "ISM DocTypes"
			for item in sidebar.items
		)
		if not has_ism_section and ism_doctypes:
			sidebar.append(
				"items",
				{
					"label": "ISM DocTypes",
					"type": "Section Break",
					"child": 0,
					"collapsible": 1,
					"show_arrow": 0,
				},
			)

		for dt in ism_doctypes:
			if dt not in sidebar_links:
				sidebar.append(
					"items",
					{
						"label": dt,
						"type": "Link",
						"link_type": "DocType",
						"link_to": dt,
						"child": 1,
						"collapsible": 1,
						"icon": "file",
						"show_arrow": 0,
					},
				)

		sidebar.save(ignore_permissions=True)

	# 4. In developer mode, export updated definitions back to files
	if frappe.conf.developer_mode:
		try:
			export_to_files(record_list=[["Workspace", workspace_name]])
		except Exception:
			pass


def sync_doctype_to_workspace(doc, method=None):
	"""Hook triggered on DocType after_insert and on_trash."""
	if doc.module == "ISM" and not doc.istable:
		sync_workspace()
