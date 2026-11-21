"""End-to-end tests for Wave 3B comms caller wiring (approval.py & fulfillment.py)."""

from __future__ import annotations

from unittest.mock import patch

import frappe

from ism.api.approval import send_approval_request
from ism.comms.providers import upsert_sms_opt_out
from ism.tests.base import ISMTestCase


class TestCommsCallerWiring(ISMTestCase):
	"""Verify approval SMS/email and Ready for Pickup SMS wiring against Mock provider."""

	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")
		settings = frappe.get_single("ISM Settings")
		self._orig_provider = settings.sms_gateway_provider
		self._orig_pickup = settings.enable_ready_for_pickup_sms
		settings.sms_gateway_provider = "Mock (Deterministic Test)"
		settings.enable_ready_for_pickup_sms = 1
		settings.save(ignore_permissions=True)
		self._cleanup_phones: list[str] = []
		self._cleanup_ros: list[str] = []

	def tearDown(self) -> None:
		try:
			for ro_name in self._cleanup_ros:
				for log_name in frappe.get_all(
					"SMS Audit Log",
					filters={"reference_doctype": "Repair Order", "reference_name": ro_name},
					pluck="name",
				):
					frappe.delete_doc("SMS Audit Log", log_name, force=True, ignore_permissions=True)
			for phone in self._cleanup_phones:
				if frappe.db.exists("SMS Opt Out", phone):
					frappe.delete_doc("SMS Opt Out", phone, force=True, ignore_permissions=True)
			settings = frappe.get_single("ISM Settings")
			settings.sms_gateway_provider = self._orig_provider
			settings.enable_ready_for_pickup_sms = self._orig_pickup
			settings.save(ignore_permissions=True)
		finally:
			super().tearDown()

	def _make_ro(
		self,
		*,
		mobile_no: str = "+14165550191",
		email_id: str = "wiring@example.com",
		status: str = "Draft Estimate",
		service_status: str = "Estimating",
	):
		customer = self.create_test_customer(mobile_no=mobile_no, email_id=email_id)
		vehicle = self.create_test_vehicle(customer=customer.name, model="Model Y", model_year=2024)
		ro = self.create_test_repair_order(
			customer=customer.name,
			vehicle=vehicle.name,
			odometer_in=15000,
			status=status,
			customer_concern="Annual inspection",
			services=[
				{
					"service_title": "Brake Inspection",
					"status": service_status,
					"labor_hours": 1.0,
					"labor_rate": 150.0,
					"labor_amount": 150.0,
					"parts_total": 0.0,
					"total_amount": 150.0,
				}
			],
		)
		self._cleanup_ros.append(ro.name)
		return customer, vehicle, ro

	def test_approval_sms_creates_audit_log_and_surfaces_opt_out(self) -> None:
		phone = "+14165550192"
		self._cleanup_phones.append(phone)
		_, _, ro = self._make_ro(mobile_no=phone)

		res = send_approval_request(ro.name, channel="sms")
		self.assertTrue(res["success"])
		self.assertEqual(res["channel"], "sms")
		self.assertEqual(res["delivery_status"], "Sent")

		logs = frappe.get_all(
			"SMS Audit Log",
			filters={
				"reference_doctype": "Repair Order",
				"reference_name": ro.name,
				"message_type": "Approval Link",
			},
			fields=["name", "provider", "status", "phone_e164"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].provider, "Mock (Deterministic Test)")
		self.assertEqual(logs[0].phone_e164, phone)

		# Opt out customer and verify send_approval_request surfaces a clear opted-out error
		upsert_sms_opt_out(phone, is_opted_out=True, source="Manual Staff Override", notes="Test opt-out")
		with self.assertRaises(frappe.ValidationError) as ctx:
			send_approval_request(ro.name, channel="sms")
		self.assertIn("opted out", str(ctx.exception).lower())

	def test_ready_for_pickup_sms_audit_log_and_non_blocking_opt_out(self) -> None:
		phone = "+14165550193"
		self._cleanup_phones.append(phone)
		_, _, ro = self._make_ro(
			mobile_no=phone,
			status="Authorized - In Progress",
			service_status="Approved",
		)
		ro.approval_status = "Approved"
		ro.vehicle_location = "Completed / Parked in Lot"
		ro.save(ignore_permissions=True)

		ro.toggle_service_completion(service_row_id=ro.services[0].name, is_completed=1)
		ro.reload()
		self.assertEqual(ro.status, "Work Completed")

		logs = frappe.get_all(
			"SMS Audit Log",
			filters={
				"reference_doctype": "Repair Order",
				"reference_name": ro.name,
				"message_type": "Ready For Pickup",
			},
			fields=["name", "provider", "status", "phone_e164"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].provider, "Mock (Deterministic Test)")
		self.assertEqual(logs[0].phone_e164, phone)

		# Opt out customer, revert service, and transition again: status must succeed with Blocked - Opted Out log
		upsert_sms_opt_out(phone, is_opted_out=True, source="Manual Staff Override", notes="Test opt-out")
		ro.toggle_service_completion(service_row_id=ro.services[0].name, is_completed=0)
		frappe.db.set_value(
			"Repair Order",
			ro.name,
			{"status": "Authorized - In Progress", "vehicle_location": "Completed / Parked in Lot"},
			update_modified=False,
		)
		ro.reload()
		ro.toggle_service_completion(service_row_id=ro.services[0].name, is_completed=1)
		ro.reload()
		self.assertEqual(ro.status, "Work Completed")

		blocked_logs = frappe.get_all(
			"SMS Audit Log",
			filters={
				"reference_doctype": "Repair Order",
				"reference_name": ro.name,
				"message_type": "Ready For Pickup",
				"status": "Blocked - Opted Out",
			},
			pluck="name",
		)
		self.assertEqual(len(blocked_logs), 1)

	def test_approval_email_routes_through_dispatch_transactional_email(self) -> None:
		_, _, ro = self._make_ro(email_id="driver-wiring@example.com")

		with patch("frappe.sendmail") as mock_sendmail:
			res = send_approval_request(ro.name, channel="email")

		self.assertTrue(res["success"])
		self.assertEqual(res["channel"], "email")
		self.assertEqual(res["delivery_status"], "Sent")
		mock_sendmail.assert_called_once()
		kwargs = mock_sendmail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], ["driver-wiring@example.com"])
		self.assertEqual(kwargs["reference_doctype"], "Repair Order")
		self.assertEqual(kwargs["reference_name"], ro.name)
		self.assertTrue(kwargs["now"])
