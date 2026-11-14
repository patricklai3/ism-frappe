# Test Suite Readiness (TEST_READY.md)

## 1. Overview

This file lists the automated test commands, active test modules, and database isolation rules for ISM and `ism-agent-os`.

---

## 2. Test Execution Commands

### Host Synchronization

Sync workspace changes from Windows to WSL2 before running Frappe bench tests:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
```

### Full Frappe Test Suite

Run all ISM tests inside the bench devcontainer:

```powershell
wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
```

### Local Agent OS Unit Tests

Run `ism-agent-os` offline unit and skill-validation tests directly from the repository root:

```powershell
python -m unittest discover -s ism-agent-os/tests -t ism-agent-os -v
```

### Modular Bench Test Invocations

| Domain Area | Command Flag | Coverage Focus |
|---|---|---|
| **Estimation Math** | `--module ism.tests.test_estimation_math` | Log-normal P80, Fenton-Wilkinson summation, category sigmas, Z-scores, buffer padding |
| **Scheduling & Capacity** | `--module ism.tests.test_scheduling_capacity` | Lunch-aware intervals, bay collisions, equipment tags, auto-assign, slots, technician pool gating |
| **RO Scheduling E2E** | `--module ism.tests.test_ro_scheduling_e2e` | Early appointment states, `formalize_provisional_info`, supplemental sessions, view APIs |
| **Agent Facade** | `--module ism.tests.test_agent_facade` | Whitelisted `ism.api.agent` lifecycle endpoints and guardrails |
| **Parts & Tax Integration** | `--module ism.tests.test_parts_management` | Parts workflows, item/procurement custom fields, default tax setup |
| **Tesla Manual Parser** | `--module ism.tests.test_tesla_integration` | Service manual parsing and package model ingestion |
| **ISM Settings** | `--module ism.ism.doctype.ism_settings.test_ism_settings` | Settings validation (`default_labor_rate`, `sms_gateway_provider`, shop hours), idempotency, RBAC |
| **DocType Tests** | `--doctype "Repair Order"`, `--doctype "Repair Vehicle"`, `--doctype "Schedule Entry"`, `--doctype "Schedule Entry Type"`, `--doctype "Service Bay"` | Individual DocType controller and schema validations |

---

## 3. Transactional Hygiene & Isolation Contract

1. **Self-Contained Fixtures**: Scheduling and facade tests use `ism/tests/test_fixtures.py`, which prefixes test records with `Test ` or `_TEST_ISM_`.
2. **Transaction Rollback**: `ISMTestCase` in `ism/tests/base.py` rolls back uncommitted mutations in `tearDown()` and clears `ISM Settings` caches so singleton rows do not lock across MariaDB `REPEATABLE READ` transactions.
3. **Custom DocPerm Safety**: Tests that configure temporary role permissions must use `frappe.permissions.add_permission` (or clean up inside an uncommitted transaction) so `Custom DocPerm` rows do not shadow standard `DocPerm` roles such as `ISM Manager` and `System Manager`.
4. **Shared Site Isolation**: Tests avoid mutating pre-existing business data on `development.localhost`.
