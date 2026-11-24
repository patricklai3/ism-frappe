# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Comprehensive tests for Tesla Phone Key encrypted vault, portal API, Checkout gate, /board RBAC, and release SMS."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.auth import CookieManager
from frappe.utils import add_days, nowdate
from frappe.utils.password import get_decrypted_password, set_encrypted_password
from werkzeug.wrappers import Response

from ism.api import board as board_api
from ism.api import portal as portal_api
from ism.comms.contracts import DEFAULT_PORTAL_COOKIE_NAME
from ism.comms.providers import clear_mock_sms_outbox, get_mock_sms_outbox
from ism.comms.sessions import issue_portal_session
from ism.portal import events as portal_events
from ism.portal import phone_key_vault, pin_vault
from ism.portal.contracts import (
	PHONE_KEY_AUTO_MASK_SECONDS,
	PHONE_KEY_STATUS_NOT_PROVIDED,
	PHONE_KEY_STATUS_PROVIDED,
	PHONE_KEY_STATUS_PURGED,
	PHONE_KEY_STATUS_RELEASED,
	PIN_STATUS_PROVIDED,
	PIN_STATUS_PURGED,
	find_forbidden_portal_keys,
	is_tesla_vehicle_info,
	validate_tesla_phone_key_url,
)
from ism.tests.base import ISMTestCase


class TestPortalPhoneKeyVault(ISMTestCase):
	"""Verify Tesla Phone Key URL validation, Fernet/__Auth encryption, advisor/manager RBAC, Checkout gate, and dynamic SMS."""

	_SETTINGS_FIELDS = (
		"sms_gateway_provider",
		"enable_customer_portal",
		"portal_brand_name",
		"enable_ready_for_pickup_sms",
		"customer_journey_demo_mode",
	)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._saved_settings = {
			f: frappe.db.get_single_value("ISM Settings", f) for f in self._SETTINGS_FIELDS
		}
		self._update_settings({
			"sms_gateway_provider": "Mock (Deterministic Test)",
			"enable_customer_portal": 1,
			"portal_brand_name": "ISM EV Service",
			"enable_ready_for_pickup_sms": 0,
			"customer_journey_demo_mode": 0,
		})
		clear_mock_sms_outbox()
		self.customer = self.create_test_customer(mobile_no="+14165550199")
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		clear_mock_sms_outbox()
		self._update_settings(self._saved_settings)
		if hasattr(frappe.local, "request"):
			frappe.local.request = None
		super().tearDown()

	def _update_settings(self, updates: dict) -> None:
		for key, value in updates.items():
			frappe.db.set_single_value("ISM Settings", key, value, update_modified=False)
		frappe.clear_document_cache("ISM Settings", "ISM Settings")

	def _create_ro(
		self,
		*,
		customer: str | None = None,
		vehicle: str | None = None,
		status: str = "Authorized - In Progress",
		approval_status: str = "Approved",
		services: list[dict] | None = None,
		**extra,
	):
		ro_dict = {
			"doctype": "Repair Order",
			"customer": customer or self.customer.name,
			"vehicle": vehicle or self.vehicle.name,
			"odometer_in": 40000,
			"status": status,
			"approval_status": approval_status,
			"tax_rate": 13.0,
			"services": services
			or [
				{
					"service_title": "Brake Inspection & Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
				}
			],
		}
		ro_dict.update(extra)
		ro = frappe.get_doc(ro_dict).insert(ignore_permissions=True)
		self.track(ro)
		return ro

	def _create_schedule_entry(
		self,
		*,
		status: str = "Scheduled",
		customer: str | None = None,
		vehicle: str | None = None,
		provisional_model: str | None = None,
	):
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": f"_TEST_ISM_PKBay_{frappe.generate_hash(length=6)}",
			"bay_type": "General Service",
			"is_active": 1,
		}).insert(ignore_permissions=True)
		self.track(bay)
		se_dict = {
			"doctype": "Schedule Entry",
			"title": "Tesla Phone Key Appointment",
			"entry_type": "Diagnostic",
			"status": status,
			"service_bay": bay.name,
			"customer": customer or self.customer.name,
			"scheduled_date": add_days(nowdate(), 2),
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
		}
		if vehicle:
			se_dict["repair_vehicle"] = vehicle
		if provisional_model:
			se_dict["provisional_vehicle_model"] = provisional_model
		se = frappe.get_doc(se_dict).insert(ignore_permissions=True)
		self.track(se)
		return se

	def _create_test_user(self, prefix: str, role: str) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.com"
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": prefix,
			"last_name": "PhoneKeyTester",
			"enabled": 1,
			"user_type": "System User",
			"send_welcome_email": 0,
			"roles": [{"role": role}],
		})
		user.flags.ignore_password_policy = True
		user.insert(ignore_permissions=True)
		self.track(user)
		return email

	def _attach_request(self, cookie_value: str | None = None, ip: str = "198.51.100.88") -> CookieManager:
		cm = CookieManager()
		frappe.local.cookie_manager = cm
		frappe.local.request_ip = ip
		cookies = {DEFAULT_PORTAL_COOKIE_NAME: cookie_value} if cookie_value else {}
		frappe.local.request = SimpleNamespace(
			cookies=cookies,
			remote_addr=ip,
			headers={"User-Agent": "ISM-Test-Agent"},
			scheme="https",
			host="portal.example.test",
			path="/api/method/ism.api.portal",
			environ={},
		)
		return cm

	def _extract_cookie_value(self, cm: CookieManager) -> str | None:
		resp = Response("ok")
		cm.flush_cookies(resp)
		for header in resp.headers.getlist("Set-Cookie"):
			if header.startswith(f"{DEFAULT_PORTAL_COOKIE_NAME}="):
				raw = header.split(";", 1)[0].split("=", 1)[1]
				return raw or None
		return None

	def _count_auth_rows(self, doctype: str, name: str, fieldname: str = "tesla_phone_key_encrypted") -> int:
		row = frappe.db.sql(
			"""
			SELECT COUNT(*)
			FROM `__Auth`
			WHERE doctype = %s
			  AND name = %s
			  AND fieldname = %s
			""",
			(doctype, name, fieldname),
		)
		return int(row[0][0]) if row else 0

	def _track_action_items(self, doctype: str, name: str) -> list[dict]:
		items = frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": doctype, "reference_name": name},
			fields=["name", "title", "priority", "dedup_key", "details", "status"],
		)
		for item in items:
			self.track("Shop Action Item", item["name"])
		return items

	def test_url_validation_and_tesla_vehicle_detection(self):
		"""Validates official HTTPS Tesla URLs (including share-sheet text) and rejects unsafe/non-Tesla links."""
		self.assertEqual(
			validate_tesla_phone_key_url("https://ts.la/driver-invite-abc123"),
			"https://ts.la/driver-invite-abc123",
		)
		self.assertEqual(
			validate_tesla_phone_key_url(
				"You've been invited to drive my Model Y: https://www.tesla.com/add-driver/xyz987!"
			),
			"https://www.tesla.com/add-driver/xyz987",
		)
		self.assertEqual(
			validate_tesla_phone_key_url("https://engage.tesla.com/driver/token-42?ref=app"),
			"https://engage.tesla.com/driver/token-42?ref=app",
		)

		invalid_inputs = [
			"",
			"   ",
			None,
			12345,
			"http://ts.la/insecure-http",
			"javascript:alert(1)",
			"https://evil-tesla.com/add-driver/123",
			"https://ts.la.evil.com/add-driver/123",
			"https://user:pass@ts.la/add-driver/123",
			"https://ts.la/",
			"https://www.tesla.com",
			"https://ts.la:8443/add-driver/123",
		]
		for bad in invalid_inputs:
			with self.assertRaises(frappe.ValidationError, msg=f"Should reject {bad!r}"):
				validate_tesla_phone_key_url(bad)

		self.assertTrue(is_tesla_vehicle_info(manufacturer="Tesla"))
		self.assertTrue(is_tesla_vehicle_info(vin="5YJ3E1EA1NF123456"))
		self.assertTrue(is_tesla_vehicle_info(model="Model Y"))
		self.assertTrue(is_tesla_vehicle_info(provisional_info="2024 Cybertruck AWD"))
		self.assertFalse(is_tesla_vehicle_info(manufacturer="Toyota", model="Camry", vin="4T1BF1FK5EU123456"))

	def test_encryption_zero_plaintext_leakage_and_cross_ro_binding(self):
		"""Invite URL is Fernet-encrypted in __Auth, absent from DB/events/comments, and bound to its RO."""
		ro = self._create_ro()
		ro_other = self._create_ro()
		invite_url = "https://ts.la/add-driver/secret-token-98765"

		realtime_calls = []
		with patch("frappe.publish_realtime", side_effect=lambda *a, **kw: realtime_calls.append((a, kw))):
			customer_state = phone_key_vault.submit_ro_phone_key(ro.name, invite_url)

		self.assertTrue(customer_state["is_tesla"])
		self.assertEqual(customer_state["status"], PHONE_KEY_STATUS_PROVIDED)
		self.assertNotIn(invite_url, str(customer_state))
		self.assertEqual(find_forbidden_portal_keys(customer_state), [])
		self.assertEqual(self._count_auth_rows("Repair Order", ro.name), 1)

		raw_col = frappe.db.get_value("Repair Order", ro.name, "tesla_phone_key_encrypted")
		self.assertNotEqual(raw_col, invite_url)
		self.assertNotIn("secret-token-98765", str(raw_col or ""))

		stored_ct = get_decrypted_password(
			"Repair Order", ro.name, fieldname="tesla_phone_key_encrypted", raise_exception=True
		)
		self.assertNotIn("secret-token-98765", stored_ct)

		# Seam 5 action item & realtime contain ZERO invite URL
		action_items = self._track_action_items("Repair Order", ro.name)
		pk_items = [a for a in action_items if a.get("dedup_key") == f"portal:phone_key:{ro.name}"]
		self.assertEqual(len(pk_items), 1)
		self.assertNotIn("secret-token-98765", str(pk_items[0]))
		self.assertNotIn("secret-token-98765", str(realtime_calls))

		# Transplant ciphertext to ro_other -> must fail RO binding
		set_encrypted_password(
			"Repair Order", ro_other.name, stored_ct, fieldname="tesla_phone_key_encrypted"
		)
		frappe.db.set_value(
			"Repair Order", ro_other.name, "tesla_phone_key_status", PHONE_KEY_STATUS_PROVIDED
		)
		with self.assertRaises(frappe.ValidationError):
			phone_key_vault.get_ro_phone_key_for_staff(ro_other.name)

	def test_advisor_manager_rbac_and_technician_exclusion(self):
		"""Advisors & Managers can reveal on Desk & /board with audit trail; Technicians & Guests are strictly denied."""
		ro = self._create_ro()
		invite_url = "https://ts.la/add-driver/rbac-check-4411"
		phone_key_vault.submit_ro_phone_key(ro.name, invite_url)
		self._track_action_items("Repair Order", ro.name)

		advisor = self._create_test_user("pk_advisor", "ISM Service Advisor")
		manager = self._create_test_user("pk_manager", "ISM Manager")
		tech = self._create_test_user("pk_tech", "ISM Technician")
		sales = self._create_test_user("pk_sales", "Sales User")

		# Advisor can reveal via Desk & /board
		frappe.set_user(advisor)
		state_adv = phone_key_vault.get_ro_phone_key_state(ro.name)
		self.assertTrue(state_adv["has_phone_key"])
		self.assertTrue(state_adv["can_reveal"])
		self.assertNotIn("invite_url", state_adv)

		res_desk = phone_key_vault.reveal_ro_phone_key_desk(ro.name)
		self.assertTrue(res_desk["available"])
		self.assertEqual(res_desk["invite_url"], invite_url)
		self.assertEqual(res_desk["auto_mask_seconds"], PHONE_KEY_AUTO_MASK_SECONDS)
		self.assertEqual(res_desk["view_count"], 1)

		# Manager can reveal via /board endpoint
		frappe.set_user(manager)
		res_board = board_api.reveal_ro_phone_key(ro.name)
		self.assertTrue(res_board["available"])
		self.assertEqual(res_board["invite_url"], invite_url)
		self.assertEqual(res_board["view_count"], 2)

		# Technician is strictly blocked on Desk AND /board (and can_reveal is False)
		frappe.set_user(tech)
		state_tech = phone_key_vault.get_ro_phone_key_state(ro.name)
		self.assertTrue(state_tech["has_phone_key"])
		self.assertFalse(state_tech["can_reveal"])
		with self.assertRaises(frappe.PermissionError):
			phone_key_vault.get_ro_phone_key_for_staff(ro.name)
		with self.assertRaises(frappe.PermissionError):
			board_api.reveal_ro_phone_key(ro.name)

		# Sales User & Guest rejected
		frappe.set_user(sales)
		with self.assertRaises(frappe.PermissionError):
			phone_key_vault.get_ro_phone_key_for_staff(ro.name)

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			phone_key_vault.get_ro_phone_key_for_staff(ro.name)

		# Audit comments logged without URL leakage
		frappe.set_user("Administrator")
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name},
			pluck="content",
		)
		self.assertTrue(any("access #1" in (c or "") for c in comments))
		self.assertTrue(any("access #2" in (c or "") for c in comments))
		self.assertFalse(any("rbac-check-4411" in (c or "") for c in comments))

	def test_portal_api_non_blocking_submit_update_remove_and_idor(self):
		"""Portal API lets owners submit/update/remove without blocking badges, and rejects cross-scope IDOR."""
		ro = self._create_ro(status="Pending Authorization", approval_status="Pending Approval")
		other_cust = self.create_test_customer(mobile_no="+14165550188")
		other_veh = self.create_test_vehicle(customer=other_cust.name)
		ro_other = self._create_ro(customer=other_cust.name, vehicle=other_veh.name)

		cm = self._attach_request()
		session_info = issue_portal_session(
			scope_type="customer",
			customer=self.customer.name,
			repair_order=ro.name,
			auth_method="sms_otp",
			phone_e164="+14165550199",
			set_response_cookie=True,
		)
		raw_cookie = self._extract_cookie_value(cm)
		self.assertTrue(raw_cookie)
		self.track("Customer Portal Session", session_info.session_id)

		frappe.set_user("Guest")
		self._attach_request(cookie_value=raw_cookie)

		hub_initial = portal_api.get_portal_hub(active_ro=ro.name)
		self.assertEqual(find_forbidden_portal_keys(hub_initial), [])
		active = hub_initial["active_visit"]
		self.assertTrue(active["vehicle"]["is_tesla"])
		self.assertTrue(active["phone_key"]["is_tesla"])
		self.assertEqual(active["phone_key"]["status"], PHONE_KEY_STATUS_NOT_PROVIDED)
		self.assertTrue(active["phone_key"]["can_submit"])
		# Optional: must NOT add a blocking badge in visits list
		visit_entry = next(v for v in hub_initial["visits"] if v["repair_order"] == ro.name)
		self.assertFalse(any("Phone Key" in b for b in visit_entry["action_required_badges"]))

		# Submit invite link via portal API
		res_submit = portal_api.submit_tesla_phone_key(
			repair_order=ro.name,
			invite_url="Share my Tesla: https://ts.la/add-driver/portal-key-111",
		)
		self.assertTrue(res_submit["success"])
		self.assertEqual(res_submit["phone_key"]["status"], PHONE_KEY_STATUS_PROVIDED)
		self.assertNotIn("portal-key-111", str(res_submit))
		self.assertEqual(find_forbidden_portal_keys(res_submit["hub"]), [])

		# Remove invite link via portal API
		res_remove = portal_api.remove_tesla_phone_key(repair_order=ro.name)
		self.assertTrue(res_remove["success"])
		self.assertEqual(res_remove["phone_key"]["status"], PHONE_KEY_STATUS_NOT_PROVIDED)
		self.assertEqual(self._count_auth_rows("Repair Order", ro.name), 0)

		# Cross-scope IDOR rejected
		with self.assertRaises(frappe.PermissionError):
			portal_api.submit_tesla_phone_key(
				repair_order=ro_other.name,
				invite_url="https://ts.la/add-driver/idor-attempt",
			)

		frappe.set_user("Administrator")
		self._track_action_items("Repair Order", ro.name)

	def test_schedule_entry_phone_key_carryover_and_cancel_purge(self):
		"""Phone Key submitted on a confirmed appointment carries over to RO on check-in and purges on cancel."""
		se = self._create_schedule_entry(status="Confirmed", vehicle=self.vehicle.name)
		invite_url = "https://ts.la/add-driver/se-carryover-777"
		pk_state = phone_key_vault.submit_schedule_entry_phone_key(se.name, invite_url)
		self.assertEqual(pk_state["status"], PHONE_KEY_STATUS_PROVIDED)
		self.assertEqual(self._count_auth_rows("Schedule Entry", se.name), 1)
		self._track_action_items("Schedule Entry", se.name)

		ro = self._create_ro()
		se.reload()
		se.repair_order = ro.name
		se.save(ignore_permissions=True)

		self.assertEqual(self._count_auth_rows("Schedule Entry", se.name), 0)
		self.assertEqual(self._count_auth_rows("Repair Order", ro.name), 1)
		ro_reveal = phone_key_vault.get_ro_phone_key_for_staff(ro.name)
		self.assertEqual(ro_reveal["invite_url"], invite_url)
		self._track_action_items("Repair Order", ro.name)

		# Separate appointment cancelled without RO -> purged
		se_cancel = self._create_schedule_entry(status="Scheduled", provisional_model="Model 3")
		phone_key_vault.submit_schedule_entry_phone_key(
			se_cancel.name, "https://ts.la/add-driver/se-cancel-888"
		)
		self._track_action_items("Schedule Entry", se_cancel.name)
		se_cancel.reload()
		se_cancel.status = "Cancelled"
		se_cancel.save(ignore_permissions=True)
		se_cancel.reload()
		self.assertEqual(se_cancel.tesla_phone_key_status, PHONE_KEY_STATUS_PURGED)
		self.assertEqual(self._count_auth_rows("Schedule Entry", se_cancel.name), 0)

	def test_checkout_gate_release_state_and_dynamic_sms_both_pin_and_phone_key(self):
		"""Checkout requires phone_key_removed_confirmed when Phone Key is Provided; releases key & sends ONE combined SMS."""
		ro = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		pin_vault.submit_ro_pin(ro.name, "0427")
		phone_key_vault.submit_ro_phone_key(ro.name, "https://ts.la/add-driver/checkout-both-999")
		self._track_action_items("Repair Order", ro.name)
		ro.reload()
		ro.status = "Work Completed"
		ro.save(ignore_permissions=True)
		ro.reload()

		# Attempting release WITHOUT confirming shop phone removal must fail
		with self.assertRaises(frappe.ValidationError):
			ro.process_payment_and_release(
				payment_method="Debit / Interac",
				odometer_out=40050,
			)
		self.assertEqual(self._count_auth_rows("Repair Order", ro.name, "tesla_phone_key_encrypted"), 1)
		self.assertEqual(self._count_auth_rows("Repair Order", ro.name, "pin_to_drive_encrypted"), 1)
		self.assertEqual(len(get_mock_sms_outbox()), 0)

		# Confirming removal succeeds, purges BOTH secrets, marks Released, and sends ONE combined SMS
		res = ro.process_payment_and_release(
			payment_method="Debit / Interac",
			odometer_out=40050,
			phone_key_removed_confirmed=1,
		)
		self.assertEqual(res["status"], "success")
		if res.get("sales_invoice"):
			self.track("Sales Invoice", res["sales_invoice"])
		for pe_name in res.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		ro.reload()
		self.assertEqual(ro.status, "Closed / Released")
		self.assertEqual(ro.pin_to_drive_status, PIN_STATUS_PURGED)
		self.assertEqual(ro.tesla_phone_key_status, PHONE_KEY_STATUS_RELEASED)
		self.assertTrue(ro.tesla_phone_key_released_at)
		self.assertEqual(ro.tesla_phone_key_released_by, "Administrator")
		self.assertEqual(self._count_auth_rows("Repair Order", ro.name, "pin_to_drive_encrypted"), 0)
		self.assertEqual(self._count_auth_rows("Repair Order", ro.name, "tesla_phone_key_encrypted"), 0)

		# Customer portal shows Phone Key Released
		portal_pk = phone_key_vault.get_ro_phone_key_customer_state(ro)
		self.assertEqual(portal_pk["status"], PHONE_KEY_STATUS_RELEASED)
		self.assertTrue(portal_pk["released_at"])
		self.assertFalse(portal_pk["can_submit"])
		self.assertFalse(portal_pk["can_update"])

		# Exactly ONE SMS mentioning both PIN-to-Drive and Tesla Phone Key
		outbox = get_mock_sms_outbox()
		self.assertEqual(len(outbox), 1)
		body = outbox[0]["body"]
		self.assertIn("PIN-to-Drive", body)
		self.assertIn("Tesla Phone Key", body)
		self.assertNotIn("0427", body)
		self.assertNotIn("checkout-both-999", body)

		# Re-saving released RO must NOT send a duplicate SMS
		ro.save(ignore_permissions=True)
		self.assertEqual(len(get_mock_sms_outbox()), 1)

	def test_dynamic_release_sms_pin_only_phone_key_only_neither_and_cancel(self):
		"""Verifies dynamic SMS phrasing for PIN-only, Phone-Key-only, neither, and no SMS on Cancelled."""
		# 1. PIN-only release
		clear_mock_sms_outbox()
		ro_pin = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		pin_vault.submit_ro_pin(ro_pin.name, "1357")
		self._track_action_items("Repair Order", ro_pin.name)
		ro_pin.reload()
		ro_pin.status = "Work Completed"
		ro_pin.save(ignore_permissions=True)
		ro_pin.reload()
		# No Phone Key -> checkout works without phone_key_removed_confirmed
		res_pin = ro_pin.process_payment_and_release(payment_method="Debit / Interac", odometer_out=40100)
		if res_pin.get("sales_invoice"):
			self.track("Sales Invoice", res_pin["sales_invoice"])
		for pe in res_pin.get("payment_entries") or []:
			self.track("Payment Entry", pe)
		outbox = get_mock_sms_outbox()
		self.assertEqual(len(outbox), 1)
		self.assertIn("PIN-to-Drive", outbox[0]["body"])
		self.assertNotIn("Tesla Phone Key", outbox[0]["body"])

		# 2. Phone-Key-only release
		clear_mock_sms_outbox()
		ro_pk = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		phone_key_vault.submit_ro_phone_key(ro_pk.name, "https://ts.la/add-driver/only-pk-555")
		self._track_action_items("Repair Order", ro_pk.name)
		ro_pk.reload()
		ro_pk.status = "Work Completed"
		ro_pk.save(ignore_permissions=True)
		ro_pk.reload()
		res_pk = ro_pk.process_payment_and_release(
			payment_method="Debit / Interac",
			odometer_out=40120,
			phone_key_removed_confirmed=True,
		)
		if res_pk.get("sales_invoice"):
			self.track("Sales Invoice", res_pk["sales_invoice"])
		for pe in res_pk.get("payment_entries") or []:
			self.track("Payment Entry", pe)
		outbox = get_mock_sms_outbox()
		self.assertEqual(len(outbox), 1)
		self.assertIn("Tesla Phone Key", outbox[0]["body"])
		self.assertNotIn("PIN-to-Drive", outbox[0]["body"])

		# 3. Neither provided -> 0 SMS
		clear_mock_sms_outbox()
		ro_none = self._create_ro(status="Work Completed", approval_status="Approved")
		res_none = ro_none.process_payment_and_release(payment_method="Debit / Interac", odometer_out=40150)
		if res_none.get("sales_invoice"):
			self.track("Sales Invoice", res_none["sales_invoice"])
		for pe in res_none.get("payment_entries") or []:
			self.track("Payment Entry", pe)
		self.assertEqual(len(get_mock_sms_outbox()), 0)

		# 4. Cancelled RO -> purges Phone Key to Purged, 0 SMS
		clear_mock_sms_outbox()
		ro_cancel = self._create_ro(status="Authorized - In Progress")
		phone_key_vault.submit_ro_phone_key(ro_cancel.name, "https://ts.la/add-driver/cancel-ro-222")
		self._track_action_items("Repair Order", ro_cancel.name)
		ro_cancel.reload()
		ro_cancel.status = "Cancelled"
		ro_cancel.save(ignore_permissions=True)
		ro_cancel.reload()
		self.assertEqual(ro_cancel.tesla_phone_key_status, PHONE_KEY_STATUS_PURGED)
		self.assertEqual(self._count_auth_rows("Repair Order", ro_cancel.name), 0)
		self.assertEqual(len(get_mock_sms_outbox()), 0)

	def test_daily_sweeper_and_seam5_zero_board_import(self):
		"""Daily scheduler sweeps stale closed RO Phone Keys and vault/events maintain zero ism.board imports."""
		ro_stale = self._create_ro()
		phone_key_vault.submit_ro_phone_key(ro_stale.name, "https://ts.la/add-driver/stale-sweep-333")
		self._track_action_items("Repair Order", ro_stale.name)
		frappe.db.set_value("Repair Order", ro_stale.name, "status", "Closed / Released", update_modified=False)

		swept = pin_vault.purge_stale_closed_ro_pins()
		self.assertGreaterEqual(swept, 1)
		self.assertEqual(self._count_auth_rows("Repair Order", ro_stale.name), 0)
		self.assertEqual(
			frappe.db.get_value("Repair Order", ro_stale.name, "tesla_phone_key_status"),
			PHONE_KEY_STATUS_RELEASED,
		)

		portal_dir = Path(phone_key_vault.__file__).resolve().parent
		for fname in ("phone_key_vault.py", "pin_vault.py", "events.py"):
			tree = ast.parse((portal_dir / fname).read_text(encoding="utf-8"))
			for node in ast.walk(tree):
				if isinstance(node, ast.Import):
					for alias in node.names:
						self.assertFalse(alias.name.startswith("ism.board"), f"{fname} imports {alias.name}")
				elif isinstance(node, ast.ImportFrom) and node.module:
					self.assertFalse(node.module.startswith("ism.board"), f"{fname} imports {node.module}")
