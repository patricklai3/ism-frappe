# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import frappe
from frappe.utils import now_datetime

from ism.api.agent_fleet import (
	_action_item_urls,
	apply_inspection_template as _apply_inspection_template_impl,
	create_shop_action_item as _create_shop_action_item_impl,
	decode_vehicle_vin as _decode_vehicle_vin_impl,
	fetch_tesla_manual as _fetch_tesla_manual_impl,
	get_check_in_inspection as _get_check_in_inspection_impl,
	get_inspection_template as _get_inspection_template_impl,
	get_vehicle_intelligence as _get_vehicle_intelligence_impl,
	link_vehicle_customer as _link_vehicle_customer_impl,
	list_inspection_templates as _list_inspection_templates_impl,
	list_shop_action_items as _list_shop_action_items_impl,
	record_check_in_inspection as _record_check_in_inspection_impl,
	resolve_shop_action_item as _resolve_shop_action_item_impl,
	search_vehicles as _search_vehicles_impl,
	sync_action_item_state as _sync_action_item_state_impl,
)
from ism.api.agent_monitoring import (
	get_cockpit_snapshot as _get_cockpit_snapshot_impl,
	get_pnl_digest_inputs as _get_pnl_digest_inputs_impl,
	publish_health_snapshot as _publish_health_snapshot_impl,
)
from ism.api.agent_procurement import (
	_attach_package_parts,
	_build_procurement_urls,
	_collect_required_part_qtys,
	check_parts_stock as _check_parts_stock_impl,
	create_purchase_invoice as _create_purchase_invoice_impl,
	create_purchase_payment as _create_purchase_payment_impl,
	get_component_alternates as _get_component_alternates_impl,
	import_supplier_catalog as _import_supplier_catalog_impl,
	import_tesla_procedure as _import_tesla_procedure_impl,
	receive_purchase_order as _receive_purchase_order_impl,
	submit_procurement_orders as _submit_procurement_orders_domain_impl,
)
from ism.api.agent_quotes import (
	_SERVICE_HOURS_ALIASES,
	_SERVICE_TITLE_ALIASES,
	_build_service_row,
	_coerce_service_list,
	_estimate_duration_minutes,
	_first_present,
	_get_default_company,
	_get_ro_doc,
	_lookup_vehicle,
	_normalize_service_row_targets,
	_optional_str,
	_require_non_negative_int,
	_require_str,
	_resolve_and_validate_parts,
	_resolve_customer,
	_resolve_odometer_out,
	_resolve_service_package_name,
	_resolve_vehicle,
	_serialize_pending_deferred,
	add_deferred_services as _add_deferred_services_impl,
	add_supplement as _add_supplement_impl,
	amend_quote as _amend_quote_impl,
	checkout_repair_order as _checkout_repair_order_impl,
	create_quote as _create_quote_impl,
	duplicate_service_for_opposite_side as _duplicate_service_for_opposite_side_impl,
	get_deferred_services as _get_deferred_services_impl,
	record_ro_deposit as _record_ro_deposit_impl,
	send_quote_for_approval as _send_quote_for_approval_impl,
	toggle_service_completion as _toggle_service_completion_impl,
	update_job_status as _update_job_status_impl,
)
from ism.api.agent_scheduling import (
	_VALID_SCHEDULE_ENTRY_STATUSES,
	_build_ambiguous_bay_availability_message,
	_build_conflict_summary,
	_infer_schedule_entry_type,
	_resolve_bay_for_booking,
	_schedule_entry_urls,
	_serialize_schedule_entry,
	book_appointment as _book_appointment_impl,
	create_ro_from_appointment as _create_ro_from_appointment_impl,
	estimate_duration as _estimate_duration_impl,
	find_available_slots as _find_available_slots_impl,
	get_shop_floor_status as _get_shop_floor_status_impl,
	get_tech_overview as _get_tech_overview_impl,
	get_unscheduled_services as _get_unscheduled_services_impl,
	intake_and_find_slots as _intake_and_find_slots_impl,
	reschedule_appointment as _reschedule_appointment_impl,
	update_appointment_status as _update_appointment_status_impl,
)
from ism.api.parts import submit_procurement_orders as _submit_procurement_orders_impl
from ism.api.tesla import fetch_and_parse_tesla_procedure

__all__ = [
	"_SERVICE_HOURS_ALIASES",
	"_SERVICE_TITLE_ALIASES",
	"_VALID_SCHEDULE_ENTRY_STATUSES",
	"_action_item_urls",
	"_attach_package_parts",
	"_build_ambiguous_bay_availability_message",
	"_build_conflict_summary",
	"_build_procurement_urls",
	"_build_service_row",
	"_coerce_service_list",
	"_collect_required_part_qtys",
	"_estimate_duration_minutes",
	"_first_present",
	"_get_default_company",
	"_get_ro_doc",
	"_infer_schedule_entry_type",
	"_lookup_vehicle",
	"_normalize_service_row_targets",
	"_optional_str",
	"_require_non_negative_int",
	"_require_str",
	"_resolve_and_validate_parts",
	"_resolve_bay_for_booking",
	"_resolve_customer",
	"_resolve_odometer_out",
	"_resolve_service_package_name",
	"_resolve_vehicle",
	"_schedule_entry_urls",
	"_serialize_pending_deferred",
	"_serialize_schedule_entry",
	"_submit_procurement_orders_impl",
	"add_deferred_services",
	"add_supplement",
	"amend_quote",
	"apply_inspection_template",
	"book_appointment",
	"check_parts_stock",
	"checkout_repair_order",
	"create_purchase_invoice",
	"create_purchase_payment",
	"create_quote",
	"create_ro_from_appointment",
	"create_shop_action_item",
	"decode_vehicle_vin",
	"duplicate_service_for_opposite_side",
	"estimate_duration",
	"fetch_and_parse_tesla_procedure",
	"fetch_tesla_manual",
	"find_available_slots",
	"get_check_in_inspection",
	"get_cockpit_snapshot",
	"get_component_alternates",
	"get_deferred_services",
	"get_inspection_template",
	"get_pnl_digest_inputs",
	"get_shop_floor_status",
	"get_tech_overview",
	"get_unscheduled_services",
	"get_vehicle_intelligence",
	"import_supplier_catalog",
	"import_tesla_procedure",
	"intake_and_find_slots",
	"link_vehicle_customer",
	"list_inspection_templates",
	"list_shop_action_items",
	"now_datetime",
	"publish_health_snapshot",
	"receive_purchase_order",
	"record_check_in_inspection",
	"record_ro_deposit",
	"reschedule_appointment",
	"resolve_shop_action_item",
	"search_vehicles",
	"send_quote_for_approval",
	"submit_procurement_orders",
	"sync_action_item_state",
	"toggle_service_completion",
	"update_appointment_status",
	"update_job_status",
]


@frappe.whitelist()
def create_quote(
	customer_name: Optional[str] = None,
	vehicle_identifier: Optional[str] = None,
	customer: Optional[str] = None,
	vehicle: Optional[str] = None,
	services: Optional[Union[List[Any], str]] = None,
	parts: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	concern: Optional[str] = None,
	customer_phone: Optional[str] = None,
	customer_email: Optional[str] = None,
	company: Optional[str] = None,
	odometer_in: Optional[int] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Create a new Repair Order in Draft Estimate state with server-computed totals."""
	return _create_quote_impl(
		customer_name=customer_name,
		vehicle_identifier=vehicle_identifier,
		customer=customer,
		vehicle=vehicle,
		services=services,
		parts=parts,
		notes=notes,
		concern=concern,
		customer_phone=customer_phone,
		customer_email=customer_email,
		company=company,
		odometer_in=odometer_in,
		include_summary=include_summary,
	)


@frappe.whitelist()
def amend_quote(
	repair_order: str,
	add_services: Optional[Union[List[Any], str]] = None,
	remove_service_indices: Optional[Union[List[int], str]] = None,
	update_services: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	discount_amount: Optional[Union[float, int, str]] = None,
	shop_supplies_amount: Optional[Union[float, int, str]] = None,
	environmental_fee_amount: Optional[Union[float, int, str]] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Safely amend lines on an unapproved Repair Order in Draft Estimate state."""
	return _amend_quote_impl(
		repair_order=repair_order,
		add_services=add_services,
		remove_service_indices=remove_service_indices,
		update_services=update_services,
		notes=notes,
		discount_amount=discount_amount,
		shop_supplies_amount=shop_supplies_amount,
		environmental_fee_amount=environmental_fee_amount,
		include_summary=include_summary,
	)


@frappe.whitelist()
def find_available_slots(
	repair_order: Optional[str] = None,
	duration_minutes: Optional[int] = None,
	start_date: Optional[str] = None,
	target_date: Optional[str] = None,
	days_ahead: int = 3,
	equipment_tags: Optional[Union[List[str], str]] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	include_summary: Any = False,
	include_resources: Any = False,
	max_slots: int = 15,
	include_past: Any = False,
	min_start_time: Optional[str] = None,
) -> Dict[str, Any]:
	"""Query dual-resource capacity for available booking slots across shop operating hours."""
	return _find_available_slots_impl(
		repair_order=repair_order,
		duration_minutes=duration_minutes,
		start_date=start_date,
		target_date=target_date,
		days_ahead=days_ahead,
		equipment_tags=equipment_tags,
		service_bay=service_bay,
		technician=technician,
		include_summary=include_summary,
		include_resources=include_resources,
		max_slots=max_slots,
		include_past=include_past,
		min_start_time=min_start_time,
	)


@frappe.whitelist()
def book_appointment(
	repair_order: Optional[str] = None,
	scheduled_date: str = "",
	start_time: str = "",
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	entry_type: Optional[str] = None,
	notes: Optional[str] = None,
	estimated_duration: Optional[int] = None,
	provisional_customer_name: Optional[str] = None,
	provisional_vehicle_info: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Allocate a linked or standalone Schedule Entry and broadcast ambient realtime update."""
	return _book_appointment_impl(
		repair_order=repair_order,
		scheduled_date=scheduled_date,
		start_time=start_time,
		service_bay=service_bay,
		technician=technician,
		entry_type=entry_type,
		notes=notes,
		estimated_duration=estimated_duration,
		provisional_customer_name=provisional_customer_name,
		provisional_vehicle_info=provisional_vehicle_info,
		include_summary=include_summary,
	)


@frappe.whitelist()
def send_quote_for_approval(
	repair_order: str,
	communication_medium: str = "sms",
	phone_number: Optional[str] = None,
	recipient: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Generate secure approval token and dispatch authorization link via approval service."""
	return _send_quote_for_approval_impl(
		repair_order=repair_order,
		communication_medium=communication_medium,
		phone_number=phone_number,
		recipient=recipient,
		include_summary=include_summary,
	)


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
	return _import_supplier_catalog_impl(
		items=items,
		raw_text=raw_text,
		supplier=supplier,
		default_source=default_source,
		default_condition=default_condition,
		service_package=service_package,
		include_summary=include_summary,
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
	return _get_component_alternates_impl(
		service_package=service_package,
		component_slot=component_slot,
		slot_number=slot_number,
		oem_part_number=oem_part_number,
		item_code=item_code,
		include_summary=include_summary,
	)


@frappe.whitelist()
def import_tesla_procedure(
	procedure_title: Optional[str] = None,
	correction_code: Optional[str] = None,
	labor_hours: float = 1.0,
	model: str = "Model 3",
	applicable_generation: Optional[str] = None,
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
	return _import_tesla_procedure_impl(
		procedure_title=procedure_title,
		correction_code=correction_code,
		labor_hours=labor_hours,
		model=model,
		applicable_generation=applicable_generation,
		parts=parts,
		repair_order=repair_order,
		url=url,
		data=data,
		on_conflict=on_conflict,
		force=force,
		side=side,
		is_supplement=is_supplement,
		advisor_notes=advisor_notes,
		include_summary=include_summary,
	)


@frappe.whitelist()
def get_shop_floor_status(
	date: Optional[str] = None, include_summary: Any = False
) -> Dict[str, Any]:
	"""Executive query summarizing bay occupancy, active tech pool, and active Repair Orders."""
	return _get_shop_floor_status_impl(date=date, include_summary=include_summary)


@frappe.whitelist()
def update_job_status(
	repair_order: str,
	action: str,
	odometer_out: Optional[int] = None,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Technician & Advisor 1-tap state transition enforcement with legal authorization locks."""
	return _update_job_status_impl(
		repair_order=repair_order,
		action=action,
		odometer_out=odometer_out,
		notes=notes,
		include_summary=include_summary,
	)


@frappe.whitelist()
def check_parts_stock(
	repair_order: Optional[str] = None,
	items: Optional[Union[List[Any], str]] = None,
	company: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Check warehouse stock availability and deficits for a Repair Order or item list."""
	return _check_parts_stock_impl(
		repair_order=repair_order,
		items=items,
		company=company,
		include_summary=include_summary,
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
	return _submit_procurement_orders_domain_impl(
		repair_order=repair_order,
		parts_data=parts_data,
		submit=submit,
		company=company,
		payment_method=payment_method,
		paid_amount=paid_amount,
		reference_no=reference_no,
		reference_date=reference_date,
		include_summary=include_summary,
	)


@frappe.whitelist()
def fetch_tesla_manual(
	url: str,
	repair_order: Optional[str] = None,
	vehicle: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Fetch and parse a Tesla Service Manual procedure URL with optional compatibility check."""
	return _fetch_tesla_manual_impl(
		url=url,
		repair_order=repair_order,
		vehicle=vehicle,
		include_summary=include_summary,
	)


@frappe.whitelist()
def get_tech_overview(
	date: Optional[str] = None, include_summary: Any = False
) -> Dict[str, Any]:
	"""Return daily technician workload, shift windows, and bottleneck analysis."""
	return _get_tech_overview_impl(date=date, include_summary=include_summary)


@frappe.whitelist()
def get_unscheduled_services(
	repair_order: str, include_summary: Any = False
) -> Dict[str, Any]:
	"""Return approved services on a Repair Order that lack a scheduled bay allocation."""
	return _get_unscheduled_services_impl(
		repair_order=repair_order, include_summary=include_summary
	)


@frappe.whitelist()
def receive_purchase_order(
	purchase_order: Optional[str] = None,
	submit_po: Any = True,
	purchase_invoice: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Submit PO (if still Draft) and post a Purchase Receipt for a Purchase Order or un-received Purchase Invoice."""
	return _receive_purchase_order_impl(
		purchase_order=purchase_order,
		submit_po=submit_po,
		purchase_invoice=purchase_invoice,
		include_summary=include_summary,
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
	return _create_purchase_payment_impl(
		purchase_order=purchase_order,
		purchase_invoice=purchase_invoice,
		payment_method=payment_method,
		paid_amount=paid_amount,
		reference_no=reference_no,
		reference_date=reference_date,
		include_summary=include_summary,
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
	return _create_purchase_invoice_impl(
		purchase_order=purchase_order,
		supplier=supplier,
		parts_data=parts_data,
		update_stock=update_stock,
		company=company,
		bill_no=bill_no,
		bill_date=bill_date,
		attach_advances=attach_advances,
		include_summary=include_summary,
	)


@frappe.whitelist()
def record_ro_deposit(
	repair_order: str,
	amount: Union[float, int, str],
	payment_method: str = "Debit / Interac",
	reference_no: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Record a customer deposit payment against a Repair Order."""
	return _record_ro_deposit_impl(
		repair_order=repair_order,
		amount=amount,
		payment_method=payment_method,
		reference_no=reference_no,
		include_summary=include_summary,
	)


@frappe.whitelist()
def checkout_repair_order(
	repair_order: str,
	payment_method: Optional[str] = None,
	paid_amount: Optional[float] = None,
	odometer_out: Optional[int] = None,
	payments: Optional[Union[List[Any], str]] = None,
	confirm: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Invoice, record payment, complete linked schedule entries, and release a Repair Order."""
	return _checkout_repair_order_impl(
		repair_order=repair_order,
		payment_method=payment_method,
		paid_amount=paid_amount,
		odometer_out=odometer_out,
		payments=payments,
		confirm=confirm,
		include_summary=include_summary,
	)


@frappe.whitelist()
def toggle_service_completion(
	repair_order: str,
	service_row_id: Optional[Union[str, List[str]]] = None,
	is_completed: Any = True,
	service_row_ids: Optional[Union[List[str], str]] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Toggle completion status on one or more approved service rows on a Repair Order."""
	return _toggle_service_completion_impl(
		repair_order=repair_order,
		service_row_id=service_row_id,
		is_completed=is_completed,
		service_row_ids=service_row_ids,
		include_summary=include_summary,
	)


@frappe.whitelist()
def add_supplement(
	repair_order: str,
	service_title: str,
	category: str = "Maintenance",
	labor_hours: float = 1.0,
	labor_rate: Optional[float] = None,
	parts: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Add a mid-job supplemental finding/operation in Estimating status for customer re-approval."""
	return _add_supplement_impl(
		repair_order=repair_order,
		service_title=service_title,
		category=category,
		labor_hours=labor_hours,
		labor_rate=labor_rate,
		parts=parts,
		notes=notes,
		include_summary=include_summary,
	)


@frappe.whitelist()
def duplicate_service_for_opposite_side(
	repair_order: str,
	service_row_id: str,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Duplicate an LH or RH symmetrical service operation for the opposite vehicle side."""
	return _duplicate_service_for_opposite_side_impl(
		repair_order=repair_order,
		service_row_id=service_row_id,
		include_summary=include_summary,
	)


@frappe.whitelist()
def get_deferred_services(
	repair_order: Optional[str] = None,
	vehicle: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Retrieve open previously declined/deferred service recommendations for a vehicle or Repair Order."""
	return _get_deferred_services_impl(
		repair_order=repair_order,
		vehicle=vehicle,
		include_summary=include_summary,
	)


@frappe.whitelist()
def add_deferred_services(
	repair_order: str,
	deferred_items: Optional[Union[List[Any], str]] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Add previously deferred service recommendations onto an existing Repair Order."""
	return _add_deferred_services_impl(
		repair_order=repair_order,
		deferred_items=deferred_items,
		include_summary=include_summary,
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
	return _list_shop_action_items_impl(
		status=status,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		category=category,
		limit=limit,
		include_summary=include_summary,
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
	return _create_shop_action_item_impl(
		title=title,
		category=category,
		priority=priority,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		item_code=item_code,
		details=details,
		dedup_key=dedup_key,
		source=source,
		include_summary=include_summary,
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
	return _sync_action_item_state_impl(
		dedup_key=dedup_key,
		action=action,
		snooze_minutes=snooze_minutes,
		title=title,
		notes=notes,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		include_summary=include_summary,
	)


@frappe.whitelist()
def get_cockpit_snapshot(
	date: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return batched live shop telemetry, dual-resource schedule, action overlays, and cached scores."""
	return _get_cockpit_snapshot_impl(date=date, include_summary=include_summary)


@frappe.whitelist()
def get_pnl_digest_inputs(
	from_date: Optional[str] = None,
	to_date: Optional[str] = None,
	company: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return server-computed P&L, labor rate, parts margin, and unbilled WIP metrics for agent scoring."""
	return _get_pnl_digest_inputs_impl(
		from_date=from_date,
		to_date=to_date,
		company=company,
		include_summary=include_summary,
	)


@frappe.whitelist()
def publish_health_snapshot(
	metric_key: str,
	score: Any,
	verdict: str,
	grade: Optional[str] = None,
	period_date: Optional[str] = None,
	report_url: Optional[str] = None,
	details: Optional[Union[Dict[str, Any], str]] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Publish a synced 0-100 Shop Health Snapshot for manager cockpits."""
	return _publish_health_snapshot_impl(
		metric_key=metric_key,
		score=score,
		verdict=verdict,
		grade=grade,
		period_date=period_date,
		report_url=report_url,
		details=details,
		include_summary=include_summary,
	)


@frappe.whitelist()
def resolve_shop_action_item(
	action_item: str,
	resolution_notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Resolve an open Shop Action Item with resolution notes."""
	return _resolve_shop_action_item_impl(
		action_item=action_item,
		resolution_notes=resolution_notes,
		include_summary=include_summary,
	)


@frappe.whitelist()
def reschedule_appointment(
	schedule_entry: str,
	scheduled_date: Optional[str] = None,
	start_time: Optional[str] = None,
	duration_minutes: Optional[int] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	notes: Optional[str] = None,
	include_summary: Any = False,
	clear_bay: Any = False,
) -> Dict[str, Any]:
	"""Move an existing Schedule Entry to a new date, time, bay, or technician with capacity checks."""
	return _reschedule_appointment_impl(
		schedule_entry=schedule_entry,
		scheduled_date=scheduled_date,
		start_time=start_time,
		duration_minutes=duration_minutes,
		service_bay=service_bay,
		technician=technician,
		notes=notes,
		include_summary=include_summary,
		clear_bay=clear_bay,
	)


@frappe.whitelist()
def intake_and_find_slots(
	customer_name: Optional[str] = None,
	vehicle_identifier: Optional[str] = None,
	customer: Optional[str] = None,
	vehicle: Optional[str] = None,
	services: Optional[Union[List[Any], str]] = None,
	parts: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	concern: Optional[str] = None,
	customer_phone: Optional[str] = None,
	customer_email: Optional[str] = None,
	company: Optional[str] = None,
	odometer_in: Optional[int] = None,
	start_date: Optional[str] = None,
	target_date: Optional[str] = None,
	days_ahead: int = 3,
	duration_minutes: Optional[int] = None,
	equipment_tags: Optional[Union[List[str], str]] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	max_slots: int = 10,
	include_past: Any = False,
	min_start_time: Optional[str] = None,
	include_resources: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Composite intake endpoint: create Draft Estimate quote and return candidate booking slots."""
	return _intake_and_find_slots_impl(
		customer_name=customer_name,
		vehicle_identifier=vehicle_identifier,
		customer=customer,
		vehicle=vehicle,
		services=services,
		parts=parts,
		notes=notes,
		concern=concern,
		customer_phone=customer_phone,
		customer_email=customer_email,
		company=company,
		odometer_in=odometer_in,
		start_date=start_date,
		target_date=target_date,
		days_ahead=days_ahead,
		duration_minutes=duration_minutes,
		equipment_tags=equipment_tags,
		service_bay=service_bay,
		technician=technician,
		max_slots=max_slots,
		include_past=include_past,
		min_start_time=min_start_time,
		include_resources=include_resources,
		include_summary=include_summary,
	)


@frappe.whitelist()
def update_appointment_status(
	schedule_entry: str,
	status: str,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Transition a Schedule Entry status and push a single ambient calendar refresh."""
	return _update_appointment_status_impl(
		schedule_entry=schedule_entry,
		status=status,
		notes=notes,
		include_summary=include_summary,
	)


@frappe.whitelist()
def create_ro_from_appointment(
	schedule_entry: str,
	odometer_in: Optional[int] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Convert a pre-intake or diagnostic Schedule Entry into a Checked In Repair Order."""
	return _create_ro_from_appointment_impl(
		schedule_entry=schedule_entry,
		odometer_in=odometer_in,
		include_summary=include_summary,
	)


@frappe.whitelist()
def estimate_duration(
	repair_order: Optional[str] = None,
	operations: Optional[Union[List[Any], str]] = None,
	percentile: Optional[float] = None,
	sigma: Optional[float] = None,
	buffer_minutes: Optional[int] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Estimate probabilistic shop duration (log-normal + Fenton-Wilkinson) for an RO or operations list."""
	return _estimate_duration_impl(
		repair_order=repair_order,
		operations=operations,
		percentile=percentile,
		sigma=sigma,
		buffer_minutes=buffer_minutes,
		include_summary=include_summary,
	)


@frappe.whitelist()
def decode_vehicle_vin(
	vin: str,
	manufactured_month: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Decode a 17-character Tesla VIN offline and return specs and manual URL."""
	return _decode_vehicle_vin_impl(
		vin=vin,
		manufactured_month=manufactured_month,
		include_summary=include_summary,
	)


@frappe.whitelist()
def search_vehicles(
	query: Optional[str] = None,
	customer: Optional[str] = None,
	model: Optional[str] = None,
	generation: Optional[str] = None,
	limit: int = 20,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Search Repair Vehicles by VIN, license plate, model, generation, or linked customer."""
	return _search_vehicles_impl(
		query=query,
		customer=customer,
		model=model,
		generation=generation,
		limit=limit,
		include_summary=include_summary,
	)


@frappe.whitelist()
def get_vehicle_intelligence(
	vehicle: str,
	history_limit: int = 20,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return decoded VIN specs, owners, Repair Order history, and open deferred services for a vehicle."""
	return _get_vehicle_intelligence_impl(
		vehicle=vehicle,
		history_limit=history_limit,
		include_summary=include_summary,
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
	return _link_vehicle_customer_impl(
		vehicle=vehicle,
		customer=customer,
		is_primary=is_primary,
		relationship=relationship,
		include_summary=include_summary,
	)


@frappe.whitelist()
def list_inspection_templates(
	include_inactive: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""List vehicle check-in inspection templates with item counts."""
	return _list_inspection_templates_impl(
		include_inactive=include_inactive,
		include_summary=include_summary,
	)


@frappe.whitelist()
def get_inspection_template(
	template_name: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Retrieve an Inspection Template and its ordered checklist items."""
	return _get_inspection_template_impl(
		template_name=template_name,
		include_summary=include_summary,
	)


@frappe.whitelist()
def apply_inspection_template(
	repair_order: str,
	template_name: Optional[str] = None,
	replace_existing: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Apply an Inspection Template's checklist items onto a Repair Order."""
	return _apply_inspection_template_impl(
		repair_order=repair_order,
		template_name=template_name,
		replace_existing=replace_existing,
		include_summary=include_summary,
	)


@frappe.whitelist()
def get_check_in_inspection(
	repair_order: str,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return current check-in inspection items and summary counts for a Repair Order."""
	return _get_check_in_inspection_impl(
		repair_order=repair_order,
		include_summary=include_summary,
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
	return _record_check_in_inspection_impl(
		repair_order=repair_order,
		items=items,
		odometer_in=odometer_in,
		odometer=odometer,
		customer_concern=customer_concern,
		template_name=template_name,
		auto_apply_template=auto_apply_template,
		include_summary=include_summary,
	)
