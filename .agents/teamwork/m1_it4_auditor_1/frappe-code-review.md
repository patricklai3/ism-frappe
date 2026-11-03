# Quality Code Review (Local Copy for m1_it4_auditor_1)
Source: d:\ism\.agents\skills\frappe-code-review\SKILL.md

Review code for any Frappe application — a checklist distilled from years of
engineering practice on correctness, security, performance, concurrency,
readability, API design, and testing.

Core Methodology:
1. Correctness & stability (highest consequence): fail early, no partial commits, don't sacrifice atomicity, watch destructive APIs with empty filters.
2. Security: Injection prevention, validate input types at trust boundaries, avoid raw SQL, permissions/roles enforcement.
3. Performance: No DB calls in loops, O(1)/O(log N) hot paths, proper indexes, clean memory.
4. Concurrency: Check-then-act is a race, locking footguns on unindexed queries, avoid global mutable state.
5. Readability & Maintainability: Pure functions, boring constructs, consistency.
6. API Design: Least astonishment, backward compatibility, idempotent patches.
7. Testing: Clean up test data in teardown (Two-phase teardown / cascading deletion in reverse dependency order), deterministic tests, prevent deadlocks.
8. Error messages & Observability.
