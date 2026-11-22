# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_to_date, get_url, now_datetime

from ism.api._validation import (
	coerce_bool,
	optional_int,
	optional_str,
	parse_json_arg,
	require_str,
)
from ism.api.agent_quotes import _get_ro_doc
from ism.api.agent_serializers import (
	build_agent_envelope,
	build_inspection_summary,
	build_ro_urls,
	build_vehicle_intel_summary,
	build_vin_decode_summary,
)
from ism.api.inspection import (
	apply_inspection_template as _apply_inspection_template_impl,
	get_check_in_inspection as _get_check_in_inspection_impl,
	get_inspection_template as _get_inspection_template_impl,
	list_inspection_templates as _list_inspection_templates_impl,
	record_check_in_inspection as _record_check_in_inspection_impl,
)
from ism.api.tesla import (
	decode_vehicle_vin as _decode_vehicle_vin_impl,
	get_vehicle_intelligence as _get_vehicle_intelligence_impl,
	link_vehicle_customer as _link_vehicle_customer_impl,
	search_vehicles as _search_vehicles_impl,
)
from ism.ism.doctype.shop_action_item.shop_action_item import (
	create_shop_action_item as _create_shop_action_item_impl,
	resolve_action_item as _resolve_action_item_impl,
)


def _action_item_urls(
	name: str, reference_doctype: Optional[str] = None, reference_name: Optional[str] = None
) -> Dict[str, str]:
	desk_url = f"{get_url()}/app/shop-action-item/{quote(str(name), safe='')}"
	urls = {"desk": desk_url}
	if reference_doctype == "Repair Order" and reference_name:
		urls.update(build_ro_urls(reference_name))
		urls["desk"] = desk_url
	return urls


def _validate_optional_reference(
	reference_doctype: Optional[str], reference_name: Optional[str]
) -> None:
	"""Validate optional DocType/docname reference pairs and enforce read permission."""
	if reference_name and not reference_doctype:
		frappe.throw(
			_("reference_doctype is required when reference_name is specified."),
			frappe.ValidationError,
		)
	if not reference_doctype:
		return
	if not frappe.db.exists("DocType", reference_doctype):
		frappe.throw(_("Invalid reference_doctype {0}.").format(reference_doctype), frappe.ValidationError)
	frappe.has_permission(reference_doctype, "read", throw=True)
	if reference_name:
		if not frappe.db.exists(reference_doctype, reference_name):
			frappe.throw(
				_("{0} {1} does not exist.").format(reference_doctype, reference_name),
				frappe.DoesNotExistError,
			)
		frappe.has_permission(reference_doctype, "read", doc=reference_name, throw=True)


@frappe.whitelist()
def fetch_tesla_manual(
	url: str,
	repair_order: Optional[str] = None,
	vehicle: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Fetch and parse a Tesla Service Manual procedure URL with optional compatibility check."""
	import ism.api.agent as agent_mod

	frappe.has_permission("Service Package", "read", throw=True)
	url = require_str(url, "URL")
	repair_order = optional_str(repair_order, "Repair Order")
	vehicle = optional_str(vehicle, "Repair Vehicle")
	if repair_order:
		ro = _get_ro_doc(repair_order, ptype="read")
		repair_order = ro.name
	if vehicle:
		if not frappe.db.exists("Repair Vehicle", vehicle):
			frappe.throw(_("Repair Vehicle {0} does not exist.").format(vehicle), frappe.DoesNotExistError)
		frappe.has_permission("Repair Vehicle", "read", doc=vehicle, throw=True)
	parsed = agent_mod.fetch_and_parse_tesla_procedure(
		url, repair_order=repair_order, vehicle=vehicle
	)
	urls: Dict[str, str] = {"manual": url}
	if repair_order:
		urls.update(build_ro_urls(repair_order))
	elif vehicle:
		urls["vehicle"] = f"{get_url()}/app/repair-vehicle/{quote(vehicle, safe='')}"

	def _build_manual_summary() -> str:
		title = parsed.get("title", "Unknown") if isinstance(parsed, dict) else "Parsed"
		lines = [
			"### Tesla Service Manual Procedure",
			f"- **Title**: `{title}`",
			f"- **URL**: {url}",
		]
		compat = parsed.get("compatibility") if isinstance(parsed, dict) else None
		if isinstance(compat, dict) and compat.get("warning"):
			lines.append(f"- **Compatibility Warning**: {compat['warning']}")
		return "\n".join(lines)

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=parsed if isinstance(parsed, dict) else {"result": parsed},
		message=_("Parsed Tesla Service Manual procedure."),
		include_summary=include_summary,
		summary_builder=_build_manual_summary,
	)


@frappe.whitelist()
def list_shop_action_items(
	status: Optional[str] = "Open",
	reference_doctype: Optional[str] = None,
	reference_name: Optional[str] = None,
	category: Optional[str] = None,
	limit: int = 20,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""List Shop Action Items filtered by status, reference document, or category."""
	frappe.has_permission("Shop Action Item", "read", throw=True)
	status = optional_str(status, "Status")
	reference_doctype = optional_str(reference_doctype, "Reference DocType")
	reference_name = optional_str(reference_name, "Reference Name")
	category = optional_str(category, "Category")
	safe_limit = optional_int(limit, "Limit", min_val=1, max_val=100, default=20)
	_validate_optional_reference(reference_doctype, reference_name)

	filters: Dict[str, Any] = {}
	if status and status.lower() != "all":
		filters["status"] = status
	if reference_doctype:
		filters["reference_doctype"] = reference_doctype
	if reference_name:
		filters["reference_name"] = reference_name
	if category:
		filters["category"] = category

	items = frappe.get_list(
		"Shop Action Item",
		filters=filters,
		fields=[
			"name",
			"title",
			"category",
			"priority",
			"status",
			"source",
			"dedup_key",
			"snoozed_until",
			"reference_doctype",
			"reference_name",
			"item_code",
			"details",
			"reported_by",
			"resolved_by",
			"resolved_at",
			"creation",
			"modified",
		],
		order_by="creation desc",
		limit_page_length=safe_limit,
	)
	return build_agent_envelope(
		success=True,
		urls={"list": f"{get_url()}/app/shop-action-item"},
		data={"count": len(items), "items": items},
		message=_("Retrieved {0} Shop Action Item(s).").format(len(items)),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Shop Action Items ({len(items)})\n"
			+ (
				"\n".join(
					f"- `{it['name']}` [{it['priority']}/{it['status']}] {it['title']}"
					for it in items
				)
				or "_No action items match filter._"
			)
		),
	)


@frappe.whitelist()
def create_shop_action_item(
	title: str,
	category: str = "Shop Floor Discrepancy",
	priority: str = "High",
	reference_doctype: Optional[str] = None,
	reference_name: Optional[str] = None,
	item_code: Optional[str] = None,
	details: Optional[str] = None,
	dedup_key: Optional[str] = None,
	source: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Create a Shop Action Item escalation for shop floor discrepancies, shortages, or safety flags."""
	frappe.has_permission("Shop Action Item", "create", throw=True)
	title = require_str(title, "title")
	category = optional_str(category, "Category") or "Shop Floor Discrepancy"
	priority = optional_str(priority, "Priority") or "High"
	reference_doctype = optional_str(reference_doctype, "Reference DocType")
	reference_name = optional_str(reference_name, "Reference Name")
	item_code = optional_str(item_code, "Item Code")
	details = optional_str(details, "Details")
	dedup_key = optional_str(dedup_key, "Deduplication Key")
	source = optional_str(source, "Source")
	_validate_optional_reference(reference_doctype, reference_name)
	if item_code:
		if not frappe.db.exists("Item", item_code):
			frappe.throw(_("Item {0} does not exist.").format(item_code), frappe.DoesNotExistError)
		frappe.has_permission("Item", "read", doc=item_code, throw=True)

	name = _create_shop_action_item_impl(
		title=title,
		category=category,
		priority=priority,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		item_code=item_code,
		details=details,
		dedup_key=dedup_key,
		source=source,
	)
	doc = frappe.get_doc("Shop Action Item", name)
	doc.check_permission("read")
	return build_agent_envelope(
		success=True,
		urls=_action_item_urls(doc.name, doc.reference_doctype, doc.reference_name),
		data={
			"name": doc.name,
			"title": doc.title,
			"category": doc.category,
			"priority": doc.priority,
			"status": doc.status,
			"source": doc.source,
			"dedup_key": doc.dedup_key,
			"reference_doctype": doc.reference_doctype,
			"reference_name": doc.reference_name,
			"item_code": doc.item_code,
		},
		message=_("Created Shop Action Item {0} ({1}).").format(doc.name, doc.priority),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Shop Action Item Created: `{doc.name}`\n"
			f"- **Title**: {doc.title}\n"
			f"- **Priority**: `{doc.priority}`"
		),
	)


@frappe.whitelist()
def sync_action_item_state(
	dedup_key: str,
	action: str,
	snooze_minutes: Optional[int] = None,
	title: Optional[str] = None,
	notes: Optional[str] = None,
	reference_doctype: Optional[str] = None,
	reference_name: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Snooze, dismiss, or reopen a deduplicated Shop Action Item across manager workstations."""
	frappe.has_permission("Shop Action Item", "write", throw=True)
	dedup_key = require_str(dedup_key, "dedup_key")
	action_norm = require_str(action, "action").lower()
	if action_norm not in ("snooze", "dismiss", "reopen"):
		frappe.throw(
			_("Action must be one of: snooze, dismiss, reopen."),
			frappe.ValidationError,
		)
	safe_minutes = optional_int(snooze_minutes, "snooze_minutes", min_val=1, max_val=43200, default=60)
	title = optional_str(title, "title")
	notes = optional_str(notes, "notes")
	reference_doctype = optional_str(reference_doctype, "Reference DocType")
	reference_name = optional_str(reference_name, "Reference Name")
	_validate_optional_reference(reference_doctype, reference_name)

	existing = frappe.get_all(
		"Shop Action Item",
		filters={"dedup_key": dedup_key},
		fields=["name"],
		order_by="creation desc",
		limit_page_length=1,
	)
	if existing:
		doc = frappe.get_doc("Shop Action Item", existing[0]["name"])
		doc.check_permission("write")
	else:
		frappe.has_permission("Shop Action Item", "create", throw=True)
		doc = frappe.new_doc("Shop Action Item")
		doc.dedup_key = dedup_key
		doc.title = title or f"Synced rule {dedup_key}"
		doc.category = "Analytics Threshold"
		doc.priority = "Medium"
		doc.source = "Rule Snooze"
		doc.status = "Open"
		doc.reference_doctype = reference_doctype
		doc.reference_name = reference_name
		doc.reported_by = frappe.session.user

	if action_norm == "snooze":
		if doc.status in ("Resolved", "Dismissed"):
			doc.status = "Open"
		doc.snoozed_until = add_to_date(now_datetime(), minutes=safe_minutes)
		if notes:
			doc.details = notes
	elif action_norm == "dismiss":
		doc.status = "Dismissed"
		doc.snoozed_until = None
		doc.resolution_notes = notes or "Dismissed in cockpit"
		doc.resolved_by = frappe.session.user
		doc.resolved_at = now_datetime()
	else:
		doc.status = "Open"
		doc.snoozed_until = None
		doc.resolved_by = None
		doc.resolved_at = None

	if doc.is_new():
		doc.insert()
	else:
		doc.flags.skip_board_projection = True
		doc.save()
	doc.check_permission("read")

	return build_agent_envelope(
		success=True,
		urls=_action_item_urls(doc.name, doc.reference_doctype, doc.reference_name),
		data={
			"name": doc.name,
			"title": doc.title,
			"status": doc.status,
			"source": doc.source,
			"dedup_key": doc.dedup_key,
			"snoozed_until": str(doc.snoozed_until) if doc.snoozed_until else None,
			"reference_doctype": doc.reference_doctype,
			"reference_name": doc.reference_name,
		},
		message=_("Action item state '{0}' synced for {1}.").format(action_norm, doc.dedup_key),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Action Item State Synced: `{doc.name}`\n"
			f"- **Dedup Key**: `{doc.dedup_key}`\n"
			f"- **Status**: `{doc.status}`"
		),
	)


@frappe.whitelist()
def resolve_shop_action_item(
	action_item: str,
	resolution_notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Resolve an open Shop Action Item with resolution notes."""
	action_item = require_str(action_item, "Shop Action Item")
	resolution_notes = optional_str(resolution_notes, "Resolution Notes")
	frappe.has_permission("Shop Action Item", "write", throw=True)
	if not frappe.db.exists("Shop Action Item", action_item):
		frappe.throw(
			_("Shop Action Item {0} does not exist.").format(action_item),
			frappe.DoesNotExistError,
		)
	frappe.has_permission("Shop Action Item", "write", doc=action_item, throw=True)

	_resolve_action_item_impl(name=action_item, resolution_notes=resolution_notes)
	doc = frappe.get_doc("Shop Action Item", action_item)
	doc.check_permission("read")
	return build_agent_envelope(
		success=True,
		urls=_action_item_urls(doc.name, doc.reference_doctype, doc.reference_name),
		data={
			"name": doc.name,
			"title": doc.title,
			"status": doc.status,
			"resolution_notes": doc.resolution_notes,
			"resolved_by": doc.resolved_by,
			"resolved_at": doc.resolved_at,
		},
		message=_("Resolved Shop Action Item {0}.").format(doc.name),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Resolved Shop Action Item: `{doc.name}`\n"
			f"- **Notes**: {doc.resolution_notes or 'None'}"
		),
	)


@frappe.whitelist()
def decode_vehicle_vin(vin: str, include_summary: Any = False) -> Dict[str, Any]:
	"""Decode a 17-character Tesla VIN offline and return specs and manual URL."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	vin = optional_str(vin, "VIN") or ""
	decoded = _decode_vehicle_vin_impl(vin=vin)
	has_error = bool(decoded.get("error"))
	urls: Dict[str, str] = {}
	if decoded.get("recommended_manual_url"):
		urls["manual"] = decoded["recommended_manual_url"]
	norm_vin = decoded.get("vin")
	if norm_vin and frappe.db.exists("Repair Vehicle", norm_vin):
		urls["desk"] = f"{get_url()}/app/repair-vehicle/{quote(str(norm_vin), safe='')}"

	msg = (
		decoded["error"]
		if has_error
		else _("Decoded Tesla VIN {0} ({1} {2}).").format(
			norm_vin, decoded.get("model_year") or "", decoded.get("model") or ""
		).strip()
	)
	return build_agent_envelope(
		success=not has_error,
		urls=urls,
		data=decoded,
		message=msg,
		error=decoded.get("error"),
		include_summary=include_summary,
		summary_builder=lambda: build_vin_decode_summary(decoded),
	)


@frappe.whitelist()
def search_vehicles(
	query: Optional[str] = None,
	customer: Optional[str] = None,
	model: Optional[str] = None,
	limit: int = 20,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Search Repair Vehicles by VIN, license plate, model, or linked customer."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	query = optional_str(query, "Query")
	customer = optional_str(customer, "Customer")
	model = optional_str(model, "Model")
	safe_limit = optional_int(limit, "Limit", min_val=1, max_val=100, default=20)
	if customer:
		frappe.has_permission("Customer", "read", throw=True)
		if frappe.db.exists("Customer", customer):
			frappe.has_permission("Customer", "read", doc=customer, throw=True)
	vehicles = _search_vehicles_impl(
		query=query, customer=customer, model=model, limit=safe_limit
	) or []
	return build_agent_envelope(
		success=True,
		urls={"list": f"{get_url()}/app/repair-vehicle"},
		data={"count": len(vehicles), "vehicles": vehicles},
		message=_("Found {0} matching vehicle(s).").format(len(vehicles)),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Vehicle Search ({len(vehicles)} match(es))\n"
			+ (
				"\n".join(
					f"- `{v.get('name')}` | {v.get('model_year') or ''} {v.get('model') or ''} "
					f"| Plate: `{v.get('license_plate') or '-'}` "
					f"| Owner: `{v.get('customer_name') or v.get('customer') or '-'}`"
					for v in vehicles[:15]
				)
				or "_No matching vehicles found._"
			)
		),
	)


@frappe.whitelist()
def get_vehicle_intelligence(
	vehicle: str,
	history_limit: int = 20,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return decoded VIN specs, owners, Repair Order history, and open deferred services for a vehicle."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	frappe.has_permission("Repair Order", "read", throw=True)
	vehicle = require_str(vehicle, "Repair Vehicle")
	safe_history_limit = optional_int(history_limit, "History Limit", min_val=1, max_val=100, default=20)
	if frappe.db.exists("Repair Vehicle", vehicle):
		frappe.has_permission("Repair Vehicle", "read", doc=vehicle, throw=True)
	intel = _get_vehicle_intelligence_impl(vehicle=vehicle, history_limit=safe_history_limit)
	veh = intel.get("vehicle") or {}
	veh_name = veh.get("name") or vehicle
	if veh_name and frappe.db.exists("Repair Vehicle", veh_name):
		frappe.has_permission("Repair Vehicle", "read", doc=veh_name, throw=True)
	urls: Dict[str, str] = {"desk": f"{get_url()}/app/repair-vehicle/{quote(str(veh_name), safe='')}"}
	if intel.get("recommended_manual_url"):
		urls["manual"] = intel["recommended_manual_url"]

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=intel,
		message=_("Retrieved vehicle intelligence for {0}.").format(veh_name),
		include_summary=include_summary,
		summary_builder=lambda: build_vehicle_intel_summary(intel),
	)


@frappe.whitelist()
def link_vehicle_customer(
	vehicle: str,
	customer: str,
	is_primary: Any = False,
	relationship: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Link or update a primary or secondary customer owner on a Repair Vehicle."""
	frappe.has_permission("Repair Vehicle", "write", throw=True)
	frappe.has_permission("Customer", "read", throw=True)
	vehicle = require_str(vehicle, "Repair Vehicle")
	customer = require_str(customer, "Customer")
	relationship = optional_str(relationship, "Relationship")
	if not frappe.db.exists("Repair Vehicle", vehicle):
		frappe.throw(_("Repair Vehicle {0} does not exist.").format(vehicle), frappe.DoesNotExistError)
	frappe.has_permission("Repair Vehicle", "write", doc=vehicle, throw=True)
	if not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} does not exist.").format(customer), frappe.DoesNotExistError)
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	res = _link_vehicle_customer_impl(
		vehicle=vehicle,
		customer=customer,
		is_primary=coerce_bool(is_primary),
		relationship=relationship,
	)
	veh_name = res.get("vehicle") or vehicle
	urls = {
		"desk": f"{get_url()}/app/repair-vehicle/{quote(str(veh_name), safe='')}",
		"customer": f"{get_url()}/app/customer/{quote(str(customer), safe='')}",
	}
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=res,
		message=_("Linked customer {0} to vehicle {1}.").format(customer, veh_name),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Customer Linked to Vehicle `{veh_name}`\n"
			f"- **Customer**: `{customer}` | **Primary**: `{res.get('primary_customer')}`\n"
			f"- **Total Owners**: `{len(res.get('owners') or [])}`"
		),
	)


@frappe.whitelist()
def list_inspection_templates(
	include_inactive: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""List vehicle check-in inspection templates with item counts."""
	frappe.has_permission("Inspection Template", "read", throw=True)
	res = _list_inspection_templates_impl(include_inactive=coerce_bool(include_inactive))
	templates = res.get("templates") or []
	return build_agent_envelope(
		success=True,
		urls={"list": f"{get_url()}/app/inspection-template"},
		data=res,
		message=_("Found {0} inspection template(s).").format(res.get("count", len(templates))),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Inspection Templates ({len(templates)})\n"
			+ (
				"\n".join(
					f"- `{t.get('name')}` "
					f"({'Active' if t.get('is_active') else 'Inactive'}, {t.get('item_count', 0)} items)"
					for t in templates
				)
				or "_No inspection templates found._"
			)
		),
	)


@frappe.whitelist()
def get_inspection_template(
	template_name: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Retrieve an Inspection Template and its ordered checklist items."""
	frappe.has_permission("Inspection Template", "read", throw=True)
	template_name = optional_str(template_name, "Inspection Template")
	if template_name:
		if not frappe.db.exists("Inspection Template", template_name):
			frappe.throw(
				_("Inspection Template {0} does not exist.").format(template_name),
				frappe.DoesNotExistError,
			)
		frappe.has_permission("Inspection Template", "read", doc=template_name, throw=True)
	res = _get_inspection_template_impl(template_name=template_name)
	resolved_name = res.get("name") or ""
	if resolved_name and frappe.db.exists("Inspection Template", resolved_name):
		frappe.has_permission("Inspection Template", "read", doc=resolved_name, throw=True)
	items = res.get("items") or []
	return build_agent_envelope(
		success=True,
		urls={"desk": f"{get_url()}/app/inspection-template/{quote(str(resolved_name), safe='')}"},
		data=res,
		message=_("Loaded inspection template {0} ({1} item(s)).").format(resolved_name, len(items)),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Inspection Template: `{resolved_name}` ({len(items)} items)\n"
			+ "\n".join(f"- {it.get('idx')}. {it.get('inspection_description')}" for it in items[:15])
		),
	)


@frappe.whitelist()
def apply_inspection_template(
	repair_order: str,
	template_name: Optional[str] = None,
	replace_existing: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Apply an Inspection Template's checklist items onto a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="write")
	repair_order = ro.name
	template_name = optional_str(template_name, "Inspection Template")
	frappe.has_permission("Inspection Template", "read", throw=True)
	if template_name:
		if not frappe.db.exists("Inspection Template", template_name):
			frappe.throw(
				_("Inspection Template {0} does not exist.").format(template_name),
				frappe.DoesNotExistError,
			)
		frappe.has_permission("Inspection Template", "read", doc=template_name, throw=True)
	res = _apply_inspection_template_impl(
		repair_order=repair_order,
		template_name=template_name,
		replace_existing=coerce_bool(replace_existing),
	)
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(repair_order),
		data=res,
		message=_("Applied template {0} to {1} ({2} added, {3} skipped).").format(
			res.get("template_name"),
			repair_order,
			res.get("added_count", 0),
			res.get("skipped_count", 0),
		),
		include_summary=include_summary,
		summary_builder=lambda: build_inspection_summary(res, heading="Inspection Template Applied"),
	)


@frappe.whitelist()
def get_check_in_inspection(
	repair_order: str,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return current check-in inspection items and summary counts for a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="read")
	repair_order = ro.name
	res = _get_check_in_inspection_impl(repair_order=repair_order)
	summary = res.get("summary") or {}
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(repair_order),
		data=res,
		message=_("Inspection for {0}: {1}/{2} items completed.").format(
			repair_order, summary.get("completed", 0), summary.get("total", 0)
		),
		include_summary=include_summary,
		summary_builder=lambda: build_inspection_summary(res),
	)


@frappe.whitelist()
def record_check_in_inspection(
	repair_order: str,
	items: Optional[Union[List[Any], str]] = None,
	odometer_in: Optional[Union[int, float, str]] = None,
	odometer: Optional[Union[int, float, str]] = None,
	customer_concern: Optional[str] = None,
	template_name: Optional[str] = None,
	auto_apply_template: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Record or update check-in intake fields and inspection findings on a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="write")
	repair_order = ro.name
	template_name = optional_str(template_name, "Inspection Template")
	customer_concern = optional_str(customer_concern, "Customer Concern")
	validated_odometer_in = optional_int(odometer_in, "Odometer In", min_val=0)
	validated_odometer = optional_int(odometer, "Odometer", min_val=0)
	parsed_items = (
		parse_json_arg(items, default=None, label="items")
		if isinstance(items, str)
		else items
	)
	auto_apply = coerce_bool(auto_apply_template)
	if auto_apply or template_name:
		frappe.has_permission("Inspection Template", "read", throw=True)
		if template_name:
			if not frappe.db.exists("Inspection Template", template_name):
				frappe.throw(
					_("Inspection Template {0} does not exist.").format(template_name),
					frappe.DoesNotExistError,
				)
			frappe.has_permission("Inspection Template", "read", doc=template_name, throw=True)
	res = _record_check_in_inspection_impl(
		repair_order=repair_order,
		items=parsed_items,
		odometer_in=validated_odometer_in,
		odometer=validated_odometer,
		customer_concern=customer_concern,
		template_name=template_name,
		auto_apply_template=auto_apply,
	)
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(repair_order),
		data=res,
		message=_("Recorded check-in inspection on {0} ({1} updated, {2} added).").format(
			repair_order, res.get("updated_count", 0), res.get("added_count", 0)
		),
		include_summary=include_summary,
		summary_builder=lambda: build_inspection_summary(res, heading="Check-In Inspection Recorded"),
	)
