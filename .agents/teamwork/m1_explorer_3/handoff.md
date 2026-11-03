# Handoff Report: Milestone 1 - ISM Settings Schema, Seeding & Migration Safety Specification

**Author**: M1 Explorer 3 (`teamwork_preview_explorer`)  
**Target Milestone**: Milestone 1 (Resource & Capacity Data Models & Settings Consolidation)  
**Target Files**:
- `ism/ism/doctype/ism_settings/ism_settings.json`
- `ism/ism/doctype/ism_settings/ism_settings.py`
- `ism/setup.py`
- `ism/ism/doctype/ism_settings/test_ism_settings.py` (New Unit Test Suite)

---

## 1. Observation

### A. Existing `ISM Settings` DocType
- **File Path**: `d:\ism\ism\ism\doctype\ism_settings\ism_settings.json` (lines 1–104).
- **DocType Type**: Singleton DocType (`"issingle": 1`).
- **Current `field_order`**:
  ```json
  "field_order": [
   "sb_approval_settings",
   "authorization_token_expiry_hours",
   "cost_tolerance_percentage",
   "cost_tolerance_flat_amount",
   "disclaimer_text",
   "sb_sms_settings",
   "sms_gateway_provider",
   "sms_gateway_endpoint",
   "sms_gateway_api_key",
   "sms_sender_id"
  ]
  ```
- **Controller File**: `ism/ism/doctype/ism_settings/ism_settings.py` (lines 1–8):
  ```python
  from frappe.model.document import Document

  class ISMSettings(Document):
      pass
  ```
  Currently has no validation logic.

### B. Existing Seeding in `ism/setup.py` & Hooks
- **File Path**: `d:\ism\ism\setup.py` (lines 88–103):
  ```python
  def setup_default_ism_settings():
      """Initialize default ISM Settings if not populated."""
      settings = frappe.get_single("ISM Settings")
      if not settings.authorization_token_expiry_hours:
          settings.authorization_token_expiry_hours = 48
      if not settings.cost_tolerance_percentage:
          settings.cost_tolerance_percentage = 10.0
      if not settings.cost_tolerance_flat_amount:
          settings.cost_tolerance_flat_amount = 50.0
      if not settings.disclaimer_text:
          settings.disclaimer_text = (
              "I hereby authorize the repair work listed above to be done along with the necessary materials. "
              "You and your employees may operate the vehicle for purpose of testing, inspection, or delivery."
          )
      settings.save(ignore_permissions=True)
  ```
- **Migration & Install Hooks**:
  - `ism/hooks.py` (line 95): `after_install = "ism.setup.after_install"`
  - `ism/hooks.py` (line 143): `after_migrate = ["ism.setup.after_migrate"]`
  - In `ism/setup.py` (lines 506–522), `after_migrate()` directly calls `setup_default_ism_settings()`, and `after_install()` delegates to `after_migrate()`.

### C. Reference Implementation in `induct_shop`
- **Reference DocType**: `$env:TEMP\induct_shop\induct_shop\induct_shop\doctype\shop_settings\shop_settings.json` (lines 1–113).
  - Used separate singleton DocType `Shop Settings` with fields:
    - `operating_hours_start` (Time, default `"08:00:00"`, `reqd: 1`)
    - `operating_hours_end` (Time, default `"17:00:00"`, `reqd: 1`)
    - `break_start` (Time, default `"12:00:00"`)
    - `break_end` (Time, default `"12:30:00"`)
    - `default_slot_interval` (Int, default `30`, `reqd: 1`)
    - `scheduling_horizon_days` (Int, default `30`, `reqd: 1`)
    - `holiday_list` (Link: `Holiday List`)
    - `technician_designation` (Data, default `"Technician"`)
    - `enable_technician_capacity` (Check, default `1`)
- **Survey Handshake**: In ISM, `ORIGINAL_REQUEST.md` R4 consolidates scheduling and estimation parameters into `ISM Settings` rather than creating a separate DocType.

### D. Benchmark Execution & WSL2 Container Verification
- **Baseline Test Run**:
  Command: `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"`
  Output:
  ```
  Running 78 unspecified-category tests for ism
  ism.tests.test_parts_management.TestPartsManagement: 30 tests passed
  ism.tests.test_tesla_integration.TestTeslaIntegration: 7 tests passed
  ism.ism.doctype.repair_vehicle.test_repair_vehicle.TestRepairVehicle: 6 tests passed
  ism.ism.doctype.repair_order.test_repair_order.TestRepairOrder: 35 tests passed
  ----------------------------------------------------------------------
  Ran 78 tests in 32.201s
  OK
  ```
- **Sync Script Verification**:
  Command: `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
  Output: `[sync-to-wsl] Complete.` (Exit code 0).
- **Migration Execution**:
  Command: `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"`
  Output: Exit code 0, executed all `after_migrate` hooks and updated all DocTypes.

---

## 2. Logic Chain

### A. Consolidation of Scheduling & Estimation into `ISM Settings`
1. Per `ORIGINAL_REQUEST.md` R4 and `DISPATCH.md`, ISM unifies all operational configuration in `ISM Settings` (Singleton DocType).
2. Existing sections `sb_approval_settings` and `sb_sms_settings` are retained without modification.
3. Three new sections are added:
   - `sb_scheduling` (Label: "Scheduling & Shop Hours"): Defines shop operating hours, lunch break window, slot interval, scheduling horizon, and optional holiday list.
   - `sb_capacity` (Label: "Resource Capacity"): Defines technician designation (for querying active technicians from `Employee`) and the capacity gating master toggle.
   - `sb_estimation` (Label: "Duration Estimation Tuning"): Defines parameters for the Fenton-Wilkinson duration estimation service (`estimation_percentile`, `estimation_sigma`, `buffer_padding_minutes`, and `default_diagnostic_duration`).

### B. Defensive Controller Validation in `ISMSettings.validate()`
1. If operating hours start is later than or equal to operating hours end, slot generation creates empty or infinite loops. Validating `start < end` prevents invalid schedule configurations.
2. If lunch break start is later than break end, or falls outside operating hours, effective end time calculations produce negative durations. Validating `op_start <= break_start < break_end <= op_end` ensures math consistency.
3. For `estimation_percentile`: If an administrator or script inputs a fraction (e.g. `0.80`), auto-normalizing to `80.0` prevents display confusion while validating that the percentile target remains within standard confidence limits (50.0% to 99.0%).
4. For `estimation_sigma`: Fenton-Wilkinson log-normal summation divides by `sigma`. Enforcing `0.05 <= sigma <= 1.00` guarantees numerical stability.
5. For `buffer_padding_minutes`: Negative buffer values are forbidden (`buffer >= 0`).

### C. Safe Default Seeding in `setup.py`
1. For Check (boolean) fields like `enable_technician_capacity` (default 1) and Int fields like `buffer_padding_minutes` (default 0), standard truthy checks (`if not settings.field:`) are dangerous because if an administrator intentionally sets `enable_technician_capacity = 0`, an `after_migrate` run would overwrite it back to `1`.
2. Therefore, using `if settings.get("enable_technician_capacity") is None:` and `if settings.get("buffer_padding_minutes") is None:` guarantees that:
   - On initial migration, uninitialized fields (`None`) receive their defaults (`1` and `0`).
   - On subsequent migrations, intentional `0` values configured by the shop administrator are strictly preserved.

### D. Zero-DDL Migration Safety Analysis
1. In Frappe Framework, Single DocTypes (`"issingle": 1`) do NOT map to a separate SQL table `tabISM Settings`. All values are stored as rows in `tabSingles (doctype, field, value)`.
2. Consequently, modifying `ism_settings.json` never issues `ALTER TABLE` statements or acquires table metadata locks.
3. `bench migrate` reads the JSON schema, updates `tabDocType` and `tabDocField`, and executes `setup_default_ism_settings()` which performs standard row inserts/updates in `tabSingles`.
4. This guarantees 100% migration safety and zero schema deadlocks during deployment.

---

## 3. Concrete Implementation Plan & Code Specifications

### Specification 1: `ism/ism/doctype/ism_settings/ism_settings.json`
Replace the file contents with the following exact JSON schema:

```json
{
 "actions": [],
 "creation": "2026-10-06 00:00:00.000000",
 "doctype": "DocType",
 "engine": "InnoDB",
 "field_order": [
  "sb_approval_settings",
  "authorization_token_expiry_hours",
  "cost_tolerance_percentage",
  "cost_tolerance_flat_amount",
  "disclaimer_text",
  "sb_sms_settings",
  "sms_gateway_provider",
  "sms_gateway_endpoint",
  "sms_gateway_api_key",
  "sms_sender_id",
  "sb_scheduling",
  "operating_hours_start",
  "operating_hours_end",
  "break_start",
  "break_end",
  "cb_scheduling",
  "default_slot_interval",
  "scheduling_horizon_days",
  "holiday_list",
  "sb_capacity",
  "technician_designation",
  "enable_technician_capacity",
  "sb_estimation",
  "estimation_percentile",
  "estimation_sigma",
  "cb_estimation",
  "buffer_padding_minutes",
  "default_diagnostic_duration"
 ],
 "fields": [
  {
   "fieldname": "sb_approval_settings",
   "fieldtype": "Section Break",
   "label": "Customer Approval Settings"
  },
  {
   "default": "48",
   "fieldname": "authorization_token_expiry_hours",
   "fieldtype": "Int",
   "label": "Authorization Token Expiry (Hours)"
  },
  {
   "default": "10.0",
   "fieldname": "cost_tolerance_percentage",
   "fieldtype": "Percent",
   "label": "Cost Tolerance (%)"
  },
  {
   "default": "20.0",
   "fieldname": "cost_tolerance_flat_amount",
   "fieldtype": "Currency",
   "label": "Cost Tolerance Cap ($)"
  },
  {
   "default": "I hereby authorize the repair work and necessary materials as itemized above. I agree that the shop is not responsible for loss or damage to vehicle in case of fire, theft, or any other cause beyond your control.",
   "fieldname": "disclaimer_text",
   "fieldtype": "Text",
   "label": "Customer Legal Disclaimer Text"
  },
  {
   "fieldname": "sb_sms_settings",
   "fieldtype": "Section Break",
   "label": "Outbound SMS Gateway Settings"
  },
  {
   "default": "Custom HTTP Webhook",
   "fieldname": "sms_gateway_provider",
   "fieldtype": "Select",
   "label": "SMS Gateway Provider",
   "options": "Custom HTTP Webhook\nTwilio\nDisabled"
  },
  {
   "fieldname": "sms_gateway_endpoint",
   "fieldtype": "Data",
   "label": "SMS Gateway Endpoint URL"
  },
  {
   "fieldname": "sms_gateway_api_key",
   "fieldtype": "Password",
   "label": "API Key / Auth Token"
  },
  {
   "default": "ISM",
   "fieldname": "sms_sender_id",
   "fieldtype": "Data",
   "label": "Sender ID / From Number"
  },
  {
   "fieldname": "sb_scheduling",
   "fieldtype": "Section Break",
   "label": "Scheduling & Shop Hours"
  },
  {
   "default": "08:00:00",
   "fieldname": "operating_hours_start",
   "fieldtype": "Time",
   "label": "Operating Hours Start",
   "reqd": 1
  },
  {
   "default": "17:00:00",
   "fieldname": "operating_hours_end",
   "fieldtype": "Time",
   "label": "Operating Hours End",
   "reqd": 1
  },
  {
   "default": "12:00:00",
   "fieldname": "break_start",
   "fieldtype": "Time",
   "label": "Break Start"
  },
  {
   "default": "12:30:00",
   "fieldname": "break_end",
   "fieldtype": "Time",
   "label": "Break End"
  },
  {
   "fieldname": "cb_scheduling",
   "fieldtype": "Column Break"
  },
  {
   "default": "30",
   "fieldname": "default_slot_interval",
   "fieldtype": "Int",
   "label": "Default Slot Interval (Minutes)",
   "reqd": 1
  },
  {
   "default": "30",
   "fieldname": "scheduling_horizon_days",
   "fieldtype": "Int",
   "label": "Scheduling Horizon (Days)",
   "reqd": 1
  },
  {
   "fieldname": "holiday_list",
   "fieldtype": "Link",
   "label": "Holiday List",
   "options": "Holiday List"
  },
  {
   "fieldname": "sb_capacity",
   "fieldtype": "Section Break",
   "label": "Resource Capacity"
  },
  {
   "default": "Technician",
   "fieldname": "technician_designation",
   "fieldtype": "Data",
   "label": "Technician Designation"
  },
  {
   "default": "1",
   "fieldname": "enable_technician_capacity",
   "fieldtype": "Check",
   "label": "Enable Technician Capacity"
  },
  {
   "fieldname": "sb_estimation",
   "fieldtype": "Section Break",
   "label": "Duration Estimation Tuning"
  },
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
   "fieldname": "cb_estimation",
   "fieldtype": "Column Break"
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
 ],
 "issingle": 1,
 "links": [],
 "modified": "2026-10-26 12:00:00.000000",
 "modified_by": "Administrator",
 "module": "ISM",
 "name": "ISM Settings",
 "owner": "Administrator",
 "permissions": [
  {
   "create": 1,
   "delete": 1,
   "email": 1,
   "export": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "System Manager",
   "share": 1,
   "write": 1
  }
 ],
 "row_format": "Dynamic",
 "sort_field": "creation",
 "sort_order": "DESC",
 "states": [],
 "track_changes": 1
}
```

---

### Specification 2: `ism/ism/doctype/ism_settings/ism_settings.py`
Replace the controller with the following implementation:

```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import to_timedelta


class ISMSettings(Document):
	def validate(self):
		self._validate_operating_hours()
		self._validate_break_hours()
		self._validate_estimation_parameters()

	def _validate_operating_hours(self):
		if self.operating_hours_start and self.operating_hours_end:
			start = to_timedelta(self.operating_hours_start)
			end = to_timedelta(self.operating_hours_end)
			if start >= end:
				frappe.throw(_("Operating Hours Start must be before Operating Hours End."))

		if self.default_slot_interval is not None and self.default_slot_interval <= 0:
			frappe.throw(_("Default Slot Interval must be greater than 0 minutes."))

		if self.scheduling_horizon_days is not None and self.scheduling_horizon_days <= 0:
			frappe.throw(_("Scheduling Horizon must be greater than 0 days."))

	def _validate_break_hours(self):
		if self.break_start and self.break_end:
			b_start = to_timedelta(self.break_start)
			b_end = to_timedelta(self.break_end)
			if b_start >= b_end:
				frappe.throw(_("Break Start must be before Break End."))

			if self.operating_hours_start and self.operating_hours_end:
				op_start = to_timedelta(self.operating_hours_start)
				op_end = to_timedelta(self.operating_hours_end)
				if b_start < op_start or b_end > op_end:
					frappe.throw(_("Lunch break window must be within shop operating hours."))

	def _validate_estimation_parameters(self):
		if self.estimation_percentile is not None:
			# Auto-convert decimal fraction (e.g. 0.80) to percentage (80.0)
			if 0 < self.estimation_percentile <= 1.0:
				self.estimation_percentile = self.estimation_percentile * 100.0

			if self.estimation_percentile < 50.0 or self.estimation_percentile > 99.0:
				frappe.throw(_("Estimation Percentile must be between 50% and 99%."))

		if self.estimation_sigma is not None:
			if self.estimation_sigma < 0.05 or self.estimation_sigma > 1.0:
				frappe.throw(_("Estimation Sigma must be between 0.05 and 1.00."))

		if self.buffer_padding_minutes is not None and self.buffer_padding_minutes < 0:
			frappe.throw(_("Buffer Padding cannot be negative."))

		if self.default_diagnostic_duration is not None and self.default_diagnostic_duration <= 0:
			frappe.throw(_("Default Diagnostic Duration must be greater than 0 minutes."))
```

---

### Specification 3: `ism/setup.py`
Update `setup_default_ism_settings()` (lines 88–103) as follows:

```python
def setup_default_ism_settings():
	"""Initialize default ISM Settings if not populated."""
	settings = frappe.get_single("ISM Settings")
	changed = False

	# Customer Approval Settings
	if not settings.authorization_token_expiry_hours:
		settings.authorization_token_expiry_hours = 48
		changed = True
	if not settings.cost_tolerance_percentage:
		settings.cost_tolerance_percentage = 10.0
		changed = True
	if not settings.cost_tolerance_flat_amount:
		settings.cost_tolerance_flat_amount = 50.0
		changed = True
	if not settings.disclaimer_text:
		settings.disclaimer_text = (
			"I hereby authorize the repair work listed above to be done along with the necessary materials. "
			"You and your employees may operate the vehicle for purpose of testing, inspection, or delivery."
		)
		changed = True

	# Scheduling & Shop Hours
	if not settings.operating_hours_start:
		settings.operating_hours_start = "08:00:00"
		changed = True
	if not settings.operating_hours_end:
		settings.operating_hours_end = "17:00:00"
		changed = True
	if not settings.break_start:
		settings.break_start = "12:00:00"
		changed = True
	if not settings.break_end:
		settings.break_end = "12:30:00"
		changed = True
	if not settings.default_slot_interval:
		settings.default_slot_interval = 30
		changed = True
	if not settings.scheduling_horizon_days:
		settings.scheduling_horizon_days = 30
		changed = True

	# Resource Capacity
	if not settings.technician_designation:
		settings.technician_designation = "Technician"
		changed = True
	if settings.get("enable_technician_capacity") is None:
		settings.enable_technician_capacity = 1
		changed = True

	# Duration Estimation Tuning
	if not settings.estimation_percentile:
		settings.estimation_percentile = 80.0
		changed = True
	if not settings.estimation_sigma:
		settings.estimation_sigma = 0.30
		changed = True
	if settings.get("buffer_padding_minutes") is None:
		settings.buffer_padding_minutes = 0
		changed = True
	if not settings.default_diagnostic_duration:
		settings.default_diagnostic_duration = 60
		changed = True

	if changed or settings.is_new():
		settings.save(ignore_permissions=True)
```

Note: In `after_migrate()`, `setup_default_ism_settings()` is already called at line 510, and `after_install()` delegates to `after_migrate()`. No hook registration change is needed in `hooks.py`.

---

### Specification 4: Dedicated Unit Test Suite `ism/ism/doctype/ism_settings/test_ism_settings.py`
Create a new unit test suite:

```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import unittest
import frappe
from frappe.utils import to_timedelta
from ism.setup import setup_default_ism_settings


class TestISMSettings(unittest.TestCase):
	def setUp(self):
		setup_default_ism_settings()

	def test_default_ism_settings_initialized(self):
		settings = frappe.get_single("ISM Settings")
		self.assertIsNotNone(settings)
		self.assertEqual(str(settings.operating_hours_start), "08:00:00")
		self.assertEqual(str(settings.operating_hours_end), "17:00:00")
		self.assertEqual(str(settings.break_start), "12:00:00")
		self.assertEqual(str(settings.break_end), "12:30:00")
		self.assertEqual(settings.default_slot_interval, 30)
		self.assertEqual(settings.scheduling_horizon_days, 30)
		self.assertEqual(settings.technician_designation, "Technician")
		self.assertEqual(settings.enable_technician_capacity, 1)
		self.assertEqual(settings.estimation_percentile, 80.0)
		self.assertAlmostEqual(settings.estimation_sigma, 0.30, places=2)
		self.assertEqual(settings.buffer_padding_minutes, 0)
		self.assertEqual(settings.default_diagnostic_duration, 60)

	def test_validate_operating_hours_order(self):
		settings = frappe.get_single("ISM Settings")
		original_start = settings.operating_hours_start
		try:
			settings.operating_hours_start = "18:00:00"
			self.assertRaises(frappe.ValidationError, settings.save)
		finally:
			settings.operating_hours_start = original_start
			settings.save(ignore_permissions=True)

	def test_validate_break_hours_order(self):
		settings = frappe.get_single("ISM Settings")
		original_start = settings.break_start
		try:
			settings.break_start = "13:00:00"
			self.assertRaises(frappe.ValidationError, settings.save)
		finally:
			settings.break_start = original_start
			settings.save(ignore_permissions=True)

	def test_validate_break_within_operating_hours(self):
		settings = frappe.get_single("ISM Settings")
		original_start = settings.break_start
		try:
			settings.break_start = "07:00:00"
			self.assertRaises(frappe.ValidationError, settings.save)
		finally:
			settings.break_start = original_start
			settings.save(ignore_permissions=True)

	def test_validate_estimation_percentile_bounds(self):
		settings = frappe.get_single("ISM Settings")
		orig_p = settings.estimation_percentile
		try:
			settings.estimation_percentile = 40.0
			self.assertRaises(frappe.ValidationError, settings.save)

			settings.estimation_percentile = 100.0
			self.assertRaises(frappe.ValidationError, settings.save)

			# Auto-conversion from 0.85 to 85.0
			settings.estimation_percentile = 0.85
			settings.save(ignore_permissions=True)
			reloaded = frappe.get_single("ISM Settings")
			self.assertEqual(reloaded.estimation_percentile, 85.0)
		finally:
			settings.estimation_percentile = orig_p
			settings.save(ignore_permissions=True)

	def test_validate_estimation_sigma_bounds(self):
		settings = frappe.get_single("ISM Settings")
		orig_s = settings.estimation_sigma
		try:
			settings.estimation_sigma = 0.02
			self.assertRaises(frappe.ValidationError, settings.save)

			settings.estimation_sigma = 1.5
			self.assertRaises(frappe.ValidationError, settings.save)
		finally:
			settings.estimation_sigma = orig_s
			settings.save(ignore_permissions=True)

	def test_setup_idempotency_preserves_customizations(self):
		settings = frappe.get_single("ISM Settings")
		orig_interval = settings.default_slot_interval
		orig_cap = settings.enable_technician_capacity
		try:
			settings.default_slot_interval = 45
			settings.enable_technician_capacity = 0
			settings.save(ignore_permissions=True)

			# Re-run setup_default_ism_settings
			setup_default_ism_settings()

			reloaded = frappe.get_single("ISM Settings")
			self.assertEqual(reloaded.default_slot_interval, 45)
			self.assertEqual(reloaded.enable_technician_capacity, 0)
		finally:
			settings.default_slot_interval = orig_interval
			settings.enable_technician_capacity = orig_cap
			settings.save(ignore_permissions=True)
```

---

## 4. Caveats

1. **Time Representation in Frappe Controllers**:
   - In Frappe, `Time` fields are returned by the ORM as either `datetime.timedelta` objects (e.g. `timedelta(seconds=28800)`) or ISO time strings (`"08:00:00"`).
   - In `ISMSettings.validate()`, always use `frappe.utils.to_timedelta()` before performing comparison operators (`<`, `<=`, `>`, `>=`) to avoid Python `TypeError` between `str` and `timedelta`.
2. **Holiday List Pre-requisites**:
   - `holiday_list` links to standard ERPNext `Holiday List`. In fresh test environments without initialized holidays, the field can safely remain null (`None`). Scheduling logic must gracefully handle `if not settings.holiday_list: return False`.
3. **Concurrent Test Execution**:
   - Running `bench migrate` while tests are executing can trigger MariaDB row lock timeouts on test fixture tables. Run `bench migrate` sequentially before test execution.

---

## 5. Conclusion

- The specification provides complete, production-ready schema and code definitions for `ISM Settings`, incorporating all 13 required fields across `sb_scheduling`, `sb_capacity`, and `sb_estimation`.
- `setup_default_ism_settings()` is updated with defensive `is None` checks, ensuring complete idempotency and safe migration.
- Controller-level validation protects downstream estimation and scheduling engines against division-by-zero or inverted time windows.
- Because `ISM Settings` is an `issingle: 1` DocType backed by `tabSingles`, migration carries zero DDL database lock risk.
- The sync script (`scripts/sync-to-wsl.ps1`) and container migration workflow were fully verified and demonstrated 100% clean execution.

---

## 6. Verification Method

To independently verify the schema, code, migration, and tests once implemented:

1. **Sync Files from Windows to WSL2 Bench**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
   ```
   *Expected Output*: `[sync-to-wsl] Complete.`

2. **Execute Migration on Docker Site**:
   ```powershell
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"
   ```
   *Expected Output*: Exit code 0, updates DocTypes and executes `setup_default_ism_settings()`.

3. **Verify Settings in Database via Bench Console**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost execute frappe.db.get_single_value --kwargs "{'doctype':'ISM Settings','fieldname':'operating_hours_start'}"
   ```
   *Expected Output*: `08:00:00`

4. **Run the Automated Test Suite**:
   ```powershell
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
   ```
   *Expected Output*: All tests pass (including new `TestISMSettings`), 0 failures, 0 errors.
