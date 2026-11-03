# Quality Code Review (Local Skill Copy)

A reviewer's checklist for Frappe applications. Protect **correctness, security, and the future maintainer**, in that order of consequence.

## Concurrency & Deadlocks
- Check-then-act is a race. Use DB-level unique constraints.
- Rapid consecutive deletes and inserts against identical primary keys cause MariaDB 1020 (HA_ERR_RECORD_CHANGED) errors due to unpurged tombstone records.
- Avoid unnecessary commits; use transactional rollback where possible.

## Testing Isolation & Teardown
- Tests must be deterministic and independent.
- Clean up test data in teardown. Do not rely solely on database rollback.
- Track created documents and delete in reverse dependency order.
- Child links (like Repair Vehicle Customer) must be cleaned up when parent documents are removed.
- Residual purges should ensure zero test entities remain in the database.
