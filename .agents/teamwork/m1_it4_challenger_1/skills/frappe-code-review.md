# Frappe Code Review Skill
Summary of key methodology from frappe-code-review:
- Correctness & stability: No partial commits, atomicity, don't sacrifice invariants.
- Concurrency: Locking footguns, tabSingles unindexed scans, avoid check-then-act races, no global mutable state.
- Performance: Avoid N+1, no DB calls in loops, unkeyed table churn causes deadlocks in InnoDB REPEATABLE READ.
- Testing: Deterministic, independent, clean up test data in teardown without leaving residual records or locks.
