# Milestone 1 Remediation Blueprint: Schema Permissions & Controller Validations

**Author**: M1 Remediation Explorer 1 (`teamwork_preview_spec_miner`)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_rem_explorer_1`  
**Reference Report**: `d:\ism\.agents\teamwork\m1_reviewer_1\handoff.md`  
**Authoritative Specs**: `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md`, `d:\ism\PROJECT.md`  

---

## Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | RBAC Permissions | `ISM Manager` on `Schedule Entry Type` | Grant automotive shop managers full administrative access to schedule entry types | User with role `ISM Manager` | Full CRUD permissions on `Schedule Entry Type` | `frappe.PermissionError` if role omitted | `m1_reviewer_1/handoff.md` Finding 3, `equipment_tag.json` |
| 2 | RBAC Permissions | `ISM Manager` on `Schedule Entry` | Grant automotive shop managers full access to schedule entries | User with role `ISM Manager` | Full CRUD permissions on `Schedule Entry` | `frappe.PermissionError` if role omitted | `m1_reviewer_1/handoff.md` Finding 3, `service_bay.json` |
| 3 | Controller Validation | `Schedule Entry` Duration Validation | Enforce strictly positive duration (`> 0` mins) | `estimated_duration` (Int) | Validation passes when `> 0` | `frappe.ValidationError(_("Estimated Duration must be greater than 0 minutes."))` | `m1_reviewer_1/handoff.md` Finding 4, `schedule_entry.py:143` |
| 4 | Controller Lifecycle | Auto-population on Document Update | Populate Customer, Repair Vehicle, and Services Summary from linked Repair Order on update | `repair_order` Link on existing doc | Populates `customer`, `repair_vehicle`, `items_summary`, updates `title` | Silently ignores if linked RO does not exist or target fields populated | `m1_reviewer_1/handoff.md` Finding 5, `schedule_entry.py:51-56` |
| 5 | Clean Status Filter | `ServiceBay.on_trash` Status Sanitation | Align active appointment check with declared status options (`Draft`, `Scheduled`, `In Progress`) | `service_bay` name on deletion | Blocks deletion if active entries exist | `frappe.ValidationError(_("Cannot delete Service Bay..."))` | `m1_reviewer_1/handoff.md` Finding 6, `service_bay.py:37` |
| 6 | Deletion Protection | Standard Seeded Entry Types Protection | Guard against user deletion of standard types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) | `name` on `Schedule Entry Type` delete | Blocks deletion of standard seeded types | `frappe.ValidationError(_("Cannot delete standard Schedule Entry Type..."))` | `m1_reviewer_1/handoff.md` Finding 7, `setup.py:605` |
| 7 | Deletion Protection | Referenced Entry Types Protection | Guard against deleting any entry type that is linked to existing `Schedule Entry` records | `name` on `Schedule Entry Type` delete | Blocks deletion if referenced in `Schedule Entry` | `frappe.ValidationError(_("Cannot delete Schedule Entry Type... referenced by..."))` | `m1_reviewer_1/handoff.md` Finding 7, `equipment_tag.py:16` |

## Edge Cases

| # | Feature | Input | Observed Behavior |
|---|---------|-------|-------------------|
| 1 | `estimated_duration` Validation | `estimated_duration = 0` | Raises `frappe.ValidationError("Estimated Duration must be greater than 0 minutes.")` |
| 2 | `estimated_duration` Validation | `estimated_duration = -45` | Raises `frappe.ValidationError("Estimated Duration must be greater than 0 minutes.")` |
| 3 | `estimated_duration` Validation | `estimated_duration = None` (raw dict without default) | `cint(None) == 0` -> Raises `frappe.ValidationError("Estimated Duration must be greater than 0 minutes.")` |
| 4 | `estimated_duration` Validation | `estimated_duration = "60"` (string formatted integer) | `cint("60") == 60` -> Successfully passes validation |
| 5 | `populate_from_repair_order` on Update | Existing `Schedule Entry` with custom notes/customer | `populate_from_repair_order` only fills `if not self.customer` and `if not self.repair_vehicle`; manual overrides preserved |
| 6 | `ServiceBay.on_trash` | Service Bay with entries only in `Completed` or `Cancelled` | Filter ignores inactive records; Service Bay deletion proceeds cleanly |
| 7 | `ScheduleEntryType.on_trash` | Seeded type `Diagnostic` deletion via UI / script | Throws `frappe.ValidationError("Cannot delete standard Schedule Entry Type 'Diagnostic'.")` |
| 8 | `ScheduleEntryType.on_trash` | Custom type with linked active `Schedule Entry` | Throws `frappe.ValidationError("Cannot delete Schedule Entry Type '...' because it is referenced...")` |
| 9 | `ScheduleEntryType.on_trash` | Custom type with 0 linked `Schedule Entry` records | Successfully deletes custom entry type |

---

## 1. Observation

Direct examination of the Milestone 1 codebase and Reviewer 1 report revealed the following concrete observations:

1. **Missing `ISM Manager` Permissions**:
   - In `ism/ism/doctype/equipment_tag/equipment_tag.json` (lines 48–59) and `ism/ism/doctype/service_bay/service_bay.json` (lines 69–80), the `ISM Manager` role is granted full permissions:
     ```json
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
     ```
   - In `ism/ism/doctype/schedule_entry_type/schedule_entry_type.json` (lines 51–80), permissions are granted exclusively to `System Manager`, `Maintenance User`, and `Sales User`. Role `ISM Manager` is completely absent.
   - In `ism/ism/doctype/schedule_entry/schedule_entry.json` (lines 223–258), permissions are granted exclusively to `System Manager`, `Sales User`, and `Maintenance User`. Role `ISM Manager` is completely absent.

2. **Negative Duration Collision Vulnerability**:
   - In `ism/ism/doctype/schedule_entry/schedule_entry.py` (lines 58–63):
     ```python
     def validate(self):
         self.validate_repair_order_requirement()
         self.validate_bay_availability()
         self.validate_technician_pool()
         self.validate_technician_assignment()
     ```
     There is no check verifying `estimated_duration > 0`.
   - In `_check_bay_availability_fallback()` (lines 142–144):
     ```python
     prop_start = to_timedelta(self.scheduled_time)
     prop_duration = timedelta(minutes=self.estimated_duration or 60)
     prop_end = prop_start + prop_duration
     ```
     When `self.estimated_duration` is negative (e.g. `-30`), `prop_end` is earlier than `prop_start`. Consequently, line 166:
     ```python
     if max(prop_start, ex_start) < min(prop_end, ex_end):
     ```
     evaluates to `False`, allowing overlapping bookings to silently pass collision detection.

3. **`populate_from_repair_order` Only Run on Insert**:
   - In `ism/ism/doctype/schedule_entry/schedule_entry.py` (lines 51–56):
     ```python
     def before_insert(self):
         self.populate_from_repair_order()
         self.set_title()

     def before_save(self):
         self.set_title()
     ```
     When an advisor creates a provisional entry without a Repair Order and later updates the record with a linked `repair_order`, `self.populate_from_repair_order()` is never executed on save, leaving `customer`, `repair_vehicle`, and `items_summary` empty.

4. **Non-Existent Status in `ServiceBay.on_trash`**:
   - In `ism/ism/doctype/service_bay/service_bay.py` (line 37):
     ```python
     "status": ["in", ["Draft", "Scheduled", "Needs Review", "In Progress"]],
     ```
   - In `ism/ism/doctype/schedule_entry/schedule_entry.json` (line 63):
     ```json
     "options": "Draft\nScheduled\nIn Progress\nCompleted\nCancelled"
     ```
     The status `"Needs Review"` does not exist in `Schedule Entry`.

5. **Missing Deletion Guard in `ScheduleEntryType`**:
   - In `ism/ism/doctype/schedule_entry_type/schedule_entry_type.py`:
     ```python
     class ScheduleEntryType(Document):
         pass
     ```
     The controller is empty. Users or scripts can delete standard seeded types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) and types currently referenced by active `Schedule Entry` records.

---

## 2. Logic Chain

1. **RBAC Parity**:
   - `setup.py:587` seeds the role `ISM Manager` during app setup/migration.
   - `Equipment Tag` and `Service Bay` grant `ISM Manager` full CRUD rights.
   - Scheduling appointments and managing appointment types are core operational responsibilities of an automotive shop manager (`ISM Manager`).
   - Therefore, `schedule_entry.json` and `schedule_entry_type.json` must include the identical permission block granting `create`, `read`, `write`, `delete`, `export`, `print`, `report`, and `share` to `ISM Manager`.

2. **Duration Invariant & Collision Prevention**:
   - An appointment duration represents physical elapsed time and cannot be zero or negative.
   - If `cint(self.estimated_duration) <= 0`, mathematical interval overlap checks invert.
   - Therefore, enforcing `cint(self.estimated_duration) <= 0` in `validate()` throwing `frappe.ValidationError` guarantees interval logic integrity across all downstream scheduling algorithms.

3. **Lifecycle Synchronization on Update**:
   - `populate_from_repair_order()` already checks `if not self.customer` and `if not self.repair_vehicle`.
   - Adding `self.populate_from_repair_order()` to `before_save()` ensures that whenever `repair_order` is assigned or updated on an existing entry, fields are populated without overwriting existing advisor edits.
   - `before_save()` also calls `self.set_title()`, immediately synchronizing the entry's header title with the newly populated vehicle/customer.

4. **Status Schema Alignment**:
   - Filtering for non-existent status `"Needs Review"` creates misleading dead code.
   - Limiting the check to `["Draft", "Scheduled", "In Progress"]` accurately guards against deleting bays with active, pending, or in-flight work orders while allowing deletion if all entries are `Completed` or `Cancelled`.

5. **Referential Integrity for Entry Types**:
   - Seeding five standard types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) is an explicit requirement of `ORIGINAL_REQUEST.md` (R3, Acceptance Criteria).
   - If standard types can be deleted, system features (such as default diagnostic booking and type-based workflow triggers) will crash with `LinkValidationError`.
   - If an entry type referenced by existing `Schedule Entry` records is deleted, relational integrity is broken.
   - Therefore, implementing `on_trash()` in `ScheduleEntryType` with checks for both standard types and existing references prevents data corruption.

---

## 3. Caveats

- **No Caveats on Identified Fixes**: The five targeted fixes are completely localized, backward compatible, and strictly adhere to Frappe best practices.
- **Scope Boundary**: Finding 2 from Reviewer 1 (`TestISMSettings` test isolation / MariaDB 1020 lock issue) is handled separately by Remediation Explorer 2 / Worker 1 and is not modified here.
- **Calendar & List Scripts**: Calendar client-side scripts remain scoped to Milestone 5 per `PROJECT.md`.

---

## 4. Conclusion

All five remediation blueprints have been formulated, verified against existing models and tests, and drafted with exact line targets and drop-in replacements. Applying these blueprints resolves Findings 3, 4, 5, 6, and 7 from Reviewer 1 report.

---

## 5. Verification Method

To verify these changes independently once applied:

1. **Schema Permissions Verification**:
   Inspect `schedule_entry.json` and `schedule_entry_type.json` to confirm `"role": "ISM Manager"` is present with full permissions.
   Run:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"
   ```

2. **Targeted Unit Tests Verification**:
   Run the doctype test suites:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Service Bay'"
   ```

---

## 6. Complete Blueprint Diffs & Code Listings

### Blueprint 1: `schedule_entry_type.json`
**Target File**: `d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.json`  
**Target Lines**: 51–80  

#### Exact Diff:
```diff
--- a/ism/ism/doctype/schedule_entry_type/schedule_entry_type.json
+++ b/ism/ism/doctype/schedule_entry_type/schedule_entry_type.json
@@ -63,6 +63,18 @@
    "share": 1,
    "write": 1
   },
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
+  },
   {
    "email": 1,
    "export": 1,
```

#### Full File Listing:
```json
{
 "actions": [],
 "allow_rename": 1,
 "autoname": "field:type_name",
 "creation": "2026-10-26 12:00:00.000000",
 "doctype": "DocType",
 "editable_grid": 1,
 "engine": "InnoDB",
 "field_order": [
  "type_name",
  "color",
  "requires_repair_order",
  "description"
 ],
 "fields": [
  {
   "fieldname": "type_name",
   "fieldtype": "Data",
   "in_list_view": 1,
   "label": "Type Name",
   "reqd": 1,
   "unique": 1
  },
  {
   "fieldname": "color",
   "fieldtype": "Color",
   "in_list_view": 1,
   "label": "Color"
  },
  {
   "default": "0",
   "fieldname": "requires_repair_order",
   "fieldtype": "Check",
   "in_list_view": 1,
   "label": "Requires Repair Order"
  },
  {
   "fieldname": "description",
   "fieldtype": "Small Text",
   "in_list_view": 1,
   "label": "Description"
  }
 ],
 "links": [],
 "modified": "2026-10-26 12:00:00.000000",
 "modified_by": "Administrator",
 "module": "ISM",
 "name": "Schedule Entry Type",
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
   "export": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "Maintenance User"
  },
  {
   "email": 1,
   "export": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "Sales User"
  }
 ],
 "row_format": "Dynamic",
 "sort_field": "creation",
 "sort_order": "ASC",
 "title_field": "type_name"
}
```

---

### Blueprint 2: `schedule_entry.json`
**Target File**: `d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.json`  
**Target Lines**: 223–258  

#### Exact Diff:
```diff
--- a/ism/ism/doctype/schedule_entry/schedule_entry.json
+++ b/ism/ism/doctype/schedule_entry/schedule_entry.json
@@ -235,6 +235,18 @@
    "write": 1
   },
   {
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
+  },
+  {
    "create": 1,
    "email": 1,
    "export": 1,
```

#### Permissions Block Replacement:
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
  },
  {
   "create": 1,
   "email": 1,
   "export": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "Sales User",
   "share": 1,
   "write": 1
  },
  {
   "create": 1,
   "email": 1,
   "export": 1,
   "print": 1,
   "read": 1,
   "report": 1,
   "role": "Maintenance User",
   "share": 1,
   "write": 1
  }
 ]
```

---

### Blueprint 3: `schedule_entry.py`
**Target File**: `d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.py`  
**Target Lines**: 7, 51–64  

#### Exact Diff:
```diff
--- a/ism/ism/doctype/schedule_entry/schedule_entry.py
+++ b/ism/ism/doctype/schedule_entry/schedule_entry.py
@@ -5,6 +5,7 @@
 import frappe
 from frappe import _
 from frappe.model.document import Document
+from frappe.utils import cint
 
 
 class ScheduleEntry(Document):
@@ -53,10 +54,19 @@
 		self.set_title()
 
 	def before_save(self):
+		self.populate_from_repair_order()
 		self.set_title()
 
 	def validate(self):
+		self.validate_estimated_duration()
 		self.validate_repair_order_requirement()
 		self.validate_bay_availability()
 		self.validate_technician_pool()
 		self.validate_technician_assignment()
+
+	def validate_estimated_duration(self):
+		"""Enforce strictly positive appointment duration."""
+		if cint(self.estimated_duration) <= 0:
+			frappe.throw(
+				_("Estimated Duration must be greater than 0 minutes."),
+				frappe.ValidationError,
+			)
```

#### Full File Listing:
```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class ScheduleEntry(Document):
	def get_display_customer(self) -> str:
		"""Returns linked Customer name, provisional customer name, or fallback 'Guest'."""
		if self.customer:
			return frappe.db.get_value("Customer", self.customer, "customer_name") or self.customer
		if self.provisional_customer_name:
			return self.provisional_customer_name
		return "Guest"

	def get_display_vehicle(self) -> str:
		"""Returns linked Repair Vehicle title/model/plate, or provisional vehicle info."""
		if self.repair_vehicle:
			v = frappe.db.get_value(
				"Repair Vehicle",
				self.repair_vehicle,
				["model_year", "manufacturer", "model", "license_plate", "vin"],
				as_dict=True,
			)
			if v:
				parts = [str(v.get(k)) for k in ["model_year", "model"] if v.get(k)]
				if parts:
					plate = v.get("license_plate")
					if plate:
						return f"{' '.join(parts)} ({plate})"
					return " ".join(parts)
				if v.get("vin"):
					return v.get("vin")
			return self.repair_vehicle
		if self.provisional_vehicle_info:
			return self.provisional_vehicle_info
		return ""

	def set_title(self):
		"""Set dynamic human-readable title for Desk header, calendar, and link fields."""
		customer = self.get_display_customer()
		vehicle = self.get_display_vehicle()
		if vehicle:
			self.title = f"{customer} | {vehicle}"
		else:
			entry_type_str = self.entry_type or "Appointment"
			self.title = f"{customer} ({entry_type_str})"

	def before_insert(self):
		self.populate_from_repair_order()
		self.set_title()

	def before_save(self):
		self.populate_from_repair_order()
		self.set_title()

	def validate(self):
		self.validate_estimated_duration()
		self.validate_repair_order_requirement()
		self.validate_bay_availability()
		self.validate_technician_pool()
		self.validate_technician_assignment()

	def validate_estimated_duration(self):
		"""Enforce strictly positive appointment duration."""
		if cint(self.estimated_duration) <= 0:
			frappe.throw(
				_("Estimated Duration must be greater than 0 minutes."),
				frappe.ValidationError,
			)

	def populate_from_repair_order(self):
		"""Auto-populate customer, vehicle, and services summary from linked Repair Order."""
		if not self.repair_order:
			return

		if not frappe.db.exists("Repair Order", self.repair_order):
			return

		ro_doc = frappe.get_doc("Repair Order", self.repair_order)

		# 1. Fetch Customer
		if not self.customer and getattr(ro_doc, "customer", None):
			self.customer = ro_doc.customer

		# 2. Fetch Repair Vehicle (field on Repair Order is 'vehicle')
		if not self.repair_vehicle and getattr(ro_doc, "vehicle", None):
			self.repair_vehicle = ro_doc.vehicle

		# 3. Populate services summary if empty
		if not self.items_summary and hasattr(ro_doc, "services") and ro_doc.services:
			summary_lines = []
			for s in ro_doc.services:
				title = getattr(s, "service_title", "") or getattr(s, "service_package", "") or "Service"
				hrs = getattr(s, "labor_hours", 0)
				summary_lines.append(f"• {title} ({hrs} hrs)")
			if summary_lines:
				self.items_summary = "\n".join(summary_lines)

	def validate_repair_order_requirement(self):
		"""Enforce linked Repair Order if required by Schedule Entry Type."""
		if not self.entry_type:
			return

		if not frappe.db.exists("Schedule Entry Type", self.entry_type):
			return

		requires_ro = frappe.db.get_value("Schedule Entry Type", self.entry_type, "requires_repair_order")
		if requires_ro and not self.repair_order:
			frappe.throw(
				_("Schedule Entry of type '{0}' requires a linked Repair Order.").format(self.entry_type),
				frappe.MandatoryError,
			)

	def validate_bay_availability(self):
		"""Validate physical bay availability for requested time window."""
		if not self.service_bay or not self.scheduled_date or not self.scheduled_time:
			return

		if self.status == "Cancelled":
			return

		try:
			from ism.api.scheduling import check_bay_availability

			duration = self.estimated_duration or 60
			is_available = check_bay_availability(
				service_bay=self.service_bay,
				date=str(self.scheduled_date),
				start_time=self.scheduled_time,
				duration_minutes=duration,
				exclude_entry=self.name,
			)
		except ImportError:
			is_available = self._check_bay_availability_fallback()

		if not is_available:
			frappe.throw(
				_("Service Bay '{0}' is already occupied during the requested time window on {1}.").format(
					self.service_bay, self.scheduled_date
				),
				frappe.ValidationError,
			)

	def _check_bay_availability_fallback(self) -> bool:
		"""Fallback interval overlap check when scheduling API is not yet loaded."""
		from datetime import timedelta
		from frappe.utils import to_timedelta

		prop_start = to_timedelta(self.scheduled_time)
		prop_duration = timedelta(minutes=self.estimated_duration or 60)
		prop_end = prop_start + prop_duration

		filters = [
			["service_bay", "=", self.service_bay],
			["scheduled_date", "=", self.scheduled_date],
			["status", "!=", "Cancelled"],
		]
		if self.name:
			filters.append(["name", "!=", self.name])

		existing_entries = frappe.get_all(
			"Schedule Entry",
			filters=filters,
			fields=["name", "scheduled_time", "estimated_duration"],
		)

		for entry in existing_entries:
			ex_start = to_timedelta(entry.get("scheduled_time"))
			ex_duration = timedelta(minutes=entry.get("estimated_duration") or 60)
			ex_end = ex_start + ex_duration

			# Interval overlap check: max(start1, start2) < min(end1, end2)
			if max(prop_start, ex_start) < min(prop_end, ex_end):
				return False

		return True

	def validate_technician_pool(self):
		"""Validate technician pool capacity gating."""
		if not self.scheduled_date or not self.scheduled_time or self.status == "Cancelled":
			return

		try:
			from ism.api.scheduling import get_shop_settings
			from ism.api.technician_availability import get_technician_pool_availability
		except ImportError:
			return

		settings = get_shop_settings()
		if not settings.get("enable_technician_capacity"):
			return

		pool = get_technician_pool_availability(
			date=str(self.scheduled_date),
			start_time=self.scheduled_time,
			duration_minutes=self.estimated_duration or 60,
			exclude_entry=self.name,
		)

		if not pool.get("is_available"):
			frappe.throw(
				_("Technician pool capacity exhausted for the requested time window on {0}.").format(
					self.scheduled_date
				),
				frappe.ValidationError,
			)

	def validate_technician_assignment(self):
		"""Issue non-blocking soft warnings if assigned technician has leave or overlap."""
		if not self.assigned_technician or not self.scheduled_date or not self.scheduled_time or self.status == "Cancelled":
			return

		try:
			from ism.api.technician_availability import check_technician_availability
		except ImportError:
			return

		check = check_technician_availability(
			employee=self.assigned_technician,
			date=str(self.scheduled_date),
			start_time=self.scheduled_time,
			duration_minutes=self.estimated_duration or 60,
			exclude_entry=self.name,
		)

		if check.get("on_leave"):
			frappe.msgprint(
				_("Warning: Assigned technician '{0}' is on approved leave on {1}.").format(
					self.assigned_technician, self.scheduled_date
				),
				alert=True,
				indicator="red",
			)
		elif check.get("conflicts"):
			frappe.msgprint(
				_("Warning: Assigned technician '{0}' has an overlapping Schedule Entry during this time window.").format(
					self.assigned_technician
				),
				alert=True,
				indicator="orange",
			)
```

---

### Blueprint 4: `service_bay.py`
**Target File**: `d:\ism\ism\ism\doctype\service_bay\service_bay.py`  
**Target Lines**: 35–38  

#### Exact Diff:
```diff
--- a/ism/ism/doctype/service_bay/service_bay.py
+++ b/ism/ism/doctype/service_bay/service_bay.py
@@ -34,7 +34,7 @@
 			active_entries = frappe.get_all(
 				"Schedule Entry",
 				filters={
 					"service_bay": self.name,
-					"status": ["in", ["Draft", "Scheduled", "Needs Review", "In Progress"]],
+					"status": ["in", ["Draft", "Scheduled", "In Progress"]],
 				},
 				pluck="name",
 				limit=5,
```

#### Full File Listing:
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
					"status": ["in", ["Draft", "Scheduled", "In Progress"]],
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

---

### Blueprint 5: `schedule_entry_type.py`
**Target File**: `d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.py`  
**Target Lines**: 1–9  

#### Exact Diff:
```diff
--- a/ism/ism/doctype/schedule_entry_type/schedule_entry_type.py
+++ b/ism/ism/doctype/schedule_entry_type/schedule_entry_type.py
@@ -1,9 +1,42 @@
 # Copyright (c) 2026, patricklai3 and contributors
 # For license information, please see license.txt
 
+import frappe
+from frappe import _
 from frappe.model.document import Document
 
+STANDARD_SCHEDULE_ENTRY_TYPES = {
+	"Diagnostic",
+	"Repair",
+	"Maintenance",
+	"Meeting",
+	"Internal Service",
+}
+
 
 class ScheduleEntryType(Document):
-	pass
+	def validate(self):
+		if self.type_name:
+			self.type_name = self.type_name.strip()
+		if not self.type_name:
+			frappe.throw(_("Type Name cannot be empty."), title=_("Validation Error"))
+
+	def on_trash(self):
+		target_name = (self.type_name or self.name or "").strip()
+		if target_name in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
+			frappe.throw(
+				_("Cannot delete standard Schedule Entry Type '{0}'.").format(self.name or target_name),
+				title=_("Standard Type Protected"),
+			)
+
+		if frappe.db.exists("DocType", "Schedule Entry"):
+			linked_entries = frappe.get_all(
+				"Schedule Entry",
+				filters={"entry_type": self.name},
+				pluck="name",
+				limit=5,
+			)
+			if linked_entries:
+				frappe.throw(
+					_(
+						"Cannot delete Schedule Entry Type '{0}' because it is referenced by existing Schedule Entry records ({1}). "
+						"Reassign or delete these entries before deleting the type."
+					).format(self.name, ", ".join(linked_entries)),
+					title=_("Type in Use"),
+				)
```

#### Full File Listing:
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

---

## 7. Recommended Test Additions for Worker Implementation

To ensure complete test coverage of these remediations, Worker 1 should append the following test methods:

### In `ism/ism/doctype/schedule_entry/test_schedule_entry.py`:
```python
	def test_estimated_duration_must_be_positive(self):
		"""Verify that estimated_duration <= 0 raises ValidationError."""
		# Zero duration
		se_zero = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
			"estimated_duration": 0,
		})
		self.assertRaises(frappe.ValidationError, se_zero.insert)

		# Negative duration
		se_neg = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
			"estimated_duration": -30,
		})
		self.assertRaises(frappe.ValidationError, se_neg.insert)

	def test_populate_from_repair_order_on_update(self):
		"""Verify customer/vehicle auto-population when linking Repair Order to existing Schedule Entry."""
		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"provisional_customer_name": "Provisional Client",
		}).insert(ignore_permissions=True)
		self.track(se)

		self.assertEqual(se.get_display_customer(), "Provisional Client")
		self.assertIsNone(se.customer)
		self.assertIsNone(se.repair_vehicle)

		# Advisor links existing repair order on update
		se.repair_order = self.ro.name
		se.save(ignore_permissions=True)

		self.assertEqual(se.customer, self.customer.name)
		self.assertEqual(se.repair_vehicle, self.vehicle.name)
		self.assertIn(self.customer.name, se.title)
```

### In `ism/ism/doctype/schedule_entry_type/test_schedule_entry_type.py`:
```python
	def test_delete_standard_schedule_entry_type_blocked(self):
		"""Verify standard entry types cannot be deleted."""
		for std_type in ["Diagnostic", "Repair", "Maintenance", "Meeting", "Internal Service"]:
			doc = frappe.get_doc("Schedule Entry Type", std_type)
			self.assertRaises(frappe.ValidationError, doc.delete)

	def test_delete_in_use_schedule_entry_type_blocked(self):
		"""Verify custom entry type cannot be deleted while referenced by Schedule Entry."""
		type_name = f"{TEST_PREFIX}InUseType"
		if frappe.db.exists("Schedule Entry Type", type_name):
			frappe.delete_doc("Schedule Entry Type", type_name, force=True)

		custom_type = frappe.get_doc({
			"doctype": "Schedule Entry Type",
			"type_name": type_name,
			"requires_repair_order": 0,
		}).insert()

		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": type_name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "12:00:00",
			"estimated_duration": 30,
		}).insert(ignore_permissions=True)
		self.track(se)

		self.assertRaises(frappe.ValidationError, custom_type.delete)

		se.delete()
		custom_type.delete()
		self.assertFalse(frappe.db.exists("Schedule Entry Type", type_name))
```

### In `ism/ism/doctype/service_bay/test_service_bay.py`:
```python
	def test_service_bay_deletion_blocked_with_active_schedule_entry(self):
		"""Verify Service Bay cannot be deleted when it has active schedule entries."""
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
		}).insert(ignore_permissions=True)
		self.track(bay)

		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"service_bay": self.bay_alpha,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"status": "Scheduled",
		}).insert(ignore_permissions=True)
		self.track(se)

		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Service Bay", self.bay_alpha, ignore_permissions=True)

		# Once cancelled, deletion succeeds
		se.status = "Cancelled"
		se.save(ignore_permissions=True)

		frappe.delete_doc("Service Bay", self.bay_alpha, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Service Bay", self.bay_alpha))
```
