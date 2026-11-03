# Skill Copy: frappe-code-review
Source: d:\ism\.agents\skills\frappe-code-review\SKILL.md

Review code for any Frappe application — correctness, security, performance, concurrency, readability, API design, testing, teardown data cleanup.
Teardown rule: Clean up test data in teardown. Do not rely solely on database rollback. ERPNext hooks, naming series, or workflows often commit mid-test. Tests must track created documents and delete them in tearDown(). Cancel submitted documents before deletion. Use a dedicated test prefix for test record names. Residual purge routines must query transactions by party or asset rather than relying on document name prefixes.
