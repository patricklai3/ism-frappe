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
					{"name": "Model S Feb 2021 - May 2025", "start": (2021, 2), "end": (2025, 5)},
					{"name": "Model S June 2025", "start": (2025, 6), "end": None},
				],
				"default_url": "https://service.tesla.com/docs/ModelS/ServiceManual/Palladium/en-us/",
			},
			{
				"url_match": None,  # classic / legacy
				"generations": [
					{"name": "Model S Feb 2012 - Mar 2016", "start": (2012, 2), "end": (2016, 3)},
					{"name": "Model S Apr 2016 - Jan 2021", "start": (2016, 4), "end": (2021, 1)},
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
					{"name": "Model 3 Jan 2024", "start": (2024, 1), "end": None},
				],
				"default_url": "https://service.tesla.com/docs/Model3/ServiceManual/2024/en-us/",
			},
			{
				"url_match": None,
				"generations": [
					{"name": "Model 3 Jun 2017 - Dec 2023", "start": (2017, 6), "end": (2023, 12)},
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
					{"name": "Model X Mar 2021 - May 2025", "start": (2021, 3), "end": (2025, 5)},
					{"name": "Model X June 2025", "start": (2025, 6), "end": None},
				],
				"default_url": "https://service.tesla.com/docs/ModelX/Palladium/ServiceManual/en-us/",
			},
			{
				"url_match": None,
				"generations": [
					{"name": "Model X Sep 2015 - Feb 2021", "start": (2015, 9), "end": (2021, 2)},
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
					{"name": "Model Y Feb 2025", "start": (2025, 2), "end": None},
				],
				"default_url": "https://service.tesla.com/docs/ModelY/ServiceManual/2025/en-us/",
			},
			{
				"url_match": None,
				"generations": [
					{"name": "Model Y Jan 2020 - Jan 2025", "start": (2020, 1), "end": (2025, 1)},
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
					{"name": "Cybertruck Nov 2023 - Present", "start": (2023, 11), "end": None},
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
			result["generations"] = [g["name"] for g in variant["generations"]]
			first_gen = variant["generations"][0]
			result["matched_generation"] = first_gen["name"]
			result["gen_start"] = _format_year_month_tuple(first_gen.get("start"))
			result["gen_end"] = _format_year_month_tuple(first_gen.get("end"))
		break

	return result


def get_tesla_manual_url_for_vehicle(vehicle_doc):
	"""Return the recommended Tesla service manual base URL for a vehicle."""
	if not vehicle_doc:
		return "https://service.tesla.com/docs/"

	model_raw = _vget(vehicle_doc, "model", "") or ""
	model_clean = model_raw.replace(" ", "").strip()
	manuf_month = _vget(vehicle_doc, "manufactured_month", None)
	model_yr = _vget(vehicle_doc, "model_year", None)
	parsed_date = parse_vehicle_year_month(manuf_month, model_yr)

	matched_key = None
	for key, cfg in GENERATION_CONFIG.items():
		if key.lower() in model_clean.lower() or cfg["label"].lower() in model_raw.lower():
			matched_key = key
			break

	if not matched_key:
		return "https://service.tesla.com/docs/"

	model_cfg = GENERATION_CONFIG[matched_key]
	if parsed_date:
		yr, mon = parsed_date
		for variant in model_cfg["variants"]:
			for gen in variant["generations"]:
				if is_in_generation_range(yr, mon, gen.get("start"), gen.get("end")):
					return variant["default_url"]

	return model_cfg["variants"][0]["default_url"]


def validate_vehicle_compatibility(vehicle_doc, procedure_url):
	"""Verify if a vehicle's manufactured date/model year matches the procedure's generation."""
	if not vehicle_doc or not procedure_url:
		return {"is_compatible": True, "warning": None}

	parsed_url = parse_tesla_manual_url(procedure_url)
	model_key = parsed_url.get("model_key")
	if not model_key:
		return {"is_compatible": True, "warning": None}

	v_model = _vget(vehicle_doc, "model", "") or ""
	v_manuf_month = _vget(vehicle_doc, "manufactured_month", None)
	v_year = _vget(vehicle_doc, "model_year", None)
	v_date = parse_vehicle_year_month(v_manuf_month, v_year)

	expected_label = GENERATION_CONFIG.get(model_key, {}).get("label", "")
	if expected_label and expected_label.lower() not in v_model.lower():
		return {
			"is_compatible": False,
			"warning": (
				f"Model mismatch: The repair vehicle is a '{v_model}', "
				f"but this procedure is for '{expected_label}'."
			),
			"vehicle_desc": f"{v_model} ({v_year or ''})",
			"procedure_gen": parsed_url.get("matched_generation") or expected_label,
		}

	if v_date and model_key in GENERATION_CONFIG:
		yr, mon = v_date
		matched_variant = _match_generation_for_model(model_key, str(procedure_url))
		if matched_variant:
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
