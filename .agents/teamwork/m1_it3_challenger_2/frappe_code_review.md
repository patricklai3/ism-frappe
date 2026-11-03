# Frappe Code Review Methodology (Local Dump)
Referenced from d:\ism\.agents\skills\frappe-code-review\SKILL.md.
Key Focus Areas for Adversarial Review:
1. Correctness & stability: stateful corruption, uncommitted transactions, destructive API guards.
2. Security: Injection, authorization, allowlists vs blocklists.
3. Testing: Test isolation, cleanup in teardown, reverse dependency deletion, deterministic execution.
4. Concurrency & locking: MariaDB 1020, tabSingles, check-then-act.
