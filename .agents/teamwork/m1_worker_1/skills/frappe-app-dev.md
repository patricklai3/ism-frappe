# frappe-app-dev summary
See d:\ism\.agents\skills\frappe-app-dev\SKILL.md
- Bare bench commands: bench --site <site> ...
- Do not mkdir DocType folders manually (bench migrate handles it, or create module/doctype files properly).
- DocType definitions: JSON schema with standard DocType fields, python controller subclassing Document.
- Test suites: IntegrationTestCase (or FrappeTestCase) in test_<doctype>.py.
- Validations in controller `validate()`, `before_save()`, etc.
