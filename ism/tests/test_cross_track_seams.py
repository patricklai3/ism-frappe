# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Unmocked cross-track integration tests proving Seams 1-6 across Projects P1-P6."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import frappe
from frappe.auth import CookieManager
from frappe.utils import add_days, flt, getdate, nowdate
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

import ism.api.agent_quotes as agent_quotes_api
import ism.api.board as board_api
import ism.api.booking as booking_api
import ism.api.portal as portal_api
from ism.api.sms import dispatch_sms
from ism.board.cockpit_view import get_cockpit_view
from ism.board.projections import ensure_thread_for_ro
from ism.booking.anti_abuse import clear_booking_rate_limits, issue_booking_form_nonce
from ism.comms.contracts import DEFAULT_PORTAL_COOKIE_NAME
from ism.comms.otp import clear_comms_rate_limits, get_last_mock_otp
from ism.comms.providers import clear_mock_sms_outbox, get_mock_sms_outbox
from ism.comms.sessions import issue_portal_session
from ism.payments.contracts import INTENT_MANUAL_DEPOSIT, SQUARE_CURRENCY, SQUARE_ENV_MOCK
from ism.payments.deposit_requests import create_deposit_request
from ism.payments.square_client import MockSquareClient
from ism.portal.auth import clear_portal_auth_rate_limits
from ism.portal.contracts import PIN_STATUS_PROVIDED
from ism.scheduling.arrival_policy import to_customer_arrival_time
from ism.scheduling.time_utils import invalidate_shop_settings_cache
from ism.setup_tasks.seed import ensure_square_bank_account_and_mop, setup_default_ism_settings
from ism.tests.base import ISMTestCase

TINY_PNG_DATA_URI = (
	"data:image/png;base64,"
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _RecordingCookieManager(CookieManager):
	def __init__(self) -> None:
		self.recorded: dict[str, dict[str, Any]] = {}

	def set_cookie(self, key: str, value: str, **kwargs: Any) -> None:
		self.recorded[key] = {"value": value, **kwargs}


class TestCrossTrackSeams(ISMTestCase):
	"""Verify Seams 1-6 end-to-end without mocking Track A <-> Track B boundaries."""

	_SETTINGS_FIELDS: tuple[str, ...] = (
		"enable_customer_portal",
		"enable_self_serve_booking",
		"customer_arrival_offset_minutes",
		"sms_gateway_provider",
		"sms_enforce_opt_out",
		"portal_session_signing_secret",
		"portal_session_cookie_name",
		"portal_session_cookie_samesite",
		"portal_session_secure_cookie",
		"square_environment",
		"square_currency",
		"square_application_id",
		"square_location_id",
		"board_ai_provider",
		"board_ai_model",
		"board_ai_auto_process",
		"self_serve_horizon_days",
		"self_serve_min_lead_time_minutes",
		"self_serve_max_packages_per_booking",
		"self_serve_booking_ip_rate_limit_per_hour",
		"self_serve_booking_phone_rate_limit_per_day",
		"self_serve_honeypot_enabled",
	)

	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")
		self._saved_user = frappe.session.user
		self._had_request = hasattr(frappe.local, "request")
		self._prev_request = getattr(frappe.local, "request", None)
		self._had_request_ip = hasattr(frappe.local, "request_ip")
		self._prev_request_ip = getattr(frappe.local, "request_ip", None)
		self._had_cookie_manager = hasattr(frappe.local, "cookie_manager")
		self._prev_cookie_manager = getattr(frappe.local, "cookie_manager", None)

		settings = frappe.get_single("ISM Settings")
		self._saved_settings = {field: settings.get(field) for field in self._SETTINGS_FIELDS}

		self._save_settings(
			{
				"enable_customer_portal": 1,
				"enable_self_serve_booking": 1,
				"customer_arrival_offset_minutes": 15,
				"sms_gateway_provider": "Mock (Deterministic Test)",
				"sms_enforce_opt_out": 1,
				"portal_session_signing_secret": "cross-track-seams-test-secret-key-0123456789abcdef",
				"portal_session_cookie_name": DEFAULT_PORTAL_COOKIE_NAME,
				"portal_session_cookie_samesite": "Lax",
				"portal_session_secure_cookie": 1,
				"square_environment": SQUARE_ENV_MOCK,
				"square_currency": SQUARE_CURRENCY,
				"square_application_id": "sq0idp-mock-cross-track",
				"square_location_id": "LMOCK00000001",
				"board_ai_provider": "Mock (Deterministic Test)",
				"board_ai_auto_process": 0,
				"self_serve_horizon_days": 30,
				"self_serve_min_lead_time_minutes": 0,
				"self_serve_max_packages_per_booking": 5,
				"self_serve_booking_ip_rate_limit_per_hour": 50,
				"self_serve_booking_phone_rate_limit_per_day": 10,
				"self_serve_honeypot_enabled": 1,
			}
		)
		ensure_square_bank_account_and_mop()
		clear_mock_sms_outbox()
		clear_comms_rate_limits()
		clear_portal_auth_rate_limits()
		clear_booking_rate_limits()
		MockSquareClient.reset_store()

	def tearDown(self) -> None:
		try:
			frappe.set_user("Administrator")
			clear_mock_sms_outbox()
			clear_comms_rate_limits()
			clear_portal_auth_rate_limits()
			clear_booking_rate_limits()
			MockSquareClient.reset_store()

			if self._had_cookie_manager:
				frappe.local.cookie_manager = self._prev_cookie_manager
			elif hasattr(frappe.local, "cookie_manager"):
				delattr(frappe.local, "cookie_manager")

			frappe.local.request_ip = self._prev_request_ip

			if self._had_request:
				frappe.local.request = self._prev_request
			elif hasattr(frappe.local, "request"):
				delattr(frappe.local, "request")

			super().tearDown()
		finally:
			frappe.set_user("Administrator")
			self._save_settings(self._saved_settings)
			frappe.set_user(self._saved_user)

	def _save_settings(self, updates: dict[str, Any]) -> None:
		settings = frappe.get_single("ISM Settings")
		for field, value in updates.items():
			settings.set(field, value)
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.db.commit()
		frappe.clear_cache(doctype="ISM Settings")
		invalidate_shop_settings_cache()

	def _create_user_with_roles(self, prefix: str, roles: list[str]) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.test"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": prefix,
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": r} for r in roles],
			}
		).insert(ignore_permissions=True)
		self.track(user)
		return email

	def _attach_portal_request(self, cookie_value: str | None = None) -> _RecordingCookieManager:
		cm = _RecordingCookieManager()
		frappe.local.cookie_manager = cm
		headers = {"User-Agent": "ISM-CrossTrack-Test/1.0"}
		if cookie_value:
			headers["Cookie"] = f"{DEFAULT_PORTAL_COOKIE_NAME}={cookie_value}"
		builder = EnvironBuilder(
			path="/portal",
			method="GET",
			base_url="https://portal.example.test",
			headers=headers,
			environ_base={"REMOTE_ADDR": "198.51.100.77"},
		)
		frappe.local.request = Request(builder.get_environ())
		frappe.local.request_ip = "198.51.100.77"
		return cm

	def _create_ro(
		self,
		*,
		phone: str = "+14165550301",
		email: str = "seam@example.test",
		status: str = "Pending Authorization",
		approval_status: str = "Pending Approval",
		service_status: str = "Pending Approval",
		labor_hours: float = 2.0,
		labor_rate: float = 150.0,
	):
		customer = self.create_test_customer()
		customer.db_set("mobile_no", phone, update_modified=False)
		customer.db_set("email_id", email, update_modified=False)
		vehicle = self.create_test_vehicle(
			customer=customer.name,
			model_year=2023,
			model="Model 3",
			manufacturer="Tesla",
		)
		ro = self.create_test_repair_order(
			customer.name,
			vehicle.name,
			status=status,
			approval_status=approval_status,
			services=[
				{
					"service_title": "Front Brake Service",
					"status": service_status,
					"labor_hours": labor_hours,
					"labor_rate": labor_rate,
					"labor_amount": labor_hours * labor_rate,
					"parts_total": 0.0,
					"total_amount": labor_hours * labor_rate,
				}
			],
		)
		return ro, customer, vehicle

	def _next_weekday(self, offset_days: int = 3) -> str:
		day = getdate(add_days(nowdate(), offset_days))
		while day.weekday() >= 5:
			day = getdate(add_days(day, 1))
		return str(day)

	def _track_action_item_and_events(self, sai_name: str) -> None:
		self.track("Shop Action Item", sai_name)
		for ev_name in frappe.get_all(
			"Shop Board Event",
			filters={"idempotency_key": ["like", f"action_item:{sai_name}%"]},
			pluck="name",
		):
			self.track("Shop Board Event", ev_name)

	def test_seam1_ism_settings_13_sections_and_seed_idempotency(self) -> None:
		settings_path = (
			Path(frappe.get_app_path("ism"))
			/ "ism"
			/ "doctype"
			/ "ism_settings"
			/ "ism_settings.json"
		)
		schema = json.loads(settings_path.read_text(encoding="utf-8"))
		fieldnames = [f["fieldname"] for f in schema["fields"]]
		sections = [f["fieldname"] for f in schema["fields"] if f["fieldtype"] == "Section Break"]

		self.assertEqual(len(fieldnames), len(set(fieldnames)))
		self.assertEqual(len(sections), 13)

		self._save_settings(
			{
				"enable_self_serve_booking": 0,
				"customer_arrival_offset_minutes": 0,
				"board_ai_model": "gemini-3.8-flash",
			}
		)
		setup_default_ism_settings()
		reloaded = frappe.get_single("ISM Settings")
		self.assertEqual(int(reloaded.enable_self_serve_booking), 0)
		self.assertEqual(int(reloaded.customer_arrival_offset_minutes), 0)
		self.assertEqual(reloaded.board_ai_model, "gemini-3.8-flash")

	def test_seam2_p1_sms_bus_audit_and_opt_out(self) -> None:
		phone_ok = "+14165550901"
		phone_opted_out = "+14165550911"
		ro, _, _ = self._create_ro(
			phone=phone_ok,
			status="Authorized - In Progress",
			approval_status="Approved",
			service_status="Approved",
		)

		res_ok = dispatch_sms(
			phone_ok,
			"Your Tesla front brake service is ready.",
			message_type="Notification",
			reference_doctype="Repair Order",
			reference_name=ro.name,
		)
		self.assertTrue(res_ok["success"])
		self.assertFalse(res_ok["opted_out"])
		self.assertEqual(res_ok["status"], "Sent")

		outbox = get_mock_sms_outbox()
		self.assertTrue(any(m.get("phone_e164") == phone_ok for m in outbox))

		audit_logs = frappe.get_all(
			"SMS Audit Log",
			filters={"phone_e164": phone_ok},
			fields=["name", "provider", "status", "reference_name"],
		)
		self.assertGreaterEqual(len(audit_logs), 1)
		for row in audit_logs:
			self.track("SMS Audit Log", row["name"])
		self.assertEqual(audit_logs[0]["provider"], "Mock (Deterministic Test)")
		self.assertEqual(audit_logs[0]["status"], "Sent")
		self.assertEqual(audit_logs[0]["reference_name"], ro.name)

		opt_doc = frappe.get_doc(
			{
				"doctype": "SMS Opt Out",
				"phone_e164": phone_opted_out,
				"is_opted_out": 1,
				"source": "Telnyx STOP Keyword",
				"last_keyword": "STOP",
			}
		).insert(ignore_permissions=True)
		self.track(opt_doc)

		res_blocked = dispatch_sms(
			phone_opted_out,
			"Second message after STOP",
			message_type="Notification",
			reference_doctype="Repair Order",
			reference_name=ro.name,
		)
		self.assertFalse(res_blocked["success"])
		self.assertTrue(res_blocked["opted_out"])
		self.assertFalse(any(m.get("phone_e164") == phone_opted_out for m in get_mock_sms_outbox()))

		for log_name in frappe.get_all("SMS Audit Log", filters={"phone_e164": phone_opted_out}, pluck="name"):
			self.track("SMS Audit Log", log_name)

	def test_seam3_online_square_deposit_and_agent_os_checkout_reconcile_erpnext(self) -> None:
		mgr_user = self._create_user_with_roles(
			"seam3_mgr",
			["ISM Manager", "Accounts Manager", "Sales Manager", "Stock User"],
		)
		phone = "+14165550902"
		ro, customer, _ = self._create_ro(
			phone=phone,
			email="seam3@example.test",
			status="Authorized - In Progress",
			approval_status="Approved",
			service_status="Approved",
			labor_hours=2.0,
			labor_rate=150.0,
		)
		ro.db_set({"odometer_in": 40000, "odometer_out": 40010}, update_modified=False)

		dep_req = create_deposit_request(ro.name, 100.0, note="Online Square deposit")
		self.assertEqual(dep_req["deposit_request"]["status"], "Requested")
		frappe.db.commit()

		cm = self._attach_portal_request()
		sess = issue_portal_session(
			auth_method="magic_link",
			scope_type="repair_order",
			customer=customer.name,
			phone_e164=phone,
			repair_order=ro.name,
			set_response_cookie=True,
		)
		self.track("Customer Portal Session", sess.session_id)
		cookie_val = cm.recorded[DEFAULT_PORTAL_COOKIE_NAME]["value"]

		frappe.set_user("Guest")
		self._attach_portal_request(cookie_value=cookie_val)
		pay_res = portal_api.submit_portal_payment(
			repair_order=ro.name,
			payment_intent=INTENT_MANUAL_DEPOSIT,
			source_id="mock_tok_ok",
			idempotency_key=f"seam3-sq-{ro.name}",
			expected_amount_cents=10000,
			buyer_email="seam3@example.test",
		)
		self.assertTrue(pay_res["success"])
		sq_pe = pay_res["payment_entry"]
		self.assertTrue(sq_pe)

		frappe.set_user("Administrator")
		for tx_name in frappe.get_all("ISM Payment Transaction", filters={"repair_order": ro.name}, pluck="name"):
			self.track("ISM Payment Transaction", tx_name)
		self.track("Payment Entry", sq_pe)
		for sai_name in frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name},
			pluck="name",
		):
			self._track_action_item_and_events(sai_name)

		frappe.set_user(mgr_user)
		res_chk = agent_quotes_api.checkout_repair_order(
			repair_order=ro.name,
			payment_method="Debit / Interac",
			odometer_out=40015,
			confirm=True,
		)
		self.assertTrue(res_chk["success"])
		chk_data = res_chk["data"]
		si_name = chk_data["sales_invoice"]
		floor_pe = chk_data.get("payment_entry")
		self.assertTrue(si_name)
		self.assertTrue(floor_pe)

		frappe.set_user("Administrator")
		self.track("Sales Invoice", si_name)
		self.track("Payment Entry", floor_pe)

		si = frappe.get_doc("Sales Invoice", si_name)
		self.assertEqual(si.docstatus, 1)
		self.assertAlmostEqual(flt(si.outstanding_amount), 0.0, places=2)
		self.assertAlmostEqual(flt(si.total_advance), 100.0, places=2)
		self.assertAlmostEqual(flt(chk_data["deposit_allocated"]), 100.0, places=2)

		ro.reload()
		self.assertEqual(ro.status, "Closed / Released")
		self.assertAlmostEqual(flt(ro.paid_amount), flt(si.grand_total), places=2)
		self.assertAlmostEqual(flt(ro.outstanding_amount), 0.0, places=2)

		sq_pe_doc = frappe.get_doc("Payment Entry", sq_pe)
		floor_pe_doc = frappe.get_doc("Payment Entry", floor_pe)
		self.assertAlmostEqual(flt(sq_pe_doc.paid_amount), 100.0, places=2)
		self.assertAlmostEqual(
			flt(sq_pe_doc.paid_amount) + flt(floor_pe_doc.paid_amount),
			flt(si.grand_total),
			places=2,
		)

	def test_seam4_unmocked_arrival_offset_parity_and_cockpit_internal_time(self) -> None:
		# P3 arrival_policy converts internal 08:00:00 -> customer arrival 07:45:00
		self.assertEqual(to_customer_arrival_time("08:00:00"), "07:45:00")
		self.assertEqual(to_customer_arrival_time("08:00:00")[:5], "07:45")

		bay_name = self.get_manifest().bays["main_lift"]

		target_day = self._next_weekday(3)
		ro, customer, vehicle = self._create_ro(
			phone="+14165550903",
			status="Authorized - In Progress",
			approval_status="Approved",
			service_status="Approved",
		)
		se = self.create_test_schedule_entry(
			customer=customer.name,
			vehicle=vehicle.name,
			repair_order=ro.name,
			service_bay=bay_name,
			scheduled_date=target_day,
			scheduled_time="08:00:00",
			estimated_duration=60,
			status="Scheduled",
		)

		view = get_cockpit_view(date=target_day)
		bay_lane = next(l for l in view["swimlane"]["bay_lanes"] if l["id"] == bay_name)
		block = next(b for b in bay_lane["blocks"] if b["name"] == se.name)
		self.assertEqual(block["start_clock"], "08:00")

	def test_seam5_track_a_action_items_insert_repeat_updates_and_zero_cockpit_echo(self) -> None:
		phone = "+14165550904"
		self.get_manifest()
		start_day = self._next_weekday(5)

		pkg = frappe.get_doc(
			{
				"doctype": "Service Package",
				"package_name": f"Seam5 Tire Rotation {frappe.generate_hash(length=5)}",
				"applicable_model": "All Models",
				"standard_labor_hours": 1.0,
				"allow_self_serve_booking": 1,
				"self_serve_duration_minutes": 60,
				"customer_title": "Tire Rotation",
				"customer_price_display_mode": "Exact Price",
				"customer_estimate_min": 99.0,
			}
		).insert(ignore_permissions=True)
		self.track(pkg)

		# 1. P5 unlinked self-serve booking creates Shop Action Item with
		# clearing_condition_type='schedule_converted_to_ro' and ZERO board thread/event
		self._attach_portal_request()
		nonce = issue_booking_form_nonce(now_ts=int(time.time()) - 10)
		frappe.set_user("Guest")
		slots_res = booking_api.get_booking_slots(
			package_ids=[pkg.name],
			start_date=start_day,
			days_ahead=7,
			vehicle_model="Model 3",
			vehicle_year=2023,
		)
		self.assertTrue(slots_res["success"])
		self.assertGreaterEqual(slots_res["total_slots"], 1)
		chosen_day = slots_res["days"][0]
		target_day = chosen_day["date"]
		arrival_hhmm = chosen_day["slots"][0]["customer_arrival_hhmm"]

		otp_res = booking_api.request_booking_otp(
			package_ids=[pkg.name],
			target_date=target_day,
			customer_arrival_time=arrival_hhmm,
			customer_name="Seam5 Driver",
			phone=phone,
			email="seam5@example.test",
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
			website_url="",
		)
		self.assertTrue(otp_res["success"])
		otp_code = get_last_mock_otp(phone)

		with patch("frappe.sendmail"):
			book_res = booking_api.confirm_self_serve_booking(
				challenge_id=otp_res["challenge_id"],
				otp_code=otp_code,
				package_ids=[pkg.name],
				target_date=target_day,
				customer_arrival_time=arrival_hhmm,
				customer_name="Seam5 Driver",
				phone=phone,
				email="seam5@example.test",
				vehicle_model="Model 3",
				vehicle_year=2023,
				form_nonce=nonce,
				website_url="",
				idempotency_key=f"seam5-book-{frappe.generate_hash(length=6)}",
			)
		self.assertTrue(book_res["success"])
		se_name = book_res["booking_reference"]

		frappe.set_user("Administrator")
		self.track("Schedule Entry", se_name)
		for dt in ("Customer Portal Session", "Portal OTP Challenge", "SMS Audit Log"):
			for n in frappe.get_all(dt, filters={"phone_e164": phone}, pluck="name"):
				self.track(dt, n)

		booking_sais = frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Schedule Entry", "reference_name": se_name},
			fields=["name", "clearing_condition_type", "status"],
		)
		self.assertEqual(len(booking_sais), 1)
		self._track_action_item_and_events(booking_sais[0]["name"])
		self.assertEqual(booking_sais[0]["clearing_condition_type"], "schedule_converted_to_ro")
		self.assertEqual(booking_sais[0]["status"], "Open")

		# Zero board thread or event created for unlinked self-serve booking
		self.assertIsNone(
			frappe.db.get_value(
				"Shop Board Event",
				{"idempotency_key": f"action_item:{booking_sais[0]['name']}"},
				"name",
			)
		)
		self.assertFalse(frappe.db.exists("Shop Board Thread", "SHOP:INTAKE"))

		# Linking the Schedule Entry to an RO auto-clears the booking Shop Action Item!
		converted_ro, _, _ = self._create_ro(phone="+14165550914", email="seam5conv@example.test")
		se_doc = frappe.get_doc("Schedule Entry", se_name)
		se_doc.repair_order = converted_ro.name
		se_doc.save(ignore_permissions=True)
		self.assertEqual(
			frappe.db.get_value("Shop Action Item", booking_sais[0]["name"], "status"),
			"Resolved",
		)
		self.assertEqual(
			int(frappe.db.get_value("Shop Action Item", booking_sais[0]["name"], "auto_cleared")),
			1,
		)

		# 2. P4 quote approval + PIN submit + repeat PIN update (.save()) -> RO:<id> SYSTEM cards
		ro, customer, _ = self._create_ro(phone="+14165550905", email="seam5pin@example.test")
		tkey = ensure_thread_for_ro(ro.name)
		svc_name = ro.services[0].name

		cm = self._attach_portal_request()
		sess = issue_portal_session(
			auth_method="magic_link",
			scope_type="repair_order",
			customer=customer.name,
			phone_e164="+14165550905",
			repair_order=ro.name,
			set_response_cookie=True,
		)
		self.track("Customer Portal Session", sess.session_id)
		cookie_val = cm.recorded[DEFAULT_PORTAL_COOKIE_NAME]["value"]

		frappe.set_user("Guest")
		self._attach_portal_request(cookie_value=cookie_val)
		portal_api.submit_portal_quote_approval(
			repair_order=ro.name,
			approvals=json.dumps({svc_name: "Approved"}),
			signer_name="Seam5 Signer",
			signature_data=TINY_PNG_DATA_URI,
		)
		portal_api.submit_pin_to_drive(repair_order=ro.name, pin="1357")
		count_after_first_pin = frappe.db.count(
			"Shop Board Event", {"thread": tkey, "actor_type": "SYSTEM"}
		)
		self.assertGreaterEqual(count_after_first_pin, 2)

		# Repeat PIN opt-out edits the existing open Shop Action Item -> projects :upd:<hash> card
		portal_api.opt_out_pin_to_drive(repair_order=ro.name)
		count_after_second_pin = frappe.db.count(
			"Shop Board Event", {"thread": tkey, "actor_type": "SYSTEM"}
		)
		self.assertEqual(count_after_second_pin, count_after_first_pin + 1)

		frappe.set_user("Administrator")
		ro_sais = frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name},
			fields=["name", "dedup_key"],
		)
		for row in ro_sais:
			self._track_action_item_and_events(row["name"])

		pin_sai = next(r for r in ro_sais if "pin" in (r["dedup_key"] or "").lower())
		upd_events = frappe.get_all(
			"Shop Board Event",
			filters={"idempotency_key": ["like", f"action_item:{pin_sai['name']}:upd:%"]},
			pluck="name",
		)
		self.assertEqual(len(upd_events), 1)

		# Identical no-op save produces zero extra board events
		pin_doc = frappe.get_doc("Shop Action Item", pin_sai["name"])
		pin_doc.save(ignore_permissions=True)
		self.assertEqual(
			frappe.db.count("Shop Board Event", {"thread": tkey, "actor_type": "SYSTEM"}),
			count_after_second_pin,
		)

		# 3. Cockpit snooze-with-notes and dismiss produce ZERO echo events on /board
		mgr_user = self._create_user_with_roles("seam5_mgr", ["ISM Manager"])
		frappe.set_user(mgr_user)
		board_api.sync_cockpit_action(
			dedup_key=pin_sai["dedup_key"],
			action="snooze",
			snooze_minutes=30,
			notes="Advisor checked PIN",
		)
		board_api.sync_cockpit_action(
			dedup_key=pin_sai["dedup_key"],
			action="dismiss",
			notes="Done checking",
		)
		self.assertEqual(
			frappe.db.count("Shop Board Event", {"thread": tkey, "actor_type": "SYSTEM"}),
			count_after_second_pin,
		)

	def test_seam6_unmocked_pin_visible_on_billed_paid_and_purged_on_closed_released(self) -> None:
		tech_user = self._create_user_with_roles("seam6_tech", ["ISM Technician"])
		unauth_user = self._create_user_with_roles("seam6_guest", ["Guest"])
		phone = "+14165550906"
		ro, customer, _ = self._create_ro(phone=phone, email="seam6@example.test")
		tkey = ensure_thread_for_ro(ro.name)
		svc_name = ro.services[0].name

		cm = self._attach_portal_request()
		sess = issue_portal_session(
			auth_method="magic_link",
			scope_type="repair_order",
			customer=customer.name,
			phone_e164=phone,
			repair_order=ro.name,
			set_response_cookie=True,
		)
		self.track("Customer Portal Session", sess.session_id)
		cookie_val = cm.recorded[DEFAULT_PORTAL_COOKIE_NAME]["value"]

		frappe.set_user("Guest")
		self._attach_portal_request(cookie_value=cookie_val)
		portal_api.submit_portal_quote_approval(
			repair_order=ro.name,
			approvals=json.dumps({svc_name: "Approved"}),
			signer_name="Seam6 Signer",
			signature_data=TINY_PNG_DATA_URI,
		)
		portal_api.submit_pin_to_drive(repair_order=ro.name, pin="0427")

		frappe.set_user("Administrator")
		for sai_name in frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name},
			pluck="name",
		):
			self._track_action_item_and_events(sai_name)

		# 1. Real ISM Technician sees pin_state.can_reveal=True (never plaintext) & reveals via /board
		frappe.set_user(tech_user)
		thread_res = board_api.get_board_thread(thread_key=tkey)
		self.assertNotIn("0427", json.dumps(thread_res))
		self.assertTrue(thread_res["pin_state"]["pin_feature_installed"])
		self.assertTrue(thread_res["pin_state"]["has_pin"])
		self.assertTrue(thread_res["pin_state"]["can_reveal"])
		self.assertEqual(thread_res["pin_state"]["status"], PIN_STATUS_PROVIDED)

		reveal_res = board_api.reveal_ro_pin(repair_order=ro.name)
		self.assertTrue(reveal_res["available"])
		self.assertEqual(reveal_res["pin"], "0427")

		# Unauthorized user blocked
		frappe.set_user(unauth_user)
		with self.assertRaises(frappe.PermissionError):
			board_api.reveal_ro_pin(repair_order=ro.name)

		# 2. Billed / Paid preserves PIN reveal despite board_status == CLOSED
		frappe.set_user("Administrator")
		ro.reload()
		ro.status = "Billed / Paid"
		ro.save(ignore_permissions=True)

		frappe.set_user(tech_user)
		billed_thread = board_api.get_board_thread(thread_key=tkey)
		self.assertEqual(billed_thread["thread"]["board_status"], "CLOSED")
		self.assertTrue(billed_thread["pin_state"]["has_pin"])
		self.assertTrue(billed_thread["pin_state"]["can_reveal"])
		billed_reveal = board_api.reveal_ro_pin(repair_order=ro.name)
		self.assertTrue(billed_reveal["available"])
		self.assertEqual(billed_reveal["pin"], "0427")

		# 3. Closed / Released auto-purges __Auth and immediately hides/blocks reveal
		frappe.set_user("Administrator")
		ro.reload()
		ro.status = "Closed / Released"
		ro.save(ignore_permissions=True)

		auth_rows = frappe.db.sql(
			"SELECT COUNT(*) FROM `__Auth` WHERE doctype='Repair Order' AND name=%s AND fieldname='pin_to_drive_encrypted'",
			(ro.name,),
		)[0][0]
		self.assertEqual(auth_rows, 0)

		frappe.set_user(tech_user)
		closed_thread = board_api.get_board_thread(thread_key=tkey)
		self.assertFalse(closed_thread["pin_state"]["has_pin"])
		self.assertFalse(closed_thread["pin_state"]["can_reveal"])
		with self.assertRaises(frappe.PermissionError):
			board_api.reveal_ro_pin(repair_order=ro.name)
