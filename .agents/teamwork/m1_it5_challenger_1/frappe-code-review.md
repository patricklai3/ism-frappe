# Frappe Code Review Skill Reference
(Dumped from d:\ism\.agents\skills\frappe-code-review\SKILL.md)

Key review focus for Challenger 1:
- Concurrency & Deadlocks:
  - Check-then-act race conditions
  - Locking footguns: SELECT ... FOR UPDATE on unindexed queries, table locks
  - In MariaDB 11.8 with innodb_snapshot_isolation = ON, uncommitted reads leave read snapshots open
  - MariaDB Error 1020: "Record has changed since last read in table ...; try restarting transaction"
  - tabSingles bulk delete/insert triggering ER_CHECKREAD under REPEATABLE READ
- Testing & Teardown:
  - Clean up test data in teardown without deleting protected shared static fixtures
  - Tests must be deterministic and independent
  - No partial commits or leaked transaction state
