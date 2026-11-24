# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Shared vehicle display formatting and intake/check-in validation helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import frappe

VEHICLE_DISPLAY_FIELDS = (
	"name",
	"vin",
	"model_year",
	"manufacturer",
	"model",
	"color",
	"license_plate",
	"display_title",
)


def _clean_text(value: Any) -> str | None:
	if value is None or isinstance(value, (bool, list, dict, tuple, set)):
		return None
	text = " ".join(str(value).split())
	if not text or text.lower() == "none":
		return None
	return text


def _clean_year(value: Any) -> str | None:
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, int):
		return str(value) if value > 0 else None
	text = _clean_text(value)
	if not text:
		return None
	if text.isdigit() and int(text) <= 0:
		return None
	return text


def normalize_vehicle_color(value: Any) -> str | None:
	"""Trim and collapse whitespace on a vehicle color; return None if empty."""
	return _clean_text(value)


def normalize_license_plate(value: Any) -> str | None:
	"""Trim, collapse whitespace, and uppercase a license plate; return None if empty."""
	text = _clean_text(value)
	return text.upper() if text else None


def _get_field(source: Any, *keys: str) -> Any:
	if isinstance(source, Mapping):
		for key in keys:
			if key in source and source[key] not in (None, ""):
				return source[key]
		return None
	for key in keys:
		val = getattr(source, key, None)
		if val not in (None, ""):
			return val
	return None


def _is_vin_like(value: str) -> bool:
	return len(value) == 17 and value.isalnum()


def build_vehicle_display_dict(vehicle: Any = None, *, fallback: str | None = None) -> dict[str, Any]:
	"""Build a structured human-friendly display dictionary for a Repair Vehicle or payload."""
	clean_fallback = _clean_text(fallback)
	source = vehicle
	docname_hint: str | None = None

	if isinstance(vehicle, str):
		vehicle_str = _clean_text(vehicle)
		if vehicle_str:
			docname_hint = vehicle_str
			row = frappe.db.get_value(
				"Repair Vehicle",
				vehicle_str,
				list(VEHICLE_DISPLAY_FIELDS),
				as_dict=True,
			)
			if row:
				source = row
			else:
				source = {
					"name": vehicle_str,
					"vin": vehicle_str.upper() if _is_vin_like(vehicle_str) else None,
				}
		else:
			source = None

	if source is None:
		display = clean_fallback or "Vehicle"
		return {
			"vehicle": None,
			"vin": None,
			"model_year": None,
			"manufacturer": None,
			"model": None,
			"color": None,
			"license_plate": None,
			"display": display,
			"compact_display": display,
			"label": display,
		}

	source_doctype = getattr(source, "doctype", None)
	if source_doctype in (None, "Repair Vehicle"):
		name_or_none = _clean_text(_get_field(source, "name", "vehicle", "repair_vehicle")) or docname_hint
	else:
		name_or_none = _clean_text(_get_field(source, "vehicle", "repair_vehicle")) or docname_hint
	raw_vin = _clean_text(_get_field(source, "vin"))
	vin_or_none = raw_vin.upper() if raw_vin else None
	model_year = _clean_year(
		_get_field(source, "model_year", "year", "vehicle_year", "provisional_vehicle_year")
	)
	manufacturer = _clean_text(_get_field(source, "manufacturer"))
	model = _clean_text(_get_field(source, "model", "vehicle_model", "provisional_vehicle_model"))
	color = normalize_vehicle_color(
		_get_field(source, "color", "vehicle_color", "provisional_vehicle_color")
	)
	license_plate = normalize_license_plate(
		_get_field(source, "license_plate", "plate", "provisional_license_plate")
	)

	legacy_vin_or_plate = _clean_text(_get_field(source, "provisional_vin_or_plate"))
	if legacy_vin_or_plate and not license_plate and not vin_or_none:
		if _is_vin_like(legacy_vin_or_plate):
			vin_or_none = legacy_vin_or_plate.upper()
		else:
			license_plate = normalize_license_plate(legacy_vin_or_plate)

	core_model = model or manufacturer
	base_parts = [part for part in (model_year, color, core_model) if part]
	base_display = " ".join(base_parts)

	if base_display and license_plate:
		display = f"{base_display} ({license_plate})"
	elif base_display:
		display = base_display
	elif license_plate:
		display = license_plate
	else:
		display = vin_or_none or clean_fallback or "Vehicle"

	compact_parts = [part for part in (color, model) if part]
	if compact_parts:
		compact_display = " ".join(compact_parts)
	else:
		compact_display = (
			model_year
			or license_plate
			or manufacturer
			or vin_or_none
			or clean_fallback
			or "Vehicle"
		)

	return {
		"vehicle": name_or_none,
		"vin": vin_or_none,
		"model_year": model_year,
		"manufacturer": manufacturer,
		"model": model,
		"color": color,
		"license_plate": license_plate,
		"display": display,
		"compact_display": compact_display,
		"label": display,
	}


def format_vehicle_display(
	vehicle: Any = None,
	*,
	include_vin: bool = False,
	compact: bool = False,
	fallback: str | None = None,
) -> str:
	"""Format a human-friendly vehicle label (year, color, model, plate), optionally appending VIN."""
	info = build_vehicle_display_dict(vehicle, fallback=fallback)
	if compact:
		return info["compact_display"]
	display = info["display"]
	if include_vin and info["vin"] and display != info["vin"]:
		return f"{display} · VIN: {info['vin']}"
	return display


def get_vehicle_display_map(vehicle_names: Iterable[str | None] | None) -> dict[str, dict[str, Any]]:
	"""Batch-fetch unique Repair Vehicle rows in one query and return display dicts keyed by name."""
	if not vehicle_names:
		return {}
	unique_names = list(dict.fromkeys(name for raw in vehicle_names if (name := _clean_text(raw))))
	if not unique_names:
		return {}

	rows = frappe.get_all(
		"Repair Vehicle",
		filters={"name": ["in", unique_names]},
		fields=list(VEHICLE_DISPLAY_FIELDS),
		ignore_permissions=True,
	)
	return {row["name"]: build_vehicle_display_dict(row) for row in rows if row.get("name")}


def require_vehicle_intake_fields(
	*,
	color: Any,
	license_plate: Any,
	context: str = "vehicle intake / check-in",
) -> tuple[str, str]:
	"""Normalize and enforce required vehicle color and license plate on intake / check-in."""
	norm_color = normalize_vehicle_color(color)
	norm_plate = normalize_license_plate(license_plate)

	if not norm_color and not norm_plate:
		frappe.throw(
			frappe._("Vehicle Color and License Plate are required for {0}.").format(context),
			frappe.MandatoryError,
		)
	if not norm_color:
		frappe.throw(
			frappe._("Vehicle Color is required for {0}.").format(context),
			frappe.MandatoryError,
		)
	if not norm_plate:
		frappe.throw(
			frappe._("License Plate is required for {0}.").format(context),
			frappe.MandatoryError,
		)

	return norm_color, norm_plate


def validate_repair_vehicle_ready_for_check_in(vehicle_name: str) -> dict[str, Any]:
	"""Verify a Repair Vehicle has both color and license plate before check-in."""
	clean_name = _clean_text(vehicle_name)
	if not clean_name:
		frappe.throw(
			frappe._("Vehicle is required for vehicle check-in."),
			frappe.MandatoryError,
		)

	row = frappe.db.get_value(
		"Repair Vehicle",
		clean_name,
		list(VEHICLE_DISPLAY_FIELDS),
		as_dict=True,
	)
	if not row:
		frappe.throw(
			frappe._("Repair Vehicle {0} does not exist.").format(clean_name),
			frappe.DoesNotExistError,
		)

	norm_color, norm_plate = require_vehicle_intake_fields(
		color=row.get("color"),
		license_plate=row.get("license_plate"),
		context="vehicle check-in",
	)
	result = dict(row)
	result["color"] = norm_color
	result["license_plate"] = norm_plate
	result.update(build_vehicle_display_dict(result, fallback=clean_name))
	return result
