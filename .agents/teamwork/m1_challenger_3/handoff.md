# Empirical Challenge Report: Milestone 1 Data Models & Collision Logic

**Challenger**: M1 Challenger 3 (`m1_challenger_3` / EMPIRICAL CHALLENGER)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_challenger_3`  
**Target Repository**: `d:\ism`  
**Target Bench Site**: `development.localhost`  
**Target Docker Container**: `devcontainer-frappe-1`  
**Empirical Test Harness**: `d:\ism\scripts\empirical_stress_data_models.py`  
**Final Verdict**: **APPROVE**

---

## 1. Observation

### 1.1 Investigated Source Code

1. **`ism/ism/doctype/schedule_entry/schedule_entry.py`**:
   - Lines 52–59:
     ```python
     def before_insert(self):
         self.populate_from_repair_order()
         self.set_title()

     def before_save(self):
         self.populate_from_repair_order()
         self.set_title()
     ```
   - Lines 67–73 (`validate_estimated_duration`):
     ```python
     def validate_estimated_duration(self):
         """Enforce strictly positive appointment duration."""
         if cint(self.estimated_duration) <= 0:
             frappe.throw(
                 _("Estimated Duration must be greater than 0 minutes."),
                 frappe.ValidationError,
             )
     ```
   - Lines 75–102 (`populate_from_repair_order`): Auto-populates `self.customer`, `self.repair_vehicle`, and formats `self.items_summary` as `• {service_title} ({labor_hours} hrs)` when linked to a valid `Repair Order`.
   - Lines 148–180 (`_check_bay_availability_fallback`):
     ```python
     prop_start = to_timedelta(self.scheduled_time)
     prop_duration = timedelta(minutes=self.estimated_duration or 60)
     prop_end = prop_start + prop_duration
     ...
     if max(prop_start, ex_start) < min(prop_end, ex_end):
         return False
     ```
     Correctly excludes `self.name` and entries with `status == "Cancelled"`.

2. **`ism/ism/doctype/service_bay/service_bay.py`**:
   - Lines 31–49 (`on_trash`):
     ```python
     def on_trash(self):
         if frappe.db.exists("DocType", "Schedule Entry"):
             active_entries = frappe.get_all(
                 "Schedule Entry",
                 filters={
                     "service_bay": self.name,
                     "status": ["in", ["Draft", "Scheduled", "In Progress"]],
                 },
                 pluck="name",
                 limit=5,
             )
             if active_entries:
                 frappe.throw(
                     _(
                         "Cannot delete Service Bay '{0}' because it has active schedule entries ({1}). "
                         "Reassign or cancel these entries before deleting."
                     ).format(self.name, ", ".join(active_entries)),
                     title=_("Bay in Use"),
                 )
     ```
     Excludes `Completed` and `Cancelled` appointments.

3. **`ism/ism/doctype/schedule_entry_type/schedule_entry_type.py`**:
   - Lines 24–47 (`on_trash`):
     ```python
     def on_trash(self):
         target_name = (self.type_name or self.name or "").strip()
         if target_name in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
             frappe.throw(
                 _("Cannot delete standard Schedule Entry Type '{0}'.").format(self.name or target_name),
                 title=_("Standard Type Protected"),
             )

         if frappe.db.exists("DocType", "Schedule Entry"):
             linked_entries = frappe.get_all(
                 "Schedule Entry",
                 filters={"entry_type": self.name},
                 pluck="name",
                 limit=5,
             )
             if linked_entries:
                 frappe.throw(
                     _(
                         "Cannot delete Schedule Entry Type '{0}' because it is referenced by existing Schedule Entry records ({1}). "
                         "Reassign or delete these entries before deleting the type."
                     ).format(self.name, ", ".join(linked_entries)),
                     title=_("Type in Use"),
                 )
     ```

---

### 1.2 Verbatim Empirical Stress Test Results

The empirical test harness `scripts/empirical_stress_data_models.py` (38 test cases) was executed directly against MariaDB in `devcontainer-frappe-1`.

#### Execution Command
```bash
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python /workspace/development/frappe-bench/apps/ism/scripts/empirical_stress_data_models.py
```

#### Verbatim Terminal Output
```text
======================================================================
STARTING M1 CHALLENGER 3 EMPIRICAL STRESS TEST EXECUTION
======================================================================
[*] Running: 1.1 Schedule Entry duration zero rejected ... PASS
[*] Running: 1.2 Schedule Entry negative duration (-1) rejected ... PASS
[*] Running: 1.3 Schedule Entry large negative duration (-60) rejected ... PASS
[*] Running: 1.4 Schedule Entry None duration behavior on insert (default applied) and update (rejected) ... PASS
[*] Running: 1.5 Schedule Entry empty string duration rejected ... PASS
[*] Running: 1.6 Schedule Entry string zero ('0') rejected ... PASS
[*] Running: 1.7 Schedule Entry string negative ('-15') rejected ... PASS
[*] Running: 1.8 Schedule Entry non-numeric string rejected ... PASS
[*] Running: 1.9 Schedule Entry strictly positive durations (1 and '45') permitted ... PASS
[*] Running: 1.10 Schedule Entry update to 0 or negative duration blocked on save ... PASS
[*] Running: 2.0 Setup bays and base appointment for collision tests ... PASS
[*] Running: 2.1 Collision exact abutting BEFORE (08:00 - 09:00) allowed ... PASS
[*] Running: 2.2 Collision exact abutting AFTER (10:00 - 11:00) allowed ... PASS
[*] Running: 2.3 Collision 1-minute overlap BEFORE (08:30 - 09:01) blocked ... PASS
[*] Running: 2.4 Collision 1-minute overlap AFTER (09:59 - 10:30) blocked ... PASS
[*] Running: 2.5 Collision strictly nested inside (09:15 - 09:45) blocked ... PASS
[*] Running: 2.6 Collision enclosing superset (08:45 - 10:15) blocked ... PASS
[*] Running: 2.7 Collision exact identical interval (09:00 - 10:00) blocked ... PASS
[*] Running: 2.8 Parallel booking on different bay at identical time allowed ... PASS
[*] Running: 2.9 Booking on same bay at identical time on different date allowed ... PASS
[*] Running: 2.10 Self-update does not trigger self-collision ... PASS
[*] Running: 2.11 Cancelled entry allows rebooking on same bay and time ... PASS
[*] Running: 3.0 Setup Customer, Tesla Vehicle, and Repair Order with services ... PASS
[*] Running: 3.1 populate_from_repair_order on creation populates customer, vehicle, items summary ... PASS
[*] Running: 3.2 populate_from_repair_order on update (before_save) populates unlinked entry ... PASS
[*] Running: 3.3 populate_from_repair_order preserves pre-existing explicit overrides ... PASS
[*] Running: 4.1 Booking 4 distinct appointments on a single Repair Order succeeds ... PASS
[*] Running: 4.2 Independent mutation across multiple appointments on same Repair Order ... PASS
[*] Running: 4.3 Independent cancellation across multiple appointments on same Repair Order ... PASS
[*] Running: 5.1 Service Bay deletion blocked by active 'Draft' schedule entry ... PASS
[*] Running: 5.2 Service Bay deletion blocked by active 'Scheduled' schedule entry ... PASS
[*] Running: 5.3 Service Bay deletion blocked by active 'In Progress' schedule entry ... PASS
[*] Running: 5.4 Service Bay deletion allowed when entries are 'Completed' ... PASS
[*] Running: 5.5 Service Bay deletion allowed when entries are 'Cancelled' ... PASS
[*] Running: 5.6 Service Bay mixed status resolution (Completed + Cancelled + In Progress) ... PASS
[*] Running: 6.1 Standard Schedule Entry Types are strictly protected from deletion ... PASS
[*] Running: 6.2 Custom Schedule Entry Type deletion blocked while linked to Schedule Entry ... PASS
[*] Running: 6.3 Custom Schedule Entry Type deletion allowed after referencing entry deleted ... PASS

======================================================================
TOTAL TESTS:  38
PASSED:       38
FAILED:       0
VERDICT: APPROVE
======================================================================
```

#### Repeatability & Idempotency Stress Test (3x Consecutive Executions)
```text
=== REPETITION RUN 1 ===
Run 1 PASSED cleanly.
=== REPETITION RUN 2 ===
Run 2 PASSED cleanly.
=== REPETITION RUN 3 ===
Run 3 PASSED cleanly.
All 3 consecutive runs passed with 0 failures!
```

---

### 1.3 Standard Unit Test Suites Execution

All 4 DocType unit test suites in Milestone 1 were re-run independently via `bench run-tests`:

1. **`Service Bay`**:
   - Command: `wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Service Bay"`
   - Output: `Ran 5 tests in 2.073s — OK` (100% pass)
2. **`Schedule Entry Type`**:
   - Command: `wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry Type"`
   - Output: `Ran 4 tests in 1.838s — OK` (100% pass)
3. **`Schedule Entry`**:
   - Command: `wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"`
   - Output: `Ran 7 tests in 2.217s — OK` (100% pass)
4. **`ISM Settings`**:
   - Command: `wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"`
   - Output: `Ran 7 tests in 1.403s — OK` (100% pass)

---

## 2. Logic Chain

1. **Positive Duration Enforcement (§1.1, §1.2 tests 1.1–1.10)**:
   - On document creation, inputs `0`, `-1`, `-60`, `""`, `"0"`, `"-15"`, and non-numeric string `"invalid_duration"` all produce `cint(self.estimated_duration) <= 0` and are strictly rejected with `frappe.ValidationError: Estimated Duration must be greater than 0 minutes.`.
   - On document creation, passing `estimated_duration = None` triggers Frappe's field default mechanism (`"default": "60"` in `schedule_entry.json`), initializing the duration to 60.
   - On document update (`save()`), setting `estimated_duration` to `0`, `-10`, or `None` triggers `validate_estimated_duration()` and is blocked with `ValidationError`.
   - Positive integer values (boundary `1`, string `"45"`, standard `60`) insert and persist cleanly.

2. **Collision Interval Math & Fallback (§1.1, §1.2 tests 2.1–2.11)**:
   - Base appointment window: `[09:00:00, 10:00:00)`.
   - Abutting intervals:
     - Preceding appointment `[08:00:00, 09:00:00)`: `max(08:00, 09:00) < min(09:00, 10:00)` evaluates to `09:00 < 09:00` (False) → **Permitted**.
     - Succeeding appointment `[10:00:00, 11:00:00)`: `max(10:00, 09:00) < min(11:00, 10:00)` evaluates to `10:00 < 10:00` (False) → **Permitted**.
   - Overlapping intervals:
     - 1-minute overlap preceding `[08:30:00, 09:01:00)`: `09:00 < 09:01` (True) → **Blocked**.
     - 1-minute overlap succeeding `[09:59:00, 10:30:00)`: `09:59 < 10:00` (True) → **Blocked**.
     - Nested interval `[09:15:00, 09:45:00)` → **Blocked**.
     - Enclosing interval `[08:45:00, 10:15:00)` → **Blocked**.
     - Identical interval `[09:00:00, 10:00:00)` → **Blocked**.
   - Spatial and temporal independence:
     - Parallel booking on Bay B at identical time `[09:00:00, 10:00:00)` → **Permitted**.
     - Booking on Bay A at identical time on a different date → **Permitted**.
   - Self-mutation & cancellation:
     - Modifying an existing appointment does not collide with itself (`filters.append(["name", "!=", self.name])`) → **Permitted**.
     - Cancelling the base appointment removes it from active collision filtering, allowing immediate rebooking on the same bay and time window → **Permitted**.

3. **Auto-Population from Repair Order (§1.1, §1.2 tests 3.1–3.3)**:
   - On document creation (`before_insert`), assigning `repair_order` auto-populates `customer`, `repair_vehicle` (from `ro.vehicle`), and generates formatted bulleted line items from `ro.services`: `• {service_title} ({labor_hours} hrs)`.
   - On document update (`before_save`), linking an existing unlinked appointment (e.g. walk-in `Diagnostic`) to a `Repair Order` automatically synchronizes `customer`, `repair_vehicle`, and `items_summary`, updating the human-readable desk title accordingly.
   - Pre-existing explicit values (e.g. custom party override or bespoke items summary) are protected and not clobbered (`if not self.customer` and `if not self.items_summary`).

4. **1:N Cardinality Invariant (§1.1, §1.2 tests 4.1–4.3)**:
   - In accordance with `ORIGINAL_REQUEST.md` (R1 & Core Domain Rules), a single active `Repair Order` successfully supported 4 distinct appointments across 2 days (Diagnostic, Mechanical Teardown, Supplemental Assembly, Road Test).
   - All 4 appointments retained distinct naming series keys and remained queryable via `frappe.get_all("Schedule Entry", filters={"repair_order": ro.name})`.
   - Independent mutation: altering duration (180 → 240 mins) and status (`In Progress`) on one appointment left all other appointments completely untouched in their original state.
   - Independent cancellation: cancelling one appointment left the remaining 3 appointments active and intact.

5. **ServiceBay Deletion Guards (§1.1, §1.2 tests 5.1–5.6)**:
   - `ServiceBay.on_trash()` accurately checks for active entries in `["Draft", "Scheduled", "In Progress"]`.
   - Deletion attempts while active appointments exist are rejected with `frappe.ValidationError` and title `Bay in Use`.
   - Inactive appointments with status `Completed` or `Cancelled` do NOT trigger `on_trash()`.
   - Mixed status resolution: a bay with 1 Completed, 1 Cancelled, and 1 In Progress appointment blocks deletion until the In Progress appointment is transitioned to Completed, at which point deletion succeeds immediately.

6. **ScheduleEntryType Protection (§1.1, §1.2 tests 6.1–6.3)**:
   - Standard seeded types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) are strictly protected from deletion via `on_trash()` throwing `ValidationError: Cannot delete standard Schedule Entry Type`.
   - Custom entry types referenced by active or historical `Schedule Entry` records are blocked with `ValidationError: Cannot delete Schedule Entry Type ... referenced by existing Schedule Entry records`.
   - Unreferenced custom entry types delete cleanly once referencing entries are removed.

---

## 3. Caveats

1. **Scheduling API Layer Integration (Milestone 3 Scope)**:
   - In Milestone 1, `ism.api.scheduling` and `ism.api.technician_availability` are not yet authored. `ScheduleEntry.validate_bay_availability()` cleanly caught `ImportError` and delegated to `_check_bay_availability_fallback()`. Testing of the full `check_bay_availability()` service (including lunch break extension and HRMS holiday checks) is deferred to Milestone 3.
2. **Standard Link Foreign Key Enforcement**:
   - Deletion of `ServiceBay` and `ScheduleEntryType` records was tested using `force=True` to explicitly isolate and stress-test the custom business logic in controller `on_trash()` hooks without conflation with Frappe's generic relational foreign key check (`check_if_doc_is_linked`).

---

## 4. Conclusion

All 6 focus areas mandated in the dispatch instructions were empirically stress-tested directly against live MariaDB in `devcontainer-frappe-1`:
1. **Positive Duration**: Non-positive numbers, negative values, invalid strings, and update mutations to zero/negative are strictly rejected.
2. **Collision Intervals**: Abutting intervals pass; 1-minute overlaps, nested intervals, and enclosing intervals are blocked; parallel bays and different dates pass; self-updates and rebooking over cancelled entries pass.
3. **Auto-Population**: Insert and `before_save` update cleanly populate customer, vehicle, and multi-service summaries while preserving explicit overrides.
4. **1:N Cardinality**: Multiple appointments per Repair Order succeed with full mutation and cancellation isolation.
5. **ServiceBay Deletion Guard**: Active entries (`Draft`, `Scheduled`, `In Progress`) block deletion; `Completed` and `Cancelled` entries permit deletion.
6. **ScheduleEntryType Deletion Guard**: Standard types are protected; custom types linked to schedule entries are protected; unreferenced custom types delete cleanly.

All 38 empirical tests passed across 3 consecutive executions (114 test passes, 0 failures). All 4 standard DocType test suites passed 100% (23/23 tests pass).

**Final Verdict: APPROVE**

---

## 5. Verification Method

To independently reproduce and verify this empirical challenge in the live environment:

1. **Execute Empirical Stress Test Suite**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python /workspace/development/frappe-bench/apps/ism/scripts/empirical_stress_data_models.py
   ```
   *Expected Outcome*: Prints `TOTAL TESTS: 38`, `PASSED: 38`, `FAILED: 0`, `VERDICT: APPROVE`, exits with code `0`.

2. **Execute 3x Consecutive Repetition**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "import subprocess, sys; [sys.exit(1) if subprocess.run(['/workspace/development/frappe-bench/env/bin/python', '/workspace/development/frappe-bench/apps/ism/scripts/empirical_stress_data_models.py']).returncode != 0 else None for _ in range(3)]; print('ALL 3 RUNS PASSED')"
   ```
   *Expected Outcome*: Completes all 3 runs with 0 errors and exits with code `0`.

3. **Run Standard Milestone 1 Unit Test Suites**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Service Bay"
   wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry Type"
   wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"
   wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"
   ```
   *Expected Outcome*: All 4 test suites pass with `OK` (23 tests total).
