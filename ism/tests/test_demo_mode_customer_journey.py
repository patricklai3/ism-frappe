# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""End-to-end tests for Customer Journey Demo Mode (/book, /portal, and Repair Order stock bypass)."""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_days, nowdate

from ism.booking.anti_abuse import clear_booking_rate_limits, issue_booking_form_nonce
from ism.booking.contracts import ERR_SMS_UNCONFIGURED
from ism.booking.service import (
	get_booking_boot_config,
	get_public_booking_packages,
	get_public_booking_slots,
	initiate_booking_otp,
	verify_and_commit_booking,
)
from ism.ism.doctype.repair_order.billing import process_ro_payment_and_release
from ism.portal.auth import (
	clear_portal_auth_rate_limits,
	get_public_portal_branding,
	request_portal_login_otp,
	verify_portal_login_otp,
)
from ism.scheduling.self_serve import reschedule_self_serve_appointment
from ism.scheduling.time_utils import invalidate_shop_settings_cache
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestDemoModeCustomerJourney(ISMTestCase):
	def setUp(self):
		super().setUp()
		clear_booking_rate_limits()
		clear_portal_auth_rate_limits()
		invalidate_shop_settings_cache()

	def tearDown(self):
		clear_booking_rate_limits()
		clear_portal_auth_rate_limits()
		invalidate_shop_settings_cache()
		super().tearDown()

	def _configure_settings(self, *, demo_mode: int, enable_booking: int = 0, sms_provider: str = "Disabled"):
		settings = frappe.get_single("ISM Settings")
		settings.flags.ignore_self_serve_sms_warning = True
		settings.customer_journey_demo_mode = demo_mode
		settings.enable_self_serve_booking = enable_booking
		settings.sms_gateway_provider = sms_provider
		settings.save(ignore_permissions=True)
		invalidate_shop_settings_cache()
		frappe.clear_cache(doctype="ISM Settings")

	def _clear_real_self_serve_catalog_and_capacity(self):
		frappe.db.sql("UPDATE `tabService Package` SET allow_self_serve_booking = 0")
		frappe.db.sql("UPDATE `tabService Bay` SET is_active = 0")
		frappe.db.sql("UPDATE `tabEmployee` SET status = 'Inactive' WHERE status = 'Active'")

	def test_demo_mode_off_preserves_sms_gate(self):
		self._configure_settings(demo_mode=0, enable_booking=1, sms_provider="Disabled")
		boot = get_booking_boot_config()
		self.assertFalse(boot["enabled"])
		self.assertFalse(boot["demo_mode"])
		self.assertEqual(boot["disabled_reason"], ERR_SMS_UNCONFIGURED)

	def test_demo_mode_booking_without_sms_packages_or_capacity(self):
		self._configure_settings(demo_mode=1, enable_booking=0, sms_provider="Disabled")
		self._clear_real_self_serve_catalog_and_capacity()

		boot = get_booking_boot_config()
		self.assertTrue(boot["enabled"])
		self.assertTrue(boot["demo_mode"])
		self.assertIsNone(boot["disabled_reason"])

		pkg_res = get_public_booking_packages(vehicle_model="Model 3", vehicle_year=2023)
		self.assertTrue(pkg_res["success"])
		self.assertTrue(pkg_res["is_demo_catalog"])
		self.assertGreaterEqual(len(pkg_res["packages"]), 1)
		pkg_id = pkg_res["packages"][0]["package_id"]
		self.assertTrue(pkg_id.startswith("demo-"))

		start_date = add_days(nowdate(), 1)
		slots_res = get_public_booking_slots(
			package_ids=[pkg_id],
			start_date=start_date,
			days_ahead=3,
			vehicle_model="Model 3",
			vehicle_year=2023,
		)
		self.assertTrue(slots_res["success"])
		self.assertGreater(slots_res["total_slots"], 0)
		first_day = slots_res["days"][0]
		first_slot = first_day["slots"][0]

		nonce = issue_booking_form_nonce()
		otp_res = initiate_booking_otp(
			package_ids=[pkg_id],
			target_date=first_day["date"],
			customer_arrival_time=first_slot["customer_arrival_time"],
			customer_name="Demo Driver",
			phone="4165550199",
			vehicle_model="Model 3",
			vehicle_year=2023,
			vehicle_color="Pearl White",
			form_nonce=nonce,
		)
		self.assertTrue(otp_res["success"])
		self.assertTrue(otp_res["demo_mode"])
		self.assertTrue(otp_res["challenge_id"].startswith("demo-"))

		# Commit directly with demo-bypass (no OTP code needed)
		commit_1 = verify_and_commit_booking(
			challenge_id="demo-bypass",
			otp_code=None,
			package_ids=[pkg_id],
			target_date=first_day["date"],
			customer_arrival_time=first_slot["customer_arrival_time"],
			customer_name="Demo Driver",
			phone="4165550199",
			vehicle_model="Model 3",
			vehicle_year=2023,
			vehicle_color="Pearl White",
			form_nonce=nonce,
			set_response_cookie=False,
		)
		self.assertTrue(commit_1["success"])
		se_name = commit_1["booking_reference"]
		self.track("Schedule Entry", se_name)
		self.assertIsNone(frappe.db.get_value("Schedule Entry", se_name, "service_bay"))

		# Same-day repeat booking with identical phone succeeds in Demo Mode
		second_slot = first_day["slots"][1] if len(first_day["slots"]) > 1 else first_slot
		commit_2 = verify_and_commit_booking(
			challenge_id=otp_res["challenge_id"],
			otp_code=None,
			package_ids=[pkg_id],
			target_date=first_day["date"],
			customer_arrival_time=second_slot["customer_arrival_time"],
			customer_name="Demo Driver Two",
			phone="4165550199",
			vehicle_model="Model Y",
			vehicle_year=2024,
			vehicle_color="Stealth Grey",
			form_nonce=nonce,
			set_response_cookie=False,
		)
		self.assertTrue(commit_2["success"])
		self.track("Schedule Entry", commit_2["booking_reference"])

		# Portal reschedule also succeeds without bays/techs in Demo Mode
		resched_day = slots_res["days"][-1]
		resched_slot = resched_day["slots"][-1]
		if resched_day["date"] != first_day["date"] or resched_slot["customer_arrival_time"] != first_slot["customer_arrival_time"]:
			resched = reschedule_self_serve_appointment(
				se_name,
				target_date=resched_day["date"],
				customer_arrival_time=resched_slot["customer_arrival_time"],
				reason="Demo reschedule",
				emit_notification=False,
			)
			self.assertEqual(resched["reschedule_count"], 1)

	def test_demo_mode_portal_otp_login_without_sms(self):
		self._configure_settings(demo_mode=1, enable_booking=0, sms_provider="Disabled")

		branding = get_public_portal_branding()
		self.assertTrue(branding["demo_mode"])

		challenge = request_portal_login_otp("+14165550188")
		self.assertTrue(challenge["success"])
		self.assertTrue(challenge["demo_mode"])
		self.assertEqual(challenge["demo_code"], "000000")
		self.assertTrue(challenge["challenge_id"].startswith("demo-"))

		verified = verify_portal_login_otp(
			challenge["challenge_id"],
			"000000",
			raw_phone="+14165550188",
			set_response_cookie=False,
		)
		self.assertTrue(verified["verified"])
		self.assertIsNotNone(verified["session"])
		self.assertEqual(verified["session"].phone_e164, "+14165550188")

	def test_demo_mode_ro_bypasses_parts_stock_holds_and_checkout(self):
		manifest = self.get_manifest()
		customer = manifest.customers["retail_1"]
		vehicle = manifest.vehicles["veh_model3_liam"]
		company = manifest.company
		cash_mop = manifest.modes_of_payment["cash"]
		unstocked_code = f"{TEST_PREFIX}DEMO-UNSTOCKED-PART"

		if not frappe.db.exists("Item", unstocked_code):
			self.create_test_item(
				unstocked_code,
				is_stock_item=1,
				standard_rate=150.0,
				item_name="Demo Unstocked Control Arm",
			)

		ro = frappe.get_doc(
			{
				"doctype": "Repair Order",
				"company": company,
				"customer": customer,
				"vehicle": vehicle,
				"odometer_in": 45000,
				"status": "Pending Authorization",
				"approval_status": "Approved",
				"services": [
					{
						"service_title": "Control Arm Replacement",
						"status": "Approved",
						"labor_hours": 1.5,
						"labor_rate": 140.0,
						"parts": json.dumps(
							[
								{
									"item_code": unstocked_code,
									"item_name": "Demo Unstocked Control Arm",
									"qty": 1,
									"rate": 150.0,
								}
							]
						),
						"parts_total": 150.0,
					}
				],
			}
		)

		# With Demo Mode OFF, unstocked part triggers Waiting on Parts
		self._configure_settings(demo_mode=0, enable_booking=0, sms_provider="Disabled")
		ro.insert(ignore_permissions=True)
		self.track(ro)
		self.assertEqual(ro.status, "Waiting on Parts")

		# With Demo Mode ON, stock check passes and RO moves to Authorized - In Progress -> Checkout
		self._configure_settings(demo_mode=1, enable_booking=0, sms_provider="Disabled")
		ro._update_parts_waiting_status()
		self.assertEqual(ro.status, "Authorized - In Progress")
		ro.save(ignore_permissions=True)

		ro.pick_up_vehicle()
		ro.reload()
		ro.services[0].is_completed = 1
		ro.save(ignore_permissions=True)
		ro.park_vehicle(odometer_out=45005)
		ro.reload()

		checkout = process_ro_payment_and_release(
			ro,
			payment_method=cash_mop,
			paid_amount=0,
			odometer_out=45005,
		)
		si_name = checkout["sales_invoice"]
		self.track("Sales Invoice", si_name)
		self.assertEqual(frappe.db.get_value("Sales Invoice", si_name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", si_name, "update_stock"), 0)
