# frappe-code-review Methodology (Local Dump)
Source: d:\ism\.agents\skills\frappe-code-review\SKILL.md
Loaded: 2026-10-26

Key Principles:
1. Correctness & stability (fail early, loudly, no partial commits, preserve invariants).
2. Security (injection, type confusion, permissions).
3. Concurrency (check-then-act races, DB-level unique constraints, transaction isolation).
4. Testing (deterministic, clean up in teardown, stress boundary conditions).
