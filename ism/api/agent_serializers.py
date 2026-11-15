"""Compact serialization and envelope helpers for the ISM Agent Facade."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from frappe.utils import cint, flt

from ism.api._validation import coerce_bool, normalize_condition, parse_json_arg

__all__ = [
	"build_agent_envelope",
	"build_alternates_summary",
	"build_appointment_summary",
	"build_catalog_summary",
	"build_estimate_summary",
	"build_floor_summary",
	"build_inspection_summary",
	"build_quote_summary",
	"build_ro_urls",
	"build_slots_summary",
	"build_vehicle_intel_summary",
	"build_vin_decode_summary",
	"coerce_bool",
	"normalize_condition",
	"parse_json_arg",
	"serialize_repair_order_compact",
	"serialize_service_package_compact",
]


def build_ro_urls(ro_name: str, approval_url: Optional[str] = None) -> Dict[str, str]:
	"""Build canonical Desk and Print URLs for a Repair Order."""
	from frappe.utils import get_url

	base = get_url()
	encoded_ro = quote(str(ro_name), safe="")
	urls = {
		"desk": f"{base}/app/repair-order/{encoded_ro}",
		"print": f"{base}/printview?doctype=Repair+Order&name={encoded_ro}&format=Automotive+Repair+Invoice",
	}
	if approval_url:
		urls["approval"] = approval_url
	return urls


def build_agent_envelope(
	success: bool,
	data: Optional[Dict[str, Any]] = None,
	urls: Optional[Dict[str, str]] = None,
	message: Optional[str] = None,
	error: Optional[str] = None,
	include_summary: Any = False,
	summary_builder: Optional[Callable[[], str] | str] = None,
) -> Dict[str, Any]:
	"""Return a compact standardized response envelope for all Agent Facade endpoints."""
	payload: Dict[str, Any] = {
		"success": bool(success),
		"message": message or ("Operation succeeded" if success else "Operation failed"),
		"urls": urls or {},
		"data": data if data is not None else {},
	}
	if error is not None:
		payload["error"] = error
	if coerce_bool(include_summary) and summary_builder is not None:
		payload["summary_card"] = summary_builder() if callable(summary_builder) else str(summary_builder)
	return payload


def serialize_repair_order_compact(
	ro: Any,
	vehicle_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
	"""Serialize a Repair Order without child HTML blobs or unused DocType metadata."""
	services: List[Dict[str, Any]] = []
	for svc in getattr(ro, "services", None) or []:
		services.append({
			"name": svc.name,
			"service_title": svc.service_title,
			"service_package": getattr(svc, "service_package", None),
			"service_position": getattr(svc, "service_position", None),
			"correction_code": getattr(svc, "correction_code", None),
			"status": getattr(svc, "status", None),
			"is_completed": cint(getattr(svc, "is_completed", 0)),
			"is_supplement": cint(getattr(svc, "is_supplement", 0)),
			"labor_hours": flt(getattr(svc, "labor_hours", 0.0)),
			"labor_rate": flt(getattr(svc, "labor_rate", 0.0)),
			"labor_amount": flt(getattr(svc, "labor_amount", 0.0), 2),
			"parts_total": flt(getattr(svc, "parts_total", 0.0), 2),
			"total_amount": flt(getattr(svc, "total_amount", 0.0), 2),
		})

	meta = vehicle_meta or {}
	vin = getattr(ro, "vin", None) or meta.get("vin")
	license_plate = getattr(ro, "license_plate", None) or meta.get("license_plate")

	return {
		"name": ro.name,
		"status": ro.status,
		"approval_status": ro.approval_status,
		"customer": ro.customer,
		"vehicle": ro.vehicle,
		"vin": vin,
		"license_plate": license_plate,
		"vehicle_location": getattr(ro, "vehicle_location", None),
		"total_bay_hours": flt(getattr(ro, "total_bay_hours", 0.0), 2),
		"net_total": flt(getattr(ro, "net_total", 0.0), 2),
		"total_taxes_and_charges": flt(getattr(ro, "total_taxes_and_charges", 0.0), 2),
		"grand_total": flt(getattr(ro, "grand_total", 0.0), 2),
		"total_services": len(services),
		"progress_percent": flt(getattr(ro, "progress_percent", 0.0), 1),
		"odometer_in": getattr(ro, "odometer_in", None),
		"odometer_out": getattr(ro, "odometer_out", None),
		"services": services,
	}


def serialize_service_package_compact(pkg: Any) -> Dict[str, Any]:
	"""Serialize a Service Package and its item lines concisely."""
	items: List[Dict[str, Any]] = []
	for row in getattr(pkg, "items", None) or []:
		items.append({
			"item_code": getattr(row, "item_code", None),
			"item_name": getattr(row, "item_name", None),
			"oem_part_number": getattr(row, "oem_part_number", None),
			"qty": flt(getattr(row, "qty", 1.0)),
			"part_source": getattr(row, "part_source", None),
			"part_condition": getattr(row, "part_condition", None),
			"component_slot": getattr(row, "component_slot", None),
			"is_default": int(getattr(row, "is_default", 0) or 0),
		})

	return {
		"name": pkg.name,
		"package_name": pkg.package_name,
		"correction_code": getattr(pkg, "correction_code", None),
		"applicable_model": getattr(pkg, "applicable_model", None),
		"standard_labor_hours": flt(getattr(pkg, "standard_labor_hours", 0.0)),
		"items_count": len(items),
		"items": items,
	}


def build_quote_summary(
	ro: Any,
	duration_min: int,
	title_prefix: str = "Repair Order Quote",
	deferred_services: Optional[List[Dict[str, Any]]] = None,
) -> str:
	"""Render an optional markdown quote summary card."""
	urls = build_ro_urls(ro.name)
	lines = [
		f"### {title_prefix} #{ro.name}",
		f"**Customer**: `{ro.customer}` | **Vehicle**: `{ro.vehicle}`",
		f"**Status**: `{ro.status}` | **Est. Shop Time**: `{duration_min} mins`",
		"",
		"| Service Operation | Hours | Labor ($) | Parts ($) | Line Total ($) |",
		"| :--- | :---: | :---: | :---: | :---: |",
	]
	for s in ro.services or []:
		lines.append(
			f"| {s.service_title} | {flt(s.labor_hours):.1f} | ${flt(s.labor_amount):.2f} "
			f"| ${flt(s.parts_total):.2f} | ${flt(s.total_amount):.2f} |"
		)
	tax_rate = getattr(ro, "tax_rate", None)
	tax_label = f"Tax ({flt(tax_rate):g}%)" if tax_rate is not None else "Tax"
	lines.extend([
		"",
		f"**Subtotal**: `${flt(ro.net_total):,.2f}` | **{tax_label}**: `${flt(ro.total_taxes_and_charges):,.2f}` "
		f"| **Grand Total**: `${flt(ro.grand_total):,.2f}`",
	])
	if deferred_services:
		lines.extend(["", f"**Open Deferred Services on Vehicle ({len(deferred_services)})**:"])
		for d in deferred_services:
			haz = " [Safety Hazard]" if cint(d.get("safety_hazard", 0)) else ""
			lines.append(f"- `{d.get('job_title')}` (${flt(d.get('estimated_amount')):,.2f}){haz}")
	lines.extend([
		"",
		f"[View Print Preview]({urls['print']})  |  [Open in Desk]({urls['desk']})",
	])
	return "\n".join(lines)


def build_slots_summary(slots: List[Dict[str, Any]], duration_minutes: int) -> str:
	"""Render an optional markdown slot availability table."""
	lines = [
		f"### Available Booking Slots ({duration_minutes} min duration)",
		"",
		"| Date | Start Time | Available Bays | Available Techs | Bottleneck |",
		"| :--- | :---: | :---: | :---: | :---: |",
	]
	for r in slots:
		lines.append(
			f"| {r.get('date')} | {r.get('start_time')} | {r.get('available_bays')} "
			f"| {r.get('available_technicians')} | {r.get('bottleneck') or 'None'} |"
		)
	if not slots:
		lines.append("| No free slots found in requested range. | - | - | - | Full |")
	return "\n".join(lines)


def build_floor_summary(summary: Dict[str, Any], active_ros: List[Dict[str, Any]]) -> str:
	"""Render an optional markdown shop floor overview."""
	lines = [
		"### Shop Floor Status",
		f"- **Active Bays:** {summary.get('active_bays', 0)}",
		f"- **Active Technicians:** {summary.get('active_technicians', 0)}",
		f"- **Active Repair Orders:** {summary.get('active_repair_orders', 0)}",
		f"- **Active Bay Allocations:** {summary.get('active_allocations', 0)}",
	]
	if active_ros:
		lines.extend(["", "| RO | Status | Approval | Location |", "|---|---|---|---|"])
		for ro in active_ros[:10]:
			lines.append(
				f"| {ro.get('name')} | {ro.get('status')} | {ro.get('approval_status')} | {ro.get('vehicle_location') or '-'} |"
			)
	return "\n".join(lines)


def build_estimate_summary(est: Dict[str, Any]) -> str:
	"""Render an optional markdown duration estimation breakdown."""
	ro_name = est.get("repair_order")
	heading = f"### Estimated Shop Duration for #{ro_name}" if ro_name else "### Estimated Shop Duration"
	total_min = est.get("total_duration_minutes", 0)
	p_pure = est.get("p_pure", 0)
	buf = est.get("buffer_padding", 0)
	ops = est.get("operations_count", 0)
	return "\n".join([
		heading,
		f"- **Total Duration**: `{total_min} min` (`{round(total_min / 60.0, 2)} hrs`)",
		f"- **Pure Model Estimate**: `{p_pure} min` | **Buffer Padding**: `{buf} min`",
		f"- **Operations Count**: `{ops}`",
	])


def build_appointment_summary(heading: str, entry: Dict[str, Any], urls: Dict[str, str]) -> str:
	"""Render an optional markdown Schedule Entry confirmation card."""
	lines = [
		f"### {heading}: `{entry.get('name')}`",
		f"- **Status**: `{entry.get('status')}`",
		f"- **Date**: `{entry.get('scheduled_date')}` | **Time**: `{entry.get('scheduled_time')}`",
		f"- **Bay**: `{entry.get('service_bay') or 'Unassigned'}` | "
		f"**Technician**: `{entry.get('assigned_technician') or 'Unassigned'}`",
	]
	if entry.get("warnings"):
		lines.append(f"- **Warnings**: {'; '.join(str(w) for w in entry['warnings'])}")
	links = [f"[Inspect Entry]({urls['desk']})" if urls.get("desk") else "", f"[View Calendar]({urls['calendar']})" if urls.get("calendar") else ""]
	links = [lnk for lnk in links if lnk]
	if links:
		lines.extend(["", "  |  ".join(links)])
	return "\n".join(lines)


def build_catalog_summary(supplier: Optional[str], created: List[str], updated: List[str], errors: List[Any]) -> str:
	"""Render an optional markdown summary for supplier catalog ingestion."""
	lines = [
		"### Supplier Catalog Ingestion Complete",
		f"**Supplier**: `{supplier or 'Unspecified'}`",
		f"**Created Items**: `{len(created)}`",
		f"**Updated Items**: `{len(updated)}`",
		f"**Errors**: `{len(errors)}`",
	]
	return "\n".join(lines)


def build_alternates_summary(
	alternates: List[Dict[str, Any]],
	service_package: Optional[str] = None,
	oem_part_number: Optional[str] = None,
	item_code: Optional[str] = None,
) -> str:
	"""Render an optional markdown summary for component alternate lookup."""
	target = service_package or oem_part_number or item_code or "Query"
	lines = [f"### Component Alternates ({target}) — {len(alternates)} found"]
	for alt in alternates[:10]:
		lines.append(
			f"- `{alt.get('item_code')}` ({alt.get('part_source', 'OEM')} / {alt.get('part_condition', 'New')}) "
			f"— `${flt(alt.get('rate')):.2f}`, Stock: `{flt(alt.get('stock_qty'))}`"
		)
	return "\n".join(lines)


def build_vin_decode_summary(decoded: Dict[str, Any]) -> str:
	"""Render an optional markdown summary for offline Tesla VIN decoding."""
	if decoded.get("error"):
		return f"### VIN Decode Error (`{decoded.get('vin') or 'N/A'}`)\n- **Error**: {decoded['error']}"
	return "\n".join([
		f"### Tesla VIN Decoded: `{decoded.get('vin')}`",
		f"- **Vehicle**: `{decoded.get('model_year')}` `{decoded.get('model')}` ({decoded.get('drivetrain')})",
		f"- **Hardware**: `{decoded.get('autopilot_hardware')}` | **Check Digit Valid**: `{bool(decoded.get('is_valid_vin'))}`",
		f"- **Manual**: {decoded.get('recommended_manual_url')}",
	])


def build_vehicle_intel_summary(intel: Dict[str, Any]) -> str:
	"""Render an optional markdown summary for full vehicle intelligence."""
	veh = intel.get("vehicle") or {}
	deferred = intel.get("deferred_services") or {}
	return "\n".join([
		f"### Vehicle Intelligence: `{veh.get('name')}`",
		f"- **Model**: `{veh.get('model_year')}` `{veh.get('model')}` | **Plate**: `{veh.get('license_plate') or 'N/A'}`",
		f"- **Owners**: `{len(intel.get('owners') or [])}` | **Past ROs**: `{len(intel.get('service_history') or [])}`",
		f"- **Open Deferred**: `{deferred.get('open_count', 0)}` (`${flt(deferred.get('open_estimated_total')):,.2f}`)",
	])


def build_inspection_summary(payload: Dict[str, Any], heading: str = "Check-In Inspection") -> str:
	"""Render an optional markdown summary for Repair Order check-in inspection state."""
	summary = payload.get("summary") or {}
	return "\n".join([
		f"### {heading}: `{payload.get('repair_order')}`",
		f"- **Template**: `{payload.get('inspection_template') or 'Custom'}` | **Odometer**: `{payload.get('odometer_in')}`",
		f"- **Items**: `{summary.get('completed', 0)}/{summary.get('total', 0)}` completed "
		f"(Pass: `{summary.get('pass', 0)}`, Attention: `{summary.get('attention', 0)}`, Fail: `{summary.get('fail', 0)}`)",
	])
