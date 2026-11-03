# Frappe Code Review Methodology (Local Copy)
Source: d:\ism\.agents\skills\frappe-code-review\SKILL.md

Core Methodology:
1. Correctness & stability (highest consequence): fail early/loudly, no partial commits, preserve invariants over UX, check type boundaries.
2. Security: injection protection, type confusion defense, access control.
3. Performance: bounded complexity, no DB calls in loops, indexed queries.
4. Concurrency: check-then-act races, locking footguns, avoiding shared mutable state, MariaDB 1020 / deadlock prevention.
5. Testing: deterministic, independent, robust cleanup, adversarial stress testing.
