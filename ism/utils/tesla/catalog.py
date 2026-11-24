# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import logging
import os
import re

try:
	import frappe
	from frappe.utils import cint
except ImportError:  # Allow standalone Python ingestion/CLI usage without Frappe
	frappe = None

	def cint(value, default=0):
		try:
			return int(float(value))
		except (TypeError, ValueError):
			return default


_LOGGER = logging.getLogger(__name__)
_CATEGORIES_CACHE = None

MONTH_MAP = {
	"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
	"jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
	"january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
	"july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

GENERATION_CONFIG = {
	"ModelS": {
		"label": "Model S",
		"variants": [
			{
				"url_match": "/Palladium/",
				"generations": [
					{
						"name": "Model S Feb 2021 - May 2025",
						"date_range": "Feb 2021 - May 2025",
						"short_label": "Palladium (Feb 2021 - May 2025)",
						"start": (2021, 2),
						"end": (2025, 5),
					},
					{
						"name": "Model S June 2025",
						"date_range": "June 2025 - Present",
						"short_label": "Refresh (June 2025 - Present)",
						"start": (2025, 6),
						"end": None,
					},
				],
				"default_url": "https://service.tesla.com/docs/ModelS/ServiceManual/Palladium/en-us/",
			},
			{
				"url_match": None,  # classic / legacy
				"generations": [
					{
						"name": "Model S Feb 2012 - Mar 2016",
						"date_range": "Feb 2012 - Mar 2016",
						"short_label": "Pre-Facelift (Feb 2012 - Mar 2016)",
						"start": (2012, 2),
						"end": (2016, 3),
					},
					{
						"name": "Model S Apr 2016 - Jan 2021",
						"date_range": "Apr 2016 - Jan 2021",
						"short_label": "Facelift (Apr 2016 - Jan 2021)",
						"start": (2016, 4),
						"end": (2021, 1),
					},
				],
				"default_url": "https://service.tesla.com/docs/ModelS/ServiceManual/en-us/",
			},
		],
	},
	"Model3": {
		"label": "Model 3",
		"variants": [
			{
				"url_match": "/2024/",
				"generations": [
					{
						"name": "Model 3 Jan 2024",
						"date_range": "Jan 2024 - Present",
						"short_label": "Highland (Jan 2024 - Present)",
						"start": (2024, 1),
						"end": None,
					},
				],
				"default_url": "https://service.tesla.com/docs/Model3/ServiceManual/2024/en-us/",
			},
			{
				"url_match": None,
				"generations": [
					{
						"name": "Model 3 Jun 2017 - Dec 2023",
						"date_range": "Jun 2017 - Dec 2023",
						"short_label": "Original (Jun 2017 - Dec 2023)",
						"start": (2017, 6),
						"end": (2023, 12),
					},
				],
				"default_url": "https://service.tesla.com/docs/Model3/ServiceManual/en-us/",
			},
		],
	},
	"ModelX": {
		"label": "Model X",
		"variants": [
			{
				"url_match": "/Palladium/",
				"generations": [
					{
						"name": "Model X Mar 2021 - May 2025",
						"date_range": "Mar 2021 - May 2025",
						"short_label": "Palladium (Mar 2021 - May 2025)",
						"start": (2021, 3),
						"end": (2025, 5),
					},
					{
						"name": "Model X June 2025",
						"date_range": "June 2025 - Present",
						"short_label": "Refresh (June 2025 - Present)",
						"start": (2025, 6),
						"end": None,
					},
				],
				"default_url": "https://service.tesla.com/docs/ModelX/ServiceManual/Palladium/en-us/",
			},
			{
				"url_match": None,
				"generations": [
					{
						"name": "Model X Sep 2015 - Feb 2021",
						"date_range": "Sep 2015 - Feb 2021",
						"short_label": "Legacy (Sep 2015 - Feb 2021)",
						"start": (2015, 9),
						"end": (2021, 2),
					},
				],
				"default_url": "https://service.tesla.com/docs/ModelX/ServiceManual/en-us/",
			},
		],
	},
	"ModelY": {
		"label": "Model Y",
		"variants": [
			{
				"url_match": "/2025/",
				"generations": [
					{
						"name": "Model Y Feb 2025",
						"date_range": "Feb 2025 - Present",
						"short_label": "Juniper (Feb 2025 - Present)",
						"start": (2025, 2),
						"end": None,
					},
				],
				"default_url": "https://service.tesla.com/docs/ModelY/ServiceManual/2025/en-us/",
			},
			{
				"url_match": None,
				"generations": [
					{
						"name": "Model Y Jan 2020 - Jan 2025",
						"date_range": "Jan 2020 - Jan 2025",
						"short_label": "Legacy (Jan 2020 - Jan 2025)",
						"start": (2020, 1),
						"end": (2025, 1),
					},
				],
				"default_url": "https://service.tesla.com/docs/ModelY/ServiceManual/en-us/",
			},
		],
	},
	"Cybertruck": {
		"label": "Cybertruck",
		"variants": [
			{
				"url_match": None,
				"generations": [
					{
						"name": "Cybertruck Nov 2023 - Present",
						"date_range": "Nov 2023 - Present",
						"short_label": "First Gen (Nov 2023 - Present)",
						"start": (2023, 11),
						"end": None,
					},
				],
				"default_url": "https://service.tesla.com/docs/Cybertruck/ServiceManual/en-us/",
			},
		],
	},
}


def _get_categories_dict():
	"""Load bundled tesla_service_categories.json (cached in-process)."""
	global _CATEGORIES_CACHE
	if _CATEGORIES_CACHE is not None:
		return _CATEGORIES_CACHE

	data_path = os.path.normpath(
		os.path.join(os.path.dirname(__file__), "..", "..", "data", "tesla_service_categories.json")
	)
	if os.path.exists(data_path):
		try:
			with open(data_path, "r", encoding="utf-8") as f:
				_CATEGORIES_CACHE = json.load(f)
				return _CATEGORIES_CACHE
		except (OSError, ValueError) as exc:
			_LOGGER.warning("Failed to load tesla_service_categories.json: %s", exc)
	return {"categories": {}, "subcategories": {}}


def _vget(vehicle_doc, field, default=None):
	"""Read attribute or dict key safely from a vehicle record."""
	if isinstance(vehicle_doc, dict):
		return vehicle_doc.get(field, default)
	return getattr(vehicle_doc, field, default)


def _match_generation_for_model(model_key, url_str):
	"""Return the matching GENERATION_CONFIG variant dict for model_key and URL."""
	model_data = GENERATION_CONFIG.get(model_key)
	if not model_data:
		return None

	variants = model_data.get("variants", [])
	for variant in variants:
		match_pattern = variant.get("url_match")
		if match_pattern and match_pattern in url_str:
			return variant

	for variant in variants:
		if variant.get("url_match") is None:
			return variant

	return None


def get_category_for_correction_code(correction_code):
	"""Return (category_name, subcategory_name) for a Tesla correction code."""
	if not correction_code:
		return ("Maintenance", "")

	code_str = str(correction_code).strip()
	cat_db = _get_categories_dict()
	cats = cat_db.get("categories", {})
	subcats = cat_db.get("subcategories", {})

	category = cats.get(code_str[:2], "Maintenance")
	subcategory = subcats.get(code_str[:4], "")
	return (category, subcategory)


def parse_vehicle_year_month(manufactured_month=None, model_year=None):
	"""Parse vehicle manufactured month and model year into a (year, month) tuple.

	Formats supported:
	- '05/22' or '5/22' -> (2022, 5)
	- '05/2022' or '2022-05' -> (2022, 5)
	- 'May 2022' -> (2022, 5)
	- model_year=2022 -> (2022, 6)
	"""
	if manufactured_month:
		raw = str(manufactured_month).strip()
		m1 = re.match(r"^(\d{1,2})[/.-](\d{2,4})$", raw)
		if m1:
			month = cint(m1.group(1))
			yr = cint(m1.group(2))
			if yr < 100:
				yr += 2000
			if 1 <= month <= 12 and yr >= 2000:
				return (yr, month)

		m2 = re.match(r"^(\d{4})[/.-](\d{1,2})$", raw)
		if m2:
			yr = cint(m2.group(1))
			month = cint(m2.group(2))
			if 1 <= month <= 12 and yr >= 2000:
				return (yr, month)

		m3 = re.match(r"^([a-zA-Z]+)\s+(\d{4})$", raw)
		if m3:
			month = MONTH_MAP.get(m3.group(1).lower(), 6)
			yr = cint(m3.group(2))
			return (yr, month)

	if model_year:
		yr = cint(model_year)
		if yr < 100:
			yr += 2000
		if yr >= 2000:
			return (yr, 6)

	return None


def is_in_generation_range(year, month, start_tuple, end_tuple):
	"""Return True if (year, month) falls within start_tuple and end_tuple."""
	if not year or not month:
		return True

	curr = (year, month)
	if start_tuple and curr < start_tuple:
		return False
	if end_tuple and curr > end_tuple:
		return False
	return True


def _format_year_month_tuple(ym_tuple):
	"""Format a (year, month) tuple as YYYY-MM or return None."""
	if not ym_tuple:
		return None
	return f"{ym_tuple[0]:04d}-{ym_tuple[1]:02d}"


def _normalize_model_key(model=None):
	"""Return canonical GENERATION_CONFIG key for a model label/key, or None."""
	raw = str(model or "").strip()
	if not raw or raw.lower() == "all models":
		return None
	compact = raw.replace(" ", "").lower()
	low = raw.lower()
	for key, cfg in GENERATION_CONFIG.items():
		if key.lower() == compact or cfg["label"].lower() == low:
			return key
	for key, cfg in GENERATION_CONFIG.items():
		if key.lower() in compact or cfg["label"].lower() in low:
			return key
	return None


def _generation_to_entry(model_label, variant, gen):
	"""Build a structured generation metadata dict for API/Desk/table serialization."""
	start = gen.get("start")
	end = gen.get("end")
	return {
		"applicable_model": model_label,
		"generation_name": gen["name"],
		"date_range": gen.get("date_range") or "",
		"short_label": gen.get("short_label") or gen["name"],
		"year_from": start[0] if start else None,
		"month_from": start[1] if start else None,
		"year_to": end[0] if end else None,
		"month_to": end[1] if end else None,
		"manual_base_url": variant.get("default_url") or "",
	}


def get_generation_catalog(model=None):
	"""Return structured Tesla generation entries for one model (list) or all models (dict)."""
	if model is not None:
		model_key = _normalize_model_key(model)
		if not model_key:
			return []
		cfg = GENERATION_CONFIG[model_key]
		entries = [
			_generation_to_entry(cfg["label"], variant, gen)
			for variant in cfg.get("variants", [])
			for gen in variant.get("generations", [])
		]
		entries.sort(key=lambda item: (item["year_from"] or 0, item["month_from"] or 0))
		return entries

	return {
		cfg["label"]: get_generation_catalog(cfg["label"])
		for cfg in GENERATION_CONFIG.values()
	}


def get_generation_names(model=None):
	"""Return canonical generation name strings for one model or across all models."""
	if model is not None:
		return [entry["generation_name"] for entry in get_generation_catalog(model)]
	names = []
	for entries in get_generation_catalog().values():
		names.extend(entry["generation_name"] for entry in entries)
	return names


def resolve_generation_entries(raw_generation=None, model=None, manual_url=None):
	"""Resolve generation names, lists, aliases, or manual URLs into canonical generation entries."""
	model_key = _normalize_model_key(model)
	if model and not model_key and str(model).strip().lower() != "all models":
		return []

	if model_key:
		search_pool = get_generation_catalog(GENERATION_CONFIG[model_key]["label"])
	else:
		search_pool = [
			entry
			for entries in get_generation_catalog().values()
			for entry in entries
		]

	matched = []
	seen = set()

	def _add(entry):
		key = (entry["applicable_model"], entry["generation_name"])
		if key not in seen:
			seen.add(key)
			matched.append(entry)

	raw_str = str(raw_generation or "").strip()
	if raw_str:
		for raw_tok in re.split(r"[,;]+", raw_str):
			token = raw_tok.strip()
			if not token:
				continue
			tok_low = token.lower()

			exact = [
				entry
				for entry in search_pool
				if entry["generation_name"].lower() == tok_low
				or f"{entry['applicable_model']} {entry['date_range']}".lower() == tok_low
				or entry["short_label"].lower() == tok_low
				or (model_key and entry["date_range"].lower() == tok_low)
			]
			if exact:
				for entry in exact:
					_add(entry)
				continue

			if "model y jan 2020 - present" in tok_low or (
				model_key == "ModelY" and "jan 2020 - present" in tok_low
			):
				for entry in search_pool:
					if entry["applicable_model"] == "Model Y":
						_add(entry)
				continue

			if "highland" in tok_low or (
				"2024+" in tok_low and (not model_key or model_key == "Model3")
			):
				for entry in search_pool:
					if entry["generation_name"] == "Model 3 Jan 2024":
						_add(entry)
				continue

			if "juniper" in tok_low or (
				"2025+" in tok_low and (not model_key or model_key == "ModelY")
			):
				for entry in search_pool:
					if entry["generation_name"] == "Model Y Feb 2025":
						_add(entry)
				continue

			if "palladium" in tok_low:
				for entry in search_pool:
					if "/Palladium/" in (entry.get("manual_base_url") or ""):
						_add(entry)
				continue

	if not matched and manual_url:
		url_meta = parse_tesla_manual_url(manual_url)
		for entry in url_meta.get("generation_entries") or []:
			if not model_key or entry["applicable_model"] == GENERATION_CONFIG[model_key]["label"]:
				_add(entry)

	matched.sort(key=lambda item: (item["applicable_model"], item["year_from"] or 0, item["month_from"] or 0))
	return matched


def parse_tesla_manual_url(url):
	"""Extract model, generation info, and doc_guid from a Tesla manual URL."""
	if not url:
		return {}

	url_str = str(url).strip()
	result = {
		"url": url_str,
		"model_key": None,
		"applicable_model": None,
		"generations": [],
		"generation_entries": [],
		"applicable_generation": None,
		"matched_generation": None,
		"gen_start": None,
		"gen_end": None,
		"doc_guid": None,
	}

	guid_match = re.search(
		r"(GUID-[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})",
		url_str,
		re.IGNORECASE,
	)
	if not guid_match:
		guid_match = re.search(
			r"/(GUID-[0-9A-Z]+(?:-[0-9A-Z]+)*)(?:\.html?)?(?:[?#]|$)",
			url_str,
			re.IGNORECASE,
		)
	if guid_match:
		result["doc_guid"] = guid_match.group(1).upper()

	year_seg = re.search(r"/(20\d{2})/", url_str)
	if year_seg:
		result["year_hint"] = f"{year_seg.group(1)}+"

	for model_key, model_data in GENERATION_CONFIG.items():
		if f"/{model_key}/" not in url_str:
			continue
		result["model_key"] = model_key
		result["applicable_model"] = model_data["label"]
		variant = _match_generation_for_model(model_key, url_str)
		if variant and variant.get("generations"):
			gens = sorted(
				variant["generations"],
				key=lambda item: item.get("start") or (0, 0),
			)
			result["generations"] = [g["name"] for g in gens]
			result["generation_entries"] = [
				_generation_to_entry(model_data["label"], variant, g)
				for g in gens
			]
			first_gen = gens[0]
			result["matched_generation"] = first_gen["name"]
			result["applicable_generation"] = ", ".join(result["generations"])
			starts = [g["start"] for g in gens if g.get("start")]
			result["gen_start"] = _format_year_month_tuple(min(starts)) if starts else None
			if any(g.get("end") is None for g in gens):
				result["gen_end"] = None
			else:
				ends = [g["end"] for g in gens if g.get("end")]
				result["gen_end"] = _format_year_month_tuple(max(ends)) if ends else None
		break

	return result


def resolve_vehicle_generation(vehicle_doc):
	"""Resolve vehicle attributes into canonical generation, candidates, ambiguity, and manual URL."""
	empty = {
		"model_key": None,
		"applicable_model": "",
		"vehicle_generation": "",
		"generation_date_range": "",
		"recommended_manual_url": "https://service.tesla.com/docs/",
		"candidate_generations": [],
		"generation_ambiguous": False,
	}
	if not vehicle_doc:
		return empty

	model_raw = _vget(vehicle_doc, "model", "") or ""
	matched_key = _normalize_model_key(model_raw)
	if not matched_key:
		return empty

	model_cfg = GENERATION_CONFIG[matched_key]
	label = model_cfg["label"]
	all_entries = get_generation_catalog(label)
	default_url = model_cfg["variants"][0]["default_url"]

	manuf_month = _vget(vehicle_doc, "manufactured_month", None)
	model_year = _vget(vehicle_doc, "model_year", None)
	drive_unit = str(_vget(vehicle_doc, "drive_unit", "") or "").strip()
	explicit_gen = str(
		_vget(vehicle_doc, "vehicle_generation", None)
		or _vget(vehicle_doc, "generation", None)
		or ""
	).strip()

	def _entry_in_range(entry, yr, mon):
		start = (entry["year_from"], entry["month_from"] or 1) if entry["year_from"] else None
		end = (entry["year_to"], entry["month_to"] or 12) if entry["year_to"] else None
		return is_in_generation_range(yr, mon, start, end)

	exact_ym = parse_vehicle_year_month(manuf_month, None) if manuf_month else None
	chosen = None
	candidates = []
	is_ambiguous = False

	if exact_ym:
		yr, mon = exact_ym
		matched = [entry for entry in all_entries if _entry_in_range(entry, yr, mon)]
		if matched:
			candidates = matched
			chosen = matched[0]
			is_ambiguous = len(matched) > 1
	elif explicit_gen:
		resolved = resolve_generation_entries(explicit_gen, model=label)
		if resolved:
			candidates = resolved
			chosen = resolved[0]
			is_ambiguous = len(resolved) > 1

	if not chosen and not exact_ym:
		du_low = drive_unit.lower()
		if matched_key == "ModelY" and "juniper" in du_low:
			candidates = [e for e in all_entries if e["generation_name"] == "Model Y Feb 2025"]
		elif matched_key == "Model3" and "highland" in du_low:
			candidates = [e for e in all_entries if e["generation_name"] == "Model 3 Jan 2024"]

		if candidates:
			chosen = candidates[0]
			is_ambiguous = len(candidates) > 1

	if not chosen and model_year:
		yr = cint(model_year)
		if 0 < yr < 100:
			yr += 2000
		if yr >= 2000:
			candidates = [
				entry
				for entry in all_entries
				if (not entry["year_from"] or entry["year_from"] <= yr)
				and (not entry["year_to"] or entry["year_to"] >= yr)
			]
			if "palladium" in drive_unit.lower() and matched_key in ("ModelS", "ModelX"):
				palladium_only = [e for e in candidates if "/Palladium/" in (e.get("manual_base_url") or "")]
				if palladium_only:
					candidates = palladium_only
			if candidates:
				mid_year_matches = [e for e in candidates if _entry_in_range(e, yr, 6)]
				chosen = mid_year_matches[0] if mid_year_matches else candidates[0]
				is_ambiguous = len(candidates) > 1

	if not chosen and not candidates:
		candidates = all_entries
		is_ambiguous = len(all_entries) > 1 and bool(model_raw)

	return {
		"model_key": matched_key,
		"applicable_model": label,
		"vehicle_generation": chosen["generation_name"] if chosen else "",
		"generation_date_range": chosen["date_range"] if chosen else "",
		"recommended_manual_url": (chosen["manual_base_url"] if chosen else "") or default_url,
		"candidate_generations": candidates,
		"generation_ambiguous": is_ambiguous,
	}


def get_tesla_manual_details_for_vehicle(vehicle_doc):
	"""Return canonical vehicle generation metadata and recommended service manual URL."""
	return resolve_vehicle_generation(vehicle_doc)


def get_tesla_manual_url_for_vehicle(vehicle_doc):
	"""Return the recommended Tesla service manual base URL for a vehicle."""
	return resolve_vehicle_generation(vehicle_doc).get("recommended_manual_url") or "https://service.tesla.com/docs/"


def validate_vehicle_compatibility(vehicle_doc, procedure_url=None, package_doc=None):
	"""Verify if a vehicle's model, date, or generation matches the procedure URL and/or package."""
	effective_url = procedure_url or (_vget(package_doc, "tesla_manual_url", None) if package_doc else None)
	if not vehicle_doc or (not effective_url and not package_doc):
		return {"is_compatible": True, "warning": None}

	v_model = _vget(vehicle_doc, "model", "") or ""
	v_manuf_month = _vget(vehicle_doc, "manufactured_month", None)
	v_year = _vget(vehicle_doc, "model_year", None)
	v_gen = str(_vget(vehicle_doc, "vehicle_generation", "") or "").strip()
	v_date = parse_vehicle_year_month(v_manuf_month, v_year)

	if package_doc:
		pkg_model = str(_vget(package_doc, "applicable_model", "") or "").strip()
		if pkg_model and pkg_model != "All Models" and v_model and pkg_model.lower() not in v_model.lower():
			return {
				"is_compatible": False,
				"warning": (
					f"Model mismatch: The repair vehicle is a '{v_model}', "
					f"but this package is for '{pkg_model}'."
				),
				"vehicle_desc": f"{v_model} ({v_year or ''})",
				"procedure_gen": _vget(package_doc, "applicable_generation", None) or pkg_model,
			}

		compat_rows = _vget(package_doc, "generation_compatibility", None) or []
		pkg_gen = str(_vget(package_doc, "applicable_generation", "") or "").strip()
		entries = []
		for row in compat_rows:
			entries.append({
				"applicable_model": _vget(row, "applicable_model", "") or pkg_model,
				"generation_name": _vget(row, "generation_name", "") or "",
				"year_from": _vget(row, "year_from", None),
				"month_from": _vget(row, "month_from", None),
				"year_to": _vget(row, "year_to", None),
				"month_to": _vget(row, "month_to", None),
			})
		if not entries and (pkg_gen or effective_url):
			entries = resolve_generation_entries(pkg_gen, model=pkg_model, manual_url=effective_url)

		if entries and v_model:
			v_model_key = _normalize_model_key(v_model)
			v_label = GENERATION_CONFIG[v_model_key]["label"] if v_model_key else v_model
			model_entries = [
				e for e in entries
				if not e.get("applicable_model")
				or e.get("applicable_model") == "All Models"
				or e.get("applicable_model").lower() == v_label.lower()
			]
			if not model_entries:
				gen_names = ", ".join(e["generation_name"] for e in entries if e.get("generation_name")) or pkg_gen or pkg_model
				return {
					"is_compatible": False,
					"warning": (
						f"Generation mismatch: Package compatibility ({gen_names}) "
						f"does not include '{v_model}'."
					),
					"vehicle_desc": f"{v_model} ({v_year or ''})",
					"procedure_gen": gen_names,
				}
			if v_gen and any(e.get("generation_name") == v_gen for e in model_entries):
				return {"is_compatible": True, "warning": None}
			if v_date and not v_gen:
				yr, mon = v_date
				matched_in_entries = any(
					is_in_generation_range(
						yr,
						mon,
						(e["year_from"], e["month_from"] or 1) if e.get("year_from") else None,
						(e["year_to"], e["month_to"] or 12) if e.get("year_to") else None,
					)
					for e in model_entries
				)
				if not matched_in_entries:
					gen_names = ", ".join(e["generation_name"] for e in model_entries if e.get("generation_name"))
					v_date_str = f"{mon:02d}/{yr}" if mon else str(yr)
					return {
						"is_compatible": False,
						"warning": (
							f"Generation mismatch: Vehicle manufactured date ({v_date_str}) "
							f"may not match package generation ({gen_names})."
						),
						"vehicle_desc": f"{v_model} {v_date_str}",
						"procedure_gen": gen_names,
					}
			elif v_gen and all(e.get("generation_name") for e in model_entries):
				gen_names = ", ".join(e["generation_name"] for e in model_entries)
				return {
					"is_compatible": False,
					"warning": (
						f"Generation mismatch: Vehicle generation ({v_gen}) "
						f"does not match package generation ({gen_names})."
					),
					"vehicle_desc": f"{v_model} {v_gen}",
					"procedure_gen": gen_names,
				}

		yr_from = cint(_vget(package_doc, "applicable_year_from", 0))
		yr_to = cint(_vget(package_doc, "applicable_year_to", 0))
		yr_int = cint(v_year)
		if yr_int and ((yr_from and yr_int < yr_from) or (yr_to and yr_int > yr_to)):
			range_label = f"{yr_from or '...'} - {yr_to or 'Present'}"
			return {
				"is_compatible": False,
				"warning": f"Model year {yr_int} is outside package year range ({range_label}).",
				"vehicle_desc": f"{v_model} ({yr_int})",
				"procedure_gen": pkg_gen or range_label,
			}

	if not effective_url:
		return {"is_compatible": True, "warning": None}

	parsed_url = parse_tesla_manual_url(effective_url)
	model_key = parsed_url.get("model_key")
	if not model_key:
		return {"is_compatible": True, "warning": None}

	expected_label = GENERATION_CONFIG.get(model_key, {}).get("label", "")
	if expected_label and v_model and expected_label.lower() not in v_model.lower():
		return {
			"is_compatible": False,
			"warning": (
				f"Model mismatch: The repair vehicle is a '{v_model}', "
				f"but this procedure is for '{expected_label}'."
			),
			"vehicle_desc": f"{v_model} ({v_year or ''})",
			"procedure_gen": parsed_url.get("applicable_generation") or parsed_url.get("matched_generation") or expected_label,
		}

	if model_key in GENERATION_CONFIG:
		matched_variant = _match_generation_for_model(model_key, str(effective_url))
		if matched_variant:
			if v_gen and any(g["name"] == v_gen for g in matched_variant["generations"]):
				return {"is_compatible": True, "warning": None}
			if v_date:
				yr, mon = v_date
				matched_in_variant = any(
					is_in_generation_range(yr, mon, gen.get("start"), gen.get("end"))
					for gen in matched_variant["generations"]
				)
				if not matched_in_variant:
					gen_names = ", ".join(g["name"] for g in matched_variant["generations"])
					v_date_str = f"{mon:02d}/{yr}" if mon else str(yr)
					return {
						"is_compatible": False,
						"warning": (
							f"Generation mismatch: Vehicle manufactured date ({v_date_str}) "
							f"may not match procedure generation ({gen_names})."
						),
						"vehicle_desc": f"{v_model} {v_date_str}",
						"procedure_gen": gen_names,
					}

	return {"is_compatible": True, "warning": None}
