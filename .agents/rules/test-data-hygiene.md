---
description: Test data cleanup and teardown rules to prevent records leaking into Desk lists
globs: ["**/test_*.py", "**/tests/**"]
---

# Test Data Hygiene & Teardown Rules

1. **Transaction-Aware Teardown**:
   - Any test that generates or triggers transactions (`Repair Order`, `Sales Invoice`, `Payment Entry`, `Quotation`, `Sales Order`) must ensure downstream documents are cancelled and deleted before parent records.
   - For `Repair Order`, if `sales_invoice` was created (e.g. via `process_payment_and_release`), cancel/delete linked `Payment Entry` documents, cancel/delete the `Sales Invoice`, and clear `ro.sales_invoice` before cancelling the `Repair Order`.

2. **Party Preservation During Teardown**:
   - Never delete `Customer` before linked `Sales Invoice` or `Payment Entry` documents are cancelled and deleted. ERPNext requires the party to exist when reversing ledger entries (otherwise failing with `Could not find Party: <Customer>`).

3. **Naming Series vs. Prefix**:
   - Transactional documents receive series names (`RO-`, `ACC-SINV-`, `ACC-PAY-`, `CHK-IN-`). Residual cleanup routines must match transactions by test customer or test vehicle, not by document name prefix alone.

4. **Dedicated Test Prefix**:
   - Always prefix generated master records (Customer, Item, Service Package) with `_TEST_ISM_` (or the app's `TEST_PREFIX`).
   - Use standard test VINs (`5YJ3E1EBXNF123456`, `1HGCR2F83HA123456`, `5YJSA1E26CF000001`) and test plates (`REPAIR-01`, `TEST-PLATE`, `TEST-INTAKE`).

5. **Preserve Business and Seed Data**:
   - Never purge or modify seed/live records (specifically `Prestiga-Biz`, its vehicles `LRW3F7FS2NC5xxxxx` and `LRW3F7ET2RC1xxxxx`, check-ins, and `RO-2026-00067`).
