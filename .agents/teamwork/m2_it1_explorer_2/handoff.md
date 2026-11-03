# Milestone 2 Investigation Report: Server-Side Estimation API & Settings Integration

**Author**: Explorer 2 (`m2_it1_explorer_2`)  
**Mission**: Investigate server-side API service (`ism/api/estimation_service.py`), integration with `ISM Settings`, FRT / category resolution, payload dictionary structure, and database schema compatibility for Milestone 2.  
**Date**: 2026-10-27T01:50:00Z  

---

## 1. Observation

### 1.1 Non-Existence of `ism/api/estimation_service.py`
A search of `d:\ism\ism\api` reveals 5 existing files (`__init__.py`, `approval.py`, `parts.py`, `sms.py`, `tesla.py`). The file `ism/api/estimation_service.py` does not currently exist.

### 1.2 `ISM Settings` Estimation Schema
In `ism/ism/doctype/ism_settings/ism_settings.json` (lines 167–199), the duration estimation tuning fields are declared as:
```json
{
 "default": "80.0",
 "fieldname": "estimation_percentile",
 "fieldtype": "Percent",
 "label": "Estimation Percentile Target"
},
{
 "default": "0.30",
 "fieldname": "estimation_sigma",
 "fieldtype": "Float",
 "label": "Estimation Sigma (Dispersion)",
 "precision": "2"
},
{
 "default": "0",
 "fieldname": "buffer_padding_minutes",
 "fieldtype": "Int",
 "label": "Buffer Padding (Minutes)"
},
{
 "default": "60",
 "fieldname": "default_diagnostic_duration",
 "fieldtype": "Int",
 "label": "Default Diagnostic Duration (Minutes)"
}
```
In `ism/setup.py` (lines 142–152), `setup_default_ism_settings()` initializes these fields if not present.

### 1.3 Interface Contract 2 Requirements
In `d:\ism\PROJECT.md` (lines 109–116):
```markdown
### 2. `ism.api.estimation_service` ↔ Client / Other Modules
- Whitelisted Functions:
  - `get_estimate(item_code: str, **kwargs) -> Dict[str, Any]`:
    - Looks up `Item.custom_frt` (or kwargs), resolves sigma by item group, queries `ISM Settings` for `estimation_percentile`, `estimation_sigma`, and `buffer_padding_minutes`.
    - Returns: `{"duration_minutes": int, "p_pure": int, "buffer_padding": int, "percentile": float, "sigma": float}`.
  - `get_total_estimate(item_codes: Union[str, List], **kwargs) -> Dict[str, Any]`:
    - Accepts list of items, calculates multi-operation Fenton-Wilkinson estimate + buffer padding from `ISM Settings`.
    - Returns: `{"total_duration_minutes": int, "p_pure": int, "buffer_padding": int, "operations_count": int}`.
```

### 1.4 Test Suite Assertions for Service Layer
In `d:\ism\ism\tests\test_estimation_math.py`:
- `test_tier1_f5_single_item_estimate_with_buffer` (lines 212–224):
  Calls `ensure_ism_settings(buffer_padding_minutes=10, estimation_percentile=0.80, estimation_sigma=0.30)` and `res = get_estimate(self.item_brake1)`.
  Asserts `res["p_pure"] == 71`, `res["buffer_padding"] == 10`, `res["duration_minutes"] == 81`.
- `test_tier1_f5_multi_item_estimate_with_buffer` (lines 226–235):
  Calls `ensure_ism_settings(buffer_padding_minutes=15)` and `res = get_total_estimate([self.item_brake1, self.item_brake2])`.
  Asserts `res["buffer_padding"] == 15`, `res["total_duration_minutes"] == res["p_pure"] + 15`.
- `test_tier1_f5_zero_buffer_setting` (lines 236–243):
  Asserts `res["p_pure"] == 71` with `buffer_padding_minutes=0`.
- `test_tier1_f5_max_buffer_setting` (lines 245–251):
  Asserts `res["duration_minutes"] == 71 + 30` with `buffer_padding_minutes=30`.
- `test_tier1_f5_whitelisted_json_payload` (lines 253–258):
  Passes JSON string `json.dumps([self.item_brake1, self.item_tire])` to `get_total_estimate`.
- `test_tier2_f5_missing_item_fallback_frt` (lines 342–349):
  Calls `get_estimate("NON_EXISTENT_PART_XYZ", fallback_frt=45.0, sigma=0.30)`.
  Asserts `res["duration_minutes"] > 0`.
- `test_tier2_f5_skip_missing_items` (lines 350–358):
  Calls `get_total_estimate([self.item_brake1, "NON_EXISTENT_XYZ"], skip_missing=True)`.
  Asserts `dur_skip == dur_single`.
- `test_tier3_fw_sum_with_category_sigmas_and_buffer` (lines 363–372):
  Calls `get_total_estimate([self.item_brake1, self.item_elec, self.item_tire])`.
  Asserts `res["buffer_padding"] == 15`, `res["operations_count"] == 3`, `res["total_duration_minutes"] > 200`.
- `test_tier3_dynamic_settings_recalculation` (lines 373–385):
  Calls `ensure_ism_settings(buffer_padding_minutes=5, estimation_percentile=0.50)` then modifies to `(buffer_padding_minutes=25, estimation_percentile=0.95)` and verifies `dur_p95 > dur_p50`.
- `test_tier4_scenario_full_service_package_estimation` (lines 390–415):
  Calls `get_total_estimate([self.item_brake1, self.item_brake2, self.item_elec, self.item_tire])`.
  Asserts `total == pure + buffer`, `300 < pure < 380`, `buffer == 15`.

### 1.5 Database Column Gap: `Item.custom_frt`
Direct database inspection via `bench execute` reveals:
```
MySQLdb.OperationalError: (1054, "Unknown column 'custom_frt' in 'SELECT'")
```
In `ism/setup.py` (lines 434–481), `setup_item_custom_fields()` defines custom fields for `Item` (`is_vehicle_part`, `part_source`, `part_condition`, `oem_part_number`, `revision`), but **omits** `custom_frt`.

### 1.6 Test Fixture Stale Master Record Gap
In `ism/tests/test_fixtures.py` (lines 261–285), `ensure_items()` only inserts new items if `not frappe.db.exists("Item", item_code)`:
```python
def ensure_items():
    for item_code, data in ITEMS.items():
        if not frappe.db.exists("Item", item_code):
            ...
            if "custom_frt" in data and frappe.db.has_column("Item", "custom_frt"):
                doc_dict["custom_frt"] = data["custom_frt"]
            frappe.get_doc(doc_dict).insert(...)
```
Because `Test EST_ITEM_001` already exists in `tabItem` from previous test runs without the column, once the column is created, `ensure_items()` will skip it and leave `custom_frt` as `NULL` unless an update branch is added or the test items are updated.

---

## 2. Logic Chain

1. **Service Location & Decorator**:
   `PROJECT.md § Code Layout` requires `ism/api/estimation_service.py`. The methods `get_estimate` and `get_total_estimate` must be whitelisted (`@frappe.whitelist()`) so they are accessible both from Python code and via Desk / RPC web requests (`/api/method/ism.api.estimation_service.get_estimate`).

2. **Unit Conversion**:
   In standard automotive practice (and in `test_fixtures.py` lines 80–115), `Item.custom_frt` is stored in **Hours** (e.g. 1.0, 1.5, 0.5, 2.0). However, the mathematical estimation core (`estimation_light.py`) operates in **Minutes**. Therefore, when `_get_frt()` reads `custom_frt` from the database, it must multiply by 60.0 (`val * 60.0`). Conversely, if a caller passes `flat_rate_minutes` / `frt_minutes`, it is already in minutes. If they pass `flat_rate_hours` / `custom_frt` / `qty` in an operation dict, it must be multiplied by 60.0.

3. **Dynamic Settings & Normalization**:
   `ISM Settings` stores `estimation_percentile` as a `Percent` field with default `80.0`. However, tests and callers may pass or set decimals such as `0.80`, `0.50`, `0.95` (as seen in `test_tier3_dynamic_settings_recalculation`). Therefore, `_get_setting_percentile()` must normalize: if `percentile > 1.0`, divide by 100.0 (`80.0 -> 0.80`).
   Similarly, `buffer_padding_minutes` (Int) must default to 0 if not set, and `estimation_sigma` (Float) must default to 0.30 (`DEFAULT_SIGMA`).

4. **Return Schema Conformance**:
   - `get_estimate` must return a dict matching `{"duration_minutes": int, "p_pure": int, "buffer_padding": int, "percentile": float, "sigma": float}`.
   - `get_total_estimate` must return a dict matching `{"total_duration_minutes": int, "p_pure": int, "buffer_padding": int, "operations_count": int}`.
   - If an empty item list is passed to `get_total_estimate`, it must return `total_duration_minutes = 0`, `p_pure = 0`, `operations_count = 0` (buffer padding should not be applied to empty jobs).

5. **Schema Gap Remediation**:
   Without `custom_frt` on `tabItem`, all item lookups fall back to default FRT (60 min), causing multi-item Fenton-Wilkinson summation tests (such as Tier 4 full service package requiring > 300m pure duration) to fail. Adding `custom_frt` to `setup_item_custom_fields()` and providing an update branch in `ensure_items()` ensures seamless test execution.

---

## 3. Caveats

1. **Module Independence**: `ism/api/estimation_service.py` depends on `ism/scheduling/estimation_light.py` (assigned to Explorer 1). Both files must be placed simultaneously by the Worker for the module import to resolve.
2. **Settings Caching**: Do NOT cache `ISM Settings` at Python module scope (e.g. `_SETTINGS = ...`). Tests mutate settings dynamically using `ensure_ism_settings()` within the same process. Fresh retrieval via `frappe.get_single("ISM Settings")` on each function call is required.
3. **No Caveats** regarding external network access or external libraries — the implementation is 100% self-contained and zero-scipy.

---

## 4. Conclusion & Implementation Plan

### 4.1 Plan Step 1: Add `custom_frt` Custom Field to `ism/setup.py`
In `ism/setup.py` inside `setup_item_custom_fields()`, add the field definition:
```python
		{
			"fieldname": "custom_frt",
			"label": "Flat Rate Time (Hours)",
			"fieldtype": "Float",
			"insert_after": "revision",
		},
```
Execute `setup_item_custom_fields()` via `bench execute` or migration.

### 4.2 Plan Step 2: Ensure Fixture Item Updating in `ism/tests/test_fixtures.py`
Update `ensure_items()` in `ism/tests/test_fixtures.py` to update `custom_frt` if the item already exists:
```python
		else:
			if "custom_frt" in data and frappe.db.has_column("Item", item_code):
				curr = frappe.db.get_value("Item", item_code, "custom_frt")
				if curr != data["custom_frt"]:
					frappe.db.set_value("Item", item_code, "custom_frt", data["custom_frt"], update_modified=False)
```

### 4.3 Plan Step 3: Implement `ism/api/estimation_service.py`
Create `ism/api/estimation_service.py` with the complete implementation:

```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from typing import Any, Dict, List, Optional, Union

import frappe

from ism.scheduling.estimation_light import (
	DEFAULT_SIGMA,
	estimate_duration,
	estimate_total_duration,
	get_sigma_for_item,
)


def _get_ism_settings():
	"""Retrieve ISM Settings single doctype safely."""
	if not frappe.db.exists("DocType", "ISM Settings"):
		return None
	try:
		return frappe.get_single("ISM Settings")
	except Exception:
		return None


def _get_setting_percentile(settings, default: float = 0.80) -> float:
	val = getattr(settings, "estimation_percentile", None)
	if val is None or val == "":
		return default
	try:
		p = float(val)
		if p > 1.0:
			p = p / 100.0
		if 0.50 <= p <= 0.99:
			return p
	except (ValueError, TypeError):
		pass
	return default


def _get_setting_sigma(settings, default: float = DEFAULT_SIGMA) -> float:
	val = getattr(settings, "estimation_sigma", None)
	if val is None or val == "":
		return default
	try:
		s = float(val)
		if 0.05 <= s <= 1.0:
			return s
	except (ValueError, TypeError):
		pass
	return default


def _get_setting_buffer(settings, default: int = 0) -> int:
	val = getattr(settings, "buffer_padding_minutes", None)
	if val is None or val == "":
		return default
	try:
		b = int(float(val))
		return max(0, b)
	except (ValueError, TypeError):
		return default


def _get_setting_default_diagnostic(settings, default: float = 60.0) -> float:
	val = getattr(settings, "default_diagnostic_duration", None)
	if val is None or val == "":
		return default
	try:
		d = float(val)
		return d if d > 0 else default
	except (ValueError, TypeError):
		return default


def _get_frt(item_code: str, fallback: Optional[float] = None) -> Optional[float]:
	"""Safely retrieve FRT in MINUTES from Item DocType.

	Item.custom_frt is stored in HOURS (e.g. 1.0 hour, 1.5 hours).
	Returns FRT in MINUTES (e.g. 60.0 minutes, 90.0 minutes).
	"""
	if not item_code:
		return fallback

	if frappe.db.has_column("Item", "custom_frt"):
		if frappe.db.exists("Item", item_code):
			frt = frappe.db.get_value("Item", item_code, "custom_frt")
			if frt is not None:
				try:
					val = float(frt)
					if val > 0:
						return val * 60.0
				except (ValueError, TypeError):
					pass

	return fallback


@frappe.whitelist()
def get_estimate(item_code: Optional[str] = None, **kwargs) -> Dict[str, Any]:
	"""Get risk-adjusted duration estimate for a single operation.

	Args:
		item_code: Item code to look up in Frappe database.
		**kwargs:
			- fallback_frt: Default FRT in minutes if item/custom_frt missing.
			- flat_rate_minutes / frt_minutes: Explicit FRT in minutes.
			- custom_frt / flat_rate_hours: Explicit FRT in hours.
			- sigma: Custom sigma override.
			- percentile: Target percentile (0.50 to 0.99, or 50 to 99).
			- buffer_padding / buffer_padding_minutes: Padding in minutes override.

	Returns:
		Dict with:
			- duration_minutes: int (p_pure + buffer_padding, or 0)
			- p_pure: int (pure log-normal estimate)
			- buffer_padding: int
			- percentile: float
			- sigma: float
	"""
	settings = _get_ism_settings()

	# Resolve percentile
	percentile = kwargs.get("percentile")
	if percentile is not None and percentile != "":
		try:
			p = float(percentile)
			if p > 1.0:
				p = p / 100.0
			percentile = p
		except (ValueError, TypeError):
			percentile = _get_setting_percentile(settings)
	else:
		percentile = _get_setting_percentile(settings)

	# Resolve buffer padding
	buffer_padding = kwargs.get("buffer_padding")
	if buffer_padding is None:
		buffer_padding = kwargs.get("buffer_padding_minutes")
	if buffer_padding is not None and buffer_padding != "":
		try:
			buffer_padding = max(0, int(float(buffer_padding)))
		except (ValueError, TypeError):
			buffer_padding = _get_setting_buffer(settings)
	else:
		buffer_padding = _get_setting_buffer(settings)

	# Resolve default sigma from settings
	settings_sigma = _get_setting_sigma(settings)

	# Resolve fallback FRT
	fallback_frt = kwargs.get("fallback_frt")
	if fallback_frt is not None and fallback_frt != "":
		try:
			fallback_frt = float(fallback_frt)
		except (ValueError, TypeError):
			fallback_frt = None

	# Resolve FRT in minutes
	frt = None
	if kwargs.get("flat_rate_minutes") is not None:
		try:
			frt = float(kwargs["flat_rate_minutes"])
		except (ValueError, TypeError):
			frt = None
	elif kwargs.get("frt_minutes") is not None:
		try:
			frt = float(kwargs["frt_minutes"])
		except (ValueError, TypeError):
			frt = None
	elif kwargs.get("custom_frt") is not None:
		try:
			frt = float(kwargs["custom_frt"]) * 60.0
		except (ValueError, TypeError):
			frt = None
	elif kwargs.get("flat_rate_hours") is not None:
		try:
			frt = float(kwargs["flat_rate_hours"]) * 60.0
		except (ValueError, TypeError):
			frt = None

	if frt is None and item_code:
		default_fb = fallback_frt if fallback_frt is not None else _get_setting_default_diagnostic(settings)
		frt = _get_frt(str(item_code), fallback=default_fb)
	elif frt is None and fallback_frt is not None:
		frt = fallback_frt

	# Resolve sigma
	sigma = kwargs.get("sigma")
	if sigma is not None and sigma != "":
		try:
			sigma = float(sigma)
		except (ValueError, TypeError):
			sigma = None

	if sigma is None:
		if item_code and frappe.db.exists("Item", item_code):
			item_group = frappe.db.get_value("Item", item_code, "item_group")
			sigma = get_sigma_for_item(item_group, default_sigma=settings_sigma)
		else:
			sigma = settings_sigma

	if frt is None or frt <= 0:
		return {
			"duration_minutes": 0,
			"p_pure": 0,
			"buffer_padding": buffer_padding,
			"percentile": percentile,
			"sigma": sigma,
		}

	p_pure = estimate_duration(frt, sigma=sigma, percentile=percentile)
	duration_minutes = p_pure + buffer_padding

	return {
		"duration_minutes": duration_minutes,
		"p_pure": p_pure,
		"buffer_padding": buffer_padding,
		"percentile": percentile,
		"sigma": sigma,
	}


@frappe.whitelist()
def get_total_estimate(
	item_codes: Union[str, List[Union[str, Dict[str, Any]]]],
	**kwargs,
) -> Dict[str, Any]:
	"""Get multi-operation duration estimate using Fenton-Wilkinson summation + buffer.

	Args:
		item_codes: JSON string, list of item codes, or list of operation dicts.
		**kwargs:
			- fallback_frt: Default FRT in minutes for missing items.
			- skip_missing: If True, ignore missing items without valid FRT.
			- sigma: Custom default sigma override.
			- percentile: Target percentile (0.50 to 0.99, or 50 to 99).
			- buffer_padding / buffer_padding_minutes: Padding in minutes override.

	Returns:
		Dict with:
			- total_duration_minutes: int (p_pure + buffer_padding, or 0)
			- p_pure: int (pure F-W log-normal estimate)
			- buffer_padding: int
			- operations_count: int
	"""
	settings = _get_ism_settings()

	# Resolve percentile
	percentile = kwargs.get("percentile")
	if percentile is not None and percentile != "":
		try:
			p = float(percentile)
			if p > 1.0:
				p = p / 100.0
			percentile = p
		except (ValueError, TypeError):
			percentile = _get_setting_percentile(settings)
	else:
		percentile = _get_setting_percentile(settings)

	# Resolve buffer padding
	buffer_padding = kwargs.get("buffer_padding")
	if buffer_padding is None:
		buffer_padding = kwargs.get("buffer_padding_minutes")
	if buffer_padding is not None and buffer_padding != "":
		try:
			buffer_padding = max(0, int(float(buffer_padding)))
		except (ValueError, TypeError):
			buffer_padding = _get_setting_buffer(settings)
	else:
		buffer_padding = _get_setting_buffer(settings)

	# Resolve default sigma from settings
	settings_sigma = _get_setting_sigma(settings)
	override_default_sigma = kwargs.get("sigma")
	if override_default_sigma is not None and override_default_sigma != "":
		try:
			default_sigma = float(override_default_sigma)
		except (ValueError, TypeError):
			default_sigma = settings_sigma
	else:
		default_sigma = settings_sigma

	# Parse skip_missing
	skip_missing = kwargs.get("skip_missing", False)
	if isinstance(skip_missing, str):
		skip_missing = skip_missing.lower() in ("true", "1")
	else:
		skip_missing = bool(skip_missing)

	# Fallback FRT
	fallback_frt = kwargs.get("fallback_frt")
	if fallback_frt is not None and fallback_frt != "":
		try:
			fallback_frt = float(fallback_frt)
		except (ValueError, TypeError):
			fallback_frt = None

	# Parse item_codes
	if isinstance(item_codes, str):
		try:
			item_codes = frappe.parse_json(item_codes)
		except Exception:
			try:
				item_codes = json.loads(item_codes)
			except Exception:
				item_codes = [c.strip() for c in item_codes.split(",") if c.strip()]

	if not item_codes or not isinstance(item_codes, (list, tuple)):
		return {
			"total_duration_minutes": 0,
			"p_pure": 0,
			"buffer_padding": buffer_padding,
			"operations_count": 0,
		}

	operations = []
	for item in item_codes:
		if isinstance(item, dict):
			code = item.get("item_code")
			item_frt_min = item.get("flat_rate_minutes")
			if item_frt_min is None:
				item_frt_min = item.get("frt_minutes")
			item_frt_hr = (
				item.get("flat_rate_hours")
				or item.get("frt_hours")
				or item.get("custom_frt")
				or item.get("qty")
			)
			item_sig = item.get("sigma")

			if item_frt_min is not None:
				try:
					item_frt = float(item_frt_min)
				except (ValueError, TypeError):
					item_frt = None
			elif item_frt_hr is not None:
				try:
					item_frt = float(item_frt_hr) * 60.0
				except (ValueError, TypeError):
					item_frt = None
			else:
				item_frt = None
		elif isinstance(item, (int, float)):
			code = None
			item_frt = float(item)
			item_sig = None
		else:
			code = str(item).strip() if item else None
			item_frt = None
			item_sig = None

		if item_frt is None and code:
			effective_fallback = None if skip_missing else (
				fallback_frt if fallback_frt is not None else _get_setting_default_diagnostic(settings)
			)
			item_frt = _get_frt(code, fallback=effective_fallback)

		if item_frt is None or item_frt <= 0:
			if skip_missing:
				continue
			item_frt = fallback_frt if fallback_frt and fallback_frt > 0 else None

		if item_frt is None or item_frt <= 0:
			continue

		if item_sig is None:
			if code and frappe.db.exists("Item", code):
				item_group = frappe.db.get_value("Item", code, "item_group")
				item_sig = get_sigma_for_item(item_group, default_sigma=default_sigma)
			else:
				item_sig = default_sigma
		else:
			try:
				item_sig = float(item_sig)
			except (ValueError, TypeError):
				item_sig = default_sigma

		operations.append({"flat_rate_minutes": float(item_frt), "sigma": float(item_sig)})

	if not operations:
		return {
			"total_duration_minutes": 0,
			"p_pure": 0,
			"buffer_padding": buffer_padding,
			"operations_count": 0,
		}

	p_pure = estimate_total_duration(operations, percentile=percentile)
	total_duration_minutes = p_pure + buffer_padding

	return {
		"total_duration_minutes": total_duration_minutes,
		"p_pure": p_pure,
		"buffer_padding": buffer_padding,
		"operations_count": len(operations),
	}
```

---

## 5. Verification Method

1. **Host Sync**:
   Sync modified and new files to WSL bench:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
   ```

2. **Schema Migration & Setup**:
   Ensure `custom_frt` is added to `tabItem`:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost execute ism.setup.setup_item_custom_fields
   ```

3. **Verify Column Presence**:
   Verify `tabItem` has `custom_frt`:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost execute "frappe.db.has_column" --args "('Item', 'custom_frt')"
   ```

4. **Execute Full Estimation Test Suite**:
   Run the 41-test suite in `test_estimation_math`:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math
   ```
   **Pass Condition**: 41 passed, 0 failed, 0 skipped.

5. **Execute Backward-Compatible Test Proxy**:
   Run `test_estimation`:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation
   ```
   **Pass Condition**: 41 passed, 0 failed, 0 skipped.
