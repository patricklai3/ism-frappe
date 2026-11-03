# Frappe Code Review Skill
Reference: d:\ism\.agents\skills\frappe-code-review\SKILL.md
Methodology: Review code for Frappe applications focusing on correctness, security, performance, concurrency, API design, and testing.
Key points:
- Prevent silent stateful corruption; fail early and loudly.
- Verify type matching and boundary conditions.
- Strict SQL injection prevention and type confusion auditing in @frappe.whitelist methods.
- Validate input types and defensive programming at trust boundaries.
