# frappe-code-review skill copy
(Refer to d:\ism\.agents\skills\frappe-code-review\SKILL.md for complete details)
Core methodology: Quality Code Review for Frappe applications focusing on Correctness, Security, Performance, Concurrency, Readability, API design, and Testing.
Key items:
- Concurrency & locking: check-then-act races, SELECT ... FOR UPDATE on unindexed queries, tabSingles contention, stateless query building.
- Migration & patch hygiene: idempotency, dependency order, schema updates.
- Deterministic tests, teardown cleanup.
