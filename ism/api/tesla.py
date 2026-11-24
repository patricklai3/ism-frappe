# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import urllib.error
import urllib.request
from typing import Any, Optional, Union
from urllib.parse import urljoin, urlparse

import frappe
from frappe.utils import cint, flt

from ism.api._validation import escape_like
from ism.api.parts import get_default_warehouse, quick_create_part
from ism.ism.doctype.repair_order.repair_order import (
	_get_default_labor_rate,
	build_service_package_row_fields,
)
from ism.ism.doctype.repair_vehicle.repair_vehicle import (
	build_vehicle_intelligence,
	build_vehicle_search_results,
	decode_tesla_vin,
	resolve_vehicle_identifier,
)
from ism.utils.tesla_manual import (
	detect_service_side,
	get_generation_catalog,
	normalize_tesla_procedure,
	parse_tesla_procedure_html,
	parse_tesla_procedure_markdown,
	resolve_generation_entries,
	swap_side_in_title,
	validate_vehicle_compatibility,
)

_ALLOWED_TESLA_HOSTS = ("service.tesla.com", "service.teslamotors.com")
_VALID_CONFLICT_POLICIES = ("skip", "overwrite", "add", "error")
_LEGACY_CONFLICT_ALIASES = {"update": "overwrite", "overwrite": "overwrite", "add": "add", "skip": "skip", "error": "error"}
_MAX_TESLA_HTML_BYTES = 5 * 1024 * 1024
_DEFAULT_URLOPEN = urllib.request.urlopen


class _TeslaSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
	"""Re-validate every HTTP redirect target against the Tesla HTTPS domain allowlist."""

	def redirect_request(self, req, fp, code, msg, headers, newurl):
		resolved_url = urljoin(req.full_url, newurl)
		_validate_tesla_url(resolved_url)
		return super().redirect_request(req, fp, code, msg, headers, resolved_url)


@frappe.whitelist()
def fetch_and_parse_tesla_procedure(
	url: str,
	repair_order: Optional[str] = None,
	vehicle: Optional[str] = None,
) -> dict[str, Any]:
	"""Fetch a Tesla service manual procedure server-side and return its normalized metadata."""
	if not (frappe.has_permission("Service Package", "read") or frappe.has_permission("Repair Order", "read")):
		frappe.throw(frappe._("Not permitted to read Tesla service procedures."), frappe.PermissionError)

	url = _require_non_empty_str(url, "Tesla procedure URL")
	repair_order = _optional_str(repair_order, "Repair Order")
	vehicle = _optional_str(vehicle, "Repair Vehicle")

	_validate_tesla_url(url)
	html_content = _fetch_tesla_html(url)

	parsed = parse_tesla_procedure_html(html_content, url=url)
	if not parsed.get("title") and not parsed.get("correction_code"):
		frappe.throw(frappe._("Could not find a valid Tesla procedure on this page. Please verify the URL."))

	target_vehicle = vehicle
	if not target_vehicle and repair_order and frappe.db.exists("Repair Order", repair_order):
		ro_doc = frappe.get_doc("Repair Order", repair_order)
		ro_doc.check_permission("read")
		target_vehicle = ro_doc.vehicle

	if target_vehicle:
		v_doc = frappe.get_doc("Repair Vehicle", target_vehicle)
		v_doc.check_permission("read")
		parsed["compatibility"] = validate_vehicle_compatibility(v_doc, url)

	return parsed


@frappe.whitelist()
def import_tesla_procedure(
	repair_order: str,
	url: Optional[str] = None,
	data: Optional[Union[dict[str, Any], str]] = None,
	raw_html: Optional[str] = None,
	raw_markdown: Optional[str] = None,
	on_conflict: str = "skip",
	force: Any = False,
	allow_generation_mismatch: Any = False,
	duplicate_action: Optional[str] = None,
	is_supplement: Any = 0,
	side: Optional[str] = None,
	advisor_notes: Optional[str] = None,
	**kwargs: Any,
) -> dict[str, Any]:
	"""Import a Tesla service procedure onto a Repair Order using declarative conflict/compatibility policies."""
	frappe.has_permission("Repair Order", "write", throw=True)

	repair_order = _require_non_empty_str(repair_order, "Repair Order")
	url = _optional_str(url, "Tesla procedure URL")
	if url:
		_validate_tesla_url(url)
	raw_html = _optional_str(raw_html, "Raw HTML")
	raw_markdown = _optional_str(raw_markdown, "Raw Markdown")
	if raw_html and len(raw_html.encode("utf-8")) > _MAX_TESLA_HTML_BYTES:
		frappe.throw(frappe._("Raw HTML payload exceeds maximum allowed size."), frappe.ValidationError)
	if raw_markdown and len(raw_markdown.encode("utf-8")) > _MAX_TESLA_HTML_BYTES:
		frappe.throw(frappe._("Raw Markdown payload exceeds maximum allowed size."), frappe.ValidationError)

	if not frappe.db.exists("Repair Order", repair_order):
		frappe.throw(frappe._("Repair Order '{0}' does not exist.").format(repair_order))

	ro = frappe.get_doc("Repair Order", repair_order)
	ro.check_permission("write")

	conflict_policy = _resolve_conflict_policy(on_conflict, duplicate_action)
	is_forced = _coerce_bool(force) or _coerce_bool(allow_generation_mismatch)

	raw_data = _parse_raw_procedure_data(data)
	if not raw_data and raw_html:
		raw_data = parse_tesla_procedure_html(raw_html, url=url or "")
	elif not raw_data and raw_markdown:
		raw_data = parse_tesla_procedure_markdown(raw_markdown, url=url or "")
	elif not raw_data and url:
		raw_data = fetch_and_parse_tesla_procedure(url)

	if not raw_data or (not raw_data.get("title") and not raw_data.get("procedure_title") and not raw_data.get("procedure_name") and not raw_data.get("correction_code")):
		frappe.throw(frappe._("Incomplete procedure data. Title or Correction Code required."))

	raw_proc_url = raw_data.get("tesla_manual_url") or raw_data.get("url") or url or ""
	proc_url = _optional_str(raw_proc_url, "Tesla procedure URL") or ""
	if proc_url:
		_validate_tesla_url(proc_url)
	proc_data = normalize_tesla_procedure(raw_data, url=proc_url)

	warnings: list[str] = []
	v_doc = None
	if ro.vehicle:
		v_doc = frappe.get_doc("Repair Vehicle", ro.vehicle)
		v_doc.check_permission("read")
	compat = validate_vehicle_compatibility(v_doc, proc_url)
	if not compat.get("is_compatible"):
		msg = compat.get("warning") or frappe._("Vehicle model or generation does not match Tesla procedure.")
		if not is_forced:
			frappe.throw(msg, frappe.ValidationError)
		warnings.append(msg)

	side_choice = side if side is not None else (kwargs.get("side") or proc_data.get("side") or "")
	existing_pkg_name = _find_existing_service_package_name(
		proc_data.get("correction_code") or "",
		proc_data.get("title") or "",
		applicable_model=proc_data.get("applicable_model") or "",
		applicable_generation=proc_data.get("applicable_generation") or "",
		doc_guid=proc_data.get("doc_guid") or "",
		tesla_manual_url=proc_data.get("tesla_manual_url") or proc_url or "",
	)
	existing_pkg = frappe.get_cached_doc("Service Package", existing_pkg_name) if existing_pkg_name else None
	pre_ops = _resolve_import_operations(proc_data, existing_pkg, side_choice)

	# Enforce conflict policy before mutating or creating Service Package records
	matched_pre_rows = [
		_find_matching_ro_service(ro, op["code"], op["side"], existing_pkg_name) for op in pre_ops
	]
	if conflict_policy == "error":
		for op, matched_row in zip(pre_ops, matched_pre_rows, strict=True):
			if matched_row:
				code_display = op["code"] or existing_pkg_name or "this procedure"
				side_str = f" ({op['side']})" if op["side"] and op["side"] != "None" else ""
				frappe.throw(
					frappe._("Correction code {0}{1} already exists on Repair Order {2}.").format(
						code_display, side_str, ro.name
					),
					frappe.DuplicateEntryError,
				)

	if conflict_policy == "skip" and pre_ops and all(matched_pre_rows):
		first_skipped = matched_pre_rows[0]
		title = proc_data["title"]
		correction_code = proc_data["correction_code"]
		return {
			"status": "skipped",
			"action": "skipped",
			"conflict_policy": conflict_policy,
			"message": f"Procedure '{title}' already exists on {ro.name}; skipped.",
			"repair_order": ro.name,
			"service_package": existing_pkg_name,
			"correction_code": correction_code,
			"frt": proc_data["frt"],
			"labor_amount": flt(getattr(first_skipped, "labor_amount", 0), 2),
			"is_mobile_capable": cint(getattr(first_skipped, "is_mobile_capable", 0)),
			"service_row": first_skipped.as_dict() if hasattr(first_skipped, "as_dict") else None,
			"count_added": 0,
			"count_updated": 0,
			"count_skipped": len(pre_ops),
			"warnings": warnings,
		}

	pkg = _upsert_service_package(proc_data, proc_url)
	operations = _resolve_import_operations(proc_data, pkg, side_choice)

	notes = advisor_notes if advisor_notes is not None else (kwargs.get("advisor_notes") or "")
	supp_flag = is_supplement or proc_data.get("is_supplement") or kwargs.get("is_supplement")
	has_prior_auth = _ro_has_prior_authorization(ro)
	is_supp = 1 if (_coerce_bool(supp_flag) or has_prior_auth) else 0

	hourly_rate = flt(pkg.hourly_rate) or _get_default_labor_rate()
	labor_hours = flt(proc_data.get("frt"), 2) or flt(pkg.standard_labor_hours) or 1.0
	labor_amount = flt(labor_hours * hourly_rate, 2)

	outcome = _apply_operations_to_ro(
		ro=ro,
		pkg=pkg,
		proc_data=proc_data,
		proc_url=proc_url,
		operations=operations,
		conflict_policy=conflict_policy,
		is_supp=is_supp,
		hourly_rate=hourly_rate,
		labor_hours=labor_hours,
		labor_amount=labor_amount,
		advisor_notes=notes,
	)

	if outcome["mutated"]:
		if is_supp and any(s.status == "Approved" for s in ro.services):
			ro.approval_status = "Partially Approved"
		if ro.services:
			ro.is_mobile_capable = 1 if all(cint(s.is_mobile_capable) for s in ro.services) else 0
		ro.flags.ignore_validate_update_after_submit = True
		ro.save()

	affected_rows = outcome["affected_rows"]
	last_row = affected_rows[-1] if affected_rows else outcome["first_skipped_row"]
	action_taken = outcome["action"]
	title = proc_data["title"]
	correction_code = proc_data["correction_code"]

	if outcome["mutated"]:
		frappe.publish_realtime(
			"repair_order_updated",
			{
				"docname": ro.name,
				"service_title": title,
				"correction_code": correction_code,
				"frt": proc_data["frt"],
				"action": action_taken,
				"is_mobile_capable": last_row.is_mobile_capable if last_row else 0,
			},
			doctype="Repair Order",
			docname=ro.name,
		)

	return {
		"status": "skipped" if action_taken == "skipped" else "success",
		"action": action_taken,
		"conflict_policy": conflict_policy,
		"message": (
			f"Procedure '{title}' already exists on {ro.name}; skipped."
			if action_taken == "skipped"
			else f"Successfully {action_taken} '{title}' on {ro.name}"
		),
		"repair_order": ro.name,
		"service_package": pkg.name,
		"correction_code": correction_code,
		"frt": proc_data["frt"],
		"labor_amount": labor_amount,
		"is_mobile_capable": last_row.is_mobile_capable if last_row else 0,
		"service_row": last_row.as_dict() if hasattr(last_row, "as_dict") else None,
		"count_added": outcome["count_added"],
		"count_updated": outcome["count_updated"],
		"count_skipped": outcome["count_skipped"],
		"warnings": warnings,
	}


@frappe.whitelist()
def decode_vehicle_vin(vin: str, manufactured_month: Optional[str] = None) -> dict[str, Any]:
	"""Decode a 17-character Tesla VIN strictly offline using bundled WMI and spec tables."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	if not isinstance(vin, str):
		frappe.throw(frappe._("VIN must be a string."), frappe.ValidationError)
	month_str = _optional_str(manufactured_month, "Manufactured Month")
	return decode_tesla_vin(vin, manufactured_month=month_str)


@frappe.whitelist()
def get_vehicle_generation_catalog(model: Optional[str] = None) -> Any:
	"""Return canonical Tesla generation metadata for a specific model or all models."""
	if not (frappe.has_permission("Service Package", "read") or frappe.has_permission("Repair Vehicle", "read")):
		frappe.throw(frappe._("Not permitted to read Tesla generation catalog."), frappe.PermissionError)
	model_str = _optional_str(model, "Vehicle Model")
	return get_generation_catalog(model_str)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def query_service_packages_for_vehicle(
	doctype: Optional[str] = None,
	txt: str = "",
	searchfield: Optional[str] = None,
	start: int = 0,
	page_len: int = 20,
	filters: Optional[Union[dict[str, Any], str]] = None,
) -> list[list[str]]:
	"""Filter Service Package link field options by Repair Vehicle model and generation."""
	from ism.scheduling.bookable_catalog import is_package_eligible_for_vehicle

	frappe.has_permission("Service Package", "read", throw=True)
	filter_dict = filters
	if isinstance(filter_dict, str):
		try:
			filter_dict = json.loads(filter_dict)
		except Exception:
			filter_dict = {}
	if not isinstance(filter_dict, dict):
		filter_dict = {}

	vehicle_name = filter_dict.get("vehicle")
	v_model = None
	v_year = None
	v_gen = None
	v_month = None
	if vehicle_name and frappe.db.exists("Repair Vehicle", vehicle_name):
		frappe.has_permission("Repair Vehicle", "read", doc=vehicle_name, throw=True)
		v_doc = frappe.get_cached_doc("Repair Vehicle", vehicle_name)
		v_model = getattr(v_doc, "model", None)
		v_year = cint(getattr(v_doc, "model_year", 0)) or None
		v_gen = getattr(v_doc, "vehicle_generation", None)
		v_month = getattr(v_doc, "manufactured_month", None)

	txt_clean = (txt or "").strip()
	or_filters = None
	if txt_clean:
		escaped = escape_like(txt_clean)
		or_filters = [
			["name", "like", f"%{escaped}%"],
			["package_name", "like", f"%{escaped}%"],
			["correction_code", "like", f"%{escaped}%"],
			["applicable_model", "like", f"%{escaped}%"],
			["applicable_generation", "like", f"%{escaped}%"],
		]

	rows = frappe.get_list(
		"Service Package",
		fields=[
			"name",
			"package_name",
			"correction_code",
			"applicable_model",
			"applicable_generation",
			"applicable_year_from",
			"applicable_year_to",
			"tesla_manual_url",
		],
		or_filters=or_filters,
		limit=200,
		order_by="modified desc",
	)

	names = [r["name"] for r in rows]
	compat_by_parent: dict[str, list[dict[str, Any]]] = {}
	if names:
		compat_rows = frappe.get_all(
			"Service Package Generation",
			filters={"parenttype": "Service Package", "parent": ["in", names]},
			fields=[
				"parent",
				"applicable_model",
				"generation_name",
				"year_from",
				"month_from",
				"year_to",
				"month_to",
			],
		)
		for crow in compat_rows:
			compat_by_parent.setdefault(crow["parent"], []).append(crow)

	eligible = []
	for row in rows:
		row["generation_compatibility"] = compat_by_parent.get(row["name"], [])
		if v_model and not is_package_eligible_for_vehicle(
			row,
			vehicle_model=v_model,
			vehicle_year=v_year,
			vehicle_generation=v_gen,
			manufactured_month=v_month,
		):
			continue
		meta_parts = [p for p in (row.get("correction_code"), row.get("applicable_model"), row.get("applicable_generation")) if p]
		desc = " | ".join(meta_parts) if meta_parts else (row.get("package_name") or row["name"])
		eligible.append([row["name"], desc])

	start_idx = max(0, cint(start))
	limit_len = max(1, min(cint(page_len) or 20, 100))
	return eligible[start_idx : start_idx + limit_len]


@frappe.whitelist()
def search_vehicles(
	query: Optional[str] = None,
	customer: Optional[str] = None,
	model: Optional[str] = None,
	generation: Optional[str] = None,
	limit: int = 20,
) -> list[dict[str, Any]]:
	"""Search vehicles across VIN, license plate, model, generation, primary owner, and linked co-owners."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	query = _optional_str(query, "Search query")
	customer = _optional_str(customer, "Customer")
	model = _optional_str(model, "Model")
	generation = _optional_str(generation, "Generation")
	if isinstance(limit, bool):
		frappe.throw(frappe._("Search limit cannot be a boolean."), frappe.ValidationError)
	try:
		safe_limit = max(1, min(int(limit), 100))
	except (TypeError, ValueError):
		frappe.throw(frappe._("Search limit must be a positive integer."), frappe.ValidationError)

	results = build_vehicle_search_results(
		query=query,
		customer=customer,
		model=model,
		generation=generation,
		limit=safe_limit,
	)
	if not results:
		return []

	candidate_names = [row["name"] for row in results if row.get("name")]
	if not candidate_names:
		return []

	permitted_names = set(
		frappe.get_list(
			"Repair Vehicle",
			filters={"name": ["in", candidate_names]},
			pluck="name",
		)
	)
	return [row for row in results if row.get("name") in permitted_names]


@frappe.whitelist()
def get_vehicle_intelligence(vehicle: str, history_limit: int = 20) -> dict[str, Any]:
	"""Return decoded vehicle specs, multi-customer owners, RO history, and deferred service summary."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	vehicle = _require_non_empty_str(vehicle, "Vehicle")
	vehicle_name = resolve_vehicle_identifier(vehicle)
	frappe.get_doc("Repair Vehicle", vehicle_name).check_permission("read")
	if isinstance(history_limit, bool):
		frappe.throw(frappe._("history_limit cannot be a boolean."), frappe.ValidationError)
	try:
		safe_history_limit = max(1, min(int(history_limit), 100))
	except (TypeError, ValueError):
		frappe.throw(frappe._("history_limit must be a positive integer."), frappe.ValidationError)
	return build_vehicle_intelligence(vehicle_identifier=vehicle_name, history_limit=safe_history_limit)


@frappe.whitelist()
def link_vehicle_customer(
	vehicle: str,
	customer: str,
	is_primary: Any = False,
	relationship: Optional[str] = None,
) -> dict[str, Any]:
	"""Link or update a secondary or primary customer owner on a Repair Vehicle."""
	frappe.has_permission("Repair Vehicle", "write", throw=True)
	vehicle = _require_non_empty_str(vehicle, "Vehicle")
	customer = _require_non_empty_str(customer, "Customer")
	relationship = _optional_str(relationship, "Relationship")

	vehicle_name = resolve_vehicle_identifier(vehicle)
	vdoc = frappe.get_doc("Repair Vehicle", vehicle_name)
	vdoc.check_permission("write")
	vdoc.add_customer(
		customer=customer,
		is_primary=_coerce_bool(is_primary),
		relationship=relationship,
	)
	vdoc.reload()
	return {
		"vehicle": vdoc.name,
		"primary_customer": vdoc.customer,
		"owners": [
			{
				"customer": row.customer,
				"customer_name": row.customer_name,
				"is_primary": cint(row.is_primary),
				"relationship": row.relationship or "",
			}
			for row in (vdoc.customers or [])
		],
	}


def _require_non_empty_str(value: Any, label: str) -> str:
	if not isinstance(value, str) or not value.strip():
		frappe.throw(frappe._("{0} must be a non-empty string.").format(label), frappe.ValidationError)
	return value.strip()


def _optional_str(value: Any, label: str) -> Optional[str]:
	if value is None or value == "":
		return None
	if not isinstance(value, str):
		frappe.throw(frappe._("{0} must be a string.").format(label), frappe.ValidationError)
	return value.strip() or None


def _coerce_bool(val: Any) -> bool:
	if isinstance(val, bool):
		return val
	if isinstance(val, (int, float)):
		return bool(val)
	if isinstance(val, str):
		return val.strip().lower() in ("1", "true", "yes", "on")
	return bool(val)


def _resolve_conflict_policy(on_conflict: str, duplicate_action: Optional[str]) -> str:
	if duplicate_action is not None and str(duplicate_action).strip():
		alias = str(duplicate_action).strip().lower()
		if alias in _LEGACY_CONFLICT_ALIASES:
			return _LEGACY_CONFLICT_ALIASES[alias]
		frappe.throw(frappe._("Invalid duplicate_action '{0}'.").format(duplicate_action), frappe.ValidationError)

	policy = str(on_conflict or "skip").strip().lower()
	if policy not in _VALID_CONFLICT_POLICIES:
		frappe.throw(
			frappe._("Invalid on_conflict policy '{0}'. Must be one of: {1}.").format(
				on_conflict, ", ".join(_VALID_CONFLICT_POLICIES)
			),
			frappe.ValidationError,
		)
	return policy


def _validate_tesla_url(url: str) -> None:
	if not isinstance(url, str) or not url.strip():
		frappe.throw(frappe._("Tesla procedure URL is required."))
	cleaned = url.strip()
	if any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
		frappe.throw(frappe._("Invalid characters in Tesla procedure URL."))
	parsed_url = urlparse(cleaned)
	hostname = (parsed_url.hostname or "").lower()
	if (
		parsed_url.scheme != "https"
		or hostname not in _ALLOWED_TESLA_HOSTS
		or parsed_url.username
		or parsed_url.password
		or parsed_url.port not in (None, 443)
	):
		frappe.throw(frappe._("Invalid Tesla manual domain. URL must be an https:// URL from service.tesla.com."))


def _open_tesla_request(req: urllib.request.Request):
	if urllib.request.urlopen is not _DEFAULT_URLOPEN:
		return urllib.request.urlopen(req, timeout=15)
	opener = urllib.request.build_opener(_TeslaSafeRedirectHandler)
	return opener.open(req, timeout=15)


def _read_bounded_tesla_response(response) -> str:
	final_url = response.geturl() if hasattr(response, "geturl") else None
	if isinstance(final_url, str) and final_url.strip():
		_validate_tesla_url(final_url)
	raw = response.read(_MAX_TESLA_HTML_BYTES + 1)
	if isinstance(raw, str):
		raw_bytes = raw.encode("utf-8", errors="ignore")
	else:
		raw_bytes = raw or b""
	if len(raw_bytes) > _MAX_TESLA_HTML_BYTES:
		frappe.throw(
			frappe._("Tesla procedure page exceeds the maximum allowed size."),
			frappe.ValidationError,
		)
	return raw_bytes.decode("utf-8", errors="ignore")


def _fetch_tesla_html(url: str) -> str:
	_validate_tesla_url(url)
	headers = {
		"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
		"Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
		"Accept-Language": "en-US,en;q=0.9",
		"Referer": "https://service.tesla.com/",
	}
	req = urllib.request.Request(url, headers=headers)
	try:
		with _open_tesla_request(req) as response:
			return _read_bounded_tesla_response(response)
	except frappe.ValidationError:
		raise
	except urllib.error.HTTPError:
		try:
			fallback_req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
			with _open_tesla_request(fallback_req) as response:
				return _read_bounded_tesla_response(response)
		except frappe.ValidationError:
			raise
		except Exception:
			frappe.log_error(title="Tesla Procedure Fetch Failed", message=frappe.get_traceback())
			frappe.throw(
				frappe._("Unable to fetch Tesla service manual page. Please verify the URL and try again."),
				frappe.ValidationError,
			)
	except Exception:
		frappe.log_error(title="Tesla Procedure Fetch Failed", message=frappe.get_traceback())
		frappe.throw(
			frappe._("Unable to fetch Tesla service manual page. Please verify the URL and try again."),
			frappe.ValidationError,
		)


def _parse_raw_procedure_data(data: Optional[Union[dict[str, Any], str]]) -> dict[str, Any]:
	if data is None or data == "":
		return {}
	if isinstance(data, dict):
		return data
	if isinstance(data, str):
		stripped = data.strip()
		if not stripped:
			return {}
		try:
			decoded = json.loads(stripped)
		except Exception:
			frappe.throw(frappe._("Malformed JSON in Tesla procedure data."), frappe.ValidationError)
		if not isinstance(decoded, dict):
			frappe.throw(frappe._("Tesla procedure data JSON must be an object."), frappe.ValidationError)
		return decoded
	frappe.throw(frappe._("Tesla procedure data must be a dict or JSON object string."), frappe.ValidationError)


def _ro_has_prior_authorization(ro) -> bool:
	return bool(
		ro.authorized_at
		or any(s.status == "Approved" for s in (ro.services or []))
		or ro.approval_status in ("Approved", "Partially Approved")
		or ro.status in ("Authorized - In Progress", "Waiting on Parts", "Work Completed", "Billed / Paid")
	)


def _resolve_import_operations(proc_data: dict[str, Any], pkg, side_choice: str) -> list[dict[str, str]]:
	title = proc_data["title"]
	correction_code = proc_data["correction_code"]
	side_param = str(side_choice or "").strip().lower()
	is_symmetrical = cint(proc_data.get("is_symmetrical") or (1 if pkg and pkg.is_symmetrical else 0))

	rh_code = proc_data.get("rh_correction_code") or (getattr(pkg, "rh_correction_code", None) if pkg else None) or correction_code
	lh_code = getattr(pkg, "correction_code", None) or correction_code
	base_side = detect_service_side(title, correction_code, rh_code)

	if base_side == "RH":
		rh_title = title
		lh_title = swap_side_in_title(title, "LH")
		rh_code = correction_code
		if pkg and getattr(pkg, "correction_code", None) and pkg.correction_code != rh_code:
			lh_code = pkg.correction_code
	else:
		lh_title = title
		rh_title = swap_side_in_title(title, "RH")
		lh_code = correction_code

	if side_param == "both" and is_symmetrical:
		return [
			{"side": "LH", "title": lh_title, "code": lh_code},
			{"side": "RH", "title": rh_title, "code": rh_code},
		]
	if side_param == "rh" and is_symmetrical:
		return [{"side": "RH", "title": rh_title, "code": rh_code}]
	if side_param == "lh" and is_symmetrical:
		return [{"side": "LH", "title": lh_title, "code": lh_code}]

	pos = "LH" if is_symmetrical and base_side != "RH" else ("RH" if is_symmetrical and base_side == "RH" else "None")
	return [{"side": pos, "title": title, "code": correction_code}]


def _find_matching_ro_service(ro, op_code: str, op_side: str, pkg_name: Optional[str]):
	for row in (ro.services or []):
		s_code = str(getattr(row, "correction_code", "") or "").strip()
		s_pos = str(getattr(row, "service_position", "") or "None").strip()
		s_pkg = str(getattr(row, "service_package", "") or "").strip()

		pos_matches = (s_pos == op_side) or (s_pos in ("None", "") and op_side in ("None", ""))
		code_or_pkg_matches = (op_code and s_code == op_code) or (pkg_name and s_pkg == pkg_name)
		if pos_matches and code_or_pkg_matches:
			return row
	return None


def _find_existing_service_package_name(
	correction_code: str,
	title: str,
	applicable_model: str = "",
	applicable_generation: str = "",
	doc_guid: str = "",
	tesla_manual_url: str = "",
) -> Optional[str]:
	"""Find an existing Service Package scoped by GUID/URL or (model, generation)."""
	app_model = (applicable_model or "").strip()
	app_gen = (applicable_generation or "").strip()
	target_url = (tesla_manual_url or "").strip()

	def _matches_scope(cand: dict[str, Any]) -> bool:
		c_model = (cand.get("applicable_model") or "").strip()
		c_gen = (cand.get("applicable_generation") or "").strip()
		if app_model and c_model and c_model != app_model:
			return False
		if app_gen and c_gen:
			if c_gen == app_gen:
				return True
			in_names = {e["generation_name"] for e in resolve_generation_entries(app_gen, model=app_model, manual_url=target_url)}
			cand_names = {e["generation_name"] for e in resolve_generation_entries(c_gen, model=c_model)}
			if in_names and cand_names:
				return in_names == cand_names
			return False
		if app_gen != c_gen:
			return False
		if app_model and c_model:
			return c_model == app_model
		return not app_model and not c_model

	if doc_guid:
		by_guid = frappe.db.get_all(
			"Service Package",
			filters={"doc_guid": doc_guid},
			fields=["name", "applicable_model", "applicable_generation"],
			limit=5,
		)
		for cand in by_guid:
			if not app_model or not (cand.get("applicable_model") or "").strip() or (cand.get("applicable_model") or "").strip() == app_model:
				return cand["name"]

	if target_url:
		by_url = frappe.db.get_value("Service Package", {"tesla_manual_url": target_url}, "name")
		if by_url:
			return by_url

	candidates = []
	if correction_code:
		candidates.extend(
			frappe.db.get_all(
				"Service Package",
				filters={"correction_code": correction_code},
				fields=["name", "applicable_model", "applicable_generation"],
			)
		)
	if title:
		seen = {c["name"] for c in candidates}
		for cand in frappe.db.get_all(
			"Service Package",
			filters={"package_name": title},
			fields=["name", "applicable_model", "applicable_generation"],
		):
			if cand["name"] not in seen:
				candidates.append(cand)
				seen.add(cand["name"])

	for cand in candidates:
		if _matches_scope(cand):
			return cand["name"]

	return None


def _apply_operations_to_ro(
	ro,
	pkg,
	proc_data: dict[str, Any],
	proc_url: str,
	operations: list[dict[str, str]],
	conflict_policy: str,
	is_supp: int,
	hourly_rate: float,
	labor_hours: float,
	labor_amount: float,
	advisor_notes: str,
) -> dict[str, Any]:
	affected_rows = []
	first_skipped_row = None
	count_added = 0
	count_updated = 0
	count_skipped = 0
	ro_company = getattr(ro, "company", None)

	for op in operations:
		op_side = op["side"]
		op_title = op["title"]
		op_code = op["code"]
		existing_service = _find_matching_ro_service(ro, op_code, op_side, pkg.name if pkg else None)

		if existing_service and conflict_policy == "error":
			code_display = op_code or pkg.name or "this procedure"
			side_str = f" ({op_side})" if op_side and op_side != "None" else ""
			frappe.throw(
				frappe._("Correction code {0}{1} already exists on Repair Order {2}.").format(
					code_display, side_str, ro.name
				),
				frappe.DuplicateEntryError,
			)

		if existing_service and conflict_policy == "skip":
			count_skipped += 1
			if first_skipped_row is None:
				first_skipped_row = existing_service
			continue

		row_fields = build_service_package_row_fields(
			pkg,
			company=ro_company,
			cur_side=op_side,
			cur_title=op_title,
			cur_code=op_code,
			default_labor_rate=hourly_rate,
			advisor_notes=advisor_notes,
			is_supplement=is_supp,
		)
		model_val = getattr(pkg, "applicable_model", None) or proc_data.get("applicable_model", "")
		gen_val = getattr(pkg, "applicable_generation", None) or proc_data.get("applicable_generation", "")
		mobile_val = cint(proc_data.get("is_mobile_capable", getattr(pkg, "is_mobile_capable", 0)))
		parts_total = flt(row_fields.get("parts_total", 0.0), 2)
		row_fields.update({
			"applicable_model": model_val,
			"applicable_generation": gen_val,
			"labor_hours": labor_hours,
			"labor_rate": hourly_rate,
			"labor_amount": labor_amount,
			"total_amount": flt(labor_amount + parts_total, 2),
			"tesla_manual_url": proc_url or row_fields.get("tesla_manual_url") or "",
			"is_mobile_capable": mobile_val,
		})

		if existing_service and conflict_policy == "overwrite":
			count_updated += 1
			existing_service.service_title = row_fields["service_title"]
			existing_service.service_position = row_fields["service_position"]
			existing_service.correction_code = row_fields["correction_code"]
			existing_service.labor_hours = labor_hours
			existing_service.labor_rate = hourly_rate
			existing_service.labor_amount = labor_amount
			parsed_parts = json.loads(row_fields["parts"]) if row_fields.get("parts") else []
			if parsed_parts:
				existing_service.parts = row_fields["parts"]
				existing_service.parts_total = parts_total
			existing_service.total_amount = flt(labor_amount + flt(existing_service.parts_total), 2)
			existing_service.tesla_manual_url = row_fields["tesla_manual_url"]
			existing_service.applicable_model = model_val
			existing_service.applicable_generation = gen_val
			existing_service.is_mobile_capable = mobile_val
			if advisor_notes:
				existing_service.advisor_notes = advisor_notes
			affected_rows.append(existing_service)
		else:
			count_added += 1
			target_service = ro.append("services", row_fields)
			affected_rows.append(target_service)

	if count_added > 0:
		action = "added"
	elif count_updated > 0:
		action = "updated"
	else:
		action = "skipped"

	return {
		"action": action,
		"mutated": (count_added + count_updated) > 0,
		"affected_rows": affected_rows,
		"first_skipped_row": first_skipped_row,
		"count_added": count_added,
		"count_updated": count_updated,
		"count_skipped": count_skipped,
	}


def _extract_parts_for_side(pkg, side: str = "Both", company: Optional[str] = None):
	"""Extract nested parts list from Service Package matching specified side."""
	parts_list = []
	parts_total = 0.0
	if not pkg or not getattr(pkg, "items", None):
		return parts_list, parts_total

	warehouse = get_default_warehouse(company)
	for row in pkg.items:
		row_side = getattr(row, "side", None) or "Both"
		if side in ("Both", "None") or row_side in ("Both", side):
			item_doc = frappe.get_cached_doc("Item", row.item_code)
			qty = flt(row.qty) or 1.0
			rate = flt(row.rate) if flt(row.rate) > 0 else flt(getattr(item_doc, "standard_rate", 0))
			amt = flt(qty * rate, 2)
			parts_total += amt
			parts_list.append({
				"item_code": row.item_code,
				"item_name": row.item_name or item_doc.item_name,
				"qty": qty,
				"uom": getattr(row, "uom", None) or item_doc.stock_uom or "Nos",
				"rate": rate,
				"amount": amt,
				"warehouse": warehouse,
			})

	return parts_list, parts_total


def _upsert_service_package(proc_data, proc_url=""):
	"""Upsert a Service Package and its catalog parts using the unified procedure schema."""
	normalized = normalize_tesla_procedure(proc_data, url=proc_url or "")
	correction_code = normalized["correction_code"]
	title = normalized["title"]
	frt = normalized["frt"]
	model = normalized["applicable_model"]
	generation = normalized["applicable_generation"]
	parts = normalized["parts"]

	existing_pkg_name = _find_existing_service_package_name(
		correction_code,
		title,
		applicable_model=model,
		applicable_generation=generation,
		doc_guid=normalized.get("doc_guid") or "",
		tesla_manual_url=normalized.get("tesla_manual_url") or proc_url or "",
	)

	if existing_pkg_name:
		frappe.has_permission("Service Package", "write", throw=True)
		pkg = frappe.get_doc("Service Package", existing_pkg_name)
		pkg.check_permission("write")
		if correction_code and pkg.package_name and pkg.package_name.startswith(correction_code):
			pkg.package_name = title
		if frt > 0:
			pkg.standard_labor_hours = frt
		if normalized["tesla_manual_url"]:
			pkg.tesla_manual_url = normalized["tesla_manual_url"]
		if model:
			pkg.applicable_model = model
		if generation:
			pkg.applicable_generation = generation
		if normalized["doc_guid"]:
			pkg.doc_guid = normalized["doc_guid"]
		pkg.is_mobile_capable = cint(normalized["is_mobile_capable"])
		pkg.is_symmetrical = cint(normalized["is_symmetrical"])
		if normalized["rh_correction_code"]:
			pkg.rh_correction_code = normalized["rh_correction_code"]
		if normalized["category"] and pkg.meta.has_field("category"):
			pkg.category = normalized["category"]
		if normalized["subcategory"] and pkg.meta.has_field("subcategory"):
			pkg.subcategory = normalized["subcategory"]
		_sync_service_package_parts(pkg, parts)
		pkg.save()
		return pkg

	frappe.has_permission("Service Package", "create", throw=True)
	doc_fields = {
		"doctype": "Service Package",
		"package_name": title,
		"applicable_model": model,
		"applicable_generation": generation,
		"correction_code": correction_code,
		"standard_labor_hours": frt or 1.0,
		"is_flat_rate": 0,
		"is_mobile_capable": cint(normalized["is_mobile_capable"]),
		"is_symmetrical": cint(normalized["is_symmetrical"]),
		"rh_correction_code": normalized["rh_correction_code"],
		"tesla_manual_url": normalized["tesla_manual_url"],
		"doc_guid": normalized["doc_guid"],
	}
	pkg = frappe.get_doc(doc_fields)
	if normalized["category"] and pkg.meta.has_field("category"):
		pkg.category = normalized["category"]
	if normalized["subcategory"] and pkg.meta.has_field("subcategory"):
		pkg.subcategory = normalized["subcategory"]
	_sync_service_package_parts(pkg, parts)
	pkg.insert()
	return pkg


def _sync_service_package_parts(pkg, parts: list[dict[str, Any]]) -> None:
	"""Persist canonical normalized parts onto Service Package.items without duplicate rows."""
	if not parts:
		return

	existing_by_key = {}
	for row in (pkg.items or []):
		existing_by_key[(row.item_code, row.side or "Both")] = row

	next_slot = max((cint(getattr(r, "slot_number", 0)) for r in (pkg.items or [])), default=0) + 1
	for part in parts:
		part_number = part["part_number"]
		oem_pn = part.get("oem_part_number") or part_number
		item_code = _resolve_or_create_part_item(part_number, oem_pn, part)
		side = part.get("side") or "Both"
		qty = flt(part.get("qty") or 1.0)
		rate = flt(part.get("rate") or 0.0, 2)
		if rate <= 0:
			rate = flt(frappe.db.get_value("Item", item_code, "standard_rate"), 2)

		existing_row = existing_by_key.get((item_code, side))
		if existing_row:
			existing_row.qty = qty
			if rate > 0:
				existing_row.rate = rate
		else:
			row = pkg.append("items", {
				"slot_number": next_slot,
				"item_code": item_code,
				"qty": qty,
				"rate": rate,
				"side": side,
				"is_default": 1,
			})
			existing_by_key[(item_code, side)] = row
			next_slot += 1


def _resolve_or_create_part_item(part_number: str, oem_pn: str, part: dict[str, Any]) -> str:
	"""Find an existing ERPNext Item by item_code or OEM part number, or create via quick_create_part."""
	if frappe.db.exists("Item", part_number):
		return part_number

	if oem_pn and frappe.db.has_column("Item", "oem_part_number"):
		existing = frappe.db.get_value("Item", {"oem_part_number": oem_pn}, "name")
		if existing:
			return existing

	created = quick_create_part(
		part_number=part_number,
		part_name=part.get("part_name") or part_number,
		part_source=part.get("part_source") or "OEM",
		part_condition=part.get("part_condition") or "New",
		oem_part_number=oem_pn,
		unit_rate=flt(part.get("rate") or 0.0, 2),
		side=part.get("side") or "Both",
	)
	if isinstance(created, dict):
		return created.get("name") or created.get("item_code") or part_number
	return getattr(created, "name", None) or part_number
