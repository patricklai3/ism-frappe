# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Helpers and SettingsSectionStep base class for live ISM Settings section introspection."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import frappe
from frappe.utils import cint, flt

from ism.setup_wizard.base import BaseSetupStep
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_PASS,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	UNMANAGED_SETTINGS_FIELDS,
	CheckResult,
	SetupContext,
	StepApplyResult,
	StepInspection,
)

_LAYOUT_FIELDTYPES = frozenset({"Section Break", "Column Break", "Tab Break"})
_ISM_SETTINGS_JSON_PATH = (
	Path(__file__).resolve().parent.parent / "ism" / "doctype" / "ism_settings" / "ism_settings.json"
)


def _load_ism_settings_fields_from_json() -> list[dict[str, Any]]:
	data = json.loads(_ISM_SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
	fields = data.get("fields") or []
	field_order = data.get("field_order") or []
	if not field_order:
		return list(fields)
	by_name = {f.get("fieldname"): f for f in fields if f.get("fieldname")}
	ordered: list[dict[str, Any]] = []
	seen: set[str] = set()
	for fn in field_order:
		if fn in by_name:
			ordered.append(by_name[fn])
			seen.add(fn)
	for f in fields:
		fn = f.get("fieldname")
		if fn and fn not in seen:
			ordered.append(f)
	return ordered


def _get_raw_ism_settings_fields() -> list[dict[str, Any]]:
	"""Return ordered raw field definitions from live frappe.get_meta('ISM Settings') or JSON fallback."""
	try:
		if getattr(frappe, "db", None) is not None:
			meta = frappe.get_meta("ISM Settings")
			if meta and getattr(meta, "fields", None):
				result: list[dict[str, Any]] = []
				for df in meta.fields:
					result.append({
						"fieldname": df.fieldname,
						"label": df.label,
						"fieldtype": df.fieldtype,
						"options": df.options,
						"default": df.default,
						"description": df.description,
						"depends_on": df.depends_on,
						"reqd": cint(df.reqd),
						"read_only": cint(df.read_only),
						"length": getattr(df, "length", None),
						"precision": getattr(df, "precision", None),
						"no_copy": cint(getattr(df, "no_copy", 0)),
					})
				return result
	except Exception:
		pass
	return _load_ism_settings_fields_from_json()


def list_all_ism_settings_sections() -> list[str]:
	"""Return all Section Break fieldnames defined on ISM Settings in document order."""
	return [
		str(f["fieldname"])
		for f in _get_raw_ism_settings_fields()
		if f.get("fieldtype") == "Section Break" and f.get("fieldname")
	]


def get_ism_settings_password_fields() -> frozenset[str]:
	"""Return all Password-typed fieldnames on ISM Settings."""
	return frozenset(
		str(f["fieldname"])
		for f in _get_raw_ism_settings_fields()
		if f.get("fieldtype") == "Password" and f.get("fieldname")
	)


def get_ism_settings_section_fields(section_fieldname: str) -> list[dict[str, Any]]:
	"""Extract every data field between section_fieldname and the next Section Break."""
	raw_fields = _get_raw_ism_settings_fields()
	in_section = False
	found_section = False
	extracted: list[dict[str, Any]] = []

	for df in raw_fields:
		fieldtype = df.get("fieldtype") or ""
		fieldname = df.get("fieldname") or ""

		if fieldtype == "Section Break":
			if in_section:
				break
			if fieldname == section_fieldname:
				in_section = True
				found_section = True
			continue

		if not in_section:
			continue

		if fieldtype in _LAYOUT_FIELDTYPES or not fieldname:
			continue

		extracted.append({
			"fieldname": fieldname,
			"label": df.get("label") or fieldname,
			"fieldtype": fieldtype,
			"options": df.get("options"),
			"default": df.get("default"),
			"description": df.get("description"),
			"depends_on": df.get("depends_on"),
			"reqd": bool(cint(df.get("reqd"))),
			"read_only": bool(cint(df.get("read_only"))),
			"is_secret": fieldtype == "Password",
		})

	if not found_section:
		raise KeyError(f"Unknown ISM Settings section: '{section_fieldname}'")

	return extracted


def _serialize_setting_value(value: Any) -> Any:
	"""Convert Frappe document field values (including Time timedeltas) into JSON-safe values."""
	if isinstance(value, datetime.timedelta):
		total_seconds = int(value.total_seconds())
		hours, remainder = divmod(total_seconds, 3600)
		minutes, seconds = divmod(remainder, 60)
		return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
	if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
		return value.isoformat()
	return value


def _coerce_setting_value(fieldtype: str, value: Any) -> Any:
	if value is None:
		return None
	if fieldtype == "Check":
		if isinstance(value, str):
			return 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0
		return 1 if bool(value) else 0
	if fieldtype == "Int":
		if isinstance(value, str) and not value.strip():
			return None
		return cint(value)
	if fieldtype in ("Float", "Currency", "Percent"):
		if isinstance(value, str) and not value.strip():
			return None
		return flt(value)
	return value


def _values_equal(fieldtype: str, current: Any, target: Any) -> bool:
	cur_ser = _serialize_setting_value(current)
	tgt_ser = _serialize_setting_value(target)
	if fieldtype == "Check":
		return cint(cur_ser) == cint(tgt_ser)
	if fieldtype == "Int":
		return (cur_ser in (None, "") and tgt_ser in (None, "")) or cint(cur_ser) == cint(tgt_ser)
	if fieldtype in ("Float", "Currency", "Percent"):
		if cur_ser in (None, "") and tgt_ser in (None, ""):
			return True
		return abs(flt(cur_ser) - flt(tgt_ser)) < 1e-9
	if cur_ser in (None, "") and tgt_ser in (None, ""):
		return True
	return str(cur_ser) == str(tgt_ser)


def _is_password_field_set(settings_doc: Any, fieldname: str) -> bool:
	try:
		return bool(settings_doc.get_password(fieldname, raise_exception=False))
	except Exception:
		return False


class SettingsSectionStep(BaseSetupStep):
	"""BaseSetupStep that dynamically derives schema, apply, and blueprint export from ISM Settings sections."""

	allow_read_only_fields: frozenset[str] = frozenset()

	def get_section_fields(self) -> list[dict[str, Any]]:
		"""Return ordered field definitions across all managed_settings_sections."""
		fields: list[dict[str, Any]] = []
		seen: set[str] = set()
		for section in self.meta.managed_settings_sections:
			for df in get_ism_settings_section_fields(section):
				fn = df["fieldname"]
				if fn in UNMANAGED_SETTINGS_FIELDS or fn in seen:
					continue
				seen.add(fn)
				fields.append(df)
		return fields

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		settings = frappe.get_single("ISM Settings")
		descriptors: list[dict[str, Any]] = []
		secret_fields: list[str] = []

		for df in self.get_section_fields():
			fn = df["fieldname"]
			entry: dict[str, Any] = {
				"fieldname": fn,
				"label": df["label"],
				"fieldtype": df["fieldtype"],
				"options": df["options"],
				"default": df["default"],
				"description": df["description"],
				"depends_on": df["depends_on"],
				"reqd": bool(df["reqd"]),
				"read_only": bool(df["read_only"]),
				"is_secret": bool(df["is_secret"]),
			}
			if df["is_secret"]:
				entry["is_set"] = _is_password_field_set(settings, fn)
				secret_fields.append(fn)
			else:
				entry["value"] = _serialize_setting_value(settings.get(fn))
			descriptors.append(entry)

		return {
			"step_id": self.meta.id,
			"meta": self.meta.to_dict(),
			"managed_settings_sections": list(self.meta.managed_settings_sections),
			"fields": descriptors,
			"secret_fields": secret_fields,
			"presets": self.get_presets(ctx),
			"desk_urls": {"ism_settings": f"{ctx.site_url}/app/ism-settings"},
		}

	def inspect(self, ctx: SetupContext) -> StepInspection:
		settings = frappe.get_single("ISM Settings")
		missing_required: list[str] = []
		for df in self.get_section_fields():
			if not df["reqd"] or df["read_only"]:
				continue
			fn = df["fieldname"]
			if df["is_secret"]:
				if not _is_password_field_set(settings, fn):
					missing_required.append(fn)
			else:
				val = settings.get(fn)
				if val is None or (isinstance(val, str) and not val.strip()):
					missing_required.append(fn)

		if missing_required:
			msg = f"Missing required ISM Settings field(s): {', '.join(missing_required)}."
			return StepInspection(
				status=STEP_STATUS_INCOMPLETE,
				summary=msg,
				checks=(
					CheckResult(
						code=f"{self.meta.id}_required_fields",
						level=CHECK_FAIL,
						message=msg,
						remediation=f"Configure {', '.join(missing_required)} in ISM Settings.",
					),
				),
				desk_urls={"ism_settings": f"{ctx.site_url}/app/ism-settings"},
			)

		summary = f"ISM Settings section(s) {', '.join(self.meta.managed_settings_sections)} configured."
		return StepInspection(
			status=STEP_STATUS_COMPLETE,
			summary=summary,
			checks=(
				CheckResult(
					code=f"{self.meta.id}_settings_ok",
					level=CHECK_PASS,
					message=summary,
				),
			),
			desk_urls={"ism_settings": f"{ctx.site_url}/app/ism-settings"},
		)

	def apply(
		self,
		payload: dict[str, Any],
		ctx: SetupContext,
		*,
		dry_run: bool = False,
	) -> StepApplyResult:
		settings = frappe.get_single("ISM Settings")
		section_fields = self.get_section_fields()
		field_map = {df["fieldname"]: df for df in section_fields}

		raw_settings: dict[str, Any] = {}
		if isinstance(payload.get("settings"), dict):
			raw_settings.update(payload["settings"])
		for fn, raw_val in payload.items():
			if fn not in ("settings", "secrets", "_secrets"):
				raw_settings[fn] = raw_val
		raw_secrets: dict[str, Any] = {}
		if isinstance(payload.get("secrets"), dict):
			raw_secrets.update(payload["secrets"])
		if isinstance(payload.get("_secrets"), dict):
			raw_secrets.update(payload["_secrets"])

		updated: list[str] = []
		unchanged: list[str] = []
		warnings: list[str] = []
		dirty = False

		for fn, raw_val in raw_settings.items():
			if fn in ("settings", "secrets", "_secrets"):
				continue
			df = field_map.get(fn)
			if df is None:
				continue
			if df["is_secret"]:
				warnings.append(
					f"Field '{fn}' is a Password field and was ignored in plain payload; pass via secrets."
				)
				continue
			if df["read_only"] and fn not in self.allow_read_only_fields:
				warnings.append(f"Skipped read-only field '{fn}'.")
				continue

			coerced = _coerce_setting_value(df["fieldtype"], raw_val)
			if _values_equal(df["fieldtype"], settings.get(fn), coerced):
				unchanged.append(fn)
			else:
				settings.set(fn, coerced)
				updated.append(fn)
				dirty = True

		for fn, secret_val in raw_secrets.items():
			df = field_map.get(fn)
			if df is None:
				continue
			if not df["is_secret"]:
				warnings.append(f"Secret key '{fn}' is not a Password field; skipped.")
				continue
			if df["read_only"] and fn not in self.allow_read_only_fields:
				warnings.append(f"Skipped read-only secret field '{fn}'.")
				continue
			if secret_val is None or (isinstance(secret_val, str) and not secret_val.strip()):
				unchanged.append(fn)
				continue

			settings.set(fn, str(secret_val))
			updated.append(f"{fn}: [UPDATED SECRET]")
			dirty = True

		if dirty:
			settings.save(ignore_permissions=True)

		return StepApplyResult(
			step_id=self.meta.id,
			dry_run=dry_run,
			applied=dirty,
			created=(),
			updated=tuple(updated),
			unchanged=tuple(unchanged),
			warnings=tuple(warnings),
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		settings = frappe.get_single("ISM Settings")
		exported_settings: dict[str, Any] = {}
		for df in self.get_section_fields():
			if df["is_secret"] or df["read_only"]:
				continue
			fn = df["fieldname"]
			exported_settings[fn] = _serialize_setting_value(settings.get(fn))
		return {"settings": exported_settings}
