# Quality Code Review (Local Copy for m1_it7_auditor_1)
Source: d:\ism\.agents\skills\frappe-code-review\SKILL.md

A reviewer's checklist for Frappe applications. Protect correctness, security, and the future maintainer.
Key checks:
1. Correctness & stability: fail early, no partial commits (frappe.db.commit mid-transaction), destructive DB APIs with None filters, type mismatch in condition, question return shape.
2. Security: SQL injection (ORM/qb preferred, parameter substitution), type confusion in filters, safe_exec, allow_guest, scoping permissions.
3. Performance: <100ms reads, <5s writes, no DB calls in loops (N+1), proper indexing, aggregate in SQL.
4. Concurrency: check-then-act races, SELECT FOR UPDATE locking footguns, global mutable state.
5. Readability & maintainability: pure functions, avoid mutating inputs, consistent style.
6. API design & backward compatibility: principle of least astonishment, avoid loose types, do not monkey-patch core.
7. Testing: decent test coverage, regression testing, deterministic/independent tests, clean up test data in teardown (dedicated prefixes, cancel submitted before deletion, avoid reliance on rollback alone when commits happen).
8. Error messages, logging & observability: clear error messages.
