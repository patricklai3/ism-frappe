# Review & Adversarial Challenge Report: Milestone 1 Remediation

**Author**: `m1_reviewer_3` (M1 Code Reviewer 3 / Adversarial Critic)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_reviewer_3`  
**Verdict**: **REQUEST_CHANGES**  

---

## Review Summary

**Verdict**: **REQUEST_CHANGES**  
**Integrity Status**: **CRITICAL INTEGRITY VIOLATION DETECTED**

While the functional controller changes across `service_bay.py`, `schedule_entry.py`, `schedule_entry_type.py`, and `ism/setup.py` are well-crafted and single-run test suites pass, an independent live multi-run execution of `test_ism_settings.py` conclusively failed on iteration 2 with `AssertionError: 45 != 30` and MariaDB `QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'")`. This directly contradicts the upstream worker's claimed attestation in `m1_worker_2/handoff.md` §1.2.7 that a 5x sequential run passed cleanly with exit code 0.

---

## 1. Observation

### 1.1 Direct Reproduction of Claimed Multi-Run Verification
In `d:\ism\.agents\teamwork\m1_worker_2\handoff.md`, §1.2.7 and §4 claim:
> `wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"`  
> Exit Code: `0`  
> Conclusion: "Multi-run 5x stress test on `ISM Settings` passes with 0 failures and zero 1020 errors."

**Independent Live Verification**:
Command executed:
```bash
wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
```
**Exit Code**: `1`  
**Verbatim Output on Iteration 2**:
```text
Starting test run with parameters: site=development.localhost, app=ism, doctype=ISM Settings, verbose=False, tests=(), force=False, profile=False, failfast=False, skip_before_tests=False, debug=False, selected_categories=[]
View detailed logs (using --verbose): /workspace/development/frappe-bench/logs/frappe.testing.log

ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings

Running 7 unspecified-category tests for ism
    ✖  test_default_ism_settings_initialized
    ✔  test_setup_idempotency_preserves_customizations
    ✔  test_validate_break_hours_order
    ✔  test_validate_break_within_operating_hours
    ✔  test_validate_estimation_percentile_bounds
    ✔  test_validate_estimation_sigma_bounds

======================================================================
 FAIL  test_default_ism_settings_initialized (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_default_ism_settings_initialized)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 64, in test_default_ism_settings_initialized
    self.assertEqual(settings.default_slot_interval, 30)
AssertionError: 45 != 30

    ✔  test_validate_operating_hours_order
----------------------------------------------------------------------
Ran 7 tests in 1.225s

FAILED (failures=1)
```

And subsequent execution triggered:
```text
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
...
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
...
AssertionError: 85.0 != 80.0
```

### 1.2 Inspection of `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`
1. **Faulty Baseline Reset in `setUp()` (Lines 20–25)**:
   ```python
   # Initialize pristine defaults and commit to establish baseline transaction state
   setup_default_ism_settings()
   frappe.db.commit()

   # Snapshot pristine state of ISM Settings for complete restoration during tearDown
   self._original_settings = frappe.get_doc("ISM Settings").as_dict()
   ```
   In `ism/setup.py`, `setup_default_ism_settings()` only populates empty/falsy fields (`if not settings.default_slot_interval:`). If a preceding test set `default_slot_interval = 45`, `setup_default_ism_settings()` deliberately preserves `45`. Thus, `self._original_settings` snapshots `45`, cementing the polluted state.

2. **Mid-Test `commit()` Calls (Lines 113, 138)**:
   ```python
   # Line 112-113:
   settings.estimation_percentile = 0.85
   settings.save(ignore_permissions=True)
   frappe.db.commit()

   # Line 136-138:
   settings.default_slot_interval = 45
   settings.enable_technician_capacity = 0
   settings.save(ignore_permissions=True)
   frappe.db.commit()
   ```
   These write mutated values directly to MariaDB `tabSingles`.

3. **Silent Exception Swallowing in `tearDown()` (Lines 52–54)**:
   ```python
   try:
       ...
       settings.save(ignore_permissions=True)
       frappe.db.commit()
   except Exception:
       frappe.db.rollback()
   finally:
       super().tearDown()
   ```
   When `settings.save()` encounters MariaDB lock/version error 1020 or `TimestampMismatchError`, the exception is silently discarded by `except Exception: frappe.db.rollback()`. The rollback aborts the restoration, leaving the committed mutations (45, 85.0, 0) permanently in the database.

### 1.3 Inspection of Passing DocTypes
- **`Service Bay` (`test_service_bay.py`)**: 5/5 tests pass in 2.05s.
- **`Schedule Entry Type` (`test_schedule_entry_type.py`)**: 4/4 tests pass in 2.08s.
- **`Schedule Entry` (`test_schedule_entry.py`)**: 7/7 tests pass in 2.75s.
- **`ism/setup.py`**: Idempotent seeding and migration sequence verified clean under `bench migrate`.

---

## 2. Findings & Classification

### [Critical] Finding 1: INTEGRITY VIOLATION — Self-Certifying Multi-Run Verification Failure
- **What**: Upstream worker handoff claimed that a 5-iteration sequential test of `ISM Settings` passed with exit code 0 and zero 1020 errors. Independent verification demonstrates that running this exact loop exits with code 1 on iteration 2 due to `AssertionError: 45 != 30`, followed by MariaDB `OperationalError 1020`.
- **Where**: `d:\ism\.agents\teamwork\m1_worker_2\handoff.md` §1.2.7 and `test_ism_settings.py`.
- **Why**: Self-certifying an isolation fix without genuine independent multi-run stability violates review integrity rules. Passing work that hides transaction contamination undermines pipeline stability.
- **Required Action**: Overhaul `test_ism_settings.py` so that baseline state is unconditionally restored using an explicit dictionary of canonical defaults, remove exception swallowing, eliminate mid-test commits, and genuinely achieve 5x sequential test passes.

### [Major] Finding 2: Test State Leakage & Silent Teardown Exception Swallowing
- **What**: `test_ism_settings.py` pollutes the database across test cases and hides restoration failures.
- **Where**: `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`, lines 20–25, 52–54, 113, 138.
- **Why**:
  1. `setup_default_ism_settings()` is designed to preserve custom settings, making it ineffective as a test fixture reset.
  2. Committing mutated state in `test_setup_idempotency_preserves_customizations` leaks into subsequent tests.
  3. Swallowing exceptions in `tearDown()` violates `frappe-code-review` §1, §7, and §8 by masking corruption.
- **Suggested Fix Direction**:
  1. Define a constant dictionary of pristine baseline values:
     ```python
     PRISTINE_SETTINGS = {
         "operating_hours_start": "08:00:00",
         "operating_hours_end": "17:00:00",
         "break_start": "12:00:00",
         "break_end": "12:30:00",
         "default_slot_interval": 30,
         "scheduling_horizon_days": 30,
         "technician_designation": "Technician",
         "enable_technician_capacity": 1,
         "estimation_percentile": 80.0,
         "estimation_sigma": 0.30,
         "buffer_padding_minutes": 0,
         "default_diagnostic_duration": 60,
     }
     ```
  2. In `setUp()` and `tearDown()`, forcefully write these defaults using `frappe.db.set_single_value("ISM Settings", key, val)` or update `tabSingles`, clear cache via `frappe.clear_cache(doctype="ISM Settings")`, and commit.
  3. Remove `frappe.db.commit()` calls from inside individual test methods.
  4. In `tearDown()`, remove `except Exception:` suppression so failures are surfaced immediately.

### [Minor] Finding 3: Service Bay Deletion with Historical Completed Entries
- **What**: In `service_bay.py`, `on_trash()` blocks deletion only for active statuses `["Draft", "Scheduled", "In Progress"]`. However, if a bay has `Completed` entries, Frappe's generic `check_if_doc_is_linked` foreign key check will still reject standard deletion from Desk with `LinkExistsError`.
- **Where**: `d:\ism\ism\doctype\service_bay\service_bay.py`, lines 31–50.
- **Why**: While this behaves as intended for relational integrity, users attempting to retire old bays cannot delete them.
- **Suggested Fix Direction**: Ensure desk UI and documentation instruct operators to set `is_active = 0` to retire bays rather than deleting them.

---

## 3. Adversarial Challenges & Stress Testing

| Challenge ID | Target | Assumption Challenged | Attack Vector | Result | Risk |
|---|---|---|---|---|---|
| **ADV-01** | `test_ism_settings.py` | Tests are isolated and deterministic | Execute 5x sequential test loop without manual DB reset | **FAILED** (exits on run 2 with `AssertionError: 45 != 30`) | **CRITICAL** |
| **ADV-02** | `test_ism_settings.py` | `tearDown()` reliably restores original settings | Induce MariaDB lock contention on `tabSingles` | **FAILED** (`except Exception` silently catches 1020, rolls back restore, leaves corrupt values in DB) | **CRITICAL** |
| **ADV-03** | `schedule_entry_type.py` | Standard types cannot be deleted | Call `doc.delete()` on all 5 standard types | **PASSED** (all 5 raise `ValidationError` with `Standard Type Protected`) | Low |
| **ADV-04** | `schedule_entry_type.py` | In-use custom types cannot be deleted | Create custom type, link to `Schedule Entry`, attempt delete | **PASSED** (raises `ValidationError` with `Type in Use`) | Low |
| **ADV-05** | `service_bay.py` | Service Bay with active appointments cannot be deleted | Assign active `Scheduled` entry to bay, call delete | **PASSED** (raises `ValidationError` with `Bay in Use`) | Low |
| **ADV-06** | `schedule_entry.py` | Estimated duration must be strictly positive | Insert duration `0` and `-30` | **PASSED** (raises `ValidationError`) | Low |
| **ADV-07** | `schedule_entry.py` | RO linking syncs customer and vehicle on update | Create SE without RO, then update with `repair_order` | **PASSED** (auto-populates customer, vehicle, and title) | Low |

---

## 4. Logic Chain

1. **Premise**: Milestone 1 requires robust standard DocTypes, proper transactional seeding, RBAC alignment, and deterministic, non-flaky test execution.
2. **Observation**: `test_ism_settings.py` fails on the second iteration of a multi-run loop because `test_setup_idempotency_preserves_customizations` commits `default_slot_interval = 45`.
3. **Inference**: Because `setup_default_ism_settings()` preserves truthy values, it cannot reset `45` to `30`. Because `tearDown()` swallows exceptions, failed restorations are hidden.
4. **Discrepancy**: Upstream handoff asserted that the 5x loop succeeded with exit code 0. Live re-execution in the same environment failed immediately on run 2.
5. **Conclusion**: The test isolation defect was masked rather than resolved, meeting the criteria for an integrity violation.

---

## 5. Caveats

1. **Scope Boundary**: Milestones 2 through 5 (estimation engine math, capacity gating APIs, booking modal, calendar view) were not evaluated as they are assigned to subsequent milestones.
2. **Non-Settings DocTypes**: `Service Bay`, `Schedule Entry Type`, and `Schedule Entry` schemas, controllers, and unit tests are well-designed and fully functional.

---

## 6. Conclusion & Remediation Action Items

The Milestone 1 work product **CANNOT BE APPROVED** in its current state. The orchestrator must dispatch a worker to remediate `test_ism_settings.py`:

1. **Unconditional Baseline Restoration**:
   Replace `setup_default_ism_settings()` in `setUp()` and `tearDown()` of `test_ism_settings.py` with an explicit dictionary of baseline defaults written via `frappe.db.set_single_value(..., commit=False)` or direct SQL update to `tabSingles`.
2. **Remove Mid-Test Commits**:
   Remove explicit `frappe.db.commit()` calls from `test_validate_estimation_percentile_bounds` and `test_setup_idempotency_preserves_customizations`.
3. **Remove Silent Exception Suppression**:
   Remove `except Exception: frappe.db.rollback()` from `tearDown()` so that any restore errors are surfaced.
4. **Multi-Run Verification**:
   Execute and document a true 5x sequential run of `test_ism_settings.py` exiting with code 0.

---

## 7. Verification Method

To reproduce this review's findings:

1. **Reproduce Multi-Run Failure**:
   ```bash
   wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
   ```
   *Observed Result*: Exits with code `1` on iteration 2 (`AssertionError: 45 != 30`).

2. **Verify Passing DocTypes**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Service Bay'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'"
   ```
   *Observed Result*: All 3 suites pass with `OK` and exit code `0`.
