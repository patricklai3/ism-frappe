# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.utils import flt

from ism.setup_tasks.seed import ensure_ism_roles
from ism.tests.base import ISMTestCase


_SETTINGS_FIELDS = (
	"cost_tolerance_percentage",
	"cost_tolerance_flat_amount",
)


class TestAuthAndRoles(ISMTestCase):
	"""Tests for Shared Field Contract schema, estimate lock tolerance, and advisor/technician role split."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		ensure_ism_roles()
		settings = frappe.get_single("ISM Settings")
		self._orig_settings = {f: getattr(settings, f, None) for f in _SETTINGS_FIELDS}
		self.customer = self.create_test_customer()
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		try:
			settings = frappe.get_single("ISM Settings")
			for field, value in self._orig_settings.items():
				settings.set(field, value)
			settings.save(ignore_permissions=True)
		finally:
			super().tearDown()

	def _set_tolerance(self, pct: float, flat: float):
		settings = frappe.get_single("ISM Settings")
		settings.cost_tolerance_percentage = pct
		settings.cost_tolerance_flat_amount = flat
		settings.save(ignore_permissions=True)

	def _create_test_user(self, prefix: str, role: str) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.com"
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": prefix,
			"last_name": "Tester",
			"enabled": 1,
			"user_type": "System User",
			"send_welcome_email": 0,
			"roles": [{"role": role}],
		})
		user.flags.ignore_password_policy = True
		user.insert(ignore_permissions=True)
		self.track(user)
		return email

	def _make_ro(self, services, status="Pending Authorization", approval_status="Pending Approval"):
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 45000,
			"status": status,
			"approval_status": approval_status,
			"services": services,
		})
		ro.insert()
		self.track(ro)
		return ro

	def test_shared_field_contract_schema_fields(self):
		"""All Shared Field Contract fields exist on their DocTypes with expected types and permlevels."""
		ros_meta = frappe.get_meta("Repair Order Service")
		auth_amt = ros_meta.get_field("authorized_amount")
		self.assertIsNotNone(auth_amt)
		self.assertEqual(auth_amt.fieldtype, "Currency")
		self.assertEqual(int(auth_amt.read_only or 0), 1)
		self.assertEqual(int(auth_amt.permlevel or 0), 1)

		row_disc = ros_meta.get_field("discount_amount")
		self.assertIsNotNone(row_disc)
		self.assertEqual(row_disc.fieldtype, "Currency")
		self.assertEqual(int(row_disc.permlevel or 0), 1)

		for fname in ("labor_rate", "labor_amount"):
			self.assertEqual(int(ros_meta.get_field(fname).permlevel or 0), 1)

		ro_meta = frappe.get_meta("Repair Order")
		for fname, ftype, ro_flag, pl in (
			("discount_amount", "Currency", 0, 1),
			("shop_supplies_amount", "Currency", 0, 1),
			("environmental_fee_amount", "Currency", 0, 1),
			("deposit_amount", "Currency", 1, 0),
			("tax_rate", "Percent", 0, 1),
			("taxes_and_charges", "Link", 0, 1),
			("approval_status", "Select", 0, 1),
		):
			field = ro_meta.get_field(fname)
			self.assertIsNotNone(field, f"Missing Repair Order field {fname}")
			self.assertEqual(field.fieldtype, ftype)
			self.assertEqual(int(field.read_only or 0), ro_flag)
			self.assertEqual(int(field.permlevel or 0), pl)

		settings_meta = frappe.get_meta("ISM Settings")
		for fname, ftype in (
			("shop_supplies_percentage", "Percent"),
			("shop_supplies_cap", "Currency"),
			("enable_ready_for_pickup_sms", "Check"),
		):
			field = settings_meta.get_field(fname)
			self.assertIsNotNone(field, f"Missing ISM Settings field {fname}")
			self.assertEqual(field.fieldtype, ftype)

	def test_authorize_jobs_snapshots_authorized_amount(self):
		"""authorize_jobs snapshots total_amount onto authorized_amount for both per-row and confirm_all."""
		ro = self._make_ro([
			{
				"service_title": "Brake Pad Service",
				"status": "Pending Approval",
				"labor_hours": 2.0,
				"labor_rate": 150.0,
			},
			{
				"service_title": "Cabin Filter",
				"status": "Pending Approval",
				"labor_hours": 0.5,
				"labor_rate": 100.0,
			},
		])

		ro.authorize_jobs(approvals={"1": "Approved"}, channel="In-Person Signature", signer_name="Jane Doe")
		ro.reload()
		self.assertEqual(ro.services[0].status, "Approved")
		self.assertEqual(flt(ro.services[0].authorized_amount, 2), 300.0)
		self.assertEqual(ro.services[1].status, "Pending Approval")
		self.assertEqual(flt(ro.services[1].authorized_amount, 2), 0.0)

		ro.authorize_jobs(confirm_all=1, channel="Advisor Manual", signer_name="Jane Doe")
		ro.reload()
		self.assertEqual(ro.services[1].status, "Approved")
		self.assertEqual(flt(ro.services[1].authorized_amount, 2), 50.0)
		self.assertEqual(flt(ro.services[0].authorized_amount, 2), 300.0)

	def test_cost_tolerance_within_and_beyond_limits(self):
		"""Approved row stays Approved within tolerance and demotes to Pending Approval when exceeded."""
		self._set_tolerance(pct=10.0, flat=50.0)

		# $200 authorized -> 10% = $20 (< $50 cap) -> max $220
		ro = self._make_ro([
			{
				"service_title": "Alignment",
				"status": "Pending Approval",
				"labor_hours": 2.0,
				"labor_rate": 100.0,
			}
		])
		ro.authorize_jobs(approvals={"1": "Approved"}, signer_name="Driver")
		ro.reload()
		self.assertEqual(flt(ro.services[0].authorized_amount, 2), 200.0)

		# Exactly at boundary ($220) -> remains Approved
		ro.services[0].labor_hours = 2.2
		ro.save()
		ro.reload()
		self.assertEqual(ro.services[0].status, "Approved")
		self.assertEqual(ro.approval_status, "Approved")

		# Exceeds $220 ($225) -> demoted to Pending Approval
		ro.services[0].labor_hours = 2.25
		ro.save()
		ro.reload()
		self.assertEqual(ro.services[0].status, "Pending Approval")
		self.assertEqual(ro.approval_status, "Pending Approval")

		# Re-authorizing updates authorized_amount snapshot to new $225
		ro.authorize_jobs(approvals={"1": "Approved"}, signer_name="Driver")
		ro.reload()
		self.assertEqual(ro.services[0].status, "Approved")
		self.assertEqual(flt(ro.services[0].authorized_amount, 2), 225.0)

	def test_cost_tolerance_flat_cap_and_partial_approval_recompute(self):
		"""Flat cap limits overrun on high-ticket jobs and multi-row demotion sets Partially Approved."""
		self._set_tolerance(pct=10.0, flat=50.0)

		# Row 1: $1000 (10% = $100, capped at $50 -> max $1050)
		# Row 2: $200 (stays within tolerance)
		ro = self._make_ro([
			{
				"service_title": "Battery Pack Service",
				"status": "Pending Approval",
				"labor_hours": 10.0,
				"labor_rate": 100.0,
			},
			{
				"service_title": "Tire Rotation",
				"status": "Pending Approval",
				"labor_hours": 2.0,
				"labor_rate": 100.0,
			},
		])
		ro.authorize_jobs(approvals={"1": "Approved", "2": "Approved"}, signer_name="Driver")
		ro.reload()
		self.assertEqual(ro.approval_status, "Approved")

		# Increase Row 1 to $1060 (within 10% of $1000, but exceeds $50 flat cap)
		ro.services[0].labor_hours = 10.6
		ro.status = "Authorized - In Progress"
		ro.save()
		ro.reload()

		self.assertEqual(ro.services[0].status, "Pending Approval")
		self.assertEqual(ro.services[1].status, "Approved")
		self.assertEqual(ro.approval_status, "Partially Approved")
		self.assertEqual(flt(ro.net_total, 2), 200.0)

	def test_cost_tolerance_edge_cases_legacy_and_zero_cost_warranty(self):
		"""Legacy approved rows (authorized_amount=0) stay Approved; $0 warranty jobs cannot jump cost."""
		self._set_tolerance(pct=10.0, flat=50.0)

		# Legacy / manual row created directly as Approved with authorized_amount=0
		legacy_ro = self._make_ro(
			[
				{
					"service_title": "Legacy Service",
					"status": "Approved",
					"labor_hours": 2.0,
					"labor_rate": 120.0,
				}
			],
			status="Authorized - In Progress",
			approval_status="Approved",
		)
		legacy_ro.reload()
		self.assertEqual(flt(legacy_ro.services[0].authorized_amount, 2), 0.0)
		legacy_ro.services[0].advisor_notes = "Completed check"
		legacy_ro.save()
		legacy_ro.reload()
		self.assertEqual(legacy_ro.services[0].status, "Approved")

		# Zero-cost warranty job authorized at $0
		warranty_ro = self._make_ro([
			{
				"service_title": "Warranty Recall Inspection",
				"status": "Pending Approval",
				"labor_hours": 1.0,
				"labor_rate": 0.0,
			}
		])
		warranty_ro.authorize_jobs(approvals={"1": "Approved"}, signer_name="Driver")
		warranty_ro.reload()
		self.assertEqual(warranty_ro.services[0].status, "Approved")
		self.assertEqual(flt(warranty_ro.services[0].authorized_amount, 2), 0.0)

		# Re-saving at $0 works cleanly (no ZeroDivisionError)
		warranty_ro.services[0].advisor_notes = "Zero-cost comp verified"
		warranty_ro.save()
		warranty_ro.reload()
		self.assertEqual(warranty_ro.services[0].status, "Approved")

		# Raising cost of $0-authorized warranty job to $100 demotes it for re-authorization
		warranty_ro.services[0].labor_rate = 100.0
		warranty_ro.save()
		warranty_ro.reload()
		self.assertEqual(warranty_ro.services[0].status, "Pending Approval")
		self.assertEqual(warranty_ro.approval_status, "Pending Approval")

	def test_roles_seeded_and_doctype_permissions_enforced(self):
		"""ISM Manager, ISM Service Advisor, and ISM Technician roles have expected DocType & permlevel access."""
		for role_name in ("ISM Manager", "ISM Service Advisor", "ISM Technician"):
			self.assertTrue(frappe.db.exists("Role", role_name), f"Role {role_name} must be seeded")
			self.assertEqual(int(frappe.db.get_value("Role", role_name, "desk_access") or 0), 1)

		advisor = self._create_test_user("test_ism_adv", "ISM Service Advisor")
		tech = self._create_test_user("test_ism_tech", "ISM Technician")
		mgr = self._create_test_user("test_ism_mgr", "ISM Manager")
		tech_emp = frappe.get_doc({
			"doctype": "Employee",
			"first_name": "Test ISM Tech",
			"employee_name": "Test ISM Tech",
			"status": "Active",
			"designation": "Technician",
			"company": self.get_manifest().company,
			"gender": "Male",
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2020-01-01",
			"user_id": tech,
		}).insert(ignore_permissions=True)
		self.track(tech_emp)

		# Repair Order DocType-level permissions
		self.assertTrue(frappe.has_permission("Repair Order", "create", user=advisor))
		self.assertTrue(frappe.has_permission("Repair Order", "write", user=advisor))
		self.assertFalse(frappe.has_permission("Repair Order", "delete", user=advisor))

		self.assertTrue(frappe.has_permission("Repair Order", "read", user=tech))
		self.assertTrue(frappe.has_permission("Repair Order", "write", user=tech))
		self.assertFalse(frappe.has_permission("Repair Order", "create", user=tech))
		self.assertFalse(frappe.has_permission("Repair Order", "delete", user=tech))

		# ISM Settings: Manager can write, Advisor & Technician are read-only
		self.assertTrue(frappe.has_permission("ISM Settings", "write", user=mgr))
		self.assertTrue(frappe.has_permission("ISM Settings", "read", user=advisor))
		self.assertFalse(frappe.has_permission("ISM Settings", "write", user=advisor))
		self.assertTrue(frappe.has_permission("ISM Settings", "read", user=tech))
		self.assertFalse(frappe.has_permission("ISM Settings", "write", user=tech))

		# Service Package: Advisor can write/create, Technician is read-only
		self.assertTrue(frappe.has_permission("Service Package", "create", user=advisor))
		self.assertTrue(frappe.has_permission("Service Package", "read", user=tech))
		self.assertFalse(frappe.has_permission("Service Package", "write", user=tech))
		self.assertFalse(frappe.has_permission("Service Package", "create", user=tech))

		# Repair Vehicle: Advisor can create/write, Technician can read/write (not create)
		self.assertTrue(frappe.has_permission("Repair Vehicle", "create", user=advisor))
		self.assertTrue(frappe.has_permission("Repair Vehicle", "write", user=tech))
		self.assertFalse(frappe.has_permission("Repair Vehicle", "create", user=tech))

		# Repair Order permlevel 1 access check
		ro = self._make_ro(
			[
				{
					"service_title": "Brake Inspection",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 150.0,
				}
			],
			status="Authorized - In Progress",
			approval_status="Approved",
		)
		self.create_test_schedule_entry(
			repair_order=ro.name,
			assigned_technician=tech_emp.name,
			status="Confirmed",
		)

		frappe.set_user(advisor)
		self.assertIn(1, ro.get_permlevel_access("read"))
		self.assertIn(1, ro.get_permlevel_access("write"))

		frappe.set_user(mgr)
		self.assertIn(1, ro.get_permlevel_access("read"))
		self.assertIn(1, ro.get_permlevel_access("write"))

		frappe.set_user(tech)
		self.assertIn(1, ro.get_permlevel_access("read"))
		self.assertNotIn(1, ro.get_permlevel_access("write"))

		# Technician can perform bay execution: pick up vehicle, log time, complete service, update inspection
		ro_tech = frappe.get_doc("Repair Order", ro.name)
		ro_tech.pick_up_vehicle()
		ro_tech.reload()
		self.assertEqual(ro_tech.vehicle_location, "In Bay / Being Serviced")

		ro_tech.append("inspection_items", {
			"inspection_description": "Front Left Tire",
			"status": "Pass",
			"notes": "6/32 tread",
		})
		ro_tech.save()
		ro_tech.toggle_service_completion(service_row_id=ro_tech.services[0].name, is_completed=1)
		ro_tech.reload()
		self.assertEqual(int(ro_tech.services[0].is_completed), 1)

		# Technician cannot delete Repair Order or write permlevel 1 fields
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Repair Order", ro.name)

		for fname in (
			"discount_amount",
			"shop_supplies_amount",
			"environmental_fee_amount",
			"tax_rate",
			"taxes_and_charges",
			"approval_status",
		):
			self.assertTrue(ro_tech.has_permlevel_access_to(fname, permission_type="read"))
			self.assertFalse(ro_tech.has_permlevel_access_to(fname, permission_type="write"))

		svc_row = ro_tech.services[0]
		for fname in ("labor_rate", "labor_amount", "discount_amount", "authorized_amount"):
			self.assertTrue(ro_tech.has_permlevel_access_to(fname, df=svc_row.meta.get_field(fname), permission_type="read"))
			self.assertFalse(ro_tech.has_permlevel_access_to(fname, df=svc_row.meta.get_field(fname), permission_type="write"))

		frappe.set_user(advisor)
		ro_adv = frappe.get_doc("Repair Order", ro.name)
		for fname in ("discount_amount", "tax_rate", "taxes_and_charges", "approval_status"):
			self.assertTrue(ro_adv.has_permlevel_access_to(fname, permission_type="write"))
		for fname in ("labor_rate", "labor_amount", "discount_amount"):
			self.assertTrue(ro_adv.has_permlevel_access_to(fname, df=ro_adv.services[0].meta.get_field(fname), permission_type="write"))
