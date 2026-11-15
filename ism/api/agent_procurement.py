# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, flt, get_url

from ism.api._validation import (
	coerce_bool,
	normalize_condition,
	optional_flt,
	optional_str,
	parse_json_arg,
)
from ism.api.agent_quotes import _get_ro_doc
from ism.api.agent_serializers import (
	build_agent_envelope,
	build_alternates_summary,
	build_catalog_summary,
	build_ro_urls,
	serialize_service_package_compact,
)
from ism.api.parts_catalog import (
	_upsert_part_row_on_package_doc,
	bulk_import_parts,
	get_component_alternates as _get_component_alternates_impl,
	quick_create_part,
)
from ism.api.parts_procurement import (
	create_purchase_invoice as _create_purchase_invoice_impl,
	create_purchase_payment as _create_purchase_payment_impl,
	ensure_supplier_and_link_to_item,
)
from ism.api.parts_stock import _get_ro_parts_stock_internal
from ism.api.tesla import import_tesla_procedure as _import_tesla_ro_procedure


def _attach_package_parts(pkg: Any, parts: List[Any]) -> None:
	"""Attach parsed part definitions onto a Service Package's items child table cleanly."""
	for idx, entry in enumerate(parts, start=1):
		if isinstance(entry, str):
			part_name = entry.strip()
			if not part_name:
				continue
			part_number = ""
			qty = 1.0
			source = "OEM"
			condition = "New"
			explicit_rate: Optional[float] = None
			item_code = None
			slot_num = idx
		elif isinstance(entry, dict):
			part_name = (
				optional_str(entry.get("part_name") or entry.get("item_name") or entry.get("name"), "Part Name")
				or f"Part {idx}"
			)
			part_number = optional_str(entry.get("part_number") or entry.get("oem_part_number"), "Part Number") or ""
			qty = optional_flt(entry.get("qty", 1.0), "Part Qty", positive=True, default=1.0) or 1.0
			source = optional_str(entry.get("part_source") or entry.get("source"), "Part Source") or "OEM"
			condition = normalize_condition(entry.get("part_condition") or entry.get("condition") or "New")
			explicit_rate = optional_flt(entry.get("rate"), "Part Rate", non_negative=True, default=None)
			slot_num = cint(entry.get("slot_number")) if entry.get("slot_number") is not None else idx
			item_code = optional_str(entry.get("item_code"), "Item Code")
		else:
			frappe.throw(_("Each procedure part entry must be a string or dictionary."), frappe.ValidationError)

		if not item_code:
			frappe.has_permission("Item", "create", throw=True)
			created = quick_create_part(
				part_name=part_name,
				part_number=part_number or part_name,
				part_source=source,
				part_condition=condition,
				oem_part_number=part_number or None,
				unit_rate=explicit_rate or 0.0,
			)
			item_code = created.get("item_code") if isinstance(created, dict) else created
		else:
			if not frappe.db.exists("Item", item_code):
				frappe.throw(_("Catalog Item {0} does not exist.").format(item_code), frappe.DoesNotExistError)

		item_doc = frappe.get_cached_doc("Item", item_code)
		item_doc.check_permission("read")
		resolved_rate = explicit_rate if explicit_rate is not None else flt(item_doc.standard_rate)
		_upsert_part_row_on_package_doc(
			pkg,
			item_doc,
			slot_number=slot_num,
			is_default=1 if idx == 1 or slot_num > 0 else 0,
			qty=qty,
			rate=resolved_rate,
			side="Both",
		)


def _collect_required_part_qtys(repair_order: Optional[str], ro_doc: Any = None) -> Dict[str, float]:
	"""Sum required part quantities per item_code across a Repair Order's services."""
	req: Dict[str, float] = {}
	if ro_doc is None:
		repair_order = optional_str(repair_order, "Repair Order")
		if not repair_order or not frappe.db.exists("Repair Order", repair_order):
			return req
		ro_doc = _get_ro_doc(repair_order, ptype="read")
	for svc in ro_doc.services or []:
		parts = parse_json_arg(svc.parts, [], label="Service Parts") or []
		if not isinstance(parts, list):
			continue
		for p in parts:
			if isinstance(p, dict) and p.get("item_code"):
				code = str(p["item_code"]).strip()
				req[code] = req.get(code, 0.0) + flt(p.get("qty", 1.0))
	return req


def _build_procurement_urls(
	ro_name: Optional[str] = None,
	purchase_orders: Optional[List[str]] = None,
	rfqs: Optional[List[str]] = None,
	payment_entries: Optional[List[str]] = None,
	purchase_invoices: Optional[List[str]] = None,
	purchase_receipt: Optional[str] = None,
	primary: Optional[tuple[str, str]] = None,
) -> Dict[str, Any]:
	"""Build canonical Desk URLs for procurement, invoicing, payment, and receipt documents."""
	base_url = get_url()
	urls: Dict[str, str] = build_ro_urls(ro_name) if ro_name else {}
	if primary and "desk" not in urls:
		urls["desk"] = f"{base_url}/app/{primary[0]}/{quote(str(primary[1]), safe='')}"

	po_list = purchase_orders or []
	if len(po_list) == 1:
		urls["purchase_order"] = f"{base_url}/app/purchase-order/{quote(str(po_list[0]), safe='')}"
		urls.setdefault("desk", urls["purchase_order"])
	if po_list:
		urls["purchase_orders"] = [
			f"{base_url}/app/purchase-order/{quote(str(name), safe='')}" for name in po_list
		]

	rfq_list = rfqs or []
	if len(rfq_list) == 1:
		urls["request_for_quotation"] = (
			f"{base_url}/app/request-for-quotation/{quote(str(rfq_list[0]), safe='')}"
		)
		urls.setdefault("desk", urls["request_for_quotation"])
	if rfq_list:
		urls["rfqs"] = [
			f"{base_url}/app/request-for-quotation/{quote(str(name), safe='')}" for name in rfq_list
		]

	pe_list = payment_entries or []
	if len(pe_list) == 1:
		urls["payment_entry"] = f"{base_url}/app/payment-entry/{quote(str(pe_list[0]), safe='')}"
		urls.setdefault("desk", urls["payment_entry"])
	if pe_list:
		urls["payment_entries"] = [
			f"{base_url}/app/payment-entry/{quote(str(name), safe='')}" for name in pe_list
		]

	pi_list = purchase_invoices or []
	if len(pi_list) == 1:
		urls["purchase_invoice"] = f"{base_url}/app/purchase-invoice/{quote(str(pi_list[0]), safe='')}"
		urls.setdefault("desk", urls["purchase_invoice"])
	if pi_list:
		urls["purchase_invoices"] = [
			f"{base_url}/app/purchase-invoice/{quote(str(name), safe='')}" for name in pi_list
		]

	if purchase_receipt:
		urls["purchase_receipt"] = f"{base_url}/app/purchase-receipt/{quote(str(purchase_receipt), safe='')}"
		urls.setdefault("desk", urls["purchase_receipt"])
	return urls


@frappe.whitelist()
def import_supplier_catalog(
	items: Optional[Union[List[Dict[str, Any]], Dict[str, Any], str]] = None,
	raw_text: Optional[str] = None,
	supplier: Optional[str] = None,
	default_source: str = "OEM",
	default_condition: str = "New",
	service_package: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Bulk ingest structured catalog items (primary) or CSV/tabular text into Item catalog."""
	frappe.has_permission("Item", "create", throw=True)
	frappe.has_permission("Item", "write", throw=True)
	supplier = optional_str(supplier, "Supplier")
	service_package = optional_str(service_package, "Service Package")
	default_source = optional_str(default_source, "Default Source") or "OEM"
	default_condition = optional_str(default_condition, "Default Condition") or "New"
	if supplier:
		if frappe.db.exists("Supplier", supplier):
			frappe.has_permission("Supplier", "read", doc=supplier, throw=True)
		else:
			frappe.has_permission("Supplier", "create", throw=True)
	if service_package:
		frappe.has_permission("Service Package", "read", throw=True)
		if not frappe.db.exists("Service Package", service_package):
			frappe.throw(_("Service Package {0} does not exist.").format(service_package), frappe.DoesNotExistError)
		frappe.has_permission("Service Package", "write", doc=service_package, throw=True)

	has_items = (
		items is not None
		and items != []
		and items != {}
		and (not isinstance(items, str) or bool(items.strip()))
	)
	has_raw = bool(raw_text and str(raw_text).strip())
	if not has_items and not has_raw:
		frappe.throw(_("Structured catalog items or CSV/tabular content is required."), frappe.ValidationError)

	normalized_cond = normalize_condition(default_condition)
	result = bulk_import_parts(
		items=items,
		raw_text=raw_text,
		service_package=service_package,
		default_source=default_source,
		default_condition=normalized_cond,
		supplier=supplier,
	)

	created = result.get("created", [])
	updated = result.get("updated", [])
	errors = result.get("errors", [])

	if supplier:
		for item_entry in created + updated:
			if isinstance(item_entry, dict) and item_entry.get("supplier"):
				continue
			item_code = item_entry.get("item_code") if isinstance(item_entry, dict) else item_entry
			if item_code and frappe.db.exists("Item", item_code):
				ensure_supplier_and_link_to_item(supplier_name=supplier, item_code=item_code)

	formatted_errors = [
		(err.get("error") or str(err)) if isinstance(err, dict) else str(err)
		for err in errors
	]

	data = {
		"supplier": supplier,
		"created": created,
		"updated": updated,
		"errors": formatted_errors,
		"total_created": len(created),
		"total_updated": len(updated),
		"total_errors": len(formatted_errors),
	}

	return build_agent_envelope(
		success=True,
		data=data,
		message=_("Ingested {0} parts successfully.").format(len(created) + len(updated)),
		include_summary=include_summary,
		summary_builder=lambda: build_catalog_summary(supplier, created, updated, formatted_errors),
	)


@frappe.whitelist()
def get_component_alternates(
	service_package: Optional[str] = None,
	component_slot: Optional[str] = None,
	slot_number: Optional[int] = None,
	oem_part_number: Optional[str] = None,
	item_code: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return compatible OEM, aftermarket, and reconditioned alternates for a component slot or part."""
	frappe.has_permission("Item", "read", throw=True)
	service_package = optional_str(service_package, "Service Package")
	component_slot = optional_str(component_slot, "Component Slot")
	oem_part_number = optional_str(oem_part_number, "OEM Part Number")
	item_code = optional_str(item_code, "Item Code")
	if service_package:
		frappe.has_permission("Service Package", "read", throw=True)
		if frappe.db.exists("Service Package", service_package):
			frappe.has_permission("Service Package", "read", doc=service_package, throw=True)
	if item_code and frappe.db.exists("Item", item_code):
		frappe.has_permission("Item", "read", doc=item_code, throw=True)

	alternates = (
		_get_component_alternates_impl(
			service_package=service_package,
			component_slot=component_slot,
			slot_number=slot_number,
			oem_part_number=oem_part_number,
			item_code=item_code,
		)
		or []
	)

	urls: Dict[str, str] = {}
	if service_package and frappe.db.exists("Service Package", service_package):
		urls["desk"] = f"{get_url()}/app/service-package/{quote(str(service_package), safe='')}"
	elif item_code and frappe.db.exists("Item", item_code):
		urls["desk"] = f"{get_url()}/app/item/{quote(str(item_code), safe='')}"

	parsed_slot_num = cint(slot_number) if slot_number is not None and str(slot_number).strip() != "" else None
	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"service_package": service_package,
			"component_slot": component_slot,
			"slot_number": parsed_slot_num,
			"oem_part_number": oem_part_number,
			"item_code": item_code,
			"count": len(alternates),
			"alternates": alternates,
		},
		message=_("Found {0} component alternate(s).").format(len(alternates)),
		include_summary=include_summary,
		summary_builder=lambda: build_alternates_summary(
			alternates,
			service_package=service_package,
			oem_part_number=oem_part_number,
			item_code=item_code,
		),
	)


def _coerce_procedure_parts_arg(parts: Any) -> List[Any]:
	if parts is None:
		return []
	if isinstance(parts, list):
		return parts
	if isinstance(parts, dict):
		return [parts]
	if isinstance(parts, str):
		stripped = parts.strip()
		if not stripped:
			return []
		parsed = parse_json_arg(stripped, [], label="parts")
		if isinstance(parsed, list):
			return parsed
		if isinstance(parsed, dict):
			return [parsed]
		if isinstance(parsed, str):
			return [parsed]
		frappe.throw(_("Procedure parts must be a list."), frappe.ValidationError)
	frappe.throw(_("Procedure parts must be a list."), frappe.ValidationError)


@frappe.whitelist()
def import_tesla_procedure(
	procedure_title: Optional[str] = None,
	correction_code: Optional[str] = None,
	labor_hours: float = 1.0,
	model: str = "Model 3",
	parts: Optional[Union[List[Any], str]] = None,
	repair_order: Optional[str] = None,
	url: Optional[str] = None,
	data: Optional[Union[Dict[str, Any], str]] = None,
	on_conflict: str = "skip",
	force: Any = False,
	side: Optional[str] = None,
	is_supplement: Any = 0,
	advisor_notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Ingest a Tesla procedure into a Service Package (or directly onto a Repair Order)."""
	import ism.api.agent as agent_mod

	repair_order = optional_str(repair_order, "Repair Order")
	procedure_title = optional_str(procedure_title, "Procedure Title")
	correction_code = optional_str(correction_code, "Correction Code")
	url = optional_str(url, "URL")
	side = optional_str(side, "Side")
	advisor_notes = optional_str(advisor_notes, "Advisor Notes")
	model = optional_str(model, "Model") or "Model 3"
	on_conflict = optional_str(on_conflict, "On Conflict") or "skip"
	labor_hours_val = optional_flt(labor_hours, "Labor Hours", non_negative=True, default=1.0)
	parsed_data = parse_json_arg(data, None, label="data") if isinstance(data, str) else data
	if parsed_data is not None and not isinstance(parsed_data, dict):
		frappe.throw(_("Procedure data must be a JSON object."), frappe.ValidationError)

	if repair_order:
		_get_ro_doc(repair_order, ptype="write")
		frappe.has_permission("Service Package", "create", throw=True)
		if parsed_data is None and not url and (procedure_title or correction_code):
			parsed_parts = _coerce_procedure_parts_arg(parts)
			parsed_data = {
				"title": procedure_title or correction_code,
				"correction_code": correction_code or "",
				"frt": flt(labor_hours_val),
				"applicable_model": model,
				"parts": parsed_parts,
			}
		ro_res = _import_tesla_ro_procedure(
			repair_order=repair_order,
			url=url,
			data=parsed_data,
			on_conflict=on_conflict,
			force=coerce_bool(force),
			side=side,
			is_supplement=1 if coerce_bool(is_supplement) else 0,
			advisor_notes=advisor_notes,
		)
		urls = build_ro_urls(repair_order)
		if isinstance(ro_res, dict) and ro_res.get("service_package"):
			urls["service_package"] = (
				f"{get_url()}/app/service-package/{quote(str(ro_res['service_package']), safe='')}"
			)
		return build_agent_envelope(
			success=True,
			urls=urls,
			data=ro_res if isinstance(ro_res, dict) else {"result": ro_res},
			message=(
				ro_res.get("message")
				if isinstance(ro_res, dict) and ro_res.get("message")
				else _("Tesla procedure imported onto Repair Order {0}.").format(repair_order)
			),
			include_summary=include_summary,
			summary_builder=lambda: (
				f"### Tesla Procedure Imported onto `{repair_order}`\n"
				f"- **Action**: `{ro_res.get('action')}` | **Policy**: `{ro_res.get('conflict_policy')}`\n"
				f"- **Service Package**: `{ro_res.get('service_package')}` | **Correction Code**: `{ro_res.get('correction_code') or 'N/A'}`\n"
				f"- **FRT**: `{flt(ro_res.get('frt', 0)):.2f} hrs`"
				if isinstance(ro_res, dict)
				else f"Tesla procedure imported onto {repair_order}."
			),
		)

	if not (
		frappe.has_permission("Service Package", "create") or frappe.has_permission("Service Package", "write")
	):
		frappe.has_permission("Service Package", "create", throw=True)

	if not procedure_title and not parsed_data and url:
		parsed_data = agent_mod.fetch_and_parse_tesla_procedure(url)

	if isinstance(parsed_data, dict):
		procedure_title = procedure_title or optional_str(
			parsed_data.get("title")
			or parsed_data.get("procedure_title")
			or parsed_data.get("procedure_name"),
			"Procedure Title",
		)
		correction_code = correction_code or optional_str(parsed_data.get("correction_code"), "Correction Code")
		if parsed_data.get("frt") is not None and flt(labor_hours_val) == 1.0:
			labor_hours_val = flt(parsed_data.get("frt"))
		model = parsed_data.get("applicable_model") or parsed_data.get("model") or model
		if parts is None and parsed_data.get("parts") is not None:
			parts = parsed_data.get("parts")

	if not procedure_title:
		frappe.throw(_("procedure_title is required when importing a Service Package."), frappe.MandatoryError)

	parsed_parts = _coerce_procedure_parts_arg(parts)

	existing_name = (
		(correction_code and frappe.db.get_value("Service Package", {"correction_code": correction_code}, "name"))
		or frappe.db.get_value("Service Package", {"package_name": procedure_title}, "name")
		or frappe.db.get_value("Service Package", {"name": procedure_title}, "name")
	)

	if existing_name:
		pkg = frappe.get_doc("Service Package", existing_name)
		pkg.check_permission("write")
	else:
		frappe.has_permission("Service Package", "create", throw=True)
		pkg = frappe.new_doc("Service Package")
		pkg.package_name = procedure_title
		pkg.check_permission("create")

	pkg.applicable_model = model
	pkg.correction_code = correction_code or ""
	pkg.standard_labor_hours = flt(labor_hours_val)

	if parsed_parts:
		_attach_package_parts(pkg, parsed_parts)

	pkg.save()

	desk_url = f"{get_url()}/app/service-package/{quote(str(pkg.name), safe='')}"
	return build_agent_envelope(
		success=True,
		urls={"desk": desk_url},
		data=serialize_service_package_compact(pkg),
		message=_("Tesla procedure saved as Service Package."),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Tesla Procedure Ingested: `{pkg.name}`\n"
			f"**Correction Code**: `{correction_code or 'N/A'}` | **Model**: `{model}`\n"
			f"**Flat Rate Time**: `{flt(labor_hours_val):.2f} hours` | **Parts**: `{len(pkg.items or [])}`\n"
			f"[Inspect Service Package]({desk_url})"
		),
	)


@frappe.whitelist()
def check_parts_stock(
	repair_order: Optional[str] = None,
	items: Optional[Union[List[Any], str]] = None,
	company: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Check warehouse stock availability and deficits for a Repair Order or item list."""
	repair_order = optional_str(repair_order, "Repair Order")
	company = optional_str(company, "Company")
	frappe.has_permission("Item", "read", throw=True)

	ro = _get_ro_doc(repair_order, ptype="read") if repair_order else None
	parsed_items: Any = items
	if isinstance(items, str):
		stripped = items.strip()
		if stripped.startswith(("[", "{")):
			parsed_items = parse_json_arg(stripped, [], label="Items")
		else:
			parsed_items = [s.strip() for s in stripped.split(",") if s.strip()]

	stock_map = _get_ro_parts_stock_internal(
		ro_name=ro.name if ro else repair_order,
		items=parsed_items,
		company=company,
		ro_doc=ro,
	)
	if not isinstance(stock_map, dict):
		stock_map = {}

	req_qtys = _collect_required_part_qtys(ro.name if ro else None, ro_doc=ro) if ro else {}
	deficit_items: List[str] = []
	for code, info in stock_map.items():
		if not isinstance(info, dict):
			continue
		req_qty = req_qtys.get(code, 1.0)
		if "deficit" in info:
			deficit = max(0.0, flt(info["deficit"]))
		else:
			deficit = max(0.0, flt(req_qty) - flt(info.get("available_qty", 0.0)))
		info["required_qty"] = req_qty
		info["deficit"] = deficit
		if deficit > 0:
			deficit_items.append(code)

	urls = build_ro_urls(ro.name) if ro else {}

	sku_count = len(stock_map)
	sku_word = _("SKU") if sku_count == 1 else _("SKUs")

	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"repair_order": ro.name if ro else repair_order,
			"all_available": len(deficit_items) == 0,
			"deficit_count": len(deficit_items),
			"items": stock_map,
		},
		message=_("Checked stock for {0} unique part {1}.").format(sku_count, sku_word),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Parts Stock Check ({(ro.name if ro else repair_order) or 'Items'})\n"
			f"- **Checked SKUs**: `{sku_count}`\n"
			f"- **Deficits**: `{len(deficit_items)}`"
		),
	)


@frappe.whitelist()
def submit_procurement_orders(
	repair_order: Optional[str] = None,
	parts_data: Union[List[Dict[str, Any]], str, None] = None,
	submit: Any = True,
	company: Optional[str] = None,
	payment_method: Optional[str] = None,
	paid_amount: Optional[float] = None,
	reference_no: Optional[str] = None,
	reference_date: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Create and submit (by default) Purchase Orders and RFQs, standalone or for a Repair Order, with optional PO payment."""
	import ism.api.agent as agent_mod

	clean_ro = optional_str(repair_order, "Repair Order")
	if clean_ro:
		ro = _get_ro_doc(clean_ro, ptype="read")
		clean_ro = ro.name

	clean_company = optional_str(company, "Company")
	pay_method = optional_str(payment_method, "Payment Method")
	ref_no = optional_str(reference_no, "Reference No")
	ref_date = optional_str(reference_date, "Reference Date")
	should_submit = coerce_bool(submit, default=True)
	explicit_paid = optional_flt(paid_amount, "Paid Amount", positive=True, default=None)

	if explicit_paid is not None or ref_no or ref_date:
		if not pay_method:
			frappe.throw(
				_("Payment Method is required when recording payment details with procurement."),
				frappe.MandatoryError,
			)
	if pay_method and not should_submit:
		frappe.throw(
			_("Cannot record a Payment Entry against a Draft Purchase Order. Submit the Purchase Order instead."),
			frappe.ValidationError,
		)

	frappe.has_permission("Purchase Order", "create", throw=True)
	if should_submit:
		frappe.has_permission("Purchase Order", "submit", throw=True)
	if pay_method:
		frappe.has_permission("Payment Entry", "create", throw=True)
		frappe.has_permission("Payment Entry", "submit", throw=True)

	parsed_parts = parse_json_arg(parts_data, [], label="parts_data")
	if not isinstance(parsed_parts, list) or not parsed_parts:
		frappe.throw(_("No parts selected for procurement."), frappe.ValidationError)

	# Enforce single-PO payment constraints upfront BEFORE creating any POs/RFQs.
	if pay_method:
		po_suppliers = {
			optional_str(entry.get("supplier"), "Supplier")
			for entry in parsed_parts
			if isinstance(entry, dict)
			and (optional_str(entry.get("order_mode"), "Order Mode") or "Purchase Order") != "Request for Quotation"
			and optional_str(entry.get("supplier"), "Supplier")
		}
		if not po_suppliers:
			frappe.throw(
				_("No Purchase Orders would be created to apply Payment Method '{0}' against.").format(pay_method),
				frappe.ValidationError,
			)
		if len(po_suppliers) > 1 and explicit_paid is not None:
			frappe.throw(
				_("Cannot apply a single paid_amount across multiple supplier Purchase Orders; run create_purchase_payment per PO."),
				frappe.ValidationError,
			)

	impl_kwargs: Dict[str, Any] = {
		"repair_order": clean_ro,
		"parts_data": parsed_parts,
		"submit": should_submit,
	}
	if clean_company:
		impl_kwargs["company"] = clean_company

	res = agent_mod._submit_procurement_orders_impl(**impl_kwargs)
	created_pos = res.get("purchase_orders", [])
	created_rfqs = res.get("rfqs", [])

	created_payments: List[str] = []
	payment_details: List[Dict[str, Any]] = []
	if pay_method:
		if not created_pos:
			frappe.throw(
				_("No Purchase Orders were created to apply Payment Method '{0}' against.").format(pay_method),
				frappe.ValidationError,
			)
		if len(created_pos) > 1 and explicit_paid is not None:
			frappe.throw(
				_("Cannot apply a single paid_amount across multiple supplier Purchase Orders; run create_purchase_payment per PO."),
				frappe.ValidationError,
			)
		for po_name in created_pos:
			pay_res = _create_purchase_payment_impl(
				purchase_order=po_name,
				payment_method=pay_method,
				paid_amount=explicit_paid,
				reference_no=ref_no,
				reference_date=ref_date,
			)
			created_payments.append(pay_res["payment_entry"])
			payment_details.append(pay_res)

	urls = _build_procurement_urls(
		ro_name=clean_ro,
		purchase_orders=created_pos,
		rfqs=created_rfqs,
		payment_entries=created_payments,
	)

	target_label = f"#{clean_ro}" if clean_ro else "Inventory"
	msg = _("Created {0} PO(s) and {1} RFQ(s).").format(len(created_pos), len(created_rfqs))
	if created_payments:
		msg = _("Created {0} PO(s), {1} RFQ(s), and {2} Payment Entry(s).").format(
			len(created_pos), len(created_rfqs), len(created_payments)
		)

	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"repair_order": clean_ro,
			"company": res.get("company") or clean_company,
			"submitted": should_submit,
			"purchase_orders": created_pos,
			"rfqs": created_rfqs,
			"payment_entries": created_payments,
			"payments": payment_details,
		},
		message=msg,
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Procurement Orders ({target_label})\n"
			f"- **Submitted**: `{should_submit}`\n"
			f"- **Purchase Orders**: `{', '.join(created_pos) or 'None'}`\n"
			f"- **RFQs**: `{', '.join(created_rfqs) or 'None'}`\n"
			f"- **Payment Entries**: `{', '.join(created_payments) or 'None'}`"
		),
	)


@frappe.whitelist()
def receive_purchase_order(
	purchase_order: Optional[str] = None,
	submit_po: Any = True,
	purchase_invoice: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Submit PO (if still Draft) and post a Purchase Receipt for a Purchase Order or un-received Purchase Invoice."""
	purchase_order = optional_str(purchase_order, "Purchase Order")
	purchase_invoice = optional_str(purchase_invoice, "Purchase Invoice")
	if not purchase_order and not purchase_invoice:
		frappe.throw(
			_("Either Purchase Order or Purchase Invoice is required to receive goods."),
			frappe.MandatoryError,
		)

	# Enforce Purchase Receipt permissions upfront BEFORE potentially submitting a Draft PO.
	frappe.has_permission("Purchase Receipt", "create", throw=True)
	frappe.has_permission("Purchase Receipt", "submit", throw=True)

	po = None
	if purchase_order:
		frappe.has_permission("Purchase Order", "read", throw=True)
		if not frappe.db.exists("Purchase Order", purchase_order):
			frappe.throw(_("Purchase Order {0} does not exist.").format(purchase_order), frappe.DoesNotExistError)
		po = frappe.get_doc("Purchase Order", purchase_order)
		po.check_permission("read")

	pi = None
	if purchase_invoice:
		frappe.has_permission("Purchase Invoice", "read", throw=True)
		if not frappe.db.exists("Purchase Invoice", purchase_invoice):
			frappe.throw(_("Purchase Invoice {0} does not exist.").format(purchase_invoice), frappe.DoesNotExistError)
		pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
		pi.check_permission("read")
		if pi.docstatus != 1:
			frappe.throw(
				_("Purchase Invoice {0} must be submitted before receiving stock.").format(pi.name),
				frappe.ValidationError,
			)
		if cint(getattr(pi, "update_stock", 0)) == 1:
			frappe.throw(
				_("Purchase Invoice {0} already updated stock on submission; do not create a duplicate Purchase Receipt.").format(
					pi.name
				),
				frappe.ValidationError,
			)
		linked_pos = [row.purchase_order for row in (pi.get("items") or []) if getattr(row, "purchase_order", None)]
		if po and linked_pos and po.name not in linked_pos:
			frappe.throw(
				_("Purchase Invoice {0} is not linked to Purchase Order {1}.").format(pi.name, po.name),
				frappe.ValidationError,
			)
		if not po and linked_pos:
			po = frappe.get_doc("Purchase Order", linked_pos[0])
			po.check_permission("read")

	if po and po.docstatus == 0:
		if not coerce_bool(submit_po):
			frappe.throw(
				_("Purchase Order {0} is in Draft. Pass submit_po=True after explicit operator confirmation.").format(
					po.name
				),
				frappe.ValidationError,
			)
		po.check_permission("submit")
		po.submit()

	if po and not pi:
		if flt(getattr(po, "per_received", 0.0), 2) >= 100.0:
			frappe.throw(
				_("Purchase Order {0} has already been 100% received.").format(po.name),
				frappe.ValidationError,
			)
		# Scenario A: If a submitted non-stock Purchase Invoice already billed this PO,
		# map the Purchase Receipt from that Purchase Invoice so both PO and PI per_received reach 100%.
		linked_pi_names = frappe.get_all(
			"Purchase Invoice Item",
			filters={"purchase_order": po.name, "docstatus": 1},
			distinct=True,
			pluck="parent",
		)
		if linked_pi_names and frappe.has_permission("Purchase Invoice", "read"):
			matching_pis = frappe.get_list(
				"Purchase Invoice",
				filters={
					"name": ["in", linked_pi_names],
					"docstatus": 1,
					"update_stock": 0,
					"per_received": ["<", 100.0],
				},
				pluck="name",
				limit_page_length=1,
			)
			if matching_pis:
				pi = frappe.get_doc("Purchase Invoice", matching_pis[0])
				pi.check_permission("read")

	if pi:
		from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import (
			make_purchase_receipt as make_pr_from_pi,
		)

		pr_doc = make_pr_from_pi(source_name=pi.name)
	else:
		from erpnext.buying.doctype.purchase_order.purchase_order import (
			make_purchase_receipt as make_pr_from_po,
		)

		pr_doc = make_pr_from_po(source_name=po.name)

	if not pr_doc.get("items"):
		target_ref = pi.name if pi else po.name
		frappe.throw(
			_("No pending unreceived items remain on {0}.").format(target_ref),
			frappe.ValidationError,
		)

	pr_doc.check_permission("create")
	pr_doc.insert()
	pr_doc.check_permission("submit")
	pr_doc.submit()
	if po:
		po.reload()
	if pi:
		pi.reload()

	ro_name = getattr(po, "repair_order", None) if po else None
	ro_status = None
	if ro_name and frappe.db.exists("Repair Order", ro_name) and frappe.has_permission("Repair Order", "read", doc=ro_name):
		ro_status = frappe.db.get_value("Repair Order", ro_name, "status")

	urls = _build_procurement_urls(
		ro_name=ro_name,
		purchase_orders=[po.name] if po else None,
		purchase_invoices=[pi.name] if pi else None,
		purchase_receipt=pr_doc.name,
		primary=("purchase-receipt", pr_doc.name),
	)

	per_received = flt(getattr(po, "per_received", getattr(pi, "per_received", 100.0)))
	source_label = po.name if po else pi.name

	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"purchase_order": po.name if po else None,
			"purchase_invoice": pi.name if pi else None,
			"purchase_receipt": pr_doc.name,
			"docstatus": pr_doc.docstatus,
			"per_received": per_received,
			"repair_order": ro_name,
			"repair_order_status": ro_status,
			"items": [
				{"item_code": i.item_code, "qty": flt(i.qty), "warehouse": i.warehouse}
				for i in (pr_doc.items or [])
			],
		},
		message=_("Received {0} via Purchase Receipt {1}.").format(source_label, pr_doc.name),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Purchase Receipt Created: `{pr_doc.name}`\n"
			f"- **Source**: `{source_label}` ({per_received}% received)\n"
			f"- **Repair Order**: `{ro_name or 'None'}` (`{ro_status or 'N/A'}`)"
		),
	)


@frappe.whitelist()
def create_purchase_payment(
	purchase_order: Optional[str] = None,
	purchase_invoice: Optional[str] = None,
	payment_method: Optional[str] = None,
	paid_amount: Optional[float] = None,
	reference_no: Optional[str] = None,
	reference_date: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Create and submit a supplier Payment Entry against a Purchase Order (advance) or Purchase Invoice."""
	res = _create_purchase_payment_impl(
		purchase_order=purchase_order,
		purchase_invoice=purchase_invoice,
		payment_method=payment_method,
		paid_amount=paid_amount,
		reference_no=reference_no,
		reference_date=reference_date,
	)
	ro_name = res.get("repair_order")
	po_name = res["reference_name"] if res["reference_doctype"] == "Purchase Order" else None
	pi_name = res["reference_name"] if res["reference_doctype"] == "Purchase Invoice" else None
	urls = _build_procurement_urls(
		ro_name=ro_name,
		purchase_orders=[po_name] if po_name else None,
		purchase_invoices=[pi_name] if pi_name else None,
		payment_entries=[res["payment_entry"]],
		primary=("payment-entry", res["payment_entry"]),
	)
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=res,
		message=_("Submitted Payment Entry {0} ({1} {2}) against {3} {4}.").format(
			res["payment_entry"],
			res["mode_of_payment"],
			res["paid_amount"],
			res["reference_doctype"],
			res["reference_name"],
		),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Supplier Payment Submitted: `{res['payment_entry']}`\n"
			f"- **Target**: `{res['reference_doctype']}` `{res['reference_name']}`\n"
			f"- **Supplier**: `{res['supplier']}`\n"
			f"- **Method**: `{res['mode_of_payment']}` | **Paid Amount**: `{res['paid_amount']:.2f}`"
		),
	)


@frappe.whitelist()
def create_purchase_invoice(
	purchase_order: Optional[str] = None,
	supplier: Optional[str] = None,
	parts_data: Union[List[Dict[str, Any]], str, None] = None,
	update_stock: Any = False,
	company: Optional[str] = None,
	bill_no: Optional[str] = None,
	bill_date: Optional[str] = None,
	attach_advances: Any = True,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Create and submit a Purchase Invoice (from PO or standalone), with optional update_stock and PO advance attachment."""
	parsed_parts = parse_json_arg(parts_data, None, label="parts_data") if parts_data is not None else None
	res = _create_purchase_invoice_impl(
		purchase_order=purchase_order,
		supplier=supplier,
		parts_data=parsed_parts,
		update_stock=update_stock,
		company=company,
		bill_no=bill_no,
		bill_date=bill_date,
		attach_advances=attach_advances,
	)
	po_name = res.get("purchase_order")
	ro_name = res.get("repair_order")
	urls = _build_procurement_urls(
		ro_name=ro_name,
		purchase_orders=[po_name] if po_name else None,
		purchase_invoices=[res["purchase_invoice"]],
		primary=("purchase-invoice", res["purchase_invoice"]),
	)
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=res,
		message=_("Submitted Purchase Invoice {0} (update_stock={1}, advance_attached={2}, outstanding={3}).").format(
			res["purchase_invoice"],
			res["update_stock"],
			res["total_advance"],
			res["outstanding_amount"],
		),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Purchase Invoice Submitted: `{res['purchase_invoice']}`\n"
			f"- **Supplier**: `{res['supplier']}` | **PO**: `{po_name or 'Standalone'}`\n"
			f"- **Update Stock**: `{bool(res['update_stock'])}` | **Grand Total**: `{res['grand_total']:.2f}`\n"
			f"- **Advances Attached**: `{res['total_advance']:.2f}` | **Outstanding**: `{res['outstanding_amount']:.2f}`"
		),
	)
