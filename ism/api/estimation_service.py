# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import math
from typing import Any, Dict, List, Optional, Tuple, Union

import frappe
from frappe import _
from frappe.utils import flt

from ism.scheduling.estimation_light import (
	DEFAULT_SIGMA,
	_Z_SCORES,
	approximate_z_score,
	estimate_duration,
	estimate_total_duration,
	get_sigma_for_item,
)

_MIN_PERCENTILE = 0.50
_MAX_PERCENTILE = 1.0
_MIN_SIGMA = 0.01
_MAX_SIGMA = 2.0


def _ensure_authenticated_estimation_user() -> None:
	"""Deny anonymous Guest calls on whitelisted estimation endpoints."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(_("Authentication is required to estimate repair durations."), frappe.PermissionError)


def _get_ism_settings():
	"""Retrieve cached ISM Settings single doctype safely."""
	if not frappe.db.exists("DocType", "ISM Settings"):
		return None
	try:
		return frappe.get_single("ISM Settings")
	except Exception:
		return None


def _parse_json_value(raw: Any, *, allow_csv: bool = False) -> Any:
	"""Parse JSON string payloads, optionally falling back to comma-separated tokens."""
	if not isinstance(raw, str):
		return raw
	stripped = raw.strip()
	if not stripped:
		return []
	try:
		return frappe.parse_json(stripped)
	except Exception:
		try:
			return json.loads(stripped)
		except Exception:
			if allow_csv and not stripped.startswith(("[", "{")):
				return [token.strip() for token in stripped.split(",") if token.strip()]
			if stripped.startswith(("[", "{")):
				frappe.throw(_("Invalid JSON format in estimation payload."), frappe.ValidationError)
			return []


def _parse_numeric(
	value: Any,
	*,
	label: str,
	as_int: bool = False,
	min_val: Optional[float] = None,
	max_val: Optional[float] = None,
	min_exclusive: bool = False,
	max_exclusive: bool = False,
	scale_percent: bool = False,
) -> Union[int, float]:
	"""Strictly parse a finite numeric value (rejecting booleans) and enforce bounds."""
	if isinstance(value, bool):
		frappe.throw(_("{0} cannot be a boolean value.").format(label), frappe.ValidationError)
	try:
		num = float(value)
	except (ValueError, TypeError):
		frappe.throw(_("Invalid numeric value for {0}: {1}").format(label, value), frappe.ValidationError)

	if not math.isfinite(num):
		frappe.throw(_("{0} must be a finite number.").format(label), frappe.ValidationError)

	if scale_percent and num > 1.0:
		num = num / 100.0

	if min_val is not None:
		too_small = num <= min_val if min_exclusive else num < min_val
		if too_small:
			op = ">" if min_exclusive else ">="
			frappe.throw(_("{0} must be {1} {2}.").format(label, op, min_val), frappe.ValidationError)

	if max_val is not None:
		too_large = num >= max_val if max_exclusive else num > max_val
		if too_large:
			op = "<" if max_exclusive else "<="
			frappe.throw(_("{0} must be {1} {2}.").format(label, op, max_val), frappe.ValidationError)

	return int(num) if as_int else num


def _get_numeric_setting(
	settings,
	fieldname: str,
	default: Union[int, float],
	*,
	as_int: bool = False,
	min_val: Optional[float] = None,
	max_val: Optional[float] = None,
	min_exclusive: bool = False,
	max_exclusive: bool = False,
	scale_percent: bool = False,
) -> Union[int, float]:
	"""Read and validate a numeric setting from ISM Settings, returning default if unset/invalid."""
	val = getattr(settings, fieldname, None) if settings else None
	if val is None or val == "":
		return default
	try:
		return _parse_numeric(
			val,
			label=fieldname,
			as_int=as_int,
			min_val=min_val,
			max_val=max_val,
			min_exclusive=min_exclusive,
			max_exclusive=max_exclusive,
			scale_percent=scale_percent,
		)
	except frappe.ValidationError:
		return default


def _parse_bool_flag(value: Any, default: bool = False) -> bool:
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, int):
		return bool(value)
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	frappe.throw(_("Invalid boolean flag in estimation options."), frappe.ValidationError)


def _resolve_estimation_options(settings, kwargs: Dict[str, Any]) -> Dict[str, Any]:
	"""Resolve and validate shared estimation kwargs against ISM Settings defaults."""
	raw_pct = kwargs.get("percentile")
	if raw_pct is not None and raw_pct != "":
		percentile = float(
			_parse_numeric(
				raw_pct,
				label="percentile",
				min_val=_MIN_PERCENTILE,
				max_val=_MAX_PERCENTILE,
				max_exclusive=True,
				scale_percent=True,
			)
		)
	else:
		percentile = float(
			_get_numeric_setting(
				settings,
				"estimation_percentile",
				0.80,
				min_val=_MIN_PERCENTILE,
				max_val=_MAX_PERCENTILE,
				max_exclusive=True,
				scale_percent=True,
			)
		)

	raw_buf = kwargs.get("buffer_padding")
	if raw_buf is None:
		raw_buf = kwargs.get("buffer_padding_minutes")
	if raw_buf is not None and raw_buf != "":
		buffer_padding = int(_parse_numeric(raw_buf, label="buffer_padding", as_int=True, min_val=0))
	else:
		buffer_padding = int(
			_get_numeric_setting(settings, "buffer_padding_minutes", 0, as_int=True, min_val=0)
		)

	settings_sigma = float(
		_get_numeric_setting(settings, "estimation_sigma", DEFAULT_SIGMA, min_val=_MIN_SIGMA, max_val=_MAX_SIGMA)
	)
	raw_sigma = kwargs.get("sigma")
	if raw_sigma is not None and raw_sigma != "":
		sigma_override: Optional[float] = float(
			_parse_numeric(raw_sigma, label="sigma", min_val=_MIN_SIGMA, max_val=_MAX_SIGMA)
		)
		default_sigma = sigma_override
	else:
		sigma_override = None
		default_sigma = settings_sigma

	raw_fallback = kwargs.get("fallback_frt")
	if raw_fallback is not None and raw_fallback != "":
		fallback_frt: Optional[float] = float(
			_parse_numeric(raw_fallback, label="fallback_frt", min_val=0.0)
		)
	else:
		fallback_frt = None

	default_diag = float(
		_get_numeric_setting(settings, "default_diagnostic_duration", 60.0, min_val=0.0, min_exclusive=True)
	)
	skip_missing = _parse_bool_flag(kwargs.get("skip_missing", False), default=False)
	empirical = _parse_bool_flag(kwargs.get("empirical", False), default=False)

	return {
		"percentile": percentile,
		"buffer_padding": buffer_padding,
		"sigma_override": sigma_override,
		"default_sigma": default_sigma,
		"fallback_frt": fallback_frt,
		"default_diagnostic": default_diag,
		"skip_missing": skip_missing,
		"empirical": empirical,
	}


def _compute_empirical_percentile_minutes(
	key_names: List[str],
	percentile: float,
	*,
	item_code: Optional[str] = None,
	min_samples: int = 3,
) -> Optional[Tuple[int, int, float]]:
	"""Compute empirical percentile duration (minutes) from completed closed Repair Order Service history.

	Returns (ceil_minutes, sample_count, raw_percentile_minutes) or None when fewer than min_samples exist.
	"""
	clean_keys = [str(k).strip() for k in (key_names or []) if isinstance(k, str) and k.strip()]
	clean_item = item_code.strip() if isinstance(item_code, str) and item_code.strip() else None
	if not clean_keys and not clean_item:
		return None
	if not frappe.db.exists("DocType", "Repair Order") or not frappe.db.exists("DocType", "Repair Order Service"):
		return None

	ros = frappe.qb.DocType("Repair Order Service")
	ro = frappe.qb.DocType("Repair Order")

	match_cond = None
	if clean_keys:
		match_cond = (ros.service_package.isin(clean_keys)) | (ros.service_title.isin(clean_keys))
	if clean_item:
		item_part_cond = ros.parts.like(f'%"{clean_item}"%')
		pkg_or_title = (ros.service_package == clean_item) | (ros.service_title == clean_item) | item_part_cond
		match_cond = (match_cond | pkg_or_title) if match_cond is not None else pkg_or_title

	rows = (
		frappe.qb.from_(ros)
		.inner_join(ro)
		.on((ros.parent == ro.name) & (ros.parenttype == "Repair Order"))
		.select(ros.actual_duration_hours, ros.service_package, ros.service_title, ros.parts)
		.where(ro.status == "Closed / Released")
		.where(ros.is_completed == 1)
		.where(ros.actual_duration_hours > 0)
		.where(match_cond)
		.run(as_dict=True)
	)

	key_set = set(clean_keys)
	samples: List[float] = []
	for row in rows:
		hrs = flt(row.get("actual_duration_hours"))
		if hrs <= 0:
			continue
		if clean_item:
			matched_direct = row.get("service_package") in key_set or row.get("service_title") in key_set or row.get("service_package") == clean_item or row.get("service_title") == clean_item
			if not matched_direct:
				part_codes = _extract_part_item_codes(row.get("parts"))
				if clean_item not in part_codes:
					continue
		samples.append(hrs * 60.0)

	if len(samples) < min_samples:
		return None

	samples.sort()
	n = len(samples)
	idx = min(n - 1, max(0, math.ceil(float(percentile) * n) - 1))
	raw_val = float(samples[idx])
	return (max(1, math.ceil(raw_val)), n, raw_val)


def _prefetch_items_meta(item_codes: List[str]) -> Dict[str, Dict[str, Any]]:
	"""Batch-load item_group and optional custom_frt for a list of item codes."""
	unique_codes = list({str(c).strip() for c in item_codes if isinstance(c, str) and c.strip()})
	if not unique_codes or not frappe.db.exists("DocType", "Item"):
		return {}

	fields = ["name", "item_group"]
	if frappe.db.has_column("Item", "custom_frt"):
		fields.append("custom_frt")

	rows = frappe.get_list("Item", filters={"name": ["in", unique_codes]}, fields=fields)
	return {r["name"]: r for r in rows}


def _prefetch_package_hours(package_names: List[str]) -> Dict[str, float]:
	"""Batch-load standard_labor_hours for Service Packages."""
	unique_pkgs = list({str(p).strip() for p in package_names if p})
	if not unique_pkgs or not frappe.db.exists("DocType", "Service Package"):
		return {}
	rows = frappe.get_list(
		"Service Package",
		filters={"name": ["in", unique_pkgs]},
		fields=["name", "standard_labor_hours"],
	)
	out: Dict[str, float] = {}
	for r in rows:
		val = flt(r.get("standard_labor_hours"))
		if val > 0:
			out[r["name"]] = val
	return out


def _extract_part_item_codes(parts_raw: Any) -> List[str]:
	"""Extract item codes from JSON string, list of dicts, or child documents."""
	if not parts_raw:
		return []
	parsed = _parse_json_value(parts_raw, allow_csv=False) if isinstance(parts_raw, str) else parts_raw
	if not isinstance(parsed, (list, tuple)):
		return []
	codes: List[str] = []
	for entry in parsed:
		code = entry.get("item_code") if isinstance(entry, dict) else getattr(entry, "item_code", None)
		if isinstance(code, str) and code.strip():
			codes.append(code.strip())
	return codes


def _estimate_operations_core(
	operations: List[Dict[str, float]],
	*,
	percentile: float,
	buffer_padding: int,
) -> Dict[str, Any]:
	"""Run Fenton-Wilkinson summation + buffer over pre-resolved operations."""
	if not operations:
		return {
			"total_duration_minutes": 0,
			"p_pure": 0,
			"buffer_padding": buffer_padding,
			"operations_count": 0,
		}

	p_pure = estimate_total_duration(operations, percentile=percentile)
	return {
		"total_duration_minutes": p_pure + buffer_padding,
		"p_pure": p_pure,
		"buffer_padding": buffer_padding,
		"operations_count": len(operations),
	}


def _resolve_explicit_frt_minutes(source: Dict[str, Any], allow_qty_alias: bool = False) -> Optional[float]:
	"""Resolve explicit flat rate time in minutes from minute or hour keys, rejecting booleans."""
	for min_key in ("flat_rate_minutes", "frt_minutes"):
		if source.get(min_key) is not None and source.get(min_key) != "":
			return float(_parse_numeric(source[min_key], label=min_key, min_val=0.0))

	hour_keys = ("flat_rate_hours", "frt_hours", "custom_frt", "qty") if allow_qty_alias else (
		"custom_frt",
		"flat_rate_hours",
	)
	for hr_key in hour_keys:
		if source.get(hr_key) is not None and source.get(hr_key) != "":
			return float(_parse_numeric(source[hr_key], label=hr_key, min_val=0.0)) * 60.0

	return None


@frappe.whitelist()
def get_estimate(item_code: Optional[str] = None, **kwargs) -> Dict[str, Any]:
	"""Get risk-adjusted duration estimate for a single operation."""
	_ensure_authenticated_estimation_user()
	if item_code is not None and not isinstance(item_code, str):
		frappe.throw(_("Invalid item_code identifier."), frappe.ValidationError)

	clean_code = item_code.strip() if isinstance(item_code, str) and item_code.strip() else None
	if clean_code:
		frappe.has_permission("Item", "read", throw=True)

	settings = _get_ism_settings()
	opts = _resolve_estimation_options(settings, kwargs)
	items_meta = _prefetch_items_meta([clean_code]) if clean_code else {}
	meta = items_meta.get(clean_code) if clean_code else None

	frt = _resolve_explicit_frt_minutes(kwargs, allow_qty_alias=False)
	if frt is None and clean_code:
		raw_custom_frt = meta.get("custom_frt") if meta else None
		if raw_custom_frt is not None and flt(raw_custom_frt) > 0:
			frt = flt(raw_custom_frt) * 60.0
		else:
			frt = opts["fallback_frt"] if opts["fallback_frt"] is not None else opts["default_diagnostic"]
	elif frt is None and opts["fallback_frt"] is not None:
		frt = opts["fallback_frt"]

	if opts["sigma_override"] is not None:
		sigma = opts["sigma_override"]
	elif meta and meta.get("item_group"):
		sigma = get_sigma_for_item(meta.get("item_group"), default_sigma=opts["default_sigma"])
	else:
		sigma = opts["default_sigma"]

	if opts["empirical"]:
		emp_keys = [
			str(k).strip()
			for k in (kwargs.get("service_package"), kwargs.get("service_title"))
			if isinstance(k, str) and k.strip()
		]
		emp = _compute_empirical_percentile_minutes(
			emp_keys,
			opts["percentile"],
			item_code=clean_code,
		)
		if emp is not None:
			emp_p_pure, sample_count, _ = emp
			return {
				"duration_minutes": emp_p_pure + opts["buffer_padding"],
				"p_pure": emp_p_pure,
				"buffer_padding": opts["buffer_padding"],
				"percentile": opts["percentile"],
				"sigma": sigma,
				"empirical_used": True,
				"sample_count": sample_count,
			}

	if frt is None or frt <= 0:
		res = {
			"duration_minutes": 0,
			"p_pure": 0,
			"buffer_padding": opts["buffer_padding"],
			"percentile": opts["percentile"],
			"sigma": sigma,
		}
		if opts["empirical"]:
			res["empirical_used"] = False
			res["sample_count"] = 0
		return res

	p_pure = estimate_duration(frt, sigma=sigma, percentile=opts["percentile"])
	res = {
		"duration_minutes": p_pure + opts["buffer_padding"],
		"p_pure": p_pure,
		"buffer_padding": opts["buffer_padding"],
		"percentile": opts["percentile"],
		"sigma": sigma,
	}
	if opts["empirical"]:
		res["empirical_used"] = False
		res["sample_count"] = 0
	return res


@frappe.whitelist()
def get_total_estimate(
	item_codes: Union[str, List[Union[str, Dict[str, Any]]]],
	**kwargs,
) -> Dict[str, Any]:
	"""Get multi-operation duration estimate using Fenton-Wilkinson summation + buffer."""
	_ensure_authenticated_estimation_user()
	settings = _get_ism_settings()
	opts = _resolve_estimation_options(settings, kwargs)

	parsed_items = _parse_json_value(item_codes, allow_csv=True) if isinstance(item_codes, str) else item_codes
	if parsed_items is None or parsed_items == [] or parsed_items == ():
		return _estimate_operations_core([], percentile=opts["percentile"], buffer_padding=opts["buffer_padding"])
	if not isinstance(parsed_items, (list, tuple)):
		frappe.throw(_("Operations must be a list of item codes or operation specs."), frappe.ValidationError)

	normalized_items = []
	candidate_codes: List[str] = []
	for item in parsed_items:
		if isinstance(item, bool):
			frappe.throw(_("Boolean entries are not permitted in operations list."), frappe.ValidationError)
		if isinstance(item, dict):
			raw_code = item.get("item_code")
			if raw_code is not None and not isinstance(raw_code, str):
				frappe.throw(_("Invalid item_code: expected string."), frappe.ValidationError)
			code = raw_code.strip() if isinstance(raw_code, str) and raw_code.strip() else None
			if code:
				candidate_codes.append(code)
			item_frt = _resolve_explicit_frt_minutes(item, allow_qty_alias=True)
			raw_sig = item.get("sigma")
			if raw_sig is not None and raw_sig != "":
				item_sig: Optional[float] = float(
					_parse_numeric(raw_sig, label="sigma", min_val=_MIN_SIGMA, max_val=_MAX_SIGMA)
				)
			else:
				item_sig = None
		elif isinstance(item, (int, float)):
			code = None
			item_frt = float(_parse_numeric(item, label="flat_rate_minutes", min_val=0.0))
			item_sig = None
		elif isinstance(item, str):
			code = item.strip() or None
			if code:
				candidate_codes.append(code)
			item_frt = None
			item_sig = None
		elif item is None:
			continue
		else:
			frappe.throw(
				_("Invalid item entry in operations: expected string, number, or dict."),
				frappe.ValidationError,
			)

		normalized_items.append((code, item_frt, item_sig))

	if candidate_codes:
		frappe.has_permission("Item", "read", throw=True)
	items_meta = _prefetch_items_meta(candidate_codes) if candidate_codes else {}

	operations: List[Dict[str, float]] = []
	for code, item_frt, item_sig in normalized_items:
		meta = items_meta.get(code) if code else None

		if item_frt is None and code:
			frt_hours = meta.get("custom_frt") if meta else None
			if frt_hours is not None and flt(frt_hours) > 0:
				item_frt = flt(frt_hours) * 60.0
			elif not opts["skip_missing"]:
				item_frt = opts["fallback_frt"] if opts["fallback_frt"] is not None else opts["default_diagnostic"]

		if item_frt is None or item_frt <= 0:
			if opts["skip_missing"]:
				continue
			item_frt = opts["fallback_frt"] if opts["fallback_frt"] and opts["fallback_frt"] > 0 else None

		if item_frt is None or item_frt <= 0:
			continue

		if item_sig is None:
			if meta and meta.get("item_group"):
				item_sig = get_sigma_for_item(meta.get("item_group"), default_sigma=opts["default_sigma"])
			else:
				item_sig = opts["default_sigma"]

		operations.append({"flat_rate_minutes": float(item_frt), "sigma": float(item_sig)})

	return _estimate_operations_core(
		operations,
		percentile=opts["percentile"],
		buffer_padding=opts["buffer_padding"],
	)


def _resolve_service_frt_minutes(
	s,
	package_hours: Optional[Dict[str, float]] = None,
	items_meta: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[float]:
	"""Extract flat rate time in minutes from a Repair Order Service row."""
	for attr in ("labor_hours", "frt"):
		raw = getattr(s, attr, None)
		if raw is not None and not isinstance(raw, bool):
			try:
				val = float(raw)
				if math.isfinite(val) and val > 0:
					return val * 60.0
			except (ValueError, TypeError):
				pass

	pkg_name = getattr(s, "service_package", None)
	if pkg_name:
		if package_hours is not None:
			if pkg_name in package_hours:
				return package_hours[pkg_name] * 60.0
		elif frappe.db.exists("Service Package", pkg_name):
			pkg_hr = frappe.db.get_value("Service Package", pkg_name, "standard_labor_hours")
			if pkg_hr is not None and flt(pkg_hr) > 0:
				return flt(pkg_hr) * 60.0

	item_code = getattr(s, "item_code", None)
	if isinstance(item_code, str) and item_code.strip():
		code = item_code.strip()
		meta_map = items_meta if items_meta is not None else _prefetch_items_meta([code])
		meta = meta_map.get(code)
		if meta and flt(meta.get("custom_frt")) > 0:
			return flt(meta.get("custom_frt")) * 60.0

	return None


def _resolve_service_sigma(
	s,
	default_sigma: float,
	items_meta: Optional[Dict[str, Dict[str, Any]]] = None,
) -> float:
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

	part_codes = _extract_part_item_codes(getattr(s, "parts", None))
	if part_codes and items_meta is None:
		items_meta = _prefetch_items_meta(part_codes)

	for code in part_codes:
		meta = items_meta.get(code) if items_meta else None
		item_group = meta.get("item_group") if meta else None
		if item_group:
			sig = get_sigma_for_item(item_group, default_sigma=default_sigma)
			if sig != default_sigma:
				return sig

	return default_sigma


@frappe.whitelist()
def estimate_repair_order(repair_order: Optional[str] = None, **kwargs) -> Dict[str, Any]:
	"""Estimate duration for a Repair Order based on its active services."""
	if not repair_order:
		frappe.has_permission("Repair Order", "read", throw=True)
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
		ro.check_permission("create" if ro.is_new() else "read")
		repair_order_name = ro.name
	else:
		if not isinstance(repair_order, str):
			frappe.throw(_("Invalid Repair Order identifier."), frappe.ValidationError)
		frappe.has_permission("Repair Order", "read", throw=True)
		repair_order_name = repair_order.strip()
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
				_("Repair Order {0} does not exist").format(repair_order_name),
				frappe.DoesNotExistError,
			)
		ro = frappe.get_doc("Repair Order", repair_order_name)
		ro.check_permission("read")

	settings = _get_ism_settings()
	opts = _resolve_estimation_options(settings, kwargs)

	services = [s for s in (getattr(ro, "services", []) or []) if getattr(s, "status", None) != "Declined"]
	pkg_names = [getattr(s, "service_package", None) for s in services if getattr(s, "service_package", None)]
	item_codes: List[str] = []
	for s in services:
		item_code = getattr(s, "item_code", None)
		if item_code:
			item_codes.append(str(item_code).strip())
		item_codes.extend(_extract_part_item_codes(getattr(s, "parts", None)))

	package_hours = _prefetch_package_hours(pkg_names)
	items_meta = _prefetch_items_meta(item_codes)

	operations: List[Dict[str, float]] = []
	empirical_sample_counts: Dict[str, int] = {}
	single_emp_p_pure: Optional[int] = None
	z_pct = _Z_SCORES.get(opts["percentile"])
	if z_pct is None:
		z_pct = approximate_z_score(opts["percentile"])

	for s in services:
		sig = _resolve_service_sigma(s, opts["default_sigma"], items_meta=items_meta)
		if opts["empirical"]:
			emp_keys = [
				str(v).strip()
				for v in (getattr(s, "service_package", None), getattr(s, "service_title", None))
				if isinstance(v, str) and v.strip()
			]
			emp = _compute_empirical_percentile_minutes(emp_keys, opts["percentile"])
			if emp is not None:
				emp_ceil, emp_count, raw_emp_min = emp
				label = getattr(s, "service_package", None) or getattr(s, "service_title", None) or f"row_{len(operations) + 1}"
				empirical_sample_counts[label] = emp_count
				single_emp_p_pure = emp_ceil
				eff_frt = raw_emp_min / math.exp(float(sig) * float(z_pct))
				operations.append({"flat_rate_minutes": float(max(0.1, eff_frt)), "sigma": float(sig)})
				continue

		frt_min = _resolve_service_frt_minutes(s, package_hours=package_hours, items_meta=items_meta)
		if frt_min is not None and frt_min > 0:
			operations.append({"flat_rate_minutes": float(frt_min), "sigma": float(sig)})

	if not operations:
		diag_duration = opts["fallback_frt"] if opts["fallback_frt"] is not None else opts["default_diagnostic"]
		if diag_duration > 0:
			diag_sigma = get_sigma_for_item("Diagnostic", default_sigma=opts["default_sigma"])
			operations.append({
				"flat_rate_minutes": float(diag_duration),
				"sigma": float(diag_sigma),
			})

	if opts["empirical"] and len(operations) == 1 and single_emp_p_pure is not None and len(empirical_sample_counts) == 1:
		res = {
			"total_duration_minutes": single_emp_p_pure + opts["buffer_padding"],
			"p_pure": single_emp_p_pure,
			"buffer_padding": opts["buffer_padding"],
			"operations_count": 1,
		}
	else:
		res = _estimate_operations_core(
			operations,
			percentile=opts["percentile"],
			buffer_padding=opts["buffer_padding"],
		)
	if opts["empirical"]:
		res["empirical_used"] = bool(empirical_sample_counts)
		res["empirical_sample_counts"] = empirical_sample_counts
	res["total_minutes"] = res["total_duration_minutes"]
	res["repair_order"] = repair_order_name
	return res
