# Frappe Code Review (Local Copy)
Source: d:\ism\.agents\skills\frappe-code-review\SKILL.md

Key principles:
1. Correctness & stability (highest consequence): no partial commits, validate issue before fixing, preserve invariants, watch empty filters.
2. Security: no SQL injection (use qb/ORM), validate types at trust boundaries, allowlists over blocklists.
3. Performance: reads < 100ms, no DB calls in loops, reorder conditionals so DB call is last, don't fetch whole doc for one value.
4. Concurrency: check-then-act is race, ensure locking uses indexed filters.
5. Readability: pure functions where possible, match surrounding style, docstrings on public APIs.
6. API design & backward compatibility: principle of least astonishment, non-breaking changes, clean teardown.
7. Testing & Hygiene: deterministic tests, clean up test data in teardown.
