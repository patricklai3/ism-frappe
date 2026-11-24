# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Tests for Wave 2B: encrypted PIN-to-Drive vault, Seam 6 staff reveal, auto-purge, and Seam 5 events."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from ism.portal import events as portal_events
from ism.portal import pin_vault
from ism.portal.contracts import (
	PIN_AUTO_MASK_SECONDS,
	PIN_STATUS_NOT_PROMPTED,
	PIN_STATUS_OPTED_OUT,
	PIN_STATUS_PROVIDED,
	PIN_STATUS_PURGED,
	find_forbidden_portal_keys,
)
from ism.tests.base import ISMTestCase


class TestPortalPinVault(ISMTestCase):
	"""Verify Fernet + __Auth PIN encryption, RBAC reveal, auto-purge, and Seam 5 events."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.customer = self.create_test_customer()
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _create_ro(
		self,
		*,
		status: str = "Authorized - In Progress",
		approval_status: str = "Approved",
		services: list[dict] | None = None,
		**extra,
	):
		ro_dict = {
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
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

	def _create_test_user(self, prefix: str, role: str) -> str:
		email = f"{prefix}_{frappe.generate_hash(length=6)}@example.com"
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": prefix,
			"last_name": "PinTester",
			"enabled": 1,
			"user_type": "System User",
			"send_welcome_email": 0,
			"roles": [{"role": role}],
		})
		user.flags.ignore_password_policy = True
		user.insert(ignore_permissions=True)
		self.track(user)
		return email

	def _count_auth_rows(self, ro_name: str) -> int:
		row = frappe.db.sql(
			"""
			SELECT COUNT(*)
			FROM `__Auth`
			WHERE doctype = 'Repair Order'
			  AND name = %s
			  AND fieldname = 'pin_to_drive_encrypted'
			""",
			(ro_name,),
		)
		return int(row[0][0]) if row else 0

	def _track_action_items_for_ro(self, ro_name: str) -> list[dict]:
		items = frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro_name},
			fields=["name", "title", "category", "priority", "source", "dedup_key", "details", "status"],
		)
		for item in items:
			self.track("Shop Action Item", item["name"])
		return items

	def test_pin_encryption_leading_zeros_and_no_plaintext_storage(self):
		"""Leading zeros survive Fernet + __Auth round-trip while plaintext stays absent from RO, logs, and events."""
		ro = self._create_ro()

		for bad_pin in ("123", "12345", "abcd", "12a4", "", 1234, None):
			with self.assertRaises(frappe.ValidationError):
				pin_vault.submit_ro_pin(ro.name, bad_pin)

		with patch("frappe.publish_realtime") as mock_rt:
			cust_state = pin_vault.submit_ro_pin(ro.name, "0427")

		self.assertEqual(cust_state["status"], PIN_STATUS_PROVIDED)
		self.assertEqual(cust_state["masked_display"], "••••")
		self.assertFalse(cust_state["prompt_required"])
		self.assertFalse(cust_state["can_submit"])
		self.assertTrue(cust_state["can_update"])
		self.assertTrue(cust_state["submitted_at"])
		self.assertTrue(cust_state["updated_at"])
		self.assertEqual(find_forbidden_portal_keys(cust_state), [])
		self.assertNotIn("0427", str(cust_state))

		# Verify __Auth holds encrypted envelope (not raw PIN) and tabRepair Order holds no plaintext
		self.assertEqual(self._count_auth_rows(ro.name), 1)
		raw_sql_row = frappe.db.sql(
			"SELECT * FROM `tabRepair Order` WHERE name = %s",
			(ro.name,),
			as_dict=True,
		)[0]
		self.assertNotIn("0427", str(raw_sql_row))
		self.assertEqual(raw_sql_row.get("pin_to_drive_encrypted"), "********")

		ro.reload()
		self.assertNotIn("0427", str(ro.as_dict()))

		# Stored ciphertext in __Auth is Fernet-encrypted, not plaintext
		stored_cipher = get_decrypted_password(
			"Repair Order",
			ro.name,
			fieldname="pin_to_drive_encrypted",
			raise_exception=False,
		)
		self.assertTrue(stored_cipher)
		self.assertNotIn("0427", stored_cipher)

		# Regular save of an active RO preserves __Auth ciphertext
		ro.customer_concerns = "Customer reported squeaky front brakes"
		ro.save(ignore_permissions=True)
		self.assertEqual(self._count_auth_rows(ro.name), 1)

		# Staff reveal recovers exact leading-zero PIN
		reveal = pin_vault.get_ro_pin_for_staff(ro.name, channel="Desk")
		self.assertTrue(reveal["available"])
		self.assertEqual(reveal["pin"], "0427")
		self.assertEqual(reveal["auto_mask_seconds"], PIN_AUTO_MASK_SECONDS)

		# Realtime payloads and Shop Action Items never leak PIN digits
		for call in mock_rt.call_args_list:
			self.assertNotIn("0427", str(call.args))
			self.assertNotIn("0427", str(call.kwargs))

		items = self._track_action_items_for_ro(ro.name)
		self.assertEqual(len(items), 1)
		self.assertNotIn("0427", str(items[0]))

		# Test leading double-zero "0042"
		pin_vault.submit_ro_pin(ro.name, "0042")
		reveal2 = pin_vault.get_ro_pin_for_staff(ro.name, channel="Desk")
		self.assertEqual(reveal2["pin"], "0042")

	def test_cross_ro_ciphertext_transplant_rejected(self):
		"""Copying encrypted PIN ciphertext from RO-A to RO-B must fail RO-binding check."""
		ro_a = self._create_ro()
		ro_b = self._create_ro()

		pin_vault.submit_ro_pin(ro_a.name, "0427")
		self._track_action_items_for_ro(ro_a.name)

		cipher_a = get_decrypted_password(
			"Repair Order",
			ro_a.name,
			fieldname="pin_to_drive_encrypted",
			raise_exception=True,
		)

		# Transplant RO-A ciphertext onto RO-B
		set_encrypted_password(
			"Repair Order",
			ro_b.name,
			cipher_a,
			fieldname="pin_to_drive_encrypted",
		)
		frappe.db.set_value(
			"Repair Order",
			ro_b.name,
			"pin_to_drive_status",
			PIN_STATUS_PROVIDED,
			update_modified=False,
		)

		with self.assertRaises(frappe.ValidationError):
			pin_vault.get_ro_pin_for_staff(ro_b.name, channel="Desk")

	def test_pre_work_update_allowed_before_pickup_and_blocked_after_pickup(self):
		"""Authorized - In Progress with picked_up_at=None allows submit/update; pick_up_vehicle() locks updates."""
		ro = self._create_ro(
			status="Authorized - In Progress",
			approval_status="Approved",
			vehicle_location="In Customer Lot",
		)

		initial_state = pin_vault.get_ro_pin_customer_state(ro)
		self.assertEqual(initial_state["status"], PIN_STATUS_NOT_PROMPTED)
		self.assertIsNone(initial_state["masked_display"])
		# Quote approval alone no longer triggers prompt_required; PIN is requested at appointment confirmation.
		self.assertFalse(initial_state["prompt_required"])
		self.assertTrue(initial_state["can_submit"])
		self.assertTrue(initial_state["can_update"])

		# Customer submits as Guest on Authorized - In Progress RO prior to tech pickup
		frappe.set_user("Guest")
		try:
			after_submit = pin_vault.submit_ro_pin(ro.name, "0042")
			self.assertEqual(after_submit["status"], PIN_STATUS_PROVIDED)
			self.assertEqual(after_submit["masked_display"], "••••")
			self.assertTrue(after_submit["can_update"])
			first_submitted_at = after_submit["submitted_at"]

			# Pre-work update before pickup succeeds and preserves original submitted_at
			after_update = pin_vault.submit_ro_pin(ro.name, "0427")
			self.assertEqual(after_update["status"], PIN_STATUS_PROVIDED)
			self.assertEqual(after_update["submitted_at"], first_submitted_at)
			self.assertTrue(after_update["updated_at"])
		finally:
			frappe.set_user("Administrator")

		self._track_action_items_for_ro(ro.name)
		self.assertEqual(pin_vault.get_ro_pin_for_staff(ro.name)["pin"], "0427")

		# Technician picks up vehicle into bay -> locks further customer updates and opt-outs
		ro.reload()
		ro.pick_up_vehicle()
		ro.reload()
		self.assertTrue(ro.picked_up_at)

		locked_state = pin_vault.get_ro_pin_customer_state(ro)
		self.assertFalse(locked_state["can_update"])

		with self.assertRaises(frappe.ValidationError):
			pin_vault.submit_ro_pin(ro.name, "9999")
		with self.assertRaises(frappe.ValidationError):
			pin_vault.opt_out_ro_pin(ro.name)

		# Completed / Billed / Closed / Cancelled ROs block initial submission too
		for locked_status in ("Work Completed", "Billed / Paid", "Closed / Released", "Cancelled"):
			locked_ro = self._create_ro(status=locked_status)
			st = pin_vault.get_ro_pin_customer_state(locked_ro)
			self.assertFalse(st["can_submit"])
			self.assertFalse(st["can_update"])
			self.assertFalse(st["prompt_required"])
			with self.assertRaises(frappe.ValidationError):
				pin_vault.submit_ro_pin(locked_ro.name, "1234")

	def test_opt_out_erases_prior_ciphertext(self):
		"""Opting out after submitting a PIN immediately removes __Auth ciphertext."""
		ro = self._create_ro()
		pin_vault.submit_ro_pin(ro.name, "0427")
		self.assertEqual(self._count_auth_rows(ro.name), 1)

		opted = pin_vault.opt_out_ro_pin(ro.name)
		self._track_action_items_for_ro(ro.name)

		self.assertEqual(opted["status"], PIN_STATUS_OPTED_OUT)
		self.assertIsNone(opted["masked_display"])
		self.assertTrue(opted["opted_out_at"])
		self.assertEqual(self._count_auth_rows(ro.name), 0)

		staff_state = pin_vault.get_ro_pin_state(ro.name)
		self.assertEqual(staff_state["status"], PIN_STATUS_OPTED_OUT)
		self.assertFalse(staff_state["has_pin"])
		self.assertFalse(staff_state["can_reveal"])

		reveal = pin_vault.get_ro_pin_for_staff(ro.name)
		self.assertFalse(reveal["available"])
		self.assertIsNone(reveal["pin"])
		self.assertEqual(reveal["status"], PIN_STATUS_OPTED_OUT)

		# Pre-work customer can change mind and provide a PIN again
		reprovided = pin_vault.submit_ro_pin(ro.name, "8080")
		self.assertEqual(reprovided["status"], PIN_STATUS_PROVIDED)
		self.assertIsNone(reprovided["opted_out_at"])
		self.assertEqual(self._count_auth_rows(ro.name), 1)
		self.assertEqual(pin_vault.get_ro_pin_for_staff(ro.name)["pin"], "8080")

	def test_staff_rbac_audit_counters_comment_and_seam6_compatibility(self):
		"""Allowed staff roles reveal PIN with audit trail; Guest/Sales User rejected; get_ro_pin_state never reads __Auth."""
		ro = self._create_ro()
		pin_vault.submit_ro_pin(ro.name, "0427")
		self._track_action_items_for_ro(ro.name)

		tech_user = self._create_test_user("pin_tech", "ISM Technician")
		tech_emp = frappe.get_doc({
			"doctype": "Employee",
			"first_name": "Pin Tech",
			"employee_name": "Pin Tech",
			"status": "Active",
			"designation": "Technician",
			"company": self.get_manifest().company,
			"gender": "Male",
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2020-01-01",
			"user_id": tech_user,
		}).insert(ignore_permissions=True)
		self.track(tech_emp)
		self.create_test_schedule_entry(
			repair_order=ro.name,
			assigned_technician=tech_emp.name,
			status="Confirmed",
		)
		advisor_user = self._create_test_user("pin_advisor", "ISM Service Advisor")
		manager_user = self._create_test_user("pin_mgr", "ISM Manager")
		sysmgr_user = self._create_test_user("pin_sysmgr", "System Manager")
		sales_user = self._create_test_user("pin_sales", "Sales User")

		# get_ro_pin_state NEVER reads __Auth
		with patch("ism.portal.pin_vault.get_decrypted_password") as mock_decrypt:
			state = pin_vault.get_ro_pin_state(ro.name)
			mock_decrypt.assert_not_called()

		self.assertTrue(state["pin_feature_installed"])
		self.assertEqual(state["status"], PIN_STATUS_PROVIDED)
		self.assertTrue(state["has_pin"])
		self.assertTrue(state["can_reveal"])
		self.assertNotIn("pin", state)
		self.assertNotIn("0427", str(state))

		# Denied users: Guest and Sales User
		for denied_user in ("Guest", sales_user):
			frappe.set_user(denied_user)
			denied_state = pin_vault.get_ro_pin_state(ro.name)
			self.assertFalse(denied_state["can_reveal"], f"{denied_user} must not be able to reveal")
			with self.assertRaises(frappe.PermissionError):
				pin_vault.get_ro_pin_for_staff(ro.name, channel="Desk")
			with self.assertRaises(frappe.PermissionError):
				pin_vault.reveal_ro_pin_desk(ro.name)

		# Allowed roles increment audit counters and write non-sensitive Comments
		expected_count = 0
		for allowed_user in (tech_user, advisor_user, manager_user, sysmgr_user):
			frappe.set_user(allowed_user)
			expected_count += 1
			st = pin_vault.get_ro_pin_state(ro.name)
			self.assertTrue(st["can_reveal"], f"{allowed_user} should have can_reveal=True")

			res = pin_vault.reveal_ro_pin_desk(ro.name)
			self.assertTrue(res["available"])
			self.assertTrue(res["pin_feature_installed"])
			self.assertEqual(res["pin"], "0427")
			self.assertEqual(res["auto_mask_seconds"], PIN_AUTO_MASK_SECONDS)
			self.assertEqual(res["viewed_by"], allowed_user)
			self.assertEqual(res["view_count"], expected_count)

		frappe.set_user("Administrator")
		ro.reload()
		self.assertEqual(ro.pin_to_drive_view_count, expected_count)
		self.assertEqual(ro.pin_to_drive_last_viewed_by, sysmgr_user)
		self.assertTrue(ro.pin_to_drive_last_viewed_at)

		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name, "comment_type": "Comment"},
			fields=["name", "content"],
		)
		self.assertGreaterEqual(len(comments), expected_count)
		for c in comments:
			self.assertNotIn("0427", c["content"] or "")

		# Confirm Seam 6 soft-import in ism.api.board.reveal_ro_pin works cleanly
		from ism.api.board import reveal_ro_pin as board_reveal_ro_pin

		frappe.set_user(tech_user)
		board_res = board_reveal_ro_pin(ro.name)
		self.assertTrue(board_res["ok"])
		self.assertTrue(board_res["available"])
		self.assertTrue(board_res["pin_feature_installed"])
		self.assertEqual(board_res["pin"], "0427")
		self.assertEqual(board_res["auto_mask_seconds"], PIN_AUTO_MASK_SECONDS)
		self.assertEqual(board_res["view_count"], expected_count + 1)

	def test_auto_purge_on_closed_released_direct_save_and_cancelled(self):
		"""Transitioning RO to Closed / Released or Cancelled purges __Auth to 0 rows and blocks staff reveal."""
		# 1. Direct save -> Closed / Released
		ro_close = self._create_ro()
		pin_vault.submit_ro_pin(ro_close.name, "0427")
		self._track_action_items_for_ro(ro_close.name)
		self.assertEqual(self._count_auth_rows(ro_close.name), 1)

		ro_close.reload()
		with patch("frappe.publish_realtime") as mock_rt:
			ro_close.status = "Closed / Released"
			ro_close.save(ignore_permissions=True)

		self.assertEqual(ro_close.pin_to_drive_status, PIN_STATUS_PURGED)
		ro_close.reload()
		self.assertEqual(ro_close.pin_to_drive_status, PIN_STATUS_PURGED)
		self.assertTrue(ro_close.pin_to_drive_purged_at)
		self.assertEqual(self._count_auth_rows(ro_close.name), 0)

		purge_events = [
			c for c in mock_rt.call_args_list
			if c.args and c.args[0] == "ism_portal_event" and c.args[1].get("event_type") == "pin_purged"
		]
		self.assertEqual(len(purge_events), 1)
		self.assertTrue(purge_events[0].kwargs.get("after_commit"))
		self.assertNotIn("0427", str(purge_events[0].args))

		state_after = pin_vault.get_ro_pin_state(ro_close.name)
		self.assertEqual(state_after["status"], PIN_STATUS_PURGED)
		self.assertFalse(state_after["has_pin"])
		self.assertFalse(state_after["can_reveal"])

		with self.assertRaises(frappe.PermissionError):
			pin_vault.get_ro_pin_for_staff(ro_close.name)

		# 2. Cancel RO -> Cancelled auto-purges
		ro_cancel = self._create_ro()
		pin_vault.submit_ro_pin(ro_cancel.name, "0099")
		self._track_action_items_for_ro(ro_cancel.name)
		self.assertEqual(self._count_auth_rows(ro_cancel.name), 1)

		ro_cancel.reload()
		ro_cancel.cancel_repair_order()
		ro_cancel.reload()
		self.assertEqual(ro_cancel.status, "Cancelled")
		self.assertEqual(ro_cancel.pin_to_drive_status, PIN_STATUS_PURGED)
		self.assertTrue(ro_cancel.pin_to_drive_purged_at)
		self.assertEqual(self._count_auth_rows(ro_cancel.name), 0)
		with self.assertRaises(frappe.PermissionError):
			pin_vault.get_ro_pin_for_staff(ro_cancel.name)

		# 3. Closing an RO whose PIN was Not Prompted preserves Not Prompted (does not falsely set Purged)
		ro_noprompt = self._create_ro()
		ro_noprompt.status = "Closed / Released"
		ro_noprompt.save(ignore_permissions=True)
		ro_noprompt.reload()
		self.assertEqual(ro_noprompt.pin_to_drive_status, PIN_STATUS_NOT_PROMPTED)
		self.assertEqual(self._count_auth_rows(ro_noprompt.name), 0)

	def test_auto_purge_via_process_payment_and_release(self):
		"""End-to-end process_payment_and_release() auto-purges __Auth PIN ciphertext to 0 rows."""
		ro = self._create_ro(
			status="Authorized - In Progress",
			approval_status="Approved",
		)
		pin_vault.submit_ro_pin(ro.name, "0427")
		self._track_action_items_for_ro(ro.name)
		self.assertEqual(self._count_auth_rows(ro.name), 1)

		# Move RO to Work Completed and run canonical checkout & release
		ro.reload()
		ro.status = "Work Completed"
		ro.odometer_out = 40100
		ro.save(ignore_permissions=True)
		self.assertEqual(self._count_auth_rows(ro.name), 1)

		release_res = ro.process_payment_and_release(
			payment_method="Debit / Interac",
			odometer_out=40100,
		)
		self.assertEqual(release_res["status"], "success")
		if release_res.get("sales_invoice"):
			self.track("Sales Invoice", release_res["sales_invoice"])
		for pe_name in release_res.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		ro.reload()
		self.assertEqual(ro.status, "Closed / Released")
		self.assertEqual(ro.pin_to_drive_status, PIN_STATUS_PURGED)
		self.assertTrue(ro.pin_to_drive_purged_at)
		self.assertEqual(self._count_auth_rows(ro.name), 0)

		with self.assertRaises(frappe.PermissionError):
			pin_vault.get_ro_pin_for_staff(ro.name)

		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name, "comment_type": "Comment"},
			pluck="content",
		)
		self.assertTrue(any("purged" in (c or "").lower() for c in comments))
		for c in comments:
			self.assertNotIn("0427", c or "")

	def test_purge_stale_closed_ro_pins_and_defensive_staff_purge(self):
		"""Daily scheduler sweeps stale closed ROs and residual __Auth rows; staff reveal also purges defensively."""
		# 1. Stale Closed / Released RO with planted Provided status + __Auth ciphertext
		ro_stale = self._create_ro(status="Closed / Released")
		set_encrypted_password(
			"Repair Order",
			ro_stale.name,
			"stale-ciphertext-placeholder",
			fieldname="pin_to_drive_encrypted",
		)
		frappe.db.set_value(
			"Repair Order",
			ro_stale.name,
			"pin_to_drive_status",
			PIN_STATUS_PROVIDED,
			update_modified=False,
		)
		self.assertEqual(self._count_auth_rows(ro_stale.name), 1)

		swept = pin_vault.purge_stale_closed_ro_pins()
		self.assertGreaterEqual(swept, 1)
		self.assertEqual(self._count_auth_rows(ro_stale.name), 0)
		self.assertEqual(
			frappe.db.get_value("Repair Order", ro_stale.name, "pin_to_drive_status"),
			PIN_STATUS_PURGED,
		)

		# 2. Residual __Auth row on Cancelled RO gets defensively removed on get_ro_pin_for_staff
		ro_cancelled = self._create_ro(status="Cancelled")
		set_encrypted_password(
			"Repair Order",
			ro_cancelled.name,
			"residual-ciphertext",
			fieldname="pin_to_drive_encrypted",
		)
		self.assertEqual(self._count_auth_rows(ro_cancelled.name), 1)
		with self.assertRaises(frappe.PermissionError):
			pin_vault.get_ro_pin_for_staff(ro_cancelled.name)
		self.assertEqual(self._count_auth_rows(ro_cancelled.name), 0)

	def test_seam5_events_dedup_no_pin_leak_and_zero_board_import(self):
		"""events.py emits deduplicated Shop Action Items + realtime with zero PIN leakage and zero ism.board imports."""
		# Static AST check: zero ism.board imports in pin_vault.py and events.py
		portal_dir = Path(pin_vault.__file__).resolve().parent
		for fname in ("pin_vault.py", "events.py"):
			source = (portal_dir / fname).read_text(encoding="utf-8")
			tree = ast.parse(source, filename=fname)
			for node in ast.walk(tree):
				if isinstance(node, ast.Import):
					for alias in node.names:
						self.assertFalse(
							alias.name == "ism.board" or alias.name.startswith("ism.board."),
							f"{fname} must never import ism.board",
						)
				elif isinstance(node, ast.ImportFrom):
					mod = node.module or ""
					self.assertFalse(
						mod == "ism.board" or mod.startswith("ism.board."),
						f"{fname} must never import from ism.board",
					)

		ro = self._create_ro()

		# PIN submit + update deduplicates on portal:pin:<ro>
		with patch("frappe.publish_realtime") as mock_rt:
			pin_vault.submit_ro_pin(ro.name, "0427")
			pin_vault.opt_out_ro_pin(ro.name)

		pin_items = self._track_action_items_for_ro(ro.name)
		self.assertEqual(len(pin_items), 1)
		self.assertEqual(pin_items[0]["category"], "General")
		self.assertEqual(pin_items[0]["source"], "Agent")
		self.assertEqual(pin_items[0]["priority"], "Low")
		self.assertEqual(pin_items[0]["dedup_key"], f"portal:pin:{ro.name}")
		self.assertIn("Opted Out", pin_items[0]["title"])
		self.assertNotIn("0427", str(pin_items[0]))

		pin_rt_calls = [
			c for c in mock_rt.call_args_list
			if c.args and c.args[0] == "ism_portal_event" and c.args[1].get("event_type") == "pin_updated"
		]
		self.assertEqual(len(pin_rt_calls), 2)
		self.assertTrue(all(c.kwargs.get("after_commit") for c in pin_rt_calls))

		# Quote approval event emits both legacy ism_ro_approval_updated & ism_portal_event and does not collide with PIN item
		ro.reload()
		with patch("frappe.publish_realtime") as mock_rt:
			portal_events.emit_portal_quote_approved_event(ro, {"svc1": "Approved"}, "Alex Driver")
			portal_events.emit_portal_quote_approved_event(ro, {"svc1": "Declined"}, "Alex Driver")

		all_items = self._track_action_items_for_ro(ro.name)
		self.assertEqual(len(all_items), 2)
		approval_items = [i for i in all_items if i["dedup_key"] == f"portal:approval:{ro.name}"]
		self.assertEqual(len(approval_items), 1)
		self.assertEqual(approval_items[0]["category"], "General")
		self.assertEqual(approval_items[0]["source"], "Agent")
		self.assertEqual(approval_items[0]["priority"], "High")

		legacy_calls = [c for c in mock_rt.call_args_list if c.args and c.args[0] == "ism_ro_approval_updated"]
		portal_calls = [
			c for c in mock_rt.call_args_list
			if c.args and c.args[0] == "ism_portal_event" and c.args[1].get("event_type") == "quote_approved"
		]
		self.assertEqual(len(legacy_calls), 2)
		self.assertEqual(len(portal_calls), 2)
		self.assertTrue(all(c.kwargs.get("after_commit") for c in legacy_calls + portal_calls))

		# Reschedule event emits realtime ONLY (does not create duplicate Shop Action Item)
		with patch("frappe.publish_realtime") as mock_rt:
			portal_events.emit_portal_reschedule_event(
				"SE-TEST-001",
				ro.name,
				{"date": "2026-12-01", "customer_arrival_time": "07:45"},
				{"date": "2026-12-02", "customer_arrival_time": "08:45"},
			)

		resched_calls = [
			c for c in mock_rt.call_args_list
			if c.args and c.args[0] == "ism_portal_event" and c.args[1].get("event_type") == "appointment_rescheduled"
		]
		self.assertEqual(len(resched_calls), 1)
		self.assertTrue(resched_calls[0].kwargs.get("after_commit"))
		self.assertEqual(len(self._track_action_items_for_ro(ro.name)), 2)

	def _count_se_auth_rows(self, se_name: str) -> int:
		row = frappe.db.sql(
			"""
			SELECT COUNT(*)
			FROM `__Auth`
			WHERE doctype = 'Schedule Entry'
			  AND name = %s
			  AND fieldname = 'pin_to_drive_encrypted'
			""",
			(se_name,),
		)
		return int(row[0][0]) if row else 0

	def _create_schedule_entry(self, *, status: str = "Scheduled", customer: str | None = None, vehicle: str | None = None):
		from frappe.utils import add_days, nowdate

		if not customer:
			cust = self.create_test_customer(mobile_no="+14165550911")
			customer = cust.name
		if not vehicle:
			veh = self.create_test_vehicle(customer=customer, license_plate=f"SE{frappe.generate_hash(length=4)}")
			vehicle = veh.name
		bay = frappe.get_doc(
			{
				"doctype": "Service Bay",
				"bay_name": f"_TEST_ISM_PinBay_{frappe.generate_hash(length=6)}",
				"bay_type": "General Service",
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		self.track(bay)
		se = frappe.get_doc(
			{
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"status": status,
				"scheduled_date": add_days(nowdate(), 3),
				"scheduled_time": "09:00:00",
				"estimated_duration": 60,
				"service_bay": bay.name,
				"customer": customer,
				"repair_vehicle": vehicle,
			}
		).insert(ignore_permissions=True)
		self.track(se)
		return se

	def test_schedule_entry_pin_prompt_carryover_staff_reveal_and_purge(self):
		"""PIN is requested on confirmed/scheduled appointments, carries over to RO on check-in, and purges cleanly."""
		from ism.api.scheduling import create_repair_order_from_schedule_entry

		se = self._create_schedule_entry(status="Scheduled")

		# 1. Appointment prompts for PIN before RO exists
		cust_initial = pin_vault.get_schedule_entry_pin_customer_state(se)
		self.assertEqual(cust_initial["status"], PIN_STATUS_NOT_PROMPTED)
		self.assertTrue(cust_initial["prompt_required"])
		self.assertTrue(cust_initial["can_submit"])
		self.assertTrue(cust_initial["can_update"])

		# 2. Submit leading-zero PIN on Schedule Entry -> stored encrypted, never plaintext
		with patch("frappe.publish_realtime") as mock_rt:
			after_submit = pin_vault.submit_schedule_entry_pin(se.name, "0073")
		self.assertEqual(after_submit["status"], PIN_STATUS_PROVIDED)
		self.assertEqual(after_submit["masked_display"], "••••")
		self.assertFalse(after_submit["prompt_required"])
		self.assertNotIn("0073", str(after_submit))
		self.assertEqual(self._count_se_auth_rows(se.name), 1)

		se_items = frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Schedule Entry", "reference_name": se.name},
			fields=["name", "title", "details"],
		)
		for item in se_items:
			self.track("Shop Action Item", item["name"])
		self.assertEqual(len(se_items), 1)
		self.assertNotIn("0073", str(se_items[0]))
		for call in mock_rt.call_args_list:
			self.assertNotIn("0073", str(call.args))

		# 3. Cross-SE ciphertext transplant is rejected
		se_other = self._create_schedule_entry(status="Scheduled")
		se_cipher = get_decrypted_password(
			"Schedule Entry", se.name, fieldname="pin_to_drive_encrypted", raise_exception=True
		)
		set_encrypted_password("Schedule Entry", se_other.name, se_cipher, fieldname="pin_to_drive_encrypted")
		frappe.db.set_value("Schedule Entry", se_other.name, "pin_to_drive_status", PIN_STATUS_PROVIDED, update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			pin_vault.get_schedule_entry_pin_for_staff(se_other.name, channel="Desk")
		pin_vault.purge_schedule_entry_pin(se_other.name, reason="Cancelled")

		# 4. Staff reveal on Schedule Entry succeeds before RO exists
		se_reveal = pin_vault.get_schedule_entry_pin_for_staff(se.name, channel="Desk")
		self.assertTrue(se_reveal["available"])
		self.assertEqual(se_reveal["pin"], "0073")
		self.assertEqual(se_reveal["view_count"], 1)

		# 5. Creating RO from Schedule Entry transfers PIN to RO-bound envelope and removes SE __Auth copy
		created = create_repair_order_from_schedule_entry(schedule_entry=se.name, odometer_in=15000)
		ro_name = created["repair_order"]
		self.track("Repair Order", ro_name)
		self._track_action_items_for_ro(ro_name)

		self.assertEqual(self._count_se_auth_rows(se.name), 0)
		self.assertEqual(self._count_auth_rows(ro_name), 1)

		ro_reveal = pin_vault.get_ro_pin_for_staff(ro_name, channel="Board")
		self.assertTrue(ro_reveal["available"])
		self.assertEqual(ro_reveal["pin"], "0073")
		self.assertEqual(ro_reveal["view_count"], 2)

		# 6. Closing RO purges both RO and linked Schedule Entry PIN state
		ro_doc = frappe.get_doc("Repair Order", ro_name)
		ro_doc.approval_status = "Approved"
		ro_doc.status = "Closed / Released"
		ro_doc.save(ignore_permissions=True)
		self.assertEqual(self._count_auth_rows(ro_name), 0)
		self.assertEqual(self._count_se_auth_rows(se.name), 0)
		se.reload()
		self.assertEqual(se.pin_to_drive_status, PIN_STATUS_PURGED)

		# 7. Unlinked Schedule Entry cancellation auto-purges SE PIN
		se_cancel = self._create_schedule_entry(status="Confirmed")
		pin_vault.submit_schedule_entry_pin(se_cancel.name, "4321")
		for item in frappe.get_all("Shop Action Item", filters={"reference_doctype": "Schedule Entry", "reference_name": se_cancel.name}, pluck="name"):
			self.track("Shop Action Item", item)
		self.assertEqual(self._count_se_auth_rows(se_cancel.name), 1)
		se_cancel.reload()
		se_cancel.status = "Cancelled"
		se_cancel.save(ignore_permissions=True)
		self.assertEqual(self._count_se_auth_rows(se_cancel.name), 0)
		se_cancel.reload()
		self.assertEqual(se_cancel.pin_to_drive_status, PIN_STATUS_PURGED)

		# 8. Opt-out on Schedule Entry carries over to Repair Order on manual link
		se_opt = self._create_schedule_entry(status="Scheduled")
		pin_vault.opt_out_schedule_entry_pin(se_opt.name)
		for item in frappe.get_all("Shop Action Item", filters={"reference_doctype": "Schedule Entry", "reference_name": se_opt.name}, pluck="name"):
			self.track("Shop Action Item", item)
		ro_opt = self._create_ro()
		se_opt.reload()
		se_opt.repair_order = ro_opt.name
		se_opt.save(ignore_permissions=True)
		ro_opt.reload()
		self.assertEqual(ro_opt.pin_to_drive_status, PIN_STATUS_OPTED_OUT)
