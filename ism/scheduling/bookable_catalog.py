# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import frappe
from frappe import _
from frappe.utils import cint, flt

from ism.scheduling.capacity import parse_required_tags
from ism.scheduling.estimation_light import estimate_total_duration

ALLOWED_PRICE_DISPLAY_MODES: Tuple[str, ...] = (
	"Exact Price",
	"Starting At",
	"Estimate Range",
	"Contact for Quote",
)
MAX_PACKAGES_PER_BOOKING: int = 10

_MODEL_ALIASES: Dict[str, str] = {
	"model 3": "Model 3",
	"model3": "Model 3",
	"m3": "Model 3",
	"model y": "Model Y",
	"modely": "Model Y",
	"my": "Model Y",
	"model s": "Model S",
	"models": "Model S",
	"ms": "Model S",
	"model x": "Model X",
	"modelx": "Model X",
	"mx": "Model X",
	"cybertruck": "Cybertruck",
	"ct": "Cybertruck",
	"roadster": "Roadster",
	"all models": "All Models",
	"all": "All Models",
}

_YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")
_OPEN_END_PATTERN = re.compile(r"(?:\bpresent\b|\bcurrent\b|\bonward\b|\+)", re.IGNORECASE)

_CATALOG_QUERY_FIELDS: List[str] = [
	"name",
	"package_name",
	"applicable_model",
	"applicable_generation",
	"applicable_year_from",
	"applicable_year_to",
	"is_mobile_capable",
	"allow_self_serve_booking",
	"self_serve_sort_order",
	"customer_title",
	"customer_summary",
	"customer_price_display_mode",
	"customer_estimate_min",
	"customer_estimate_max",
	"standard_labor_hours",
	"hourly_rate",
	"service_rate",
	"self_serve_duration_minutes",
	"self_serve_required_tags",
]


def normalize_vehicle_model(vehicle_model: Optional[str]) -> Optional[str]:
	"""Normalize vehicle model inputs (e.g. 'tesla model 3' -> 'Model 3', 'model y' -> 'Model Y')."""
	if not vehicle_model or not isinstance(vehicle_model, str):
		return None
	cleaned = re.sub(r"\s+", " ", vehicle_model.strip())
	if not cleaned:
		return None
	stripped = re.sub(r"^tesla\s+", "", cleaned, flags=re.IGNORECASE).strip()
	if not stripped:
		return None
	return _MODEL_ALIASES.get(stripped.lower(), stripped)


def parse_generation_year_bounds(applicable_generation: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
	"""Extract (min_year, max_year) from Tesla generation strings like 'Model 3 Jun 2017 - Dec 2023'
	or 'Model Y Jan 2020 - Present' (open upper bound -> None)."""
	if not applicable_generation or not isinstance(applicable_generation, str):
		return (None, None)

	years = [int(y) for y in _YEAR_PATTERN.findall(applicable_generation)]
	if not years:
		return (None, None)

	min_year = min(years)
	if _OPEN_END_PATTERN.search(applicable_generation) or len(years) == 1:
		return (min_year, None)

	return (min_year, max(years))


def is_package_eligible_for_vehicle(
	pkg: Dict[str, Any],
	vehicle_model: Optional[str] = None,
	vehicle_year: Optional[int] = None,
) -> bool:
	"""Return True iff pkg matches vehicle_model (exact or 'All Models'/blank) and vehicle_year
	(explicit applicable_year_from/to if set, else parsed applicable_generation bounds)."""
	req_model = normalize_vehicle_model(vehicle_model)
	pkg_model = normalize_vehicle_model(pkg.get("applicable_model"))

	if req_model and req_model != "All Models":
		if pkg_model and pkg_model != "All Models" and pkg_model != req_model:
			return False

	if vehicle_year is not None:
		try:
			year = int(vehicle_year)
		except (TypeError, ValueError):
			return False

		yr_from = cint(pkg.get("applicable_year_from") or 0)
		yr_to = cint(pkg.get("applicable_year_to") or 0)

		if yr_from > 0 or yr_to > 0:
			if yr_from > 0 and year < yr_from:
				return False
			if yr_to > 0 and year > yr_to:
				return False
		else:
			gen_min, gen_max = parse_generation_year_bounds(pkg.get("applicable_generation"))
			if gen_min is not None and year < gen_min:
				return False
			if gen_max is not None and year > gen_max:
				return False

	return True


def _format_money(amount: float, currency: str) -> str:
	return f"${amount:,.2f} {currency}"


def format_package_price_display(
	pkg: Dict[str, Any],
	*,
	items_subtotal: float = 0.0,
	default_labor_rate: float = 140.0,
	currency: str = "CAD",
) -> Dict[str, Any]:
	"""Compute customer-facing price display dict:
	{'price_display_mode', 'currency', 'price_min', 'price_max', 'price_display_label'}.
	Falls back to package service_rate (or FRT * rate) + default parts subtotal when explicit estimate_min is 0,
	and degrades cleanly to 'Contact for Quote' if no price can be computed."""
	raw_mode = (pkg.get("customer_price_display_mode") or "Exact Price").strip()
	mode = raw_mode if raw_mode in ALLOWED_PRICE_DISPLAY_MODES else "Contact for Quote"

	if mode == "Contact for Quote":
		return {
			"price_display_mode": "Contact for Quote",
			"currency": currency,
			"price_min": None,
			"price_max": None,
			"price_display_label": "Contact for Quote",
		}

	est_min = round(flt(pkg.get("customer_estimate_min") or 0), 2)
	est_max = round(flt(pkg.get("customer_estimate_max") or 0), 2)

	labor = flt(pkg.get("service_rate") or 0)
	if labor <= 0:
		frt = flt(pkg.get("standard_labor_hours") or 0)
		rate = flt(pkg.get("hourly_rate") or 0) or flt(default_labor_rate)
		if frt > 0 and rate > 0:
			labor = frt * rate
	fallback_total = round(labor + flt(items_subtotal or 0), 2)

	base_min = est_min if est_min > 0 else fallback_total
	if base_min <= 0:
		return {
			"price_display_mode": "Contact for Quote",
			"currency": currency,
			"price_min": None,
			"price_max": None,
			"price_display_label": "Contact for Quote",
		}

	if mode == "Exact Price":
		return {
			"price_display_mode": "Exact Price",
			"currency": currency,
			"price_min": base_min,
			"price_max": base_min,
			"price_display_label": _format_money(base_min, currency),
		}

	if mode == "Starting At":
		return {
			"price_display_mode": "Starting At",
			"currency": currency,
			"price_min": base_min,
			"price_max": None,
			"price_display_label": f"Starting at {_format_money(base_min, currency)}",
		}

	# Estimate Range
	if est_max > 0 and est_max >= base_min:
		return {
			"price_display_mode": "Estimate Range",
			"currency": currency,
			"price_min": base_min,
			"price_max": est_max,
			"price_display_label": f"{_format_money(base_min, currency)} – {_format_money(est_max, currency)}",
		}

	return {
		"price_display_mode": "Starting At",
		"currency": currency,
		"price_min": base_min,
		"price_max": None,
		"price_display_label": f"Starting at {_format_money(base_min, currency)}",
	}


def _get_default_items_subtotals(package_names: Sequence[str]) -> Dict[str, float]:
	if not package_names or not frappe.db.exists("DocType", "Service Package Item"):
		return {}
	rows = frappe.get_all(
		"Service Package Item",
		filters={"parent": ["in", list(package_names)], "is_default": 1},
		fields=["parent", "qty", "rate"],
		ignore_permissions=True,
	)
	totals: Dict[str, float] = {}
	for row in rows:
		parent = row.get("parent")
		if not parent:
			continue
		totals[parent] = round(totals.get(parent, 0.0) + flt(row.get("qty") or 0) * flt(row.get("rate") or 0), 2)
	return totals


def _get_catalog_settings() -> Dict[str, Any]:
	defaults: Dict[str, Any] = {
		"default_labor_rate": 140.0,
		"currency": "CAD",
		"estimation_sigma": 0.30,
		"estimation_percentile": 0.80,
		"buffer_padding_minutes": 0,
		"default_diagnostic_duration": 60,
	}
	if not frappe.db.exists("DocType", "ISM Settings"):
		return defaults
	try:
		doc = frappe.get_single("ISM Settings")
	except frappe.DoesNotExistError:
		return defaults

	raw_rate = flt(getattr(doc, "default_labor_rate", 0) or 0)
	if raw_rate > 0:
		defaults["default_labor_rate"] = raw_rate

	raw_sigma = getattr(doc, "estimation_sigma", None)
	if raw_sigma is not None and flt(raw_sigma) > 0:
		defaults["estimation_sigma"] = min(2.0, max(0.01, flt(raw_sigma)))

	raw_pct = getattr(doc, "estimation_percentile", None)
	if raw_pct is not None and flt(raw_pct) > 0:
		pct = flt(raw_pct) / 100.0 if flt(raw_pct) > 1.0 else flt(raw_pct)
		defaults["estimation_percentile"] = min(0.99, max(0.50, pct))

	defaults["buffer_padding_minutes"] = max(0, cint(getattr(doc, "buffer_padding_minutes", 0) or 0))
	diag = cint(getattr(doc, "default_diagnostic_duration", 60) or 60)
	defaults["default_diagnostic_duration"] = max(15, diag)
	return defaults


def _build_public_catalog_card(
	pkg: Dict[str, Any],
	*,
	items_subtotal: float,
	default_labor_rate: float,
	currency: str,
) -> Dict[str, Any]:
	title = (pkg.get("customer_title") or "").strip() or (pkg.get("package_name") or pkg.get("name") or "").strip()
	summary = (pkg.get("customer_summary") or "").strip()
	price_info = format_package_price_display(
		pkg,
		items_subtotal=items_subtotal,
		default_labor_rate=default_labor_rate,
		currency=currency,
	)
	sort_order = pkg.get("self_serve_sort_order")
	return {
		"package_id": pkg["name"],
		"title": title,
		"summary": summary,
		"applicable_model": pkg.get("applicable_model") or "All Models",
		"is_mobile_capable": cint(pkg.get("is_mobile_capable") or 0),
		"sort_order": cint(sort_order) if sort_order is not None else 100,
		"price_display_mode": price_info["price_display_mode"],
		"currency": price_info["currency"],
		"price_min": price_info["price_min"],
		"price_max": price_info["price_max"],
		"price_display_label": price_info["price_display_label"],
	}


def list_bookable_packages(
	*,
	vehicle_model: Optional[str] = None,
	vehicle_year: Optional[int] = None,
	search: Optional[str] = None,
) -> List[Dict[str, Any]]:
	"""Return sorted customer-safe catalog cards for Service Packages with allow_self_serve_booking = 1.
	Each item contains ONLY:
	{
	  'package_id', 'title', 'summary', 'applicable_model',
	  'is_mobile_capable', 'sort_order',
	  'price_display_mode', 'currency', 'price_min', 'price_max', 'price_display_label',
	}
	Strictly omits correction_code, rh_correction_code, hourly_rate, tesla_manual_url,
	doc_guid, internal item_codes, supplier costs, and internal duration estimation parameters."""
	rows = frappe.get_all(
		"Service Package",
		filters={"allow_self_serve_booking": 1},
		fields=_CATALOG_QUERY_FIELDS,
		order_by="self_serve_sort_order asc, customer_title asc, package_name asc",
		ignore_permissions=True,
	)
	if not rows:
		return []

	search_term = search.strip().lower() if isinstance(search, str) and search.strip() else None
	settings = _get_catalog_settings()
	subtotals = _get_default_items_subtotals([r["name"] for r in rows])

	cards: List[Dict[str, Any]] = []
	for pkg in rows:
		if not is_package_eligible_for_vehicle(pkg, vehicle_model=vehicle_model, vehicle_year=vehicle_year):
			continue

		card = _build_public_catalog_card(
			pkg,
			items_subtotal=subtotals.get(pkg["name"], 0.0),
			default_labor_rate=settings["default_labor_rate"],
			currency=settings["currency"],
		)
		if search_term:
			haystack = f"{card['title']} {card['summary']}".lower()
			if search_term not in haystack:
				continue
		cards.append(card)

	cards.sort(key=lambda c: (c["sort_order"], c["title"].lower(), c["package_id"]))
	return cards


def _parse_package_ids(package_ids: Union[str, Sequence[str]]) -> List[str]:
	if package_ids is None or isinstance(package_ids, (bool, int, float, dict)):
		frappe.throw(_("Invalid package_ids parameter."))

	raw_list: List[str] = []
	if isinstance(package_ids, str):
		stripped = package_ids.strip()
		if not stripped:
			frappe.throw(_("At least one service package must be selected."))
		if stripped.startswith("[") or stripped.startswith("{"):
			try:
				parsed = frappe.parse_json(stripped)
			except (ValueError, TypeError):
				frappe.throw(_("Invalid package_ids parameter."))
			if not isinstance(parsed, (list, tuple)):
				frappe.throw(_("Invalid package_ids parameter."))
			raw_list = [str(item).strip() for item in parsed if isinstance(item, str) and item.strip()]
		else:
			raw_list = [p.strip() for p in stripped.split(",") if p.strip()]
	elif isinstance(package_ids, (list, tuple, set)):
		for item in package_ids:
			if not isinstance(item, str):
				frappe.throw(_("Invalid package_ids parameter."))
			if item.strip():
				raw_list.append(item.strip())
	else:
		frappe.throw(_("Invalid package_ids parameter."))

	deduped = list(dict.fromkeys(raw_list))
	if not deduped:
		frappe.throw(_("At least one service package must be selected."))
	return deduped


def compute_packages_booking_requirements(
	package_ids: Union[str, Sequence[str]],
	*,
	vehicle_model: Optional[str] = None,
	vehicle_year: Optional[int] = None,
) -> Dict[str, Any]:
	"""Server-side resolver validating all package_ids exist, have allow_self_serve_booking = 1,
	and match vehicle_model/vehicle_year (capped at MAX_PACKAGES_PER_BOOKING).
	Computes:
	- duration_minutes (int >= 15): deterministic sum of self_serve_duration_minutes overrides PLUS
	  Fenton-Wilkinson estimate_total_duration() over FRT-derived packages + buffer_padding_minutes.
	- required_tags (List[str]): deduplicated sorted union of self_serve_required_tags across packages.
	- items_summary (str): clean bullet list for Schedule Entry.items_summary.
	- booked_packages_snapshot (List[Dict[str, Any]]): JSON-serializable customer snapshot for
	  Schedule Entry.booked_packages_json.
	- packages (List[Dict[str, Any]]): public catalog cards."""
	ids = _parse_package_ids(package_ids)
	if not ids:
		frappe.throw(_("At least one service package must be selected."))
	if len(ids) > MAX_PACKAGES_PER_BOOKING:
		frappe.throw(
			_("Cannot book more than {0} service packages in a single appointment.").format(
				MAX_PACKAGES_PER_BOOKING
			)
		)

	rows = frappe.get_all(
		"Service Package",
		filters={"name": ["in", ids]},
		fields=_CATALOG_QUERY_FIELDS,
		ignore_permissions=True,
	)
	pkg_by_name = {row["name"]: row for row in rows}

	settings = _get_catalog_settings()
	subtotals = _get_default_items_subtotals(ids)

	override_minutes = 0
	frt_minutes_list: List[float] = []
	tags_set = set()
	cards: List[Dict[str, Any]] = []
	snapshot: List[Dict[str, Any]] = []

	for pkg_id in ids:
		pkg = pkg_by_name.get(pkg_id)
		if not pkg:
			frappe.throw(_("Service Package '{0}' was not found.").format(pkg_id))
		if cint(pkg.get("allow_self_serve_booking") or 0) != 1:
			frappe.throw(_("Service Package '{0}' is not available for self-serve booking.").format(pkg_id))

		card = _build_public_catalog_card(
			pkg,
			items_subtotal=subtotals.get(pkg_id, 0.0),
			default_labor_rate=settings["default_labor_rate"],
			currency=settings["currency"],
		)
		if not is_package_eligible_for_vehicle(pkg, vehicle_model=vehicle_model, vehicle_year=vehicle_year):
			frappe.throw(
				_("Service Package '{0}' is not eligible for the selected vehicle.").format(card["title"])
			)

		cards.append(card)
		snapshot.append(
			{
				"package_id": card["package_id"],
				"title": card["title"],
				"price_display_mode": card["price_display_mode"],
				"currency": card["currency"],
				"price_min": card["price_min"],
				"price_max": card["price_max"],
				"price_display_label": card["price_display_label"],
			}
		)

		for tag in parse_required_tags(pkg.get("self_serve_required_tags")):
			tags_set.add(tag)

		override = cint(pkg.get("self_serve_duration_minutes") or 0)
		if override > 0:
			override_minutes += override
		else:
			frt_hours = flt(pkg.get("standard_labor_hours") or 0)
			if frt_hours > 0:
				frt_minutes_list.append(frt_hours * 60.0)
			else:
				frt_minutes_list.append(float(settings["default_diagnostic_duration"]))

	frt_duration = (
		estimate_total_duration(
			frt_minutes_list,
			sigma=settings["estimation_sigma"],
			percentile=settings["estimation_percentile"],
		)
		if frt_minutes_list
		else 0
	)
	total_duration = max(15, override_minutes + frt_duration + settings["buffer_padding_minutes"])

	return {
		"duration_minutes": total_duration,
		"required_tags": sorted(tags_set),
		"items_summary": "\n".join(f"- {card['title']}" for card in cards),
		"booked_packages_snapshot": snapshot,
		"packages": cards,
	}
