# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Idempotent post_model_sync migration consolidating Service Package and RO equipment tags."""

from __future__ import annotations

import frappe

from ism.scheduling.capacity import format_required_tags, load_package_tags_map, parse_required_tags
from ism.setup_tasks.seed import seed_equipment_tags


def _load_existing_package_tag_state() -> tuple[set[tuple[str, str]], dict[str, int]]:
	rows = frappe.get_all(
		"Service Package Equipment",
		filters={"parenttype": "Service Package"},
		fields=["parent", "equipment_tag", "idx"],
	)
	existing_pairs: set[tuple[str, str]] = set()
	max_idx: dict[str, int] = {}
	for row in rows:
		parent = row.get("parent")
		tag = (row.get("equipment_tag") or "").strip()
		if parent and tag:
			existing_pairs.add((parent, tag))
		if parent:
			idx = int(row.get("idx") or 0)
			if idx > max_idx.get(parent, 0):
				max_idx[parent] = idx
	return existing_pairs, max_idx


def _attach_package_tag(
	pkg_name: str,
	tag: str,
	valid_tags: set[str],
	existing_pairs: set[tuple[str, str]],
	max_idx: dict[str, int],
) -> None:
	if not pkg_name or tag not in valid_tags or (pkg_name, tag) in existing_pairs:
		return
	next_idx = max_idx.get(pkg_name, 0) + 1
	max_idx[pkg_name] = next_idx
	existing_pairs.add((pkg_name, tag))
	frappe.get_doc(
		{
			"doctype": "Service Package Equipment",
			"parent": pkg_name,
			"parenttype": "Service Package",
			"parentfield": "equipment_tags",
			"idx": next_idx,
			"equipment_tag": tag,
		}
	).db_insert()


def _backfill_repair_order_tags() -> None:
	if not (
		frappe.db.exists("DocType", "Repair Order")
		and frappe.db.exists("DocType", "Repair Order Service")
		and frappe.db.has_column("Repair Order Service", "required_equipment_tags")
		and frappe.db.has_column("Repair Order", "required_equipment_tags")
	):
		return

	svc_rows = frappe.get_all(
		"Repair Order Service",
		filters={"parenttype": "Repair Order"},
		fields=["name", "parent", "service_package", "required_equipment_tags"],
	)
	missing_pkg_names = [
		row.service_package
		for row in svc_rows
		if row.service_package and not (row.required_equipment_tags or "").strip()
	]
	pkg_tags_map = load_package_tags_map(missing_pkg_names) if missing_pkg_names else {}

	ro_tags: dict[str, set[str]] = {}
	for row in svc_rows:
		existing_str = (row.required_equipment_tags or "").strip()
		if existing_str:
			tags = parse_required_tags(existing_str)
		elif row.service_package:
			tags = pkg_tags_map.get(row.service_package, [])
			formatted = format_required_tags(tags)
			if formatted:
				frappe.db.set_value(
					"Repair Order Service",
					row.name,
					"required_equipment_tags",
					formatted,
					update_modified=False,
				)
		else:
			tags = []

		if row.parent and tags:
			ro_tags.setdefault(row.parent, set()).update(tags)

	for ro in frappe.get_all("Repair Order", fields=["name", "required_equipment_tags"]):
		desired = format_required_tags(ro_tags.get(ro.name, set()))
		current = (ro.required_equipment_tags or "").strip()
		if current != desired:
			frappe.db.set_value(
				"Repair Order",
				ro.name,
				"required_equipment_tags",
				desired,
				update_modified=False,
			)


def execute():
	if not (
		frappe.db.exists("DocType", "Service Package")
		and frappe.db.exists("DocType", "Service Package Equipment")
		and frappe.db.exists("DocType", "Equipment Tag")
	):
		return

	seed_equipment_tags()
	valid_tags = set(frappe.get_all("Equipment Tag", pluck="name"))
	existing_pairs, max_idx = _load_existing_package_tag_state()

	if frappe.db.has_column("Service Package", "self_serve_required_tags"):
		legacy_tag_rows = frappe.db.sql(
			"""
			SELECT name, self_serve_required_tags
			FROM `tabService Package`
			WHERE IFNULL(self_serve_required_tags, '') != ''
			""",
			as_dict=True,
		)
		for row in legacy_tag_rows:
			for tag in parse_required_tags(row.get("self_serve_required_tags")):
				_attach_package_tag(row["name"], tag, valid_tags, existing_pairs, max_idx)

	if frappe.db.has_column("Service Package", "is_mobile_capable") and "Lift" in valid_tags:
		non_mobile_manual_rows = frappe.db.sql(
			"""
			SELECT name
			FROM `tabService Package`
			WHERE IFNULL(tesla_manual_url, '') != ''
			  AND IFNULL(is_mobile_capable, 0) = 0
			""",
			as_dict=True,
		)
		for row in non_mobile_manual_rows:
			_attach_package_tag(row["name"], "Lift", valid_tags, existing_pairs, max_idx)

	_backfill_repair_order_tags()
