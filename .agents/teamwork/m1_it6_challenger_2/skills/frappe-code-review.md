# Frappe Code Review Skill Dump

Summary of frappe-code-review methodology:
Checklist distilled from years of engineering practice on correctness, security, performance, concurrency, readability, API design, and testing.
Key priorities:
1. Correctness & stability (prevent silent stateful corruption, no partial commits, don't sacrifice atomicity).
2. Concurrency & deadlocks (MariaDB 1020 / lock contention, transaction restarts, cache clearing).
3. Testing hygiene (cleanup test data in teardown, reverse dependency order, deterministic fixtures, zero state bleed across consecutive runs).
