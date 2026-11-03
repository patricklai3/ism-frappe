# Milestone 1 Iteration 3: Security, RBAC, & Standard Type Protection Remediation Blueprint

**Author**: `m1_it3_explorer_2_gen2` (M1 It3 Security & Permissions Spec Miner 2)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it3_explorer_2_gen2`  
**Target Roles / Recipients**: Orchestrator (`0e38d9c2-368a-4e62-8c88-6e3643ab0b5d`), Implementation Worker  

---

## Executive Summary

This report delivers the authoritative remediation blueprint, replacement code, unified diff patches, and adversarial test coverage for two critical findings identified by `m1_reviewer_4`:
1. **Circumvention of Standard Entry Type Deletion Guard via Rename**: Empirical reproduction proved that standard seeded types (`Meeting`, `Internal Service`) could be renamed and immediately deleted without restriction, permanently destroying system records. Analysis of Frappe Framework internals revealed that DocType metadata `"allow_rename": 0` alone is bypassable via programmatic `frappe.rename_doc(..., force=True)`. We formulate a multi-layered defense-in-depth architecture: controller `before_rename` hook, save-time `validate` guard, and DocType `"allow_rename": 0`.
2. **Missing `ISM Manager` Role Permissions on `ISM Settings`**: Verified that `ism_settings.json` restricted all access to `System Manager`. Automotive shop managers holding the `ISM Manager` role were completely blocked from viewing or configuring shop operating hours, break windows, slot intervals, capacity gating, and estimation tuning parameters in Frappe Desk. We provide the exact permission block aligned with all other ISM DocTypes.

---

## 1. Observation

### 1.1 Empirical Reproduction of Rename Deletion Bypass

Executing against `development.localhost` in the devcontainer:
```python
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "
import frappe
frappe.init(site='development.localhost')
frappe.connect()

for name in ['Meeting', 'Internal Service']:
    cnt = frappe.db.count('Schedule Entry', {'entry_type': name})
    if cnt == 0:
        frappe.rename_doc('Schedule Entry Type', name, f'{name}_Temp')
        frappe.delete_doc('Schedule Entry Type', f'{name}_Temp')
        print(f'VULNERABILITY CONFIRMED: DELETED standard type {name} through rename bypass!')
frappe.db.rollback()
"
```
**Verbatim Output**:
```text
VULNERABILITY CONFIRMED: DELETED standard type Meeting through rename bypass!
VULNERABILITY CONFIRMED: DELETED standard type Internal Service through rename bypass!
```
*Exact Location*:
- `d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.json`: Line 3 (`"allow_rename": 1`)
- `d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.py`: Lines 24–30 (`on_trash`) checks only `self.name` and `self.type_name`. When renamed to `Meeting_Temp`, neither matches `STANDARD_SCHEDULE_ENTRY_TYPES`, permitting deletion.

### 1.2 Inspection of Frappe Framework `rename_doc` Semantics

Inspecting `frappe.model.rename_doc.validate_rename`:
```python
if not force and not ignore_permissions and not meta.allow_rename:
    frappe.throw(_("{0} not allowed to be renamed").format(_(doctype)))
```
When `force=True` is passed to `frappe.rename_doc`, `not force` evaluates to `False`, completely bypassing `meta.allow_rename`.

In contrast, in `frappe.model.rename_doc.rename_doc`:
```python
if validate:
    old_doc = doc or frappe.get_doc(doctype, old)
    out = old_doc.run_method("before_rename", old, new, merge) or {}
    new = (out.get("new") or new) if isinstance(out, dict) else (out or new)
    new = validate_rename(...)
```
`before_rename` is evaluated **before** `validate_rename` and is executed even when `force=True`. Raising `frappe.ValidationError` in `before_rename` cannot be bypassed by `force=True`.

### 1.3 Inspection of Direct Property Mutation on Save

Testing direct assignment of `type_name` on standard records:
```python
doc = frappe.get_doc('Schedule Entry Type', 'Diagnostic')
doc.type_name = 'Diagnostic_Hacked'
doc.save()
```
`doc.save()` succeeded, leaving `name = 'Diagnostic'` but mutating the underlying database field `tabSchedule Entry Type.type_name` to `'Diagnostic_Hacked'`, desynchronizing `title_field` and list view display from the primary key.

### 1.4 Empirical Verification of Missing `ISM Manager` Permissions

Testing user permissions on `ISM Settings` with a user assigned only `ISM Manager`:
```python
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "
import frappe
frappe.init(site='development.localhost')
frappe.connect()

user = frappe.get_doc({'doctype': 'User', 'email': 'mgr@test.com', 'first_name': 'M', 'roles': [{'role': 'ISM Manager'}]}).insert(ignore_permissions=True)
frappe.set_user('mgr@test.com')
print('Read permission:', frappe.has_permission('ISM Settings', 'read'))
print('Write permission:', frappe.has_permission('ISM Settings', 'write'))
frappe.db.rollback()
"
```
**Verbatim Output**:
```text
Read permission: False
Write permission: False
```
*Exact Location*:
- `d:\ism\ism\ism\doctype\ism_settings\ism_settings.json`: Lines 208–221 defines only `role: "System Manager"`.
- In contrast, `service_bay.json` (line 77), `schedule_entry.json` (line 244), and `equipment_tag.json` (line 56) all define full permissions for `ISM Manager`.

---

## 2. Logic Chain

1. **Vulnerability in Schedule Entry Type**:
   - `Schedule Entry Type` seeds 5 standard system types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) required for shop operations and milestone logic.
   - Deletion protection in `on_trash()` depends entirely on `self.name in STANDARD_SCHEDULE_ENTRY_TYPES`.
   - Because `schedule_entry_type.json` set `"allow_rename": 1` and `schedule_entry_type.py` lacked a `before_rename` hook, any actor could rename any standard type.
   - Once renamed, `on_trash()` evaluated the new name against `STANDARD_SCHEDULE_ENTRY_TYPES`, found no match, and permitted deletion.
   - For entries without linked appointments (such as `Meeting` or `Internal Service`), this deleted the standard type permanently.

2. **Comparative Evaluation of Remediation Options**:
   - **Option A (`allow_rename: 0` in JSON alone)**:
     - Disables rename action in Desk UI.
     - *Defect*: Any script or API call passing `force=True` to `frappe.rename_doc` bypasses `allow_rename: 0`. Thus Option A alone does NOT close the security hole.
     - *Defect*: Also prevents shop managers from ever renaming custom entry types (e.g., fixing a typo in "Detailing").
   - **Option B (`before_rename` hook in controller alone)**:
     - Intercepts all rename attempts at runtime, including `force=True`.
     - Selectively protects standard types (`STANDARD_SCHEDULE_ENTRY_TYPES`) while allowing custom types to be renamed safely with automatic cascade to linked records.
     - Also prevents renaming custom types *to* standard names (`new_name in STANDARD_SCHEDULE_ENTRY_TYPES`), stopping naming hijacking.
     - *Defect*: Desk UI still renders the "Rename" menu button; clicking it produces a validation error rather than hiding the affordance.
   - **Option C (Recommended Defense-in-Depth)**:
     - Combine Option A and Option B:
       1. Set `"allow_rename": 0` in `schedule_entry_type.json` to disable the rename affordance across Frappe Desk forms.
       2. Add `before_rename(self, old_name, new_name, merge=False)` in `ScheduleEntryType` to cryptographically block programmatic renaming or merging involving standard types.
       3. Add save-time validation in `validate()` to block direct mutation of `self.type_name` on standard records (`self.type_name != self.name`).
       4. Maintain existing `on_trash()` guard against deletion.

3. **RBAC Resolution for ISM Settings**:
   - `ISM Settings` is the operational nerve center for shop operating hours, lunch breaks, slot intervals, capacity toggles, and duration estimation tuning.
   - `setup.py` explicitly created the `ISM Manager` role with `desk_access: 1` to empower automotive shop managers.
   - However, `ism_settings.json` omitted `ISM Manager` from `permissions`.
   - Adding `role: "ISM Manager"` with `read: 1, write: 1` (and standard Desk flags `create`, `delete`, `print`, `email`, `export`, `report`, `share`) restores intended RBAC parity with `Service Bay`, `Equipment Tag`, and `Schedule Entry`.

---

## 3. Caveats

1. **Bench Migrate Required**:
   - Modifying `ism_settings.json` and `schedule_entry_type.json` updates metadata files on disk. To sync them into MariaDB (`tabDocType` and `tabDocPerm`), `bench --site <site> migrate` (or `bench reload-doctype`) must be executed.
2. **Separation from Finding 1 & 2**:
   - MariaDB 1020 transaction isolation and `tearDown()` exception handling in `test_ism_settings.py` are addressed by `m1_it3_explorer_1_gen2`. Our test additions for `ISM Settings` permissions should be incorporated after or in coordination with the test isolation refactoring.

---

## 4. Conclusion & Implementation Blueprints

### 4.1 Remediation Blueprint: `Schedule Entry Type`

#### Target Files:
1. `d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.py`
2. `d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.json`
3. `d:\ism\ism\ism\doctype\schedule_entry_type\test_schedule_entry_type.py`

#### Controller Implementation (`schedule_entry_type.py`):
```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

STANDARD_SCHEDULE_ENTRY_TYPES = {
	"Diagnostic",
	"Repair",
	"Maintenance",
	"Meeting",
	"Internal Service",
}


class ScheduleEntryType(Document):
	def validate(self):
		if self.type_name:
			self.type_name = self.type_name.strip()
		if not self.type_name:
			frappe.throw(_("Type Name cannot be empty."), title=_("Validation Error"))

		# Prevent direct renaming via doc.type_name = "new_name"; doc.save()
		if not self.is_new() and self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
			if self.type_name != self.name:
				frappe.throw(
					_("Cannot rename standard Schedule Entry Type '{0}'.").format(self.name),
					title=_("Standard Type Protected"),
				)

	def before_rename(self, old_name, new_name, merge=False):
		target = (old_name or self.name or "").strip()
		if target in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
			frappe.throw(
				_("Cannot rename standard Schedule Entry Type '{0}'.").format(self.name or target),
				title=_("Standard Type Protected"),
			)

		new_target = (new_name or "").strip()
		if new_target in STANDARD_SCHEDULE_ENTRY_TYPES:
			frappe.throw(
				_("Cannot rename to standard Schedule Entry Type '{0}'.").format(new_name),
				title=_("Standard Type Protected"),
			)

		return new_target

	def on_trash(self):
		target_name = (self.type_name or self.name or "").strip()
		if target_name in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
			frappe.throw(
				_("Cannot delete standard Schedule Entry Type '{0}'.").format(self.name or target_name),
				title=_("Standard Type Protected"),
			)

		if frappe.db.exists("DocType", "Schedule Entry"):
			linked_entries = frappe.get_all(
				"Schedule Entry",
				filters={"entry_type": self.name},
				pluck="name",
				limit=5,
			)
			if linked_entries:
				frappe.throw(
					_(
						"Cannot delete Schedule Entry Type '{0}' because it is referenced by existing Schedule Entry records ({1}). "
						"Reassign or delete these entries before deleting the type."
					).format(self.name, ", ".join(linked_entries)),
					title=_("Type in Use"),
				)
```

#### Metadata Implementation (`schedule_entry_type.json`):
Line 3 change:
```diff
- "allow_rename": 1,
+ "allow_rename": 0,
```

#### Unit Test Additions (`test_schedule_entry_type.py`):
Append the following four test methods to `TestScheduleEntryType`:
```python
	def test_rename_standard_schedule_entry_type_blocked(self):
		"""Verify standard entry types cannot be renamed via rename_doc (even with force=True)."""
		for std_type in ["Diagnostic", "Repair", "Maintenance", "Meeting", "Internal Service"]:
			with self.assertRaises(frappe.ValidationError):
				frappe.rename_doc(
					"Schedule Entry Type",
					std_type,
					f"{std_type}_Renamed",
					force=True,
				)
			self.assertTrue(
				frappe.db.exists("Schedule Entry Type", std_type),
				f"Standard Schedule Entry Type '{std_type}' must remain intact in DB",
			)

	def test_rename_to_standard_schedule_entry_type_blocked(self):
		"""Verify custom entry types cannot be renamed to adopt a standard type name."""
		custom_name = f"{TEST_PREFIX}Custom_Rename_Target"
		if frappe.db.exists("Schedule Entry Type", custom_name):
			frappe.delete_doc("Schedule Entry Type", custom_name, force=True)

		custom_doc = frappe.get_doc({
			"doctype": "Schedule Entry Type",
			"type_name": custom_name,
			"requires_repair_order": 0,
		}).insert()
		self.track(custom_doc)

		with self.assertRaises(frappe.ValidationError):
			frappe.rename_doc(
				"Schedule Entry Type",
				custom_name,
				"Diagnostic",
				force=True,
			)

		custom_doc.delete()

	def test_modify_standard_type_name_on_save_blocked(self):
		"""Verify direct mutation of type_name on standard entry types is blocked on save."""
		doc = frappe.get_doc("Schedule Entry Type", "Diagnostic")
		doc.type_name = "Diagnostic_Mutated"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_rename_bypass_deletion_prevented(self):
		"""Adversarial test: verify that attempting to rename and then delete a standard type fails."""
		for std_type in ["Meeting", "Internal Service"]:
			with self.assertRaises(frappe.ValidationError):
				frappe.rename_doc("Schedule Entry Type", std_type, f"{std_type}_Bypass", force=True)
			doc = frappe.get_doc("Schedule Entry Type", std_type)
			with self.assertRaises(frappe.ValidationError):
				doc.delete()
			self.assertTrue(frappe.db.exists("Schedule Entry Type", std_type))
```

---

### 4.2 Remediation Blueprint: `ISM Settings` Permissions

#### Target File:
`d:\ism\ism\ism\doctype\ism_settings\ism_settings.json`

#### Permission Array Replacement (Lines 208–221):
```json
 "permissions": [
  {
   "create": 1,
   "delete": 1,
   "email": 1,
   "export": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "System Manager",
   "share": 1,
   "write": 1
  },
  {
   "create": 1,
   "delete": 1,
   "email": 1,
   "export": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "ISM Manager",
   "share": 1,
   "write": 1
  }
 ],
```

#### Unit Test Addition (`test_ism_settings.py`):
```python
	def test_ism_manager_permissions(self):
		"""Verify that a user with only ISM Manager role has read and write permissions on ISM Settings."""
		user_email = f"test_ism_mgr_{frappe.generate_hash(length=6)}@example.com"
		if frappe.db.exists("User", user_email):
			frappe.delete_doc("User", user_email, force=True)

		user = frappe.get_doc({
			"doctype": "User",
			"email": user_email,
			"first_name": "Test ISM Manager",
			"roles": [{"role": "ISM Manager"}],
		}).insert(ignore_permissions=True)
		self.track(user)

		self.assertTrue(
			frappe.has_permission("ISM Settings", "read", user=user_email),
			"ISM Manager role should have read permission on ISM Settings",
		)
		self.assertTrue(
			frappe.has_permission("ISM Settings", "write", user=user_email),
			"ISM Manager role should have write permission on ISM Settings",
		)
```

---

### 4.3 Unified Diff Patch

Reference file written to:
`d:\ism\.agents\teamwork\m1_it3_explorer_2_gen2\remediation_m1_it3_explorer_2.patch`

```patch
diff --git a/ism/ism/doctype/schedule_entry_type/schedule_entry_type.py b/ism/ism/doctype/schedule_entry_type/schedule_entry_type.py
index 2345678..3456789 100644
--- a/ism/ism/doctype/schedule_entry_type/schedule_entry_type.py
+++ b/ism/ism/doctype/schedule_entry_type/schedule_entry_type.py
@@ -20,6 +20,31 @@ class ScheduleEntryType(Document):
 			self.type_name = self.type_name.strip()
 		if not self.type_name:
 			frappe.throw(_("Type Name cannot be empty."), title=_("Validation Error"))
+
+		# Prevent direct renaming via doc.type_name = "new_name"; doc.save()
+		if not self.is_new() and self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
+			if self.type_name != self.name:
+				frappe.throw(
+					_("Cannot rename standard Schedule Entry Type '{0}'.").format(self.name),
+					title=_("Standard Type Protected"),
+				)
+
+	def before_rename(self, old_name, new_name, merge=False):
+		target = (old_name or self.name or "").strip()
+		if target in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
+			frappe.throw(
+				_("Cannot rename standard Schedule Entry Type '{0}'.").format(self.name or target),
+				title=_("Standard Type Protected"),
+			)
+
+		new_target = (new_name or "").strip()
+		if new_target in STANDARD_SCHEDULE_ENTRY_TYPES:
+			frappe.throw(
+				_("Cannot rename to standard Schedule Entry Type '{0}'.").format(new_name),
+				title=_("Standard Type Protected"),
+			)
+
+		return new_target
 
 	def on_trash(self):
 		target_name = (self.type_name or self.name or "").strip()
diff --git a/ism/ism/doctype/schedule_entry_type/schedule_entry_type.json b/ism/ism/doctype/schedule_entry_type/schedule_entry_type.json
index 4567890..5678901 100644
--- a/ism/ism/doctype/schedule_entry_type/schedule_entry_type.json
+++ b/ism/ism/doctype/schedule_entry_type/schedule_entry_type.json
@@ -1,6 +1,6 @@
 {
  "actions": [],
- "allow_rename": 1,
+ "allow_rename": 0,
  "autoname": "field:type_name",
  "creation": "2026-10-26 12:00:00.000000",
  "doctype": "DocType",
diff --git a/ism/ism/doctype/ism_settings/ism_settings.json b/ism/ism/doctype/ism_settings/ism_settings.json
index 6789012..7890123 100644
--- a/ism/ism/doctype/ism_settings/ism_settings.json
+++ b/ism/ism/doctype/ism_settings/ism_settings.json
@@ -217,6 +217,18 @@
    "role": "System Manager",
    "share": 1,
    "write": 1
+  },
+  {
+   "create": 1,
+   "delete": 1,
+   "email": 1,
+   "export": 1,
+   "print": 1,
+   "read": 1,
+   "report": 1,
+   "role": "ISM Manager",
+   "share": 1,
+   "write": 1
   }
  ],
  "row_format": "Dynamic",
diff --git a/ism/ism/doctype/schedule_entry_type/test_schedule_entry_type.py b/ism/ism/doctype/schedule_entry_type/test_schedule_entry_type.py
index 8901234..9012345 100644
--- a/ism/ism/doctype/schedule_entry_type/test_schedule_entry_type.py
+++ b/ism/ism/doctype/schedule_entry_type/test_schedule_entry_type.py
@@ -93,3 +93,59 @@ class TestScheduleEntryType(ISMTestCase):
 		se.delete()
 		custom_type.delete()
 		self.assertFalse(frappe.db.exists("Schedule Entry Type", type_name))
+
+	def test_rename_standard_schedule_entry_type_blocked(self):
+		"""Verify standard entry types cannot be renamed via rename_doc (even with force=True)."""
+		for std_type in ["Diagnostic", "Repair", "Maintenance", "Meeting", "Internal Service"]:
+			with self.assertRaises(frappe.ValidationError):
+				frappe.rename_doc(
+					"Schedule Entry Type",
+					std_type,
+					f"{std_type}_Renamed",
+					force=True,
+				)
+			# Ensure the standard type still exists with its original name
+			self.assertTrue(
+				frappe.db.exists("Schedule Entry Type", std_type),
+				f"Standard Schedule Entry Type '{std_type}' must remain intact in DB",
+			)
+
+	def test_rename_to_standard_schedule_entry_type_blocked(self):
+		"""Verify custom entry types cannot be renamed to adopt a standard type name."""
+		custom_name = f"{TEST_PREFIX}Custom_Rename_Target"
+		if frappe.db.exists("Schedule Entry Type", custom_name):
+			frappe.delete_doc("Schedule Entry Type", custom_name, force=True)
+
+		custom_doc = frappe.get_doc({
+			"doctype": "Schedule Entry Type",
+			"type_name": custom_name,
+			"requires_repair_order": 0,
+		}).insert()
+		self.track(custom_doc)
+
+		with self.assertRaises(frappe.ValidationError):
+			frappe.rename_doc(
+				"Schedule Entry Type",
+				custom_name,
+				"Diagnostic",
+				force=True,
+			)
+
+		custom_doc.delete()
+
+	def test_modify_standard_type_name_on_save_blocked(self):
+		"""Verify direct mutation of type_name on standard entry types is blocked on save."""
+		doc = frappe.get_doc("Schedule Entry Type", "Diagnostic")
+		doc.type_name = "Diagnostic_Mutated"
+		with self.assertRaises(frappe.ValidationError):
+			doc.save()
+
+	def test_rename_bypass_deletion_prevented(self):
+		"""Adversarial test: verify that attempting to rename and then delete a standard type fails."""
+		for std_type in ["Meeting", "Internal Service"]:
+			with self.assertRaises(frappe.ValidationError):
+				frappe.rename_doc("Schedule Entry Type", std_type, f"{std_type}_Bypass", force=True)
+			doc = frappe.get_doc("Schedule Entry Type", std_type)
+			with self.assertRaises(frappe.ValidationError):
+				doc.delete()
+			self.assertTrue(frappe.db.exists("Schedule Entry Type", std_type))
```

---

## 5. Verification Method

Once changes are applied by the implementation worker, run the following verification steps:

1. **Migrate DocType Schemas into Database**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate
   ```

2. **Verify Schedule Entry Type Test Suite**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry Type"
   ```
   *Expected Result*: All 8 tests pass (4 original tests + 4 new rename/mutation protection tests).

3. **Verify Adversarial Rename Bypass is Blocked**:
   ```python
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "
   import frappe
   frappe.init(site='development.localhost')
   frappe.connect()

   for std in ['Diagnostic', 'Meeting', 'Internal Service']:
       try:
           frappe.rename_doc('Schedule Entry Type', std, f'{std}_X', force=True)
           print(f'FAILURE: Renamed {std}!')
       except frappe.ValidationError as e:
           print(f'VERIFIED: Rename of {std} blocked with ValidationError: {e}')
   frappe.db.rollback()
   "
   ```

4. **Verify ISM Manager Permissions on ISM Settings**:
   ```python
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "
   import frappe
   frappe.init(site='development.localhost')
   frappe.connect()

   user_email = 'test_ism_mgr@example.com'
   if frappe.db.exists('User', user_email):
       frappe.delete_doc('User', user_email, force=True)
   user = frappe.get_doc({'doctype': 'User', 'email': user_email, 'first_name': 'Test', 'roles': [{'role': 'ISM Manager'}]}).insert(ignore_permissions=True)

   print('ISM Manager read perm:', frappe.has_permission('ISM Settings', 'read', user=user_email))
   print('ISM Manager write perm:', frappe.has_permission('ISM Settings', 'write', user=user_email))

   frappe.delete_doc('User', user_email, force=True)
   frappe.db.rollback()
   "
   ```
   *Expected Result*: Both read perm and write perm output `True`.
