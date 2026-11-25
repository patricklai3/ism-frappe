# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Base classes and reusable helpers for ISM Setup Wizard steps."""

from __future__ import annotations

import copy
import csv
import io
import json
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import quote

import frappe

from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_PASS,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_NOT_APPLICABLE,
	CheckResult,
	SetupContext,
	StepApplyResult,
	StepInspection,
	StepMeta,
	StepVerifyResult,
)


def build_desk_link(doctype: str, name: str | None = None, *, site_url: str | None = None) -> str:
	"""Build a clickable Frappe Desk URL for a DocType list or specific document."""
	slug = doctype.strip().lower().replace(" ", "-")
	if site_url is None:
		try:
			base = (frappe.utils.get_url() or "").rstrip("/")
		except Exception:
			base = ""
	else:
		base = site_url.rstrip("/")

	if name is not None and str(name).strip():
		encoded_name = quote(str(name).strip(), safe="")
		return f"{base}/app/{slug}/{encoded_name}"
	return f"{base}/app/{slug}"


def normalize_records_list(raw: Any) -> list[dict[str, Any]]:
	"""Normalize a list of dicts, JSON string, or CSV text into a list of record dicts."""
	if raw is None:
		return []

	if isinstance(raw, dict):
		return [copy.deepcopy(raw)]

	if isinstance(raw, (list, tuple)):
		records: list[dict[str, Any]] = []
		for idx, item in enumerate(raw):
			if not isinstance(item, dict):
				raise ValueError(f"Record at index {idx} must be a dict, got {type(item).__name__}.")
			records.append(copy.deepcopy(item))
		return records

	if isinstance(raw, str):
		stripped = raw.strip()
		if not stripped:
			return []

		if stripped.startswith(("[", "{")):
			try:
				parsed = json.loads(stripped)
			except json.JSONDecodeError as exc:
				raise ValueError(f"Invalid JSON records payload: {exc}") from exc
			return normalize_records_list(parsed)

		reader = csv.DictReader(io.StringIO(stripped))
		if not reader.fieldnames:
			raise ValueError("CSV records payload is missing a header row.")
		headers = [h.strip() for h in reader.fieldnames if h is not None and h.strip()]
		if not headers:
			raise ValueError("CSV records payload contains no valid column headers.")

		csv_records: list[dict[str, Any]] = []
		for row in reader:
			cleaned: dict[str, Any] = {}
			for key, value in row.items():
				if key is None:
					continue
				clean_key = key.strip()
				if not clean_key:
					continue
				cleaned[clean_key] = value.strip() if isinstance(value, str) else value
			if any(v not in (None, "") for v in cleaned.values()):
				csv_records.append(cleaned)
		return csv_records

	raise ValueError(f"Unsupported records format: {type(raw).__name__}.")


class BaseSetupStep(ABC):
	"""Abstract base class for every declarative ISM Setup Wizard step."""

	meta: StepMeta

	def is_applicable(self, ctx: SetupContext) -> bool:
		"""Return True when this step applies to the current SetupContext."""
		return True

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		"""Return machine-readable field schema, presets, and Desk URLs for Agent OS."""
		return {
			"step_id": self.meta.id,
			"meta": self.meta.to_dict(),
			"fields": [],
			"secret_fields": [],
			"presets": self.get_presets(ctx),
			"desk_urls": {},
		}

	@abstractmethod
	def inspect(self, ctx: SetupContext) -> StepInspection:
		"""Inspect live Frappe/ERPNext database state for this step (read-only)."""

	@abstractmethod
	def apply(
		self,
		payload: dict[str, Any],
		ctx: SetupContext,
		*,
		dry_run: bool = False,
	) -> StepApplyResult:
		"""Apply configuration changes idempotently.

		Runner executes apply() inside a DB savepoint and rolls back when dry_run=True.
		Steps must avoid non-DB external side effects (outbound SMS/email/API calls) when dry_run=True.
		"""

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		"""Verify that the step's configuration is live and healthy."""
		inspection = self.inspect(ctx)
		has_fail = any(check.level == CHECK_FAIL for check in inspection.checks)
		passed = (
			inspection.status in (STEP_STATUS_COMPLETE, STEP_STATUS_NOT_APPLICABLE)
			and not has_fail
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=inspection.summary,
			checks=inspection.checks,
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		"""Return portable, non-secret configuration data for Staging -> Production carryover."""
		return {}

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		"""Return curated starter presets as a list of {id, label, description, payload} dicts."""
		return []

	def resolve_preset_payload(self, preset_name: str, ctx: SetupContext) -> dict[str, Any]:
		"""Look up a preset by id or label (case-insensitive) and return a deep copy of its payload."""
		target = (preset_name or "").strip().lower()
		presets = self.get_presets(ctx)
		for item in presets:
			pid = str(item.get("id") or "").strip().lower()
			label = str(item.get("label") or item.get("name") or "").strip().lower()
			if target and (target == pid or target == label):
				payload = item.get("payload")
				return copy.deepcopy(payload) if isinstance(payload, dict) else {}
		available = [str(p.get("id") or p.get("label") or "") for p in presets]
		raise ValueError(
			f"Unknown preset '{preset_name}' for step '{self.meta.id}'. Available presets: {available}"
		)


def _normalize_compare_value(val: Any) -> Any:
	if val is None:
		return ""
	if isinstance(val, bool):
		return int(val)
	if isinstance(val, (int, float)):
		return val
	return str(val).strip()


class MasterRecordsStep(BaseSetupStep):
	"""Reusable BaseSetupStep for idempotent upsert of repeating master DocTypes."""

	doctype: str = ""
	key_field: str = "name"
	match_fields: tuple[str, ...] = ()
	payload_key: str = "records"
	managed_fields: tuple[str, ...] = ()
	child_table_fields: tuple[str, ...] = ()
	min_records_for_complete: int = 1
	default_filters: dict[str, Any] = {}

	def _effective_match_fields(self) -> tuple[str, ...]:
		if self.match_fields:
			return tuple(self.match_fields)
		return (self.key_field,)

	def extract_records(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
		"""Extract and normalize records from a step apply payload."""
		if self.payload_key in payload:
			raw = payload[self.payload_key]
		elif "records" in payload:
			raw = payload["records"]
		elif "items" in payload:
			raw = payload["items"]
		else:
			raw = []
		return normalize_records_list(raw)

	def prepare_record(self, record: dict[str, Any], ctx: SetupContext) -> dict[str, Any]:
		"""Hook for subclasses to normalize or enrich a single record before upsert."""
		return copy.deepcopy(record)

	def find_existing_record(self, record: dict[str, Any], ctx: SetupContext) -> str | None:
		"""Return existing document name matching record's natural key, or None."""
		filters: dict[str, Any] = {}
		for fieldname in self._effective_match_fields():
			val = record.get(fieldname)
			if val in (None, ""):
				return None
			filters[fieldname] = val
		return frappe.db.get_value(self.doctype, filters, "name")

	def record_label(self, record: dict[str, Any], doc_name: str | None = None) -> str:
		if doc_name:
			return str(doc_name)
		parts = [str(record.get(f, "")) for f in self._effective_match_fields() if record.get(f)]
		return " / ".join(parts) or self.doctype

	def _allowed_fields(self, record: dict[str, Any]) -> list[str]:
		if self.managed_fields:
			return [f for f in self.managed_fields if f in record]
		excluded = {"doctype", "name", "owner", "creation", "modified", "modified_by", "idx", "docstatus"}
		return [k for k in record if k not in excluded]

	def _child_rows_equal(self, existing_rows: list[Any], desired_rows: list[dict[str, Any]]) -> bool:
		if len(existing_rows) != len(desired_rows):
			return False
		for existing, desired in zip(existing_rows, desired_rows):
			for key, val in desired.items():
				if _normalize_compare_value(existing.get(key)) != _normalize_compare_value(val):
					return False
		return True

	def upsert_records(
		self,
		records: list[dict[str, Any]],
		ctx: SetupContext,
		*,
		dry_run: bool = False,
	) -> StepApplyResult:
		"""Idempotently insert or update a batch of master records."""
		created: list[str] = []
		updated: list[str] = []
		unchanged: list[str] = []
		warnings: list[str] = []
		child_set = set(self.child_table_fields)

		for raw_record in records:
			record = self.prepare_record(raw_record, ctx)
			existing_name = self.find_existing_record(record, ctx)
			fields_to_set = self._allowed_fields(record)

			if not existing_name:
				doc_data: dict[str, Any] = {"doctype": self.doctype}
				for fieldname in fields_to_set:
					doc_data[fieldname] = record[fieldname]
				for fieldname in self._effective_match_fields():
					if fieldname in record and fieldname not in doc_data:
						doc_data[fieldname] = record[fieldname]
				doc = frappe.get_doc(doc_data)
				doc.insert(ignore_permissions=True)
				created.append(self.record_label(record, doc.name))
				continue

			doc = frappe.get_doc(self.doctype, existing_name)
			changed = False
			for fieldname in fields_to_set:
				new_val = record[fieldname]
				if fieldname in child_set and isinstance(new_val, list):
					existing_rows = list(doc.get(fieldname) or [])
					if not self._child_rows_equal(existing_rows, new_val):
						doc.set(fieldname, [])
						for child_row in new_val:
							doc.append(fieldname, child_row)
						changed = True
				else:
					if _normalize_compare_value(doc.get(fieldname)) != _normalize_compare_value(new_val):
						doc.set(fieldname, new_val)
						changed = True

			if changed:
				doc.save(ignore_permissions=True)
				updated.append(self.record_label(record, doc.name))
			else:
				unchanged.append(self.record_label(record, doc.name))

		return StepApplyResult(
			step_id=self.meta.id,
			dry_run=dry_run,
			applied=bool(created or updated),
			created=tuple(created),
			updated=tuple(updated),
			unchanged=tuple(unchanged),
			warnings=tuple(warnings),
		)

	def apply(
		self,
		payload: dict[str, Any],
		ctx: SetupContext,
		*,
		dry_run: bool = False,
	) -> StepApplyResult:
		records = self.extract_records(payload)
		return self.upsert_records(records, ctx, dry_run=dry_run)

	def inspect(self, ctx: SetupContext) -> StepInspection:
		filters = dict(self.default_filters)
		count = frappe.db.count(self.doctype, filters=filters) if frappe.db.exists("DocType", self.doctype) else 0
		complete = count >= self.min_records_for_complete
		status = STEP_STATUS_COMPLETE if complete else STEP_STATUS_INCOMPLETE
		level = CHECK_PASS if complete else CHECK_FAIL
		summary = (
			f"{count} {self.doctype} record(s) configured."
			if complete
			else f"Found {count} {self.doctype} record(s); at least {self.min_records_for_complete} required."
		)
		return StepInspection(
			status=status,
			summary=summary,
			checks=(
				CheckResult(
					code=f"{self.meta.id}_count",
					level=level,
					message=summary,
					remediation=(
						None
						if complete
						else f"Add at least {self.min_records_for_complete} {self.doctype} record(s)."
					),
				),
			),
			counts={self.doctype: count},
			desk_urls={"list": build_desk_link(self.doctype, site_url=ctx.site_url)},
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		if not self.doctype or not frappe.db.exists("DocType", self.doctype):
			return {self.payload_key: []}

		names = frappe.get_all(
			self.doctype,
			filters=dict(self.default_filters),
			pluck="name",
			order_by=f"{self.key_field} asc" if self.key_field != "name" else "name asc",
		)
		exported: list[dict[str, Any]] = []
		child_set = set(self.child_table_fields)
		scalar_fields = [f for f in self.managed_fields if f not in child_set]

		for doc_name in names:
			doc = frappe.get_doc(self.doctype, doc_name)
			row: dict[str, Any] = {}
			for fieldname in scalar_fields:
				row[fieldname] = doc.get(fieldname)
			for child_field in self.child_table_fields:
				child_rows = []
				for child in doc.get(child_field) or []:
					child_dict = child.as_dict()
					for meta_key in (
						"name",
						"owner",
						"creation",
						"modified",
						"modified_by",
						"docstatus",
						"idx",
						"parent",
						"parentfield",
						"parenttype",
						"doctype",
					):
						child_dict.pop(meta_key, None)
					child_rows.append(child_dict)
				row[child_field] = child_rows
			exported.append(row)

		return {self.payload_key: exported}
