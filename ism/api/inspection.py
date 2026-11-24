# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import base64
import binascii
import json
import math
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

DEFAULT_INSPECTION_TEMPLATE = "Standard Vehicle Walkaround"
VALID_INSPECTION_STATUSES = ("Pending", "Pass", "Attention", "Fail", "N/A")
_MAX_INSPECTION_PHOTO_BYTES = 5 * 1024 * 1024
_MAX_PHOTOS_PER_ITEM = 20
_ALLOWED_IMAGE_DATA_PREFIXES = (
	("data:image/png;base64,", "png"),
	("data:image/jpeg;base64,", "jpg"),
	("data:image/webp;base64,", "webp"),
)
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

	rows = frappe.get_list(
		"Inspection Template",
		filters=filters,
		fields=["name", "template_name", "is_active", "description"],
		order_by="template_name asc",
	)

	item_counts: dict[str, int] = {}
	if rows:
		template_names = [row.name for row in rows]
		count_rows = frappe.db.sql(
			"""
			SELECT parent, COUNT(*) AS item_count
			FROM `tabInspection Template Item`
			WHERE parent IN %s AND parenttype = 'Inspection Template'
			GROUP BY parent
			""",
			(tuple(template_names),),
			as_dict=True,
		)
		for count_row in count_rows:
			item_counts[count_row.parent] = cint(count_row.item_count)

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
	if template_name is not None and not isinstance(template_name, str):
		frappe.throw(_("Inspection Template name must be a string."))
	resolved_name = _resolve_template_name(template_name)
	doc = frappe.get_doc("Inspection Template", resolved_name)
	doc.check_permission("read")

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
	frappe.has_permission("Inspection Template", "read", throw=True)
	if template_name is not None and not isinstance(template_name, str):
		frappe.throw(_("Inspection Template name must be a string."))
	resolved_name = _resolve_template_name(template_name or getattr(ro, "inspection_template", None))
	template_doc = frappe.get_doc("Inspection Template", resolved_name)
	template_doc.check_permission("read")

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
	vehicle_color: str | None = None,
	color: str | None = None,
	license_plate: str | None = None,
) -> dict:
	"""Record or update vehicle check-in intake fields and inspection item findings."""
	from ism.utils.vehicle_display import (
		normalize_license_plate,
		normalize_vehicle_color,
		validate_repair_vehicle_ready_for_check_in,
	)

	ro = _get_repair_order(repair_order, "write")
	if template_name is not None and not isinstance(template_name, str):
		frappe.throw(_("Inspection Template name must be a string."))
	if customer_concern is not None and not isinstance(customer_concern, str):
		frappe.throw(_("Customer concern must be a string."))
	raw_color = vehicle_color if vehicle_color is not None else color
	if raw_color is not None and not isinstance(raw_color, str):
		frappe.throw(_("Vehicle color must be a string."))
	if license_plate is not None and not isinstance(license_plate, str):
		frappe.throw(_("License plate must be a string."))

	if getattr(ro, "vehicle", None):
		clean_color = normalize_vehicle_color(raw_color)
		clean_plate = normalize_license_plate(license_plate)
		if clean_color or clean_plate:
			veh_doc = frappe.get_doc("Repair Vehicle", ro.vehicle)
			veh_doc.check_permission("write")
			changed = False
			if clean_color and clean_color != normalize_vehicle_color(getattr(veh_doc, "color", None)):
				veh_doc.color = clean_color
				changed = True
			if clean_plate and clean_plate != normalize_license_plate(getattr(veh_doc, "license_plate", None)):
				veh_doc.license_plate = clean_plate
				changed = True
			if changed:
				veh_doc.save()
		validate_repair_vehicle_ready_for_check_in(ro.vehicle)

	if _as_bool(auto_apply_template):
		frappe.has_permission("Inspection Template", "read", throw=True)
		resolved_tpl = _resolve_template_name(template_name or getattr(ro, "inspection_template", None))
		template_doc = frappe.get_doc("Inspection Template", resolved_tpl)
		template_doc.check_permission("read")
		ro.inspection_template = template_doc.name
		_merge_template_items(ro, template_doc)
	elif template_name and template_name.strip():
		frappe.has_permission("Inspection Template", "read", throw=True)
		resolved_tpl = _resolve_template_name(template_name)
		template_doc = frappe.get_doc("Inspection Template", resolved_tpl)
		template_doc.check_permission("read")
		ro.inspection_template = template_doc.name

	raw_odo = odometer_in if odometer_in is not None else odometer
	if raw_odo is not None and raw_odo != "":
		ro.odometer_in = _parse_odometer(raw_odo)

	if customer_concern is not None:
		ro.customer_concern = customer_concern.strip()

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
		if (is_active is None or cint(is_active)) and frappe.has_permission(
			"Inspection Template", "read", doc=DEFAULT_INSPECTION_TEMPLATE
		):
			return DEFAULT_INSPECTION_TEMPLATE

	fallback = frappe.get_list(
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
	has_name = entry.get("name") is not None
	has_row_id = entry.get("row_id") is not None
	if has_name or has_row_id:
		raw_id = entry.get("name") if has_name else entry.get("row_id")
		if not isinstance(raw_id, str) or not raw_id.strip():
			frappe.throw(_("Inspection item row identifier must be a non-empty string."))
		row_id = raw_id.strip()
		for row in rows:
			if row.name == row_id:
				return row, False
		frappe.throw(
			_("Inspection row '{0}' was not found on Repair Order {1}.").format(row_id, ro.name),
			frappe.DoesNotExistError,
		)

	raw_idx = entry.get("idx")
	if raw_idx is not None:
		if isinstance(raw_idx, bool):
			frappe.throw(_("Inspection item idx must be a positive integer."))
		if isinstance(raw_idx, int):
			target_idx = raw_idx
		elif isinstance(raw_idx, str) and raw_idx.strip().isdigit():
			target_idx = int(raw_idx.strip())
		else:
			frappe.throw(_("Inspection item idx must be a positive integer."))
		if target_idx <= 0:
			frappe.throw(_("Inspection item idx must be a positive integer."))
		for row in rows:
			if row.idx == target_idx:
				return row, False
		frappe.throw(
			_("Inspection row #{0} was not found on Repair Order {1}.").format(target_idx, ro.name),
			frappe.DoesNotExistError,
		)

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
	if "inspection_description" in entry and entry.get("inspection_description") is not None:
		raw_desc = entry["inspection_description"]
		if not isinstance(raw_desc, str) or not raw_desc.strip():
			frappe.throw(_("Inspection description must be a non-empty string."))
		row.inspection_description = raw_desc.strip()

	if "status" in entry and entry["status"] is not None:
		row.status = _normalize_inspection_status(entry["status"])
		touched = True

	if "condition" in entry:
		cond = entry["condition"]
		if cond is not None and not isinstance(cond, str):
			frappe.throw(_("Inspection condition must be a string."))
		row.condition = cond
		touched = True

	if "notes" in entry:
		notes = entry["notes"]
		if notes is not None and not isinstance(notes, str):
			frappe.throw(_("Inspection notes must be a string."))
		row.notes = notes
		touched = True

	if "image" in entry or "photos" in entry:
		photos = _collect_and_save_photos(ro, row, entry)
		row.photos = json.dumps(photos) if photos else None
		row.image = photos[0] if photos else None
		touched = True

	if touched:
		row.inspected_by = frappe.session.user
		row.inspected_at = stamp


def _decode_raster_data_uri(value: str) -> tuple[bytes, str]:
	"""Validate an allowlisted raster base64 data URI and return decoded bytes + extension."""
	lower = value.lower()
	matched_prefix = None
	ext = None
	for prefix, candidate_ext in _ALLOWED_IMAGE_DATA_PREFIXES:
		if lower.startswith(prefix):
			matched_prefix = prefix
			ext = candidate_ext
			break

	if not matched_prefix or not ext:
		frappe.throw(_("Unsupported photo data URI. Allowed formats: PNG, JPEG, WebP."))

	b64_payload = value[len(matched_prefix) :].strip()
	if not b64_payload:
		frappe.throw(_("Inspection photo payload cannot be empty."))

	max_b64_len = (_MAX_INSPECTION_PHOTO_BYTES * 4 // 3) + 16
	if len(b64_payload) > max_b64_len:
		frappe.throw(_("Inspection photo exceeds maximum allowed size (5 MB)."))

	try:
		decoded = base64.b64decode(b64_payload, validate=True)
	except (binascii.Error, ValueError):
		frappe.throw(_("Inspection photo contains invalid base64 data."))

	if not decoded or len(decoded) > _MAX_INSPECTION_PHOTO_BYTES:
		frappe.throw(_("Inspection photo exceeds allowed size bounds."))

	return decoded, ext


def _is_safe_persisted_file_url(value: str) -> bool:
	"""Allow only safe local Frappe file URLs under /files/ or /private/files/."""
	if not value or not value.startswith(("/private/files/", "/files/")):
		return False
	if "\\" in value or any(ch.isspace() or ord(ch) < 32 for ch in value):
		return False
	parsed = urlparse(value)
	if parsed.scheme or parsed.netloc or parsed.path != value or parsed.query or parsed.fragment:
		return False
	if ".." in value.split("/"):
		return False
	return True


def _collect_and_save_photos(ro, row, entry: dict) -> list[str]:
	from frappe.utils.file_manager import save_file

	incoming: list[str] = []
	raw_image = entry.get("image")
	if raw_image is not None:
		if not isinstance(raw_image, str):
			frappe.throw(_("Inspection image must be a string."))
		if raw_image.strip():
			incoming.append(raw_image.strip())

	raw_photos = entry.get("photos")
	if raw_photos is not None:
		if isinstance(raw_photos, str):
			try:
				raw_photos = json.loads(raw_photos) if raw_photos.strip() else []
			except Exception:
				frappe.throw(_("Invalid JSON format for inspection photos."))
		if not isinstance(raw_photos, list):
			frappe.throw(_("Inspection photos must be a list of URLs or data URIs."))
		for photo in raw_photos:
			if not isinstance(photo, str):
				frappe.throw(_("Each inspection photo entry must be a string."))
			if photo.strip():
				incoming.append(photo.strip())

	if len(incoming) > _MAX_PHOTOS_PER_ITEM:
		frappe.throw(_("Too many photos attached to a single inspection item."))

	# Preserve existing photos unless the caller explicitly passed a replacement photos list
	base_photos = [] if "photos" in entry else _parse_row_photos(row)
	saved_urls = list(base_photos)

	for value in incoming:
		if value.lower().startswith("data:"):
			decoded, ext = _decode_raster_data_uri(value)
			file_doc = save_file(
				fname=f"inspect_{frappe.generate_hash(length=12)}.{ext}",
				content=decoded,
				dt="Repair Order",
				dn=ro.name,
				decode=False,
				is_private=1,
			)
			url = file_doc.file_url
		else:
			if not _is_safe_persisted_file_url(value):
				frappe.throw(_("Invalid inspection photo URL. Only local /files/ or /private/files/ paths are permitted."))
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

	from ism.utils.vehicle_display import build_vehicle_display_dict, get_vehicle_display_map

	veh_name = getattr(ro, "vehicle", None)
	v_info = (
		get_vehicle_display_map([veh_name]).get(veh_name)
		if veh_name
		else build_vehicle_display_dict(None)
	) or build_vehicle_display_dict(None, fallback=veh_name)

	total = len(items)
	completed = total - counts["pending"]
	return {
		"repair_order": ro.name,
		"status": ro.status,
		"vehicle": veh_name,
		"vehicle_display": v_info.get("display") or "",
		"vehicle_info": v_info,
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
	if isinstance(value, bool) or not isinstance(value, (int, float, str)):
		frappe.throw(_("Invalid odometer reading."))
	try:
		numeric = float(value)
	except (TypeError, ValueError):
		frappe.throw(_("Odometer reading must be a valid integer."))
	if not math.isfinite(numeric):
		frappe.throw(_("Odometer reading must be a finite number."))
	parsed = int(numeric)
	if parsed < 0:
		frappe.throw(_("Odometer reading cannot be negative."))
	return parsed


def _as_bool(value: bool | int | str) -> bool:
	if value is None:
		return False
	if isinstance(value, bool):
		return value
	if isinstance(value, int):
		return bool(value)
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	frappe.throw(_("Boolean flag must be a bool, int, or string."))
