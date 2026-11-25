# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frozen contracts, constants, dataclasses, and secret redaction guards for the ISM Setup Wizard."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

PROFILE_STAGING = "Staging"
PROFILE_PRODUCTION = "Production"
VALID_PROFILES = (PROFILE_STAGING, PROFILE_PRODUCTION)

TIER_CORE = "core"
TIER_OPTIONAL_PACK = "optional_pack"
VALID_TIERS = (TIER_CORE, TIER_OPTIONAL_PACK)

STEP_STATUS_COMPLETE = "complete"
STEP_STATUS_INCOMPLETE = "incomplete"
STEP_STATUS_WARNING = "warning"
STEP_STATUS_BLOCKED = "blocked"
STEP_STATUS_SKIPPED = "skipped"
STEP_STATUS_NOT_APPLICABLE = "not_applicable"
VALID_STEP_STATUSES = (
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_WARNING,
	STEP_STATUS_BLOCKED,
	STEP_STATUS_SKIPPED,
	STEP_STATUS_NOT_APPLICABLE,
)

CHECK_PASS = "pass"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"
CHECK_INFO = "info"
VALID_CHECK_LEVELS = (CHECK_PASS, CHECK_WARN, CHECK_FAIL, CHECK_INFO)

WIZARD_STATUS_NOT_STARTED = "Not Started"
WIZARD_STATUS_IN_PROGRESS = "In Progress"
WIZARD_STATUS_COMPLETED = "Completed"
WIZARD_STATUS_NEEDS_ATTENTION = "Needs Attention"

# Explicit allowlist of ISM Settings fields intentionally excluded from Setup Wizard management.
UNMANAGED_SETTINGS_FIELDS: frozenset[str] = frozenset()

REDACTED_VALUE = "[REDACTED]"
_SAFE_REDACTED_SENTINELS = frozenset({REDACTED_VALUE, "[UPDATED SECRET]"})

# Patterns matching secret-bearing field names while avoiding false positives on
# benign metadata fields like `authorization_token_expiry_hours`,
# `portal_booking_token_ttl_days`, `telnyx_webhook_public_key`, or disclaimer text.
SECRET_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
	re.compile(r"(^|_)password($|_)", re.IGNORECASE),
	re.compile(r"(^|_)passwd($|_)", re.IGNORECASE),
	re.compile(r"(^|_)secrets?($|_)", re.IGNORECASE),
	re.compile(r"(^|_)api_?key($|_)", re.IGNORECASE),
	re.compile(r"(^|_)access_?token($|_)", re.IGNORECASE),
	re.compile(r"(^|_)refresh_?token($|_)", re.IGNORECASE),
	re.compile(r"(^|_)auth_?token($|_)", re.IGNORECASE),
	re.compile(r"(^|_)bearer_?token($|_)", re.IGNORECASE),
	re.compile(r"(^|_)oauth_?token($|_)", re.IGNORECASE),
	re.compile(r"(^|_)private_?key($|_)", re.IGNORECASE),
	re.compile(r"(^|_)encryption_?key($|_)", re.IGNORECASE),
	re.compile(r"(^|_)signature_?key($|_)", re.IGNORECASE),
	re.compile(r"(^|_)signing_?key($|_)", re.IGNORECASE),
	re.compile(r"(^|_)service_account_json($|_)", re.IGNORECASE),
	re.compile(r"(^|_)credentials?($|_)", re.IGNORECASE),
)

_SECRET_CONTAINER_KEYS = frozenset({"_secrets", "secrets"})
_SAFE_METADATA_KEYS = frozenset({"is_secret", "secret_fields", "secret_keys", "secrets_keys"})


class SecretLeakError(ValueError):
	"""Raised when a secret or Password field value is detected in an export or non-secret payload."""


def normalize_profile(profile: str | None, *, default: str = PROFILE_STAGING) -> str:
	"""Normalize a staging/production profile string (case-insensitive) to canonical casing."""
	if profile is None or (isinstance(profile, str) and not profile.strip()):
		return default
	if not isinstance(profile, str):
		raise ValueError(f"Profile must be a string, got {type(profile).__name__}.")
	cleaned = profile.strip().lower()
	if cleaned == "staging":
		return PROFILE_STAGING
	if cleaned == "production":
		return PROFILE_PRODUCTION
	raise ValueError(f"Invalid setup profile '{profile}'. Expected 'Staging' or 'Production'.")


@dataclass(frozen=True)
class StepMeta:
	id: str
	title: str
	phase: str
	order: int
	schema_version: int = 1
	tier: str = TIER_CORE
	depends_on: tuple[str, ...] = ()
	owner_domain: str = "ism"
	managed_settings_sections: tuple[str, ...] = ()
	allow_skip: bool = True
	description: str = ""

	def __post_init__(self) -> None:
		object.__setattr__(self, "depends_on", tuple(self.depends_on or ()))
		object.__setattr__(
			self,
			"managed_settings_sections",
			tuple(self.managed_settings_sections or ()),
		)
		if self.tier not in VALID_TIERS:
			raise ValueError(f"Invalid StepMeta.tier '{self.tier}'. Expected one of {VALID_TIERS}.")

	def to_dict(self) -> dict[str, Any]:
		return {
			"id": self.id,
			"title": self.title,
			"phase": self.phase,
			"order": self.order,
			"schema_version": self.schema_version,
			"tier": self.tier,
			"depends_on": list(self.depends_on),
			"owner_domain": self.owner_domain,
			"managed_settings_sections": list(self.managed_settings_sections),
			"allow_skip": bool(self.allow_skip),
			"description": self.description,
		}


@dataclass(frozen=True)
class CheckResult:
	code: str
	level: str
	message: str
	remediation: str | None = None

	def __post_init__(self) -> None:
		if self.level not in VALID_CHECK_LEVELS:
			raise ValueError(f"Invalid CheckResult.level '{self.level}'. Expected one of {VALID_CHECK_LEVELS}.")

	def to_dict(self) -> dict[str, Any]:
		data: dict[str, Any] = {
			"code": self.code,
			"level": self.level,
			"message": self.message,
		}
		if self.remediation is not None:
			data["remediation"] = self.remediation
		return data


@dataclass(frozen=True)
class StepInspection:
	status: str
	summary: str
	checks: tuple[CheckResult, ...] = ()
	counts: dict[str, Any] | None = None
	desk_urls: dict[str, str] | None = None
	webhook_urls: dict[str, str] | None = None

	def __post_init__(self) -> None:
		if self.status not in VALID_STEP_STATUSES:
			raise ValueError(
				f"Invalid StepInspection.status '{self.status}'. Expected one of {VALID_STEP_STATUSES}."
			)
		object.__setattr__(self, "checks", tuple(self.checks or ()))

	def to_dict(self) -> dict[str, Any]:
		return {
			"status": self.status,
			"summary": self.summary,
			"checks": [c.to_dict() for c in self.checks],
			"counts": dict(self.counts) if self.counts is not None else None,
			"desk_urls": dict(self.desk_urls) if self.desk_urls is not None else None,
			"webhook_urls": dict(self.webhook_urls) if self.webhook_urls is not None else None,
		}


@dataclass(frozen=True)
class StepApplyResult:
	step_id: str
	dry_run: bool
	applied: bool
	created: tuple[Any, ...] = ()
	updated: tuple[Any, ...] = ()
	unchanged: tuple[Any, ...] = ()
	warnings: tuple[str, ...] = ()
	handoff: dict[str, Any] | None = None

	def __post_init__(self) -> None:
		object.__setattr__(self, "created", tuple(self.created or ()))
		object.__setattr__(self, "updated", tuple(self.updated or ()))
		object.__setattr__(self, "unchanged", tuple(self.unchanged or ()))
		object.__setattr__(self, "warnings", tuple(self.warnings or ()))

	def to_dict(self) -> dict[str, Any]:
		return {
			"step_id": self.step_id,
			"dry_run": bool(self.dry_run),
			"applied": bool(self.applied),
			"created": list(self.created),
			"updated": list(self.updated),
			"unchanged": list(self.unchanged),
			"warnings": list(self.warnings),
			"handoff": dict(self.handoff) if self.handoff is not None else None,
		}


@dataclass(frozen=True)
class StepVerifyResult:
	step_id: str
	passed: bool
	summary: str
	checks: tuple[CheckResult, ...] = ()

	def __post_init__(self) -> None:
		object.__setattr__(self, "checks", tuple(self.checks or ()))

	def to_dict(self) -> dict[str, Any]:
		return {
			"step_id": self.step_id,
			"passed": bool(self.passed),
			"summary": self.summary,
			"checks": [c.to_dict() for c in self.checks],
		}


@dataclass
class SetupContext:
	profile: str
	company: str | None
	site_url: str
	setup_state_doc: Any = field(default=None, repr=False)

	def to_dict(self) -> dict[str, Any]:
		return {
			"profile": self.profile,
			"company": self.company,
			"site_url": self.site_url,
		}


def is_secret_key(key: str, extra_secret_keys: Iterable[str] = ()) -> bool:
	"""Return True if a dictionary key represents a secret or Password field."""
	if not isinstance(key, str):
		return False
	lowered = key.strip().lower()
	if not lowered:
		return False
	if lowered in _SAFE_METADATA_KEYS:
		return False
	extra_set = {k.strip().lower() for k in extra_secret_keys if isinstance(k, str) and k.strip()}
	if lowered in extra_set or lowered in _SECRET_CONTAINER_KEYS:
		return True
	if lowered.endswith("_set") or lowered.startswith(("has_", "is_")):
		return False
	return any(pattern.search(lowered) for pattern in SECRET_KEY_PATTERNS)


def _redact_leaf_value(val: Any) -> Any:
	"""Redact non-empty non-boolean secret values while preserving None, '', and booleans."""
	if isinstance(val, bool) or val in (None, ""):
		return val
	return REDACTED_VALUE


def redact_secret_values(obj: Any, extra_secret_keys: Iterable[str] = ()) -> Any:
	"""Recursively copy obj, replacing secret-like key values with '[REDACTED]'."""
	extra_tuple = tuple(extra_secret_keys)
	if isinstance(obj, dict):
		redacted: dict[Any, Any] = {}
		for key, val in obj.items():
			key_str = str(key)
			if key_str.strip().lower() in _SECRET_CONTAINER_KEYS:
				if isinstance(val, dict):
					redacted[key] = {sk: _redact_leaf_value(sv) for sk, sv in val.items()}
				else:
					redacted[key] = _redact_leaf_value(val)
			elif is_secret_key(key_str, extra_tuple):
				if isinstance(val, dict):
					redacted[key] = {sk: _redact_leaf_value(sv) for sk, sv in val.items()}
				elif isinstance(val, list):
					redacted[key] = [_redact_leaf_value(item) for item in val]
				else:
					redacted[key] = _redact_leaf_value(val)
			else:
				redacted[key] = redact_secret_values(val, extra_tuple)
		return redacted
	if isinstance(obj, list):
		return [redact_secret_values(item, extra_tuple) for item in obj]
	if isinstance(obj, tuple):
		return [redact_secret_values(item, extra_tuple) for item in obj]
	return obj


def _is_non_empty_secret_value(value: Any) -> bool:
	if value is None or isinstance(value, bool):
		return False
	if isinstance(value, str):
		stripped = value.strip()
		if not stripped or stripped in _SAFE_REDACTED_SENTINELS:
			return False
		return True
	if isinstance(value, (list, tuple, set, dict)):
		return len(value) > 0
	return True


def assert_no_secrets_in_payload(obj: Any, password_fields: Iterable[str] = ()) -> None:
	"""Raise SecretLeakError if any secret or Password field carries a value anywhere in obj."""
	password_set = {k.strip().lower() for k in password_fields if isinstance(k, str) and k.strip()}

	def _walk(node: Any, path: str) -> None:
		if isinstance(node, dict):
			for key, val in node.items():
				key_str = str(key)
				key_lower = key_str.strip().lower()
				child_path = f"{path}.{key_str}" if path else key_str
				if key_lower in password_set:
					if _is_non_empty_secret_value(val):
						raise SecretLeakError(
							f"Password field '{child_path}' must not be present with a value in exported payload."
						)
					continue
				if key_lower in _SECRET_CONTAINER_KEYS:
					if _is_non_empty_secret_value(val):
						raise SecretLeakError(
							f"Secret container '{child_path}' must not be present in exported payload."
						)
					continue
				if is_secret_key(key_str) and _is_non_empty_secret_value(val):
					raise SecretLeakError(
						f"Secret-like key '{child_path}' must not contain a value in exported payload."
					)
				_walk(val, child_path)
		elif isinstance(node, (list, tuple)):
			for idx, item in enumerate(node):
				_walk(item, f"{path}[{idx}]")

	_walk(obj, "")
