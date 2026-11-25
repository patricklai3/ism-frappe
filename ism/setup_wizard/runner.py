# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Orchestration engine for inspecting, applying, skipping, and verifying Setup Wizard steps."""

from __future__ import annotations

import copy
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from ism.api._validation import get_active_company
from ism.setup_wizard.base import BaseSetupStep
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	PROFILE_PRODUCTION,
	PROFILE_STAGING,
	REDACTED_VALUE,
	STEP_STATUS_BLOCKED,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_NOT_APPLICABLE,
	STEP_STATUS_SKIPPED,
	STEP_STATUS_WARNING,
	TIER_CORE,
	WIZARD_STATUS_COMPLETED,
	WIZARD_STATUS_IN_PROGRESS,
	WIZARD_STATUS_NEEDS_ATTENTION,
	WIZARD_STATUS_NOT_STARTED,
	SetupContext,
	StepInspection,
	StepVerifyResult,
	normalize_profile,
	redact_secret_values,
)
from ism.setup_wizard.registry import get_ordered_steps, get_step
from ism.setup_wizard.settings_helper import get_ism_settings_password_fields

_SAVEPOINT_NAME = "ism_setup_step_apply"


def _get_setup_state_doc() -> Any | None:
	if not frappe.db.exists("DocType", "ISM Setup State"):
		return None
	return frappe.get_single("ISM Setup State")


def _resolve_profile(profile_override: str | None, setup_state_doc: Any | None) -> str:
	if profile_override is not None and str(profile_override).strip():
		try:
			return normalize_profile(profile_override)
		except ValueError as exc:
			frappe.throw(str(exc), frappe.ValidationError)

	stored = getattr(setup_state_doc, "environment_profile", None) if setup_state_doc else None
	if stored:
		try:
			return normalize_profile(stored)
		except ValueError:
			return PROFILE_STAGING
	return PROFILE_STAGING


def _require_step(step_id: str) -> BaseSetupStep:
	try:
		return get_step(step_id)
	except KeyError:
		frappe.throw(
			_("Setup wizard step '{0}' does not exist.").format(step_id),
			frappe.DoesNotExistError,
		)


def build_context(profile_override: str | None = None) -> SetupContext:
	"""Build a SetupContext from ISM Setup State, active Company (safe fallback), and site URL."""
	setup_state_doc = _get_setup_state_doc()
	profile = _resolve_profile(profile_override, setup_state_doc)

	company: str | None = None
	try:
		company = get_active_company()
	except Exception:
		company = None

	try:
		site_url = (frappe.utils.get_url() or "").rstrip("/")
	except Exception:
		site_url = ""

	return SetupContext(
		profile=profile,
		company=company,
		site_url=site_url,
		setup_state_doc=setup_state_doc,
	)


def _serialize_recorded_row(row: Any) -> dict[str, Any]:
	return {
		"step_id": row.step_id,
		"schema_version": cint(row.schema_version or 1),
		"status": row.status or "Pending",
		"skipped_reason": row.skipped_reason or "",
		"last_applied_at": str(row.last_applied_at) if row.last_applied_at else None,
		"last_applied_by": row.last_applied_by or None,
		"last_verified_at": str(row.last_verified_at) if row.last_verified_at else None,
		"last_verified_ok": bool(cint(row.last_verified_ok)),
		"last_verify_summary": row.last_verify_summary or "",
	}


def _recorded_rows_map(setup_state_doc: Any | None) -> dict[str, Any]:
	if not setup_state_doc:
		return {}
	return {
		row.step_id: row
		for row in (getattr(setup_state_doc, "step_states", None) or [])
		if getattr(row, "step_id", None)
	}


def _evaluate_all_steps(
	ctx: SetupContext,
	*,
	include_checks: bool = True,
) -> dict[str, Any]:
	ordered_steps = get_ordered_steps()
	steps_by_id = {step.meta.id: step for step in ordered_steps}
	recorded_map = _recorded_rows_map(ctx.setup_state_doc)

	effective_status_by_id: dict[str, str] = {}
	step_entries: list[dict[str, Any]] = []
	new_or_updated_steps: list[str] = []

	status_counts: dict[str, int] = {
		STEP_STATUS_COMPLETE: 0,
		STEP_STATUS_INCOMPLETE: 0,
		STEP_STATUS_WARNING: 0,
		STEP_STATUS_BLOCKED: 0,
		STEP_STATUS_SKIPPED: 0,
		STEP_STATUS_NOT_APPLICABLE: 0,
	}
	core_applicable_total = 0
	core_complete_count = 0
	core_has_fail_check = False
	core_has_warning_status = False
	core_all_ready = True
	optional_total = 0
	optional_complete = 0

	for step in ordered_steps:
		sid = step.meta.id
		recorded_row = recorded_map.get(sid)
		applicable = bool(step.is_applicable(ctx))
		blocked_by: list[str] = []

		if not applicable:
			inspection = StepInspection(
				status=STEP_STATUS_NOT_APPLICABLE,
				summary="Not applicable for current environment/profile.",
			)
			effective_status = STEP_STATUS_NOT_APPLICABLE
			upgrade_pending = False
		else:
			inspection = step.inspect(ctx)
			effective_status = inspection.status

			for dep_id in step.meta.depends_on:
				dep_step = steps_by_id.get(dep_id)
				dep_status = effective_status_by_id.get(dep_id)
				if (
					dep_step is not None
					and dep_step.meta.tier == TIER_CORE
					and dep_status in (STEP_STATUS_INCOMPLETE, STEP_STATUS_BLOCKED)
				):
					blocked_by.append(dep_id)

			if blocked_by:
				effective_status = STEP_STATUS_BLOCKED
			elif (
				recorded_row is not None
				and recorded_row.status == "Skipped"
				and effective_status == STEP_STATUS_INCOMPLETE
				and (step.meta.allow_skip or ctx.profile != PROFILE_PRODUCTION)
			):
				effective_status = STEP_STATUS_SKIPPED

			upgrade_pending = (
				recorded_row is None
				or step.meta.schema_version > cint(recorded_row.schema_version or 0)
			)
			if upgrade_pending:
				new_or_updated_steps.append(sid)

		effective_status_by_id[sid] = effective_status
		status_counts[effective_status] = status_counts.get(effective_status, 0) + 1

		if applicable:
			if step.meta.tier == TIER_CORE:
				core_applicable_total += 1
				if effective_status == STEP_STATUS_COMPLETE:
					core_complete_count += 1
				if effective_status == STEP_STATUS_WARNING:
					core_has_warning_status = True
				if any(c.level == CHECK_FAIL for c in inspection.checks):
					core_has_fail_check = True

				step_ready = (
					effective_status in (STEP_STATUS_COMPLETE, STEP_STATUS_WARNING)
					or (effective_status == STEP_STATUS_SKIPPED and step.meta.allow_skip)
				)
				if not step_ready:
					core_all_ready = False
			else:
				optional_total += 1
				if effective_status == STEP_STATUS_COMPLETE:
					optional_complete += 1

		entry: dict[str, Any] = {
			"id": sid,
			"title": step.meta.title,
			"phase": step.meta.phase,
			"order": step.meta.order,
			"tier": step.meta.tier,
			"schema_version": step.meta.schema_version,
			"owner_domain": step.meta.owner_domain,
			"depends_on": list(step.meta.depends_on),
			"managed_settings_sections": list(step.meta.managed_settings_sections),
			"allow_skip": bool(step.meta.allow_skip),
			"description": step.meta.description,
			"status": effective_status,
			"live_status": inspection.status,
			"summary": inspection.summary,
			"blocked_by": blocked_by,
			"upgrade_pending": upgrade_pending,
			"recorded_state": _serialize_recorded_row(recorded_row) if recorded_row is not None else None,
			"counts": dict(inspection.counts) if inspection.counts is not None else None,
			"desk_urls": dict(inspection.desk_urls) if inspection.desk_urls is not None else None,
			"webhook_urls": dict(inspection.webhook_urls) if inspection.webhook_urls is not None else None,
			"checks": [c.to_dict() for c in inspection.checks] if include_checks else [],
		}
		step_entries.append(entry)

	ready_for_go_live = bool(
		core_applicable_total > 0 and core_all_ready and not core_has_fail_check
	)

	was_completed = bool(getattr(ctx.setup_state_doc, "completed_at", None)) if ctx.setup_state_doc else False
	any_progress = bool(
		recorded_map
		or status_counts[STEP_STATUS_COMPLETE] > 0
		or status_counts[STEP_STATUS_WARNING] > 0
		or status_counts[STEP_STATUS_SKIPPED] > 0
	)

	if not step_entries:
		wizard_status = WIZARD_STATUS_NOT_STARTED
	elif core_has_warning_status or (was_completed and (not ready_for_go_live or bool(new_or_updated_steps))):
		wizard_status = WIZARD_STATUS_NEEDS_ATTENTION
	elif ready_for_go_live:
		wizard_status = WIZARD_STATUS_COMPLETED
	elif any_progress:
		wizard_status = WIZARD_STATUS_IN_PROGRESS
	else:
		wizard_status = WIZARD_STATUS_NOT_STARTED

	next_recommended_step = _pick_next_recommended_step(step_entries)

	summary = {
		"total": len(step_entries),
		"complete": status_counts[STEP_STATUS_COMPLETE],
		"incomplete": status_counts[STEP_STATUS_INCOMPLETE],
		"warning": status_counts[STEP_STATUS_WARNING],
		"blocked": status_counts[STEP_STATUS_BLOCKED],
		"skipped": status_counts[STEP_STATUS_SKIPPED],
		"not_applicable": status_counts[STEP_STATUS_NOT_APPLICABLE],
		"core_total": core_applicable_total,
		"core_complete": core_complete_count,
		"core_incomplete": core_applicable_total - core_complete_count,
		"optional_total": optional_total,
		"optional_complete": optional_complete,
		"upgrade_pending_count": len(new_or_updated_steps),
	}

	return {
		"profile": ctx.profile,
		"company": ctx.company,
		"site_url": ctx.site_url,
		"wizard_status": wizard_status,
		"ready_for_go_live": ready_for_go_live,
		"new_or_updated_steps": new_or_updated_steps,
		"next_recommended_step": next_recommended_step,
		"summary": summary,
		"steps": step_entries,
	}


def _pick_next_recommended_step(step_entries: list[dict[str, Any]]) -> str | None:
	# Priority 1: incomplete core steps that are not blocked
	for entry in step_entries:
		if entry["tier"] == TIER_CORE and entry["status"] == STEP_STATUS_INCOMPLETE:
			return entry["id"]
	# Priority 2: warning core steps
	for entry in step_entries:
		if entry["tier"] == TIER_CORE and entry["status"] == STEP_STATUS_WARNING:
			return entry["id"]
	# Priority 3: incomplete optional pack steps
	for entry in step_entries:
		if entry["tier"] != TIER_CORE and entry["status"] == STEP_STATUS_INCOMPLETE:
			return entry["id"]
	# Priority 4: warning optional pack steps
	for entry in step_entries:
		if entry["tier"] != TIER_CORE and entry["status"] == STEP_STATUS_WARNING:
			return entry["id"]
	return None


def get_setup_status(
	profile_override: str | None = None,
	include_checks: bool = True,
) -> dict[str, Any]:
	"""Inspect all registered setup steps in DAG order and return consolidated status (read-only)."""
	ctx = build_context(profile_override=profile_override)
	return _evaluate_all_steps(ctx, include_checks=include_checks)


def _sanitize_schema_output(schema: dict[str, Any]) -> dict[str, Any]:
	"""Strictly redact any secret/Password values from a step schema payload."""
	password_fields = get_ism_settings_password_fields()
	cleaned = copy.deepcopy(schema)
	if isinstance(cleaned.get("fields"), list):
		for field_desc in cleaned["fields"]:
			if not isinstance(field_desc, dict):
				continue
			if (
				field_desc.get("is_secret")
				or field_desc.get("fieldtype") == "Password"
				or field_desc.get("fieldname") in password_fields
			):
				field_desc["is_secret"] = True
				field_desc.pop("value", None)
	return redact_secret_values(cleaned, extra_secret_keys=password_fields)


def get_step_schema(
	step_id: str | None = None,
	profile_override: str | None = None,
) -> dict[str, Any]:
	"""Return redacted machine-readable schema for one step or all registered steps."""
	ctx = build_context(profile_override=profile_override)

	if step_id:
		step = _require_step(step_id)
		schema = _sanitize_schema_output(step.get_schema(ctx))
		return {
			"profile": ctx.profile,
			"company": ctx.company,
			"site_url": ctx.site_url,
			"step": schema,
		}

	schemas = [_sanitize_schema_output(step.get_schema(ctx)) for step in get_ordered_steps()]
	return {
		"profile": ctx.profile,
		"company": ctx.company,
		"site_url": ctx.site_url,
		"steps": schemas,
	}


def _deep_merge_payload(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
	merged = copy.deepcopy(base)
	for key, val in override.items():
		if isinstance(val, dict) and isinstance(merged.get(key), dict):
			merged[key] = _deep_merge_payload(merged[key], val)
		else:
			merged[key] = copy.deepcopy(val)
	return merged


def _scrub_secret_literals(obj: Any, secret_values: list[str]) -> Any:
	if not secret_values:
		return obj
	if isinstance(obj, str):
		out = obj
		for secret in secret_values:
			if secret and secret in out:
				out = out.replace(secret, REDACTED_VALUE)
		return out
	if isinstance(obj, dict):
		return {k: _scrub_secret_literals(v, secret_values) for k, v in obj.items()}
	if isinstance(obj, list):
		return [_scrub_secret_literals(item, secret_values) for item in obj]
	if isinstance(obj, tuple):
		return [_scrub_secret_literals(item, secret_values) for item in obj]
	return obj


def _sync_wizard_status_fields(state_doc: Any, ctx: SetupContext) -> None:
	status_snapshot = _evaluate_all_steps(ctx, include_checks=False)
	state_doc.wizard_status = status_snapshot["wizard_status"]
	if status_snapshot["ready_for_go_live"] and not state_doc.completed_at:
		state_doc.completed_at = now_datetime()
		state_doc.completed_by = frappe.session.user


def apply_step(
	step_id: str,
	payload: dict[str, Any] | None = None,
	*,
	preset: str | None = None,
	secrets: dict[str, Any] | None = None,
	dry_run: bool = False,
	profile_override: str | None = None,
) -> dict[str, Any]:
	"""Apply a single setup step inside a DB savepoint; rolls back unconditionally on dry_run or error."""
	step = _require_step(step_id)
	ctx = build_context(profile_override=profile_override)

	effective_payload: dict[str, Any] = {}
	if preset:
		try:
			preset_payload = step.resolve_preset_payload(preset, ctx)
		except ValueError as exc:
			frappe.throw(str(exc), frappe.ValidationError)
		effective_payload = _deep_merge_payload(effective_payload, preset_payload)

	if payload is not None:
		if not isinstance(payload, dict):
			frappe.throw(_("Step payload must be a JSON object."), frappe.ValidationError)
		effective_payload = _deep_merge_payload(effective_payload, payload)

	secret_literals: list[str] = []
	secret_keys: set[str] = set(get_ism_settings_password_fields())
	if secrets is not None:
		if not isinstance(secrets, dict):
			frappe.throw(_("Secrets payload must be a JSON object."), frappe.ValidationError)
		effective_payload["_secrets"] = dict(secrets)
		for k, v in secrets.items():
			secret_keys.add(str(k))
			if isinstance(v, str) and len(v.strip()) >= 4:
				secret_literals.append(v.strip())

	frappe.db.savepoint(_SAVEPOINT_NAME)
	try:
		apply_result = step.apply(effective_payload, ctx, dry_run=bool(dry_run))
		post_inspection = step.inspect(ctx)

		if dry_run:
			frappe.db.rollback(save_point=_SAVEPOINT_NAME)
		else:
			state_doc = _get_setup_state_doc()
			if state_doc is not None:
				if profile_override:
					state_doc.environment_profile = ctx.profile
				state_doc.upsert_step_state(
					step.meta.id,
					schema_version=step.meta.schema_version,
					status="Configured",
					skipped_reason="",
					last_applied_at=now_datetime(),
					last_applied_by=frappe.session.user,
				)
				ctx.setup_state_doc = state_doc
				_sync_wizard_status_fields(state_doc, ctx)
				state_doc.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT_NAME)
		raise

	result_dict = apply_result.to_dict()
	# Preserve explicit step handoff (e.g. one-time credentials for CLI --write-env)
	# while scrubbing all diff arrays against any secret keys or secret literals.
	raw_handoff = result_dict.pop("handoff", None)
	scrubbed_result = redact_secret_values(result_dict, extra_secret_keys=secret_keys)
	scrubbed_result = _scrub_secret_literals(scrubbed_result, secret_literals)
	scrubbed_result["handoff"] = raw_handoff

	inspection_dict = _scrub_secret_literals(
		redact_secret_values(post_inspection.to_dict(), extra_secret_keys=secret_keys),
		secret_literals,
	)

	return {
		"step_id": step.meta.id,
		"profile": ctx.profile,
		"dry_run": bool(dry_run),
		"preset": preset,
		"result": scrubbed_result,
		"handoff": raw_handoff,
		"inspection": inspection_dict,
	}


def skip_step(
	step_id: str,
	reason: str = "",
	*,
	unskip: bool = False,
) -> dict[str, Any]:
	"""Mark a step as Skipped (or unskip it back to Pending) in ISM Setup State."""
	step = _require_step(step_id)
	ctx = build_context()
	cleaned_reason = (reason or "").strip()

	if not unskip and not step.meta.allow_skip and ctx.profile == PROFILE_PRODUCTION:
		frappe.throw(
			_("Step '{0}' cannot be skipped in the Production profile.").format(step_id),
			frappe.ValidationError,
		)

	state_doc = _get_setup_state_doc()
	if state_doc is not None:
		if unskip:
			state_doc.upsert_step_state(
				step.meta.id,
				schema_version=step.meta.schema_version,
				status="Pending",
				skipped_reason="",
			)
		else:
			state_doc.upsert_step_state(
				step.meta.id,
				schema_version=step.meta.schema_version,
				status="Skipped",
				skipped_reason=cleaned_reason,
			)
		ctx.setup_state_doc = state_doc
		_sync_wizard_status_fields(state_doc, ctx)
		state_doc.save(ignore_permissions=True)

	inspection = step.inspect(ctx)
	return {
		"step_id": step.meta.id,
		"profile": ctx.profile,
		"skipped": not bool(unskip),
		"reason": "" if unskip else cleaned_reason,
		"inspection": inspection.to_dict(),
	}


def verify_step(
	step_id: str | None = None,
	options: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Run verification checks on one step or all applicable steps and persist audit state."""
	ctx = build_context()
	verify_options = dict(options) if isinstance(options, dict) else {}

	if step_id:
		targets = [_require_step(step_id)]
	else:
		targets = [step for step in get_ordered_steps() if step.is_applicable(ctx)]

	results: list[StepVerifyResult] = []
	now = now_datetime()
	state_doc = _get_setup_state_doc()

	for step in targets:
		res = step.verify(ctx, options=verify_options)
		results.append(res)

		if state_doc is not None:
			existing = state_doc.get_step_state(step.meta.id)
			next_status = existing.status if existing is not None else "Pending"
			if res.passed and next_status != "Skipped":
				next_status = "Verified"
			state_doc.upsert_step_state(
				step.meta.id,
				schema_version=step.meta.schema_version,
				status=next_status,
				last_verified_at=now,
				last_verified_ok=1 if res.passed else 0,
				last_verify_summary=(res.summary or "")[:500],
			)

	if state_doc is not None:
		state_doc.last_audit_at = now
		ctx.setup_state_doc = state_doc
		_sync_wizard_status_fields(state_doc, ctx)
		state_doc.save(ignore_permissions=True)

	if step_id:
		single = results[0]
		return {
			"step_id": single.step_id,
			"profile": ctx.profile,
			"passed": single.passed,
			"result": single.to_dict(),
		}

	return {
		"profile": ctx.profile,
		"passed": all(r.passed for r in results) if results else False,
		"results": [r.to_dict() for r in results],
	}
