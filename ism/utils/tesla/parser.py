# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import re

from ism.utils.tesla.catalog import (
	get_category_for_correction_code,
	parse_tesla_manual_url,
)
from ism.utils.tesla.manifest import get_mobile_capability_from_manifest

try:
	from frappe.utils import cint, flt
except ImportError:  # Allow standalone Python ingestion/CLI usage without Frappe
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
_HTML_MOBILE_YES_MARKERS = (
	"MobileCapable-Yes",
	"icon-mobile-capable",
	"show-mobile-capable-indicator",
	"tds-chip--green-pastel",
	"Mobile Capable",
)
_HTML_MOBILE_NO_MARKERS = (
	"MobileCapable-No",
	"icon-not-mobile-capable",
	"show-not-mobile-capable-indicator",
	"Not Mobile Capable",
)


def swap_side_in_title(title, to_side="RH"):
	"""Swap symmetrical side indicator in title (e.g. LH -> RH or RH -> LH)."""
	if not title:
		return title

	to_side_upper = str(to_side).strip().upper()
	if to_side_upper == "RH":
		new_title = re.sub(r"\bLH\b", "RH", title)
		new_title = re.sub(r"\bLeft\s+Hand\b", "Right Hand", new_title, flags=re.IGNORECASE)
		new_title = re.sub(r"\bLeft\b(?!\s+(?:Turn|Foot))", "Right", new_title, flags=re.IGNORECASE)
		return new_title
	if to_side_upper == "LH":
		new_title = re.sub(r"\bRH\b", "LH", title)
		new_title = re.sub(r"\bRight\s+Hand\b", "Left Hand", new_title, flags=re.IGNORECASE)
		new_title = re.sub(r"\bRight\b(?!\s+(?:Turn|Foot))", "Left", new_title, flags=re.IGNORECASE)
		return new_title
	return title


def detect_service_side(title, correction_code=None, rh_correction_code=None):
	"""Detect whether a title or correction code represents LH, RH, or None."""
	if title:
		if re.search(r"\bRH\b|Right Hand|\bRight\b(?!\s+(?:Turn|Foot))", title, re.IGNORECASE):
			return "RH"
		if re.search(r"\bLH\b|Left Hand|\bLeft\b(?!\s+(?:Turn|Foot))", title, re.IGNORECASE):
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


def _merge_manifest_and_symmetry(
	title,
	url="",
	doc_guid=None,
	correction_code=None,
	is_mobile=0,
	mobile_level="No",
	rh_code="",
	is_symmetrical=0,
	fetch_manifest=True,
	html_content="",
):
	"""Merge manifest capability, HTML indicators, and title-based symmetry flags."""
	if fetch_manifest and url and (not is_mobile or not rh_code or not is_symmetrical):
		dep_info = get_mobile_capability_from_manifest(url, doc_guid, correction_code)
		if dep_info:
			if not is_mobile and dep_info.get("is_mobile_capable"):
				is_mobile = dep_info["is_mobile_capable"]
				mobile_level = dep_info["mobile_service_level"]
			if not rh_code and dep_info.get("rh_correction_code"):
				rh_code = dep_info["rh_correction_code"]
			if not is_symmetrical and dep_info.get("is_symmetrical"):
				is_symmetrical = 1

	if html_content and mobile_level in ("No", "Unknown", None, ""):
		if any(k in html_content for k in _HTML_MOBILE_YES_MARKERS):
			is_mobile = 1
			mobile_level = "Level 1"
		elif any(k in html_content for k in _HTML_MOBILE_NO_MARKERS):
			is_mobile = 0
			mobile_level = "No"

	if not is_mobile and title and "mobile only" in title.lower():
		is_mobile = 1
		mobile_level = "Mobile Only"

	if not is_symmetrical and title and re.search(r"\bLH\b|\bRH\b|Left Hand|Right Hand", title, re.IGNORECASE):
		is_symmetrical = 1

	return {
		"is_mobile_capable": is_mobile,
		"mobile_service_level": mobile_level,
		"rh_correction_code": rh_code,
		"is_symmetrical": is_symmetrical,
	}


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
		payload.get("mobile_service_level")
		or html_parsed.get("mobile_service_level")
		or ("Level 1" if is_mobile else "No")
	).strip()
	is_symmetrical = cint(payload.get("is_symmetrical", html_parsed.get("is_symmetrical", 0)))

	merged = _merge_manifest_and_symmetry(
		title=title,
		url=proc_url,
		doc_guid=doc_guid,
		correction_code=correction_code,
		is_mobile=is_mobile,
		mobile_level=mobile_level,
		rh_code=rh_code,
		is_symmetrical=is_symmetrical,
		fetch_manifest=fetch_manifest,
	)
	is_mobile = merged["is_mobile_capable"]
	mobile_level = merged["mobile_service_level"]
	rh_code = merged["rh_correction_code"]
	is_symmetrical = merged["is_symmetrical"]

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
		"gen_start_year_month": str(
			payload.get("gen_start_year_month")
			or html_parsed.get("gen_start_year_month")
			or url_meta.get("gen_start")
			or ""
		),
		"gen_end_year_month": str(
			payload.get("gen_end_year_month")
			or html_parsed.get("gen_end_year_month")
			or url_meta.get("gen_end")
			or ""
		),
		"description": str(payload.get("description") or html_parsed.get("description") or "")[:500],
		"is_mobile_capable": is_mobile,
		"mobile_service_level": mobile_level,
		"parts": parts,
	}


def parse_tesla_procedure_markdown(markdown_text, url=""):
	"""Extract canonical Tesla procedure metadata and OEM parts from Markdown/plain text."""
	if not markdown_text:
		return {}

	title_match = re.search(
		r"^#\s+([^\r\n]+)|Procedure\s*:\s*([^\r\n]+)",
		markdown_text,
		re.IGNORECASE | re.MULTILINE,
	)
	cc_match = re.search(r"Correction\s*Code\s*[:#]?\s*([0-9A-Z]{6,10})", markdown_text, re.IGNORECASE)
	frt_match = re.search(
		r"(?:FRT|Labor\s*Hours?)\s*[:#]?\s*([0-9]+(?:\.[0-9]{1,2})?)",
		markdown_text,
		re.IGNORECASE,
	)

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

	url_meta = parse_tesla_manual_url(url)
	data = {
		"title": "",
		"correction_code": "",
		"rh_correction_code": "",
		"is_symmetrical": 0,
		"service_side": "None",
		"frt": 0.0,
		"doc_guid": url_meta.get("doc_guid") or "",
		"tesla_manual_url": url,
		"category": "Maintenance",
		"subcategory": "",
		"applicable_model": url_meta.get("applicable_model") or "",
		"applicable_generation": url_meta.get("matched_generation") or "",
		"gen_start_year_month": url_meta.get("gen_start") or "",
		"gen_end_year_month": url_meta.get("gen_end") or "",
		"description": "",
		"is_mobile_capable": 0,
		"mobile_service_level": "No",
		"parts": [],
	}

	cc_match = re.search(
		r'class=["\'][^"\']*correction-code[^"\']*["\'][^>]*>\s*([0-9A-Z]{6,10})\s*<',
		html_content,
		re.IGNORECASE,
	)
	if not cc_match:
		cc_match = re.search(r"Correction code\s*[:#]?\s*([0-9A-Z]{6,10})", html_content, re.IGNORECASE)
	if cc_match:
		data["correction_code"] = cc_match.group(1).strip()

	frt_match = re.search(
		r'class=["\'][^"\']*frt[^"\']*["\'][^>]*>\s*([0-9]+\.[0-9]{1,2})\s*<',
		html_content,
		re.IGNORECASE,
	)
	if not frt_match:
		frt_match = re.search(r"FRT\s*[:#]?\s*([0-9]+\.[0-9]{1,2})", html_content, re.IGNORECASE)
	if frt_match:
		data["frt"] = flt(frt_match.group(1), 2)

	title_match = re.search(
		r'class=["\'][^"\']*procedure-title[^"\']*["\'][^>]*>(.*?)</span>',
		html_content,
		re.IGNORECASE | re.DOTALL,
	)
	if not title_match:
		title_match = re.search(
			r'<h1[^>]*class=["\'][^"\']*topictitle1[^"\']*["\'][^>]*>(.*?)</h1>',
			html_content,
			re.IGNORECASE | re.DOTALL,
		)
	if not title_match:
		title_match = re.search(r"<title>(.*?)</title>", html_content, re.IGNORECASE | re.DOTALL)
	if title_match:
		clean_title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
		clean_title = re.sub(r"\s*\|\s*Tesla.*$", "", clean_title).strip()
		data["title"] = strip_correction_code_prefix(clean_title, data["correction_code"])

	if data["correction_code"]:
		cat, subcat = get_category_for_correction_code(data["correction_code"])
		data["category"] = cat
		data["subcategory"] = subcat

	desc_match = re.search(r'<p class=["\']shortdesc["\'][^>]*>(.*?)</p>', html_content, re.IGNORECASE | re.DOTALL)
	if desc_match:
		clean_desc = re.sub(r"<[^>]+>", " ", desc_match.group(1))
		data["description"] = re.sub(r"\s+", " ", clean_desc).strip()[:500]

	merged = _merge_manifest_and_symmetry(
		title=data["title"],
		url=url,
		doc_guid=data.get("doc_guid"),
		correction_code=data.get("correction_code"),
		fetch_manifest=bool(url),
		html_content=html_content,
	)
	data.update(merged)

	service_side = detect_service_side(
		data.get("title"),
		data.get("correction_code"),
		data.get("rh_correction_code"),
	)
	data["service_side"] = service_side
	data["parts"] = extract_oem_parts_from_text(
		html_content,
		default_side=service_side if service_side in ("LH", "RH") else "Both",
	)
	return data
