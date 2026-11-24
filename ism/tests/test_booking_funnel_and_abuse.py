# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import builtins
import json
import re
import sys
import time
import types
from typing import Any
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, today

import ism.booking as booking_facade
from ism.api.booking import (
	confirm_self_serve_booking,
	get_booking_config,
	get_booking_packages,
	get_booking_slots,
	request_booking_otp,
)
from ism.booking.anti_abuse import (
	clear_booking_rate_limits,
	issue_booking_form_nonce,
	record_phone_daily_booking,
)
from ism.booking.contracts import (
	BOOKING_SESSION_AUTH_METHOD,
	BOOKING_SESSION_SCOPE_TYPE,
	BOOKING_SOURCE,
	ERR_BOOKING_DISABLED,
	ERR_BOT_DETECTED,
	ERR_DUPLICATE_BOOKING,
	ERR_EMAIL_REQUIRED,
	ERR_FORM_EXPIRED,
	ERR_MAX_PACKAGES_EXCEEDED,
	ERR_PHONE_DAILY_LIMIT,
	ERR_RATE_LIMITED,
	ERR_SLOT_UNAVAILABLE,
	ERR_SMS_UNCONFIGURED,
	ERR_SUBMISSION_TOO_FAST,
	MAX_FORM_NONCE_AGE_SECONDS,
	PORTAL_ROUTE,
	resolve_booking_portal_link,
)
from ism.comms.otp import clear_comms_rate_limits, get_last_mock_otp
from ism.comms.providers import clear_mock_sms_outbox, get_mock_sms_outbox
from ism.comms.sessions import verify_portal_session
from ism.scheduling.time_utils import invalidate_shop_settings_cache, to_timedelta
from ism.tests.base import TEST_PREFIX, ISMTestCase

_FORBIDDEN_INTERNAL_KEYS: frozenset[str] = frozenset(
	{
		"start_time",
		"internal_start_time",
		"offset_minutes",
		"customer_arrival_offset_minutes",
		"service_bay",
		"available_bays",
		"available_bay_ids",
		"assigned_technician",
		"available_technicians",
		"available_technician_ids",
		"bottleneck",
		"effective_capacity",
		"conflict_entry",
		"correction_code",
		"rh_correction_code",
		"hourly_rate",
		"service_rate",
		"item_code",
		"supplier_cost",
		"tesla_manual_url",
		"doc_guid",
		"cookie_value",
		"customer",
		"repair_vehicle",
	}
)
_FORBIDDEN_CUSTOMER_REGEX = re.compile(
	r"\b(buffer|leeway|early|grace\s+period|08:00|8:00\s*am)\b",
	re.IGNORECASE,
)


def _collect_keys_and_strings(obj: Any, keys_out: set[str], strings_out: list[str]) -> None:
	if isinstance(obj, dict):
		for key, value in obj.items():
			keys_out.add(str(key))
			_collect_keys_and_strings(value, keys_out, strings_out)
	elif isinstance(obj, (list, tuple, set)):
		for item in obj:
			_collect_keys_and_strings(item, keys_out, strings_out)
	elif isinstance(obj, str):
		strings_out.append(obj)


class _RecordingCookieManager:
	"""Minimal stand-in for Frappe CookieManager to inspect Set-Cookie attributes in tests."""

	def __init__(self) -> None:
		self.cookies: dict[str, dict[str, Any]] = {}

	def set_cookie(
		self,
		key: str,
		value: str,
		*,
		max_age: int | None = None,
		httponly: bool = False,
		secure: bool = False,
		samesite: str | None = None,
		path: str = "/",
	) -> None:
		self.cookies[key] = {
			"value": value,
			"max_age": max_age,
			"httponly": httponly,
			"secure": secure,
			"samesite": samesite,
			"path": path,
		}


class TestBookingFunnelAndAbuse(ISMTestCase):
	"""Comprehensive Wave 2 backend, OTP gate, anti-abuse, race-recovery, and P4 handoff tests."""

	_SETTINGS_FIELDS: tuple[str, ...] = (
		"enable_self_serve_booking",
		"self_serve_booking_title",
		"self_serve_booking_intro",
		"self_serve_max_packages_per_booking",
		"self_serve_require_email",
		"self_serve_booking_ip_rate_limit_per_hour",
		"self_serve_booking_phone_rate_limit_per_day",
		"self_serve_honeypot_enabled",
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
		"portal_session_ttl_days",
		"portal_session_cookie_samesite",
		"portal_session_secure_cookie",
		"operating_hours_start",
		"operating_hours_end",
		"break_start",
		"break_end",
		"default_slot_interval",
		"default_diagnostic_duration",
		"enable_technician_capacity",
		"technician_designation",
		"holiday_list",
		"customer_arrival_offset_minutes",
		"reschedule_cutoff_hours",
		"max_self_reschedules",
		"self_serve_default_entry_type",
		"self_serve_min_lead_time_minutes",
		"self_serve_horizon_days",
		"enable_waiter_preference",
		"buffer_padding_minutes",
	)

	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")
		self.manifest = self.get_manifest()
		self.bay1 = self.manifest.bays["main_lift"]
		self.bay2 = self.manifest.bays["alignment"]
		self.bay_hv = self.manifest.bays["hv_diagnostics"]
		self.bay_tire = self.manifest.bays["tire_quick"]
		self.active_bays = [self.bay1, self.bay2, self.bay_hv, self.bay_tire]
		self.tech1 = self.manifest.techs["marcus"]
		self.tech2 = self.manifest.techs["elena"]

		self._saved_settings: dict[str, Any] = {}
		settings = frappe.get_single("ISM Settings")
		for fieldname in self._SETTINGS_FIELDS:
			self._saved_settings[fieldname] = getattr(settings, fieldname, None)

		self._had_request_ip = hasattr(frappe.local, "request_ip")
		self._orig_request_ip = getattr(frappe.local, "request_ip", None)
		self._had_cookie_manager = hasattr(frappe.local, "cookie_manager")
		self._orig_cookie_manager = getattr(frappe.local, "cookie_manager", None)

		self._set_ism_settings(
			enable_self_serve_booking=1,
			self_serve_booking_title="Book Your EV Service",
			self_serve_booking_intro="Choose your vehicle and services to book online.",
			self_serve_max_packages_per_booking=5,
			self_serve_require_email=0,
			self_serve_booking_ip_rate_limit_per_hour=15,
			self_serve_booking_phone_rate_limit_per_day=3,
			self_serve_honeypot_enabled=1,
			sms_gateway_provider="Mock (Deterministic Test)",
			sms_default_country_code="+1",
			sms_enforce_opt_out=1,
			otp_code_length=6,
			otp_ttl_seconds=300,
			otp_resend_cooldown_seconds=15,
			otp_max_verify_attempts=3,
			otp_lockout_seconds=900,
			otp_phone_rate_limit_per_hour=20,
			otp_ip_rate_limit_per_hour=50,
			portal_session_cookie_name="ism_portal_session",
			portal_session_ttl_days=30,
			portal_session_cookie_samesite="Lax",
			portal_session_secure_cookie=1,
			operating_hours_start="08:00:00",
			operating_hours_end="17:00:00",
			break_start="12:00:00",
			break_end="12:30:00",
			default_slot_interval=30,
			default_diagnostic_duration=60,
			enable_technician_capacity=1,
			technician_designation="Technician",
			holiday_list=None,
			customer_arrival_offset_minutes=15,
			reschedule_cutoff_hours=24,
			max_self_reschedules=2,
			self_serve_default_entry_type="Diagnostic",
			self_serve_min_lead_time_minutes=120,
			self_serve_horizon_days=30,
			enable_waiter_preference=1,
			buffer_padding_minutes=0,
		)

		clear_booking_rate_limits()
		clear_comms_rate_limits()
		clear_mock_sms_outbox()

		self.base_date = getdate(add_days(today(), 5))
		self.bookable_pkg = self._create_bookable_package(
			"P5_CabinFilter",
			customer_title="Cabin Air Filter Service",
			customer_summary="Replace cabin filter and inspect HVAC intake.",
			applicable_model="Model 3",
			self_serve_duration_minutes=60,
			customer_price_display_mode="Exact Price",
			customer_estimate_min=129.0,
		)
		self.bookable_pkg_2 = self._create_bookable_package(
			"P5_BrakeService",
			customer_title="Annual Brake Caliper Service",
			customer_summary="Clean and lubricate brake calipers for winter corrosion protection.",
			applicable_model="All Models",
			self_serve_duration_minutes=60,
			customer_price_display_mode="Starting At",
			customer_estimate_min=199.0,
		)
		frappe.local.request_ip = "203.0.113.10"

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		frappe.local.request_ip = self._orig_request_ip
		if self._had_cookie_manager:
			frappe.local.cookie_manager = self._orig_cookie_manager
		elif hasattr(frappe.local, "cookie_manager"):
			delattr(frappe.local, "cookie_manager")
		clear_booking_rate_limits()
		clear_comms_rate_limits()
		clear_mock_sms_outbox()
		try:
			settings = frappe.get_single("ISM Settings")
			for fieldname, value in self._saved_settings.items():
				setattr(settings, fieldname, value)
			settings.save(ignore_permissions=True)
			frappe.clear_cache(doctype="ISM Settings")
			invalidate_shop_settings_cache()
		finally:
			super().tearDown()

	def _set_ism_settings(self, **kwargs: Any) -> None:
		settings = frappe.get_single("ISM Settings")
		for key, val in kwargs.items():
			setattr(settings, key, val)
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype="ISM Settings")
		invalidate_shop_settings_cache()

	def _create_bookable_package(self, suffix: str, **kwargs: Any):
		pkg_name = f"{TEST_PREFIX}{suffix}"
		if frappe.db.exists("Service Package", pkg_name):
			frappe.delete_doc("Service Package", pkg_name, force=True, ignore_permissions=True)
		payload: dict[str, Any] = {
			"doctype": "Service Package",
			"package_name": pkg_name,
			"applicable_model": "All Models",
			"standard_labor_hours": 1.0,
			"hourly_rate": 140.0,
			"service_rate": 140.0,
			"correction_code": "10101010",
			"allow_self_serve_booking": 1,
			"customer_price_display_mode": "Exact Price",
			"customer_estimate_min": 140.0,
			"self_serve_duration_minutes": 60,
		}
		payload.update(kwargs)
		doc = frappe.get_doc(payload).insert(ignore_permissions=True)
		self.track(doc)
		return doc

	def _fresh_valid_nonce(self, age_seconds: int = 10) -> str:
		return issue_booking_form_nonce(now_ts=int(time.time()) - age_seconds)

	def _track_artifacts_for_phone(self, phone_e164: str) -> None:
		for doctype, field in (
			("Schedule Entry", "provisional_phone"),
			("Portal OTP Challenge", "phone_e164"),
			("Customer Portal Session", "phone_e164"),
			("SMS Audit Log", "phone_e164"),
		):
			names = frappe.get_all(doctype, filters={field: phone_e164}, pluck="name")
			for name in names:
				self.track(doctype, name)
				if doctype == "Schedule Entry":
					sai_names = frappe.get_all(
						"Shop Action Item",
						filters={"reference_doctype": "Schedule Entry", "reference_name": name},
						pluck="name",
					)
					for sai in sai_names:
						self.track("Shop Action Item", sai)

	def _create_blocking_entry(
		self,
		sched_date: Any,
		*,
		sched_time: str = "08:00:00",
		duration: int = 60,
		bay: str | None = None,
		tech: str | None = None,
	):
		payload: dict[str, Any] = {
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"status": "Scheduled",
			"scheduled_date": str(sched_date),
			"scheduled_time": sched_time,
			"estimated_duration": duration,
			"service_bay": bay or self.bay1,
		}
		if tech:
			payload["assigned_technician"] = tech
		doc = frappe.get_doc(payload).insert(ignore_permissions=True)
		self.track(doc)
		return doc

	def _assert_zero_disclosure(
		self,
		payload: Any,
		*,
		extra_forbidden_strings: list[str] | None = None,
	) -> None:
		keys: set[str] = set()
		strings: list[str] = []
		_collect_keys_and_strings(payload, keys, strings)

		leaked_keys = keys & _FORBIDDEN_INTERNAL_KEYS
		self.assertEqual(leaked_keys, set(), f"Leaked internal keys: {leaked_keys}")

		joined = " ".join(strings)
		match = _FORBIDDEN_CUSTOMER_REGEX.search(joined)
		self.assertIsNone(match, f"Forbidden customer wording '{match.group(0) if match else ''}' found.")

		tech_names = [
			frappe.db.get_value("Employee", tid, "employee_name") or tid
			for tid in (self.tech1, self.tech2)
		]
		forbidden_literals = list(self.active_bays) + [self.tech1, self.tech2] + tech_names
		if extra_forbidden_strings:
			forbidden_literals.extend(s for s in extra_forbidden_strings if s)

		for secret in forbidden_literals:
			self.assertNotIn(secret, joined, f"Forbidden internal string '{secret}' leaked in payload.")

	def test_facade_exports(self) -> None:
		for symbol in (
			"get_booking_boot_config",
			"get_public_booking_packages",
			"get_public_booking_slots",
			"initiate_booking_otp",
			"verify_and_commit_booking",
			"get_booking_settings",
			"validate_honeypot_and_nonce",
		):
			self.assertIn(symbol, booking_facade.__all__)
			self.assertTrue(hasattr(booking_facade, symbol))

	def test_disabled_toggle_and_sms_unconfigured_fail_closed(self) -> None:
		# 1. Feature toggle disabled -> 503 BOOKING_DISABLED across all funnel endpoints.
		self._set_ism_settings(enable_self_serve_booking=0)
		frappe.set_user("Guest")

		boot = get_booking_config()
		self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		self.assertFalse(boot["enabled"])
		self.assertEqual(boot["disabled_reason"], ERR_BOOKING_DISABLED)
		self.assertIsNone(boot["form_nonce"])
		self._assert_zero_disclosure(boot)

		pkg_res = get_booking_packages(vehicle_model="Model 3", vehicle_year=2023)
		self.assertFalse(pkg_res["success"])
		self.assertEqual(pkg_res["error_code"], ERR_BOOKING_DISABLED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 503)

		slot_res = get_booking_slots(package_ids=[self.bookable_pkg.name])
		self.assertFalse(slot_res["success"])
		self.assertEqual(slot_res["error_code"], ERR_BOOKING_DISABLED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 503)

		otp_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Jane Driver",
			phone="+14165550101",
		)
		self.assertFalse(otp_res["success"])
		self.assertEqual(otp_res["error_code"], ERR_BOOKING_DISABLED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 503)

		commit_res = confirm_self_serve_booking(
			challenge_id="dummy",
			otp_code="123456",
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Jane Driver",
			phone="+14165550101",
		)
		self.assertFalse(commit_res["success"])
		self.assertEqual(commit_res["error_code"], ERR_BOOKING_DISABLED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 503)

		# 2. Booking enabled but SMS provider disabled -> 503 SMS_UNCONFIGURED fail-closed.
		frappe.set_user("Administrator")
		self._set_ism_settings(enable_self_serve_booking=1, sms_gateway_provider="Disabled")
		frappe.set_user("Guest")

		boot2 = get_booking_config()
		self.assertFalse(boot2["enabled"])
		self.assertEqual(boot2["disabled_reason"], ERR_SMS_UNCONFIGURED)
		self.assertIsNone(boot2["form_nonce"])

		otp_res2 = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Jane Driver",
			phone="+14165550101",
		)
		self.assertFalse(otp_res2["success"])
		self.assertEqual(otp_res2["error_code"], ERR_SMS_UNCONFIGURED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 503)

		commit_res2 = confirm_self_serve_booking(
			challenge_id="dummy",
			otp_code="123456",
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Jane Driver",
			phone="+14165550101",
		)
		self.assertFalse(commit_res2["success"])
		self.assertEqual(commit_res2["error_code"], ERR_SMS_UNCONFIGURED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 503)

	def test_happy_path_opening_slot_parity_cookie_seam5_and_zero_disclosure(self) -> None:
		phone_raw = "(416) 555-0181"
		phone_e164 = "+14165550181"
		customer_email = "alex.driver@example.com"
		matched_cust = self.create_test_customer(
			f"{TEST_PREFIX}UniquePhoneCust",
			mobile_no=phone_e164,
			email_id=customer_email,
		)

		cookie_mgr = _RecordingCookieManager()
		frappe.local.cookie_manager = cookie_mgr
		frappe.local.request_ip = "203.0.113.21"
		frappe.set_user("Guest")

		# 1. Boot config
		boot = get_booking_config()
		self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		self.assertTrue(boot["enabled"])
		self.assertIsNone(boot["disabled_reason"])
		self.assertEqual(boot["portal_url"], PORTAL_ROUTE)
		self.assertTrue(boot["form_nonce"])
		self._assert_zero_disclosure(boot, extra_forbidden_strings=[matched_cust.name])

		# 2. Packages (filtered by Model 3 / 2023)
		pkg_res = get_booking_packages(vehicle_model="Model 3", vehicle_year="2023")
		self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		self.assertTrue(pkg_res["success"])
		pkg_ids = [p["package_id"] for p in pkg_res["packages"]]
		self.assertIn(self.bookable_pkg.name, pkg_ids)
		self._assert_zero_disclosure(pkg_res, extra_forbidden_strings=[matched_cust.name])

		# 3. Slots (JSON string with duplicate ID -> deduplicated cleanly; opening slot 08:00 -> 07:45)
		slot_res = get_booking_slots(
			package_ids=json.dumps([self.bookable_pkg.name, self.bookable_pkg.name]),
			start_date=str(self.base_date),
			days_ahead=1,
			vehicle_model="Model 3",
			vehicle_year=2023,
		)
		self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		self.assertTrue(slot_res["success"])
		self.assertGreaterEqual(slot_res["total_slots"], 1)
		first_slot = slot_res["days"][0]["slots"][0]
		self.assertEqual(first_slot["customer_arrival_time"], "07:45:00")
		self.assertEqual(first_slot["customer_arrival_hhmm"], "07:45")
		self.assertEqual(first_slot["display_time"], "7:45 AM")
		self._assert_zero_disclosure(slot_res, extra_forbidden_strings=[matched_cust.name])

		# 4. Request SMS OTP
		valid_nonce = self._fresh_valid_nonce()
		otp_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Alex Driver",
			phone=phone_raw,
			email=customer_email,
			vehicle_model="Model 3",
			vehicle_year=2023,
			vin_or_plate="EVTEST1",
			notes="Please inspect cabin intake.",
			waiter_preference="Drop-Off",
			website_url="",
			form_nonce=valid_nonce,
		)
		self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		self.assertTrue(otp_res["success"])
		challenge_id = otp_res["challenge_id"]
		self.assertTrue(challenge_id)
		self.assertEqual(otp_res["phone_masked"], "+1******0181")

		otp_code = get_last_mock_otp(phone_e164)
		self.assertIsNotNone(otp_code)
		self.assertEqual(len(otp_code), 6)
		self._assert_zero_disclosure(otp_res, extra_forbidden_strings=[matched_cust.name, otp_code])

		# Zero Schedule Entry created before OTP verification
		frappe.set_user("Administrator")
		self.assertEqual(
			frappe.db.count("Schedule Entry", {"provisional_phone": phone_e164}),
			0,
		)

		# 5. Confirm booking with OTP -> commit SE + Seam 5 + cookie + SMS + email
		frappe.set_user("Guest")
		with patch("frappe.sendmail") as mock_sendmail:
			confirm_res = confirm_self_serve_booking(
				challenge_id=challenge_id,
				otp_code=otp_code,
				package_ids=[self.bookable_pkg.name],
				target_date=str(self.base_date),
				customer_arrival_time="07:45",
				customer_name="Alex Driver",
				phone=phone_raw,
				email=customer_email,
				vehicle_model="Model 3",
				vehicle_year=2023,
				vin_or_plate="EVTEST1",
				notes="Please inspect cabin intake.",
				waiter_preference="Drop-Off",
				website_url="",
				form_nonce=valid_nonce,
				idempotency_key="idem-happy-001",
			)

		self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		self.assertTrue(confirm_res["success"])
		booking_ref = confirm_res["booking_reference"]
		self.assertTrue(booking_ref)
		self.assertEqual(confirm_res["appointment"]["customer_arrival_hhmm"], "07:45")
		self.assertEqual(confirm_res["appointment"]["display_time"], "7:45 AM")
		self.assertEqual(confirm_res["portal_handoff"]["portal_url"], PORTAL_ROUTE)
		self.assertTrue(confirm_res["portal_handoff"]["session_established"])
		self.assertEqual(confirm_res["portal_handoff"]["scope_type"], BOOKING_SESSION_SCOPE_TYPE)
		self.assertIsInstance(confirm_res.get("pin_to_drive"), dict)
		self.assertTrue(confirm_res["pin_to_drive"]["prompt_required"])
		self.assertTrue(confirm_res["pin_to_drive"]["can_submit"])
		self.assertEqual(confirm_res["pin_to_drive"]["status"], "Not Prompted")
		self.assertTrue(confirm_res["notifications"]["sms_sent"])
		self.assertTrue(confirm_res["notifications"]["email_sent"])

		# Cookie assertions & P1 verify_portal_session parity
		self.assertIn("ism_portal_session", cookie_mgr.cookies)
		cookie_entry = cookie_mgr.cookies["ism_portal_session"]
		cookie_value = cookie_entry["value"]
		self.assertTrue(cookie_entry["httponly"])
		self.assertEqual(cookie_entry["samesite"], "Lax")
		self.assertEqual(cookie_entry["path"], "/")

		# Zero disclosure across JSON confirmation (including customer ID, cookie_value, OTP code)
		self._assert_zero_disclosure(
			confirm_res,
			extra_forbidden_strings=[matched_cust.name, cookie_value, otp_code],
		)

		frappe.set_user("Administrator")
		self._track_artifacts_for_phone(phone_e164)

		# Verify Schedule Entry DB invariants (internal 08:00:00, booking_source, status)
		se = frappe.get_doc("Schedule Entry", booking_ref)
		self.assertEqual(to_timedelta(se.scheduled_time), to_timedelta("08:00:00"))
		self.assertEqual(se.booking_source, BOOKING_SOURCE)
		self.assertEqual(se.status, "Scheduled")
		self.assertEqual(se.provisional_phone, phone_e164)

		# Verify Seam 5 Shop Action Item emitted
		sai_rows = frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Schedule Entry", "reference_name": booking_ref},
			pluck="name",
		)
		self.assertGreaterEqual(len(sai_rows), 1)

		# Verify P1 portal session & Link to Portal OTP Challenge.name
		verified_session = verify_portal_session(cookie_value)
		self.assertIsNotNone(verified_session)
		self.assertEqual(verified_session.auth_method, BOOKING_SESSION_AUTH_METHOD)
		self.assertEqual(verified_session.scope_type, BOOKING_SESSION_SCOPE_TYPE)
		self.assertEqual(verified_session.schedule_entry, booking_ref)
		self.assertEqual(verified_session.phone_e164, phone_e164)
		self.assertEqual(verified_session.customer, matched_cust.name)

		challenge_doc_name = frappe.db.get_value(
			"Portal OTP Challenge", {"challenge_id": challenge_id}, "name"
		)
		session_source_link = frappe.db.get_value(
			"Customer Portal Session",
			{"session_id": verified_session.session_id},
			"source_otp_challenge",
		)
		self.assertEqual(session_source_link, challenge_doc_name)

		# Verify confirmation SMS in Mock outbox & SMS Audit Log (7:45 AM, zero buffer/08:00/internal leaks)
		outbox = get_mock_sms_outbox()
		confirm_sms = [
			m
			for m in outbox
			if m.get("phone_e164") == phone_e164 and booking_ref in (m.get("body") or "")
		]
		self.assertEqual(len(confirm_sms), 1)
		sms_body = confirm_sms[0]["body"]
		self.assertIn("7:45 AM", sms_body)
		self.assertIn(booking_ref, sms_body)
		self._assert_zero_disclosure(
			{"sms": sms_body},
			extra_forbidden_strings=[matched_cust.name, cookie_value, otp_code],
		)
		audit_confirm_count = frappe.db.count(
			"SMS Audit Log",
			{
				"phone_e164": phone_e164,
				"message_type": "Booking Confirmation",
				"reference_doctype": "Schedule Entry",
				"reference_name": booking_ref,
			},
		)
		self.assertEqual(audit_confirm_count, 1)

		# Verify confirmation email via patched frappe.sendmail (7:45 AM, zero leaks)
		mock_sendmail.assert_called_once()
		mail_kwargs = mock_sendmail.call_args.kwargs
		self.assertEqual(mail_kwargs["recipients"], [customer_email])
		self.assertIn("7:45 AM", mail_kwargs["subject"])
		self.assertIn("7:45 AM", mail_kwargs["message"])
		self.assertIn(booking_ref, mail_kwargs["message"])
		self._assert_zero_disclosure(
			{"subject": mail_kwargs["subject"], "body": mail_kwargs["message"]},
			extra_forbidden_strings=[matched_cust.name, cookie_value, otp_code],
		)

	def test_non_fatal_confirmation_dispatch_and_ambiguous_customer_lookup(self) -> None:
		phone_e164 = "+14165550182"
		# Create 2 customers sharing the same phone -> lookup must stay ambiguous (customer=None)
		self.create_test_customer(f"{TEST_PREFIX}AmbCustA", mobile_no=phone_e164)
		self.create_test_customer(f"{TEST_PREFIX}AmbCustB", mobile_no=phone_e164)

		cookie_mgr = _RecordingCookieManager()
		frappe.local.cookie_manager = cookie_mgr
		frappe.local.request_ip = "203.0.113.22"
		nonce = self._fresh_valid_nonce()

		otp_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Pat Owner",
			phone=phone_e164,
			email="pat@example.com",
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertTrue(otp_res["success"])
		code = get_last_mock_otp(phone_e164)

		with (
			patch("ism.booking.service.send_sms", side_effect=RuntimeError("SMS gateway down")),
			patch("frappe.sendmail", side_effect=RuntimeError("SMTP timeout")),
		):
			confirm_res = confirm_self_serve_booking(
				challenge_id=otp_res["challenge_id"],
				otp_code=code,
				package_ids=[self.bookable_pkg.name],
				target_date=str(self.base_date),
				customer_arrival_time="07:45",
				customer_name="Pat Owner",
				phone=phone_e164,
				email="pat@example.com",
				vehicle_model="Model 3",
				vehicle_year=2023,
				form_nonce=nonce,
			)

		self._track_artifacts_for_phone(phone_e164)
		self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		self.assertTrue(confirm_res["success"])
		self.assertFalse(confirm_res["notifications"]["sms_sent"])
		self.assertFalse(confirm_res["notifications"]["email_sent"])
		self.assertTrue(frappe.db.exists("Schedule Entry", confirm_res["booking_reference"]))

		verified = verify_portal_session(cookie_mgr.cookies["ism_portal_session"]["value"])
		self.assertIsNotNone(verified)
		self.assertIsNone(verified.customer)

	def test_invalid_otp_lockout_phone_mismatch_and_replay_rejection(self) -> None:
		phone_a = "+14165550183"
		phone_b = "+14165550184"
		frappe.local.request_ip = "203.0.113.23"
		nonce = self._fresh_valid_nonce()

		otp_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Morgan Driver",
			phone=phone_a,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertTrue(otp_res["success"])
		challenge_id = otp_res["challenge_id"]
		real_code = get_last_mock_otp(phone_a)
		wrong_code = "000000" if real_code != "000000" else "999999"

		# 1. Phone mismatch rejected (400) with zero Schedule Entry
		mismatch_res = confirm_self_serve_booking(
			challenge_id=challenge_id,
			otp_code=real_code,
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Morgan Driver",
			phone=phone_b,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertFalse(mismatch_res["success"])
		self.assertEqual(mismatch_res["error_code"], "PHONE_MISMATCH")
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		self.assertEqual(frappe.db.count("Schedule Entry", {"provisional_phone": phone_a}), 0)
		self.assertEqual(frappe.db.count("Schedule Entry", {"provisional_phone": phone_b}), 0)

		# 2. Invalid OTP decrements attempts_remaining (max_attempts=3) and locks out on 3rd failure
		bad1 = confirm_self_serve_booking(
			challenge_id=challenge_id,
			otp_code=wrong_code,
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Morgan Driver",
			phone=phone_a,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertFalse(bad1["success"])
		self.assertEqual(bad1["error_code"], "INVALID_CODE")
		self.assertEqual(bad1["attempts_remaining"], 2)
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)

		bad2 = confirm_self_serve_booking(
			challenge_id=challenge_id,
			otp_code=wrong_code,
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Morgan Driver",
			phone=phone_a,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertFalse(bad2["success"])
		self.assertEqual(bad2["error_code"], "INVALID_CODE")
		self.assertEqual(bad2["attempts_remaining"], 1)

		bad3 = confirm_self_serve_booking(
			challenge_id=challenge_id,
			otp_code=wrong_code,
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Morgan Driver",
			phone=phone_a,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertFalse(bad3["success"])
		self.assertEqual(bad3["error_code"], "LOCKED_OUT")
		self.assertEqual(frappe.local.response.get("http_status_code"), 429)
		self.assertTrue(bad3.get("locked_until"))
		self.assertEqual(frappe.db.count("Schedule Entry", {"provisional_phone": phone_a}), 0)

		# 3. Replay rejection after successful commit (both with otp_code and otp_code=None)
		otp_b = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Casey Driver",
			phone=phone_b,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertTrue(otp_b["success"])
		code_b = get_last_mock_otp(phone_b)

		ok_b = confirm_self_serve_booking(
			challenge_id=otp_b["challenge_id"],
			otp_code=code_b,
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Casey Driver",
			phone=phone_b,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertTrue(ok_b["success"])

		next_date = add_days(self.base_date, 1)
		replay_with_code = confirm_self_serve_booking(
			challenge_id=otp_b["challenge_id"],
			otp_code=code_b,
			package_ids=[self.bookable_pkg.name],
			target_date=str(next_date),
			customer_arrival_time="07:45",
			customer_name="Casey Driver",
			phone=phone_b,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertFalse(replay_with_code["success"])
		self.assertEqual(replay_with_code["error_code"], "OTP_ALREADY_USED")
		self.assertEqual(frappe.local.response.get("http_status_code"), 409)

		replay_without_code = confirm_self_serve_booking(
			challenge_id=otp_b["challenge_id"],
			otp_code=None,
			package_ids=[self.bookable_pkg.name],
			target_date=str(next_date),
			customer_arrival_time="07:45",
			customer_name="Casey Driver",
			phone=phone_b,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertFalse(replay_without_code["success"])
		self.assertEqual(replay_without_code["error_code"], "OTP_ALREADY_USED")
		self.assertEqual(frappe.local.response.get("http_status_code"), 409)
		self.assertEqual(frappe.db.count("Schedule Entry", {"provisional_phone": phone_b}), 1)

		self._track_artifacts_for_phone(phone_a)
		self._track_artifacts_for_phone(phone_b)

	def test_slot_conflict_recovery_preserves_verified_otp_for_retry(self) -> None:
		phone_e164 = "+14165550185"
		frappe.local.request_ip = "203.0.113.24"
		nonce = self._fresh_valid_nonce()

		# 1. Request OTP while 07:45 (08:00 internal) is open
		otp_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Taylor Driver",
			phone=phone_e164,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertTrue(otp_res["success"])
		challenge_id = otp_res["challenge_id"]
		code = get_last_mock_otp(phone_e164)
		sms_count_after_otp = len(get_mock_sms_outbox())

		# 2. Externally saturate ALL active bays at 08:00-09:00 on base_date
		for bay in self.active_bays:
			self._create_blocking_entry(self.base_date, sched_time="08:00:00", duration=60, bay=bay)

		# 3. Confirm with OTP -> 409 SLOT_UNAVAILABLE with otp_verified=True, consumed_at is None
		conflict_res = confirm_self_serve_booking(
			challenge_id=challenge_id,
			otp_code=code,
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Taylor Driver",
			phone=phone_e164,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self.assertFalse(conflict_res["success"])
		self.assertEqual(conflict_res["error_code"], ERR_SLOT_UNAVAILABLE)
		self.assertTrue(conflict_res["otp_verified"])
		self.assertEqual(conflict_res["challenge_id"], challenge_id)
		self.assertEqual(frappe.local.response.get("http_status_code"), 409)
		self.assertEqual(frappe.db.count("Schedule Entry", {"provisional_phone": phone_e164}), 0)

		challenge_state = frappe.db.get_value(
			"Portal OTP Challenge",
			{"challenge_id": challenge_id},
			["status", "consumed_at"],
			as_dict=True,
		)
		self.assertEqual(challenge_state["status"], "Verified")
		self.assertIsNone(challenge_state["consumed_at"])

		# 4. Retry with otp_code=None on next open slot 08:45 (09:00 internal) -> succeeds!
		retry_res = confirm_self_serve_booking(
			challenge_id=challenge_id,
			otp_code=None,
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="08:45",
			customer_name="Taylor Driver",
			phone=phone_e164,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=nonce,
		)
		self._track_artifacts_for_phone(phone_e164)

		self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		self.assertTrue(retry_res["success"])
		self.assertEqual(retry_res["appointment"]["customer_arrival_hhmm"], "08:45")
		self.assertEqual(retry_res["appointment"]["display_time"], "8:45 AM")

		se = frappe.get_doc("Schedule Entry", retry_res["booking_reference"])
		self.assertEqual(to_timedelta(se.scheduled_time), to_timedelta("09:00:00"))

		consumed_after = frappe.db.get_value(
			"Portal OTP Challenge", {"challenge_id": challenge_id}, "consumed_at"
		)
		self.assertIsNotNone(consumed_after)

		# Verify no second OTP SMS was sent (only the 1 Booking Confirmation SMS was added)
		otp_sms_messages = [
			m
			for m in get_mock_sms_outbox()
			if m.get("phone_e164") == phone_e164 and "verification code" in (m.get("body") or "").lower()
		]
		self.assertEqual(len(otp_sms_messages), sms_count_after_otp)
		self.assertEqual(
			frappe.db.count("SMS Audit Log", {"phone_e164": phone_e164, "message_type": "OTP"}),
			1,
		)

	def test_anti_abuse_honeypot_nonces_rate_limits_duplicates_idempotency_and_caps(self) -> None:
		frappe.local.request_ip = "203.0.113.25"
		valid_nonce = self._fresh_valid_nonce()

		# 1. Honeypot -> 400 BOT_DETECTED, 0 SMS sent
		hp_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Spam Bot",
			phone="+14165550191",
			vehicle_model="Model 3",
			vehicle_year=2023,
			website_url="http://spam.invalid",
			form_nonce=valid_nonce,
		)
		self.assertFalse(hp_res["success"])
		self.assertEqual(hp_res["error_code"], ERR_BOT_DETECTED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		self.assertEqual(len(get_mock_sms_outbox()), 0)

		# 2. Sub-2-second form nonce -> 400 SUBMISSION_TOO_FAST, 0 SMS sent
		fast_nonce = issue_booking_form_nonce(now_ts=int(time.time()))
		fast_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Fast Bot",
			phone="+14165550191",
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=fast_nonce,
		)
		self.assertFalse(fast_res["success"])
		self.assertEqual(fast_res["error_code"], ERR_SUBMISSION_TOO_FAST)
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		self.assertEqual(len(get_mock_sms_outbox()), 0)

		# 3. Tampered nonce -> 400 BOT_DETECTED
		tampered_nonce = f"{valid_nonce[:-4]}AAAA"
		tampered_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Tamper Bot",
			phone="+14165550191",
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=tampered_nonce,
		)
		self.assertFalse(tampered_res["success"])
		self.assertEqual(tampered_res["error_code"], ERR_BOT_DETECTED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)

		# 4. Expired nonce (>4h) -> 400 FORM_EXPIRED
		expired_nonce = issue_booking_form_nonce(now_ts=int(time.time()) - (MAX_FORM_NONCE_AGE_SECONDS + 120))
		expired_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Stale Tab",
			phone="+14165550191",
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=expired_nonce,
		)
		self.assertFalse(expired_res["success"])
		self.assertEqual(expired_res["error_code"], ERR_FORM_EXPIRED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 400)
		self.assertEqual(len(get_mock_sms_outbox()), 0)

		# 5. Package cap (> self_serve_max_packages_per_booking) -> 422 MAX_PACKAGES_EXCEEDED
		self._set_ism_settings(self_serve_max_packages_per_booking=1)
		cap_slots = get_booking_slots(
			package_ids=[self.bookable_pkg.name, self.bookable_pkg_2.name],
			start_date=str(self.base_date),
			days_ahead=1,
		)
		self.assertFalse(cap_slots["success"])
		self.assertEqual(cap_slots["error_code"], ERR_MAX_PACKAGES_EXCEEDED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 422)

		cap_otp = request_booking_otp(
			package_ids=[self.bookable_pkg.name, self.bookable_pkg_2.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Cap Tester",
			phone="+14165550191",
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=valid_nonce,
		)
		self.assertFalse(cap_otp["success"])
		self.assertEqual(cap_otp["error_code"], ERR_MAX_PACKAGES_EXCEEDED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 422)
		self._set_ism_settings(self_serve_max_packages_per_booking=5)

		# 6. require_email=1 -> 422 EMAIL_REQUIRED when email omitted
		self._set_ism_settings(self_serve_require_email=1)
		email_req_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="No Email Driver",
			phone="+14165550191",
			email=None,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=valid_nonce,
		)
		self.assertFalse(email_req_res["success"])
		self.assertEqual(email_req_res["error_code"], ERR_EMAIL_REQUIRED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 422)
		self._set_ism_settings(self_serve_require_email=0)

		# 7. Double-click idempotency replay + Same-day duplicate booking guard (zero leakage)
		phone_dup = "+14165550192"
		clear_booking_rate_limits()
		otp_dup = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Robin Driver",
			phone=phone_dup,
			vehicle_model="Model 3",
			vehicle_year=2023,
			vin_or_plate="SECRETPLT",
			form_nonce=valid_nonce,
		)
		self.assertTrue(otp_dup["success"])
		code_dup = get_last_mock_otp(phone_dup)

		first_commit = confirm_self_serve_booking(
			challenge_id=otp_dup["challenge_id"],
			otp_code=code_dup,
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Robin Driver",
			phone=phone_dup,
			vehicle_model="Model 3",
			vehicle_year=2023,
			vin_or_plate="SECRETPLT",
			form_nonce=valid_nonce,
			idempotency_key="idem-double-tap-42",
		)
		self.assertTrue(first_commit["success"])
		first_ref = first_commit["booking_reference"]

		# Double-tap replay returns identical confirmation without creating a 2nd Schedule Entry
		second_commit = confirm_self_serve_booking(
			challenge_id=otp_dup["challenge_id"],
			otp_code=code_dup,
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="Robin Driver",
			phone=phone_dup,
			vehicle_model="Model 3",
			vehicle_year=2023,
			vin_or_plate="SECRETPLT",
			form_nonce=valid_nonce,
			idempotency_key="idem-double-tap-42",
		)
		self.assertTrue(second_commit["success"])
		self.assertEqual(second_commit["booking_reference"], first_ref)
		self.assertEqual(frappe.db.count("Schedule Entry", {"provisional_phone": phone_dup}), 1)

		# Same-day duplicate booking request -> 409 DUPLICATE_BOOKING with zero leakage of existing SE/plate/time
		dup_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="09:45",
			customer_name="Robin Driver",
			phone=phone_dup,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=valid_nonce,
		)
		self.assertFalse(dup_res["success"])
		self.assertEqual(dup_res["error_code"], ERR_DUPLICATE_BOOKING)
		self.assertEqual(frappe.local.response.get("http_status_code"), 409)
		dup_json = json.dumps(dup_res)
		self.assertNotIn(first_ref, dup_json)
		self.assertNotIn("SECRETPLT", dup_json)
		self.assertNotIn("7:45", dup_json)
		self._track_artifacts_for_phone(phone_dup)

		# 8. Per-phone daily cap (both Redis and MariaDB authoritative enforcement)
		self._set_ism_settings(self_serve_booking_phone_rate_limit_per_day=2)
		phone_daily = "+14165550193"
		clear_booking_rate_limits()
		clear_comms_rate_limits()

		# 8a. Redis-enforced daily cap
		record_phone_daily_booking(phone_daily)
		record_phone_daily_booking(phone_daily)
		redis_daily_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(add_days(self.base_date, 2)),
			customer_arrival_time="07:45",
			customer_name="Daily Redis",
			phone=phone_daily,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=valid_nonce,
		)
		self.assertFalse(redis_daily_res["success"])
		self.assertEqual(redis_daily_res["error_code"], ERR_PHONE_DAILY_LIMIT)
		self.assertEqual(frappe.local.response.get("http_status_code"), 429)

		# 8b. MariaDB-enforced daily cap even after Redis keys are flushed
		clear_booking_rate_limits()
		for offset_day in (2, 3):
			se_doc = frappe.get_doc(
				{
					"doctype": "Schedule Entry",
					"entry_type": "Diagnostic",
					"status": "Scheduled",
					"scheduled_date": str(add_days(self.base_date, offset_day)),
					"scheduled_time": "08:00:00",
					"estimated_duration": 60,
					"service_bay": self.bay1,
					"booking_source": BOOKING_SOURCE,
					"provisional_phone": phone_daily,
					"provisional_customer_name": "Daily DB",
				}
			).insert(ignore_permissions=True)
			self.track(se_doc)

		db_daily_res = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(add_days(self.base_date, 4)),
			customer_arrival_time="07:45",
			customer_name="Daily DB",
			phone=phone_daily,
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=valid_nonce,
		)
		self.assertFalse(db_daily_res["success"])
		self.assertEqual(db_daily_res["error_code"], ERR_PHONE_DAILY_LIMIT)
		self.assertEqual(frappe.local.response.get("http_status_code"), 429)

		# 9. Per-IP hourly cap -> 429 RATE_LIMITED
		self._set_ism_settings(self_serve_booking_ip_rate_limit_per_hour=2)
		clear_booking_rate_limits()
		clear_comms_rate_limits()
		frappe.local.request_ip = "203.0.113.99"

		for idx in range(2):
			phone_ip = f"+1416555017{idx}"
			res_ok = request_booking_otp(
				package_ids=[self.bookable_pkg.name],
				target_date=str(self.base_date),
				customer_arrival_time="07:45",
				customer_name=f"IP User {idx}",
				phone=phone_ip,
				vehicle_model="Model 3",
				vehicle_year=2023,
				form_nonce=valid_nonce,
			)
			self.assertTrue(res_ok["success"])
			self._track_artifacts_for_phone(phone_ip)

		res_blocked = request_booking_otp(
			package_ids=[self.bookable_pkg.name],
			target_date=str(self.base_date),
			customer_arrival_time="07:45",
			customer_name="IP User Over",
			phone="+14165550179",
			vehicle_model="Model 3",
			vehicle_year=2023,
			form_nonce=valid_nonce,
		)
		self.assertFalse(res_blocked["success"])
		self.assertEqual(res_blocked["error_code"], ERR_RATE_LIMITED)
		self.assertEqual(frappe.local.response.get("http_status_code"), 429)
		self.assertGreater(res_blocked.get("retry_after_seconds", 0), 0)

	def test_p4_portal_link_handoff_present_and_import_error_fallback(self) -> None:
		# 1. P4 present: ism.portal.auth.create_booking_portal_link returns tokenized relative URL
		fake_portal_pkg = types.ModuleType("ism.portal")
		fake_portal_auth = types.ModuleType("ism.portal.auth")
		fake_portal_auth.create_booking_portal_link = lambda se: f"/portal?booking_token=tok-{se}"
		fake_portal_pkg.auth = fake_portal_auth

		with patch.dict(
			sys.modules,
			{"ism.portal": fake_portal_pkg, "ism.portal.auth": fake_portal_auth},
		):
			url_present, tokenized = resolve_booking_portal_link("SE-TEST-001")
			self.assertTrue(tokenized)
			self.assertTrue(url_present.endswith("/portal?booking_token=tok-SE-TEST-001"))

			# Also verify end-to-end confirmation SMS & Email carry the tokenized link
			phone_p4 = "+14165550195"
			frappe.local.request_ip = "203.0.113.31"
			nonce = self._fresh_valid_nonce()
			otp_res = request_booking_otp(
				package_ids=[self.bookable_pkg.name],
				target_date=str(self.base_date),
				customer_arrival_time="07:45",
				customer_name="Portal Driver",
				phone=phone_p4,
				email="portal.driver@example.com",
				vehicle_model="Model 3",
				vehicle_year=2023,
				form_nonce=nonce,
			)
			self.assertTrue(otp_res["success"])
			code = get_last_mock_otp(phone_p4)

			with patch("frappe.sendmail") as mock_mail:
				confirm_res = confirm_self_serve_booking(
					challenge_id=otp_res["challenge_id"],
					otp_code=code,
					package_ids=[self.bookable_pkg.name],
					target_date=str(self.base_date),
					customer_arrival_time="07:45",
					customer_name="Portal Driver",
					phone=phone_p4,
					email="portal.driver@example.com",
					vehicle_model="Model 3",
					vehicle_year=2023,
					form_nonce=nonce,
				)
			self._track_artifacts_for_phone(phone_p4)
			self.assertTrue(confirm_res["success"])
			ref = confirm_res["booking_reference"]
			self.assertTrue(confirm_res["portal_handoff"]["has_tokenized_link"])
			expected_tok = f"/portal?booking_token=tok-{ref}"
			confirm_sms = [
				m
				for m in get_mock_sms_outbox()
				if m.get("phone_e164") == phone_p4 and ref in (m.get("body") or "")
			]
			self.assertEqual(len(confirm_sms), 1)
			self.assertIn(expected_tok, confirm_sms[0]["body"])
			mock_mail.assert_called_once()
			self.assertIn(expected_tok, mock_mail.call_args.kwargs["message"])

		# 2. P4 absent (ImportError): falls back cleanly to <site_url>/portal without failing booking
		real_import = builtins.__import__

		def _missing_portal_import(name, globals=None, locals=None, fromlist=(), level=0):
			if name == "ism.portal.auth" or name.startswith("ism.portal"):
				raise ImportError("No module named 'ism.portal'")
			return real_import(name, globals, locals, fromlist, level)

		with (
			patch.dict(sys.modules, {"ism.portal": None, "ism.portal.auth": None}),
			patch("builtins.__import__", side_effect=_missing_portal_import),
		):
			url_fallback, tokenized_fallback = resolve_booking_portal_link("SE-TEST-002")
			self.assertFalse(tokenized_fallback)
			self.assertTrue(url_fallback.endswith("/portal"))

			phone_no_p4 = "+14165550196"
			frappe.local.request_ip = "203.0.113.32"
			nonce2 = self._fresh_valid_nonce()
			otp_res2 = request_booking_otp(
				package_ids=[self.bookable_pkg.name],
				target_date=str(add_days(self.base_date, 1)),
				customer_arrival_time="07:45",
				customer_name="Fallback Driver",
				phone=phone_no_p4,
				email="fallback.driver@example.com",
				vehicle_model="Model 3",
				vehicle_year=2023,
				form_nonce=nonce2,
			)
			self.assertTrue(otp_res2["success"])
			code2 = get_last_mock_otp(phone_no_p4)

			with patch("frappe.sendmail") as mock_mail2:
				confirm_res2 = confirm_self_serve_booking(
					challenge_id=otp_res2["challenge_id"],
					otp_code=code2,
					package_ids=[self.bookable_pkg.name],
					target_date=str(add_days(self.base_date, 1)),
					customer_arrival_time="07:45",
					customer_name="Fallback Driver",
					phone=phone_no_p4,
					email="fallback.driver@example.com",
					vehicle_model="Model 3",
					vehicle_year=2023,
					form_nonce=nonce2,
				)
			self._track_artifacts_for_phone(phone_no_p4)
			self.assertTrue(confirm_res2["success"])
			self.assertFalse(confirm_res2["portal_handoff"]["has_tokenized_link"])
			ref2 = confirm_res2["booking_reference"]
			confirm_sms2 = [
				m
				for m in get_mock_sms_outbox()
				if m.get("phone_e164") == phone_no_p4 and ref2 in (m.get("body") or "")
			]
			self.assertEqual(len(confirm_sms2), 1)
			self.assertTrue(confirm_sms2[0]["body"].endswith("/portal"))
			mock_mail2.assert_called_once()
			self.assertIn("/portal", mock_mail2.call_args.kwargs["message"])
			self.assertNotIn("booking_token", mock_mail2.call_args.kwargs["message"])

	def test_bracket_prefixed_package_ids_and_malformed_json_rejection(self) -> None:
		from ism.api.booking import _parse_package_ids_arg

		bracket_name = f"[STAGING TEST] {TEST_PREFIX}Brake Service & Inspection"
		bracket_pkg = self._create_bookable_package(
			"BracketPkg",
			package_name=bracket_name,
			customer_title="Bracket Prefixed Brake Service",
			applicable_model="All Models",
			self_serve_duration_minutes=60,
		)

		self.assertEqual(_parse_package_ids_arg(bracket_pkg.name), [bracket_pkg.name])
		self.assertEqual(
			_parse_package_ids_arg(f"{bracket_pkg.name}, {self.bookable_pkg.name}"),
			[bracket_pkg.name, self.bookable_pkg.name],
		)
		self.assertEqual(
			_parse_package_ids_arg(json.dumps([bracket_pkg.name])),
			[bracket_pkg.name],
		)

		for bad_value in (
			"{}",
			"{bad",
			"[]",
			"[bad",
			'["unterminated',
			"[123]",
			'["PKG"] trailing',
			"[PKG-1, PKG-2]",
			"[] foo",
		):
			with self.subTest(bad_package_ids=bad_value):
				with self.assertRaises(frappe.ValidationError):
					_parse_package_ids_arg(bad_value)

		frappe.set_user("Guest")
		slot_res = get_booking_slots(
			package_ids=bracket_pkg.name,
			start_date=str(self.base_date),
			days_ahead=1,
		)
		self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		self.assertTrue(slot_res["success"])
		self.assertGreaterEqual(slot_res["total_slots"], 1)

	def test_booking_boot_config_includes_vehicle_generations_and_filters_by_generation(self):
		boot = get_booking_config()
		self.assertTrue(boot["enabled"])
		self.assertIn("Model S", boot["vehicle_generations"])
		self.assertEqual(
			[g["generation_name"] for g in boot["vehicle_generations"]["Model Y"]],
			["Model Y Jan 2020 - Jan 2025", "Model Y Feb 2025"],
		)

		p_old_y = self._create_bookable_package(
			"MY_Old_Only",
			customer_title="Legacy Model Y Inspection",
			applicable_model="Model Y",
			applicable_generation="Model Y Jan 2020 - Jan 2025",
		)
		p_juniper_y = self._create_bookable_package(
			"MY_Juniper_Only",
			customer_title="Juniper Model Y Inspection",
			applicable_model="Model Y",
			applicable_generation="Model Y Feb 2025",
		)

		frappe.set_user("Guest")
		res = get_booking_packages(
			vehicle_model="Model Y",
			vehicle_year=2025,
			vehicle_generation="Model Y Feb 2025",
		)
		ids = {p["package_id"] for p in res["packages"]}
		self.assertIn(p_juniper_y.name, ids)
		self.assertNotIn(p_old_y.name, ids)


class TestBookingHostedRoute(ISMTestCase):
	"""Wave 3 hosted /book page controller, security headers, boot JSON, and asset hygiene tests."""

	_SETTINGS_FIELDS: tuple[str, ...] = (
		"enable_self_serve_booking",
		"sms_gateway_provider",
		"portal_session_signing_secret",
		"self_serve_booking_title",
		"self_serve_booking_intro",
		"self_serve_honeypot_enabled",
	)

	def setUp(self) -> None:
		super().setUp()
		self._prev_settings: dict[str, Any] = {}
		settings = frappe.get_single("ISM Settings")
		for field in self._SETTINGS_FIELDS:
			self._prev_settings[field] = settings.get(field)

		self._had_response_headers = hasattr(frappe.local, "response_headers")
		self._prev_response_headers = getattr(frappe.local, "response_headers", None)
		frappe.local.response_headers = {}

		self._set_settings(
			enable_self_serve_booking=1,
			sms_gateway_provider="Mock (Deterministic Test)",
			portal_session_signing_secret="test-p5-wave3-booking-secret-key-1234567890",
			self_serve_booking_title="Book Your EV Service",
			self_serve_booking_intro="Select your vehicle and drop-off time.",
			self_serve_honeypot_enabled=1,
		)

	def tearDown(self) -> None:
		self._set_settings(**self._prev_settings)
		if self._had_response_headers:
			frappe.local.response_headers = self._prev_response_headers
		elif hasattr(frappe.local, "response_headers"):
			delattr(frappe.local, "response_headers")
		super().tearDown()

	def _set_settings(self, **kwargs: Any) -> None:
		for field, value in kwargs.items():
			frappe.db.set_single_value("ISM Settings", field, value)
		frappe.db.commit()
		invalidate_shop_settings_cache()

	def test_website_route_rule_registered(self) -> None:
		from ism import hooks

		rules = getattr(hooks, "website_route_rules", [])
		self.assertIn({"from_route": "/book", "to_route": "book"}, rules)

	def test_get_context_headers_boot_json_and_toggle_reflection(self) -> None:
		import ism.www.book as book_page

		ctx = frappe._dict()
		book_page.get_context(ctx)

		self.assertEqual(ctx.no_cache, 1)
		self.assertFalse(ctx.show_sidebar)
		self.assertTrue(ctx.no_breadcrumbs)
		self.assertTrue(ctx.full_width)
		self.assertTrue(bool(ctx.asset_version))

		self.assertEqual(ctx.response_headers.get("X-Frame-Options"), "DENY")
		self.assertEqual(ctx.response_headers.get("Content-Security-Policy"), "frame-ancestors 'none'")
		self.assertEqual(frappe.local.response_headers.get("X-Frame-Options"), "DENY")
		self.assertEqual(
			frappe.local.response_headers.get("Content-Security-Policy"),
			"frame-ancestors 'none'",
		)

		self.assertNotIn("</", ctx.boot_config_json)
		boot_data = json.loads(ctx.boot_config_json)
		self.assertTrue(boot_data["enabled"])
		self.assertIsNone(boot_data["disabled_reason"])
		self.assertEqual(set(boot_data["shop"].keys()), {"name", "phone", "email"})
		self.assertTrue(bool(boot_data["form_nonce"]))
		self.assertEqual(boot_data["portal_url"], PORTAL_ROUTE)

		keys_found: set[str] = set()
		strings_found: list[str] = []
		_collect_keys_and_strings(boot_data, keys_found, strings_found)
		self.assertFalse(keys_found & _FORBIDDEN_INTERNAL_KEYS)
		for text in strings_found:
			self.assertIsNone(_FORBIDDEN_CUSTOMER_REGEX.search(text))

		self._set_settings(enable_self_serve_booking=0)
		frappe.local.response_headers = {}
		ctx_disabled = frappe._dict()
		book_page.get_context(ctx_disabled)

		disabled_data = json.loads(ctx_disabled.boot_config_json)
		self.assertFalse(disabled_data["enabled"])
		self.assertEqual(disabled_data["disabled_reason"], ERR_BOOKING_DISABLED)
		self.assertIsNone(disabled_data["form_nonce"])
		self.assertEqual(frappe.local.response_headers.get("X-Frame-Options"), "DENY")

	def test_live_website_response_emits_anti_iframe_headers(self) -> None:
		from frappe import app as frappe_app
		from frappe.auth import CookieManager
		from frappe.website.serve import get_response
		from werkzeug.wrappers import Request

		had_request = hasattr(frappe.local, "request")
		prev_request = getattr(frappe.local, "request", None)
		had_cm = hasattr(frappe.local, "cookie_manager")
		prev_cm = getattr(frappe.local, "cookie_manager", None)
		frappe.local.request = Request.from_values("/book", method="GET")
		frappe.local.cookie_manager = CookieManager()
		frappe.local.response_headers = {}
		try:
			response = get_response("book")
			self.assertEqual(response.status_code, 200)
			frappe_app.process_response(response)
		finally:
			if had_request:
				frappe.local.request = prev_request
			elif hasattr(frappe.local, "request"):
				delattr(frappe.local, "request")
			if had_cm:
				frappe.local.cookie_manager = prev_cm
			elif hasattr(frappe.local, "cookie_manager"):
				delattr(frappe.local, "cookie_manager")

		self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
		self.assertEqual(response.headers.get("Content-Security-Policy"), "frame-ancestors 'none'")

		html = response.get_data(as_text=True)
		self.assertIn('<meta name="referrer" content="no-referrer" />', html.split("</head>", 1)[0])
		self.assertIn('id="ism-booking-app"', html)
		self.assertIn('id="ism-booking-boot"', html)
		self.assertIn("/assets/ism/book/app.js", html)
		self.assertIsNone(_FORBIDDEN_CUSTOMER_REGEX.search(html))

	def test_hosted_assets_exist_and_enforce_security_hygiene(self) -> None:
		from pathlib import Path

		ism_root = Path(frappe.get_app_path("ism"))
		self.assertFalse((ism_root / "www" / "__init__.py").exists())

		required_files = [
			ism_root / "www" / "book.py",
			ism_root / "www" / "book.html",
			ism_root / "public" / "book" / "book.css",
			ism_root / "public" / "book" / "api.js",
			ism_root / "public" / "book" / "state.js",
			ism_root / "public" / "book" / "app.js",
		]
		for file_path in required_files:
			self.assertTrue(file_path.is_file(), f"Missing hosted booking asset: {file_path}")

		for asset_path in required_files[1:]:
			content = asset_path.read_text(encoding="utf-8")
			self.assertIsNone(
				_FORBIDDEN_CUSTOMER_REGEX.search(content),
				f"Forbidden buffer/leeway wording found in {asset_path.name}",
			)
			self.assertNotIn("innerHTML", content, f"innerHTML is forbidden in {asset_path.name}")
			self.assertNotIn("localStorage", content, f"localStorage is forbidden in {asset_path.name}")
			self.assertNotIn("sessionStorage", content, f"sessionStorage is forbidden in {asset_path.name}")

