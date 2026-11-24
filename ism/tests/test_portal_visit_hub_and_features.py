# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""End-to-end tests for Project 4 Wave 3A: Visit Hub projection, portal REST API, and approval wiring."""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.auth import CookieManager
from frappe.handler import handle as frappe_handle_rpc
from frappe.utils import add_days, flt, nowdate
from werkzeug.wrappers import Response

import ism.api.portal as portal_api
from ism.api.approval import (
	clear_portal_rate_limits,
	get_or_create_approval_token,
	submit_customer_approval,
)
from ism.comms.contracts import DEFAULT_PORTAL_COOKIE_NAME
from ism.comms.otp import get_last_mock_otp
from ism.comms.providers import clear_mock_sms_outbox, get_mock_sms_outbox
from ism.comms.sessions import issue_portal_session
from ism.payments.contracts import (
	INTENT_INVOICE_BALANCE,
	INTENT_MANUAL_DEPOSIT,
	SQUARE_CURRENCY,
	SQUARE_ENV_MOCK,
)
from ism.payments.deposit_requests import create_deposit_request
from ism.payments.square_client import MockSquareClient
from ism.portal import (
	PIN_STATUS_OPTED_OUT,
	PIN_STATUS_PROVIDED,
	PIN_STATUS_PURGED,
	PORTAL_STATUS_BEING_WORKED_ON,
	PORTAL_STATUS_CANCELLED,
	PORTAL_STATUS_READY_FOR_PICKUP,
	PORTAL_STATUS_SCHEDULED,
	build_portal_hub_payload,
	create_booking_portal_link,
	derive_binary_visit_status,
	email_sales_invoice_copy,
	find_forbidden_portal_keys,
	get_ro_invoice_summary,
	get_ro_pin_for_staff,
	get_ro_pin_state,
	opt_out_ro_pin,
	purge_ro_pin,
	render_sales_invoice_pdf,
	submit_ro_pin,
)
from ism.portal.auth import clear_portal_auth_rate_limits
from ism.setup_tasks.seed import ensure_square_bank_account_and_mop
from ism.tests.base import ISMTestCase, retry_on_deadlock

TINY_PNG_DATA_URI = (
	"data:image/png;base64,"
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
FAKE_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


class TestPortalVisitHubAndFeatures(ISMTestCase):
	"""Verify Wave 3A binary Visit Hub projection, portal REST API, locks, and anti-IDOR guards."""

	_SETTINGS_FIELDS = (
		"sms_gateway_provider",
		"sms_default_country_code",
		"sms_enforce_opt_out",
		"otp_code_length",
		"otp_ttl_seconds",
		"otp_resend_cooldown_seconds",
		"otp_max_verify_attempts",
		"otp_lockout_seconds",
		"otp_phone_rate_limit_per_hour",
		"otp_ip_rate_limit_per_hour",
		"portal_session_cookie_name",
		"portal_session_cookie_samesite",
		"portal_session_secure_cookie",
		"enable_customer_portal",
		"portal_brand_name",
		"portal_support_phone",
		"portal_pickup_hours_text",
		"portal_booking_token_ttl_days",
		"portal_invoice_print_format",
		"transactional_email_rate_limit_per_hour",
		"transactional_email_ip_rate_limit_per_hour",
		"customer_arrival_offset_minutes",
		"reschedule_cutoff_hours",
		"max_self_reschedules",
		"self_serve_horizon_days",
		"self_serve_min_lead_time_minutes",
		"operating_hours_start",
		"operating_hours_end",
		"square_environment",
		"square_currency",
		"square_application_id",
		"square_location_id",
		"square_mode_of_payment",
		"square_bank_account",
	)

	def _save_settings(self, updates: dict) -> None:
		def _do_save():
			settings = frappe.get_single("ISM Settings")
			for fieldname, value in updates.items():
				setattr(settings, fieldname, value)
			settings.save(ignore_permissions=True)

		retry_on_deadlock(_do_save)
		frappe.clear_cache(doctype="ISM Settings")

	def setUp(self):
		super().setUp()
		self._saved_user = frappe.session.user
		frappe.set_user("Administrator")

		# Snapshot ISM Settings BEFORE ensure_square_bank_account_and_mop() or any test mutation.
		settings_before = frappe.get_single("ISM Settings")
		self._saved_settings = {f: getattr(settings_before, f, None) for f in self._SETTINGS_FIELDS}

		ensure_square_bank_account_and_mop()
		MockSquareClient.reset_store()

		settings = frappe.get_single("ISM Settings")
		self._save_settings({
			"sms_gateway_provider": "Mock (Deterministic Test)",
			"sms_default_country_code": "+1",
			"sms_enforce_opt_out": 1,
			"otp_code_length": 6,
			"otp_ttl_seconds": 300,
			"otp_resend_cooldown_seconds": 60,
			"otp_max_verify_attempts": 5,
			"otp_lockout_seconds": 900,
			"otp_phone_rate_limit_per_hour": 10,
			"otp_ip_rate_limit_per_hour": 30,
			"portal_session_cookie_name": DEFAULT_PORTAL_COOKIE_NAME,
			"portal_session_cookie_samesite": "Lax",
			"portal_session_secure_cookie": 1,
			"enable_customer_portal": 1,
			"portal_brand_name": "ISM EV Service",
			"portal_support_phone": "416-555-0100",
			"portal_pickup_hours_text": "Mon-Fri 8:00 AM - 6:00 PM",
			"portal_booking_token_ttl_days": 30,
			"portal_invoice_print_format": "Standard",
			"transactional_email_rate_limit_per_hour": 2,
			"transactional_email_ip_rate_limit_per_hour": 20,
			"customer_arrival_offset_minutes": 15,
			"reschedule_cutoff_hours": 2,
			"max_self_reschedules": 3,
			"self_serve_horizon_days": 30,
			"self_serve_min_lead_time_minutes": 60,
			"operating_hours_start": "08:00:00",
			"operating_hours_end": "17:00:00",
			"square_environment": SQUARE_ENV_MOCK,
			"square_currency": SQUARE_CURRENCY,
			"square_application_id": "sandbox-sq0idb-portal-test",
			"square_location_id": "LMOCK00000001",
			"square_mode_of_payment": settings.square_mode_of_payment or "Square",
		})

		self._local_sentinel = object()
		self._saved_local_attrs = {
			attr: (getattr(frappe.local, attr) if hasattr(frappe.local, attr) else self._local_sentinel)
			for attr in ("request", "request_ip", "form_dict", "cookie_manager", "response")
		}

		clear_portal_auth_rate_limits()
		clear_portal_rate_limits()
		clear_mock_sms_outbox()

	def tearDown(self):
		frappe.set_user("Administrator")
		for attr, prev_val in self._saved_local_attrs.items():
			if prev_val is self._local_sentinel:
				if hasattr(frappe.local, attr):
					delattr(frappe.local, attr)
			else:
				setattr(frappe.local, attr, prev_val)
		clear_portal_auth_rate_limits()
		clear_portal_rate_limits()
		clear_mock_sms_outbox()
		MockSquareClient.reset_store()
		try:
			super().tearDown()
		finally:
			frappe.set_user("Administrator")
			self._save_settings(self._saved_settings)
			frappe.db.commit()
			frappe.clear_cache(doctype="ISM Settings")
			frappe.db.value_cache.clear()
			frappe.set_user(self._saved_user)

	def _attach_request(self, cookie_value: str | None = None, ip: str = "198.51.100.77") -> CookieManager:
		cm = CookieManager()
		frappe.local.cookie_manager = cm
		frappe.local.request_ip = ip
		cookies = {DEFAULT_PORTAL_COOKIE_NAME: cookie_value} if cookie_value else {}
		frappe.local.request = SimpleNamespace(
			cookies=cookies,
			remote_addr=ip,
			headers={"User-Agent": "ISM-Portal-Wave3A-Test/1.0"},
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

	def _track_session_artifacts(self, phone_e164: str | None = None, ro_name: str | None = None) -> None:
		if phone_e164:
			for dt in ("Customer Portal Session", "Portal OTP Challenge", "SMS Audit Log"):
				for name in frappe.get_all(dt, filters={"phone_e164": phone_e164}, pluck="name"):
					self.track(dt, name)
		if ro_name:
			for name in frappe.get_all(
				"Customer Portal Session", filters={"scope_repair_order": ro_name}, pluck="name"
			):
				self.track("Customer Portal Session", name)
			for name in frappe.get_all(
				"Shop Action Item",
				filters={"reference_doctype": "Repair Order", "reference_name": ro_name},
				pluck="name",
			):
				self.track("Shop Action Item", name)
			for name in frappe.get_all(
				"ISM Payment Transaction", filters={"repair_order": ro_name}, pluck="name"
			):
				tx = frappe.db.get_value(
					"ISM Payment Transaction", name, ["payment_entry"], as_dict=True
				)
				self.track("ISM Payment Transaction", name)
				if tx and tx.get("payment_entry"):
					self.track("Payment Entry", tx["payment_entry"])

	def _create_ro(
		self,
		*,
		customer: str,
		vehicle: str,
		status: str = "Pending Authorization",
		approval_status: str = "Pending Approval",
		service_status: str = "Pending Approval",
		labor_hours: float = 2.0,
		labor_rate: float = 150.0,
		**extra,
	):
		ro_dict = {
			"doctype": "Repair Order",
			"customer": customer,
			"vehicle": vehicle,
			"odometer_in": 32500,
			"status": status,
			"approval_status": approval_status,
			"tax_rate": 13.0,
			"services": [
				{
					"service_title": "Brake Pad & Rotor Service",
					"status": service_status,
					"labor_hours": labor_hours,
					"labor_rate": labor_rate,
				}
			],
		}
		ro_dict.update(extra)
		ro = frappe.get_doc(ro_dict).insert(ignore_permissions=True)
		self.track(ro)
		return ro

	def _create_bay(self) -> str:
		bay_name = f"Portal Bay {frappe.generate_hash(length=5)}"
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": bay_name,
			"bay_type": "General Service",
			"status": "Active",
		}).insert(ignore_permissions=True)
		self.track(bay)
		return bay.name

	def _create_schedule_entry(
		self,
		*,
		customer: str | None = None,
		vehicle: str | None = None,
		repair_order: str | None = None,
		bay: str | None = None,
		scheduled_date: str | None = None,
		scheduled_time: str = "08:00:00",
		status: str = "Scheduled",
		provisional_phone: str | None = None,
		provisional_customer_name: str | None = None,
		provisional_vehicle_model: str | None = None,
	):
		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"status": status,
			"scheduled_date": scheduled_date or add_days(nowdate(), 4),
			"scheduled_time": scheduled_time,
			"estimated_duration": 60,
			"service_bay": bay or self._create_bay(),
			"customer": customer,
			"repair_vehicle": vehicle,
			"repair_order": repair_order,
			"provisional_phone": provisional_phone,
			"provisional_customer_name": provisional_customer_name,
			"provisional_vehicle_model": provisional_vehicle_model,
		}).insert(ignore_permissions=True)
		self.track(se)
		return se

	def _create_sales_invoice(self, *, customer: str, rate: float = 200.0, submit: bool = True):
		item = self.create_test_item(
			f"PORTAL-INV-{frappe.generate_hash(length=6)}",
			standard_rate=rate,
		)
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"customer": customer,
			"company": frappe.defaults.get_global_default("company")
			or frappe.db.get_value("Company", {}, "name"),
			"currency": SQUARE_CURRENCY,
			"items": [
				{
					"item_code": item.item_code,
					"qty": 1,
					"rate": rate,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(si)
		if submit:
			si.submit()
			si.reload()
		return si

	def _create_user_with_role(self, prefix: str, role: str) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.test"
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": prefix,
			"enabled": 1,
			"user_type": "System User",
			"roles": [{"role": role}],
		}).insert(ignore_permissions=True)
		self.track(user)
		return user.name

	def test_public_facade_exports_method_guards_and_zero_board_imports(self):
		"""Verify frozen ism.portal facade, HTTP method constraints, and Seam 5 zero ism.board imports."""
		for symbol in (
			create_booking_portal_link,
			get_ro_pin_state,
			get_ro_pin_for_staff,
			purge_ro_pin,
			submit_ro_pin,
			opt_out_ro_pin,
			get_ro_invoice_summary,
			render_sales_invoice_pdf,
			email_sales_invoice_copy,
			build_portal_hub_payload,
			derive_binary_visit_status,
		):
			self.assertTrue(callable(symbol))

		# Verify Seam 5: zero ism.board imports anywhere in ism/portal/ or ism/api/portal.py
		portal_dir = Path(frappe.get_app_path("ism", "portal"))
		files_to_scan = list(portal_dir.glob("*.py")) + [
			Path(frappe.get_app_path("ism", "api", "portal.py"))
		]
		for file_path in files_to_scan:
			tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
			for node in ast.walk(tree):
				if isinstance(node, ast.Import):
					for alias in node.names:
						self.assertFalse(
							alias.name == "ism.board" or alias.name.startswith("ism.board."),
							msg=f"Forbidden ism.board import in {file_path}",
						)
				elif isinstance(node, ast.ImportFrom):
					mod = node.module or ""
					self.assertFalse(
						mod == "ism.board" or mod.startswith("ism.board."),
						msg=f"Forbidden ism.board import in {file_path}",
					)

		# Verify HTTP method decorators and guest flags on all 15 whitelisted portal endpoints
		post_only = (
			portal_api.exchange_portal_link,
			portal_api.request_login_otp,
			portal_api.verify_login_otp,
			portal_api.sign_out,
			portal_api.submit_portal_quote_approval,
			portal_api.submit_pin_to_drive,
			portal_api.opt_out_pin_to_drive,
			portal_api.reschedule_appointment,
			portal_api.submit_portal_payment,
			portal_api.email_invoice_copy,
			portal_api.reveal_ro_pin,
		)
		for fn in post_only:
			self.assertIn(fn, frappe.whitelisted)
			self.assertEqual(
				set(frappe.allowed_http_methods_for_whitelisted_func.get(fn, [])),
				{"POST"},
				msg=f"Expected POST-only on {fn.__name__}",
			)

		get_only = (
			portal_api.get_portal_bootstrap,
			portal_api.get_portal_hub,
			portal_api.get_reschedule_options,
			portal_api.download_invoice_pdf,
		)
		for fn in get_only:
			self.assertIn(fn, frappe.whitelisted)
			self.assertEqual(
				set(frappe.allowed_http_methods_for_whitelisted_func.get(fn, [])),
				{"GET"},
				msg=f"Expected GET-only on {fn.__name__}",
			)

		self.assertNotIn(portal_api.reveal_ro_pin, frappe.guest_methods)

	def test_binary_status_projection_all_statuses_and_zero_forbidden_keys(self):
		"""Verify strictly binary status across EVERY RO & SE status and 0 forbidden keys across full hub."""
		future_date = add_days(nowdate(), 3)
		active_se = SimpleNamespace(
			status="Scheduled",
			scheduled_date=future_date,
			scheduled_time="08:00:00",
		)

		# 1. Unconverted Schedule Entry statuses
		for se_status, expected in (
			("Scheduled", PORTAL_STATUS_SCHEDULED),
			("Confirmed", PORTAL_STATUS_SCHEDULED),
			("In Progress", PORTAL_STATUS_BEING_WORKED_ON),
			("Cancelled", PORTAL_STATUS_CANCELLED),
			("No Show", PORTAL_STATUS_CANCELLED),
		):
			se_stub = SimpleNamespace(
				status=se_status,
				scheduled_date=future_date,
				scheduled_time="08:00:00",
			)
			projected = derive_binary_visit_status(ro_doc=None, schedule_entry_doc=se_stub)
			self.assertEqual(projected["code"], expected)
			self.assertEqual(find_forbidden_portal_keys(projected), [])
			if expected == PORTAL_STATUS_SCHEDULED:
				# 08:00 internal capacity -> 07:45 silent customer drop-off
				self.assertEqual(projected["appointment"]["customer_arrival_hhmm"], "07:45")
				self.assertIn("7:45", projected["appointment"]["display_time"])

		# 2. Repair Order statuses
		ro_cases = [
			("Draft Estimate", active_se, None, 0, PORTAL_STATUS_SCHEDULED),
			("Pending Authorization", active_se, None, 0, PORTAL_STATUS_SCHEDULED),
			("Pending Authorization", active_se, "2026-11-20 09:00:00", 0, PORTAL_STATUS_BEING_WORKED_ON),
			("Draft Estimate", None, None, 0, PORTAL_STATUS_BEING_WORKED_ON),
			("Pending Authorization", None, None, 0, PORTAL_STATUS_BEING_WORKED_ON),
			("Checked In", active_se, None, 0, PORTAL_STATUS_BEING_WORKED_ON),
			("Authorized - In Progress", active_se, None, 0, PORTAL_STATUS_BEING_WORKED_ON),
			("Waiting on Parts", active_se, None, 0, PORTAL_STATUS_BEING_WORKED_ON),
			("Work Completed", active_se, None, 0, PORTAL_STATUS_READY_FOR_PICKUP),
			("Billed / Paid", active_se, None, 0, PORTAL_STATUS_READY_FOR_PICKUP),
			("Closed / Released", active_se, None, 1, PORTAL_STATUS_READY_FOR_PICKUP),
			("Cancelled", active_se, None, 0, PORTAL_STATUS_CANCELLED),
		]
		for ro_status, se_stub, picked_up_at, released, expected_code in ro_cases:
			ro_stub = SimpleNamespace(
				status=ro_status,
				picked_up_at=picked_up_at,
				vehicle_released=released,
			)
			projected = derive_binary_visit_status(ro_doc=ro_stub, schedule_entry_doc=se_stub)
			self.assertEqual(
				projected["code"],
				expected_code,
				msg=f"Wrong binary status for RO={ro_status}, picked_up_at={picked_up_at}",
			)
			self.assertEqual(find_forbidden_portal_keys(projected), [])
			if expected_code == PORTAL_STATUS_READY_FOR_PICKUP:
				self.assertEqual(projected["pickup_hours"], "Mon-Fri 8:00 AM - 6:00 PM")
				self.assertEqual(projected["vehicle_released"], bool(released))

		# 3. Full hub payload recursive forbidden-key check across all 3 scopes
		customer = self.create_test_customer(
			customer_name="Binary Audit Driver",
			mobile_no="+14165550201",
			email_id="binary.audit@example.test",
		)
		vehicle = self.create_test_vehicle(customer=customer.name, license_plate="BIN201")
		ro = self._create_ro(customer=customer.name, vehicle=vehicle.name, status="Pending Authorization")
		se_linked = self._create_schedule_entry(
			customer=customer.name,
			vehicle=vehicle.name,
			repair_order=ro.name,
			scheduled_time="08:00:00",
		)
		se_booking = self._create_schedule_entry(
			provisional_phone="+14165550201",
			provisional_customer_name="Binary Audit Driver",
			provisional_vehicle_model="Model Y",
			scheduled_date=add_days(nowdate(), 5),
			scheduled_time="09:00:00",
		)

		for scope_kwargs in (
			{
				"auth_method": "magic_link",
				"scope_type": "repair_order",
				"customer": customer.name,
				"phone_e164": "+14165550201",
				"repair_order": ro.name,
			},
			{
				"auth_method": "magic_link",
				"scope_type": "booking",
				"phone_e164": "+14165550201",
				"schedule_entry": se_booking.name,
			},
			{
				"auth_method": "sms_otp",
				"scope_type": "customer",
				"customer": customer.name,
				"phone_e164": "+14165550201",
			},
		):
			sess = issue_portal_session(**scope_kwargs, set_response_cookie=False)
			self._track_session_artifacts(phone_e164="+14165550201", ro_name=ro.name)
			hub = build_portal_hub_payload(sess)
			self.assertEqual(find_forbidden_portal_keys(hub), [])
			# Confirm JSON serializable with zero dataclass serialization issues
			serialized = json.dumps(hub)
			self.assertNotIn("signature_image", serialized)
			self.assertNotIn("internal_start", serialized)
			self.assertNotIn("arrival_offset_minutes", serialized)
			self.assertNotIn("service_bay", serialized)
			self.assertNotIn("assigned_technician", serialized)

			if scope_kwargs["scope_type"] == "repair_order":
				self.assertEqual(hub["active_visit"]["repair_order"], ro.name)
				self.assertEqual(hub["active_visit"]["schedule_entry"], se_linked.name)
				self.assertEqual(hub["active_visit"]["status"]["code"], PORTAL_STATUS_SCHEDULED)
				self.assertEqual(
					hub["active_visit"]["status"]["appointment"]["customer_arrival_hhmm"],
					"07:45",
				)
				self.assertTrue(hub["session"]["can_upgrade_to_account_view"])
			elif scope_kwargs["scope_type"] == "booking":
				self.assertEqual(hub["active_visit"]["kind"], "booking")
				self.assertEqual(hub["active_visit"]["schedule_entry"], se_booking.name)
				self.assertIsNone(hub["active_visit"]["repair_order"])
				self.assertIsNone(hub["active_visit"]["quote"])
				self.assertIsNone(hub["active_visit"]["payment"])
				self.assertIsNone(hub["active_visit"]["invoice"])
				self.assertEqual(
					hub["active_visit"]["appointment"]["display"]["customer_arrival_hhmm"],
					"08:45",
				)
			else:
				self.assertFalse(hub["session"]["can_upgrade_to_account_view"])
				self.assertEqual(len(hub["visits"]), 2)

	def test_bootstrap_zero_pii_link_exchange_otp_upgrade_and_sign_out(self):
		"""Verify Path 2 zero-PII bootstrap, Path 1 zero-OTP links, Path 2 OTP upgrade, and sign-out."""
		phone = "+14165550202"
		customer = self.create_test_customer(
			customer_name="Secret Unauth Name 202",
			mobile_no=phone,
			email_id="secret202@example.test",
		)
		vehicle = self.create_test_vehicle(
			customer=customer.name,
			license_plate="SECRET202",
			vin="5YJ3E1EA1NF202202",
		)
		ro1 = self._create_ro(customer=customer.name, vehicle=vehicle.name)
		ro2 = self._create_ro(customer=customer.name, vehicle=vehicle.name, status="Checked In")
		ro_token, _ = get_or_create_approval_token(ro1.name)

		se_booking = self._create_schedule_entry(
			provisional_phone=phone,
			provisional_customer_name="Secret Unauth Name 202",
			provisional_vehicle_model="Model 3",
		)
		booking_url = create_booking_portal_link(se_booking.name)
		booking_token = parse_qs(urlparse(booking_url).query)["booking_token"][0]

		frappe.set_user("Guest")
		try:
			# 1. Unauthenticated bootstrap -> ZERO PII
			self._attach_request(cookie_value=None)
			boot = portal_api.get_portal_bootstrap()
			self.assertFalse(boot["authenticated"])
			self.assertNotIn("hub", boot)
			self.assertEqual(boot["shop"]["company_name"], "ISM EV Service")
			boot_blob = json.dumps(boot)
			for pii in (
				"Secret Unauth Name 202",
				"SECRET202",
				"5YJ3E1EA1NF202202",
				ro1.name,
				ro2.name,
				se_booking.name,
				phone,
			):
				self.assertNotIn(pii, boot_blob)

			# 2. Path 1 RO link exchange (zero OTP!) -> sets cookie, scoped strictly to ro1
			cm = self._attach_request(cookie_value=None)
			ex_res = portal_api.exchange_portal_link(token=ro_token)
			self.assertTrue(ex_res["valid"])
			self.assertEqual(ex_res["hub"]["session"]["scope_type"], "repair_order")
			self.assertEqual(len(ex_res["hub"]["visits"]), 1)
			self.assertEqual(ex_res["hub"]["active_visit"]["repair_order"], ro1.name)
			self.assertEqual(find_forbidden_portal_keys(ex_res), [])
			ro_cookie = self._extract_cookie_value(cm)
			self.assertTrue(ro_cookie)

			# Path 3 returning cookie restore via get_portal_bootstrap
			self._attach_request(cookie_value=ro_cookie)
			boot_authed = portal_api.get_portal_bootstrap()
			self.assertTrue(boot_authed["authenticated"])
			self.assertEqual(boot_authed["hub"]["active_visit"]["repair_order"], ro1.name)

			# Path 1 booking link exchange (zero OTP!) -> pre- and post-RO conversion
			cm_book = self._attach_request(cookie_value=None)
			book_res = portal_api.exchange_portal_link(booking_token=booking_token)
			self.assertTrue(book_res["valid"])
			self.assertEqual(book_res["hub"]["session"]["scope_type"], "booking")
			self.assertEqual(book_res["hub"]["active_visit"]["kind"], "booking")
			self.assertEqual(book_res["hub"]["active_visit"]["schedule_entry"], se_booking.name)
			book_cookie = self._extract_cookie_value(cm_book)
			self.assertTrue(book_cookie)

			# Convert booking SE -> ro2; booking session automatically follows ro2
			frappe.db.set_value("Schedule Entry", se_booking.name, "repair_order", ro2.name)
			self._attach_request(cookie_value=book_cookie)
			hub_after_convert = portal_api.get_portal_hub(active_schedule_entry=se_booking.name)
			self.assertEqual(hub_after_convert["active_visit"]["kind"], "repair_order")
			self.assertEqual(hub_after_convert["active_visit"]["repair_order"], ro2.name)
			self.assertEqual(hub_after_convert["active_visit"]["schedule_entry"], se_booking.name)

			# 3. Path 2 SMS OTP upgrade from RO-scoped session -> customer scope (sees both ROs)
			self._attach_request(cookie_value=ro_cookie)
			otp_req = portal_api.request_login_otp(phone=phone)
			self.assertTrue(otp_req["success"])
			otp_code = get_last_mock_otp(phone)
			self.assertTrue(otp_code)

			cm_upg = self._attach_request(cookie_value=ro_cookie)
			otp_ver = portal_api.verify_login_otp(
				challenge_id=otp_req["challenge_id"],
				code=otp_code,
				phone=phone,
				upgrade_existing_session=1,
			)
			self.assertTrue(otp_ver["verified"])
			self.assertEqual(otp_ver["hub"]["session"]["scope_type"], "customer")
			visit_ros = {v["repair_order"] for v in otp_ver["hub"]["visits"]}
			self.assertEqual(visit_ros, {ro1.name, ro2.name})
			cust_cookie = self._extract_cookie_value(cm_upg)
			self.assertTrue(cust_cookie)

			# 4. Explicit sign-out revokes server session and clears cookie
			cm_out = self._attach_request(cookie_value=cust_cookie)
			out_res = portal_api.sign_out()
			self.assertTrue(out_res["signed_out"])
			self.assertIsNone(self._extract_cookie_value(cm_out))

			self._attach_request(cookie_value=cust_cookie)
			self.assertFalse(portal_api.get_portal_bootstrap()["authenticated"])
			with self.assertRaises(frappe.AuthenticationError):
				portal_api.get_portal_hub()
		finally:
			frappe.set_user("Administrator")
			self._track_session_artifacts(phone_e164=phone, ro_name=ro1.name)
			self._track_session_artifacts(ro_name=ro2.name)

	def test_quote_approval_pin_flow_and_staff_reveal(self):
		"""Verify quote approval does NOT prompt for PIN, while appointment hub prompts and supports submit/reveal/opt-out."""
		phone = "+14165550203"
		customer = self.create_test_customer(mobile_no=phone, email_id="pin203@example.test")
		vehicle = self.create_test_vehicle(customer=customer.name, license_plate="PIN203")
		ro = self._create_ro(customer=customer.name, vehicle=vehicle.name)
		svc_name = ro.services[0].name
		ro_token, _ = get_or_create_approval_token(ro.name)
		tech_user = self._create_user_with_role("portal_tech", "ISM Technician")
		tech_emp = frappe.get_doc({
			"doctype": "Employee",
			"first_name": "Portal Tech",
			"employee_name": "Portal Tech",
			"status": "Active",
			"designation": "Technician",
			"company": self.get_manifest().company,
			"gender": "Male",
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2020-01-01",
			"user_id": tech_user,
		}).insert(ignore_permissions=True)
		self.track(tech_emp)
		ro.services[0].db_set("assigned_technician", tech_emp.name, update_modified=False)

		frappe.set_user("Guest")
		try:
			cm = self._attach_request(cookie_value=None)
			ex = portal_api.exchange_portal_link(token=ro_token)
			self.assertTrue(ex["valid"])
			cookie = self._extract_cookie_value(cm)

			self._attach_request(cookie_value=cookie)
			with patch("frappe.publish_realtime") as mock_rt:
				app_res = portal_api.submit_portal_quote_approval(
					repair_order=ro.name,
					approvals=json.dumps({svc_name: "Approved"}),
					signer_name="Taylor Owner",
					signature_data=TINY_PNG_DATA_URI,
				)
			self.assertTrue(app_res["success"])
			self.assertEqual(app_res["approval_status"], "Approved")
			self.assertTrue(app_res["audit_hash"])
			# PIN is requested at appointment confirmation, NOT at quote authorization
			self.assertFalse(app_res["pin_prompt_required"])
			self.assertFalse(app_res["hub"]["active_visit"]["pin_to_drive"]["prompt_required"])
			self.assertEqual(find_forbidden_portal_keys(app_res), [])

			rt_events = [c.args[0] for c in mock_rt.call_args_list if c.args]
			self.assertIn("ism_ro_approval_updated", rt_events)
			self.assertIn("ism_portal_event", rt_events)

			# Voluntary PIN submit on active RO still works with leading zero; NEVER echoed in customer payload!
			pin_res = portal_api.submit_pin_to_drive(repair_order=ro.name, pin="0427")
			self.assertTrue(pin_res["success"])
			self.assertEqual(pin_res["pin_to_drive"]["status"], PIN_STATUS_PROVIDED)
			self.assertEqual(pin_res["pin_to_drive"]["masked_display"], "••••")
			self.assertFalse(pin_res["pin_to_drive"]["prompt_required"])
			self.assertNotIn("0427", json.dumps(pin_res))
			self.assertEqual(find_forbidden_portal_keys(pin_res), [])

			# Guest cannot call staff reveal_ro_pin
			with self.assertRaises(frappe.PermissionError):
				portal_api.reveal_ro_pin(repair_order=ro.name)

			# Authorized staff reveal succeeds with auto_mask_seconds=15
			frappe.set_user(tech_user)
			reveal = portal_api.reveal_ro_pin(repair_order=ro.name)
			self.assertTrue(reveal["available"])
			self.assertEqual(reveal["pin"], "0427")
			self.assertEqual(reveal["auto_mask_seconds"], 15)

			# Customer opt-out erases __Auth ciphertext
			frappe.set_user("Guest")
			self._attach_request(cookie_value=cookie)
			opt_res = portal_api.opt_out_pin_to_drive(repair_order=ro.name)
			self.assertTrue(opt_res["success"])
			self.assertEqual(opt_res["pin_to_drive"]["status"], PIN_STATUS_OPTED_OUT)
			self.assertIsNone(opt_res["pin_to_drive"]["masked_display"])
			auth_rows = frappe.db.sql(
				"SELECT COUNT(*) FROM `__Auth` WHERE doctype='Repair Order' AND name=%s AND fieldname='pin_to_drive_encrypted'",
				(ro.name,),
			)[0][0]
			self.assertEqual(auth_rows, 0)
		finally:
			frappe.set_user("Administrator")
			self._track_session_artifacts(phone_e164=phone, ro_name=ro.name)

	def test_appointment_confirmation_booking_pin_flow(self):
		"""Verify confirmed/scheduled booking prompts for PIN-to-Drive in portal hub, supports submit/reveal/opt-out."""
		phone = "+14165550299"
		customer = self.create_test_customer(mobile_no=phone, email_id="pin299@example.test")
		vehicle = self.create_test_vehicle(customer=customer.name, license_plate="PIN299")
		bay = self._create_bay()
		se = self._create_schedule_entry(
			customer=customer.name,
			vehicle=vehicle.name,
			bay=bay,
			scheduled_date=add_days(nowdate(), 4),
			scheduled_time="08:00:00",
			status="Confirmed",
			provisional_phone=phone,
		)
		booking_url = create_booking_portal_link(se.name)
		booking_token = parse_qs(urlparse(booking_url).query)["booking_token"][0]
		tech_user = self._create_user_with_role("portal_se_tech", "ISM Technician")
		tech_emp = frappe.get_doc({
			"doctype": "Employee",
			"first_name": "Portal SE Tech",
			"employee_name": "Portal SE Tech",
			"status": "Active",
			"designation": "Technician",
			"company": self.get_manifest().company,
			"gender": "Male",
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2020-01-01",
			"user_id": tech_user,
		}).insert(ignore_permissions=True)
		self.track(tech_emp)
		se.db_set("assigned_technician", tech_emp.name, update_modified=False)

		frappe.set_user("Guest")
		try:
			cm = self._attach_request(cookie_value=None)
			ex = portal_api.exchange_portal_link(booking_token=booking_token)
			self.assertTrue(ex["valid"])
			cookie = self._extract_cookie_value(cm)

			active_visit = ex["hub"]["active_visit"]
			self.assertEqual(active_visit["kind"], "booking")
			self.assertEqual(active_visit["schedule_entry"], se.name)
			self.assertIsNotNone(active_visit["pin_to_drive"])
			self.assertTrue(active_visit["pin_to_drive"]["prompt_required"])
			self.assertIn("PIN-to-Drive Requested", ex["hub"]["visits"][0]["action_required_badges"])
			self.assertEqual(find_forbidden_portal_keys(ex), [])

			self._attach_request(cookie_value=cookie)
			pin_res = portal_api.submit_pin_to_drive(schedule_entry=se.name, pin="0915")
			self.assertTrue(pin_res["success"])
			self.assertEqual(pin_res["pin_to_drive"]["status"], PIN_STATUS_PROVIDED)
			self.assertEqual(pin_res["pin_to_drive"]["masked_display"], "••••")
			self.assertFalse(pin_res["pin_to_drive"]["prompt_required"])
			self.assertNotIn("PIN-to-Drive Requested", pin_res["hub"]["visits"][0]["action_required_badges"])
			self.assertNotIn("0915", json.dumps(pin_res))
			self.assertEqual(find_forbidden_portal_keys(pin_res), [])

			# Staff reveal on Schedule Entry works for ISM Technician
			frappe.set_user(tech_user)
			reveal = portal_api.reveal_schedule_entry_pin(schedule_entry=se.name)
			self.assertTrue(reveal["available"])
			self.assertEqual(reveal["pin"], "0915")
			self.assertEqual(reveal["auto_mask_seconds"], 15)

			# Opt-out via schedule_entry erases __Auth ciphertext
			frappe.set_user("Guest")
			self._attach_request(cookie_value=cookie)
			opt_res = portal_api.opt_out_pin_to_drive(schedule_entry=se.name)
			self.assertTrue(opt_res["success"])
			self.assertEqual(opt_res["pin_to_drive"]["status"], PIN_STATUS_OPTED_OUT)
			auth_rows = frappe.db.sql(
				"SELECT COUNT(*) FROM `__Auth` WHERE doctype='Schedule Entry' AND name=%s AND fieldname='pin_to_drive_encrypted'",
				(se.name,),
			)[0][0]
			self.assertEqual(auth_rows, 0)
		finally:
			frappe.set_user("Administrator")
			for item_name in frappe.get_all(
				"Shop Action Item",
				filters={"reference_doctype": "Schedule Entry", "reference_name": se.name},
				pluck="name",
			):
				self.track("Shop Action Item", item_name)
			self._track_session_artifacts(phone_e164=phone)

	def test_reschedule_options_and_execution_with_silent_offset_sms(self):
		"""Verify P3 reschedule slots & execution with 08:00->07:45 silent drop-off offset and Mock SMS."""
		phone = "+14165550204"
		customer = self.create_test_customer(mobile_no=phone, email_id="resched204@example.test")
		vehicle = self.create_test_vehicle(customer=customer.name, license_plate="RSC204")
		bay = self._create_bay()
		orig_date = add_days(nowdate(), 4)
		se = self._create_schedule_entry(
			customer=customer.name,
			vehicle=vehicle.name,
			bay=bay,
			scheduled_date=orig_date,
			scheduled_time="08:00:00",
			status="Scheduled",
		)
		booking_url = create_booking_portal_link(se.name)
		booking_token = parse_qs(urlparse(booking_url).query)["booking_token"][0]

		frappe.set_user("Guest")
		try:
			cm = self._attach_request(cookie_value=None)
			ex = portal_api.exchange_portal_link(booking_token=booking_token)
			self.assertTrue(ex["valid"])
			cookie = self._extract_cookie_value(cm)

			self._attach_request(cookie_value=cookie)
			opts = portal_api.get_reschedule_options(
				schedule_entry=se.name,
				start_date=add_days(nowdate(), 2),
				days_ahead=7,
			)
			self.assertEqual(opts["schedule_entry"], se.name)
			self.assertTrue(opts["eligibility"]["allowed"])
			self.assertTrue(opts["slots"])
			self.assertTrue(opts["slots_by_date"])
			self.assertEqual(find_forbidden_portal_keys(opts), [])

			# Find a 07:45 drop-off slot on a different date (corresponds to 08:00 internal capacity)
			target_slot = next(
				(
					s
					for s in opts["slots"]
					if s["date"] != orig_date and s["customer_arrival_time"].startswith("07:45")
				),
				opts["slots"][0],
			)
			self.assertIn("display_date", target_slot)
			self.assertIn("display_time", target_slot)
			self.assertTrue(target_slot["available"])

			clear_mock_sms_outbox()
			with patch("frappe.publish_realtime") as mock_rt:
				res = portal_api.reschedule_appointment(
					schedule_entry=se.name,
					target_date=target_slot["date"],
					customer_arrival_time=target_slot["customer_arrival_time"],
					reason="Running late",
				)

			self.assertTrue(res["success"])
			self.assertTrue(res["sms_sent"])
			self.assertEqual(res["appointment"]["appointment_date"], target_slot["date"])
			self.assertEqual(
				res["appointment"]["customer_arrival_time"],
				target_slot["customer_arrival_time"],
			)
			self.assertEqual(find_forbidden_portal_keys(res), [])

			# Confirm Seam 5 portal realtime emitted
			portal_rt = [
				c.args[1]
				for c in mock_rt.call_args_list
				if c.args and c.args[0] == "ism_portal_event"
			]
			self.assertTrue(any(p.get("event_type") == "appointment_rescheduled" for p in portal_rt))

			# Confirm Mock SMS outbox contains ONLY customer drop-off time, ZERO buffer/offset/internal wording
			outbox = get_mock_sms_outbox()
			self.assertEqual(len(outbox), 1)
			sms_body = outbox[0]["body"]
			self.assertIn(res["appointment"]["display_time"], sms_body)
			for forbidden_word in ("buffer", "offset", "leeway", "08:00", "8:00 AM"):
				if target_slot["customer_arrival_time"].startswith("07:45"):
					self.assertNotIn(forbidden_word, sms_body.lower())

			# Verify DB stored internal time = arrival + 15 minutes
			se.reload()
			self.assertEqual(str(se.scheduled_date), target_slot["date"])
			if target_slot["customer_arrival_time"].startswith("07:45"):
				self.assertEqual(se.scheduled_time, timedelta(hours=8, minutes=0))
		finally:
			frappe.set_user("Administrator")
			self._track_session_artifacts(phone_e164=phone)
			for name in frappe.get_all(
				"Shop Action Item",
				filters={"reference_doctype": "Schedule Entry", "reference_name": se.name},
				pluck="name",
			):
				self.track("Shop Action Item", name)

	def test_square_deposit_unpaid_invoice_pdf_email_and_balance_payment(self):
		"""Verify inline Mock Square deposit + unpaid PDF download/self-email + balance payment reconciliation."""
		phone = "+14165550205"
		customer = self.create_test_customer(
			mobile_no=phone,
			email_id="billing205@example.test",
		)
		vehicle = self.create_test_vehicle(customer=customer.name, license_plate="PAY205")
		ro = self._create_ro(
			customer=customer.name,
			vehicle=vehicle.name,
			status="Authorized - In Progress",
			approval_status="Approved",
			service_status="Approved",
			labor_hours=2.0,
			labor_rate=100.0,
		)
		ro_token, _ = get_or_create_approval_token(ro.name)
		create_deposit_request(ro.name, 100.0, note="Parts order deposit")
		frappe.db.commit()

		frappe.set_user("Guest")
		try:
			cm = self._attach_request(cookie_value=None)
			ex = portal_api.exchange_portal_link(token=ro_token)
			self.assertTrue(ex["valid"])
			self.assertEqual(
				ex["hub"]["active_visit"]["payment"]["state"]["portal_payment_badge"],
				"Manual Deposit Requested",
			)
			cookie = self._extract_cookie_value(cm)

			# PCI raw card rejection on form_dict
			self._attach_request(cookie_value=cookie)
			with patch.object(
				frappe.local,
				"form_dict",
				frappe._dict({"card_number": "4242424242424242"}),
			):
				with self.assertRaises(frappe.ValidationError):
					portal_api.submit_portal_payment(
						repair_order=ro.name,
						payment_intent=INTENT_MANUAL_DEPOSIT,
						source_id="mock_tok_ok",
					)

			# Client amount override rejection
			frappe.local.form_dict = frappe._dict()
			with self.assertRaises(frappe.ValidationError):
				portal_api.submit_portal_payment(
					repair_order=ro.name,
					payment_intent=INTENT_MANUAL_DEPOSIT,
					source_id="mock_tok_ok",
					amount=1.0,
				)

			# Execute Mock Square Manual Deposit ($100.00 CAD)
			dep_res = portal_api.submit_portal_payment(
				repair_order=ro.name,
				payment_intent=INTENT_MANUAL_DEPOSIT,
				source_id="mock_tok_ok",
				idempotency_key=f"portal-dep-{ro.name}",
				expected_amount_cents=10000,
				buyer_email="billing205@example.test",
			)
			self.assertTrue(dep_res["success"])
			self.assertTrue(dep_res["payment_entry"])
			self.assertEqual(dep_res["payment"]["state"]["portal_payment_badge"], "Deposit Paid")
			self.assertEqual(find_forbidden_portal_keys(dep_res), [])

			# Create & link an UNPAID submitted Sales Invoice ($250.00 CAD)
			frappe.set_user("Administrator")
			si = self._create_sales_invoice(customer=customer.name, rate=250.0, submit=True)
			frappe.db.set_value(
				"Repair Order",
				ro.name,
				{"sales_invoice": si.name, "status": "Work Completed"},
				update_modified=False,
			)
			frappe.db.commit()

			# Switch back to Guest: UNPAID invoice MUST unlock immediately for PDF download & self-email
			expected_outstanding = flt(si.outstanding_amount, 2)
			self.assertGreater(expected_outstanding, 0.0)
			frappe.set_user("Guest")
			self._attach_request(cookie_value=cookie)
			hub_unpaid = portal_api.get_portal_hub(active_ro=ro.name)
			inv = hub_unpaid["active_visit"]["invoice"]
			self.assertTrue(inv["available"])
			self.assertFalse(inv["is_paid"])
			self.assertEqual(inv["payment_badge"], "Unpaid")
			self.assertEqual(flt(inv["outstanding_amount"], 2), expected_outstanding)
			self.assertIn("download_invoice_pdf", inv["download_url"])

			# Download PDF on UNPAID invoice + verify hardened headers on live Frappe RPC Response
			frappe.local.response = frappe._dict()
			frappe.local.request.method = "GET"
			with (
				patch("ism.portal.invoices._generate_invoice_pdf_bytes", return_value=FAKE_PDF_BYTES),
				patch.object(
					frappe.local,
					"form_dict",
					frappe._dict({
						"cmd": "ism.api.portal.download_invoice_pdf",
						"repair_order": ro.name,
					}),
				),
			):
				http_resp = frappe_handle_rpc()
			self.assertIsInstance(http_resp, Response)
			self.assertEqual(http_resp.status_code, 200)
			self.assertTrue(http_resp.data.startswith(b"%PDF-"))
			self.assertEqual(http_resp.headers.get("Content-Type"), "application/pdf")
			self.assertIn("attachment", http_resp.headers.get("Content-Disposition", ""))
			self.assertEqual(
				http_resp.headers.get("Cache-Control"),
				"private, no-store, max-age=0",
			)
			self.assertEqual(http_resp.headers.get("X-Content-Type-Options"), "nosniff")
			self.assertEqual(frappe.local.response["type"], "download")
			self.assertTrue(frappe.local.response["filecontent"].startswith(b"%PDF-"))

			# Email UNPAID invoice copy + verify P1 rate-limit propagation (limit=2/hr in setUp)
			with (
				patch("ism.portal.invoices._generate_invoice_pdf_bytes", return_value=FAKE_PDF_BYTES),
				patch("frappe.sendmail") as mock_sendmail,
			):
				em1 = portal_api.email_invoice_copy(repair_order=ro.name)
				em2 = portal_api.email_invoice_copy(repair_order=ro.name, recipient_email="")
				em3 = portal_api.email_invoice_copy(repair_order=ro.name)
			self.assertTrue(em1["success"])
			self.assertTrue(em1["sent"])
			self.assertTrue(em2["success"])
			self.assertEqual(mock_sendmail.call_count, 2)
			self.assertFalse(em3["success"])
			self.assertFalse(em3["sent"])
			self.assertTrue(em3["rate_limited"])
			self.assertGreater(em3["retry_after_seconds"], 0)

			# Pay remaining Sales Invoice balance inline via Mock Square
			bal_res = portal_api.submit_portal_payment(
				repair_order=ro.name,
				payment_intent=INTENT_INVOICE_BALANCE,
				source_id="mock_tok_ok",
				idempotency_key=f"portal-bal-{ro.name}",
				expected_amount_cents=int(round(expected_outstanding * 100)),
			)
			self.assertTrue(bal_res["success"])
			self.assertTrue(bal_res["payment_entry"])
			self.assertEqual(bal_res["payment"]["state"]["portal_payment_badge"], "Paid in Full")
			self.assertTrue(bal_res["invoice"]["is_paid"])
			self.assertEqual(bal_res["invoice"]["payment_badge"], "Paid")
			self.assertEqual(find_forbidden_portal_keys(bal_res), [])
		finally:
			frappe.set_user("Administrator")
			self._track_session_artifacts(phone_e164=phone, ro_name=ro.name)

	def test_closed_lifecycle_locks_and_cross_scope_idor_rejection(self):
		"""Verify Closed/Released locks mutating actions while keeping invoices open, and block all IDORs."""
		cust_a = self.create_test_customer(mobile_no="+14165550206", email_id="a206@example.test")
		veh_a = self.create_test_vehicle(customer=cust_a.name, license_plate="LOCK206")
		ro_a = self._create_ro(
			customer=cust_a.name,
			vehicle=veh_a.name,
			status="Authorized - In Progress",
			approval_status="Approved",
			service_status="Approved",
		)
		svc_a = ro_a.services[0].name
		token_a, _ = get_or_create_approval_token(ro_a.name)
		si_a = self._create_sales_invoice(customer=cust_a.name, rate=180.0, submit=True)

		# Store PIN while active, then close/release RO -> triggers auto-purge
		submit_ro_pin(ro_a.name, "1357")
		ro_a.reload()
		ro_a.sales_invoice = si_a.name
		ro_a.vehicle_released = 1
		ro_a.status = "Closed / Released"
		ro_a.save(ignore_permissions=True)
		ro_a.reload()
		self.assertEqual(ro_a.pin_to_drive_status, PIN_STATUS_PURGED)

		# Customer B resources (for cross-customer IDOR) + sibling RO on Customer A (for cross-scope IDOR)
		ro_a_sibling = self._create_ro(customer=cust_a.name, vehicle=veh_a.name)
		cust_b = self.create_test_customer(mobile_no="+14165550207", email_id="b207@example.test")
		veh_b = self.create_test_vehicle(customer=cust_b.name, license_plate="IDOR207")
		ro_b = self._create_ro(customer=cust_b.name, vehicle=veh_b.name)
		se_b = self._create_schedule_entry(customer=cust_b.name, vehicle=veh_b.name)

		frappe.set_user("Guest")
		try:
			cm = self._attach_request(cookie_value=None)
			ex = portal_api.exchange_portal_link(token=token_a)
			self.assertTrue(ex["valid"])
			cookie_a = self._extract_cookie_value(cm)

			# 1. Closed / Released read-only checks
			self._attach_request(cookie_value=cookie_a)
			hub = portal_api.get_portal_hub(active_ro=ro_a.name)
			self.assertTrue(hub["active_visit"]["is_read_only"])
			self.assertFalse(hub["active_visit"]["quote"]["can_authorize"])
			self.assertEqual(hub["active_visit"]["pin_to_drive"]["status"], PIN_STATUS_PURGED)
			self.assertEqual(find_forbidden_portal_keys(hub), [])

			# Mutating actions blocked on Closed / Released
			with self.assertRaises(frappe.ValidationError):
				portal_api.submit_portal_quote_approval(
					repair_order=ro_a.name,
					approvals={svc_a: "Approved"},
					signer_name="Late Signer",
					signature_data=TINY_PNG_DATA_URI,
				)
			with self.assertRaises(frappe.ValidationError):
				submit_customer_approval(
					token=token_a,
					approvals={svc_a: "Approved"},
					signer_name="Late Signer",
					signature_data=TINY_PNG_DATA_URI,
				)
			with self.assertRaises(frappe.ValidationError):
				portal_api.submit_pin_to_drive(repair_order=ro_a.name, pin="9999")
			with self.assertRaises(frappe.ValidationError):
				portal_api.opt_out_pin_to_drive(repair_order=ro_a.name)

			# Invoice PDF download & self-email remain accessible on Closed / Released
			frappe.local.response = frappe._dict()
			with patch("ism.portal.invoices._generate_invoice_pdf_bytes", return_value=FAKE_PDF_BYTES):
				portal_api.download_invoice_pdf(repair_order=ro_a.name)
			self.assertTrue(frappe.local.response["filecontent"].startswith(b"%PDF-"))

			with (
				patch("ism.portal.invoices._generate_invoice_pdf_bytes", return_value=FAKE_PDF_BYTES),
				patch("frappe.sendmail"),
			):
				em = portal_api.email_invoice_copy(repair_order=ro_a.name)
			self.assertTrue(em["success"])

			# 2. Cross-scope (sibling RO on same customer) and cross-customer IDOR rejection -> PermissionError
			for target_ro in (ro_a_sibling.name, ro_b.name, "RO-NONEXISTENT-99999"):
				with self.assertRaises(frappe.PermissionError):
					portal_api.get_portal_hub(active_ro=target_ro)
				with self.assertRaises(frappe.PermissionError):
					portal_api.submit_portal_quote_approval(
						repair_order=target_ro,
						approvals={"svc": "Approved"},
						signer_name="Attacker",
						signature_data=TINY_PNG_DATA_URI,
					)
				with self.assertRaises(frappe.PermissionError):
					portal_api.submit_pin_to_drive(repair_order=target_ro, pin="1234")
				with self.assertRaises(frappe.PermissionError):
					portal_api.opt_out_pin_to_drive(repair_order=target_ro)
				with self.assertRaises(frappe.PermissionError):
					portal_api.submit_portal_payment(
						repair_order=target_ro,
						payment_intent=INTENT_MANUAL_DEPOSIT,
						source_id="mock_tok_ok",
					)
				with self.assertRaises(frappe.PermissionError):
					portal_api.download_invoice_pdf(repair_order=target_ro)
				with self.assertRaises(frappe.PermissionError):
					portal_api.email_invoice_copy(repair_order=target_ro)

			for target_se in (se_b.name, "SE-NONEXISTENT-99999"):
				with self.assertRaises(frappe.PermissionError):
					portal_api.get_portal_hub(active_schedule_entry=target_se)
				with self.assertRaises(frappe.PermissionError):
					portal_api.submit_pin_to_drive(schedule_entry=target_se, pin="1234")
				with self.assertRaises(frappe.PermissionError):
					portal_api.opt_out_pin_to_drive(schedule_entry=target_se)
				with self.assertRaises(frappe.PermissionError):
					portal_api.get_reschedule_options(schedule_entry=target_se)
				with self.assertRaises(frappe.PermissionError):
					portal_api.reschedule_appointment(
						schedule_entry=target_se,
						target_date=add_days(nowdate(), 5),
						customer_arrival_time="08:45:00",
					)
		finally:
			frappe.set_user("Administrator")
			self._track_session_artifacts(phone_e164="+14165550206", ro_name=ro_a.name)
			self._track_session_artifacts(ro_name=ro_a_sibling.name)
			self._track_session_artifacts(phone_e164="+14165550207", ro_name=ro_b.name)

	def test_portal_vehicle_card_and_visit_hub_pills_vehicle_display(self):
		"""Portal vehicle card & visit hub pills include color, label, compact_label, and vehicle_info."""
		from ism.ism.doctype.repair_vehicle.vin_decoder import compute_vin_check_digit

		cust = self.create_test_customer(mobile_no="+14165550299")
		template = "7SAYGDEE0PF889900"  # 2023 Model Y
		vin = f"{template[:8]}{compute_vin_check_digit(template)}{template[9:]}"
		veh = self.create_test_vehicle(
			vin=vin,
			customer=cust.name,
			color="Quicksilver",
			license_plate="prt 900",
		)
		ro = self._create_ro(customer=cust.name, vehicle=veh.name)

		sess = issue_portal_session(
			auth_method="sms_otp",
			scope_type="customer",
			customer=cust.name,
			phone_e164="+14165550299",
			set_response_cookie=False,
		)
		self._track_session_artifacts(phone_e164="+14165550299", ro_name=ro.name)
		hub = build_portal_hub_payload(sess)
		expected_label = "2023 Quicksilver Model Y (PRT 900)"
		expected_compact = "Quicksilver Model Y"

		vehicle_card = hub["active_visit"]["vehicle"]
		self.assertEqual(vehicle_card["color"], "Quicksilver")
		self.assertEqual(vehicle_card["license_plate"], "PRT 900")
		self.assertEqual(vehicle_card["label"], expected_label)
		self.assertEqual(vehicle_card["compact_label"], expected_compact)
		self.assertEqual(vehicle_card["display"], expected_label)
		self.assertEqual(vehicle_card["compact_display"], expected_compact)

		visits = hub.get("visits") or []
		self.assertTrue(visits)
		pill = next((v for v in visits if v.get("repair_order") == ro.name), visits[0])
		self.assertEqual(pill["vehicle_label"], expected_label)
		self.assertEqual(pill["vehicle_compact_label"], expected_compact)
		self.assertEqual(pill["vehicle_info"]["color"], "Quicksilver")
		self.assertEqual(pill["vehicle_info"]["license_plate"], "PRT 900")
