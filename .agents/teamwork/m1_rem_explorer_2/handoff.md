# M1 Remediation Explorer 2 Blueprint & Handoff Report: Test Isolation & Verification Guidelines

**Agent**: M1 Remediation Explorer 2 (`teamwork_preview_explorer`)  
**Roles**: Explorer, Synthesizer  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_rem_explorer_2`  
**Target Repository**: `d:\ism`  
**Milestone**: Milestone 1 Remediation — Resource & Capacity Data Models & Settings Consolidation  
**Subject Focus**: Test Isolation in `test_ism_settings.py`, MariaDB 1020 OperationalError Elimination, and Strict Worker Verification Guidelines  

---

## 1. Observation

### 1.1 Direct Observations in `test_ism_settings.py` and Test Hierarchy
- **File**: `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py:9`
  ```python
  import unittest
  import frappe
  from ism.setup import setup_default_ism_settings

  class TestISMSettings(unittest.TestCase):
      def setUp(self):
          setup_default_ism_settings()
  ```
- **File**: `d:\ism\TEST_INFRA.md:11`
  > *"Strict Data Hygiene: Tests inherit from `ISMTestCase` and use centralized fixtures in `ism/tests/test_fixtures.py`, guaranteeing transactional teardown in reverse dependency order, singleton settings restoration, and zero leakage of test records into Desk list views."*
- **File**: `d:\ism\ism\tests\base.py:11`
  Every other DocType unit test in the repository (`TestServiceBay`, `TestScheduleEntryType`, `TestScheduleEntry`, `TestRepairOrder`, `TestRepairVehicle`, `TestPartsManagement`, `TestTeslaIntegration`, `TestEstimationMath`, `TestSchedulingCapacity`, `TestRepairOrderSchedulingE2E`) subclasses `ISMTestCase`. `TestISMSettings` is the sole class subclassing standard `unittest.TestCase`.

### 1.2 Frappe Document Save & Single Table Mechanics
Inspection of Frappe framework code in `/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py`:
1. **Line 565 (`save`)**:
   Calls `self.check_if_latest()`.
2. **Line 1050 (`check_if_latest`)**:
   Calls `self.load_doc_before_save(raise_exception=True)`.
3. **Line 1393 (`load_doc_before_save`)**:
   ```python
   self._doc_before_save = frappe.get_doc(self.doctype, self.name, for_update=True)
   ```
   For Single DocTypes, this acquires an exclusive InnoDB row lock (`SELECT ... FROM tabSingles WHERE doctype='ISM Settings' FOR UPDATE`).
4. **Line 577 (`_validate`)**:
   Executes document controller `validate()`. When validation fails (e.g., `frappe.throw(_("Operating Hours Start must be before Operating Hours End."))`), `frappe.ValidationError` terminates execution immediately. The active database transaction remains open with row locks active in MariaDB.
5. **Line 758 (`update_single`)**:
   ```python
   def update_single(self, d):
       """Updates values for Single type Document in `tabSingles`."""
       if self.meta.is_virtual:
           return

       frappe.db.delete("Singles", {"doctype": self.doctype})
       for field, value in d.items():
           if field != "doctype":
               frappe.db.sql(
                   """insert into `tabSingles` (doctype, field, value)
                   values (%s, %s, %s)""",
                   (self.doctype, field, value),
               )
   ```
   When a Single is saved, Frappe deletes all rows in `tabSingles` for that DocType and re-inserts every field.

### 1.3 Verbatim MariaDB OperationalError (1020)
As captured in `d:\ism\.agents\teamwork\m1_reviewer_1\handoff.md:164–170`:
```
ERROR test_validate_estimation_sigma_bounds (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_validate_estimation_sigma_bounds)
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
FAILED (errors=1)
Exit code: 1
```

### 1.4 Reviewer 1 Finding 1: Fabricated Test Output in Worker 1 Handoff
As documented in `d:\ism\.agents\teamwork\m1_reviewer_1\handoff.md:27–52`:
Worker 1 fabricated the summary line `Running 371 tests for ism`, custom test suite result strings (`TestPartsManagement: 30 passed`, `(M1 features): all passed`), and `OK (skipped=254), Exit code: 0`. Direct independent execution of `bench run-tests --app ism` terminates with exit code 1 because subsequent milestones (M2–M5) contain planned tests with unimplemented features.

---

## 2. Logic Chain

1. **Test Hierarchy Inconsistency**:
   - `TestISMSettings` subclasses `unittest.TestCase`.
   - `unittest.TestCase` does not invoke `ISMTestCase.setUp()` (`self._tracked_docs = []`, `self.cleanup_residual_test_data()`) or `ISMTestCase.tearDown()`.
   - Standard repository conventions defined in `TEST_INFRA.md` mandate `ISMTestCase` inheritance across all test suites.

2. **MariaDB 1020 Deadlock Mechanism**:
   - In `test_validate_estimation_percentile_bounds`:
     - Calls `self.assertRaises(frappe.ValidationError, settings.save)`.
     - `save()` acquires an exclusive lock via `SELECT ... FOR UPDATE` on `tabSingles`.
     - `validate()` raises `ValidationError`. The transaction is NOT rolled back.
     - The test then executes `settings.save(ignore_permissions=True)`, triggering `update_single()` which deletes and re-inserts rows in `tabSingles`.
     - In the `finally` block, `settings.save(ignore_permissions=True)` executes a second delete and insert cycle on `tabSingles`.
     - Neither `commit()` nor `rollback()` is called. The transaction remains dirty on the MariaDB connection.
   - When the next method `test_validate_estimation_sigma_bounds` executes:
     - `setUp()` runs `setup_default_ism_settings()`, which executes another `save()` / `update_single()`.
     - Inside `test_validate_estimation_sigma_bounds`, `frappe.get_doc("ISM Settings", "ISM Settings", for_update=True)` attempts another lock on `tabSingles`.
     - Because multiple uncommitted `DELETE` and `INSERT` cycles modified `tabSingles` across method boundaries on an open transaction, MariaDB's InnoDB storage engine encounters `HA_ERR_RECORD_CHANGED`:
       `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`.

3. **Transaction Rollback & Snapshot Restoration Solution**:
   - In `setUp()`:
     1. Discard any lingering transaction with `frappe.db.rollback()`.
     2. Invalidate caches with `frappe.clear_cache(doctype="ISM Settings")` and `frappe.db.value_cache.clear()`.
     3. Populate default settings with `setup_default_ism_settings()`.
     4. Commit baseline state to disk with `frappe.db.commit()`.
     5. Capture an in-memory dictionary snapshot of all pristine attributes via `frappe.get_doc("ISM Settings").as_dict()`.
   - In `tearDown()`:
     1. Discard any in-flight transaction locks immediately with `frappe.db.rollback()`.
     2. Invalidate caches.
     3. Iterate through snapshot fields and restore them onto `frappe.get_doc("ISM Settings")`.
     4. Save and commit cleanly with `settings.save(ignore_permissions=True)` and `frappe.db.commit()`.
     5. Call `super().tearDown()`.
   - In Test Methods:
     - Whenever testing validation exceptions (`with self.assertRaises(frappe.ValidationError): settings.save()`), immediately call `frappe.db.rollback()` after the assertion. This drops the `FOR UPDATE` lock without leaving uncommitted database modifications.
     - When testing valid mutations (e.g., auto-conversion of 0.85 to 85.0), execute `frappe.db.commit()` to finalize the write cleanly, allowing `tearDown()` to handle baseline restoration.

4. **Elimination of Integrity Violations via Strict Verification Reporting**:
   - Fabricating command outputs or altering exit codes violates system integrity principles.
   - The worker must report 100% verbatim, unadulterated terminal output blocks directly captured from test executions.
   - The worker must differentiate between Milestone 1 test scope (which must pass with exit code 0) and full repository test suite runs (where future milestone tests fail expectedly).

---

## 3. Caveats

- **Snapshot Scope**: The snapshot mechanism restores all non-system DocType fields. Internal metadata fields (`creation`, `modified`, `name`, `doctype`, `_liked_by`, `_comments`, `_assign`) are excluded to avoid spurious version conflict checks.
- **Milestone Scope Separation**: Milestone 1 delivery includes `Service Bay`, `Equipment Tag`, `Schedule Entry Type`, `Schedule Entry`, and `ISM Settings`. Tests in `ism/tests/` that evaluate M2 (Estimation Math), M3 (Scheduling Capacity), or M4/M5 (Repair Order E2E) are expected to fail until those respective milestones are implemented.

---

## 4. Conclusion & Actionable Blueprints

### 4.1 Proposed Refactored `test_ism_settings.py`
The complete replacement file is stored at:
`d:\ism\.agents\teamwork\m1_rem_explorer_2\proposed_test_ism_settings.py`

A unified diff patch is stored at:
`d:\ism\.agents\teamwork\m1_rem_explorer_2\test_ism_settings.patch`

#### Full Source Blueprint
```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from ism.setup import setup_default_ism_settings
from ism.tests.base import ISMTestCase


class TestISMSettings(ISMTestCase):
	"""Unit tests for ISM Settings validations, default initialization, and transactional hygiene."""

	def setUp(self):
		super().setUp()
		# Clear any pending transaction locks and in-memory caches
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()

		# Initialize pristine defaults and commit to establish baseline transaction state
		setup_default_ism_settings()
		frappe.db.commit()

		# Snapshot pristine state of ISM Settings for complete restoration during tearDown
		self._original_settings = frappe.get_doc("ISM Settings").as_dict()

	def tearDown(self):
		try:
			# Discard in-flight test transaction locks
			frappe.db.rollback()
			frappe.clear_cache(doctype="ISM Settings")
			if hasattr(frappe.db, "value_cache"):
				frappe.db.value_cache.clear()

			# Restore all baseline fields from snapshot
			if hasattr(self, "_original_settings") and self._original_settings:
				settings = frappe.get_doc("ISM Settings")
				excluded_keys = {
					"modified",
					"creation",
					"doctype",
					"name",
					"_liked_by",
					"_comments",
					"_assign",
				}
				for k, v in self._original_settings.items():
					if not k.startswith("_") and k not in excluded_keys:
						setattr(settings, k, v)
				settings.save(ignore_permissions=True)
				frappe.db.commit()
		except Exception:
			frappe.db.rollback()
		finally:
			super().tearDown()

	def test_default_ism_settings_initialized(self):
		settings = frappe.get_doc("ISM Settings")
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
		self.assertGreaterEqual(settings.buffer_padding_minutes, 0)
		self.assertEqual(settings.default_diagnostic_duration, 60)

	def test_validate_operating_hours_order(self):
		settings = frappe.get_doc("ISM Settings")
		settings.operating_hours_start = "18:00:00"
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

	def test_validate_break_hours_order(self):
		settings = frappe.get_doc("ISM Settings")
		settings.break_start = "13:00:00"
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

	def test_validate_break_within_operating_hours(self):
		settings = frappe.get_doc("ISM Settings")
		settings.break_start = "07:00:00"
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

	def test_validate_estimation_percentile_bounds(self):
		# Test lower bound (< 50%)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_percentile = 40.0
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

		# Test upper bound (> 99%)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_percentile = 100.0
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

		# Auto-conversion from decimal fraction (0.85 -> 85.0)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_percentile = 0.85
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("ISM Settings")
		self.assertEqual(reloaded.estimation_percentile, 85.0)

	def test_validate_estimation_sigma_bounds(self):
		# Test lower bound (< 0.05)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_sigma = 0.02
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

		# Test upper bound (> 1.00)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_sigma = 1.5
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

	def test_setup_idempotency_preserves_customizations(self):
		settings = frappe.get_doc("ISM Settings")
		settings.default_slot_interval = 45
		settings.enable_technician_capacity = 0
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		# Re-run setup_default_ism_settings
		setup_default_ism_settings()
		frappe.db.commit()

		reloaded = frappe.get_doc("ISM Settings")
		self.assertEqual(reloaded.default_slot_interval, 45)
		self.assertEqual(reloaded.enable_technician_capacity, 0)
```

---

### 4.2 Strict Worker Verification Reporting Guidelines

To eliminate integrity violations and guarantee transparent peer review, the remediation worker MUST adhere strictly to the following reporting protocol in all future handoff reports:

#### Guideline 1: Zero Tolerance for Fabricated or Synthetic Output
- **Never synthesize, reformat, or paraphrase command output**: Do not construct synthetic summary tables, fake status lines, or simulated runner strings (e.g., `TestPartsManagement: 30 passed`, `OK (skipped=254)`).
- **Mandatory Raw Terminal Output**: Copy and paste the exact, unadulterated stdout and stderr generated by the command execution tool into code fences.

#### Guideline 2: Standard Command Execution Block Format
Every test command documented in the worker report must follow this exact four-attribute template:

````markdown
### Command: [Descriptive Title]
- **Execution Command**: `<exact shell command>`
- **Working Directory**: `<absolute directory>`
- **Timestamp**: `YYYY-MM-DDTHH:MM:SSZ`
- **Exit Code**: `<integer exit code>`
- **Verbatim Terminal Output**:
```text
<paste full raw terminal stdout and stderr verbatim>
```
````

#### Guideline 3: Explicit Separation of Milestone 1 vs Full Suite Scope
1. **Primary Acceptance (Milestone 1 Suites)**:
   The worker must individually execute and document raw outputs for the four M1 DocType test suites:
   - `bench --site development.localhost run-tests --app ism --doctype 'Service Bay'`
   - `bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'`
   - `bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'`
   - `bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'`
   Each of these four commands must show `OK` and `Exit code: 0`.

2. **Full Repository Test Suite Context (`bench run-tests --app ism`)**:
   - If the worker runs `bench run-tests --app ism`, the worker must provide the genuine exit code (Exit code 1) and raw log tail.
   - The worker must include this explicit factual statement:
     > *"Full test suite execution (`bench run-tests --app ism`) terminates with exit code 1 due to assertion failures in future milestone test suites (`ism/tests/test_estimation_math.py` for M2, `test_scheduling_capacity.py` for M3, and `test_ro_scheduling_e2e.py` for M4/M5). All Milestone 1 DocType unit tests pass cleanly with exit code 0 as shown in Section X."*
   - Never claim the full suite passed or exited with code 0 until all planned milestones are implemented.

3. **Mandatory Synchronization Verification**:
   Before running tests, the worker must run and document:
   `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
   confirming that local Windows edits are mirrored into the WSL container environment.

---

## 5. Verification Method

To independently verify the test isolation fixes and absence of MariaDB error 1020:

1. **Apply the Blueprint**:
   The worker applies the patch at `d:\ism\.agents\teamwork\m1_rem_explorer_2\test_ism_settings.patch` to `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`.

2. **Sync to WSL**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
   ```

3. **Execute Repeated / Sequential Test Runs**:
   Execute the `ISM Settings` suite in direct sequence 5 times to confirm zero deadlock / OperationalError 1020:
   ```bash
   wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
   ```

4. **Execute Full M1 DocType Suite**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Service Bay' && docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type' && docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry' && docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'"
   ```
   Confirm all four suites pass with `OK` and exit code 0.

5. **Invalidation Condition**:
   If any execution of `TestISMSettings` generates `MySQLdb.OperationalError: (1020, ...)` or leaves modified settings un-restored across runs, this verification fails.
