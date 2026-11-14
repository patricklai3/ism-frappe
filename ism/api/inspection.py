# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

DEFAULT_INSPECTION_TEMPLATE = "Standard Vehicle Walkaround"
VALID_INSPECTION_STATUSES = ("Pending", "Pass", "Attention", "Fail", "N/A")
_STATUS_ALIASES = {
	"pending": "Pending",
	"pass": "Pass",
	"passed": "Pass",
	"ok": "Pass",
	"good": "Pass",
	"green": "Pass",
	"attention": "Attention",
	"needs attention": "Attention",
	"warn": "Attention",
	"warning": "Attention",
	"fair": "Attention",
	"monitor": "Attention",
	"yellow": "Attention",
	"amber": "Attention",
	"fail": "Fail",
	"failed": "Fail",
	"bad": "Fail",
	"issue": "Fail",
	"damaged": "Fail",
	"replace": "Fail",
	"red": "Fail",
	"n/a": "N/A",
	"na": "N/A",
	"not applicable": "N/A",
}


@frappe.whitelist()
def list_inspection_templates(include_inactive: bool | int | str = False) -> dict:
	"""List available vehicle check-in inspection templates with point counts."""
	frappe.has_permission("Inspection Template", "read", throw=True)
	filters = {} if _as_bool(include_inactive) else {"is_active": 1}

	rows = frappe.get_all(
		"Inspection Template",
		filters=filters,
		fields=["name", "template_name", "is_active", "description"],
		order_by="template_name asc",
	)

	item_counts: dict[str, int] = {}
	if rows:
		template_names = [row.name for row in rows]
		child_rows = frappe.get_all(
			"Inspection Template Item",
			filters={"parent": ["in", template_names], "parenttype": "Inspection Template"},
			fields=["parent"],
		)
		for child in child_rows:
			item_counts[child.parent] = item_counts.get(child.parent, 0) + 1

	templates = [
		{
			"name": row.name,
			"template_name": row.template_name,
			"is_active": bool(cint(row.is_active)),
			"description": row.description,
			"item_count": item_counts.get(row.name, 0),
		}
		for row in rows
	]
	return {"templates": templates, "count": len(templates)}


@frappe.whitelist()
def get_inspection_template(template_name: str | None = None) -> dict:
	"""Retrieve an Inspection Template and its ordered checklist items."""
	frappe.has_permission("Inspection Template", "read", throw=True)
	resolved_name = _resolve_template_name(template_name)
	doc = frappe.get_doc("Inspection Template", resolved_name)

	return {
		"name": doc.name,
		"template_name": doc.template_name,
		"is_active": bool(cint(getattr(doc, "is_active", 1))),
		"description": getattr(doc, "description", None),
		"items": [
			{
				"name": row.name,
				"idx": row.idx,
				"inspection_description": row.inspection_description,
				"category": getattr(row, "category", None),
				"photo_required": bool(cint(getattr(row, "photo_required", 0))),
				"default_notes": getattr(row, "default_notes", None),
			}
			for row in getattr(doc, "items", [])
		],
	}


@frappe.whitelist()
def apply_inspection_template(
	repair_order: str,
	template_name: str | None = None,
	replace_existing: bool | int | str = False,
) -> dict:
	"""Apply an Inspection Template's checklist items onto Repair Order.inspection_items."""
	ro = _get_repair_order(repair_order, "write")
	resolved_name = _resolve_template_name(template_name or getattr(ro, "inspection_template", None))
	template_doc = frappe.get_doc("Inspection Template", resolved_name)

	ro.inspection_template = template_doc.name
	if _as_bool(replace_existing):
		ro.set("inspection_items", [])

	added_count, skipped_count = _merge_template_items(ro, template_doc)
	ro.save()

	payload = _build_inspection_payload(ro)
	payload.update({
		"template_name": template_doc.name,
		"added_count": added_count,
		"skipped_count": skipped_count,
	})
	return payload


@frappe.whitelist()
def get_check_in_inspection(repair_order: str) -> dict:
	"""Return current check-in inspection state and summary for a Repair Order."""
	ro = _get_repair_order(repair_order, "read")
	return _build_inspection_payload(ro)


@frappe.whitelist()
def record_check_in_inspection(
	repair_order: str,
	items: list | str | None = None,
	odometer_in: int | float | str | None = None,
	odometer: int | float | str | None = None,
	customer_concern: str | None = None,
	template_name: str | None = None,
	auto_apply_template: bool | int | str = False,
) -> dict:
	"""Record or update vehicle check-in intake fields and inspection item findings."""
	ro = _get_repair_order(repair_order, "write")

	if _as_bool(auto_apply_template):
		resolved_tpl = _resolve_template_name(template_name or getattr(ro, "inspection_template", None))
		template_doc = frappe.get_doc("Inspection Template", resolved_tpl)
		ro.inspection_template = template_doc.name
		_merge_template_items(ro, template_doc)
	elif template_name and isinstance(template_name, str) and template_name.strip():
		ro.inspection_template = _resolve_template_name(template_name)

	raw_odo = odometer_in if odometer_in is not None else odometer
	if raw_odo is not None and raw_odo != "":
		ro.odometer_in = _parse_odometer(raw_odo)

	if customer_concern is not None:
		ro.customer_concern = customer_concern.strip() if isinstance(customer_concern, str) else customer_concern

	parsed_items = _parse_items_input(items)
	updated_count = 0
	added_count = 0
	stamp = now_datetime()

	for entry in parsed_items:
		row, created = _find_or_create_inspection_row(ro, entry)
		_apply_item_findings(ro, row, entry, stamp)
		if created:
			added_count += 1
		else:
			updated_count += 1

	ro.save()
	payload = _build_inspection_payload(ro)
	payload.update({
		"updated_count": updated_count,
		"added_count": added_count,
	})
	return payload


def _get_repair_order(repair_order: str, ptype: str):
	if not isinstance(repair_order, str) or not repair_order.strip():
		frappe.throw(_("Repair Order ID is required."))
	doc = frappe.get_doc("Repair Order", repair_order.strip())
	doc.check_permission(ptype)
	return doc


def _resolve_template_name(template_name: str | None = None) -> str:
	if isinstance(template_name, str) and template_name.strip():
		candidate = template_name.strip()
		if not frappe.db.exists("Inspection Template", candidate):
			frappe.throw(_("Inspection Template '{0}' does not exist.").format(candidate), frappe.DoesNotExistError)
		return candidate

	if frappe.db.exists("Inspection Template", DEFAULT_INSPECTION_TEMPLATE):
		is_active = frappe.db.get_value("Inspection Template", DEFAULT_INSPECTION_TEMPLATE, "is_active")
		if is_active is None or cint(is_active):
			return DEFAULT_INSPECTION_TEMPLATE

	fallback = frappe.get_all(
		"Inspection Template",
		filters={"is_active": 1},
		pluck="name",
		order_by="template_name asc",
		limit=1,
	)
	if fallback:
		return fallback[0]

	frappe.throw(_("No active Inspection Template is configured."), frappe.DoesNotExistError)


def _merge_template_items(ro, template_doc) -> tuple[int, int]:
	existing = {
		(row.inspection_description or "").strip().lower()
		for row in getattr(ro, "inspection_items", [])
		if (row.inspection_description or "").strip()
	}
	added_count = 0
	skipped_count = 0
	for tpl_item in getattr(template_doc, "items", []):
		desc = (getattr(tpl_item, "inspection_description", None) or "").strip()
		if not desc:
			continue
		if desc.lower() in existing:
			skipped_count += 1
			continue
		row_payload = {
			"inspection_description": desc,
			"status": "Pending",
		}
		if getattr(tpl_item, "default_notes", None):
			row_payload["notes"] = tpl_item.default_notes
		ro.append("inspection_items", row_payload)
		existing.add(desc.lower())
		added_count += 1
	return added_count, skipped_count


def _parse_items_input(items: list | str | None) -> list[dict]:
	if items is None:
		return []
	if isinstance(items, str):
		if not items.strip():
			return []
		try:
			items = json.loads(items)
		except Exception:
			frappe.throw(_("Invalid JSON format for inspection items."))
	if not isinstance(items, list):
		frappe.throw(_("Inspection items must be a list of objects."))
	for entry in items:
		if not isinstance(entry, dict):
			frappe.throw(_("Each inspection item entry must be a JSON object."))
	return items


def _find_or_create_inspection_row(ro, entry: dict):
	rows = getattr(ro, "inspection_items", [])
	row_id = entry.get("name") or entry.get("row_id")
	if row_id:
		for row in rows:
			if row.name == row_id:
				return row, False

	raw_idx = entry.get("idx")
	if raw_idx is not None and not isinstance(raw_idx, bool):
		target_idx = cint(raw_idx)
		for row in rows:
			if row.idx == target_idx:
				return row, False

	desc = entry.get("inspection_description")
	if isinstance(desc, str) and desc.strip():
		needle = desc.strip().lower()
		for row in rows:
			if (row.inspection_description or "").strip().lower() == needle:
				return row, False
		new_row = ro.append("inspection_items", {"inspection_description": desc.strip(), "status": "Pending"})
		return new_row, True

	frappe.throw(_("Each inspection update must identify an existing row (name or idx) or supply inspection_description."))


def _apply_item_findings(ro, row, entry: dict, stamp) -> None:
	touched = False
	if isinstance(entry.get("inspection_description"), str) and entry["inspection_description"].strip():
		row.inspection_description = entry["inspection_description"].strip()

	if "status" in entry and entry["status"] is not None:
		row.status = _normalize_inspection_status(entry["status"])
		touched = True

	if "condition" in entry:
		row.condition = entry["condition"]
		touched = True

	if "notes" in entry:
		row.notes = entry["notes"]
		touched = True

	if "image" in entry or "photos" in entry:
		photos = _collect_and_save_photos(ro, row, entry)
		row.photos = json.dumps(photos) if photos else None
		row.image = photos[0] if photos else None
		touched = True

	if touched:
		row.inspected_by = frappe.session.user
		row.inspected_at = stamp


def _collect_and_save_photos(ro, row, entry: dict) -> list[str]:
	from frappe.utils.file_manager import save_file

	incoming: list[str] = []
	if entry.get("image"):
		incoming.append(str(entry["image"]).strip())

	raw_photos = entry.get("photos")
	if isinstance(raw_photos, str):
		try:
			raw_photos = json.loads(raw_photos) if raw_photos.strip() else []
		except Exception:
			frappe.throw(_("Invalid JSON format for inspection photos."))
	if isinstance(raw_photos, list):
		for photo in raw_photos:
			if isinstance(photo, str) and photo.strip():
				incoming.append(photo.strip())

	# Preserve existing photos unless the caller explicitly passed a replacement photos list
	base_photos = [] if "photos" in entry else _parse_row_photos(row)
	saved_urls = list(base_photos)

	for idx, value in enumerate(incoming, start=1):
		if value.startswith("data:image"):
			file_doc = save_file(
				fname=f"inspect_{ro.name}_{row.idx or 'new'}_{int(now_datetime().timestamp())}_{idx}.png",
				content=value,
				dt="Repair Order",
				dn=ro.name,
				decode=True,
				is_private=0,
			)
			url = file_doc.file_url
		else:
			url = value
		if url and url not in saved_urls:
			saved_urls.append(url)

	return saved_urls


def _build_inspection_payload(ro) -> dict:
	items = []
	counts = {"pending": 0, "pass": 0, "attention": 0, "fail": 0, "na": 0}
	for row in getattr(ro, "inspection_items", []):
		status = row.status or "Pending"
		bucket = status.lower().replace("/", "")
		if bucket in counts:
			counts[bucket] += 1
		items.append({
			"name": row.name,
			"idx": row.idx,
			"inspection_description": row.inspection_description,
			"status": status,
			"condition": getattr(row, "condition", None),
			"notes": getattr(row, "notes", None),
			"image": getattr(row, "image", None),
			"photos": _parse_row_photos(row),
			"inspected_by": getattr(row, "inspected_by", None),
			"inspected_at": str(row.inspected_at) if getattr(row, "inspected_at", None) else None,
		})

	total = len(items)
	completed = total - counts["pending"]
	return {
		"repair_order": ro.name,
		"status": ro.status,
		"odometer_in": ro.odometer_in,
		"customer_concern": ro.customer_concern,
		"check_in_date": str(ro.check_in_date) if ro.check_in_date else None,
		"inspection_template": getattr(ro, "inspection_template", None),
		"items": items,
		"summary": {
			"total": total,
			**counts,
			"completed": completed,
			"all_completed": total > 0 and counts["pending"] == 0,
		},
	}


def _parse_row_photos(row) -> list[str]:
	photos: list[str] = []
	raw = getattr(row, "photos", None)
	if isinstance(raw, str) and raw.strip():
		try:
			raw = json.loads(raw)
		except Exception:
			raw = []
	if isinstance(raw, list):
		for entry in raw:
			if isinstance(entry, str) and entry.strip() and entry.strip() not in photos:
				photos.append(entry.strip())
	if getattr(row, "image", None) and row.image not in photos:
		photos.insert(0, row.image)
	return photos


def _normalize_inspection_status(value: str) -> str:
	if not isinstance(value, str) or not value.strip():
		frappe.throw(_("Inspection status must be a non-empty string."))
	canonical = _STATUS_ALIASES.get(value.strip().lower())
	if not canonical:
		frappe.throw(
			_("Invalid inspection status '{0}'. Allowed values: {1}.").format(
				value, ", ".join(VALID_INSPECTION_STATUSES)
			)
		)
	return canonical


def _parse_odometer(value: int | float | str) -> int:
	if isinstance(value, bool):
		frappe.throw(_("Invalid odometer reading."))
	try:
		parsed = int(float(value))
	except (TypeError, ValueError):
		frappe.throw(_("Odometer reading must be a valid integer."))
	if parsed < 0:
		frappe.throw(_("Odometer reading cannot be negative."))
	return parsed


def _as_bool(value: bool | int | str) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(cint(value))
