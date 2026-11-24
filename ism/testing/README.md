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
3. **Layer 3 — World Profiles & `monte_carlo(seed)` (`profiles.py`, `monte_carlo.py`)**:
   - **Profiles (`ism/testing/profiles.py`)**: `VALID_PROFILES = ("clean_master", "eval_standard", "swarm_crunch_day")`.
     - `build_profile(profile="eval_standard", *, seed=42, anchor_date=None, commit=False) -> SimManifest`
     - `seed_profile(profile="eval_standard", seed=42, anchor_date=None, commit=True) -> dict[str, Any]` (clean JSON-safe `bench execute` wrapper; zero-arg helpers: `seed_clean_master()`, `seed_eval_standard()`, `seed_swarm_crunch_day()`).
     - `provision_agent_api_keys(user="manager@lightspeedev.ca") -> dict[str, Any]` generates/commits API keys (`generate_keys(user)`) and returns `{"user", "api_key", "api_secret", "company"}`.
     - **`clean_master`**: Layer 1 canonical masters only via `seed_catalog_manifest`.
     - **`eval_standard`**: `metadata["target_date"] = next_open_day(1)`, `metadata["historical_ro_count"] = 30`, 30 closed historical ROs (`ros.hist_1..hist_30`), approved leave for `techs.elena` (`leave_applications.elena_tomorrow`), conflict booking at `09:00:00` 90m in `bays.main_lift` with `techs.marcus` (`ros.conflict_ro`, `schedule_entries.conflict_main_lift`), secondary booking at `10:00:00` 60m in `bays.hv_diagnostics` with `techs.noah` (`ros.hv_scheduled_ro`, `schedule_entries.hv_morning`), two unscheduled `authorized_in_stock` ROs (`ros.unscheduled_ro`, `ros.unscheduled_secondary_ro`), open PO + submitted `Bank Draft` advance PE (`ros.open_po_ro`, `pos.eval_open_po`, `pes.eval_open_po_advance`), and zero-stock RFQ RO (`ros.zero_stock_ro`, `rfqs.eval_open_rfq`).
     - **`swarm_crunch_day`**: ~85.3% bay utilization (435/510 min across `main_lift`, `alignment`, `hv_diagnostics`, `tire_quick` on `target_date`), tight-band FIFO contention (`ros.crunch_fifo_winner` `Authorized - In Progress` vs `ros.crunch_fifo_loser` `Waiting on Parts`), unapproved over-tolerance supplements (`> max(10%, $20)`) with `Shop Action Item` records (`action_items.crunch_over_tol_1..2`), and commercial fleet batch intake (`fleet_1` across `veh_fleet_1..5` via `ros.crunch_fleet_1..5`).
   - **Monte Carlo (`ism/testing/monte_carlo.py`)**:
     - `monte_carlo(seed, *, anchor_date=None, n_arrivals=None, walk_in_prob=0.35, stockout_prob=0.25, supplement_prob=0.30, tolerance_breach_prob=0.40, tech_leave_prob=0.20, bay_outage_prob=0.15, partial_decline_prob=0.20, deposit_prob=0.35, split_tender_prob=0.30, commit=False) -> SimManifest`
     - `seed_monte_carlo(seed=1, anchor_date=None, commit=True, **params) -> dict[str, Any]`
     - Uses deterministic `random.Random(int(seed))`, safely restores mutated `Service Bay.is_active` flags after scheduling, and populates `metadata["monte_carlo_summary"]` (`seed`, `target_date`, `n_arrivals`, `conflict_ros`, `waiting_on_parts_ros`, `unscheduled_ros`, `over_tolerance_ros`, `partially_approved_ros`, `closed_ros`, `walk_in_ros`, `outage_bays`, `techs_on_leave`, `entity_counts`).

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
  - `packages`: `symmetrical_fore_link` (`is_symmetrical=1`, `Lift`, LH/RH OEM+Aftermarket + bolts), `front_brake_overhaul` (3-slot overhaul with tight-stock rotors, `Lift`), `four_wheel_alignment` (`is_flat_rate=1`, `Lift`, `Alignment Rack`), `cabin_filter_service`, `wiper_blade_service` (`is_flat_rate=1`), `tire_rotation_balance` (`is_flat_rate=1`, `Lift`, `Tire Mount & Road Force Balancer`), `brake_fluid_flush` (`Lift`), `hv_isolation_diagnostic` (`EV High Voltage`), `hv_pyro_fuse_replace` (zero-stock, `Lift`, `EV High Voltage`, `HV Battery Station`), `rear_subframe_camber_sym` (`is_symmetrical=1`, zero-stock, `Lift`).
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
  powershell -ExecutionPolicy Bypass -File scripts\test-in-slot.ps1 -CleanReinstall -Module ism.tests.test_simulation_foundation
  ```
- **Rebuild / refresh `main_golden.sql.gz` from primary `D:\ism` (`main`)**:
  - `provision-bench-slots.ps1 -RefreshGolden` and `promote-to-main.ps1` skip golden rebuilds when `golden_input_fp` is unchanged, or rebuild `main_golden.sql.gz` from a cached Tier-0 upstream base (`base_upstream.sql.gz` + `install-app ism` + `ism.testing.catalog.seed_all`, decoupled from `development.localhost`) and warm idle stale slots asynchronously (`bg-warm`, preserving active feature worktree sticky slots). Pass `-ForceGolden` to force an ISM golden rebuild or `-RebuildBase` / `-FullUpstreamReinstall` for a cold upstream reinstall.
