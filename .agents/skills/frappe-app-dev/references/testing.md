# Testing

## File location

Tests live alongside the code they test:
```
apps/<app>/<app>/<module>/doctype/<doctype>/test_<doctype>.py
```

For feature-wise tests, place in the tests directory:
```
apps/<app>/<app>/tests/test_<feature>.py
```

## Writing tests

```python
import frappe
from frappe.tests import IntegrationTestCase

class TestExpense(IntegrationTestCase):
    def test_expense_creation(self):
        doc = frappe.get_doc(doctype="Expense", title="Test", amount=100)
        doc.insert()
        self.assertEqual(doc.amount, 100)

    def test_validation(self):
        doc = frappe.get_doc(doctype="Expense", title="Test", amount=-1)
        self.assertRaises(frappe.ValidationError, doc.insert)
```

Key patterns:
- Inherit from an app base test case or `frappe.tests.IntegrationTestCase`.
- Do not rely solely on database rollback.
- Explicitly delete created test records in `tearDown()`.

## Test data cleanup and teardown hygiene

Do not rely solely on database rollback. ERPNext triggers, naming series, and sub-operations often execute `frappe.db.commit()`. When a commit occurs, records persist in the database and pollute desk list views.

Always clean up test data in `tearDown()`:

1. Maintain a list of created records in `setUp()`.
2. Append created documents to the list.
3. In `tearDown()`, process the list in reverse order.
4. Cancel submitted documents before deletion.
5. Delete each document with `frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)`.
6. Prefix all test names with a distinct marker (for example, `_TEST_`).
7. Delete residual records matching the prefix.
8. Call `frappe.db.commit()` at the end of `tearDown()`.

Example base test case pattern:

```python
import unittest
import frappe

TEST_PREFIX = "_TEST_APP_"

class AppTestCase(unittest.TestCase):
    def setUp(self):
        self._tracked_docs = []
        self.cleanup_residual_data()

    def tearDown(self):
        self.cleanup_tracked_docs()
        self.cleanup_residual_data()
        frappe.db.commit()

    def track(self, doc):
        if hasattr(doc, "doctype") and hasattr(doc, "name"):
            self._tracked_docs.append((doc.doctype, doc.name))
        return doc

    def cleanup_tracked_docs(self):
        while self._tracked_docs:
            doctype, name = self._tracked_docs.pop()
            if frappe.db.exists(doctype, name):
                try:
                    doc = frappe.get_doc(doctype, name)
                    if doc.docstatus == 1:
                        doc.cancel()
                    frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
                except Exception:
                    pass

    @classmethod
    def cleanup_residual_data(cls):
        records = frappe.get_all("Customer", filters=[["name", "like", f"{TEST_PREFIX}%"]], pluck="name")
        for name in records:
            try:
                frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
            except Exception:
                pass
```

## Unit tests (no database)

For pure logic that doesn't need Frappe context or database:

```python
from frappe.tests import UnitTestCase

class TestExpenseUtils(UnitTestCase):
    def test_calculate_tax(self):
        self.assertEqual(calculate_tax(100, 0.1), 10)
```

`UnitTestCase` is faster — no DB setup/teardown. Use for utility functions, calculations, parsing logic.

## Test fixtures

For test data that multiple tests need, create `test_records` or use `setUp`:

```python
class TestExpense(IntegrationTestCase):
    def setUp(self):
        self.category = frappe.get_doc(doctype="Expense Category", category_name="Travel").insert()
```

## Test site

Run tests on a **separate site** from the one the user is actively working on. Tests create, modify, and delete data — running them on the development site will pollute it.

Convention: if the dev site is `expense.localhost`, create `expense-test.localhost` for tests:
```bash
bench new-site expense-test.localhost --admin-password admin
bench --site expense-test.localhost install-app <app-name>
```

Always run tests against the test site:
```bash
bench --site expense-test.localhost run-tests --app <app-name>
```

## Running tests

```bash
# All tests for an app
bench --site <site> run-tests --app <app-name>

# Specific DocType
bench --site <site> run-tests --doctype "Expense"

# Specific test file
bench --site <site> run-tests --module <app>.<module>.doctype.<doctype>.test_<doctype>

# Specific test method
bench --site <site> run-tests --module <app>.<module>.doctype.<doctype>.test_<doctype> --test test_expense_creation
```

## Common pitfalls

- If tests fail with "DocType not found", run `bench --site <site> migrate` first.
- Test files must be named `test_*.py` to be discovered.
