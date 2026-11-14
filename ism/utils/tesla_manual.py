# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import os
import re
from urllib.parse import urlparse

try:
	import frappe
	from frappe.utils import cint, flt
except ImportError:  # Allow standalone Python ingestion/CLI usage without Frappe
	frappe = None

	def cint(value, default=0):
		try:
			return int(float(value))
		except (TypeError, ValueError):
			return default

	def flt(value, precision=None):
		try:
			num = float(value)
		except (TypeError, ValueError):
			num = 0.0
		return round(num, precision) if precision is not None else num

OEM_PART_PATTERN = re.compile(r"\b(\d{7}-\d{2}-[A-Z])\b")
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

	data_path = os.path.join(os.path.dirname(__file__), "..", "data", "tesla_service_categories.json")
	if os.path.exists(data_path):
		try:
			with open(data_path, "r", encoding="utf-8") as f:
				_CATEGORIES_CACHE = json.load(f)
				return _CATEGORIES_CACHE
		except Exception:
			pass
	return {"categories": {}, "subcategories": {}}


def _vget(vehicle_doc, field, default=None):
	"""Read attribute or dict key safely from a vehicle record."""
	if isinstance(vehicle_doc, dict):
		return vehicle_doc.get(field, default)
	return getattr(vehicle_doc, field, default)


def get_category_for_correction_code(correction_code):
	"""Return (category_name, subcategory_name) for a Tesla correction code."""
	if not correction_code:
		return ("Maintenance", "")

	code_str = str(correction_code).strip()
	cat_db = _get_categories_dict()
	cats = cat_db.get("categories", {})
	subcats = cat_db.get("subcategories", {})

	cat_id = code_str[:2]
	subcat_id = code_str[:4]

	category = cats.get(cat_id, "Maintenance")
	subcategory = subcats.get(subcat_id, "")

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
		# Format: MM/YY or MM/YYYY
		m1 = re.match(r"^(\d{1,2})[/.-](\d{2,4})$", raw)
		if m1:
			month = cint(m1.group(1))
			yr = cint(m1.group(2))
			if yr < 100:
				yr += 2000
			if 1 <= month <= 12 and yr >= 2000:
				return (yr, month)

		# Format: YYYY-MM
		m2 = re.match(r"^(\d{4})[/.-](\d{1,2})$", raw)
		if m2:
			yr = cint(m2.group(1))
			month = cint(m2.group(2))
			if 1 <= month <= 12 and yr >= 2000:
				return (yr, month)

		# Format: Mon YYYY e.g. "May 2022"
		m3 = re.match(r"^([a-zA-Z]+)\s+(\d{4})$", raw)
		if m3:
			mon_name = m3.group(1).lower()
			month = MONTH_MAP.get(mon_name, 6)
			yr = cint(m3.group(2))
			return (yr, month)

	if model_year:
		yr = cint(model_year)
		if yr < 100:
			yr += 2000
		if yr >= 2000:
			# Midpoint of model year
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


def parse_tesla_manual_url(url):
	"""Extract model, generation info, doc_guid from Tesla manual URL."""
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

	# Extract doc_guid (full UUID first, then short GUID-* topic filenames)
	guid_match = re.search(
		r"(GUID-[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})",
		url_str,
		re.IGNORECASE,
	)
	if not guid_match:
		guid_match = re.search(r"/(GUID-[0-9A-Z]+(?:-[0-9A-Z]+)*)(?:\.html?)?(?:[?#]|$)", url_str, re.IGNORECASE)
	if guid_match:
		result["doc_guid"] = guid_match.group(1).upper()

	year_seg = re.search(r"/(20\d{2})/", url_str)
	if year_seg:
		result["year_hint"] = f"{year_seg.group(1)}+"

	# Match model from URL
	for model_key, model_data in GENERATION_CONFIG.items():
		if f"/{model_key}/" in url_str:
			result["model_key"] = model_key
			result["applicable_model"] = model_data["label"]

			for variant in model_data["variants"]:
				match_pattern = variant.get("url_match")
				if match_pattern and match_pattern in url_str:
					result["generations"] = [g["name"] for g in variant["generations"]]
					# Use first generation as primary
					first_gen = variant["generations"][0]
					result["matched_generation"] = first_gen["name"]
					result["gen_start"] = f"{first_gen['start'][0]:04d}-{first_gen['start'][1]:02d}" if first_gen.get("start") else None
					result["gen_end"] = f"{first_gen['end'][0]:04d}-{first_gen['end'][1]:02d}" if first_gen.get("end") else None
					break
			else:
				# Fallback to default variant (url_match is None)
				for variant in model_data["variants"]:
					if variant.get("url_match") is None:
						result["generations"] = [g["name"] for g in variant["generations"]]
						first_gen = variant["generations"][0]
						result["matched_generation"] = first_gen["name"]
						result["gen_start"] = f"{first_gen['start'][0]:04d}-{first_gen['start'][1]:02d}" if first_gen.get("start") else None
						result["gen_end"] = f"{first_gen['end'][0]:04d}-{first_gen['end'][1]:02d}" if first_gen.get("end") else None
						break
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

	# Find matched model config
	matched_key = None
	for key in GENERATION_CONFIG:
		if key.lower() in model_clean.lower() or GENERATION_CONFIG[key]["label"].lower() in model_raw.lower():
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

	# Return first variant default URL
	return model_cfg["variants"][0]["default_url"]


def validate_vehicle_compatibility(vehicle_doc, procedure_url):
	"""Verify if a vehicle's manufactured date/model year matches the procedure's generation.

	Returns:
	{"is_compatible": True/False, "warning": str or None, "vehicle_desc": str, "procedure_gen": str}
	"""
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

	# Check model name alignment
	expected_label = GENERATION_CONFIG.get(model_key, {}).get("label", "")
	if expected_label and expected_label.lower() not in v_model.lower():
		return {
			"is_compatible": False,
			"warning": f"Model mismatch: The repair vehicle is a '{v_model}', but this procedure is for '{expected_label}'.",
			"vehicle_desc": f"{v_model} ({v_year or ''})",
			"procedure_gen": parsed_url.get("matched_generation") or expected_label,
		}

	# Check generation date range
	if v_date and model_key in GENERATION_CONFIG:
		yr, mon = v_date
		# Find the variant matching this URL
		model_cfg = GENERATION_CONFIG[model_key]
		matched_variant = None
		for variant in model_cfg["variants"]:
			match_p = variant.get("url_match")
			if match_p and match_p in procedure_url:
				matched_variant = variant
				break
		if not matched_variant:
			for variant in model_cfg["variants"]:
				if variant.get("url_match") is None:
					matched_variant = variant
					break

		if matched_variant:
			matched_in_variant = False
			for gen in matched_variant["generations"]:
				if is_in_generation_range(yr, mon, gen.get("start"), gen.get("end")):
					matched_in_variant = True
					break

			if not matched_in_variant:
				gen_names = ", ".join([g["name"] for g in matched_variant["generations"]])
				v_date_str = f"{mon:02d}/{yr}" if mon else str(yr)
				return {
					"is_compatible": False,
					"warning": f"Generation mismatch: Vehicle manufactured date ({v_date_str}) may not match procedure generation ({gen_names}).",
					"vehicle_desc": f"{v_model} {v_date_str}",
					"procedure_gen": gen_names,
				}

	return {"is_compatible": True, "warning": None}


_MANIFEST_CACHE = {}


def get_mobile_capability_from_manifest(url, doc_guid=None, correction_code=None):
	"""Fetch or retrieve cached dependencies.json to determine mobile capability."""
	if not url or "/docs/" not in url:
		return None

	try:
		from urllib.parse import urlparse
		parsed_url = urlparse(url)
		if parsed_url.scheme != "https" or parsed_url.netloc not in ("service.tesla.com", "service.teslamotors.com"):
			return None

		import urllib.request
		clean_url = url.split("?")[0].split("#")[0]
		base_manifest_url = clean_url.rsplit("/", 1)[0] + "/dependencies.json"
		filename = clean_url.split("/")[-1]

		topics = None
		cache_key = f"tesla_manifest:{base_manifest_url}"
		if frappe is not None:
			try:
				cached_json = frappe.cache().get_value(cache_key)
				if cached_json:
					topics = json.loads(cached_json) if isinstance(cached_json, str) else cached_json
			except Exception:
				pass

		if topics is None:
			topics = _MANIFEST_CACHE.get(base_manifest_url)

		if topics is None:
			headers = {
				"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
				"Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
				"Accept-Language": "en-US,en;q=0.9",
				"Referer": "https://service.tesla.com/",
			}
			req = urllib.request.Request(base_manifest_url, headers=headers)
			try:
				res = urllib.request.urlopen(req, timeout=15)
				manifest_data = json.loads(res.read().decode("utf-8"))
				topics = manifest_data.get("topics", {}).get("topic", [])
				_MANIFEST_CACHE[base_manifest_url] = topics
				if frappe is not None:
					try:
						frappe.cache().set_value(cache_key, json.dumps(topics), expires_in_sec=86400)
					except Exception:
						pass
			except Exception:
				# Fallback retry with basic User-Agent
				try:
					req2 = urllib.request.Request(base_manifest_url, headers={"User-Agent": "Mozilla/5.0"})
					res2 = urllib.request.urlopen(req2, timeout=15)
					manifest_data = json.loads(res2.read().decode("utf-8"))
					topics = manifest_data.get("topics", {}).get("topic", [])
					_MANIFEST_CACHE[base_manifest_url] = topics
					if frappe is not None:
						try:
							frappe.cache().set_value(cache_key, json.dumps(topics), expires_in_sec=86400)
						except Exception:
							pass
				except Exception:
					topics = []

		target = None
		target_guid = str(doc_guid).strip().upper() if doc_guid else ""
		if not target_guid and filename and filename.startswith("GUID-"):
			target_guid = filename.split(".")[0].upper()
		target_cc = str(correction_code).strip() if correction_code else ""

		for t in (topics or []):
			t_url = str(t.get("url") or "").strip()
			t_guid = str(t.get("guid") or "").strip().upper()
			t_cc = str(t.get("correction-code") or "").strip()
			t_rh_cc = str(t.get("rh-correction-code") or "").strip()

			if filename and t_url == filename:
				target = t
				break
			if target_guid and t_guid == target_guid:
				target = t
				break
			if target_cc and (t_cc == target_cc or t_rh_cc == target_cc):
				target = t
				break

		if target:
			cap = target.get("mobile-capable")
			if isinstance(cap, list):
				is_mobile = 1 if any(str(c).strip().lower() not in ("no", "none", "unknown", "0", "false") for c in cap) else 0
				mobile_level = ", ".join(str(c) for c in cap)
			elif cap and str(cap).strip().lower() not in ("no", "none", "unknown", "0", "false"):
				is_mobile = 1
				mobile_level = str(cap)
			else:
				is_mobile = 0
				mobile_level = "No"

			rh_cc = str(target.get("rh-correction-code") or "").strip() or None
			return {
				"is_mobile_capable": is_mobile,
				"mobile_service_level": mobile_level,
				"rh_correction_code": rh_cc,
				"is_symmetrical": 1 if rh_cc else 0,
			}
	except Exception:
		pass

	return None


def swap_side_in_title(title, to_side="RH"):
	"""Swap symmetrical side indicator in title (e.g. LH -> RH or RH -> LH)."""
	if not title:
		return title

	to_side_upper = str(to_side).strip().upper()
	if to_side_upper == "RH":
		new_title = re.sub(r'\bLH\b', 'RH', title)
		new_title = re.sub(r'\bLeft\s+Hand\b', 'Right Hand', new_title, flags=re.IGNORECASE)
		new_title = re.sub(r'\bLeft\b(?!\s+(?:Turn|Foot))', 'Right', new_title, flags=re.IGNORECASE)
		return new_title
	elif to_side_upper == "LH":
		new_title = re.sub(r'\bRH\b', 'LH', title)
		new_title = re.sub(r'\bRight\s+Hand\b', 'Left Hand', new_title, flags=re.IGNORECASE)
		new_title = re.sub(r'\bRight\b', 'Left', new_title, flags=re.IGNORECASE)
		return new_title
	return title


def detect_service_side(title, correction_code=None, rh_correction_code=None):
	"""Detect whether a title or correction code represents LH, RH, or None."""
	if title:
		if re.search(r'\bRH\b|Right Hand|\bRight\b(?!\s+(?:Turn|Foot))', title, re.IGNORECASE):
			return "RH"
		if re.search(r'\bLH\b|Left Hand|\bLeft\b(?!\s+(?:Turn|Foot))', title, re.IGNORECASE):
			return "LH"
	if rh_correction_code and correction_code and str(correction_code).strip() == str(rh_correction_code).strip():
		return "RH"
	return "None"


def strip_correction_code_prefix(title, correction_code=None):
	"""Remove leading correction code prefixes (e.g. '00030700 - Title') cleanly."""
	clean = str(title or "").strip()
	code = str(correction_code or "").strip()
	if code and clean:
		clean = re.sub(rf"^{re.escape(code)}\s*[-–—:]?\s*", "", clean).strip()
	return clean


def _normalize_part_side(raw_side, part_name="", default_side="Both"):
	"""Normalize a part side into Service Package Item's 'Both' | 'LH' | 'RH' contract."""
	text = str(raw_side or "").strip().upper()
	if text in ("LH", "LEFT", "LEFT HAND"):
		return "LH"
	if text in ("RH", "RIGHT", "RIGHT HAND"):
		return "RH"
	if text == "BOTH":
		return "Both"
	if part_name:
		detected = detect_service_side(part_name)
		if detected in ("LH", "RH"):
			return detected
	fallback = str(default_side or "Both").strip().upper()
	if fallback in ("LH", "RH"):
		return fallback
	return "Both"


def extract_oem_parts_from_text(text, default_side="Both"):
	"""Extract deduplicated Tesla OEM part entries (#######-##-X) from HTML, Markdown, or text."""
	if not text:
		return []

	segments = []
	if "<tr" in text.lower() or "<li" in text.lower():
		segments.extend(re.findall(r"<(?:tr|li)[^>]*>(.*?)</(?:tr|li)>", text, re.IGNORECASE | re.DOTALL))
	else:
		segments.extend(str(text).splitlines())

	seen = {}

	def _record_part(pn, snippet=""):
		if pn in seen:
			return
		clean_line = re.sub(r"<[^>]+>", " ", snippet or "")
		clean_line = re.sub(r"\s+", " ", clean_line).strip()
		qty = 1.0
		qty_match = re.search(r"\bqty\s*[:=#]?\s*(\d+(?:\.\d+)?)\b", clean_line, re.IGNORECASE)
		if not qty_match:
			qty_match = re.search(r"(?:\bx\s*|\bquantity\s*[:=]?\s*)(\d+(?:\.\d+)?)\b", clean_line, re.IGNORECASE)
		if qty_match:
			parsed_qty = flt(qty_match.group(1))
			if parsed_qty > 0:
				qty = parsed_qty

		name_candidate = clean_line.replace(pn, " ")
		if qty_match:
			name_candidate = name_candidate.replace(qty_match.group(0), " ")
		name_candidate = re.sub(r"\b(?:part\s*(?:number|no|#)?|parts?)\b[:\-]*", " ", name_candidate, flags=re.IGNORECASE)
		name_candidate = re.sub(r"[|,;:()\[\]]+", " ", name_candidate)
		name_candidate = re.sub(r"\s+", " ", name_candidate).strip(" -–—:.")
		if len(re.findall(r"[A-Za-z]", name_candidate)) < 3 or len(name_candidate) > 120:
			name_candidate = f"Tesla OEM Part {pn}"

		seen[pn] = {
			"part_number": pn,
			"oem_part_number": pn,
			"part_name": name_candidate,
			"qty": qty,
			"rate": 0.0,
			"side": _normalize_part_side(None, part_name=name_candidate, default_side=default_side),
			"part_source": "OEM",
			"part_condition": "New",
		}

	for seg in segments:
		for pn in OEM_PART_PATTERN.findall(seg):
			_record_part(pn, seg)

	# Sweep whole plain text in case parts appear inline outside <tr>/<li> or single lines
	plain = re.sub(r"<[^>]+>", " ", str(text))
	for pn in OEM_PART_PATTERN.findall(plain):
		_record_part(pn, "")

	return list(seen.values())


def normalize_procedure_parts(raw_parts, default_side="Both"):
	"""Normalize heterogeneous part payloads (dicts, OEM codes, JSON string) into canonical dicts."""
	if not raw_parts:
		return []

	if isinstance(raw_parts, str):
		stripped = raw_parts.strip()
		try:
			decoded = json.loads(stripped)
			raw_parts = decoded if isinstance(decoded, list) else [decoded]
		except Exception:
			return extract_oem_parts_from_text(stripped, default_side=default_side)

	if not isinstance(raw_parts, list):
		return []

	normalized = []
	seen = set()
	for entry in raw_parts:
		if isinstance(entry, str):
			for extracted in extract_oem_parts_from_text(entry, default_side=default_side):
				if extracted["part_number"] not in seen:
					seen.add(extracted["part_number"])
					normalized.append(extracted)
			continue

		if not isinstance(entry, dict):
			continue

		pn = str(
			entry.get("part_number")
			or entry.get("item_code")
			or entry.get("oem_part_number")
			or ""
		).strip()
		oem_pn = str(entry.get("oem_part_number") or (pn if OEM_PART_PATTERN.fullmatch(pn) else "") or "").strip()
		if not pn and not oem_pn:
			continue

		key = pn or oem_pn
		if key in seen:
			continue
		seen.add(key)

		part_name = str(
			entry.get("part_name")
			or entry.get("item_name")
			or entry.get("description")
			or (f"Tesla OEM Part {key}" if OEM_PART_PATTERN.fullmatch(key) else key)
		).strip()
		qty = flt(entry.get("qty") or 1.0)
		if qty <= 0:
			qty = 1.0

		normalized.append({
			"part_number": pn or oem_pn,
			"oem_part_number": oem_pn or pn,
			"part_name": part_name,
			"qty": qty,
			"rate": flt(entry.get("rate") or entry.get("unit_rate") or 0.0, 2),
			"side": _normalize_part_side(entry.get("side"), part_name=part_name, default_side=default_side),
			"part_source": str(entry.get("part_source") or "OEM").strip() or "OEM",
			"part_condition": str(entry.get("part_condition") or "New").strip() or "New",
		})

	return normalized


def normalize_tesla_procedure(raw_data=None, url="", html_content="", fetch_manifest=True):
	"""Return the single authoritative normalized procedure contract across UI/API/Agent OS."""
	if isinstance(raw_data, str):
		try:
			raw_data = json.loads(raw_data)
		except Exception:
			raw_data = {}
	payload = dict(raw_data) if isinstance(raw_data, dict) else {}

	proc_url = str(payload.get("tesla_manual_url") or payload.get("url") or url or "").strip()
	html_parsed = parse_tesla_procedure_html(html_content, url=proc_url) if html_content else {}

	correction_code = str(
		payload.get("correction_code") or html_parsed.get("correction_code") or ""
	).strip()
	raw_title = (
		payload.get("title")
		or payload.get("procedure_title")
		or payload.get("procedure_name")
		or html_parsed.get("title")
		or ""
	)
	title = strip_correction_code_prefix(raw_title, correction_code) or "Tesla Service Procedure"

	frt_raw = payload.get("frt")
	if frt_raw in (None, ""):
		frt_raw = payload.get("labor_hours")
	if frt_raw in (None, ""):
		frt_raw = payload.get("standard_labor_hours")
	if frt_raw in (None, ""):
		frt_raw = html_parsed.get("frt")
	frt = flt(frt_raw, 2)

	url_meta = parse_tesla_manual_url(proc_url) if proc_url else {}
	model = str(
		payload.get("applicable_model")
		or payload.get("model")
		or html_parsed.get("applicable_model")
		or url_meta.get("applicable_model")
		or ""
	).strip()
	generation = str(
		payload.get("applicable_generation")
		or payload.get("matched_generation")
		or html_parsed.get("applicable_generation")
		or url_meta.get("matched_generation")
		or ""
	).strip()
	doc_guid = str(
		payload.get("doc_guid") or html_parsed.get("doc_guid") or url_meta.get("doc_guid") or ""
	).strip()

	cat_default, subcat_default = get_category_for_correction_code(correction_code)
	category = str(payload.get("category") or html_parsed.get("category") or cat_default).strip()
	subcategory = str(payload.get("subcategory") or html_parsed.get("subcategory") or subcat_default).strip()

	rh_code = str(payload.get("rh_correction_code") or html_parsed.get("rh_correction_code") or "").strip()
	is_mobile = cint(payload.get("is_mobile_capable", html_parsed.get("is_mobile_capable", 0)))
	mobile_level = str(
		payload.get("mobile_service_level") or html_parsed.get("mobile_service_level") or ("Level 1" if is_mobile else "No")
	).strip()
	is_symmetrical = cint(payload.get("is_symmetrical", html_parsed.get("is_symmetrical", 0)))

	if fetch_manifest and proc_url and (not is_mobile or not rh_code or not is_symmetrical):
		dep_info = get_mobile_capability_from_manifest(proc_url, doc_guid, correction_code)
		if dep_info:
			if not is_mobile and dep_info.get("is_mobile_capable"):
				is_mobile = dep_info["is_mobile_capable"]
				mobile_level = dep_info["mobile_service_level"]
			if not rh_code and dep_info.get("rh_correction_code"):
				rh_code = dep_info["rh_correction_code"]
			if not is_symmetrical and dep_info.get("is_symmetrical"):
				is_symmetrical = 1

	if not is_symmetrical and title and re.search(r"\bLH\b|\bRH\b|Left Hand|Right Hand", title, re.IGNORECASE):
		is_symmetrical = 1

	service_side = str(payload.get("service_side") or "").strip()
	if not service_side:
		service_side = detect_service_side(title, correction_code, rh_code)

	raw_parts = payload.get("parts") or payload.get("items") or html_parsed.get("parts") or []
	parts = normalize_procedure_parts(
		raw_parts,
		default_side=service_side if service_side in ("LH", "RH") else "Both",
	)

	return {
		"title": title,
		"correction_code": correction_code,
		"rh_correction_code": rh_code,
		"is_symmetrical": is_symmetrical,
		"service_side": service_side,
		"frt": frt,
		"doc_guid": doc_guid,
		"tesla_manual_url": proc_url,
		"category": category,
		"subcategory": subcategory,
		"applicable_model": model,
		"applicable_generation": generation,
		"gen_start_year_month": str(payload.get("gen_start_year_month") or html_parsed.get("gen_start_year_month") or url_meta.get("gen_start") or ""),
		"gen_end_year_month": str(payload.get("gen_end_year_month") or html_parsed.get("gen_end_year_month") or url_meta.get("gen_end") or ""),
		"description": str(payload.get("description") or html_parsed.get("description") or "")[:500],
		"is_mobile_capable": is_mobile,
		"mobile_service_level": mobile_level,
		"parts": parts,
	}


def parse_tesla_procedure_markdown(markdown_text, url=""):
	"""Extract canonical Tesla procedure metadata and OEM parts from Markdown/plain text."""
	if not markdown_text:
		return {}

	title_match = re.search(r"^#\s+([^\r\n]+)|Procedure\s*:\s*([^\r\n]+)", markdown_text, re.IGNORECASE | re.MULTILINE)
	cc_match = re.search(r"Correction\s*Code\s*[:#]?\s*([0-9A-Z]{6,10})", markdown_text, re.IGNORECASE)
	frt_match = re.search(r"(?:FRT|Labor\s*Hours?)\s*[:#]?\s*([0-9]+(?:\.[0-9]{1,2})?)", markdown_text, re.IGNORECASE)

	title = (title_match.group(1) or title_match.group(2)).strip() if title_match else ""
	code = cc_match.group(1).strip() if cc_match else ""
	service_side = detect_service_side(title, code)
	parts = extract_oem_parts_from_text(
		markdown_text,
		default_side=service_side if service_side in ("LH", "RH") else "Both",
	)
	return normalize_tesla_procedure(
		{
			"title": title,
			"correction_code": code,
			"frt": flt(frt_match.group(1), 2) if frt_match else 0.0,
			"parts": parts,
		},
		url=url,
		fetch_manifest=bool(url),
	)


def parse_tesla_procedure_html(html_content, url=""):
	"""Extract Title, Correction Code, FRT, parts, side, and metadata from Tesla manual HTML."""
	if not html_content:
		return {}

	data = {
		"title": "",
		"correction_code": "",
		"rh_correction_code": "",
		"is_symmetrical": 0,
		"service_side": "None",
		"frt": 0.0,
		"doc_guid": "",
		"tesla_manual_url": url,
		"category": "Maintenance",
		"subcategory": "",
		"applicable_model": "",
		"applicable_generation": "",
		"gen_start_year_month": "",
		"gen_end_year_month": "",
		"description": "",
		"is_mobile_capable": 0,
		"mobile_service_level": "No",
		"parts": [],
	}

	# 1. URL Metadata
	url_meta = parse_tesla_manual_url(url)
	data["doc_guid"] = url_meta.get("doc_guid") or ""
	data["applicable_model"] = url_meta.get("applicable_model") or ""
	data["applicable_generation"] = url_meta.get("matched_generation") or ""
	data["gen_start_year_month"] = url_meta.get("gen_start") or ""
	data["gen_end_year_month"] = url_meta.get("gen_end") or ""

	# 2. Extract Correction Code
	cc_match = re.search(r'class=["\'][^"\']*correction-code[^"\']*["\'][^>]*>\s*([0-9A-Z]{6,10})\s*<', html_content, re.IGNORECASE)
	if not cc_match:
		cc_match = re.search(r'Correction code\s*[:#]?\s*([0-9A-Z]{6,10})', html_content, re.IGNORECASE)

	if cc_match:
		data["correction_code"] = cc_match.group(1).strip()

	# 3. Extract FRT
	frt_match = re.search(r'class=["\'][^"\']*frt[^"\']*["\'][^>]*>\s*([0-9]+\.[0-9]{1,2})\s*<', html_content, re.IGNORECASE)
	if not frt_match:
		frt_match = re.search(r'FRT\s*[:#]?\s*([0-9]+\.[0-9]{1,2})', html_content, re.IGNORECASE)

	if frt_match:
		data["frt"] = flt(frt_match.group(1), 2)

	# 4. Extract Title
	title_match = re.search(r'class=["\'][^"\']*procedure-title[^"\']*["\'][^>]*>(.*?)</span>', html_content, re.IGNORECASE | re.DOTALL)
	if not title_match:
		title_match = re.search(r'<h1[^>]*class=["\'][^"\']*topictitle1[^"\']*["\'][^>]*>(.*?)</h1>', html_content, re.IGNORECASE | re.DOTALL)
	if not title_match:
		title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE | re.DOTALL)

	if title_match:
		clean_title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
		clean_title = re.sub(r'\s*\|\s*Tesla.*$', '', clean_title).strip()
		data["title"] = strip_correction_code_prefix(clean_title, data["correction_code"])

	# 5. Extract Category from Correction Code
	if data["correction_code"]:
		cat, subcat = get_category_for_correction_code(data["correction_code"])
		data["category"] = cat
		data["subcategory"] = subcat

	# 6. Extract Description / Notes
	desc_match = re.search(r'<p class=["\']shortdesc["\'][^>]*>(.*?)</p>', html_content, re.IGNORECASE | re.DOTALL)
	if desc_match:
		clean_desc = re.sub(r'<[^>]+>', ' ', desc_match.group(1))
		clean_desc = re.sub(r'\s+', ' ', clean_desc).strip()
		data["description"] = clean_desc[:500]

	# 7. Extract Mobile Capability
	is_mobile_capable = 0
	mobile_level = "No"

	if url:
		dep_info = get_mobile_capability_from_manifest(url, data.get("doc_guid"), data.get("correction_code"))
		if dep_info is not None:
			is_mobile_capable = dep_info["is_mobile_capable"]
			mobile_level = dep_info["mobile_service_level"]
			if dep_info.get("rh_correction_code"):
				data["rh_correction_code"] = dep_info["rh_correction_code"]
				data["is_symmetrical"] = 1

	if mobile_level in ("No", "Unknown", None):
		if any(k in html_content for k in ("MobileCapable-Yes", "icon-mobile-capable", "show-mobile-capable-indicator", "tds-chip--green-pastel", "Mobile Capable")):
			is_mobile_capable = 1
			mobile_level = "Level 1"
		elif any(k in html_content for k in ("MobileCapable-No", "icon-not-mobile-capable", "show-not-mobile-capable-indicator", "Not Mobile Capable")):
			is_mobile_capable = 0
			mobile_level = "No"

	if not is_mobile_capable and data.get("title") and "mobile only" in data["title"].lower():
		is_mobile_capable = 1
		mobile_level = "Mobile Only"

	data["is_mobile_capable"] = is_mobile_capable
	data["mobile_service_level"] = mobile_level

	# 8. Symmetrical, Service Side & OEM Parts Extraction
	if not data.get("is_symmetrical") and data.get("title"):
		if re.search(r'\bLH\b|\bRH\b|Left Hand|Right Hand', data["title"], re.IGNORECASE):
			data["is_symmetrical"] = 1

	service_side = detect_service_side(data.get("title"), data.get("correction_code"), data.get("rh_correction_code"))
	data["service_side"] = service_side
	data["parts"] = extract_oem_parts_from_text(
		html_content,
		default_side=service_side if service_side in ("LH", "RH") else "Both",
	)

	return data
