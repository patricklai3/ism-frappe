# frappe-code-review summary
See d:\ism\.agents\skills\frappe-code-review\SKILL.md
- Correctness & stability: fail early & loudly, no partial commits, validate types, no destructive empty filters.
- Security: use ORM/frappe.qb, no raw SQL concat, validate input types at trust boundaries, check permissions.
- Performance: indexes on filtered columns, no DB calls in loops, aggregate in SQL.
- Concurrency: unique constraints, stateless helpers.
- Testing: test business logic, deterministic, cleanup in tearDown().
