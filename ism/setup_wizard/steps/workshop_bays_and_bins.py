# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Step 50: Workshop Service Bays, Equipment Tags, Warehouse, Storage Bins, and capacity verification."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import frappe
from frappe.utils import cint, getdate, today

from ism.api.parts_stock import _find_company_warehouse, _get_stock_settings_default_warehouse
from ism.scheduling.capacity import ShopCapacityWindow
from ism.setup_wizard.base import build_desk_link, normalize_records_list
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_INFO,
	CHECK_PASS,
	CHECK_WARN,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_WARNING,
	TIER_CORE,
	CheckResult,
	SetupContext,
	StepApplyResult,
	StepInspection,
	StepMeta,
	StepVerifyResult,
)
from ism.setup_wizard.presets import EV_EQUIPMENT_TAGS, STARTER_STORAGE_BINS
from ism.setup_wizard.settings_helper import SettingsSectionStep

STARTER_SERVICE_BAYS: list[dict[str, Any]] = [
	{
		"bay_name": "Bay 1 - EV Lift & Diagnostics",
		"is_active": 1,
		"description": "Primary 2-post EV lift bay with Tesla Toolbox diagnostics.",
		"equipment": [
			{"equipment_tag": "Lift"},
			{"equipment_tag": "Tesla Toolbox Diagnostics"},
		],
	},
	{
		"bay_name": "Bay 2 - Alignment & Suspension",
		"is_active": 1,
		"description": "4-post optical alignment rack and suspension service bay.",
		"equipment": [
			{"equipment_tag": "Lift"},
			{"equipment_tag": "Alignment Rack"},
			{"equipment_tag": "ADAS Calibration"},
		],
	},
	{
		"bay_name": "Bay 3 - HV Battery & Thermal",
		"is_active": 1,
		"description": "High-voltage isolation bay with battery pack lift table and R-1234yf A/C machine.",
		"equipment": [
			{"equipment_tag": "Lift"},
			{"equipment_tag": "EV High Voltage"},
			{"equipment_tag": "Battery Pack Lift Table"},
			{"equipment_tag": "A/C Recovery (R-1234yf)"},
		],
	},
	{
		"bay_name": "Bay 4 - Tires & Quick Service",
		"is_active": 1,
		"description": "Seasonal tire swap, wheel balancing, and brake maintenance bay.",
		"equipment": [
			{"equipment_tag": "Lift"},
			{"equipment_tag": "Tire Changer & Balancer"},
		],
	},
]


def _normalize_equipment_tag_names(raw: Any) -> list[str]:
	if not raw:
		return []
	if isinstance(raw, str):
		return [part.strip() for part in raw.split(",") if part.strip()]
	names: list[str] = []
	if isinstance(raw, (list, tuple)):
		for item in raw:
			if isinstance(item, str) and item.strip():
				names.append(item.strip())
			elif isinstance(item, dict):
				tag = str(item.get("equipment_tag") or item.get("tag_name") or "").strip()
				if tag:
					names.append(tag)
	# Deduplicate preserving order
	seen: set[str] = set()
	out: list[str] = []
	for name in names:
		if name not in seen:
			seen.add(name)
			out.append(name)
	return out


def _resolve_default_warehouse(company: str | None) -> tuple[str, str]:
	"""Resolve default warehouse via Stock Settings (if field exists) or active company Warehouse fallback."""
	stock_wh = (_get_stock_settings_default_warehouse() or "").strip()
	if stock_wh:
		return stock_wh, "Stock Settings.default_warehouse"
	comp_wh = (_find_company_warehouse(company) or "").strip() if company else ""
	if comp_wh:
		return comp_wh, "Company Warehouse"
	fallback_wh = (
		frappe.db.get_value("Warehouse", {"is_group": 0, "disabled": 0}, "name") or ""
	).strip()
	if fallback_wh:
		return fallback_wh, "Warehouse"
	return "", ""


def _ensure_equipment_tag(tag_name: str, description: str = "") -> str:
	"""Upsert Equipment Tag and return 'created', 'updated', or 'unchanged'."""
	if not frappe.db.exists("Equipment Tag", tag_name):
		frappe.get_doc({
			"doctype": "Equipment Tag",
			"tag_name": tag_name,
			"description": description,
		}).insert(ignore_permissions=True)
		return "created"
	if description:
		doc = frappe.get_doc("Equipment Tag", tag_name)
		if (doc.description or "").strip() != description.strip():
			doc.description = description
			doc.save(ignore_permissions=True)
			return "updated"
	return "unchanged"


class WorkshopBaysAndBinsStep(SettingsSectionStep):
	meta = StepMeta(
		id="workshop_bays_and_bins",
		title="Workshop Bays, Equipment & Storage Bins",
		phase="workshop",
		order=50,
		schema_version=1,
		tier=TIER_CORE,
		depends_on=("erpnext_foundation", "shop_policy_pricing"),
		owner_domain="ism",
		managed_settings_sections=("sb_inventory_dimensions",),
		allow_skip=False,
		description=(
			"Configure workshop Equipment Tags, Service Bays, default parts Warehouse, "
			"Storage Bins, and inventory bin-on-receive enforcement."
		),
	)

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "ev_equipment_tags",
				"label": "EV & Tesla Shop Equipment Tags",
				"description": "Seeds 8 standard EV/Tesla shop equipment tags (Lift, EV High Voltage, Alignment Rack, etc.).",
				"payload": {
					"equipment_tags": EV_EQUIPMENT_TAGS,
				},
			},
			{
				"id": "starter_storage_bins",
				"label": "Starter Parts Room Storage Bins",
				"description": "Seeds 8 standard parts room Storage Bins (Receiving, Standard A1/A2, HV Hazmat, Bulk, Overflow).",
				"payload": {
					"storage_bins": STARTER_STORAGE_BINS,
				},
			},
			{
				"id": "starter_bays",
				"label": "Starter 4-Bay EV Workshop Layout",
				"description": "Seeds EV Equipment Tags and 4 starter Service Bays (Lift/Diag, Alignment, HV Battery, Tires).",
				"payload": {
					"equipment_tags": EV_EQUIPMENT_TAGS,
					"service_bays": STARTER_SERVICE_BAYS,
				},
			},
		]

	def _desk_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"ism_settings": build_desk_link("ISM Settings", site_url=ctx.site_url),
			"service_bay": build_desk_link("Service Bay", site_url=ctx.site_url),
			"equipment_tag": build_desk_link("Equipment Tag", site_url=ctx.site_url),
			"storage_bin": build_desk_link("Storage Bin", site_url=ctx.site_url),
			"warehouse": build_desk_link("Warehouse", site_url=ctx.site_url),
			"stock_settings": build_desk_link("Stock Settings", site_url=ctx.site_url),
		}

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		schema = super().get_schema(ctx)
		default_wh, _wh_src = _resolve_default_warehouse(ctx.company)
		schema["fields"].extend([
			{
				"fieldname": "default_warehouse",
				"label": "Default Parts Warehouse",
				"fieldtype": "Link",
				"options": "Warehouse",
				"reqd": False,
				"read_only": False,
				"is_secret": False,
				"value": default_wh,
			},
			{
				"fieldname": "equipment_tags",
				"label": "Equipment Tags",
				"fieldtype": "JSON",
				"default": [],
				"reqd": False,
				"read_only": False,
				"is_secret": False,
				"description": "List/JSON/CSV of {tag_name, description}.",
			},
			{
				"fieldname": "service_bays",
				"label": "Service Bays",
				"fieldtype": "JSON",
				"default": [],
				"reqd": False,
				"read_only": False,
				"is_secret": False,
				"description": "List/JSON/CSV of {bay_name, is_active, description, equipment}.",
			},
			{
				"fieldname": "storage_bins",
				"label": "Storage Bins",
				"fieldtype": "JSON",
				"default": [],
				"reqd": False,
				"read_only": False,
				"is_secret": False,
				"description": "List/JSON/CSV of {bin_code, warehouse, bin_type, is_active, zone, aisle, rack, shelf, description}.",
			},
		])
		schema["desk_urls"] = self._desk_urls(ctx)
		return schema

	def inspect(self, ctx: SetupContext) -> StepInspection:
		checks: list[CheckResult] = []

		active_bays = (
			frappe.db.count("Service Bay", filters={"is_active": 1})
			if frappe.db.exists("DocType", "Service Bay")
			else 0
		)
		if active_bays >= 1:
			checks.append(
				CheckResult(
					code="active_service_bays",
					level=CHECK_PASS,
					message=f"Found {active_bays} active Service Bay(s).",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="active_service_bays",
					level=CHECK_FAIL,
					message="No active Service Bay is configured.",
					remediation="Apply preset 'starter_bays' or add at least one active Service Bay.",
				)
			)

		default_wh, wh_source = _resolve_default_warehouse(ctx.company)
		valid_wh = bool(
			default_wh
			and frappe.db.exists("Warehouse", {"name": default_wh, "is_group": 0, "disabled": 0})
		)
		if valid_wh:
			checks.append(
				CheckResult(
					code="default_warehouse",
					level=CHECK_PASS,
					message=f"Default parts warehouse is '{default_wh}' (via {wh_source}).",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="default_warehouse",
					level=CHECK_FAIL,
					message=(
						f"Configured default warehouse '{default_wh}' is invalid or disabled."
						if default_wh
						else "No active non-group Warehouse is configured."
					),
					remediation="Configure a valid active non-group Warehouse for the Company.",
				)
			)

		tag_count = (
			frappe.db.count("Equipment Tag")
			if frappe.db.exists("DocType", "Equipment Tag")
			else 0
		)
		checks.append(
			CheckResult(
				code="equipment_tags",
				level=CHECK_PASS if tag_count >= 1 else CHECK_INFO,
				message=f"Found {tag_count} Equipment Tag(s).",
			)
		)

		active_bins = (
			frappe.db.count("Storage Bin", filters={"is_active": 1})
			if frappe.db.exists("DocType", "Storage Bin")
			else 0
		)
		require_bin = cint(
			frappe.db.get_single_value("ISM Settings", "require_bin_on_receive") or 0
		)
		if require_bin == 1 and active_bins == 0:
			checks.append(
				CheckResult(
					code="storage_bins",
					level=CHECK_WARN,
					message="require_bin_on_receive is enabled in ISM Settings, but 0 active Storage Bins exist.",
					remediation="Apply preset 'starter_storage_bins' or create active Storage Bins before receiving parts.",
				)
			)
		elif active_bins >= 1:
			checks.append(
				CheckResult(
					code="storage_bins",
					level=CHECK_PASS,
					message=f"Found {active_bins} active Storage Bin(s) (require_bin_on_receive={require_bin}).",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="storage_bins",
					level=CHECK_INFO,
					message="No active Storage Bins configured yet (require_bin_on_receive is disabled).",
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)
		if has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = "Workshop bays or default parts warehouse require configuration."
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = "Workshop bays ready, but Storage Bins are needed for bin-on-receive enforcement."
		else:
			status = STEP_STATUS_COMPLETE
			summary = (
				f"Workshop ready: {active_bays} active bay(s), {tag_count} tag(s), "
				f"{active_bins} active Storage Bin(s)."
			)

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"active_service_bays": active_bays,
				"equipment_tags": tag_count,
				"active_storage_bins": active_bins,
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
		settings_res = super().apply(payload, ctx, dry_run=dry_run)
		created = list(settings_res.created)
		updated = list(settings_res.updated)
		unchanged = list(settings_res.unchanged)
		warnings = list(settings_res.warnings)

		# 1. Optional default_warehouse
		req_wh = str(payload.get("default_warehouse") or "").strip()
		if req_wh:
			if not frappe.db.exists("Warehouse", {"name": req_wh, "is_group": 0, "disabled": 0}):
				warnings.append(f"Warehouse '{req_wh}' does not exist or is disabled; skipped.")
			elif frappe.db.exists("DocType", "Stock Settings") and frappe.get_meta("Stock Settings").has_field("default_warehouse"):
				stock_settings = frappe.get_single("Stock Settings")
				if (stock_settings.default_warehouse or "").strip() != req_wh:
					stock_settings.default_warehouse = req_wh
					stock_settings.save(ignore_permissions=True)
					updated.append(f"Stock Settings.default_warehouse={req_wh}")
				else:
					unchanged.append(f"Stock Settings.default_warehouse={req_wh}")
			elif ctx.company and frappe.db.has_column("Company", "default_warehouse"):
				cur_wh = (frappe.db.get_value("Company", ctx.company, "default_warehouse") or "").strip()
				if cur_wh != req_wh:
					frappe.db.set_value("Company", ctx.company, "default_warehouse", req_wh)
					updated.append(f"Company.default_warehouse={req_wh}")
				else:
					unchanged.append(f"Company.default_warehouse={req_wh}")
			else:
				unchanged.append(f"Warehouse: {req_wh}")

		default_wh, _wh_src = _resolve_default_warehouse(ctx.company)
		if req_wh and frappe.db.exists("Warehouse", {"name": req_wh, "is_group": 0, "disabled": 0}):
			default_wh = req_wh

		# 2. Upsert Equipment Tags
		if payload.get("equipment_tags"):
			for rec in normalize_records_list(payload["equipment_tags"]):
				tag_name = str(rec.get("tag_name") or rec.get("name") or "").strip()
				if not tag_name:
					continue
				desc = str(rec.get("description") or "").strip()
				res = _ensure_equipment_tag(tag_name, desc)
				if res == "created":
					created.append(f"Equipment Tag: {tag_name}")
				elif res == "updated":
					updated.append(f"Equipment Tag: {tag_name}")
				else:
					unchanged.append(f"Equipment Tag: {tag_name}")

		# 3. Upsert Service Bays (with Table MultiSelect equipment tags)
		if payload.get("service_bays"):
			for rec in normalize_records_list(payload["service_bays"]):
				bay_name = str(rec.get("bay_name") or rec.get("name") or "").strip()
				if not bay_name:
					continue
				is_active = cint(rec.get("is_active", 1))
				desc = str(rec.get("description") or "").strip()
				raw_equip = rec.get("equipment") if "equipment" in rec else rec.get("equipment_tags")
				tag_names = _normalize_equipment_tag_names(raw_equip)

				for tag_name in tag_names:
					if _ensure_equipment_tag(tag_name) == "created":
						created.append(f"Equipment Tag: {tag_name}")

				if not frappe.db.exists("Service Bay", bay_name):
					bay_doc = frappe.get_doc({
						"doctype": "Service Bay",
						"bay_name": bay_name,
						"is_active": is_active,
						"description": desc,
						"equipment": [{"equipment_tag": t} for t in tag_names],
					})
					bay_doc.insert(ignore_permissions=True)
					created.append(f"Service Bay: {bay_name}")
				else:
					bay_doc = frappe.get_doc("Service Bay", bay_name)
					dirty = False
					if cint(bay_doc.is_active) != is_active:
						bay_doc.is_active = is_active
						dirty = True
					if "description" in rec and (bay_doc.description or "").strip() != desc:
						bay_doc.description = desc
						dirty = True
					if "equipment" in rec or "equipment_tags" in rec:
						cur_tags = [row.equipment_tag for row in (bay_doc.equipment or []) if row.equipment_tag]
						if cur_tags != tag_names:
							bay_doc.set("equipment", [])
							for t in tag_names:
								bay_doc.append("equipment", {"equipment_tag": t})
							dirty = True
					if dirty:
						bay_doc.save(ignore_permissions=True)
						updated.append(f"Service Bay: {bay_name}")
					else:
						unchanged.append(f"Service Bay: {bay_name}")

		# 4. Upsert Storage Bins
		if payload.get("storage_bins"):
			for rec in normalize_records_list(payload["storage_bins"]):
				bin_code = str(rec.get("bin_code") or rec.get("name") or "").strip()
				if not bin_code:
					continue
				warehouse = str(rec.get("warehouse") or default_wh or "").strip()
				bin_type = str(rec.get("bin_type") or "Standard").strip()
				is_active = cint(rec.get("is_active", 1))

				fields_to_sync = {
					"warehouse": warehouse or None,
					"bin_type": bin_type,
					"is_active": is_active,
					"zone": str(rec.get("zone") or "").strip(),
					"aisle": str(rec.get("aisle") or "").strip(),
					"rack": str(rec.get("rack") or "").strip(),
					"shelf": str(rec.get("shelf") or "").strip(),
					"description": str(rec.get("description") or "").strip(),
				}

				if not frappe.db.exists("Storage Bin", bin_code):
					bin_doc = frappe.get_doc({
						"doctype": "Storage Bin",
						"bin_code": bin_code,
						**fields_to_sync,
					})
					bin_doc.insert(ignore_permissions=True)
					created.append(f"Storage Bin: {bin_code}")
				else:
					bin_doc = frappe.get_doc("Storage Bin", bin_code)
					dirty = False
					for fn, val in fields_to_sync.items():
						if fn == "is_active":
							if cint(bin_doc.get(fn)) != cint(val):
								bin_doc.set(fn, cint(val))
								dirty = True
						elif fn in rec or (fn == "warehouse" and not bin_doc.get("warehouse") and val):
							if str(bin_doc.get(fn) or "").strip() != str(val or "").strip():
								bin_doc.set(fn, val)
								dirty = True
					if dirty:
						bin_doc.save(ignore_permissions=True)
						updated.append(f"Storage Bin: {bin_code}")
					else:
						unchanged.append(f"Storage Bin: {bin_code}")

		return StepApplyResult(
			step_id=self.meta.id,
			dry_run=dry_run,
			applied=bool(created or updated),
			created=tuple(created),
			updated=tuple(updated),
			unchanged=tuple(unchanged),
			warnings=tuple(warnings),
		)

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		inspection = self.inspect(ctx)
		checks = list(inspection.checks)

		# Read-only ShopCapacityWindow check for the next open shop day
		start_dt = getdate(today()) + timedelta(days=1)
		end_dt = start_dt + timedelta(days=14)
		found_slot_date: str | None = None
		found_slot_time: str | None = None
		try:
			window = ShopCapacityWindow(start_date=start_dt, end_date=end_dt)
			for candidate_date in window.iter_dates():
				if window.is_holiday(candidate_date):
					continue
				slots = window.slots_for_date(candidate_date, duration_minutes=60, max_slots=1)
				if slots:
					found_slot_date = candidate_date.isoformat()
					first_slot = slots[0]
					found_slot_time = str(
						first_slot.get("start_time")
						or first_slot.get("time")
						or first_slot.get("slot_start")
						or "available"
					)
					break

			if found_slot_date:
				checks.append(
					CheckResult(
						code="shop_capacity_slot_check",
						level=CHECK_PASS,
						message=(
							f"Verified open 60-min shop capacity slot on {found_slot_date} "
							f"({found_slot_time})."
						),
					)
				)
			else:
				checks.append(
					CheckResult(
						code="shop_capacity_slot_check",
						level=CHECK_FAIL,
						message=(
							f"No open 60-min capacity slot found between {start_dt.isoformat()} "
							f"and {end_dt.isoformat()}."
						),
						remediation=(
							"Verify at least one active Service Bay, one active Technician Employee, "
							"and valid operating hours / Holiday List."
						),
					)
				)
		except Exception as exc:
			checks.append(
				CheckResult(
					code="shop_capacity_slot_check",
					level=CHECK_FAIL,
					message=f"ShopCapacityWindow availability check failed: {exc}",
					remediation="Verify ISM Settings scheduling fields, Service Bays, and Technician Employees.",
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		passed = not has_fail
		summary = (
			f"Workshop bays, bins, and live capacity verified (next open slot: {found_slot_date})."
			if passed
			else "Workshop bays, bins, or live capacity verification failed."
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=summary,
			checks=tuple(checks),
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		exported = super().export_blueprint(ctx)

		tags = frappe.get_all(
			"Equipment Tag",
			fields=["tag_name", "description"],
			order_by="tag_name asc",
		) if frappe.db.exists("DocType", "Equipment Tag") else []

		bays_out: list[dict[str, Any]] = []
		if frappe.db.exists("DocType", "Service Bay"):
			bay_names = frappe.get_all("Service Bay", pluck="name", order_by="bay_name asc")
			for name in bay_names:
				doc = frappe.get_doc("Service Bay", name)
				bays_out.append({
					"bay_name": doc.bay_name,
					"is_active": cint(doc.is_active),
					"description": doc.description or "",
					"equipment": [
						{"equipment_tag": row.equipment_tag}
						for row in (doc.equipment or [])
						if row.equipment_tag
					],
				})

		bins_out = frappe.get_all(
			"Storage Bin",
			fields=[
				"bin_code",
				"warehouse",
				"bin_type",
				"is_active",
				"zone",
				"aisle",
				"rack",
				"shelf",
				"description",
			],
			order_by="bin_code asc",
		) if frappe.db.exists("DocType", "Storage Bin") else []

		exported["equipment_tags"] = [dict(t) for t in tags]
		exported["service_bays"] = bays_out
		exported["storage_bins"] = [dict(b) for b in bins_out]
		return exported
