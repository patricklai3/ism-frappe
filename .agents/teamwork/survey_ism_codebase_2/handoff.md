# Handoff Report: Survey of ISM Codebase (Repair Order, Provisional Data, ISM Settings, Test Setup, Hooks & Actions)

## 1. Observation

### A. Environment, Repository Layout, & WSL2 Bench Setup
- **Host Workspace**: `d:\ism` (Git repository on Windows host). Contains `.codegraph/`, `ism/`, `extension/`, `scripts/`, `pyproject.toml`.
- **WSL2 / Docker Bench**:
  - Distro: `Ubuntu` (WSL2).
  - Frappe Bench Container: `devcontainer-frappe-1` (running image `frappe/bench:latest`, bench version `5.31.0`).
  - Bench Directory in Container: `/workspace/development/frappe-bench`
  - Active Site: `development.localhost` (verified in `/workspace/development/frappe-bench/sites/development.localhost`).
  - Synchronizer script `scripts/sync-to-wsl.ps1`: Syncs `D:\ism` to `/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/` via `rsync -rtvc`.
  - Reverse synchronizer `scripts/sync-from-wsl.ps1`: Pulls changes from WSL2 back to Windows.
  - Live watcher: `scripts/watch-sync.ps1` and `scripts/watch-sync.mjs`.

### B. Existing Test Suite Execution & Baseline
- **Execution Command**:
  ```powershell
  wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
  ```
- **Test Result**:
  ```
  Running 78 unspecified-category tests for ism
  ism.tests.test_parts_management.TestPartsManagement: 30 tests passed
  ism.tests.test_tesla_integration.TestTeslaIntegration: 7 tests passed
  ism.ism.doctype.repair_vehicle.test_repair_vehicle.TestRepairVehicle: 6 tests passed
  ism.ism.doctype.repair_order.test_repair_order.TestRepairOrder: 35 tests passed
  ----------------------------------------------------------------------
  Ran 78 tests in 35.979s
  OK
  ```
- **Test Framework Conventions**:
  - `ism/tests/base.py`: Defines `ISMTestCase(unittest.TestCase)` with `setUp()` and `tearDown()` methods tracking docs in `self._tracked_docs` and deleting them in reverse dependency order (`Repair Order` -> `Sales Invoice` -> `Payment Entry` -> `Customer`).
  - Standard test prefix: `TEST_PREFIX = "_TEST_ISM_"`.

### C. `Repair Order` DocType Schema, Fields, & Tabs
- **Schema File**: `ism/ism/doctype/repair_order/repair_order.json`
  - Current Tab Structure (`field_order`):
    - Line 8: `"tab_intake"` (Label: "Vehicle Check-in & Intake") — Currently the FIRST tab.
    - Line 31: `"tab_estimate"` (Label: "Estimate & Authorization")
    - Line 53: `"tab_workshop"` (Label: "Workshop Execution", `depends_on: "eval:in_list(['Approved', 'Partially Approved'], doc.approval_status) || in_list(['Authorized - In Progress', 'Waiting on Parts', 'Work Completed', 'Billed / Paid', 'Closed / Released', 'Cancelled'], doc.status)"`)
    - Line 72: `"tab_billing"` (Label: "Billing & Release", `depends_on: "eval:in_list(['Work Completed', 'Billed / Paid', 'Closed / Released', 'Cancelled'], doc.status) || doc.vehicle_location == 'Completed / Parked in Lot' || doc.vehicle_released"`)
  - Mandatory Field Constraints:
    - Line 107-115: `customer` (Link to `Customer`, `reqd: 1`)
    - Line 149-157: `vehicle` (Link to `Repair Vehicle`, `reqd: 1`)
    - Line 159-163: `odometer_in` (Int, `reqd: 1`)
  - Status Options (Line 180-188):
    ```
    "options": "Draft Estimate\nPending Authorization\nAuthorized - In Progress\nWaiting on Parts\nWork Completed\nBilled / Paid\nClosed / Released\nCancelled"
    ```
- **Controller File**: `ism/ism/doctype/repair_order/repair_order.py`
  - `before_insert` (Lines 119-132):
    ```python
    if not self.check_in_date:
        self.check_in_date = frappe.utils.now_datetime()
    if not self.approval_status or self.approval_status == "Draft":
        self.approval_status = "Estimating"
    ```
  - `_validate_check_in` (Lines 158-163):
    ```python
    if not self.check_in_date:
        self.check_in_date = self.creation or frappe.utils.now_datetime()
    ```
  - `_validate_vehicle_and_customer` (Lines 312-329):
    ```python
    if not self.vehicle or not self.customer:
        return
    if not frappe.db.exists("Repair Vehicle", self.vehicle):
        frappe.throw(_("Repair Vehicle {0} does not exist.").format(self.vehicle))
    ```
  - `_validate_odometer` (Lines 330-339): Enforces non-negative and odometer out >= odometer in.
- **Client Script & Form Actions**: `ism/ism/doctype/repair_order/repair_order.js`
  - Lines 34-65: `get_status_target_tab(doc_or_frm)` resolves target tab on route load. Defaults to `tab_intake` for local new documents.
  - Lines 430-585: Form buttons grouped by lifecycle phase:
    - `Jobs & Packages`: `Tesla Manual`, `Add Service Package`, `Import Tesla URL`, `Quick Add / Import Part`, `Add Supplement`, `Deferred Services`
    - `Customer Approval`: `Send Approval Link`, `In-Person Signature`, `Log Verbal Approval`, `Quick Authorize (All)`
    - `Procurement`: `Procure Parts`
    - `Bay Execution`: `Pick Up Vehicle (Into Bay)`, `Park Vehicle in Lot`
    - Settlement: `Process Payment & Release` (primary button)
    - Order Lifecycle: `Cancel Order`
- **Tab Layout Test Assertion**: `ism/ism/doctype/repair_order/test_repair_order.py`
  - Lines 1098-1108 (`test_repair_order_super_doctype_tabs_and_status_visibility`):
    ```python
    tab_names = [df.fieldname for df in meta.fields if df.fieldtype == "Tab Break"]
    intake_idx = tab_names.index("tab_intake")
    estimate_idx = tab_names.index("tab_estimate")
    workshop_idx = tab_names.index("tab_workshop")
    billing_idx = tab_names.index("tab_billing")

    self.assertLess(intake_idx, estimate_idx, "Intake tab must precede Estimate tab.")
    self.assertLess(estimate_idx, workshop_idx, "Estimate tab must precede Workshop Execution tab.")
    self.assertLess(workshop_idx, billing_idx, "Workshop Execution tab must precede Billing tab.")
    ```

### D. `ISM Settings` DocType
- **Current File**: `ism/ism/doctype/ism_settings/ism_settings.json` (lines 1-104) and `ism_settings.py` (lines 1-8).
- **Existing Fields**:
  - `sb_approval_settings`: `authorization_token_expiry_hours`, `cost_tolerance_percentage`, `cost_tolerance_flat_amount`, `disclaimer_text`
  - `sb_sms_settings`: `sms_gateway_provider`, `sms_gateway_endpoint`, `sms_gateway_api_key`, `sms_sender_id`
- **Setup Initialization**: `ism/setup.py` -> `setup_default_ism_settings()` (lines 88-103) sets defaults on `after_migrate` / `after_install`.

### E. Reference Repository (`induct_shop`) Architecture
- **Location**: Local clone confirmed at `C:\Users\real2\AppData\Local\Temp\induct_shop` ($env:TEMP\induct_shop).
- **Key Modules**:
  1. `induct_shop/scheduling/estimation_light.py`:
     - Analytical log-normal duration estimation: `estimate_duration(flat_rate_minutes, sigma, percentile)`.
     - Multi-operation Fenton-Wilkinson log-normal summation: `estimate_total_duration(flat_rate_list, sigma, percentile)`.
     - Precomputed analytical z-scores for P50, P75, P80 (0.8416), P85, P90, P95 with Abramowitz & Stegun inverse CDF approximation fallback.
     - Per-category sigma overrides: `Brake` (0.20), `Tire` (0.15), `Suspension` (0.35), `Electrical` (0.50), `Body` (0.40), `HVAC` (0.35), `Drivetrain` (0.40). Default sigma: 0.30.
  2. `induct_shop/api/scheduling.py`:
     - `effective_end_time()`: Automatically extends job end times by lunch break duration when an appointment spans `break_start`.
     - `check_bay_availability()`: Interval overlap detection `max(start1, start2) < min(end1, end2)` with lunch break adjustments.
     - `get_available_bays()`: Filters active bays against required equipment tags.
     - `get_available_slots()`: Generates time slots across operating hours, excludes lunch break window, calculates dual-resource capacity (service bays and technicians), and identifies capacity bottlenecks.
     - `is_holiday()`: Checks ERPNext `Holiday List`.
  3. `induct_shop/api/technician_availability.py`:
     - `get_active_technicians()`: Queries active `Employee` records by designation.
     - `check_employee_leave()`: Evaluates ERPNext `Leave Application` records (including full day and half day).
     - `get_technician_pool_availability()`: Pool-level capacity gating comparing on-duty technicians against concurrent bookings.
     - `check_technician_availability()`: Real-time conflict and leave checks for assigned technicians.
  4. `induct_shop/public/js/sales_order.js` (Lines 91-350):
     - Interactive client-side scheduling modal with date navigation, holiday alerts, slot buttons with dual-resource counts, equipment badges, and technician preview.
  5. `induct_shop/doctype/schedule_entry/schedule_entry_calendar.js`:
     - FullCalendar integration color-coded by status/entry type, with badges for service bay and lunch span (`🍱 Spans Lunch`), plus rich popovers.

---

## 2. Logic Chain

1. **Tab Structure Precedence**:
   - `ORIGINAL_REQUEST.md` R1 specifies: *Add an "Appointment & Scheduling" tab as the initial tab before "Vehicle Check-in & Intake" on `Repair Order`.*
   - Existing test `test_repair_order_super_doctype_tabs_and_status_visibility` checks `intake_idx < estimate_idx < workshop_idx < billing_idx`.
   - By prepending `tab_appointment` (or `tab_scheduling`) at index 0, `tab_intake` moves to index 1, `tab_estimate` to 2, `tab_workshop` to 3, `tab_billing` to 4. All existing `<` ordering assertions continue to hold true. A new assertion can verify `apt_idx < intake_idx`.
2. **Early Appointment Lifecycle & Relaxed Mandatory Fields**:
   - Currently, `Repair Order` strictly requires `customer` (`reqd: 1`), `vehicle` (`reqd: 1`), and `odometer_in` (`reqd: 1`).
   - Early appointment stages (`Draft Appointment`, `Scheduled`) occur before vehicle physical check-in. The customer may only provide provisional contact info (name, phone, email) and vehicle description notes, without having a registered master `Customer` or `Repair Vehicle`, and the odometer reading is unknown.
   - Therefore, `customer`, `vehicle`, and `odometer_in` must not be unconditionally mandatory. They should be configured with `reqd: 0` in schema and `mandatory_depends_on: "eval:!in_list(['Draft Appointment', 'Scheduled'], doc.status)"`, and enforced in Python `validate()` when `status not in ["Draft Appointment", "Scheduled"]`.
   - In `before_insert()` and `_validate_check_in()`, `check_in_date` should only default to `now_datetime()` if `status not in ["Draft Appointment", "Scheduled"]`.
3. **Provisional Data Formalization**:
   - At vehicle intake, staff transitions the Repair Order from early appointment stages to `Draft Estimate` or `Checked In`.
   - A dedicated formalization method `formalize_provisional_data(self)` will:
     - Check if master `Customer` exists matching `provisional_customer_name` or insert a new `Customer` record, populating contact info (phone, email).
     - Check or create `Repair Vehicle` if a valid 17-char Tesla VIN is provided in `provisional_vin` (or allow staff to input VIN at intake).
     - Populate `self.customer` and `self.vehicle`, link them via `Repair Vehicle Customer`, and update any linked `Schedule Entry` records.
     - Populate `self.check_in_date = frappe.utils.now_datetime()`.
     - Retain provisional notes in `customer_concern` without data loss.
4. **One Visit = One RO vs Multiple Appointments**:
   - In `induct_shop`, `Schedule Entry` had `validate_unique_sales_order` (1:1 with Sales Order).
   - In ISM, the domain rule strictly mandates: **One Visit = One Repair Order**, but **Multiple Appointments per Repair Order** representing supplemental work or additional labor time required while the Repair Order is still active.
   - Therefore, `Schedule Entry` must support 1-to-many relationships with `Repair Order`. The unique constraint from induct_shop must NOT be replicated.
5. **Estimation Engine Configuration in `ISM Settings`**:
   - `ISM Settings` is a Single DocType. All tunable estimation parameters specified in R4 (`estimation_percentile`, `estimation_sigma`, `standard_buffer_padding`, `default_diagnostic_duration`) along with shop operating hours, breaks, slot interval, scheduling horizon, and technician capacity toggle should reside directly on `ISM Settings`.
   - Pure Python estimation logic adapted from `estimation_light.py` can directly read from `frappe.get_single("ISM Settings")` (or cached values), allowing immediate shop-wide tuning without code changes.

---

## 3. Caveats

1. **Deadlock on Concurrent Migration**:
   - During our survey, a concurrent test run caused `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabRepair Order Service'")` when running `bench migrate` while tests were touching `tabRepair Order Service`. Running migrations when test execution is quiescent completes normally.
2. **Desktop / Mobile Chrome Extension Context**:
   - The ISM repository also contains a Chrome extension (`extension/`). This extension syncs active RO context via `get_active_repair_orders`. The new early appointment stages (`Draft Appointment`, `Scheduled`) should be taken into account when filtering active ROs if extension synchronization for appointments is desired.
3. **Employee vs User DocType for Technicians**:
   - In `induct_shop`, technicians are linked to ERPNext `Employee`. In `repair_order.json`, `service_advisor`, `picked_up_by`, and `parked_by` are linked to Frappe `User`.
   - For technician capacity and ERPNext `Leave Application` awareness, `assigned_technician` on `Schedule Entry` should link to `Employee` (with designation matching `technician_designation` in `ISM Settings`), while optionally storing the corresponding `User` if available.

---

## 4. Conclusion

The ISM codebase is well-structured and ready for the dual-resource capacity scheduling integration.
- The 78 existing unit and integration tests pass cleanly in the Docker bench environment (`bench --site development.localhost run-tests --app ism`).
- Prepending `"Appointment & Scheduling"` (`tab_appointment`) to `Repair Order` integrates seamlessly with existing tab visibility scripts and will not break tab ordering assertions.
- Relaxing mandatory constraints on `customer`, `vehicle`, and `odometer_in` during `Draft Appointment` and `Scheduled` statuses enables provisional intake without disturbing standard workflows.
- `induct_shop` provides proven, production-grade algorithms for Fenton-Wilkinson duration estimation, lunch-aware effective end times, bay conflict detection, and technician pool gating that can be directly adapted into `ism`.
- All scheduling and estimation settings cleanly fit into `ISM Settings` as standard fields with migration defaults in `ism/setup.py`.

---

## 5. Verification Method

To independently verify all observations and test results documented above:

1. **Run Full Test Suite via WSL2 Docker Bench**:
   ```powershell
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
   ```
   *Expected Result*: Ran 78 tests in ~36s, 0 failures, 0 errors.

2. **Verify Sync Mechanism**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
   ```
   *Expected Result*: Rsync runs successfully and outputs `[sync-to-wsl] Complete.`

3. **Inspect Tab Order in DocType JSON**:
   View `ism/ism/doctype/repair_order/repair_order.json` lines 8-85. Confirm `tab_intake` is currently the initial tab break.

4. **Inspect `induct_shop` Estimation Math**:
   View `$env:TEMP\induct_shop\induct_shop\scheduling\estimation_light.py` and run tests:
   ```powershell
   python -m unittest "C:\Users\real2\AppData\Local\Temp\induct_shop\induct_shop\tests\test_estimation_light.py"
   ```
   *Expected Result*: Ran 5 tests, OK.
