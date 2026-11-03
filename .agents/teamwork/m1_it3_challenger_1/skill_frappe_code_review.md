# Frappe Code Review Skill Local Copy
Reference: d:\ism\.agents\skills\frappe-code-review\SKILL.md

Key principles for this challenge:
1. Concurrency & Locking:
   - Check-then-act is a race.
   - Locking footguns: MariaDB error 1020 happens on record changed since last read in a table / optimistic lock conflict or concurrent modification of tabSingles.
   - Global mutable state / class attributes / cache contamination.
2. Correctness & Stability:
   - No partial commits mid-transaction.
   - Preserve invariants (canonical defaults intact: default_slot_interval=30, enable_technician_capacity=1, estimation_percentile=80.0).
3. Testing:
   - Tests must be deterministic and independent. Clean up test data in teardown. Do not corrupt tabSingles for downstream tests.
