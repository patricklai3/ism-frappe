# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, now_datetime

from ism.api.approval import (
	get_or_create_approval_token,
	get_public_quote_approval,
	send_approval_request,
	submit_customer_approval,
	submit_public_quote_approval,
)
from ism.api.inspection import (
	apply_inspection_template,
	get_check_in_inspection,
	get_inspection_template,
	list_inspection_templates,
	record_check_in_inspection,
)
from ism.api.sms import dispatch_sms, get_sms_unconfigured_reason
from ism.templates.pages.quote_approval import get_context as get_portal_context
from ism.tests.base import ISMTestCase, TEST_PREFIX

TINY_PNG_DATA_URI = (
	"data:image/png;base64,"
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_SETTINGS_FIELDS = (
	"sms_gateway_provider",
	"sms_gateway_endpoint",
	"sms_sender_id",
	"disclaimer_text",
	"authorization_token_expiry_hours",
	"cost_tolerance_percentage",
	"cost_tolerance_flat_amount",
)


class TestApprovalAndInspection(ISMTestCase):
	"""End-to-end backend tests for portal approvals, SMS/email dispatch, and check-in inspections."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		settings = frappe.get_single("ISM Settings")
		self._orig_settings = {field: getattr(settings, field, None) for field in _SETTINGS_FIELDS}
		self._orig_sms_api_key = settings.get_password("sms_gateway_api_key", raise_exception=False) if settings.sms_gateway_api_key else ""

	def tearDown(self):
		try:
			settings = frappe.get_single("ISM Settings")
			for field, value in self._orig_settings.items():
				settings.set(field, value)
			if self._orig_sms_api_key is not None:
				settings.sms_gateway_api_key = self._orig_sms_api_key
			settings.save(ignore_permissions=True)
		except Exception:
			pass
		super().tearDown()

	def _make_ro_with_services(
		self,
		services: list[dict] | None = None,
		status: str = "Draft Estimate",
		odometer_in: int | None = 42000,
		mobile_no: str | None = "+14165550199",
		email_id: str | None = "driver@example.com",
	):
		customer = self.create_test_customer(mobile_no=mobile_no, email_id=email_id)
		vehicle = self.create_test_vehicle(customer=customer.name, model="Model 3", model_year=2023)
		default_services = services or [
			{
				"service_title": "Brake Pad & Rotor Service",
				"status": "Estimating",
				"labor_hours": 1.5,
				"labor_rate": 160.0,
				"labor_amount": 240.0,
				"parts_total": 180.0,
				"total_amount": 420.0,
				"parts": json.dumps([
					{
						"item_code": "OEM-BRK-001",
						"item_name": "Front Brake Pad Set",
						"part_source": "OEM",
						"part_condition": "New",
						"warranty_period": "24 Months / 40,000 km",
						"oem_part_number": "1044621-00-B",
						"qty": 1,
						"rate": 180.0,
						"amount": 180.0,
					}
				]),
			}
		]
		ro = self.create_test_repair_order(
			customer=customer.name,
			vehicle=vehicle.name,
			odometer_in=odometer_in,
			status=status,
			customer_concern="Squeaking noise when braking at low speed",
			services=default_services,
		)
		return customer, vehicle, ro

	def test_portal_payload_drops_legacy_keys_and_enriches_ontario_fields(self):
		item_code = f"{TEST_PREFIX}PAD_ITEM"
		item = self.create_test_item(
			item_code=item_code,
			part_source="Aftermarket",
			part_condition="Reconditioned",
			warranty_period="12 Months / 20,000 km",
			oem_part_number="OEM-9988",
		)
		customer, _veh, ro = self._make_ro_with_services(
			services=[
				{
					"service_title": "Rear Caliper Rebuild",
					"status": "Pending Approval",
					"labor_hours": 1.0,
					"labor_rate": 150.0,
					"labor_amount": 150.0,
					"parts_total": 200.0,
					"total_amount": 350.0,
					"parts": json.dumps([
						{
							"item_code": item.name,
							"item_name": item.item_name,
							"qty": 1,
							"rate": 200.0,
							"amount": 200.0,
						}
					]),
				}
			]
		)
		token, _url = get_or_create_approval_token(ro.name)
		payload = get_public_quote_approval(token)

		self.assertTrue(payload["valid"])
		self.assertFalse(payload["already_submitted"])
		self.assertNotIn("quotation", payload)
		self.assertNotIn("jobs", payload)
		self.assertEqual(payload["repair_order"]["name"], ro.name)
		self.assertEqual(payload["repair_order"]["odometer_in"], 42000)
		self.assertEqual(payload["repair_order"]["customer_concern"], "Squeaking noise when braking at low speed")
		self.assertIn("disclaimer_text", payload)
		self.assertIn("cost_tolerance_percentage", payload)
		self.assertIn("cost_tolerance_flat_amount", payload)

		service = payload["services"][0]
		self.assertEqual(service["service_title"], "Rear Caliper Rebuild")
		self.assertNotIn("items", service)
		self.assertEqual(len(service["parts"]), 1)
		part = service["parts"][0]
		self.assertEqual(part["part_source"], "Aftermarket")
		self.assertEqual(part["part_condition"], "Reconditioned")
		self.assertEqual(part["warranty_period"], "12 Months / 20,000 km")
		self.assertEqual(part["oem_part_number"], "OEM-9988")

	def test_portal_page_context_and_type_confusion_guards(self):
		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			get_public_quote_approval({"!=": ""})

		with patch.object(frappe, "form_dict", frappe._dict({"token": {"!=": ""}})):
			ctx = frappe._dict()
			get_portal_context(ctx)
			self.assertFalse(ctx.approval_data["valid"])
			self.assertEqual(ctx.token, "")

		_cust, _veh, ro = self._make_ro_with_services()
		token, _url = get_or_create_approval_token(ro.name)
		ro.reload()
		ro.db_set("approval_token_expiry", add_days(now_datetime(), -1))

		expired = get_public_quote_approval(token)
		self.assertFalse(expired["valid"])
		self.assertIn("expired", expired["error"].lower())

	def test_submit_approval_audit_metadata_decline_and_guards(self):
		_cust, _veh, ro = self._make_ro_with_services(
			services=[
				{
					"service_title": "HVAC Desiccant Replacement",
					"status": "Pending Approval",
					"labor_hours": 1.0,
					"labor_rate": 150.0,
					"labor_amount": 150.0,
					"total_amount": 150.0,
				},
				{
					"service_title": "Wiper Blade Replacement",
					"status": "Pending Approval",
					"labor_hours": 0.25,
					"labor_rate": 100.0,
					"labor_amount": 25.0,
					"total_amount": 25.0,
				},
			]
		)
		token, _url = get_or_create_approval_token(ro.name)
		row0 = ro.services[0].name
		row1 = ro.services[1].name

		# Empty approvals or invalid decisions must fail cleanly without auto-approving
		with self.assertRaises(frappe.ValidationError):
			submit_customer_approval(token=token, approvals={}, signer_name="Alex", signature_data=TINY_PNG_DATA_URI)
		with self.assertRaises(frappe.ValidationError):
			submit_customer_approval(
				token=token,
				approvals={row0: "Maybe"},
				signer_name="Alex",
				signature_data=TINY_PNG_DATA_URI,
			)
		# Approving without signature must fail
		with self.assertRaises(frappe.ValidationError):
			submit_customer_approval(token=token, approvals={row0: "Approved"}, signer_name="Alex", signature_data=None)

		# Request fallback captures IP and User-Agent automatically
		mock_request = SimpleNamespace(host="localhost", scheme="http", headers={}, remote_addr="127.0.0.1")
		with (
			patch.object(frappe.local, "request_ip", "203.0.113.42", create=True),
			patch.object(frappe.local, "request", mock_request, create=True),
			patch("frappe.get_request_header", return_value="ISMTestAgent/1.0"),
		):
			res = submit_public_quote_approval(
				token=token,
				approvals=[
					{"job_id": row0, "status": "Approved"},
					{"job_id": row1, "status": "Declined"},
				],
				signer_name="Jordan Driver",
				signature_data=TINY_PNG_DATA_URI,
				cmd="ism.api.approval.submit_public_quote_approval",
			)

		self.assertTrue(res["success"])
		self.assertTrue(res.get("audit_hash"))

		ro.reload()
		self.assertEqual(ro.approval_status, "Partially Approved")
		self.assertEqual(len(ro.approval_logs), 1)
		log = ro.approval_logs[0]
		self.assertEqual(log.ip_address, "203.0.113.42")
		self.assertEqual(log.user_agent, "ISMTestAgent/1.0")
		self.assertEqual(log.audit_hash, ro.audit_hash)

		# Replay on fully decided RO fails cleanly
		with self.assertRaises(frappe.ValidationError):
			submit_customer_approval(
				token=token,
				approvals={row0: "Declined"},
				signer_name="Replay",
				signature_data=None,
			)

		# Token refresh preserves Approved status instead of overwriting with Pending Approval
		ro.db_set("approval_token_expiry", add_days(now_datetime(), -1))
		ro.db_set("approval_status", "Approved")
		for svc in ro.services:
			frappe.db.set_value("Repair Order Service", svc.name, "status", "Approved")
		get_or_create_approval_token(ro.name)
		ro.reload()
		self.assertEqual(ro.approval_status, "Approved")

	def test_full_decline_without_signature_marks_already_submitted(self):
		_cust, _veh, ro = self._make_ro_with_services()
		token, _url = get_or_create_approval_token(ro.name)

		res = submit_public_quote_approval(
			token=token,
			approvals=[{"job_id": ro.services[0].name, "status": "Declined"}],
			signer_name="No Thanks",
			signature_data=None,
		)
		self.assertTrue(res["success"])
		self.assertEqual(res["approval_status"], "Declined")

		portal = get_public_quote_approval(token)
		self.assertTrue(portal["valid"])
		self.assertTrue(portal["already_submitted"])

		with self.assertRaises(frappe.ValidationError):
			submit_customer_approval(
				token=token,
				approvals={ro.services[0].name: "Approved"},
				signer_name="No Thanks",
				signature_data=TINY_PNG_DATA_URI,
			)

	def test_sms_and_unified_approval_dispatch(self):
		settings = frappe.get_single("ISM Settings")
		settings.sms_gateway_provider = "Disabled"
		settings.save(ignore_permissions=True)

		self.assertIsNotNone(get_sms_unconfigured_reason())
		disabled_res = dispatch_sms("+14165550199", "Test SMS")
		self.assertFalse(disabled_res["success"])

		# Unsupported legacy provider name in DB is handled gracefully
		frappe.db.set_single_value("ISM Settings", "sms_gateway_provider", "Twilio")
		try:
			legacy_res = dispatch_sms("+14165550199", "Test SMS")
			self.assertFalse(legacy_res["success"])
			self.assertIn("Unsupported SMS provider", legacy_res["error"])
		finally:
			frappe.db.set_single_value("ISM Settings", "sms_gateway_provider", "Disabled")
		settings.reload()

		_cust, _veh, ro = self._make_ro_with_services()

		# Disabled fallback returns shareable link
		settings.sms_gateway_provider = "Disabled"
		settings.save(ignore_permissions=True)
		fallback = send_approval_request(ro.name, channel="sms")
		self.assertTrue(fallback["success"])
		self.assertEqual(fallback["channel"], "link")
		self.assertEqual(fallback["requested_channel"], "sms")
		self.assertEqual(fallback["delivery_status"], "Link Only (SMS Unconfigured)")

		# Active HTTP Webhook dispatch sends POST with bearer header
		settings.sms_gateway_provider = "Custom HTTP Webhook"
		settings.sms_gateway_endpoint = "https://sms.example.test/send"
		settings.sms_gateway_api_key = "secret-token-123"
		settings.sms_sender_id = "ISM-SHOP"
		settings.save(ignore_permissions=True)

		mock_ok = MagicMock(status_code=200, text='{"message_id": "msg_900"}')
		mock_ok.json.return_value = {"message_id": "msg_900"}
		with patch("ism.api.sms.requests.post", return_value=mock_ok) as post_mock:
			sent = send_approval_request(ro.name, channel="sms")
			self.assertTrue(sent["success"])
			self.assertEqual(sent["channel"], "sms")
			self.assertEqual(sent["delivery_status"], "Sent")
			self.assertEqual(sent["message_id"], "msg_900")
			self.assertTrue(post_mock.called)
			call_kwargs = post_mock.call_args.kwargs
			self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer secret-token-123")

		# Webhook 500 error propagates cleanly
		mock_err = MagicMock(status_code=500, text="Upstream Gateway Timeout")
		with patch("ism.api.sms.requests.post", return_value=mock_err):
			err_res = dispatch_sms("+14165550199", "Retry")
			self.assertFalse(err_res["success"])
			self.assertIn("HTTP 500", err_res["error"])

		# Email channel & invalid channel validation
		with patch("frappe.sendmail") as mail_mock:
			email_res = send_approval_request(ro.name, channel="email")
			self.assertEqual(email_res["channel"], "email")
			self.assertEqual(email_res["delivery_status"], "Sent")
			self.assertTrue(mail_mock.called)

		link_res = send_approval_request(ro.name, channel="Copy Link Only")
		self.assertEqual(link_res["channel"], "link")
		self.assertEqual(link_res["delivery_status"], "Link Generated")

		with self.assertRaises(frappe.ValidationError):
			send_approval_request(ro.name, channel="carrier_pigeon")

	def test_inspection_template_apply_and_structured_recording(self):
		tpl_name = f"{TEST_PREFIX}Walkaround"
		if frappe.db.exists("Inspection Template", tpl_name):
			frappe.delete_doc("Inspection Template", tpl_name, force=True, ignore_permissions=True)
		tpl = frappe.get_doc({
			"doctype": "Inspection Template",
			"template_name": tpl_name,
			"is_active": 1,
			"description": "EV Intake Walkaround",
			"items": [
				{"inspection_description": "Front Bumper & Fascia", "category": "Exterior", "photo_required": 1},
				{"inspection_description": "Tire Tread Depth", "category": "Tires & Brakes"},
				{"inspection_description": "12V / HV Warning Lights", "category": "Electronics"},
			],
		}).insert(ignore_permissions=True)
		self.track(tpl)

		# Blank template item descriptions must fail validation
		bad_tpl = frappe.get_doc({
			"doctype": "Inspection Template",
			"template_name": f"{TEST_PREFIX}BlankItem",
			"items": [{"inspection_description": "   "}],
		})
		with self.assertRaises(frappe.ValidationError):
			bad_tpl.insert(ignore_permissions=True)

		listed = list_inspection_templates()
		matched = [t for t in listed["templates"] if t["name"] == tpl.name]
		self.assertEqual(len(matched), 1)
		self.assertEqual(matched[0]["item_count"], 3)

		fetched = get_inspection_template(tpl.name)
		self.assertEqual(len(fetched["items"]), 3)
		self.assertTrue(fetched["items"][0]["photo_required"])

		_cust, _veh, ro = self._make_ro_with_services(status="Checked In", odometer_in=None)

		applied = apply_inspection_template(ro.name, template_name=tpl.name)
		self.assertEqual(applied["added_count"], 3)
		self.assertEqual(applied["summary"]["total"], 3)
		self.assertEqual(applied["summary"]["pending"], 3)

		# Idempotent re-apply skips existing points
		reapplied = apply_inspection_template(ro.name, template_name=tpl.name)
		self.assertEqual(reapplied["added_count"], 0)
		self.assertEqual(reapplied["skipped_count"], 3)

		# Record structured findings including odometer_in=0, status aliases, base64 photo, and new point
		recorded = record_check_in_inspection(
			repair_order=ro.name,
			odometer_in=0,
			customer_concern="Minor curb rash on front right wheel",
			items=[
				{
					"inspection_description": "Front Bumper & Fascia",
					"status": "warning",
					"condition": "Light scuff",
					"notes": "2 cm scuff on lower lip",
					"photos": [TINY_PNG_DATA_URI, "/files/existing-walkaround.jpg"],
				},
				{
					"idx": 2,
					"status": "ok",
					"condition": "6/32 in",
				},
				{
					"idx": 3,
					"status": "damaged",
					"notes": "TPMS warning active",
				},
				{
					"inspection_description": "Frunk Latch Operation",
					"status": "na",
					"notes": "Aftermarket hood kit",
				},
			],
		)
		self.assertEqual(recorded["odometer_in"], 0)
		self.assertEqual(recorded["updated_count"], 3)
		self.assertEqual(recorded["added_count"], 1)
		self.assertEqual(recorded["summary"]["total"], 4)
		self.assertEqual(recorded["summary"]["pending"], 0)
		self.assertEqual(recorded["summary"]["pass"], 1)
		self.assertEqual(recorded["summary"]["attention"], 1)
		self.assertEqual(recorded["summary"]["fail"], 1)
		self.assertEqual(recorded["summary"]["na"], 1)
		self.assertTrue(recorded["summary"]["all_completed"])

		bumper_item = next(i for i in recorded["items"] if i["inspection_description"] == "Front Bumper & Fascia")
		self.assertEqual(len(bumper_item["photos"]), 2)
		self.assertTrue(bumper_item["image"])
		self.assertEqual(bumper_item["inspected_by"], "Administrator")
		self.assertIsNotNone(bumper_item["inspected_at"])

		fetched_ro_insp = get_check_in_inspection(ro.name)
		self.assertEqual(fetched_ro_insp["summary"]["completed"], 4)

		# Auto-apply and replace_existing cleanly reset custom items back to template items
		replaced = apply_inspection_template(ro.name, template_name=tpl.name, replace_existing=True)
		self.assertEqual(replaced["summary"]["total"], 3)
		self.assertEqual(replaced["summary"]["pending"], 3)

		_cust2, _veh2, ro2 = self._make_ro_with_services(status="Checked In", odometer_in=None)
		auto_rec = record_check_in_inspection(
			repair_order=ro2.name,
			template_name=tpl.name,
			auto_apply_template=True,
			items=[{"inspection_description": "Tire Tread Depth", "status": "pass"}],
		)
		self.assertEqual(auto_rec["summary"]["total"], 3)
		self.assertEqual(auto_rec["summary"]["pass"], 1)
		self.assertEqual(auto_rec["summary"]["pending"], 2)

		# Reject negative odometer or invalid status cleanly
		with self.assertRaises(frappe.ValidationError):
			record_check_in_inspection(repair_order=ro.name, odometer_in=-10)
		with self.assertRaises(frappe.ValidationError):
			record_check_in_inspection(
				repair_order=ro.name,
				items=[{"idx": 1, "status": "bogus_status"}],
			)
