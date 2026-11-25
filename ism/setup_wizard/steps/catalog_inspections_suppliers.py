# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Step 60: Inspection Templates, Parts Suppliers, and Service Packages catalog."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt

from ism.setup_wizard.base import BaseSetupStep, build_desk_link, normalize_records_list
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_INFO,
	CHECK_PASS,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	TIER_CORE,
	CheckResult,
	SetupContext,
	StepApplyResult,
	StepInspection,
	StepMeta,
)
from ism.setup_wizard.presets import (
	EV_UNDERCARRIAGE_INSPECTION,
	STARTER_SERVICE_PACKAGES,
	STARTER_SUPPLIERS,
)

_DEFAULT_WALKAROUND_TEMPLATE = "Standard Vehicle Walkaround"

_PACKAGE_SCALAR_FIELDS: tuple[str, ...] = (
	"applicable_model",
	"applicable_generation",
	"correction_code",
	"rh_correction_code",
	"standard_labor_hours",
	"hourly_rate",
	"service_rate",
	"is_flat_rate",
	"is_symmetrical",
	"allow_self_serve_booking",
	"self_serve_sort_order",
	"customer_title",
	"customer_summary",
	"applicable_year_from",
	"applicable_year_to",
	"customer_price_display_mode",
	"customer_estimate_min",
	"customer_estimate_max",
	"self_serve_duration_minutes",
)

_PACKAGE_INT_FIELDS = frozenset({
	"is_flat_rate",
	"is_symmetrical",
	"allow_self_serve_booking",
	"self_serve_sort_order",
	"applicable_year_from",
	"applicable_year_to",
	"self_serve_duration_minutes",
})

_PACKAGE_FLOAT_FIELDS = frozenset({
	"standard_labor_hours",
	"hourly_rate",
	"service_rate",
	"customer_estimate_min",
	"customer_estimate_max",
})


def _normalize_tag_list(raw: Any) -> list[str]:
	if not raw:
		return []
	if isinstance(raw, str):
		return [p.strip() for p in raw.split(",") if p.strip()]
	out: list[str] = []
	seen: set[str] = set()
	if isinstance(raw, (list, tuple)):
		for item in raw:
			tag = (
				item.strip()
				if isinstance(item, str)
				else str(item.get("equipment_tag") or item.get("tag_name") or "").strip()
				if isinstance(item, dict)
				else ""
			)
			if tag and tag not in seen:
				seen.add(tag)
				out.append(tag)
	return out


def _resolve_supplier_group(requested: str | None) -> str:
	candidate = (requested or "").strip()
	if candidate and frappe.db.exists("Supplier Group", candidate):
		return candidate
	if frappe.db.exists("Supplier Group", "All Supplier Groups"):
		return "All Supplier Groups"
	return frappe.db.get_value("Supplier Group", {}, "name") or "All Supplier Groups"


class CatalogInspectionsSuppliersStep(BaseSetupStep):
	meta = StepMeta(
		id="catalog_inspections_suppliers",
		title="Inspection Templates, Suppliers & Service Packages",
		phase="catalog",
		order=60,
		schema_version=1,
		tier=TIER_CORE,
		depends_on=("workshop_bays_and_bins",),
		owner_domain="ism",
		managed_settings_sections=(),
		allow_skip=True,
		description=(
			"Seed or import Inspection Templates, parts Suppliers, and flat-rate / self-serve "
			"Service Packages for Day-1 quoting and intake."
		),
	)

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "ev_undercarriage_inspection",
				"label": "EV & Tesla Undercarriage Inspection Template",
				"description": "Adds the 10-point Ontario EV & Tesla undercarriage, brake, and HV safety inspection template.",
				"payload": {
					"inspection_templates": [EV_UNDERCARRIAGE_INSPECTION],
				},
			},
			{
				"id": "starter_suppliers",
				"label": "Starter Ontario EV Parts Suppliers",
				"description": "Adds 6 common Ontario EV/Tesla parts and shop supply vendors (Tesla, Worldpac, NAPA, etc.).",
				"payload": {
					"suppliers": STARTER_SUPPLIERS,
				},
			},
			{
				"id": "starter_service_packages",
				"label": "Starter EV Service Packages (Self-Serve Ready)",
				"description": "Adds 6 flat-rate & diagnostic EV Service Packages configured for self-serve booking.",
				"payload": {
					"service_packages": STARTER_SERVICE_PACKAGES,
				},
			},
			{
				"id": "ontario_ev_starter_catalog",
				"label": "Complete Ontario EV Starter Catalog",
				"description": "Seeds the EV undercarriage inspection template, starter suppliers, and starter service packages together.",
				"payload": {
					"inspection_templates": [EV_UNDERCARRIAGE_INSPECTION],
					"suppliers": STARTER_SUPPLIERS,
					"service_packages": STARTER_SERVICE_PACKAGES,
				},
			},
		]

	def _desk_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"inspection_template": build_desk_link("Inspection Template", site_url=ctx.site_url),
			"supplier": build_desk_link("Supplier", site_url=ctx.site_url),
			"service_package": build_desk_link("Service Package", site_url=ctx.site_url),
		}

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		return {
			"step_id": self.meta.id,
			"meta": self.meta.to_dict(),
			"fields": [
				{
					"fieldname": "inspection_templates",
					"label": "Inspection Templates",
					"fieldtype": "JSON",
					"default": [],
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"description": "List/JSON of {template_name, is_active, description, items: [{inspection_description, category, photo_required, default_notes}]}.",
				},
				{
					"fieldname": "suppliers",
					"label": "Parts Suppliers",
					"fieldtype": "JSON",
					"default": [],
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"description": "List/JSON/CSV of {supplier_name, supplier_group, supplier_type, country, default_currency}.",
				},
				{
					"fieldname": "service_packages",
					"label": "Service Packages",
					"fieldtype": "JSON",
					"default": [],
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"description": "List/JSON/CSV of Service Package definitions including self-serve pricing & equipment_tags.",
				},
			],
			"secret_fields": [],
			"presets": self.get_presets(ctx),
			"desk_urls": self._desk_urls(ctx),
		}

	def inspect(self, ctx: SetupContext) -> StepInspection:
		checks: list[CheckResult] = []

		template_count = (
			frappe.db.count("Inspection Template", filters={"is_active": 1})
			if frappe.db.exists("DocType", "Inspection Template")
			else 0
		)
		has_walkaround = bool(
			frappe.db.exists("Inspection Template", _DEFAULT_WALKAROUND_TEMPLATE)
		)
		if template_count >= 1:
			walkaround_note = (
				f" (includes '{_DEFAULT_WALKAROUND_TEMPLATE}')"
				if has_walkaround
				else ""
			)
			checks.append(
				CheckResult(
					code="inspection_templates",
					level=CHECK_PASS,
					message=f"Found {template_count} active Inspection Template(s){walkaround_note}.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="inspection_templates",
					level=CHECK_FAIL,
					message="No active Inspection Template is configured.",
					remediation="Apply preset 'ev_undercarriage_inspection' or run Tier-0 bootstrap for Standard Vehicle Walkaround.",
				)
			)

		supplier_count = (
			frappe.db.count("Supplier", filters={"disabled": 0})
			if frappe.db.exists("DocType", "Supplier")
			else 0
		)
		checks.append(
			CheckResult(
				code="suppliers",
				level=CHECK_PASS if supplier_count >= 1 else CHECK_INFO,
				message=f"Found {supplier_count} active Supplier(s).",
				remediation=None if supplier_count >= 1 else "Optionally apply preset 'starter_suppliers'.",
			)
		)

		package_count = (
			frappe.db.count("Service Package")
			if frappe.db.exists("DocType", "Service Package")
			else 0
		)
		self_serve_count = (
			frappe.db.count("Service Package", filters={"allow_self_serve_booking": 1})
			if frappe.db.exists("DocType", "Service Package")
			else 0
		)
		checks.append(
			CheckResult(
				code="service_packages",
				level=CHECK_PASS if package_count >= 1 else CHECK_INFO,
				message=(
					f"Found {package_count} Service Package(s) "
					f"({self_serve_count} enabled for self-serve booking)."
				),
				remediation=None if package_count >= 1 else "Optionally apply preset 'starter_service_packages'.",
			)
		)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		status = STEP_STATUS_INCOMPLETE if has_fail else STEP_STATUS_COMPLETE
		summary = (
			"At least one active Inspection Template is required."
			if has_fail
			else (
				f"Catalog ready: {template_count} Inspection Template(s), "
				f"{supplier_count} Supplier(s), {package_count} Service Package(s) "
				f"({self_serve_count} self-serve)."
			)
		)

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"inspection_templates": template_count,
				"suppliers": supplier_count,
				"service_packages": package_count,
				"self_serve_service_packages": self_serve_count,
			},
			desk_urls=self._desk_urls(ctx),
		)

	def apply(
		self,
		payload: dict[str, Any],
		ctx: SetupContext,
		*,
		dry_run: bool = False,
	) -> StepApplyResult:
		created: list[str] = []
		updated: list[str] = []
		unchanged: list[str] = []
		warnings: list[str] = []

		# 1. Upsert Inspection Templates
		if payload.get("inspection_templates"):
			for rec in normalize_records_list(payload["inspection_templates"]):
				tmpl_name = str(rec.get("template_name") or rec.get("name") or "").strip()
				if not tmpl_name:
					continue
				is_active = cint(rec.get("is_active", 1))
				desc = str(rec.get("description") or "").strip()
				raw_items = rec.get("items") or []
				desired_items: list[dict[str, Any]] = []
				for item in raw_items:
					if not isinstance(item, dict):
						continue
					item_desc = str(item.get("inspection_description") or "").strip()
					if not item_desc:
						continue
					desired_items.append({
						"inspection_description": item_desc,
						"category": str(item.get("category") or "").strip(),
						"photo_required": cint(item.get("photo_required", 0)),
						"default_notes": str(item.get("default_notes") or "").strip(),
					})

				if not frappe.db.exists("Inspection Template", tmpl_name):
					doc = frappe.get_doc({
						"doctype": "Inspection Template",
						"template_name": tmpl_name,
						"is_active": is_active,
						"description": desc,
						"items": desired_items,
					})
					doc.insert(ignore_permissions=True)
					created.append(f"Inspection Template: {tmpl_name}")
				else:
					doc = frappe.get_doc("Inspection Template", tmpl_name)
					dirty = False
					if cint(doc.is_active) != is_active:
						doc.is_active = is_active
						dirty = True
					if "description" in rec and (doc.description or "").strip() != desc:
						doc.description = desc
						dirty = True
					if "items" in rec:
						cur_items = [
							{
								"inspection_description": str(r.inspection_description or "").strip(),
								"category": str(r.category or "").strip(),
								"photo_required": cint(r.photo_required),
								"default_notes": str(r.default_notes or "").strip(),
							}
							for r in (doc.items or [])
						]
						if cur_items != desired_items:
							doc.set("items", [])
							for row in desired_items:
								doc.append("items", row)
							dirty = True
					if dirty:
						doc.save(ignore_permissions=True)
						updated.append(f"Inspection Template: {tmpl_name}")
					else:
						unchanged.append(f"Inspection Template: {tmpl_name}")

		# 2. Upsert Suppliers
		if payload.get("suppliers"):
			for rec in normalize_records_list(payload["suppliers"]):
				sup_name = str(rec.get("supplier_name") or rec.get("name") or "").strip()
				if not sup_name:
					continue
				sup_group = _resolve_supplier_group(rec.get("supplier_group"))
				sup_type = str(rec.get("supplier_type") or "Company").strip()
				country = str(rec.get("country") or "Canada").strip()
				default_currency = str(rec.get("default_currency") or "CAD").strip()

				existing = frappe.db.get_value("Supplier", {"supplier_name": sup_name}, "name") or (
					sup_name if frappe.db.exists("Supplier", sup_name) else None
				)
				if not existing:
					sup_data: dict[str, Any] = {
						"doctype": "Supplier",
						"supplier_name": sup_name,
						"supplier_group": sup_group,
						"supplier_type": sup_type,
						"default_currency": default_currency,
					}
					if country and frappe.db.exists("Country", country):
						sup_data["country"] = country
					sup_doc = frappe.get_doc(sup_data)
					sup_doc.insert(ignore_permissions=True)
					created.append(f"Supplier: {sup_name}")
				else:
					sup_doc = frappe.get_doc("Supplier", existing)
					dirty = False
					if "supplier_group" in rec and (sup_doc.supplier_group or "") != sup_group:
						sup_doc.supplier_group = sup_group
						dirty = True
					if "supplier_type" in rec and (sup_doc.supplier_type or "") != sup_type:
						sup_doc.supplier_type = sup_type
						dirty = True
					if "default_currency" in rec and (sup_doc.default_currency or "") != default_currency:
						sup_doc.default_currency = default_currency
						dirty = True
					if "country" in rec and country and frappe.db.exists("Country", country):
						if (sup_doc.country or "") != country:
							sup_doc.country = country
							dirty = True
					if dirty:
						sup_doc.save(ignore_permissions=True)
						updated.append(f"Supplier: {sup_name}")
					else:
						unchanged.append(f"Supplier: {sup_name}")

		# 3. Upsert Service Packages
		if payload.get("service_packages"):
			for rec in normalize_records_list(payload["service_packages"]):
				pkg_name = str(rec.get("package_name") or rec.get("name") or "").strip()
				if not pkg_name:
					continue

				tag_names = _normalize_tag_list(rec.get("equipment_tags"))
				for tag_name in tag_names:
					if not frappe.db.exists("Equipment Tag", tag_name):
						frappe.get_doc({
							"doctype": "Equipment Tag",
							"tag_name": tag_name,
						}).insert(ignore_permissions=True)
						created.append(f"Equipment Tag: {tag_name}")

				existing_pkg = frappe.db.get_value(
					"Service Package", {"package_name": pkg_name}, "name"
				)
				if not existing_pkg:
					pkg_data: dict[str, Any] = {
						"doctype": "Service Package",
						"package_name": pkg_name,
					}
					for fn in _PACKAGE_SCALAR_FIELDS:
						if fn not in rec or rec[fn] is None:
							continue
						if fn in _PACKAGE_INT_FIELDS:
							pkg_data[fn] = cint(rec[fn])
						elif fn in _PACKAGE_FLOAT_FIELDS:
							pkg_data[fn] = flt(rec[fn])
						else:
							pkg_data[fn] = rec[fn]
					if tag_names:
						pkg_data["equipment_tags"] = [{"equipment_tag": t} for t in tag_names]
					pkg_doc = frappe.get_doc(pkg_data)
					pkg_doc.insert(ignore_permissions=True)
					created.append(f"Service Package: {pkg_name}")
				else:
					pkg_doc = frappe.get_doc("Service Package", existing_pkg)
					dirty = False
					for fn in _PACKAGE_SCALAR_FIELDS:
						if fn not in rec or rec[fn] is None:
							continue
						if fn in _PACKAGE_INT_FIELDS:
							val = cint(rec[fn])
							if cint(pkg_doc.get(fn)) != val:
								pkg_doc.set(fn, val)
								dirty = True
						elif fn in _PACKAGE_FLOAT_FIELDS:
							val = flt(rec[fn])
							if abs(flt(pkg_doc.get(fn)) - val) > 1e-9:
								pkg_doc.set(fn, val)
								dirty = True
						else:
							val = str(rec[fn]).strip()
							if str(pkg_doc.get(fn) or "").strip() != val:
								pkg_doc.set(fn, val)
								dirty = True
					if "equipment_tags" in rec:
						cur_tags = [
							row.equipment_tag
							for row in (pkg_doc.equipment_tags or [])
							if row.equipment_tag
						]
						if cur_tags != tag_names:
							pkg_doc.set("equipment_tags", [])
							for t in tag_names:
								pkg_doc.append("equipment_tags", {"equipment_tag": t})
							dirty = True
					if dirty:
						pkg_doc.save(ignore_permissions=True)
						updated.append(f"Service Package: {pkg_name}")
					else:
						unchanged.append(f"Service Package: {pkg_name}")

		return StepApplyResult(
			step_id=self.meta.id,
			dry_run=dry_run,
			applied=bool(created or updated),
			created=tuple(created),
			updated=tuple(updated),
			unchanged=tuple(unchanged),
			warnings=tuple(warnings),
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		templates_out: list[dict[str, Any]] = []
		if frappe.db.exists("DocType", "Inspection Template"):
			names = frappe.get_all(
				"Inspection Template",
				filters={"template_name": ["!=", _DEFAULT_WALKAROUND_TEMPLATE]},
				pluck="name",
				order_by="template_name asc",
			)
			for name in names:
				doc = frappe.get_doc("Inspection Template", name)
				templates_out.append({
					"template_name": doc.template_name,
					"is_active": cint(doc.is_active),
					"description": doc.description or "",
					"items": [
						{
							"inspection_description": row.inspection_description or "",
							"category": row.category or "",
							"photo_required": cint(row.photo_required),
							"default_notes": row.default_notes or "",
						}
						for row in (doc.items or [])
					],
				})

		suppliers_out = (
			[
				dict(row)
				for row in frappe.get_all(
					"Supplier",
					filters={"disabled": 0},
					fields=[
						"supplier_name",
						"supplier_group",
						"supplier_type",
						"country",
						"default_currency",
					],
					order_by="supplier_name asc",
				)
			]
			if frappe.db.exists("DocType", "Supplier")
			else []
		)

		packages_out: list[dict[str, Any]] = []
		if frappe.db.exists("DocType", "Service Package"):
			pkg_names = frappe.get_all(
				"Service Package",
				pluck="name",
				order_by="package_name asc",
			)
			for name in pkg_names:
				doc = frappe.get_doc("Service Package", name)
				row_out: dict[str, Any] = {"package_name": doc.package_name}
				for fn in _PACKAGE_SCALAR_FIELDS:
					row_out[fn] = doc.get(fn)
				row_out["equipment_tags"] = [
					{"equipment_tag": r.equipment_tag}
					for r in (doc.equipment_tags or [])
					if r.equipment_tag
				]
				packages_out.append(row_out)

		return {
			"inspection_templates": templates_out,
			"suppliers": suppliers_out,
			"service_packages": packages_out,
		}
