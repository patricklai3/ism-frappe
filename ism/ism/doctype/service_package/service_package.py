# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from ism.ism.doctype.service_package_item.service_package_item import normalize_package_item_slot_and_side
from ism.utils.tesla_manual import (
	get_mobile_capability_from_manifest,
	parse_tesla_manual_url,
	resolve_generation_entries,
	strip_correction_code_prefix,
)


def _format_scoped_package_name(base: str, scope: str = "") -> str:
	"""Format a unique Service Package doc name up to Frappe's 140-char limit."""
	clean_base = (base or "").strip() or "Service Package"
	clean_scope = (scope or "").strip()
	if not clean_scope:
		return clean_base[:140]
	suffix = f" [{clean_scope}]"
	if len(suffix) >= 130:
		suffix = f" [{clean_scope[:60]}]"
	max_base = max(1, 140 - len(suffix))
	return f"{clean_base[:max_base].strip()}{suffix}"[:140]


class ServicePackage(Document):
	@property
	def frt(self):
		return flt(self.standard_labor_hours)

	@frt.setter
	def frt(self, val):
		self.standard_labor_hours = flt(val)

	def autoname(self):
		self.format_package_name()
		self.sync_generation_compatibility()
		base = (self.package_name or "").strip() or (self.correction_code or "Service Package").strip()
		if not frappe.db.exists("Service Package", base[:140]):
			self.name = base[:140]
			return

		scope = (getattr(self, "applicable_generation", None) or getattr(self, "applicable_model", None) or "").strip()
		candidate = _format_scoped_package_name(base, scope)
		if not frappe.db.exists("Service Package", candidate):
			self.name = candidate
			return

		idx = 2
		while True:
			numbered = _format_scoped_package_name(base, f"{scope} #{idx}" if scope else f"#{idx}")
			if not frappe.db.exists("Service Package", numbered):
				self.name = numbered
				return
			idx += 1

	def before_insert(self):
		self.format_package_name()
		self.sync_generation_compatibility()

	def validate(self):
		self.format_package_name()
		self.sync_generation_compatibility()
		self._validate_scoped_uniqueness()
		self.validate_duplicate_tags()

		if not self.is_flat_rate and flt(self.standard_labor_hours) and flt(self.hourly_rate):
			self.service_rate = flt(self.standard_labor_hours) * flt(self.hourly_rate)

		url_changed = hasattr(self, "has_value_changed") and self.has_value_changed("tesla_manual_url")
		needs_tags_from_manifest = (self.is_new() or url_changed) and not (self.equipment_tags or [])
		needs_symmetry_from_manifest = (
			not getattr(self, "rh_correction_code", None)
			or not getattr(self, "is_symmetrical", 0)
		)
		if getattr(self, "tesla_manual_url", None) and (needs_tags_from_manifest or needs_symmetry_from_manifest):
			try:
				dep_info = get_mobile_capability_from_manifest(
					self.tesla_manual_url,
					getattr(self, "doc_guid", None),
					getattr(self, "correction_code", None),
					allow_network=False,
				)
				if dep_info:
					if needs_tags_from_manifest:
						existing_tags = {
							(getattr(r, "equipment_tag", None) or "").strip()
							for r in (self.equipment_tags or [])
						}
						for tag in dep_info.get("suggested_equipment_tags") or []:
							if tag and tag not in existing_tags and frappe.db.exists("Equipment Tag", tag):
								self.append("equipment_tags", {"equipment_tag": tag})
								existing_tags.add(tag)
					if not getattr(self, "rh_correction_code", None) and dep_info.get("rh_correction_code"):
						self.rh_correction_code = dep_info["rh_correction_code"]
					if not getattr(self, "is_symmetrical", 0) and dep_info.get("is_symmetrical"):
						self.is_symmetrical = 1
			except Exception:
				frappe.log_error(
					message=frappe.get_traceback(),
					title="ServicePackage Manifest Lookup Error",
				)

		if not getattr(self, "is_symmetrical", 0) and self.package_name:
			if re.search(r"\bLH\b|\bRH\b|Left Hand|Right Hand", self.package_name, re.IGNORECASE):
				self.is_symmetrical = 1

		is_symm = getattr(self, "is_symmetrical", 0)
		for row in (self.items or []):
			normalize_package_item_slot_and_side(
				row,
				is_symmetrical=is_symm,
				enforce_package_symmetry=True,
			)

		self._validate_self_serve_catalog()

	def validate_duplicate_tags(self):
		"""Reject duplicate Equipment Tag rows and strip blank rows."""
		if not self.equipment_tags:
			return
		seen_tags = set()
		cleaned_rows = []
		for row in self.equipment_tags:
			tag = (getattr(row, "equipment_tag", None) or "").strip()
			if not tag:
				continue
			row.equipment_tag = tag
			if tag in seen_tags:
				frappe.throw(
					_("Duplicate equipment tag '{0}' at row #{1}.").format(tag, row.idx),
					title=_("Duplicate Tag"),
				)
			seen_tags.add(tag)
			cleaned_rows.append(row)
		if len(cleaned_rows) != len(self.equipment_tags):
			self.set("equipment_tags", cleaned_rows)

	def get_equipment_tags(self) -> list[str]:
		"""Return sorted deduplicated Equipment Tag names linked to this Service Package."""
		return sorted(
			{
				(getattr(row, "equipment_tag", None) or "").strip()
				for row in (getattr(self, "equipment_tags", None) or [])
				if (getattr(row, "equipment_tag", None) or "").strip()
			}
		)

	def _validate_self_serve_catalog(self):
		"""Normalize and validate self-serve booking catalog fields."""
		for fieldname in ("customer_title", "customer_summary"):
			val = getattr(self, fieldname, None)
			if isinstance(val, str):
				setattr(self, fieldname, val.strip())

		if flt(getattr(self, "customer_estimate_min", 0) or 0) < 0:
			frappe.throw(_("Customer Estimate Min cannot be negative."))
		if flt(getattr(self, "customer_estimate_max", 0) or 0) < 0:
			frappe.throw(_("Customer Estimate Max cannot be negative."))
		if cint(getattr(self, "self_serve_duration_minutes", 0) or 0) < 0:
			frappe.throw(_("Self-Serve Duration (Minutes) cannot be negative."))

		if cint(getattr(self, "allow_self_serve_booking", 0)) != 1:
			return

		mode = getattr(self, "customer_price_display_mode", None) or "Exact Price"
		est_min = flt(getattr(self, "customer_estimate_min", 0) or 0)
		est_max = flt(getattr(self, "customer_estimate_max", 0) or 0)
		if mode == "Estimate Range" and est_min > 0 and est_max > 0 and est_max < est_min:
			frappe.throw(_("Customer Estimate Max must be greater than or equal to Customer Estimate Min."))

		year_from = cint(getattr(self, "applicable_year_from", 0) or 0)
		year_to = cint(getattr(self, "applicable_year_to", 0) or 0)
		if year_from != 0 and not (2008 <= year_from <= 2100):
			frappe.throw(_("Applicable Year From must be between 2008 and 2100 (or 0 for no lower bound)."))
		if year_to != 0 and not (2008 <= year_to <= 2100):
			frappe.throw(_("Applicable Year To must be between 2008 and 2100 (or 0 for no upper bound)."))
		if year_from != 0 and year_to != 0 and year_from > year_to:
			frappe.throw(_("Applicable Year From cannot be greater than Applicable Year To."))

	def sync_generation_compatibility(self):
		"""Keep applicable_model, applicable_generation, and generation_compatibility synchronized."""
		app_model = (getattr(self, "applicable_model", None) or "").strip()
		app_gen = (getattr(self, "applicable_generation", None) or "").strip()
		manual_url = (getattr(self, "tesla_manual_url", None) or "").strip()

		if not app_model and manual_url:
			url_meta = parse_tesla_manual_url(manual_url)
			if url_meta.get("applicable_model"):
				self.applicable_model = url_meta["applicable_model"]
				app_model = self.applicable_model

		existing_rows = list(getattr(self, "generation_compatibility", None) or [])
		rows_changed = self.has_value_changed("generation_compatibility") if hasattr(self, "has_value_changed") else False
		gen_or_url_changed = (
			(self.has_value_changed("applicable_generation") or self.has_value_changed("tesla_manual_url") or self.has_value_changed("applicable_model"))
			if hasattr(self, "has_value_changed")
			else False
		)

		if existing_rows and (rows_changed or not (app_gen or manual_url)):
			dedup_rows = []
			seen = set()
			models = set()
			gen_names = []
			for row in existing_rows:
				row_model = (getattr(row, "applicable_model", None) or app_model or "").strip()
				row_gen = (getattr(row, "generation_name", None) or "").strip()
				if not row_gen:
					continue
				if not row_model or not getattr(row, "year_from", None):
					resolved = resolve_generation_entries(row_gen, model=row_model or app_model, manual_url=manual_url)
					if resolved:
						info = resolved[0]
						row_model = row_model or info.get("applicable_model") or ""
						row.applicable_model = row_model
						row.generation_name = info.get("generation_name") or row_gen
						row.date_range = getattr(row, "date_range", None) or info.get("date_range") or ""
						row.year_from = getattr(row, "year_from", None) or info.get("year_from")
						row.month_from = getattr(row, "month_from", None) or info.get("month_from")
						row.year_to = getattr(row, "year_to", None) if getattr(row, "year_to", None) is not None else info.get("year_to")
						row.month_to = getattr(row, "month_to", None) if getattr(row, "month_to", None) is not None else info.get("month_to")
						row.manual_base_url = getattr(row, "manual_base_url", None) or info.get("manual_base_url") or ""
				key = (row.applicable_model or row_model, row.generation_name)
				if key not in seen:
					seen.add(key)
					dedup_rows.append(row)
					if key[0] and key[0] != "All Models":
						models.add(key[0])
					gen_names.append(key[1])
			if len(dedup_rows) != len(existing_rows):
				self.set("generation_compatibility", dedup_rows)
			if gen_names and (rows_changed and not (hasattr(self, "has_value_changed") and self.has_value_changed("applicable_generation")) or not app_gen):
				self.applicable_generation = ", ".join(gen_names)
			if not app_model and models:
				self.applicable_model = next(iter(models)) if len(models) == 1 else "All Models"
			return

		if (not existing_rows and (app_gen or manual_url)) or (gen_or_url_changed and not rows_changed):
			entries = resolve_generation_entries(app_gen, model=app_model, manual_url=manual_url)
			if entries:
				self.set("generation_compatibility", [])
				for entry in entries:
					self.append(
						"generation_compatibility",
						{
							"applicable_model": entry["applicable_model"],
							"generation_name": entry["generation_name"],
							"date_range": entry.get("date_range") or "",
							"year_from": entry.get("year_from"),
							"month_from": entry.get("month_from"),
							"year_to": entry.get("year_to"),
							"month_to": entry.get("month_to"),
							"manual_base_url": entry.get("manual_base_url") or "",
						},
					)
				if not app_gen:
					self.applicable_generation = ", ".join(e["generation_name"] for e in entries)
				if not self.applicable_model:
					distinct_models = {e["applicable_model"] for e in entries if e.get("applicable_model")}
					if len(distinct_models) == 1:
						self.applicable_model = next(iter(distinct_models))
					elif len(distinct_models) > 1:
						self.applicable_model = "All Models"
			elif not app_gen and not app_model and existing_rows:
				self.set("generation_compatibility", [])

	def _validate_scoped_uniqueness(self):
		"""Allow identical package titles across distinct vehicle models or generations, reject exact duplicates."""
		pkg_name = (self.package_name or "").strip()
		if not pkg_name:
			return

		app_model = (getattr(self, "applicable_model", None) or "").strip()
		app_gen = (getattr(self, "applicable_generation", None) or "").strip()

		duplicates = frappe.db.get_all(
			"Service Package",
			filters={
				"package_name": pkg_name,
				"name": ["!=", self.name or ""],
			},
			fields=["name", "applicable_model", "applicable_generation"],
		)
		for row in duplicates:
			other_model = (row.applicable_model or "").strip()
			other_gen = (row.applicable_generation or "").strip()
			if other_model == app_model and other_gen == app_gen:
				frappe.throw(
					_(
						"A Service Package named '{0}' already exists for model '{1}' and generation '{2}'."
					).format(pkg_name, app_model or "Any", app_gen or "Any"),
					frappe.DuplicateEntryError,
				)

	def format_package_name(self):
		"""Ensure correction code is not included in package_name / service title."""
		if self.package_name:
			self.package_name = strip_correction_code_prefix(self.package_name, self.correction_code)
