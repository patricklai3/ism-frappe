# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import io
import json
import types
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate
from frappe.website.serve import get_response
from werkzeug.datastructures import FileStorage, ImmutableMultiDict
from werkzeug.wrappers import Request
from werkzeug.test import EnvironBuilder

from ism.api import board as board_api
from ism.api import tech as tech_api
from ism.board.projections import ensure_thread_for_ro, recompute_thread_projection, ro_thread_key
from ism.patches.v1_0 import prune_routine_board_system_events
from ism.permissions import get_realtime_recipients_for_ro
from ism.portal import pin_vault
from ism.tests.base import ISMTestCase, TEST_PREFIX
from ism.www import tech as tech_page


FORBIDDEN_PRICING_KEYS = frozenset({
	"grand_total",
	"labor_rate",
	"labor_amount",
	"parts_total",
	"total_amount",
	"net_total",
})


def _find_keys_recursive(obj, forbidden: frozenset[str]) -> list[str]:
	hits: list[str] = []
	if isinstance(obj, dict):
		for key, value in obj.items():
			if key in forbidden:
				hits.append(key)
			hits.extend(_find_keys_recursive(value, forbidden))
	elif isinstance(obj, list):
		for item in obj:
			hits.extend(_find_keys_recursive(item, forbidden))
	return hits


def _make_png_bytes() -> bytes:
	return (
		b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
		b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\xdac`\xf8\xff\xff?"
		b"\x00\x05\xfe\x02\xfe\xa7V\xbd\x00\x00\x00\x00IEND\xaeB`\x82"
	)


class TestTechExecutionAndIsolation(ISMTestCase):
	"""End-to-end tests for strict technician isolation, SE->RO superseding, odometer enforcement, and exception-only board."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		if hasattr(frappe.local, "request"):
			delattr(frappe.local, "request")
		frappe.local.form_dict = frappe._dict()
		frappe.local.flags.pop("redirect_location", None)
		super().tearDown()

	def _create_user_with_roles(self, prefix: str, roles: list[str], *, link_employee: bool = True) -> tuple[str, str | None]:
		email = f"{TEST_PREFIX}{prefix}_{frappe.generate_hash(length=6)}@example.test".lower()
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": prefix,
			"enabled": 1,
			"user_type": "System User",
			"roles": [{"role": r} for r in roles],
		}).insert(ignore_permissions=True)
		self.track(user)

		emp_name = None
		if link_employee:
			emp = frappe.get_doc({
				"doctype": "Employee",
				"first_name": f"Tech {prefix}",
				"employee_name": f"Tech {prefix}",
				"status": "Active",
				"designation": "Technician",
				"company": self.get_manifest().company,
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"user_id": email,
			}).insert(ignore_permissions=True)
			self.track(emp)
			emp_name = emp.name
		return email, emp_name

	def _create_ro(
		self,
		*,
		status: str = "Authorized - In Progress",
		approval_status: str = "Approved",
		service_status: str = "Approved",
		manual_parts_hold: int = 0,
		odometer_in: int = 25000,
		assigned_technician: str | None = None,
	):
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(
			customer=customer.name,
			model_year=2023,
			model="Model Y",
			manufacturer="Tesla",
			color="Midnight Silver",
			license_plate=f"EV{frappe.generate_hash(length=4).upper()}",
		)
		svc_row = {
			"service_title": "Front Brake Service",
			"status": service_status,
			"labor_hours": 1.5,
			"labor_rate": 160.0,
			"labor_amount": 240.0,
			"parts_total": 0.0,
			"total_amount": 240.0,
		}
		if assigned_technician:
			svc_row["assigned_technician"] = assigned_technician

		ro = self.create_test_repair_order(
			customer.name,
			vehicle.name,
			odometer_in=odometer_in,
			status=status,
			approval_status=approval_status,
			manual_parts_hold=manual_parts_hold,
			services=[svc_row],
		)
		return ro, customer, vehicle

	def _attach_request(self, path: str) -> None:
		builder = EnvironBuilder(path=path, method="GET")
		req = Request(builder.get_environ())
		frappe.local.request = req
		frappe.local.form_dict = frappe._dict(req.args.to_dict(flat=True))

	def _assert_security_headers(self, headers) -> None:
		self.assertEqual(headers.get("X-Frame-Options"), "DENY")
		self.assertEqual(headers.get("Content-Security-Policy"), "frame-ancestors 'none'")
		self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
		self.assertEqual(headers.get("Cache-Control"), "no-store, private")

	def test_strict_technician_isolation_desk_api_pin_media_and_realtime(self):
		"""Unassigned, unlinked, unapproved, and Waiting on Parts ROs are strictly hidden and blocked for technicians."""
		tech_a, emp_a = self._create_user_with_roles("tech_iso_a", ["ISM Technician"], link_employee=True)
		tech_b, emp_b = self._create_user_with_roles("tech_iso_b", ["ISM Technician"], link_employee=True)
		tech_unlinked, _ = self._create_user_with_roles("tech_iso_nolink", ["ISM Technician"], link_employee=False)
		mgr_email, _ = self._create_user_with_roles("tech_iso_mgr", ["ISM Manager"], link_employee=False)

		# 1. Approved & ready RO assigned to Tech A
		ro_a, _, _ = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		se_a = self.create_test_schedule_entry(
			repair_order=ro_a.name,
			assigned_technician=emp_a,
			status="Confirmed",
			entry_type="Repair",
		)

		# 2. Approved & ready RO assigned to Tech B (unassigned for Tech A)
		ro_b, _, _ = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		se_b = self.create_test_schedule_entry(
			repair_order=ro_b.name,
			assigned_technician=emp_b,
			status="Confirmed",
			entry_type="Repair",
		)

		# 3. Unapproved RO assigned to Tech A (must remain hidden)
		ro_unapproved, _, _ = self._create_ro(
			status="Pending Authorization",
			approval_status="Pending Approval",
			service_status="Pending Approval",
		)
		self.create_test_schedule_entry(
			repair_order=ro_unapproved.name,
			assigned_technician=emp_a,
			status="Confirmed",
			entry_type="Repair",
		)

		# 4. Waiting on Parts RO assigned to Tech A (must remain hidden)
		ro_parts, _, _ = self._create_ro(
			status="Waiting on Parts",
			approval_status="Approved",
			manual_parts_hold=1,
		)
		self.create_test_schedule_entry(
			repair_order=ro_parts.name,
			assigned_technician=emp_a,
			status="Confirmed",
			entry_type="Repair",
		)

		# 5. Unlinked Schedule Entry assigned to Tech B (unassigned for Tech A)
		se_b_unlinked = self.create_test_schedule_entry(
			assigned_technician=emp_b,
			status="Confirmed",
			entry_type="Diagnostic",
			scheduled_date=add_days(nowdate(), 1),
		)

		# Put a PIN and a media attachment on ro_b (Tech B's RO) to verify IDOR protection
		pin_vault.submit_ro_pin(ro_b.name, "8842")
		for sai_name in frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro_b.name},
			pluck="name",
		):
			self.track("Shop Action Item", sai_name)

		tkey_b = ensure_thread_for_ro(ro_b.name)
		png_bytes = _make_png_bytes()
		fake_file = FileStorage(
			stream=io.BytesIO(png_bytes),
			filename="secret_b.png",
			content_type="image/png",
		)
		fake_req = types.SimpleNamespace(files=ImmutableMultiDict([("files", fake_file)]), headers={})
		with patch.object(frappe, "request", fake_req, create=True):
			cap_b = board_api.post_board_capture(
				repair_order=ro_b.name,
				text="Tech B private capture",
				metadata={"skip_ai": True},
			)
		ev_b_name = cap_b["event"]["name"]
		self.track("Shop Board Event", ev_b_name)
		media_attachments = cap_b["event"].get("media_attachments") or []
		file_ref_b = media_attachments[0]["file_ref"] if media_attachments else None
		file_url_b = media_attachments[0].get("file_url") if media_attachments else None
		if file_url_b:
			for f_name in frappe.get_all("File", filters={"file_url": file_url_b}, pluck="name"):
				self.track("File", f_name)

		# Verify realtime recipients helper
		rec_a = set(get_realtime_recipients_for_ro(ro_a.name))
		self.assertIn(tech_a, rec_a)
		self.assertIn(mgr_email, rec_a)
		self.assertNotIn(tech_b, rec_a)

		rec_unapproved = set(get_realtime_recipients_for_ro(ro_unapproved.name))
		self.assertNotIn(tech_a, rec_unapproved)
		self.assertIn(mgr_email, rec_unapproved)

		rec_parts = set(get_realtime_recipients_for_ro(ro_parts.name))
		self.assertNotIn(tech_a, rec_parts)
		self.assertIn(mgr_email, rec_parts)

		# Verify Unlinked Tech fails closed everywhere
		frappe.set_user(tech_unlinked)
		unlinked_jobs = tech_api.list_my_jobs()
		self.assertTrue(unlinked_jobs["ok"])
		self.assertFalse(unlinked_jobs["employee_linked"])
		self.assertEqual(unlinked_jobs["jobs"], [])
		self.assertEqual(frappe.get_list("Repair Order", pluck="name"), [])
		self.assertEqual(frappe.get_list("Schedule Entry", pluck="name"), [])
		self.assertEqual(board_api.list_board_threads()["threads"], [])

		# Verify Tech A sees ONLY ro_a (never ro_b, ro_unapproved, ro_parts)
		frappe.set_user(tech_a)
		desk_ros = set(frappe.get_list("Repair Order", pluck="name"))
		self.assertIn(ro_a.name, desk_ros)
		for hidden_ro in (ro_b.name, ro_unapproved.name, ro_parts.name):
			self.assertNotIn(hidden_ro, desk_ros)

		desk_ses = set(frappe.get_list("Schedule Entry", pluck="name"))
		self.assertIn(se_a.name, desk_ses)
		self.assertNotIn(se_b.name, desk_ses)
		self.assertNotIn(se_b_unlinked.name, desk_ses)

		tech_jobs = tech_api.list_my_jobs()
		self.assertTrue(tech_jobs["employee_linked"])
		job_ids = {j["id"] for j in tech_jobs["jobs"]}
		self.assertIn(ro_a.name, job_ids)
		for hidden_id in (ro_b.name, ro_unapproved.name, ro_parts.name, se_a.name, se_b_unlinked.name):
			self.assertNotIn(hidden_id, job_ids)

		board_threads = board_api.list_board_threads(assigned_only=False)
		thread_keys = {t["thread_key"] for t in board_threads["threads"]}
		self.assertIn(ro_thread_key(ro_a.name), thread_keys)
		self.assertNotIn(tkey_b, thread_keys)
		self.assertNotIn(ro_thread_key(ro_unapproved.name), thread_keys)
		self.assertNotIn(ro_thread_key(ro_parts.name), thread_keys)

		# Direct access & API mutations on hidden ROs/SEs raise PermissionError
		for blocked_ro in (ro_b.name, ro_unapproved.name, ro_parts.name):
			self.assertFalse(frappe.has_permission("Repair Order", "read", doc=blocked_ro))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Repair Order", blocked_ro).check_permission("read")
			with self.assertRaises(frappe.PermissionError):
				tech_api.get_tech_repair_order(blocked_ro)
			with self.assertRaises(frappe.PermissionError):
				tech_api.pick_up_vehicle(blocked_ro)
			with self.assertRaises(frappe.PermissionError):
				board_api.get_board_thread(repair_order=blocked_ro)
			with self.assertRaises(frappe.PermissionError):
				board_api.get_thread_delta(thread_key=ro_thread_key(blocked_ro))
			with self.assertRaises(frappe.PermissionError):
				board_api.post_board_capture(repair_order=blocked_ro, text="Blocked", metadata={"skip_ai": True})

		self.assertFalse(frappe.has_permission("Shop Board Thread", "read", doc=tkey_b))
		self.assertFalse(frappe.has_permission("Shop Board Event", "read", doc=ev_b_name))

		with self.assertRaises(frappe.PermissionError):
			board_api.reveal_ro_pin(ro_b.name)
		if file_ref_b:
			with self.assertRaises(frappe.PermissionError):
				board_api.get_media(event_name=ev_b_name, file_ref=file_ref_b)

		self.assertFalse(frappe.has_permission("Schedule Entry", "read", doc=se_b_unlinked.name))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Schedule Entry", se_b_unlinked.name).check_permission("read")
		with self.assertRaises(frappe.PermissionError):
			tech_api.get_tech_schedule_entry(se_b_unlinked.name)
		with self.assertRaises(frappe.PermissionError):
			tech_api.reveal_schedule_entry_pin(se_b_unlinked.name)

	def test_schedule_entry_to_repair_order_superseding(self):
		"""Unlinked SE shows as SCHEDULE_ENTRY, hides on RO link, and yields to approved RO."""
		tech_user, tech_emp = self._create_user_with_roles("tech_sup", ["ISM Technician"], link_employee=True)
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name, model="Model 3", model_year=2022, color="Blue")

		se = self.create_test_schedule_entry(
			customer=customer.name,
			repair_vehicle=vehicle.name,
			assigned_technician=tech_emp,
			entry_type="Diagnostic",
			status="Confirmed",
			waiter_preference="Waiter",
			notes="Clicking noise on low-speed turn",
			scheduled_date=add_days(nowdate(), 1),
		)
		pin_vault.submit_schedule_entry_pin(se.name, "0192")
		for sai_name in frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Schedule Entry", "reference_name": se.name},
			pluck="name",
		):
			self.track("Shop Action Item", sai_name)

		# 1. Unlinked SE appears in list_my_jobs(), loads in get_tech_schedule_entry(), and reveals PIN
		frappe.set_user(tech_user)
		jobs_1 = tech_api.list_my_jobs()
		se_cards = [j for j in jobs_1["jobs"] if j["kind"] == "SCHEDULE_ENTRY" and j["id"] == se.name]
		self.assertEqual(len(se_cards), 1)
		self.assertTrue(se_cards[0]["has_pin"])
		self.assertEqual(se_cards[0]["waiter_preference"], "Waiter")

		se_detail = tech_api.get_tech_schedule_entry(se.name)
		self.assertTrue(se_detail["ok"])
		self.assertEqual(se_detail["schedule_entry"]["name"], se.name)
		self.assertNotIn("0192", json.dumps(se_detail))
		self.assertEqual(_find_keys_recursive(se_detail, FORBIDDEN_PRICING_KEYS), [])

		pin_res = tech_api.reveal_schedule_entry_pin(se.name)
		self.assertTrue(pin_res["available"])
		self.assertEqual(pin_res["pin"], "0192")

		# 2. Link SE to an unapproved RO -> SE disappears, unapproved RO stays hidden
		frappe.set_user("Administrator")
		ro, _, _ = self._create_ro(
			status="Pending Authorization",
			approval_status="Pending Approval",
			service_status="Pending Approval",
		)
		se.reload()
		se.repair_order = ro.name
		se.save(ignore_permissions=True)

		frappe.set_user(tech_user)
		jobs_2 = tech_api.list_my_jobs()
		ids_2 = {j["id"] for j in jobs_2["jobs"]}
		self.assertNotIn(se.name, ids_2)
		self.assertNotIn(ro.name, ids_2)

		se_sup_unapproved = tech_api.get_tech_schedule_entry(se.name)
		self.assertEqual(se_sup_unapproved["superseded_by_ro"], ro.name)
		self.assertFalse(se_sup_unapproved["ro_eligible"])

		# 3. Approve RO & set Authorized - In Progress -> RO supersedes SE cleanly
		frappe.set_user("Administrator")
		ro.reload()
		ro.services[0].status = "Approved"
		ro.approval_status = "Approved"
		ro.status = "Authorized - In Progress"
		ro.save(ignore_permissions=True)

		frappe.set_user(tech_user)
		jobs_3 = tech_api.list_my_jobs()
		ids_3 = {j["id"] for j in jobs_3["jobs"]}
		self.assertNotIn(se.name, ids_3)
		self.assertIn(ro.name, ids_3)

		se_sup_ready = tech_api.get_tech_schedule_entry(se.name)
		self.assertEqual(se_sup_ready["superseded_by_ro"], ro.name)
		self.assertTrue(se_sup_ready["ro_eligible"])

	def test_happy_path_execution_zero_pricing_and_mandatory_odometer(self):
		"""Test full 4-state lifecycle, zero pricing leak, and mandatory odometer_out on every lot park."""
		tech_user, tech_emp = self._create_user_with_roles("tech_exec", ["ISM Technician"], link_employee=True)
		ro, _, veh = self._create_ro(
			status="Authorized - In Progress",
			approval_status="Approved",
			odometer_in=41200,
		)
		se = self.create_test_schedule_entry(
			repair_order=ro.name,
			assigned_technician=tech_emp,
			status="Confirmed",
			entry_type="Repair",
		)
		svc_row_id = ro.services[0].name

		frappe.set_user(tech_user)
		jobs = tech_api.list_my_jobs()
		self.assertEqual(_find_keys_recursive(jobs, FORBIDDEN_PRICING_KEYS), [])
		ro_cards = [j for j in jobs["jobs"] if j["kind"] == "REPAIR_ORDER" and j["id"] == ro.name]
		self.assertEqual(len(ro_cards), 1)
		expected_display = f"{veh.model_year} {veh.color} {veh.model} ({veh.license_plate})"
		self.assertEqual(ro_cards[0]["vehicle_display"], expected_display)
		self.assertEqual(ro_cards[0]["vehicle_compact_display"], f"{veh.color} {veh.model}")
		self.assertEqual(ro_cards[0]["vehicle_info"]["display"], expected_display)
		self.assertEqual(ro_cards[0]["vehicle_generation"], veh.vehicle_generation or "")
		self.assertEqual(ro_cards[0]["recommended_manual_url"], veh.recommended_manual_url or "")

		detail_1 = tech_api.get_tech_repair_order(ro.name)
		self.assertEqual(_find_keys_recursive(detail_1, FORBIDDEN_PRICING_KEYS), [])
		self.assertEqual(detail_1["repair_order"]["lifecycle_state"], "READY_FOR_PICKUP")
		self.assertEqual(detail_1["repair_order"]["vehicle_display"], expected_display)
		self.assertEqual(detail_1["repair_order"]["vehicle_info"]["display"], expected_display)
		self.assertTrue(detail_1["repair_order"]["vehicle_generation"])
		self.assertTrue(
			detail_1["repair_order"]["recommended_manual_url"].startswith("https://service.tesla.com/")
		)

		# Pick up car into bay
		picked = tech_api.pick_up_vehicle(ro.name)
		self.assertEqual(picked["repair_order"]["lifecycle_state"], "IN_BAY_WORKING")
		self.assertEqual(picked["repair_order"]["vehicle_location"], "In Bay / Being Serviced")

		# Temporary mid-job lot park MUST reject missing or lower odometer_out
		for bad_odo in (None, "", 41199):
			with self.assertRaises((frappe.ValidationError, frappe.MandatoryError)):
				tech_api.drop_off_vehicle(ro.name, odometer_out=bad_odo, temporary=True)

		# Valid temporary mid-job lot park moves to In Customer Lot and stays Authorized - In Progress
		temp_parked = tech_api.drop_off_vehicle(ro.name, odometer_out=41202, temporary=True)
		self.assertEqual(temp_parked["repair_order"]["lifecycle_state"], "READY_FOR_PICKUP")
		self.assertEqual(temp_parked["repair_order"]["vehicle_location"], "In Customer Lot")
		self.assertEqual(int(temp_parked["repair_order"]["odometer_out"]), 41202)
		self.assertEqual(temp_parked["repair_order"]["status"], "Authorized - In Progress")

		# Pick up again, complete approved service -> READY_FOR_DROPOFF
		tech_api.pick_up_vehicle(ro.name)
		toggled = tech_api.toggle_service_item(ro.name, svc_row_id, is_completed=True)
		self.assertEqual(toggled["repair_order"]["lifecycle_state"], "READY_FOR_DROPOFF")

		# Temporary park even when READY_FOR_DROPOFF (all services completed) keeps Authorized - In Progress
		temp_parked_full = tech_api.drop_off_vehicle(ro.name, odometer_out=41204, temporary=True)
		self.assertEqual(temp_parked_full["repair_order"]["lifecycle_state"], "READY_FOR_PICKUP")
		self.assertEqual(temp_parked_full["repair_order"]["status"], "Authorized - In Progress")
		self.assertEqual(temp_parked_full["repair_order"]["vehicle_location"], "In Customer Lot")

		# Re-pick up for final drop-off
		tech_api.pick_up_vehicle(ro.name)

		# Final drop-off MUST reject missing or lower odometer_out
		for bad_odo in (None, "", 41150):
			with self.assertRaises((frappe.ValidationError, frappe.MandatoryError)):
				tech_api.drop_off_vehicle(ro.name, odometer_out=bad_odo, temporary=False)

		# Valid final drop-off completes RO and marks linked Schedule Entry Completed
		finished = tech_api.drop_off_vehicle(ro.name, odometer_out=41205, temporary=False)
		self.assertEqual(finished["repair_order"]["lifecycle_state"], "COMPLETED")
		self.assertEqual(finished["repair_order"]["status"], "Work Completed")
		self.assertEqual(finished["repair_order"]["vehicle_location"], "Completed / Parked in Lot")
		self.assertEqual(int(finished["repair_order"]["odometer_out"]), 41205)

		frappe.set_user("Administrator")
		se.reload()
		self.assertEqual(se.status, "Completed")

	def test_exception_only_board_and_prune_patch_idempotency(self):
		"""Routine Shop Action Items stay out of /board while prune_routine_board_system_events cleans legacy rows."""
		mgr_user, _ = self._create_user_with_roles("tech_patch_mgr", ["ISM Manager"], link_employee=False)
		tech_user, tech_emp = self._create_user_with_roles("tech_patch_usr", ["ISM Technician"], link_employee=True)
		ro, _, _ = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		self.create_test_schedule_entry(repair_order=ro.name, assigned_technician=tech_emp, status="Confirmed")
		tkey = ensure_thread_for_ro(ro.name)

		# Routine action items (portal approval, portal pin, square, parts_ready) create 0 board events & 0 board feed items
		for dedup_prefix, title in (
			("portal:approval", f"Customer Approved Estimate for {ro.name}"),
			("portal:pin", f"Customer Provided Vehicle PIN-to-Drive for {ro.name}"),
			("square:deposit", f"Square Deposit Paid for {ro.name}"),
			("parts_ready", f"Parts Ready for {ro.name}"),
		):
			sai = frappe.get_doc({
				"doctype": "Shop Action Item",
				"title": title,
				"category": "General",
				"priority": "Medium",
				"status": "Open",
				"source": "Agent",
				"dedup_key": f"{dedup_prefix}:{ro.name}",
				"reference_doctype": "Repair Order",
				"reference_name": ro.name,
			}).insert(ignore_permissions=True)
			self.track(sai)

		self.assertEqual(
			frappe.db.count("Shop Board Event", {"thread": tkey, "payload_type": "STATE_MUTATION"}),
			0,
		)

		for actor in (mgr_user, tech_user):
			frappe.set_user(actor)
			feed = board_api.list_board_action_feed(status="Open", repair_order=ro.name)
			self.assertEqual(feed["items"], [])

		# Simulate legacy pre-existing routine STATE_MUTATION event alongside a real TECH capture
		frappe.set_user("Administrator")
		human_ev = frappe.get_doc({
			"doctype": "Shop Board Event",
			"thread": tkey,
			"thread_type": "REPAIR_ORDER",
			"repair_order": ro.name,
			"actor_type": "TECH",
			"intent_tag": "BLOCKER",
			"payload_type": "TEXT",
			"target_role": "ALL",
			"body_text": "Stripped caliper bolt on front left",
			"resolution_state": "NONE",
			"ai_status": "NONE",
		}).insert(ignore_permissions=True)
		self.track(human_ev)

		legacy_ev = frappe.get_doc({
			"doctype": "Shop Board Event",
			"thread": tkey,
			"thread_type": "REPAIR_ORDER",
			"repair_order": ro.name,
			"actor_type": "SYSTEM",
			"intent_tag": "shop_action_item",
			"payload_type": "STATE_MUTATION",
			"target_role": "ALL",
			"body_text": "Customer Approved Estimate",
			"idempotency_key": f"action_item:legacy-{frappe.generate_hash(length=6)}",
			"resolution_state": "NONE",
			"ai_status": "NONE",
		}).insert(ignore_permissions=True)
		self.track(legacy_ev)
		frappe.db.set_value("Shop Board Thread", tkey, "last_event_preview", "Customer Approved Estimate", update_modified=False)

		# Execute cleanup patch twice (must be idempotent)
		prune_routine_board_system_events.execute()
		prune_routine_board_system_events.execute()

		self.assertFalse(frappe.db.exists("Shop Board Event", legacy_ev.name))
		self.assertTrue(frappe.db.exists("Shop Board Event", human_ev.name))
		recompute_thread_projection(tkey, publish=False)
		preview = frappe.db.get_value("Shop Board Thread", tkey, "last_event_preview")
		self.assertIn("Stripped caliper bolt", preview or "")

	def test_tech_ssr_headers_assets_unlinked_boot_and_se_redirect(self):
		"""Verify /tech SSR security headers, assets, unlinked employee boot, and ?se= -> ?ro= redirect."""
		tech_unlinked, _ = self._create_user_with_roles("tech_ssr_nolink", ["ISM Technician"], link_employee=False)
		tech_linked, emp_linked = self._create_user_with_roles("tech_ssr_linked", ["ISM Technician"], link_employee=True)

		# 1. Static assets exist and are non-empty
		ism_root = Path(frappe.get_app_path("ism"))
		for rel in (
			Path("www") / "tech.py",
			Path("www") / "tech.html",
			Path("public") / "tech" / "tech.css",
			Path("public") / "tech" / "api.js",
			Path("public") / "tech" / "app.js",
		):
			full = ism_root / rel
			self.assertTrue(full.is_file(), f"Missing /tech asset: {full}")
			self.assertGreater(full.stat().st_size, 0, f"Empty /tech asset: {full}")

		# 2. Live /tech HTTP response for unlinked tech
		frappe.set_user(tech_unlinked)
		self._attach_request("/tech")
		resp = get_response("tech")
		self.assertEqual(resp.status_code, 200)
		html = resp.get_data(as_text=True)
		self.assertIn("window.ISM_TECH_BOOT =", html)
		self.assertIn("/assets/ism/shared/vehicle_display.css?v=", html)
		self.assertIn("/assets/ism/shared/vehicle_display.js?v=", html)
		self.assertIn("/assets/ism/tech/tech.css?v=", html)
		self.assertIn("/assets/ism/tech/app.js?v=", html)

		ctx_unlinked = frappe._dict()
		tech_page.get_context(ctx_unlinked)
		self._assert_security_headers(ctx_unlinked.response_headers)
		boot_unlinked = json.loads(ctx_unlinked.boot_json)
		self.assertFalse(boot_unlinked["employee_linked"])

		# 3. /tech?se=<linked_to_eligible_ro> auto-redirects to /tech?ro=<id>
		frappe.set_user("Administrator")
		ro, _, _ = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		se = self.create_test_schedule_entry(
			repair_order=ro.name,
			assigned_technician=emp_linked,
			status="Confirmed",
			entry_type="Diagnostic",
		)

		frappe.set_user(tech_linked)
		frappe.local.flags.pop("redirect_location", None)
		self._attach_request(f"/tech?se={se.name}")
		ctx_se = frappe._dict()
		with self.assertRaises(frappe.Redirect):
			tech_page.get_context(ctx_se)
		self.assertEqual(frappe.local.flags.redirect_location, f"/tech?ro={ro.name}")

	def test_shop_action_item_isolation_and_standalone_self_service(self):
		"""Scoped technicians see only assigned RO/SE items and their own standalone Shop Action Items."""
		from ism.ism.doctype.shop_action_item.shop_action_item import (
			create_shop_action_item,
			resolve_action_item,
		)

		mgr_email, _ = self._create_user_with_roles("tech_sai_mgr", ["ISM Manager"], link_employee=False)
		tech_a, emp_a = self._create_user_with_roles("tech_sai_a", ["ISM Technician"], link_employee=True)
		tech_b, emp_b = self._create_user_with_roles("tech_sai_b", ["ISM Technician"], link_employee=True)

		frappe.set_user(mgr_email)
		mgr_sai = create_shop_action_item(title=f"{TEST_PREFIX}Manager Standalone SAI")
		self.track("Shop Action Item", mgr_sai)

		ro_b, cust_b, _ = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		self.create_test_schedule_entry(
			repair_order=ro_b.name,
			assigned_technician=emp_b,
			status="Confirmed",
			entry_type="Repair",
		)
		ro_b_sai = create_shop_action_item(
			title=f"{TEST_PREFIX}Tech B RO SAI",
			reference_doctype="Repair Order",
			reference_name=ro_b.name,
		)
		self.track("Shop Action Item", ro_b_sai)

		# Tech A cannot see/read manager's standalone SAI or Tech B's RO-linked SAI
		frappe.set_user(tech_a)
		visible_for_a = set(frappe.get_list("Shop Action Item", pluck="name"))
		self.assertNotIn(mgr_sai, visible_for_a)
		self.assertNotIn(ro_b_sai, visible_for_a)
		self.assertFalse(frappe.has_permission("Shop Action Item", "read", doc=mgr_sai))
		self.assertFalse(frappe.has_permission("Shop Action Item", "read", doc=ro_b_sai))

		# Tech A cannot spoof reported_by or attach non-RO/SE Customer reference
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({
				"doctype": "Shop Action Item",
				"title": f"{TEST_PREFIX}Spoofed SAI",
				"reported_by": mgr_email,
			}).insert()

		non_ro_sai = frappe.get_doc({
			"doctype": "Shop Action Item",
			"title": f"{TEST_PREFIX}Customer Ref SAI",
			"reference_doctype": "Customer",
			"reference_name": cust_b.name,
		})
		self.assertFalse(frappe.has_permission("Shop Action Item", "create", doc=non_ro_sai, user=tech_a))

		# Tech A can create, list, and resolve its own standalone Shop Action Item
		tech_a_sai = create_shop_action_item(title=f"{TEST_PREFIX}Tech A Bay Compressor")
		self.track("Shop Action Item", tech_a_sai)
		self.assertIn(tech_a_sai, set(frappe.get_list("Shop Action Item", pluck="name")))

		resolved = resolve_action_item(tech_a_sai, resolution_notes="Compressor breaker reset")
		self.assertEqual(resolved["status"], "success")
		self.assertEqual(frappe.db.get_value("Shop Action Item", tech_a_sai, "status"), "Resolved")

		# Tech B cannot see or read Tech A's standalone SAI (but can see ro_b_sai)
		frappe.set_user(tech_b)
		visible_for_b = set(frappe.get_list("Shop Action Item", pluck="name"))
		self.assertIn(ro_b_sai, visible_for_b)
		self.assertNotIn(tech_a_sai, visible_for_b)
		self.assertNotIn(mgr_sai, visible_for_b)
		self.assertFalse(frappe.has_permission("Shop Action Item", "read", doc=tech_a_sai))
