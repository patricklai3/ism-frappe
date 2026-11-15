from __future__ import annotations

import json
import math
import re
from typing import Any, Optional

import frappe
from frappe import _

_RECONDITIONED_TOKENS = frozenset(
	{"reman", "remanufactured", "reconditioned", "rebuilt", "rec", "rcn", "reb"}
)
_USED_TOKENS = frozenset({"used"})


def require_str(value: Any, label: str = "Value") -> str:
	"""Validate and strip a mandatory non-empty string argument."""
	if not isinstance(value, str) or not value.strip():
		frappe.throw(_("{0} must be a valid non-empty string.").format(label), frappe.ValidationError)
	return value.strip()


def optional_str(value: Any, label: str = "Value") -> Optional[str]:
	"""Validate an optional string argument, normalizing whitespace-only strings to None."""
	if value is None:
		return None
	if not isinstance(value, str):
		frappe.throw(_("{0} must be a string.").format(label), frappe.ValidationError)
	cleaned = value.strip()
	return cleaned or None


def scalar_str(value: Any, label: str = "Value") -> Optional[str]:
	"""Coerce an optional scalar value to a stripped string while rejecting containers and bools."""
	if value is None:
		return None
	if isinstance(value, (bool, dict, list, tuple, set)):
		frappe.throw(_("{0} must be a scalar value.").format(label), frappe.ValidationError)
	if isinstance(value, float) and not math.isfinite(value):
		frappe.throw(_("{0} must be a finite value.").format(label), frappe.ValidationError)
	cleaned = str(value).strip()
	return cleaned or None


def require_int(
	value: Any,
	label: str = "Value",
	*,
	min_val: Optional[int] = None,
	max_val: Optional[int] = None,
) -> int:
	"""Validate and return a mandatory integer, rejecting bools, non-integers, and out-of-range values."""
	if value is None or (isinstance(value, str) and not value.strip()):
		frappe.throw(_("{0} is required.").format(label), frappe.MandatoryError)
	if isinstance(value, bool):
		frappe.throw(_("{0} must be an integer.").format(label), frappe.ValidationError)
	if isinstance(value, int):
		parsed = value
	elif isinstance(value, float):
		if not math.isfinite(value) or not value.is_integer():
			frappe.throw(_("{0} must be an integer.").format(label), frappe.ValidationError)
		parsed = int(value)
	elif isinstance(value, str):
		try:
			parsed = int(value.strip())
		except ValueError:
			frappe.throw(_("{0} must be an integer.").format(label), frappe.ValidationError)
	else:
		frappe.throw(_("{0} must be an integer.").format(label), frappe.ValidationError)

	if min_val is not None and parsed < min_val:
		if min_val == 0:
			frappe.throw(_("{0} cannot be negative.").format(label), frappe.ValidationError)
		frappe.throw(_("{0} must be at least {1}.").format(label, min_val), frappe.ValidationError)
	if max_val is not None and parsed > max_val:
		frappe.throw(_("{0} cannot exceed {1}.").format(label, max_val), frappe.ValidationError)
	return parsed


def optional_int(
	value: Any,
	label: str = "Value",
	*,
	min_val: Optional[int] = None,
	max_val: Optional[int] = None,
	default: Optional[int] = None,
) -> Optional[int]:
	"""Validate an optional integer argument with optional bounds and default fallback."""
	if value is None or (isinstance(value, str) and not value.strip()):
		return default
	return require_int(value, label, min_val=min_val, max_val=max_val)


def optional_flt(
	value: Any,
	label: str = "Value",
	*,
	positive: bool = False,
	non_negative: bool = False,
	default: Optional[float] = None,
) -> Optional[float]:
	"""Validate an optional finite float, rejecting bools, containers, NaN/Inf, and invalid signs."""
	if value is None or (isinstance(value, str) and not value.strip()):
		return default
	if isinstance(value, bool) or not isinstance(value, (int, float, str)):
		frappe.throw(_("{0} must be a valid number.").format(label), frappe.ValidationError)
	raw = value.strip() if isinstance(value, str) else value
	try:
		parsed = float(raw)
	except (TypeError, ValueError):
		frappe.throw(_("{0} must be a valid number.").format(label), frappe.ValidationError)
	if not math.isfinite(parsed):
		frappe.throw(_("{0} must be a finite number.").format(label), frappe.ValidationError)
	if positive and parsed <= 0:
		frappe.throw(_("{0} must be greater than zero.").format(label), frappe.ValidationError)
	if non_negative and parsed < 0:
		frappe.throw(_("{0} cannot be negative.").format(label), frappe.ValidationError)
	return parsed


def coerce_bool(value: Any, default: bool = False) -> bool:
	"""Coerce bool, numeric, or string flags from HTTP/MCP arguments into bool safely."""
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		if isinstance(value, float) and not math.isfinite(value):
			return False
		return bool(value)
	if isinstance(value, str):
		cleaned = value.strip().lower()
		if not cleaned:
			return default
		if cleaned in ("1", "true", "yes", "on", "y"):
			return True
		if cleaned in ("0", "false", "no", "off", "n"):
			return False
		return False
	if isinstance(value, (dict, list, tuple, set)):
		frappe.throw(_("Boolean flag cannot be a list or dictionary."), frappe.ValidationError)
	return bool(value)


def parse_json_arg(value: Any, default: Any = None, label: str = "Argument") -> Any:
	"""Parse JSON-encoded strings, throwing ValidationError on malformed JSON arrays/objects."""
	if value is None:
		return default
	if isinstance(value, str):
		stripped = value.strip()
		if not stripped:
			return default
		try:
			return json.loads(stripped)
		except json.JSONDecodeError:
			if stripped.startswith(("[", "{")):
				frappe.throw(_("Invalid JSON in {0}.").format(label), frappe.ValidationError)
			return stripped
	return value


def normalize_condition(condition: Any = None, *, as_code: bool = False) -> str:
	"""Normalize part condition via exact-token matching -> New/NEW, Used/USED, Reconditioned/RCN."""
	if condition is None:
		tokens: set[str] = set()
	else:
		if not isinstance(condition, str):
			if isinstance(condition, (dict, list, tuple, set, bool)):
				frappe.throw(_("Condition must be a string."), frappe.ValidationError)
			condition = str(condition)
		tokens = set(re.findall(r"[a-z0-9]+", condition.strip().lower()))

	if tokens & _RECONDITIONED_TOKENS:
		return "RCN" if as_code else "Reconditioned"
	if tokens & _USED_TOKENS:
		return "USED" if as_code else "Used"
	return "NEW" if as_code else "New"


def get_active_company(company: Optional[str] = None, *, check_permission: bool = False) -> str:
	"""Validate an explicit Company or resolve the active default shop Company."""
	cleaned = optional_str(company, "Company")
	if cleaned:
		if not frappe.db.exists("Company", cleaned):
			frappe.throw(_("Company {0} does not exist.").format(cleaned), frappe.DoesNotExistError)
		if check_permission:
			frappe.has_permission("Company", "read", cleaned, throw=True)
		return cleaned

	user_default = frappe.defaults.get_user_default("Company")
	if user_default and frappe.db.exists("Company", user_default):
		resolved = user_default
	else:
		resolved = (
			frappe.db.get_single_value("Global Defaults", "default_company")
			or frappe.db.get_value("Company", {"is_group": 0}, "name")
			or frappe.db.get_value("Company", {}, "name")
		)
	if not resolved:
		frappe.throw(_("No active Company is configured."), frappe.ValidationError)
	if check_permission:
		frappe.has_permission("Company", "read", resolved, throw=True)
	return resolved


def escape_like(value: str) -> str:
	"""Escape SQL LIKE wildcard characters (\\, %, _) in user search strings."""
	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
