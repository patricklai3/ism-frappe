# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Layer 2 Lifecycle Transaction Engine: SimContext, SimBuilder, and historical telemetry."""

from __future__ import annotations

import datetime as _dt
import json
import math
import random
import re
from typing import Any, Callable, Optional, Sequence, Union

import frappe
from frappe.utils import flt

from ism.api.agent_procurement import create_purchase_invoice, submit_procurement_orders
from ism.api.parts_procurement import get_repair_order_procurement_data
from ism.ism.doctype.shop_action_item.shop_action_item import create_shop_action_item
from ism.scheduling.estimation_light import get_sigma_for_item
from ism.scheduling.time_utils import _default_is_holiday, get_shop_settings
from ism.testing.catalog import CATALOG_SEED, get_master_manifest
from ism.testing.manifest import SimManifest

_REL_OFFSET_RE = re.compile(r"^T([+-]\d+)d?$", re.IGNORECASE)

VALID_RO_STAGES = (
	"checked_in",
	"draft_estimate",
	"pending_authorization",
	"authorized_in_stock",
	"waiting_on_parts",
	"partially_approved_with_deferred",
	"scheduled_in_progress",
	"work_completed",
	"billed_paid",
	"closed_released",
)

_HIST_PACKAGES: tuple[tuple[str, str], ...] = (
	("front_brake_overhaul", "Brake"),
	("symmetrical_fore_link", "Suspension"),
	("four_wheel_alignment", "Suspension"),
	("cabin_filter_service", "HVAC"),
	("tire_rotation_balance", "Tire"),
	("hv_isolation_diagnostic", "Diagnostic"),
	("brake_fluid_flush", "Brake"),
)


def _normalize_anchor_date(anchor_date: Optional[str]) -> _dt.date:
	if not anchor_date:
		return _dt.date.fromisoformat(str(frappe.utils.today()))
	if isinstance(anchor_date, _dt.datetime):
		return anchor_date.date()
	if isinstance(anchor_date, _dt.date):
		return anchor_date
	return _dt.date.fromisoformat(str(anchor_date).strip()[:10])


def _normalize_time_str(raw_time: str) -> str:
	text = str(raw_time).strip()
	if len(text) == 5 and text.count(":") == 1:
		return f"{text}:00"
	return text


class SimContext:
	"""Deterministic seeded PRNG + anchor-clock context backed by a SimManifest."""

	def __init__(
		self,
		seed: int = 42,
		anchor_date: Optional[str] = None,
		manifest: Optional[SimManifest] = None,
		track_fn: Optional[Callable[[Any, Optional[str]], Any]] = None,
	):
		self.seed = int(seed)
		self.rng = random.Random(self.seed)
		self._anchor_dt = _normalize_anchor_date(anchor_date)
		self.anchor_date = self._anchor_dt.isoformat()
		self.manifest = manifest or get_master_manifest(seed=CATALOG_SEED, anchor_date=self.anchor_date)
		self.manifest.seed = self.seed
		self.manifest.anchor_date = self.anchor_date
		self.track_fn = track_fn

	@property
	def today(self) -> str:
		return self.anchor_date

	@property
	def tomorrow(self) -> str:
		return self.day(1)

	@property
	def yesterday(self) -> str:
		return self.day(-1)

	def day(self, offset: int) -> str:
		"""Return ISO date string at anchor_date + offset days."""
		return (self._anchor_dt + _dt.timedelta(days=int(offset))).isoformat()

	def rel_date(self, spec: Union[str, int, _dt.date]) -> str:
		"""Resolve 'today', 'tomorrow', 'yesterday', 'T', 'T-N', 'T+N', int offset, or ISO date."""
		if isinstance(spec, int) and not isinstance(spec, bool):
			return self.day(spec)
		if isinstance(spec, _dt.datetime):
			return spec.date().isoformat()
		if isinstance(spec, _dt.date):
			return spec.isoformat()

		text = str(spec).strip()
		low = text.lower()
		if low in ("today", "t"):
			return self.today
		if low == "tomorrow":
			return self.tomorrow
		if low == "yesterday":
			return self.yesterday

		match = _REL_OFFSET_RE.match(text)
		if match:
			return self.day(int(match.group(1)))
		return _dt.date.fromisoformat(text[:10]).isoformat()

	def dt(self, date_spec: Union[str, int, _dt.date] = "today", time_str: str = "09:00:00") -> str:
		"""Format 'YYYY-MM-DD HH:MM:SS' timestamp from relative date and clock time."""
		return f"{self.rel_date(date_spec)} {_normalize_time_str(time_str)}"

	def next_open_day(self, start_offset: int = 1) -> str:
		"""Return the first date at or after anchor + start_offset that is not a configured holiday."""
		offset = int(start_offset)
		holiday_list = get_shop_settings().get("holiday_list")
		for _ in range(365):
			candidate_dt = self._anchor_dt + _dt.timedelta(days=offset)
			if not _default_is_holiday(candidate_dt, holiday_list):
				return candidate_dt.isoformat()
			offset += 1
		return self.day(start_offset)

	def resolve(self, bucket: str, value: Optional[str]) -> Optional[str]:
		"""Resolve a logical handle within manifest.<bucket>, falling back to literal DB name."""
		if not value:
			return None
		bucket_map = getattr(self.manifest, bucket, None)
		if isinstance(bucket_map, dict) and value in bucket_map:
			return bucket_map[value]
		return value

	def track(self, doc: Any, name: Optional[str] = None) -> Any:
		"""Register a created document with optional test teardown tracker."""
		if self.track_fn is not None:
			self.track_fn(doc, name)
		return doc

	def ro(self, handle: Optional[str] = None) -> "SimBuilder":
		"""Start a fluent Repair Order lifecycle builder bound to this context."""
		return SimBuilder(self, handle=handle)


class SimBuilder:
	"""Fluent Repair Order state-machine builder walking real ISM domain APIs."""

	def __init__(self, ctx: SimContext, handle: Optional[str] = None):
		self.ctx = ctx
		self.handle = handle
		self._customer: Optional[str] = None
		self._vehicle: Optional[str] = None
		self._odometer_in: int = 42500
		self._concern: str = ""
		self._posting_date: Optional[str] = None
		self._packages: list[dict[str, Any]] = []
		self._custom_services: list[dict[str, Any]] = []
		self._inspection_cfg: Optional[dict[str, Any]] = None
		self._deposit_cfg: Optional[dict[str, Any]] = None
		self._partial_cfg: Optional[dict[str, Any]] = None
		self._procurement_cfg: Optional[dict[str, Any]] = None
		self._schedule_cfg: Optional[dict[str, Any]] = None
		self._time_logs_cfg: list[dict[str, Any]] = []
		self._supplements_cfg: list[dict[str, Any]] = []
		self._action_items_cfg: list[dict[str, Any]] = []
		self._checkout_cfg: Optional[dict[str, Any]] = None

	def for_customer(
		self,
		customer: Optional[str] = None,
		vehicle: Optional[str] = None,
		odometer_in: int = 42500,
		concern: str = "",
		posting_date: Optional[Union[str, int]] = None,
	) -> "SimBuilder":
		self._customer = customer
		self._vehicle = vehicle
		self._odometer_in = int(odometer_in)
		self._concern = concern
		self._posting_date = self.ctx.rel_date(posting_date) if posting_date is not None else None
		return self

	def with_package(
		self,
		package_or_handle: str,
		side: Optional[str] = None,
		advisor_notes: str = "",
	) -> "SimBuilder":
		self._packages.append({
			"package": package_or_handle,
			"side": side,
			"advisor_notes": advisor_notes,
		})
		return self

	def with_service(
		self,
		service_title: str,
		labor_hours: float = 1.0,
		labor_rate: Optional[float] = None,
		is_flat_rate: int = 0,
		parts: Optional[list[dict[str, Any]]] = None,
	) -> "SimBuilder":
		self._custom_services.append({
			"service_title": service_title,
			"labor_hours": float(labor_hours),
			"labor_rate": labor_rate,
			"is_flat_rate": int(is_flat_rate),
			"parts": parts,
		})
		return self

	def with_inspection(
		self,
		template_or_handle: str = "walkaround",
		default_status: str = "Pass",
	) -> "SimBuilder":
		self._inspection_cfg = {"template": template_or_handle, "default_status": default_status}
		return self

	def with_deposit(
		self,
		amount: float,
		payment_method: str = "Debit / Interac",
		reference_no: Optional[str] = None,
		handle: Optional[str] = None,
	) -> "SimBuilder":
		self._deposit_cfg = {
			"amount": float(amount),
			"payment_method": payment_method,
			"reference_no": reference_no,
			"handle": handle,
		}
		return self

	def with_partial_approval(
		self,
		approved_indices: Sequence[int] = (1,),
		declined_indices: Sequence[int] = (2,),
		safety_hazard_declines: Sequence[int] = (),
		signer_name: Optional[str] = None,
		channel: str = "Remote Web Link",
	) -> "SimBuilder":
		self._partial_cfg = {
			"approved": set(int(i) for i in approved_indices),
			"declined": set(int(i) for i in declined_indices),
			"safety_hazards": set(int(i) for i in safety_hazard_declines),
			"signer_name": signer_name,
			"channel": channel,
		}
		return self

	def with_procurement(
		self,
		order_mode: str = "Purchase Order",
		supplier: Optional[str] = None,
		payment_method: Optional[str] = None,
		paid_amount: Optional[float] = None,
		reference_no: Optional[str] = None,
		receive_via_pi: bool = False,
		bill_no: Optional[str] = None,
		po_handle: Optional[str] = None,
		rfq_handle: Optional[str] = None,
		pi_handle: Optional[str] = None,
		pe_handle: Optional[str] = None,
	) -> "SimBuilder":
		self._procurement_cfg = {
			"order_mode": order_mode,
			"supplier": supplier,
			"payment_method": payment_method,
			"paid_amount": paid_amount,
			"reference_no": reference_no,
			"receive_via_pi": bool(receive_via_pi),
			"bill_no": bill_no,
			"po_handle": po_handle,
			"rfq_handle": rfq_handle,
			"pi_handle": pi_handle,
			"pe_handle": pe_handle,
		}
		return self

	def with_schedule(
		self,
		date: Union[str, int] = "tomorrow",
		start_time: str = "09:00:00",
		duration_minutes: Optional[int] = None,
		bay: Optional[str] = None,
		technician: Optional[str] = None,
		entry_type: str = "Repair",
		status: str = "In Progress",
		handle: Optional[str] = None,
	) -> "SimBuilder":
		self._schedule_cfg = {
			"date": date,
			"start_time": start_time,
			"duration_minutes": duration_minutes,
			"bay": bay,
			"technician": technician,
			"entry_type": entry_type,
			"status": status,
			"handle": handle,
		}
		return self

	def with_time_log(
		self,
		technician: Optional[str] = None,
		hours: float = 1.0,
		from_time: Optional[str] = None,
		to_time: Optional[str] = None,
		notes: str = "Bay service execution",
	) -> "SimBuilder":
		self._time_logs_cfg.append({
			"technician": technician,
			"hours": float(hours),
			"from_time": from_time,
			"to_time": to_time,
			"notes": notes,
		})
		return self

	def with_supplement(
		self,
		service_title: str,
		labor_hours: float = 1.0,
		labor_rate: Optional[float] = None,
		parts: Optional[list[dict[str, Any]]] = None,
		notes: str = "Technician discovery during bay inspection",
		approve: bool = False,
	) -> "SimBuilder":
		self._supplements_cfg.append({
			"service_title": service_title,
			"labor_hours": float(labor_hours),
			"labor_rate": labor_rate,
			"parts": parts,
			"notes": notes,
			"approve": bool(approve),
		})
		return self

	def with_action_item(
		self,
		title: str,
		category: str = "Shop Floor Discrepancy",
		priority: str = "High",
		item_code: Optional[str] = None,
		details: str = "",
		handle: Optional[str] = None,
	) -> "SimBuilder":
		self._action_items_cfg.append({
			"title": title,
			"category": category,
			"priority": priority,
			"item_code": item_code,
			"details": details,
			"handle": handle,
		})
		return self

	def with_checkout(
		self,
		payment_method: str = "Debit / Interac",
		odometer_out: Optional[int] = None,
		payments: Optional[list[dict[str, Any]]] = None,
		paid_amount: Optional[float] = None,
		si_handle: Optional[str] = None,
		pe_handle: Optional[str] = None,
	) -> "SimBuilder":
		self._checkout_cfg = {
			"payment_method": payment_method,
			"odometer_out": odometer_out,
			"payments": payments,
			"paid_amount": paid_amount,
			"si_handle": si_handle,
			"pe_handle": pe_handle,
		}
		return self

	def _resolve_customer_and_vehicle(self) -> tuple[str, str]:
		m = self.ctx.manifest
		veh_name = self.ctx.resolve("vehicles", self._vehicle) if self._vehicle else None
		cust_name = self.ctx.resolve("customers", self._customer) if self._customer else None

		if veh_name and not cust_name:
			cust_name = frappe.db.get_value("Repair Vehicle", veh_name, "customer")
		if cust_name and not veh_name:
			veh_name = frappe.db.get_value("Repair Vehicle", {"customer": cust_name}, "name")
		if not cust_name:
			cust_name = self.ctx.resolve("customers", "retail_1")
		if not veh_name:
			veh_name = self.ctx.resolve("vehicles", "veh_model3_liam")

		if not cust_name or not veh_name:
			raise ValueError(f"Could not resolve customer ({cust_name}) and vehicle ({veh_name}) from manifest")
		return cust_name, veh_name

	def _insert_ro(self, target_stage: str) -> Any:
		cust_name, veh_name = self._resolve_customer_and_vehicle()
		initial_status = "Checked In" if target_stage == "checked_in" else "Draft Estimate"

		ro_dict: dict[str, Any] = {
			"doctype": "Repair Order",
			"company": self.ctx.manifest.company,
			"customer": cust_name,
			"vehicle": veh_name,
			"odometer_in": self._odometer_in,
			"posting_date": self._posting_date or self.ctx.today,
			"check_in_date": self.ctx.dt(self._posting_date or self.ctx.today, "08:30:00"),
			"currency": self.ctx.manifest.currency,
			"status": initial_status,
			"approval_status": "Estimating",
			"customer_concern": self._concern or "Routine EV service inspection and maintenance.",
		}
		if self._inspection_cfg:
			tmpl_name = self.ctx.resolve("inspection_templates", self._inspection_cfg["template"])
			if tmpl_name and frappe.db.exists("Inspection Template", tmpl_name):
				tmpl_doc = frappe.get_cached_doc("Inspection Template", tmpl_name)
				ro_dict["inspection_template"] = tmpl_name
				ro_dict["inspection_items"] = [
					{
						"inspection_description": row.inspection_description,
						"status": self._inspection_cfg["default_status"],
					}
					for row in (tmpl_doc.items or [])
				]

		ro = frappe.get_doc(ro_dict).insert(ignore_permissions=True)
		ro_handle = self.handle or self.ctx.manifest.next_handle("ros", "ro")
		self.ctx.manifest.register("ros", ro_handle, ro.name)
		self.ctx.track(ro)
		return ro

	def _append_requested_services(self, ro: Any, target_stage: str) -> None:
		packages = list(self._packages)
		customs = list(self._custom_services)

		if not packages and not customs and target_stage != "checked_in":
			if target_stage == "waiting_on_parts":
				packages.append({"package": "hv_pyro_fuse_replace", "side": None, "advisor_notes": ""})
			elif target_stage == "partially_approved_with_deferred":
				packages.append({"package": "cabin_filter_service", "side": None, "advisor_notes": ""})
				packages.append({"package": "four_wheel_alignment", "side": None, "advisor_notes": ""})
			else:
				packages.append({"package": "cabin_filter_service", "side": None, "advisor_notes": ""})

		if target_stage == "partially_approved_with_deferred" and (len(packages) + len(customs)) < 2:
			packages.append({"package": "four_wheel_alignment", "side": None, "advisor_notes": ""})

		for pkg_spec in packages:
			pkg_name = self.ctx.resolve("packages", pkg_spec["package"])
			add_kwargs: dict[str, Any] = {"service_package": pkg_name}
			if pkg_spec.get("side"):
				add_kwargs["side"] = pkg_spec["side"]
			if pkg_spec.get("advisor_notes"):
				add_kwargs["advisor_notes"] = pkg_spec["advisor_notes"]
			ro.add_service_package(**add_kwargs)

		if customs:
			default_rate = flt(frappe.db.get_single_value("ISM Settings", "default_labor_rate")) or 140.0
			for svc in customs:
				parts_list = []
				for p in (svc.get("parts") or []):
					p_copy = dict(p)
					if p_copy.get("item_code"):
						p_copy["item_code"] = self.ctx.resolve("items", p_copy["item_code"])
					parts_list.append(p_copy)
				ro.append("services", {
					"service_title": svc["service_title"],
					"status": "Estimating",
					"is_flat_rate": svc["is_flat_rate"],
					"labor_hours": svc["labor_hours"],
					"labor_rate": svc["labor_rate"] if svc["labor_rate"] is not None else default_rate,
					"parts": json.dumps(parts_list),
				})
			ro.save(ignore_permissions=True)
		ro.reload()

	def _record_configured_deposit(self, ro: Any) -> None:
		if not self._deposit_cfg:
			return
		res = ro.record_deposit(
			amount=self._deposit_cfg["amount"],
			payment_method=self._deposit_cfg["payment_method"],
			reference_no=self._deposit_cfg.get("reference_no"),
		)
		pe_name = res.get("payment_entry")
		if pe_name:
			pe_handle = self._deposit_cfg.get("handle") or self.ctx.manifest.next_handle("pes", "pe")
			self.ctx.manifest.register("pes", pe_handle, pe_name)
			self.ctx.track("Payment Entry", pe_name)
		ro.reload()

	def _run_procurement_if_configured(self, ro: Any) -> None:
		if not self._procurement_cfg:
			return
		cfg = self._procurement_cfg
		proc_data = get_repair_order_procurement_data(ro.name)
		deficit_parts = [p for p in (proc_data.get("parts") or []) if p.get("is_deficit")]
		if not deficit_parts:
			return

		resolved_supplier = (
			self.ctx.resolve("suppliers", cfg["supplier"])
			if cfg.get("supplier")
			else None
		)
		parts_payload = []
		for p in deficit_parts:
			sup = (
				resolved_supplier
				or p.get("default_supplier")
				or ((p.get("known_suppliers") or [None])[0])
				or self.ctx.resolve("suppliers", "tesla_oem")
			)
			qty = max(1.0, flt(p.get("qty")) - max(0.0, flt(p.get("stock_qty"))))
			rate = flt(p.get("rate")) or flt(frappe.db.get_value("Item", p["item_code"], "valuation_rate")) or 50.0
			parts_payload.append({
				"item_code": p["item_code"],
				"qty": qty,
				"rate": rate,
				"supplier": sup,
				"order_mode": cfg["order_mode"],
			})

		res = submit_procurement_orders(
			repair_order=ro.name,
			parts_data=parts_payload,
			submit=True,
			company=ro.company,
			payment_method=cfg.get("payment_method"),
			paid_amount=cfg.get("paid_amount"),
			reference_no=cfg.get("reference_no"),
		)
		data = res.get("data") or {}
		po_names = data.get("purchase_orders") or []
		rfq_names = data.get("rfqs") or []
		pe_names = data.get("payment_entries") or []

		for idx, po_name in enumerate(po_names):
			handle = cfg.get("po_handle") if (idx == 0 and cfg.get("po_handle")) else self.ctx.manifest.next_handle("pos", "po")
			self.ctx.manifest.register("pos", handle, po_name)
			self.ctx.track("Purchase Order", po_name)

		for idx, rfq_name in enumerate(rfq_names):
			handle = cfg.get("rfq_handle") if (idx == 0 and cfg.get("rfq_handle")) else self.ctx.manifest.next_handle("rfqs", "rfq")
			self.ctx.manifest.register("rfqs", handle, rfq_name)
			self.ctx.track("Request for Quotation", rfq_name)

		for idx, pe_name in enumerate(pe_names):
			handle = cfg.get("pe_handle") if (idx == 0 and cfg.get("pe_handle")) else self.ctx.manifest.next_handle("pes", "pe")
			self.ctx.manifest.register("pes", handle, pe_name)
			self.ctx.track("Payment Entry", pe_name)

		if cfg.get("receive_via_pi") and po_names:
			effective_bill_date = str(
				min(_normalize_anchor_date(self.ctx.today), _normalize_anchor_date(str(frappe.utils.today())))
			)
			for idx, po_name in enumerate(po_names):
				pi_res = create_purchase_invoice(
					purchase_order=po_name,
					update_stock=True,
					bill_no=cfg.get("bill_no") or f"INV-{po_name}",
					bill_date=effective_bill_date,
					attach_advances=True,
				)
				pi_name = (pi_res.get("data") or {}).get("purchase_invoice")
				if pi_name:
					handle = cfg.get("pi_handle") if (idx == 0 and cfg.get("pi_handle")) else self.ctx.manifest.next_handle("pis", "pi")
					self.ctx.manifest.register("pis", handle, pi_name)
					self.ctx.track("Purchase Invoice", pi_name)

		ro.reload()

	def _apply_partial_approval(self, ro: Any) -> None:
		cfg = self._partial_cfg or {
			"approved": {1},
			"declined": {2},
			"safety_hazards": set(),
			"signer_name": None,
			"channel": "Remote Web Link",
		}
		approvals: dict[str, str] = {}
		declined_titles: set[str] = set()
		for idx, row in enumerate(ro.services, start=1):
			if idx in cfg["declined"]:
				approvals[str(row.idx)] = "Declined"
				if idx in cfg["safety_hazards"]:
					declined_titles.add(row.service_title)
			elif idx in cfg["approved"] or not cfg["approved"]:
				approvals[str(row.idx)] = "Approved"
			else:
				approvals[str(row.idx)] = "Declined"
				if idx in cfg["safety_hazards"]:
					declined_titles.add(row.service_title)

		signer = cfg.get("signer_name") or ro.customer
		ro.authorize_jobs(approvals=approvals, channel=cfg.get("channel", "Remote Web Link"), signer_name=signer)
		ro.reload()

		if declined_titles and ro.vehicle:
			veh_doc = frappe.get_doc("Repair Vehicle", ro.vehicle)
			changed = False
			for row in (veh_doc.deferred_services or []):
				if row.repair_order == ro.name and row.job_title in declined_titles:
					row.safety_hazard = 1
					changed = True
			if changed:
				veh_doc.save(ignore_permissions=True)

	def _apply_schedule_and_bay_work(self, ro: Any) -> None:
		sched = self._schedule_cfg or {
			"date": "tomorrow",
			"start_time": "09:00:00",
			"duration_minutes": None,
			"bay": "main_lift",
			"technician": "marcus",
			"entry_type": "Repair",
			"status": "In Progress",
			"handle": None,
		}
		sched_date = self.ctx.rel_date(sched["date"])
		bay_name = self.ctx.resolve("bays", sched.get("bay") or "main_lift")
		tech_name = self.ctx.resolve("techs", sched.get("technician") or "marcus")
		duration = int(sched.get("duration_minutes") or max(30, round(flt(ro.total_billed_hours or 1.0) * 60)))

		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": sched.get("entry_type") or "Repair",
			"status": sched.get("status") or "In Progress",
			"scheduled_date": sched_date,
			"scheduled_time": _normalize_time_str(sched.get("start_time") or "09:00:00"),
			"estimated_duration": duration,
			"repair_order": ro.name,
			"service_bay": bay_name,
			"assigned_technician": tech_name,
		}).insert(ignore_permissions=True)
		se_handle = sched.get("handle") or self.ctx.manifest.next_handle("schedule_entries", "se")
		self.ctx.manifest.register("schedule_entries", se_handle, se.name)
		self.ctx.track(se)

		ro.reload()
		if ro.status == "Waiting on Parts":
			# Clear waiting hold if caller only requested stocked work or received via procurement.
			ro._update_parts_waiting_status()
			if ro.status == "Waiting on Parts":
				frappe.throw(
					frappe._("Cannot move Repair Order {0} into bay while status is Waiting on Parts.").format(ro.name)
				)
		ro.pick_up_vehicle()
		ro.reload()

		logs = self._time_logs_cfg or [{"technician": tech_name, "hours": 1.0, "from_time": None, "to_time": None, "notes": "Bay service execution"}]
		for log_cfg in logs:
			log_tech = self.ctx.resolve("techs", log_cfg.get("technician") or tech_name)
			hrs = flt(log_cfg.get("hours") or 1.0)
			from_dt = log_cfg.get("from_time") or self.ctx.dt(sched_date, sched.get("start_time") or "09:00:00")
			if log_cfg.get("to_time"):
				to_dt = log_cfg["to_time"]
			else:
				base_dt = _dt.datetime.fromisoformat(str(from_dt))
				to_dt = (base_dt + _dt.timedelta(hours=hrs)).strftime("%Y-%m-%d %H:%M:%S")
			ro.append("time_logs", {
				"technician": log_tech,
				"from_time": from_dt,
				"to_time": to_dt,
				"time_in_hours": flt(hrs, 2),
				"notes": log_cfg.get("notes") or "Bay service execution",
			})
		if logs:
			ro.save(ignore_permissions=True)
			ro.reload()

		for supp in self._supplements_cfg:
			parts_arg = []
			for p in (supp.get("parts") or []):
				p_copy = dict(p)
				if p_copy.get("item_code"):
					p_copy["item_code"] = self.ctx.resolve("items", p_copy["item_code"])
				parts_arg.append(p_copy)
			ro.add_supplement(
				service_title=supp["service_title"],
				labor_hours=supp["labor_hours"],
				labor_rate=supp.get("labor_rate"),
				parts=parts_arg or None,
				notes=supp.get("notes"),
			)
			ro.reload()
			if supp.get("approve"):
				pending_rows = [s for s in ro.services if s.service_title == supp["service_title"] and s.status != "Approved"]
				if pending_rows:
					ro.authorize_jobs(
						approvals={str(pending_rows[-1].idx): "Approved"},
						channel="Phone / Verbal",
						signer_name=ro.customer,
						notes="Approved supplement",
					)
					ro.reload()

		for act in self._action_items_cfg:
			item_code = self.ctx.resolve("items", act["item_code"]) if act.get("item_code") else None
			act_name = create_shop_action_item(
				title=act["title"],
				category=act.get("category") or "Shop Floor Discrepancy",
				priority=act.get("priority") or "High",
				reference_doctype="Repair Order",
				reference_name=ro.name,
				item_code=item_code,
				details=act.get("details") or f"Logged during execution of {ro.name}",
			)
			act_handle = act.get("handle") or self.ctx.manifest.next_handle("action_items", "action")
			self.ctx.manifest.register("action_items", act_handle, act_name)
			self.ctx.track("Shop Action Item", act_name)

	def build(self, stage: str = "draft_estimate") -> Any:
		"""Build and persist the Repair Order through real domain APIs up to `stage`."""
		if stage not in VALID_RO_STAGES:
			raise ValueError(f"Unsupported SimBuilder stage '{stage}'. Valid: {VALID_RO_STAGES}")

		ro = self._insert_ro(stage)
		self._append_requested_services(ro, stage)
		if stage in ("checked_in", "draft_estimate"):
			return ro

		ro.generate_approval_token()
		ro.status = "Pending Authorization"
		ro.save(ignore_permissions=True)
		ro.reload()
		if stage == "pending_authorization":
			return ro

		if stage == "partially_approved_with_deferred":
			self._apply_partial_approval(ro)
			self._record_configured_deposit(ro)
			return ro

		ro.authorize_jobs(confirm_all=1, channel="In-Person Signature", signer_name=ro.customer)
		ro.reload()
		self._record_configured_deposit(ro)

		if stage == "authorized_in_stock":
			return ro

		if stage == "waiting_on_parts":
			self._run_procurement_if_configured(ro)
			return ro

		if self._procurement_cfg and self._procurement_cfg.get("receive_via_pi"):
			self._run_procurement_if_configured(ro)

		self._apply_schedule_and_bay_work(ro)
		if stage == "scheduled_in_progress":
			return ro

		# Approve any remaining pending supplements before completing/closing.
		pending_supps = [s for s in ro.services if s.status in ("Draft", "Estimating", "Pending Approval")]
		if pending_supps:
			ro.authorize_jobs(
				approvals={str(s.idx): "Approved" for s in pending_supps},
				channel="Phone / Verbal",
				signer_name=ro.customer,
				notes="Auto-approved pending supplements for completion",
			)
			ro.reload()

		for s in ro.services:
			if s.status == "Approved" and not s.is_completed:
				ro.toggle_service_completion(service_row_id=s.name, is_completed=1)
		ro.reload()
		ro.park_vehicle()
		ro.reload()
		if stage == "work_completed":
			return ro

		# billed_paid / closed_released
		chk_cfg = self._checkout_cfg or {}
		odo_out = chk_cfg.get("odometer_out") or (int(ro.odometer_in or 0) + 15)
		chk_kwargs: dict[str, Any] = {
			"payment_method": chk_cfg.get("payment_method") or "Debit / Interac",
			"odometer_out": odo_out,
		}
		if chk_cfg.get("payments") is not None:
			chk_kwargs["payments"] = chk_cfg["payments"]
		elif chk_cfg.get("paid_amount") is not None:
			chk_kwargs["paid_amount"] = chk_cfg["paid_amount"]

		res = ro.process_payment_and_release(**chk_kwargs)
		si_name = res.get("sales_invoice")
		if si_name:
			si_handle = chk_cfg.get("si_handle") or self.ctx.manifest.next_handle("sis", "si")
			self.ctx.manifest.register("sis", si_handle, si_name)
			self.ctx.track("Sales Invoice", si_name)
		for idx, pe_name in enumerate(res.get("payment_entries") or []):
			pe_handle = chk_cfg.get("pe_handle") if (idx == 0 and chk_cfg.get("pe_handle")) else self.ctx.manifest.next_handle("pes", "pe")
			self.ctx.manifest.register("pes", pe_handle, pe_name)
			self.ctx.track("Payment Entry", pe_name)

		ro.reload()
		return ro


def seed_historical_telemetry(
	ctx: Optional[SimContext] = None,
	n_closed_ros: int = 30,
	*,
	seed: int = 42,
	anchor_date: Optional[str] = None,
) -> list[str]:
	"""Seed N closed historical Repair Orders across T-60d..T-1d with log-normal actual durations."""
	sim_ctx = ctx or SimContext(seed=seed, anchor_date=anchor_date)
	m = sim_ctx.manifest

	cust_pool = [v for k, v in m.customers.items() if k != "household_secondary"]
	veh_pool = list(m.vehicles.values())
	active_techs = [m.techs[h] for h in ("marcus", "elena", "devon", "noah") if h in m.techs]

	ro_names: list[str] = []
	for i in range(int(n_closed_ros)):
		days_ago = 1 + ((i * 2) % 60)
		day_str = sim_ctx.day(-days_ago)
		pkg_handle, cat = _HIST_PACKAGES[i % len(_HIST_PACKAGES)]
		pkg_name = m.packages.get(pkg_handle)
		if not pkg_name:
			continue

		frt_hours = flt(frappe.db.get_value("Service Package", pkg_name, "standard_labor_hours")) or 1.0
		sigma = get_sigma_for_item(cat, default_sigma=0.30)
		actual_hrs = round(max(0.15, frt_hours * math.exp(sim_ctx.rng.gauss(0.0, sigma))), 2)

		veh_name = veh_pool[i % len(veh_pool)]
		cust_name = frappe.db.get_value("Repair Vehicle", veh_name, "customer") or cust_pool[i % len(cust_pool)]
		tech_id = active_techs[i % len(active_techs)]

		start_hr = 8 + (i % 6)
		start_dt = _dt.datetime.fromisoformat(f"{day_str} {start_hr:02d}:00:00")
		end_dt = start_dt + _dt.timedelta(hours=actual_hrs)
		odo_in = 30000 + i * 750
		odo_out = odo_in + 12

		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"company": m.company,
			"customer": cust_name,
			"vehicle": veh_name,
			"odometer_in": odo_in,
			"odometer_out": odo_out,
			"posting_date": day_str,
			"check_in_date": f"{day_str} 08:00:00",
			"picked_up_at": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
			"parked_at": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
			"checkout_date": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
			"approval_status": "Approved",
			"authorized_by": cust_name,
			"authorized_at": f"{day_str} 08:15:00",
			"status": "Closed / Released",
			"payment_status": "Paid",
			"vehicle_location": "Released to Customer",
			"vehicle_released": 1,
			"total_bay_hours": actual_hrs,
			"services": [
				{
					"service_package": pkg_name,
					"service_title": pkg_name,
					"status": "Approved",
					"is_completed": 1,
					"completed_at": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
					"actual_duration_hours": actual_hrs,
					"assigned_technician": tech_id,
				}
			],
			"time_logs": [
				{
					"technician": tech_id,
					"from_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
					"to_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
					"time_in_hours": actual_hrs,
					"notes": f"Historical completed job ({pkg_name})",
				}
			],
		}).insert(ignore_permissions=True)

		m.register("ros", f"hist_{i + 1}", ro.name)
		sim_ctx.track(ro)
		ro_names.append(ro.name)

	m.metadata["historical_ros"] = list(ro_names)
	return ro_names
