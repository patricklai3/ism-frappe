# Milestone 1 Specification & Implementation Plan: Service Bay & Equipment Tag Schemas

**Author**: M1 Explorer 1 (`teamwork_preview_spec_miner`)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_explorer_1`  
**Reference Repository**: `induct_shop` (`C:\Users\real2\AppData\Local\Temp\induct_shop\induct_shop`)  
**Target Repository**: `d:\ism`  
**Authoritative Requirements**: `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md`, `d:\ism\PROJECT.md`, `d:\ism\.agents\teamwork\m1_explorer_1\DISPATCH.md`

---

## 1. Observation

Direct examination of the reference implementation `induct_shop` and the target codebase `d:\ism` revealed the following schemas, configurations, hooks, and operational behaviors:

### 1.1 Reference Implementation: `induct_shop`

1. **`Service Bay` DocType**:
   - Location: `C:\Users\real2\AppData\Local\Temp\induct_shop\induct_shop\induct_shop\doctype\service_bay\service_bay.json` (Lines 1–77)
     - `autoname`: `"field:bay_name"`, `naming_rule`: `"By fieldname"`, `title_field`: `"bay_name"`, `allow_rename`: 1.
     - Fields:
       - `bay_name` (Data, reqd: 1, unique: 1, in_list_view: 1)
       - `is_active` (Check, default: "1", in_list_view: 1)
       - `description` (Small Text)
       - `sb_equipment` (Section Break, label: "Equipment Tags")
       - `equipment` (Table, options: "Service Bay Equipment", label: "Equipment Tags")
     - Permissions (Lines 58–71):
       - Single role `System Manager` with create, read, write, delete, email, export, print, report, share.
     - Module: `"Induct Shop"`
   - Controller: `service_bay.py` (Lines 1–10)
     - Empty `class ServiceBay(Document): pass`.
   - Unit Tests: `induct_shop/tests/test_service_bay.py` (Lines 1–55)
     - Exercises creation with equipment table (`{"equipment_tag": "Lift"}`), verifies `frappe.db.exists("Service Bay", bay_name)`, and tests active bay query filtering with `filters={"is_active": 1}`.

2. **`Equipment Tag` DocType**:
   - Location: `C:\Users\real2\AppData\Local\Temp\induct_shop\induct_shop\induct_shop\doctype\equipment_tag\equipment_tag.json` (Lines 1–52)
     - `autoname`: `"field:tag_name"`, `naming_rule`: `"By fieldname"`, `title_field`: `"tag_name"`, `allow_rename`: 1.
     - Fields:
       - `tag_name` (Data, reqd: 1, unique: 1, in_list_view: 1)
       - `description` (Small Text)
     - Permissions: `System Manager` with full CRUD.
     - Module: `"Induct Shop"`
   - Controller: `equipment_tag.py` (Lines 1–6)
     - Empty `class EquipmentTag(Document): pass`.
   - Integration: `induct_shop/tests/test_equipment_tags.py` (Lines 1–46)
     - Uses default tags `"Lift"` and `"Alignment Rack"`.

3. **`Service Bay Equipment` Child DocType**:
   - Location: `C:\Users\real2\AppData\Local\Temp\induct_shop\induct_shop\induct_shop\doctype\service_bay_equipment\service_bay_equipment.json` (Lines 1–30)
     - `istable`: 1, `module`: `"Induct Shop"`, `permissions`: `[]`.
     - Fields:
       - `equipment_tag` (Link, options: "Equipment Tag", reqd: 1, in_list_view: 1).
   - Controller: `service_bay_equipment.py` (Lines 1–6)
     - Empty `class ServiceBayEquipment(Document): pass`.

4. **Reference Seeding in `induct_shop`**:
   - Location: `induct_shop/install.py` (Lines 61–69)
     - `seed_equipment_tags()` seeded only `"Lift"`.
   - Location: `induct_shop/tests/test_fixtures.py` (Lines 6, 213–221)
     - Test suite seeded three tags: `EQUIPMENT_TAGS = ["Lift", "Alignment Rack", "HV Battery Station"]`.
   - Location: `induct_shop/api/scheduling.py` (Lines 189–204)
     - Evaluates required equipment matching using set containment: `required_tags_set.issubset(bay_tags_set)`.

### 1.2 Target Codebase: `d:\ism`

1. **Module Name**:
   - `d:\ism\ism\modules.txt` Line 1: `ISM`.
   - All existing DocTypes (`inspection_template`, `repair_vehicle`, `repair_order`, `ism_settings`) declare `"module": "ISM"`.

2. **Code Style & Formatting**:
   - `d:\ism\pyproject.toml` (Lines 26–65):
     - `target-version = "py314"`
     - `indent-style = "tab"`
     - `quote-style = "double"`
     - `line-length = 110`

3. **Role & Permission Models**:
   - Target DocTypes in `d:\ism\ism\ism\doctype` declare permissions for `System Manager`, `Sales User`, `Maintenance User`, and `All`.
   - Dispatch specification explicitly mandates: `Permissions: System Manager, ISM Manager, Desk access`.

4. **Installation & Migration Hooks**:
   - `d:\ism\ism\setup.py` (Lines 506–522):
     - `after_migrate()` calls routine setup functions (`create_standard_inspection_template`, `setup_default_ism_settings`, etc.).
     - `after_install()` delegates directly to `after_migrate()`.
   - `d:\ism\ism\hooks.py` (Lines 95–96):
     - `after_install = "ism.setup.after_install"` is configured.
     - **Deficiency Observed**: `after_migrate` is NOT registered in `hooks.py`. On existing benches, running `bench migrate` does not trigger `setup.after_migrate()`. Registering `after_migrate = "ism.setup.after_migrate"` in `hooks.py` is required for idempotent schema and seed synchronizations.

---

## 2. Logic Chain

1. **Module Alignment (`Induct Shop` → `ISM`)**:
   - *Observation*: `induct_shop` uses `module: "Induct Shop"`. `d:\ism\ism\modules.txt` specifies `ISM`.
   - *Logic*: All target DocTypes (`Service Bay`, `Equipment Tag`, `Service Bay Equipment`) must specify `"module": "ISM"` to correctly bind with Frappe's app loader, directory routing, and migration sync.

2. **Schema & Field Architecture**:
   - *Observation*: `Service Bay` requires physical identification, active status toggle, optional descriptions, and an extensible list of capabilities. `bay_name` and `tag_name` are human-readable, unique identifiers.
   - *Logic*: Using `naming_rule: "By fieldname"` with `autoname: "field:bay_name"` (and `field:tag_name`) ensures that document names match the user input verbatim, allowing clean API references without cryptic hashes. Setting `allow_rename: 1` enables shop managers to rename bays while Frappe automatically updates linked child rows and schedule entries.

3. **Child Table Normalization**:
   - *Observation*: Different bays have varying combinations of equipment (e.g. Bay 1 has Lift; Bay 2 has Lift and Alignment Rack; Bay 3 is flat floor).
   - *Logic*: The child DocType `Service Bay Equipment` with `istable: 1` and a required Link field `equipment_tag` provides a fully normalized, many-to-many relationship. A section break `sb_equipment` on `Service Bay` establishes clean form hierarchy.

4. **Controller Validations**:
   - *Observation*: The reference implementation left controller classes completely empty (`pass`).
   - *Logic*:
     - *Whitespace Sanitization*: Data entry errors often introduce accidental leading or trailing spaces. Stripping `bay_name` and `tag_name` prevents duplicate key confusion.
     - *Duplicate Equipment Tag Prevention*: A user accidentally selecting "Lift" twice on the same bay causes redundant DB rows and set pollution. `ServiceBay.validate()` must inspect child rows and reject duplicates.
     - *Deletion Protection*: Deleting an `Equipment Tag` that is actively referenced in a `Service Bay Equipment` row corrupts equipment filtering queries. `EquipmentTag.on_trash()` must check `frappe.db.count("Service Bay Equipment", {"equipment_tag": self.name})` and reject deletion.
     - *Schedule Entry Protection*: Deleting a `Service Bay` with pending or active appointments breaks scheduling views. `ServiceBay.on_trash()` must check for active `Schedule Entry` records.

5. **Permission & Role Architecture**:
   - *Observation*: Dispatch requires `System Manager, ISM Manager, Desk access`. Currently, the `ISM Manager` role does not exist in a default Frappe/ERPNext installation.
   - *Logic*:
     - Seeding the `Role` "ISM Manager" with `desk_access: 1` inside `ism/setup.py` ensures the role exists before permissions are evaluated.
     - In DocType JSONs, `System Manager` and `ISM Manager` are granted full administrative CRUD permissions.
     - Standard Desk roles (`Maintenance User` and `Sales User`) are granted `read: 1` and `report: 1`, allowing technicians and intake service advisors to view available bays and tags in Desk and scheduling dialogs without permitting unauthorized layout edits.

6. **Seeding Logic Consolidation**:
   - *Observation*: Dispatch requirement 4 mandates seeding default tags: `Lift`, `EV High Voltage`, `Alignment Rack`.
   - *Logic*:
     - Define `DEFAULT_EQUIPMENT_TAGS` in `ism/setup.py`.
     - Implement idempotent `seed_equipment_tags()` that verifies tag existence before inserting.
     - Call `seed_equipment_tags()` and `ensure_ism_roles()` from `after_migrate()`.
     - Register `after_migrate = "ism.setup.after_migrate"` in `ism/hooks.py`.

---

## 3. Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|---|---|---|---|---|---|---|
| 1 | Data Model | `Service Bay` Primary Key | Primary identification of service bay using `bay_name` | `bay_name` (Data, unique, reqd) | Document named `bay_name` | Throws `DuplicateEntryError` if bay name already exists | `induct_shop/doctype/service_bay/service_bay.json:4` |
| 2 | Data Model | `Service Bay` Active State | Flag to enable or disable bay from active scheduling roster | `is_active` (Check, default 1) | Included in active queries | If 0, excluded from `get_available_bays` and slot generation | `induct_shop/doctype/service_bay/service_bay.json:26` |
| 3 | Data Model | `Service Bay` Rename Support | Allows shop administrators to rename bays with cascading references | New bay name string | Updated document and foreign key links | Fails if target name already exists | `induct_shop/doctype/service_bay/service_bay.json:3` |
| 4 | Data Model | `Equipment Tag` Primary Key | Taxonomy tag representing specialized equipment | `tag_name` (Data, unique, reqd) | Document named `tag_name` | Throws `DuplicateEntryError` if tag name exists | `induct_shop/doctype/equipment_tag/equipment_tag.json:4` |
| 5 | Data Model | `Service Bay Equipment` Child Table | Child table linking service bays to capability equipment tags | `equipment_tag` (Link: Equipment Tag) | Child row in `tabService Bay Equipment` | Throws `LinkValidationError` if tag does not exist | `induct_shop/doctype/service_bay_equipment/service_bay_equipment.json:11` |
| 6 | Validation | Duplicate Tag Prevention | Rejects duplicate equipment tags within the same service bay child table | `equipment` child table rows | Clean set of unique tags | Raises `frappe.ValidationError` specifying row and tag name | Target enhancement (quality standard) |
| 7 | Referential Integrity | Equipment Tag Deletion Protection | Prevents deleting an equipment tag while it is in active use by service bays | `tag_name` to delete | Successful deletion or block | Raises `frappe.ValidationError` listing assigned bays | Target enhancement (quality standard) |
| 8 | Referential Integrity | Service Bay Deletion Protection | Prevents deleting a service bay that has pending or active appointments | `bay_name` to delete | Successful deletion or block | Raises `frappe.ValidationError` listing active schedule entries | Target enhancement (quality standard) |
| 9 | Seeding | Default Equipment Tag Seeding | Automatically seeds standard automotive tags (`Lift`, `EV High Voltage`, `Alignment Rack`) | Migration / install hook | 3 standard `Equipment Tag` records | Idempotent: skips existing tags without overwriting custom edits | `ORIGINAL_REQUEST.md:31` & `DISPATCH.md:23` |
| 10 | Security / RBAC | ISM Manager Role Seeding | Auto-creates `ISM Manager` role with Desk login permission | Migration hook | `Role` document with `desk_access: 1` | Idempotent: checks `frappe.db.exists("Role", "ISM Manager")` | `DISPATCH.md:15` |
| 11 | Permissions | Role-Based Access Hierarchy | Grants CRUD to managers and read/report to service advisors and technicians | User session roles | Desk UI access and query authorization | Frappe `PermissionError` on unauthorized mutation | `DISPATCH.md:15` & `repair_vehicle.json` |
| 12 | Capacity / Filter | Superset Equipment Filtering | Matches bays whose equipment tags form a superset of the required tags | Proposed job `required_tags` list | Filtered list of matching bays | Returns empty list if no bay provides all required tags | `induct_shop/api/scheduling.py:206` |
| 13 | Hook Integration | After Migrate Hook Registration | Wires `setup.py:after_migrate` into Frappe's bench lifecycle | `bench migrate` CLI execution | Runs role and equipment tag seeders | None | `d:\ism\ism\hooks.py:95` |

---

## 4. Edge Cases

| # | Feature | Input / Condition | Observed Behavior | Required Target Behavior |
|---|---|---|---|---|
| 1 | `Service Bay` Creation | Empty `bay_name` (e.g. `""` or `"   "`) | In `induct_shop`, Frappe raised generic mandatory field error. | Strips whitespace and raises descriptive `ValidationError(_("Bay Name cannot be empty."))`. |
| 2 | `Service Bay` Child Table | Duplicate equipment tags added in form (e.g. `Lift` in row 1, `Lift` in row 2) | In `induct_shop`, saved duplicate rows without validation. | Controller throws `ValidationError(_("Duplicate equipment tag '{0}' at row #{1}."))`. |
| 3 | `Service Bay` Child Table | Service bay created with 0 equipment tags (empty table) | Bay is created successfully; functions as a flat-floor general bay. | Same. Bays without equipment tags match jobs requiring no special equipment (`required_tags=[]`). |
| 4 | `Equipment Tag` Deletion | User attempts to delete `Equipment Tag` that is assigned to `Bay 1` | In Frappe, generic link error or orphan child rows if not enforced. | `EquipmentTag.on_trash()` queries `tabService Bay Equipment` and raises `ValidationError` listing assigned bays. |
| 5 | `Equipment Tag` Seeding | Tag `Lift` already exists with user-customized description | Re-running migration overwrites or duplicates if non-idempotent. | `frappe.db.exists("Equipment Tag", "Lift")` skips insertion, preserving existing descriptions. |
| 6 | `Service Bay` Active State | Bay marked `is_active = 0` | In `induct_shop`, excluded from `get_all("Service Bay", filters={"is_active": 1})`. | Same. Excluded from new appointment booking while preserving historical schedule records. |
| 7 | `Service Bay` Rename | Renaming `Bay 1` to `Express Bay 1` via Desk rename dialog | Frappe cascades rename across parent keys in child tables. | Renames `Service Bay`, updates `Service Bay Equipment.parent`, and cascades to `Schedule Entry.service_bay`. |
| 8 | Equipment Filtering | Job requires `["Lift", "Alignment Rack"]`, Bay 1 has only `Lift`, Bay 2 has both | In `induct_shop`, `required_tags_set.issubset(bay_tags_set)` evaluated False for Bay 1, True for Bay 2. | Same. Bay 1 is omitted; only Bay 2 is returned as available. |
| 9 | Equipment Filtering | Job requires unknown/typo tag `["Hydraulic Crane"]` not present on any bay | Set containment evaluates False for all bays; returns `[]`. | Same. Slot generation gracefully indicates 0 available bays. |
| 10 | Site Migrations | Running `bench migrate` on existing bench site without `after_install` | In `d:\ism`, `after_migrate` was absent from `hooks.py`, so setup logic never executed on migrate. | Registering `after_migrate = "ism.setup.after_migrate"` in `hooks.py` ensures tags and roles are seeded. |

---

## 5. Caveats

1. **Role Existence Prerequisite**:
   - If `ISM Manager` is specified in `permissions` inside `service_bay.json` and `equipment_tag.json`, Frappe's `sync_permissions` requires the `Role` record to exist in `tabRole`. Seeding `ensure_ism_roles()` in `setup.py` prevents missing role warnings during site migration.
2. **Desk Access Scope**:
   - Per dispatch, permissions include `Desk access`. In Frappe, Desk access is governed by the `desk_access` flag on the `Role` DocType (e.g., `ISM Manager` with `desk_access: 1`). DocType permission dictionaries control CRUD, email, print, and export actions for that role.
3. **No Migration Execution (Read-Only Explorer)**:
   - As a specification miner, no modifications have been made to `ism/` files. The implementation plan below provides the exact file blueprints ready for immediate execution by the implementation agent.

---

## 6. Conclusion

The specification mining investigation confirms that `Service Bay`, `Equipment Tag`, and `Service Bay Equipment` provide the essential structural foundation for ISM's dual-resource scheduling system.

Adapting the schemas from `induct_shop` to `d:\ism` requires four specific adjustments:
1. Setting the module name strictly to `"ISM"` (uppercase).
2. Implementing controller-level integrity validations for duplicate child tags, whitespace sanitization, and referenced tag deletion protection.
3. Seeding the `ISM Manager` role with `desk_access: 1` and seeding the three standard default tags (`Lift`, `EV High Voltage`, `Alignment Rack`).
4. Registering `after_migrate = "ism.setup.after_migrate"` in `ism/hooks.py` to guarantee automated synchronization on `bench migrate`.

---

## 7. Concrete File-by-File Implementation Plan

The following blueprints provide the exact file contents to be created or modified in `d:\ism`:

### 7.1 `ism/ism/doctype/equipment_tag/equipment_tag.json`
```json
{
 "actions": [],
 "allow_rename": 1,
 "autoname": "field:tag_name",
 "creation": "2026-10-26 00:00:00.000000",
 "doctype": "DocType",
 "editable_grid": 1,
 "engine": "InnoDB",
 "field_order": [
  "tag_name",
  "description"
 ],
 "fields": [
  {
   "fieldname": "tag_name",
   "fieldtype": "Data",
   "in_list_view": 1,
   "label": "Tag Name",
   "reqd": 1,
   "unique": 1
  },
  {
   "fieldname": "description",
   "fieldtype": "Small Text",
   "label": "Description"
  }
 ],
 "links": [],
 "modified": "2026-10-26 00:00:00.000000",
 "modified_by": "Administrator",
 "module": "ISM",
 "name": "Equipment Tag",
 "naming_rule": "By fieldname",
 "owner": "Administrator",
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
  },
  {
   "email": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "Maintenance User"
  },
  {
   "email": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "Sales User"
  }
 ],
 "row_format": "Dynamic",
 "show_name_in_global_search": 1,
 "sort_field": "tag_name",
 "sort_order": "ASC",
 "states": [],
 "title_field": "tag_name",
 "track_changes": 1
}
```

### 7.2 `ism/ism/doctype/equipment_tag/equipment_tag.py`
```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class EquipmentTag(Document):
	def validate(self):
		if self.tag_name:
			self.tag_name = self.tag_name.strip()
		if not self.tag_name:
			frappe.throw(_("Tag Name cannot be empty."), title=_("Validation Error"))

	def on_trash(self):
		assigned_bays = frappe.get_all(
			"Service Bay Equipment",
			filters={"equipment_tag": self.name},
			pluck="parent",
		)
		if assigned_bays:
			unique_bays = sorted(set(assigned_bays))
			frappe.throw(
				_(
					"Cannot delete Equipment Tag '{0}' because it is assigned to Service Bay(s): {1}. "
					"Remove the tag from these bays before deleting."
				).format(self.name, ", ".join(unique_bays)),
				title=_("Tag in Use"),
			)
```

### 7.3 `ism/ism/doctype/service_bay_equipment/service_bay_equipment.json`
```json
{
 "actions": [],
 "creation": "2026-10-26 00:00:00.000000",
 "doctype": "DocType",
 "editable_grid": 1,
 "engine": "InnoDB",
 "field_order": [
  "equipment_tag"
 ],
 "fields": [
  {
   "fieldname": "equipment_tag",
   "fieldtype": "Link",
   "in_list_view": 1,
   "label": "Equipment Tag",
   "options": "Equipment Tag",
   "reqd": 1
  }
 ],
 "istable": 1,
 "links": [],
 "modified": "2026-10-26 00:00:00.000000",
 "modified_by": "Administrator",
 "module": "ISM",
 "name": "Service Bay Equipment",
 "owner": "Administrator",
 "permissions": [],
 "row_format": "Dynamic",
 "sort_field": "modified",
 "sort_order": "DESC",
 "states": []
}
```

### 7.4 `ism/ism/doctype/service_bay_equipment/service_bay_equipment.py`
```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ServiceBayEquipment(Document):
	pass
```

### 7.5 `ism/ism/doctype/service_bay/service_bay.json`
```json
{
 "actions": [],
 "allow_rename": 1,
 "autoname": "field:bay_name",
 "creation": "2026-10-26 00:00:00.000000",
 "doctype": "DocType",
 "editable_grid": 1,
 "engine": "InnoDB",
 "field_order": [
  "bay_name",
  "is_active",
  "description",
  "sb_equipment",
  "equipment"
 ],
 "fields": [
  {
   "fieldname": "bay_name",
   "fieldtype": "Data",
   "in_list_view": 1,
   "label": "Bay Name",
   "reqd": 1,
   "unique": 1
  },
  {
   "default": "1",
   "fieldname": "is_active",
   "fieldtype": "Check",
   "in_list_view": 1,
   "label": "Is Active"
  },
  {
   "fieldname": "description",
   "fieldtype": "Small Text",
   "label": "Description"
  },
  {
   "fieldname": "sb_equipment",
   "fieldtype": "Section Break",
   "label": "Equipment Tags"
  },
  {
   "fieldname": "equipment",
   "fieldtype": "Table",
   "label": "Equipment Tags",
   "options": "Service Bay Equipment"
  }
 ],
 "links": [],
 "modified": "2026-10-26 00:00:00.000000",
 "modified_by": "Administrator",
 "module": "ISM",
 "name": "Service Bay",
 "naming_rule": "By fieldname",
 "owner": "Administrator",
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
  },
  {
   "email": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "Maintenance User"
  },
  {
   "email": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "Sales User"
  }
 ],
 "quick_entry": 1,
 "row_format": "Dynamic",
 "show_name_in_global_search": 1,
 "sort_field": "bay_name",
 "sort_order": "ASC",
 "states": [],
 "title_field": "bay_name",
 "track_changes": 1
}
```

### 7.6 `ism/ism/doctype/service_bay/service_bay.py`
```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ServiceBay(Document):
	def validate(self):
		if self.bay_name:
			self.bay_name = self.bay_name.strip()
		if not self.bay_name:
			frappe.throw(_("Bay Name cannot be empty."), title=_("Validation Error"))
		self.validate_duplicate_tags()

	def validate_duplicate_tags(self):
		if not self.equipment:
			return
		seen_tags = set()
		for row in self.equipment:
			if not row.equipment_tag:
				continue
			if row.equipment_tag in seen_tags:
				frappe.throw(
					_("Duplicate equipment tag '{0}' at row #{1}.").format(row.equipment_tag, row.idx),
					title=_("Duplicate Tag"),
				)
			seen_tags.add(row.equipment_tag)

	def on_trash(self):
		if frappe.db.exists("DocType", "Schedule Entry"):
			active_entries = frappe.get_all(
				"Schedule Entry",
				filters={
					"service_bay": self.name,
					"status": ["in", ["Draft", "Scheduled", "Needs Review", "In Progress"]],
				},
				pluck="name",
				limit=5,
			)
			if active_entries:
				frappe.throw(
					_(
						"Cannot delete Service Bay '{0}' because it has active schedule entries ({1}). "
						"Reassign or cancel these entries before deleting."
					).format(self.name, ", ".join(active_entries)),
					title=_("Bay in Use"),
				)
```

### 7.7 `ism/ism/doctype/service_bay/test_service_bay.py`
```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from ism.setup import seed_equipment_tags


class TestServiceBay(FrappeTestCase):
	def setUp(self):
		seed_equipment_tags()
		self.bay_alpha = "Test Unit Bay Alpha"
		self.bay_beta = "Test Unit Bay Beta"
		self.cleanup()

	def tearDown(self):
		self.cleanup()

	def cleanup(self):
		for name in [self.bay_alpha, self.bay_beta]:
			if frappe.db.exists("Service Bay", name):
				frappe.delete_doc("Service Bay", name, force=True, ignore_permissions=True)

	def test_service_bay_creation_and_equipment_tags(self):
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
			"description": "Primary EV and alignment bay",
			"equipment": [
				{"equipment_tag": "Lift"},
				{"equipment_tag": "EV High Voltage"},
			],
		})
		bay.insert(ignore_permissions=True)

		self.assertTrue(frappe.db.exists("Service Bay", self.bay_alpha))
		loaded = frappe.get_doc("Service Bay", self.bay_alpha)
		self.assertEqual(len(loaded.equipment), 2)
		tags = [row.equipment_tag for row in loaded.equipment]
		self.assertIn("Lift", tags)
		self.assertIn("EV High Voltage", tags)

	def test_service_bay_duplicate_tag_rejection(self):
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
			"equipment": [
				{"equipment_tag": "Lift"},
				{"equipment_tag": "Lift"},
			],
		})
		with self.assertRaises(frappe.ValidationError):
			bay.insert(ignore_permissions=True)

	def test_service_bay_active_filtering(self):
		frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
		}).insert(ignore_permissions=True)

		frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_beta,
			"is_active": 0,
		}).insert(ignore_permissions=True)

		active_bays = frappe.get_all("Service Bay", filters={"is_active": 1}, pluck="name")
		self.assertIn(self.bay_alpha, active_bays)
		self.assertNotIn(self.bay_beta, active_bays)

	def test_equipment_tag_deletion_protection(self):
		test_tag_name = "Test Special Tool Tag"
		if not frappe.db.exists("Equipment Tag", test_tag_name):
			frappe.get_doc({
				"doctype": "Equipment Tag",
				"tag_name": test_tag_name,
				"description": "Temporary tag for deletion test",
			}).insert(ignore_permissions=True)

		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
			"equipment": [{"equipment_tag": test_tag_name}],
		}).insert(ignore_permissions=True)

		# Attempting to delete tag while assigned to bay should raise ValidationError
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Equipment Tag", test_tag_name, ignore_permissions=True)

		# Remove bay, now deletion succeeds
		frappe.delete_doc("Service Bay", self.bay_alpha, force=True, ignore_permissions=True)
		frappe.delete_doc("Equipment Tag", test_tag_name, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Equipment Tag", test_tag_name))
```

### 7.8 Seeding Additions to `ism/setup.py`
Add the following functions and update `after_migrate()` in `ism/setup.py`:

```python
DEFAULT_EQUIPMENT_TAGS = [
	{
		"tag_name": "Lift",
		"description": "Standard automotive lift (2-post / 4-post)",
	},
	{
		"tag_name": "EV High Voltage",
		"description": "High voltage isolation and battery service equipment",
	},
	{
		"tag_name": "Alignment Rack",
		"description": "3D optical wheel alignment rack and sensor system",
	},
]


def seed_equipment_tags():
	"""Idempotently seed standard default Equipment Tag records."""
	if not frappe.db.exists("DocType", "Equipment Tag"):
		return
	for tag in DEFAULT_EQUIPMENT_TAGS:
		if not frappe.db.exists("Equipment Tag", tag["tag_name"]):
			doc = frappe.get_doc({
				"doctype": "Equipment Tag",
				"tag_name": tag["tag_name"],
				"description": tag["description"],
			})
			doc.insert(ignore_permissions=True)


def ensure_ism_roles():
	"""Ensure standard ISM roles exist with appropriate Desk access."""
	roles = [
		{"role_name": "ISM Manager", "desk_access": 1},
	]
	for r in roles:
		if not frappe.db.exists("Role", r["role_name"]):
			try:
				doc = frappe.get_doc({
					"doctype": "Role",
					"role_name": r["role_name"],
					"desk_access": r["desk_access"],
				})
				doc.insert(ignore_permissions=True)
			except Exception as e:
				frappe.logger("ism").warning(f"Could not seed role {r['role_name']}: {e}")
```

Inside `after_migrate()`:
```python
def after_migrate():
	"""Run migrations and setup tasks for ISM."""
	ensure_ism_roles()
	cleanup_legacy_custom_fields()
	create_standard_inspection_template()
	setup_default_ism_settings()
	ensure_doctype_creation_enabled()
	setup_default_taxes_and_payments()
	setup_parts_item_group()
	setup_item_custom_fields()
	setup_procurement_custom_fields()
	backfill_service_package_models()
	seed_equipment_tags()
```

### 7.9 Hook Registration in `ism/hooks.py`
Line 95:
```python
after_install = "ism.setup.after_install"
after_migrate = "ism.setup.after_migrate"
```

---

## 8. Verification Method

Once the implementation agent executes the above plan, independent verification can be conducted via:

1. **Verify Schema Syntax & Linting**:
   ```powershell
   ruff check ism/ism/doctype/service_bay ism/ism/doctype/equipment_tag ism/ism/doctype/service_bay_equipment ism/setup.py ism/hooks.py
   ```
   *Expected outcome*: Zero lint errors; valid tabs and double quotes.

2. **Verify Database Migration & Seeding**:
   ```powershell
   bench --site <site> migrate
   ```
   *Expected outcome*:
   - Tables `tabService Bay`, `tabEquipment Tag`, and `tabService Bay Equipment` are created cleanly in MariaDB.
   - Default tags `Lift`, `EV High Voltage`, `Alignment Rack` are present in `tabEquipment Tag`.
   - Role `ISM Manager` exists with `desk_access = 1`.

3. **Verify Automated Unit Tests**:
   ```powershell
   bench run-tests --app ism --doctype "Service Bay"
   ```
   *Expected outcome*:
   - All tests in `test_service_bay.py` pass:
     - Bay creation and child table retrieval passes.
     - Adding duplicate tags raises `frappe.ValidationError`.
     - Active bay filtering isolates active from inactive bays.
     - Tag deletion protection prevents deleting tags referenced by service bays.
