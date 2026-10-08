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

### Transaction Hierarchy and Dependency Order

Deleting or cancelling records in the wrong order triggers link validation errors (such as `Cannot cancel Repair Order while active Sales Invoice exists` or ERPNext's `Could not find Party`). Always clean up in strict reverse dependency order:

1. **Payment Entries**: Cancel and delete payment allocations referencing Sales Invoices first.
2. **Sales Invoices**: Cancel and delete background invoices; unlink them from parent records.
3. **Primary Transactions**: (e.g. Repair Orders, Sales Orders, Quotations) Cancel and delete once downstream financial links are clear.
4. **Intake / Check-in Documents**: Delete check-ins.
5. **Assets / Vehicles**: Remove child rows (e.g. `Repair Vehicle Customer`), then delete vehicle.
6. **Customers / Parties**: **Always delete last**. ERPNext requires the party to exist when cancelling ledger entries.

Always clean up test data in `tearDown()`:

1. Maintain a list of created records in `setUp()` and track created docs with `self.track(doc)`.
2. In `tearDown()`, process the tracked list in reverse dependency order.
3. If a document triggered background financial/stock documents (e.g. Sales Invoice, Payment Entry), cancel and delete those downstream documents before cancelling the parent document.
4. Delete master records (Customer, Vehicle) only after all linked transactions are gone.
5. In `cleanup_residual_data()`, query transactions by test customer/vehicle rather than document name prefix (since naming series assign names like `RO-` or `ACC-SINV-`).
6. Call `frappe.db.commit()` at the end of `tearDown()`.

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
            if not frappe.db.exists(doctype, name):
                continue
            try:
                doc = frappe.get_doc(doctype, name)
                if doctype == "Repair Order":
                    self._teardown_repair_order(doc)
                elif doctype == "Sales Invoice":
                    self._teardown_sales_invoice(doc)
                elif doctype == "Payment Entry":
                    self._teardown_payment_entry(doc)
                elif doctype == "Customer":
                    self._teardown_customer(doc)
                else:
                    if doc.docstatus == 1:
                        doc.cancel()
                    frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
            except Exception:
                pass

    def _teardown_repair_order(self, doc):
        si_name = getattr(doc, "sales_invoice", None)
        if si_name and frappe.db.exists("Sales Invoice", si_name):
            self._teardown_sales_invoice(frappe.get_doc("Sales Invoice", si_name))
            doc.db_set("sales_invoice", None)
            doc.reload()
        if doc.docstatus == 1:
            doc.cancel()
        frappe.delete_doc("Repair Order", doc.name, force=True, ignore_permissions=True)

    def _teardown_sales_invoice(self, doc):
        pe_refs = frappe.get_all(
            "Payment Entry Reference",
            filters={"reference_doctype": "Sales Invoice", "reference_name": doc.name},
            pluck="parent",
        )
        for pe_name in set(pe_refs):
            if frappe.db.exists("Payment Entry", pe_name):
                self._teardown_payment_entry(frappe.get_doc("Payment Entry", pe_name))
        if doc.docstatus == 1:
            doc.cancel()
        frappe.delete_doc("Sales Invoice", doc.name, force=True, ignore_permissions=True)

    def _teardown_payment_entry(self, doc):
        if doc.docstatus == 1:
            doc.cancel()
        frappe.delete_doc("Payment Entry", doc.name, force=True, ignore_permissions=True)

    def _teardown_customer(self, doc):
        frappe.db.delete("Repair Vehicle Customer", {"customer": doc.name})
        if doc.docstatus == 1:
            doc.cancel()
        frappe.delete_doc("Customer", doc.name, force=True, ignore_permissions=True)

    @classmethod
    def cleanup_residual_data(cls):
        # Match test transactions via party/vehicle and purge in reverse order
        from ism.tests.cleanup import cleanup_all_test_data
        cleanup_all_test_data(verbose=False)
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
