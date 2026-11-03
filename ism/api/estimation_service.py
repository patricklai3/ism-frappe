# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from typing import Any, Dict, List, Optional, Union

import frappe
from frappe.utils import cint, flt

from ism.scheduling.estimation_light import (
	DEFAULT_SIGMA,
	estimate_duration,
	estimate_total_duration,
	get_sigma_for_item,
)


def _get_ism_settings():
	"""Retrieve ISM Settings single doctype safely."""
	if not frappe.db.exists("DocType", "ISM Settings"):
		return None
	try:
		return frappe.get_single("ISM Settings")
	except Exception:
		return None


def _get_setting_percentile(settings, default: float = 0.80) -> float:
	val = getattr(settings, "estimation_percentile", None)
	if val is None or val == "":
		return default
	try:
		p = float(val)
		if p > 1.0:
			p = p / 100.0
		if 0.50 <= p <= 0.999:
			return p
	except (ValueError, TypeError):
		pass
	return default


def _get_setting_sigma(settings, default: float = DEFAULT_SIGMA) -> float:
	val = getattr(settings, "estimation_sigma", None)
	if val is None or val == "":
		return default
	try:
		s = float(val)
		if 0.05 <= s <= 1.0:
			return s
	except (ValueError, TypeError):
		pass
	return default


def _get_setting_buffer(settings, default: int = 0) -> int:
	val = getattr(settings, "buffer_padding_minutes", None)
	if val is None or val == "":
		return default
	try:
		b = int(float(val))
		return max(0, b)
	except (ValueError, TypeError):
		return default


def _get_setting_default_diagnostic(settings, default: float = 60.0) -> float:
	val = getattr(settings, "default_diagnostic_duration", None)
	if val is None or val == "":
		return default
	try:
		d = float(val)
		return d if d > 0 else default
	except (ValueError, TypeError):
		return default


def _get_frt(item_code: str, fallback: Optional[float] = None) -> Optional[float]:
	"""Safely retrieve FRT in MINUTES from Item DocType.

	Item.custom_frt is stored in HOURS (e.g. 1.0 hour, 1.5 hours).
	Returns FRT in MINUTES (e.g. 60.0 minutes, 90.0 minutes).
	"""
	if not item_code:
		return fallback

	if frappe.db.has_column("Item", "custom_frt"):
		if frappe.db.exists("Item", item_code):
			frt = frappe.db.get_value("Item", item_code, "custom_frt")
			if frt is not None:
				try:
					val = float(frt)
					if val > 0:
						return val * 60.0
				except (ValueError, TypeError):
					pass

	return fallback


@frappe.whitelist()
def get_estimate(item_code: Optional[str] = None, **kwargs) -> Dict[str, Any]:
	"""Get risk-adjusted duration estimate for a single operation.

	Args:
		item_code: Item code to look up in Frappe database.
		**kwargs:
			- fallback_frt: Default FRT in minutes if item/custom_frt missing.
			- flat_rate_minutes / frt_minutes: Explicit FRT in minutes.
			- custom_frt / flat_rate_hours: Explicit FRT in hours.
			- sigma: Custom sigma override.
			- percentile: Target percentile (0.50 to 0.99, or 50 to 99).
			- buffer_padding / buffer_padding_minutes: Padding in minutes override.

	Returns:
		Dict with:
			- duration_minutes: int (p_pure + buffer_padding, or 0)
			- p_pure: int (pure log-normal estimate)
			- buffer_padding: int
			- percentile: float
			- sigma: float
	"""
	settings = _get_ism_settings()

	# Resolve percentile
	percentile = kwargs.get("percentile")
	if percentile is not None and percentile != "":
		try:
			p = float(percentile)
			if p > 1.0:
				p = p / 100.0
			percentile = p
		except (ValueError, TypeError):
			percentile = _get_setting_percentile(settings)
	else:
		percentile = _get_setting_percentile(settings)

	# Resolve buffer padding
	buffer_padding = kwargs.get("buffer_padding")
	if buffer_padding is None:
		buffer_padding = kwargs.get("buffer_padding_minutes")
	if buffer_padding is not None and buffer_padding != "":
		try:
			buffer_padding = max(0, int(float(buffer_padding)))
		except (ValueError, TypeError):
			buffer_padding = _get_setting_buffer(settings)
	else:
		buffer_padding = _get_setting_buffer(settings)

	# Resolve default sigma from settings
	settings_sigma = _get_setting_sigma(settings)

	# Resolve fallback FRT
	fallback_frt = kwargs.get("fallback_frt")
	if fallback_frt is not None and fallback_frt != "":
		try:
			fallback_frt = float(fallback_frt)
		except (ValueError, TypeError):
			fallback_frt = None

	# Resolve FRT in minutes
	frt = None
	if kwargs.get("flat_rate_minutes") is not None:
		try:
			frt = float(kwargs["flat_rate_minutes"])
		except (ValueError, TypeError):
			frt = None
	elif kwargs.get("frt_minutes") is not None:
		try:
			frt = float(kwargs["frt_minutes"])
		except (ValueError, TypeError):
			frt = None
	elif kwargs.get("custom_frt") is not None:
		try:
			frt = float(kwargs["custom_frt"]) * 60.0
		except (ValueError, TypeError):
			frt = None
	elif kwargs.get("flat_rate_hours") is not None:
		try:
			frt = float(kwargs["flat_rate_hours"]) * 60.0
		except (ValueError, TypeError):
			frt = None

	if frt is None and item_code:
		default_fb = fallback_frt if fallback_frt is not None else _get_setting_default_diagnostic(settings)
		frt = _get_frt(str(item_code), fallback=default_fb)
	elif frt is None and fallback_frt is not None:
		frt = fallback_frt

	# Resolve sigma
	sigma = kwargs.get("sigma")
	if sigma is not None and sigma != "":
		try:
			sigma = float(sigma)
		except (ValueError, TypeError):
			sigma = None

	if sigma is None:
		if item_code and frappe.db.exists("Item", item_code):
			item_group = frappe.db.get_value("Item", item_code, "item_group")
			sigma = get_sigma_for_item(item_group, default_sigma=settings_sigma)
		else:
			sigma = settings_sigma

	if frt is None or frt <= 0:
		return {
			"duration_minutes": 0,
			"p_pure": 0,
			"buffer_padding": buffer_padding,
			"percentile": percentile,
			"sigma": sigma,
		}

	p_pure = estimate_duration(frt, sigma=sigma, percentile=percentile)
	duration_minutes = p_pure + buffer_padding

	return {
		"duration_minutes": duration_minutes,
		"p_pure": p_pure,
		"buffer_padding": buffer_padding,
		"percentile": percentile,
		"sigma": sigma,
	}


@frappe.whitelist()
def get_total_estimate(
	item_codes: Union[str, List[Union[str, Dict[str, Any]]]],
	**kwargs,
) -> Dict[str, Any]:
	"""Get multi-operation duration estimate using Fenton-Wilkinson summation + buffer.

	Args:
		item_codes: JSON string, list of item codes, or list of operation dicts.
		**kwargs:
			- fallback_frt: Default FRT in minutes for missing items.
			- skip_missing: If True, ignore missing items without valid FRT.
			- sigma: Custom default sigma override.
			- percentile: Target percentile (0.50 to 0.99, or 50 to 99).
			- buffer_padding / buffer_padding_minutes: Padding in minutes override.

	Returns:
		Dict with:
			- total_duration_minutes: int (p_pure + buffer_padding, or 0)
			- p_pure: int (pure F-W log-normal estimate)
			- buffer_padding: int
			- operations_count: int
	"""
	settings = _get_ism_settings()

	# Resolve percentile
	percentile = kwargs.get("percentile")
	if percentile is not None and percentile != "":
		try:
			p = float(percentile)
			if p > 1.0:
				p = p / 100.0
			percentile = p
		except (ValueError, TypeError):
			percentile = _get_setting_percentile(settings)
	else:
		percentile = _get_setting_percentile(settings)

	# Resolve buffer padding
	buffer_padding = kwargs.get("buffer_padding")
	if buffer_padding is None:
		buffer_padding = kwargs.get("buffer_padding_minutes")
	if buffer_padding is not None and buffer_padding != "":
		try:
			buffer_padding = max(0, int(float(buffer_padding)))
		except (ValueError, TypeError):
			buffer_padding = _get_setting_buffer(settings)
	else:
		buffer_padding = _get_setting_buffer(settings)

	# Resolve default sigma from settings
	settings_sigma = _get_setting_sigma(settings)
	override_default_sigma = kwargs.get("sigma")
	if override_default_sigma is not None and override_default_sigma != "":
		try:
			default_sigma = float(override_default_sigma)
		except (ValueError, TypeError):
			default_sigma = settings_sigma
	else:
		default_sigma = settings_sigma

	# Parse skip_missing
	skip_missing = kwargs.get("skip_missing", False)
	if isinstance(skip_missing, str):
		skip_missing = skip_missing.lower() in ("true", "1")
	else:
		skip_missing = bool(skip_missing)

	# Fallback FRT
	fallback_frt = kwargs.get("fallback_frt")
	if fallback_frt is not None and fallback_frt != "":
		try:
			fallback_frt = float(fallback_frt)
		except (ValueError, TypeError):
			fallback_frt = None

	# Parse item_codes
	if isinstance(item_codes, str):
		try:
			item_codes = frappe.parse_json(item_codes)
		except Exception:
			try:
				item_codes = json.loads(item_codes)
			except Exception:
				item_codes = [c.strip() for c in item_codes.split(",") if c.strip()]

	if not item_codes or not isinstance(item_codes, (list, tuple)):
		return {
			"total_duration_minutes": 0,
			"p_pure": 0,
			"buffer_padding": buffer_padding,
			"operations_count": 0,
		}

	operations = []
	for item in item_codes:
		if isinstance(item, dict):
			code = item.get("item_code")
			item_frt_min = item.get("flat_rate_minutes")
			if item_frt_min is None:
				item_frt_min = item.get("frt_minutes")
			item_frt_hr = (
				item.get("flat_rate_hours")
				or item.get("frt_hours")
				or item.get("custom_frt")
				or item.get("qty")
			)
			item_sig = item.get("sigma")

			if item_frt_min is not None:
				try:
					item_frt = float(item_frt_min)
				except (ValueError, TypeError):
					item_frt = None
			elif item_frt_hr is not None:
				try:
					item_frt = float(item_frt_hr) * 60.0
				except (ValueError, TypeError):
					item_frt = None
			else:
				item_frt = None
		elif isinstance(item, (int, float)):
			code = None
			item_frt = float(item)
			item_sig = None
		else:
			code = str(item).strip() if item else None
			item_frt = None
			item_sig = None

		if item_frt is None and code:
			effective_fallback = None if skip_missing else (
				fallback_frt if fallback_frt is not None else _get_setting_default_diagnostic(settings)
			)
			item_frt = _get_frt(code, fallback=effective_fallback)

		if item_frt is None or item_frt <= 0:
			if skip_missing:
				continue
			item_frt = fallback_frt if fallback_frt and fallback_frt > 0 else None

		if item_frt is None or item_frt <= 0:
			continue

		if item_sig is None:
			if code and frappe.db.exists("Item", code):
				item_group = frappe.db.get_value("Item", code, "item_group")
				item_sig = get_sigma_for_item(item_group, default_sigma=default_sigma)
			else:
				item_sig = default_sigma
		else:
			try:
				item_sig = float(item_sig)
			except (ValueError, TypeError):
				item_sig = default_sigma

		operations.append({"flat_rate_minutes": float(item_frt), "sigma": float(item_sig)})

	if not operations:
		return {
			"total_duration_minutes": 0,
			"p_pure": 0,
			"buffer_padding": buffer_padding,
			"operations_count": 0,
		}

	p_pure = estimate_total_duration(operations, percentile=percentile)
	total_duration_minutes = p_pure + buffer_padding

	return {
		"total_duration_minutes": total_duration_minutes,
		"p_pure": p_pure,
		"buffer_padding": buffer_padding,
		"operations_count": len(operations),
	}


def _resolve_service_frt_minutes(s) -> Optional[float]:
	"""Extract flat rate time in minutes from a Repair Order Service row."""
	labor_hours = getattr(s, "labor_hours", None)
	if labor_hours is not None:
		try:
			val = float(labor_hours)
			if val > 0:
				return val * 60.0
		except (ValueError, TypeError):
			pass

	frt_val = getattr(s, "frt", None)
	if frt_val is not None:
		try:
			val = float(frt_val)
			if val > 0:
				return val * 60.0
		except (ValueError, TypeError):
			pass

	pkg_name = getattr(s, "service_package", None)
	if pkg_name and frappe.db.exists("Service Package", pkg_name):
		pkg_hours = frappe.db.get_value("Service Package", pkg_name, "standard_labor_hours")
		if pkg_hours is not None:
			try:
				val = float(pkg_hours)
				if val > 0:
					return val * 60.0
			except (ValueError, TypeError):
				pass

	item_code = getattr(s, "item_code", None)
	if item_code:
		item_frt = _get_frt(str(item_code))
		if item_frt and item_frt > 0:
			return item_frt

	return None


def _resolve_service_sigma(s, default_sigma: float) -> float:
	"""Determine variance profile (sigma) for a Repair Order Service row."""
	cat = getattr(s, "category", None)
	if cat and isinstance(cat, str):
		sig = get_sigma_for_item(cat, default_sigma=default_sigma)
		if sig != default_sigma:
			return sig

	title = getattr(s, "service_title", "") or ""
	pkg = getattr(s, "service_package", "") or ""
	combined = f"{title} {pkg}".lower()

	for keyword in ("tire", "brake", "suspension", "electrical", "diagnostic", "body", "hvac", "drivetrain"):
		if keyword in combined:
			return get_sigma_for_item(keyword, default_sigma=default_sigma)

	parts = getattr(s, "parts", []) or []
	for p in parts:
		item_code = getattr(p, "item_code", None)
		if item_code and frappe.db.exists("Item", item_code):
			item_group = frappe.db.get_value("Item", item_code, "item_group")
			sig = get_sigma_for_item(item_group, default_sigma=default_sigma)
			if sig != default_sigma:
				return sig

	return default_sigma


@frappe.whitelist()
def estimate_repair_order(repair_order: Optional[str] = None, **kwargs) -> Dict[str, Any]:
	"""Estimate duration for a Repair Order based on its active services.

	Filters out declined services, extracts flat rate times from services or linked
	service packages/items, assigns category-specific variance (sigma), and applies
	Fenton-Wilkinson summation + buffer padding.

	Falls back to default diagnostic duration if the Repair Order has no active services.

	Args:
		repair_order: Name/ID of the Repair Order.
		**kwargs: Overrides for percentile, buffer_padding, sigma, fallback_frt.

	Returns:
		Dict with:
			- total_minutes: int (same as total_duration_minutes, for modal compatibility)
			- total_duration_minutes: int
			- p_pure: int
			- buffer_padding: int
			- operations_count: int
			- repair_order: Optional[str]
	"""
	if not repair_order:
		return {
			"total_minutes": 0,
			"total_duration_minutes": 0,
			"p_pure": 0,
			"buffer_padding": 0,
			"operations_count": 0,
			"repair_order": None,
		}

	if hasattr(repair_order, "doctype") and repair_order.doctype == "Repair Order":
		ro = repair_order
		repair_order_name = ro.name
	else:
		repair_order_name = str(repair_order).strip()
		if repair_order_name.startswith("new-"):
			return {
				"total_minutes": 0,
				"total_duration_minutes": 0,
				"p_pure": 0,
				"buffer_padding": 0,
				"operations_count": 0,
				"repair_order": None,
			}
		if not frappe.db.exists("Repair Order", repair_order_name):
			frappe.throw(
				frappe._("Repair Order {0} does not exist").format(repair_order_name),
				frappe.DoesNotExistError,
			)
		ro = frappe.get_doc("Repair Order", repair_order_name)

	settings = _get_ism_settings()
	default_sigma = _get_setting_sigma(settings)
	if kwargs.get("sigma") is not None and kwargs.get("sigma") != "":
		try:
			default_sigma = float(kwargs["sigma"])
		except (ValueError, TypeError):
			pass

	services = getattr(ro, "services", []) or []
	operations = []

	for s in services:
		status = getattr(s, "status", None)
		if status == "Declined":
			continue

		frt_min = _resolve_service_frt_minutes(s)
		if frt_min is not None and frt_min > 0:
			sig = _resolve_service_sigma(s, default_sigma)
			operations.append({"flat_rate_minutes": float(frt_min), "sigma": float(sig)})

	if not operations:
		fallback_frt = kwargs.get("fallback_frt")
		if fallback_frt is not None and fallback_frt != "":
			try:
				diag_duration = float(fallback_frt)
			except (ValueError, TypeError):
				diag_duration = _get_setting_default_diagnostic(settings, default=60.0)
		else:
			diag_duration = _get_setting_default_diagnostic(settings, default=60.0)

		if diag_duration > 0:
			diag_sigma = get_sigma_for_item("Diagnostic", default_sigma=default_sigma)
			operations.append({
				"flat_rate_minutes": float(diag_duration),
				"sigma": float(diag_sigma),
			})

	res = get_total_estimate(operations, **kwargs)
	res["total_minutes"] = res["total_duration_minutes"]
	res["repair_order"] = repair_order_name
	return res
