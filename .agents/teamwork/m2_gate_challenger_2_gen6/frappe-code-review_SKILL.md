# frappe-code-review local copy
Refer to d:\ism\.agents\skills\frappe-code-review\SKILL.md.
Key tenets:
1. Correctness & stability: Fail early and loudly; no partial commits; validate boundaries; watch for destructive DB APIs with empty filters.
2. Security: No raw SQL injection; validate types at trust boundaries; avoid allow_guest unless necessary.
3. Performance: No DB calls in loops (N+1); indexes committed in code; Remove/Reduce/Reuse.
4. Concurrency: Check-then-act race prevention; avoid global mutable state.
5. API design: Principle of least astonishment; backward compatibility.
6. Testing: Deterministic and independent; teardown cleanup; regression test fixes.
