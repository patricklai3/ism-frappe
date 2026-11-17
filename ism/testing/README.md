# ISM Testing & Simulation Framework (`ism.testing`)

ISM uses a layered, deterministic testing and simulation stack backed by a clean-reinstalled Golden database (`main_golden.sql.gz`).

## Architecture

1. **Layer 1 — Canonical Master Catalog (`ism/testing/catalog.py`)**:
   - Seeds realistic EV service shop masters with zero `_TEST_ISM_` or `Test ` prefixes.
   - Entry points: `seed_all(...) -> dict`, `seed_catalog_manifest(...) -> SimManifest`, `get_master_manifest(...) -> SimManifest`.
   - Synthetic helpers: `generate_synthetic_vin()`, `generate_oem_part_number()`, `compute_catalog_part_price()`.
2. **Layer 2 — Lifecycle Transaction Engine (`ism/testing/builder.py`, `manifest.py`)**:
   - `SimContext(seed, anchor_date, manifest, track_fn)` provides a seeded `random.Random`, relative anchor-clock helpers (`today`, `tomorrow`, `yesterday`, `day()`, `rel_date()`, `dt()`, `next_open_day()`), and `track()` integration.
   - `SimBuilder` walks real ISM domain APIs (`RepairOrder`, `authorize_jobs`, `submit_procurement_orders`, `create_purchase_invoice`, `ScheduleEntry`, `pick_up_vehicle`, `add_supplement`, `create_shop_action_item`, `toggle_service_completion`, `park_vehicle`, `record_deposit`, `process_payment_and_release`) up to any target `VALID_RO_STAGES` state.
   - `seed_historical_telemetry(ctx, n_closed_ros=30)` seeds closed historical ROs across `T-60d..T-1d` with log-normal `actual_duration_hours` so `estimation_service.py` (`empirical=True`) computes empirical percentiles.
   - `SimManifest` maps logical handles to exact DB names and supports JSON round-trips, `resolve()`, and `interpolate()`.
3. **Layer 3 — World Profiles & `monte_carlo(seed)` (`profiles.py`, `monte_carlo.py`, Stage 2)**:
   - Composes `SimContext` + `SimBuilder` into repeatable evaluation profiles and seeded edge-discovery worlds.

---

## Layer 1 Master Topology & `SimManifest` Handles

- **Company & Ledger**: `company="Lightspeed EV"`, `company_abbr="LEV"`, `currency="CAD"`, `warehouse="Main Shop Stores - LEV"`, `tax_templates`: `sales_hst_13`, `purchase_hst_13` (13% HST), `price_lists`: `selling` (`Standard Selling`), `buying` (`Standard Buying`).
- **Modes of Payment** (`modes_of_payment`): `debit_interac`, `credit_card`, `e_transfer`, `cash`, `bank_draft`, `wire_transfer` — all enabled and GL-mapped to `Operating Bank - LEV` or `Cash - LEV`.
- **Canonical `ISM Settings`**: `08:00:00`–`17:00:00`, break `12:00:00`–`12:30:00`, `default_slot_interval=30`, `estimation_percentile=80.0`, `estimation_sigma=0.30`, `default_labor_rate=140.0`, `shop_supplies_percentage=0.0`, `shop_supplies_cap=0.0`, `cost_tolerance_percentage=10.0`, `cost_tolerance_flat_amount=20.0`.
- **Workforce (`users`, `techs`, `leave_types`)**:
  - `users`: `manager` (`manager@lightspeedev.ca`), `advisor` (`advisor@lightspeedev.ca`), `parts` (`parts@lightspeedev.ca`), `tech` (`tech@lightspeedev.ca`, locked at `ISM Technician` + `Maintenance User` without `permlevel=1` write access).
  - `techs`: `marcus` (*Marcus Vance*), `elena` (*Elena Rostova*), `devon` (*Devon Brooks*), `noah` (*Noah Patel*), `inactive_tech` (*Victor Halvorsen*, `Left`).
  - `holiday_list`: `"Lightspeed EV Holidays 2026"` (+ 2027 coverage & `Holiday List Assignment`); `leave_types`: `privilege`, `sick`, `casual` with submitted `Leave Allocation` rows for 2026–2027.
- **Shop Floor (`equipment_tags`, `bays`, `schedule_entry_types`)**:
  - `equipment_tags`: `lift`, `alignment_rack`, `ev_hv`, `hv_battery_station`, `adas_target`, `tire_balancer`.
  - `bays`: `main_lift` (`Bay 1 - Main Lift`), `alignment` (`Bay 2 - Alignment & Suspension`), `hv_diagnostics` (`Bay 3 - HV & Diagnostics`), `tire_quick` (`Bay 4 - Tire & Quick Service`), `offline` (`Bay 5 - Offline`, `is_active=0`).
  - `schedule_entry_types`: `diagnostic`, `repair`, `maintenance`, `meeting`, `internal_service`.
- **Inventory & Suppliers (`warehouses`, `suppliers`, `items`)**:
  - `warehouses`: `main_stores`, `special_order`, `core_return`.
  - `suppliers`: `tesla_oem`, `euro_aftermarket`, `metro_tire`, `shop_consumables`.
  - Stock bands in `Main Shop Stores - LEV` (opening `Stock Reconciliation` at `2025-01-02`):
    - **Abundant (`>= 16`)**: `dot4_brake_fluid`, `cabin_hepa_filter_oem`, `cabin_hepa_filter_aft`, `wiper_blade_set_oem`, `coolant_g48`, `lug_nut_kit`, `front_pad_set_oem`, `front_pad_set_aft`, `tpms_ble_sensor`, `ev_tire_235_45r18`, `susp_bolt_kit`.
    - **Tight (`== 2`)**: `front_rotor_oem`, `front_rotor_aft`, `fore_link_lh_oem`, `fore_link_rh_oem`, `fore_link_lh_aft`, `fore_link_rh_aft`, `ptc_cabin_heater_recon`.
    - **Zero (`== 0`)**: `hv_pyro_fuse_oem`, `rear_subframe_arm_lh`, `rear_subframe_arm_rh`, `adas_windshield_bracket`.
- **Packages & Inspections (`packages`, `inspection_templates`)**:
  - `packages`: `symmetrical_fore_link` (`is_symmetrical=1`, LH/RH OEM+Aftermarket + bolts), `front_brake_overhaul` (3-slot overhaul with tight-stock rotors), `four_wheel_alignment` (`is_flat_rate=1`), `cabin_filter_service` (`is_mobile_capable=1`), `wiper_blade_service` (`is_flat_rate=1`, `is_mobile_capable=1`), `tire_rotation_balance` (`is_flat_rate=1`), `brake_fluid_flush`, `hv_isolation_diagnostic`, `hv_pyro_fuse_replace` (zero-stock), `rear_subframe_camber_sym` (`is_symmetrical=1`, zero-stock).
  - `inspection_templates`: `walkaround`, `undercarriage_brake`.
- **Customers & Vehicles (`customers`, `vehicles`)**:
  - `customers`: `retail_1`, `retail_2`, `retail_3`, `household_primary`, `household_secondary`, `fleet_1`.
  - `vehicles`: `veh_model3_liam`, `veh_modely_sophia`, `veh_models_owen`, `veh_modelx_household` & `veh_model3_household` (multi-owner `Repair Vehicle Customer`), `veh_fleet_1`..`veh_fleet_5` (5 commercial fleet EVs).

---

## `ISMTestCase` Isolation Contract (`ism/tests/base.py`)

- Every test inheriting `ISMTestCase` starts cleanly via `frappe.db.rollback()` in `setUp()` and cleans up via rollback + LIFO/FK-ordered `cleanup_tracked_docs()` in `tearDown()`.
- Use `self.get_manifest()`, `self.sim_context()`, and `self.sim_builder()` to reuse Layer 1 masters and auto-track every created document (`Repair Order`, `Schedule Entry`, `Purchase Order`, `Purchase Invoice`, `Sales Invoice`, `Payment Entry`, `Shop Action Item`, etc.).
- Ephemeral masters created directly by a test must use `TEST_PREFIX` (`_TEST_ISM_`) and call `self.track(doc)` when committed mid-test.
- `ism/tests/cleanup.py` is an optional manual admin CLI sweep (`bench --site <site> execute ism.tests.cleanup.cleanup_all_test_data`) and is never invoked by tests.

---

## `SimBuilder` & `SimManifest` Quick Example

```python
ctx = self.sim_context(seed=42, anchor_date="2026-11-18")
ro = (
    ctx.ro("brake_job")
    .for_customer("retail_2", "veh_modely_sophia", odometer_in=34150)
    .with_package("front_brake_overhaul")
    .with_deposit(100.0, payment_method="Debit / Interac")
    .with_schedule(date="tomorrow", start_time="09:00:00", bay="main_lift", technician="marcus")
    .with_checkout(payment_method="Credit Card", odometer_out=34165)
    .build("closed_released")
)

manifest = ctx.manifest
assert manifest.resolve("ros.brake_job") == ro.name
prompt = manifest.interpolate("Check {{ros.brake_job}} in {{bays.main_lift}}")
```

Supported `build(stage)` targets: `checked_in`, `draft_estimate`, `pending_authorization`, `authorized_in_stock`, `waiting_on_parts`, `partially_approved_with_deferred`, `scheduled_in_progress`, `work_completed`, `billed_paid`, `closed_released`.

---

## Clean-Reinstall Golden Slot Workflow

- **Isolated worktree clean reinstall + test**:
  ```powershell
  powershell -ExecutionPolicy Bypass -File ism-frappe\scripts\test-in-slot.ps1 -CleanReinstall -Module ism.tests.test_simulation_foundation
  ```
- **Rebuild `main_golden.sql.gz` from primary `D:\ism` (`main`)**:
  - `provision-bench-slots.ps1 -RefreshGolden` and `promote-to-main.ps1` build `main_golden.sql.gz` from a clean `golden.localhost` site + `ism.testing.catalog.seed_all` (decoupled from `development.localhost`).
