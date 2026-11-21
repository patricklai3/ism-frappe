# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import io
import json
import sys
import types
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, now_datetime, today
from werkzeug.datastructures import FileStorage, ImmutableMultiDict

from ism.api import board as board_api
from ism.api.agent_serializers import build_ro_urls
from ism.board.contracts import MANAGER_GATED_ACTIONS, ro_thread_key
from ism.board.gateways import (
	ACOUSTIC_CLARITY_CONFIDENCE_CAP,
	assert_can_commit_approval_gate,
	dispatch_orchestrator_decision,
	resolve_actor_role,
	resolve_card_action,
	undo_receipt_action,
)
from ism.board.projections import ensure_singleton_shop_threads, ensure_thread_for_ro
from ism.tests.base import ISMTestCase


def _make_png_bytes() -> bytes:
	# Minimal valid 1x1 PNG binary header + IHDR/IDAT/IEND chunks
	return (
		b"\x89PNG\r\n\x1a\n"
		b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
		b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
		b"\x00\x00\x00\x00IEND\xaeB`\x82"
	)


class TestBoardGateways(ISMTestCase):
	"""Comprehensive tests for Wave 2A gateways, REST endpoints, RBAC, idempotency, and Seams 2/3/5/6."""

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _create_user_with_roles(self, email: str, roles: list[str]) -> str:
		if not frappe.db.exists("User", email):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0].title(),
					"enabled": 1,
					"send_welcome_email": 0,
					"roles": [{"role": r} for r in roles],
				}
			).insert(ignore_permissions=True)
			self.track(user)
		else:
			user = frappe.get_doc("User", email)
			user.set("roles", [{"role": r} for r in roles])
			user.save(ignore_permissions=True)
		return email

	def _create_ro(self, *, approved: bool = False, with_service: bool = True, **ro_kwargs):
		customer = self.create_test_customer()
		customer.db_set("mobile_no", "+14165550199", update_modified=False)
		customer.db_set("email_id", "customer@example.com", update_modified=False)
		vehicle = self.create_test_vehicle(
			customer=customer.name,
			model_year=2023,
			model="Model 3",
			manufacturer="Tesla",
		)
		if with_service and "services" not in ro_kwargs:
			ro_kwargs["services"] = [
				{
					"service_title": "Front Brake Inspection",
					"status": "Approved" if approved else "Estimating",
					"labor_hours": 1.0,
					"labor_rate": 150.0,
					"labor_amount": 150.0,
					"parts_total": 0.0,
					"total_amount": 150.0,
				}
			]
		if approved:
			ro_kwargs.setdefault("status", "Authorized - In Progress")
			ro_kwargs.setdefault("approval_status", "Approved")
		ro = self.create_test_repair_order(customer.name, vehicle.name, **ro_kwargs)
		return ro, customer, vehicle

	def _insert_capture_event(self, thread_key: str, text: str = "Tech capture") -> object:
		ev = frappe.get_doc(
			{
				"doctype": "Shop Board Event",
				"thread": thread_key,
				"event_timestamp": now_datetime(),
				"actor_type": "TECH",
				"actor_user": frappe.session.user,
				"payload_type": "TEXT",
				"body_text": text,
				"resolution_state": "NONE",
			}
		).insert(ignore_permissions=True)
		return self.track(ev)

	def _create_supplier(self, name_suffix: str) -> str:
		sup_name = f"TEST_SUP_{name_suffix}_{frappe.generate_hash(length=5)}"
		if not frappe.db.exists("Supplier", sup_name):
			sup = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": sup_name,
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
				}
			).insert(ignore_permissions=True)
			self.track(sup)
			return sup.name
		return sup_name

	def test_rbac_actor_roles_and_technician_rejections_on_every_gate_and_cockpit(self):
		tech_email = self._create_user_with_roles("board_tech_test@example.com", ["ISM Technician"])
		advisor_email = self._create_user_with_roles("board_advisor_test@example.com", ["ISM Service Advisor"])
		mgr_email = self._create_user_with_roles("board_mgr_test@example.com", ["ISM Manager"])
		nobody_email = self._create_user_with_roles("board_nobody_test@example.com", ["Guest"])

		with self.assertRaises(frappe.PermissionError):
			resolve_actor_role("Guest")
		with self.assertRaises(frappe.PermissionError):
			resolve_actor_role(nobody_email)

		self.assertEqual(resolve_actor_role(tech_email), "TECH")
		self.assertEqual(resolve_actor_role(advisor_email), "MANAGER")
		self.assertEqual(resolve_actor_role(mgr_email), "MANAGER")
		self.assertEqual(resolve_actor_role("Administrator"), "MANAGER")

		with self.assertRaises(frappe.PermissionError):
			assert_can_commit_approval_gate(tech_email)
		self.assertEqual(assert_can_commit_approval_gate(advisor_email), "MANAGER")
		self.assertEqual(assert_can_commit_approval_gate(mgr_email), "MANAGER")

		# Create one APPROVAL_GATE per every single MANAGER_GATED_ACTION and verify tech is rejected
		ro, _, _ = self._create_ro(approved=True)
		tkey = ensure_thread_for_ro(ro.name)
		source = self._insert_capture_event(tkey, "Trigger gates")

		gate_events: list[str] = []
		for action_name in MANAGER_GATED_ACTIONS:
			out = dispatch_orchestrator_decision(
				source,
				{
					"intent": action_name.lower(),
					"confidence": 0.96,
					"summary": f"Gate for {action_name}",
					"action": action_name,
					"action_payload": {"repair_order": ro.name, "amount": 100.0},
				},
			)
			self.assertEqual(out["outcome"], "APPROVAL_GATE")
			self.assertEqual(len(out["gates"]), 1)
			gate_events.append(out["gates"][0])
			self.track("Shop Board Event", out["gates"][0])

		# Technician must get frappe.PermissionError on EVERY approval gate and every Manager-only API
		frappe.set_user(tech_email)
		for gate_ev in gate_events:
			with self.assertRaises(frappe.PermissionError):
				board_api.resolve_board_card(gate_ev, "approve")
			with self.assertRaises(frappe.PermissionError):
				board_api.resolve_board_card(gate_ev, "reject")

		with self.assertRaises(frappe.PermissionError):
			board_api.get_cockpit_dashboard()
		with self.assertRaises(frappe.PermissionError):
			board_api.sync_cockpit_action("board:test", "dismiss")
		with self.assertRaises(frappe.PermissionError):
			board_api.price_part(tesla_oem_price=100.0, cost=40.0)
		with self.assertRaises(frappe.PermissionError):
			board_api.test_media_storage()

		# Service Advisor can access cockpit and price_part, but NOT test_media_storage (Manager/SysMgr only)
		frappe.set_user(advisor_email)
		priced = board_api.price_part(tesla_oem_price=100.0, cost=40.0)
		self.assertTrue(priced["ok"])
		self.assertEqual(priced["sell_price"], 70.0)
		with self.assertRaises(frappe.PermissionError):
			board_api.test_media_storage()

		frappe.set_user("Administrator")

	def test_acoustic_clarity_floor_confidence_gating_and_clarification_reenqueue(self):
		ro, _, _ = self._create_ro(approved=True)
		tkey = ensure_thread_for_ro(ro.name)
		source = self._insert_capture_event(tkey, "muffled audio control arm")

		# 1. Noisy audio (< 0.75) caps effective confidence <= 0.70 even when raw confidence is 0.96
		out = dispatch_orchestrator_decision(
			source,
			{
				"intent": "note",
				"confidence": 0.96,
				"audio_clarity_score": 0.60,
				"transcript": "control arm bushing torn",
				"action": "LOG_RO_NOTE",
				"action_payload": {"repair_order": ro.name, "note": "Should not auto-write"},
			},
		)
		self.assertEqual(out["outcome"], "CLARIFICATION_CARD")
		self.assertTrue(out["acoustic_clarity_capped"])
		self.assertLessEqual(out["effective_confidence"], ACOUSTIC_CLARITY_CONFIDENCE_CAP)
		ro.reload()
		self.assertFalse(ro.override_notes)  # Zero ERP mutation!
		clar_ev_name = out["cards"][0]
		self.track("Shop Board Event", clar_ev_name)

		# 2. Missing mandatory field (side) with high confidence emits CLARIFICATION_CARD
		out_missing = dispatch_orchestrator_decision(
			source,
			{
				"intent": "supplement",
				"confidence": 0.95,
				"action": "ADD_SUPPLEMENT_DRAFT",
				"action_payload": {"repair_order": ro.name, "service_title": "Control Arm Replacement"},
				"missing_fields": ["side"],
				"clarification": {
					"prompt": "Which side control arm?",
					"options": ["Front Left", "Front Right", "Both"],
					"target_role": "TECH",
				},
			},
		)
		self.assertEqual(out_missing["outcome"], "CLARIFICATION_CARD")
		clar_side = out_missing["cards"][0]
		self.track("Shop Board Event", clar_side)

		# 3. UPDATE_JOB_STATUS(complete) without odometer_out degrades cleanly to CLARIFICATION_CARD
		out_odo = dispatch_orchestrator_decision(
			source,
			{
				"intent": "complete_job",
				"confidence": 0.95,
				"action": "UPDATE_JOB_STATUS",
				"action_payload": {"repair_order": ro.name, "action": "complete"},
			},
		)
		self.assertEqual(out_odo["outcome"], "CLARIFICATION_CARD")
		odo_card = frappe.get_doc("Shop Board Event", out_odo["cards"][0])
		self.track(odo_card)
		odo_schema = json.loads(odo_card.card_schema)
		self.assertEqual(odo_schema.get("input_field"), "odometer_out")

		# 4. Resolve clarification card -> marks RESOLVED, appends CARD_RESOLUTION, re-enqueues orchestrator
		fake_orch = types.ModuleType("ism.board.orchestrator")
		mock_enqueue = MagicMock(return_value={"queued": True})
		fake_orch.enqueue_board_event = mock_enqueue
		with patch.dict(sys.modules, {"ism.board.orchestrator": fake_orch}):
			res1 = resolve_card_action(
				clar_side,
				"clarify",
				selected_option="Front Left",
				form_patch={"side": "Front Left"},
				idempotency_key="clar-side-1",
			)
			self.assertFalse(res1["already_resolved"])
			self.assertEqual(res1["resolution_state"], "RESOLVED")
			self.assertTrue(res1["re_enqueued"]["enqueued"])
			mock_enqueue.assert_called_once()
			args, kwargs = mock_enqueue.call_args
			self.assertEqual(args[0], source.name)
			self.assertEqual(kwargs["clarification_patch"]["side"], "Front Left")

			# Double-tap returns already_resolved=True with zero duplicate re-enqueue
			res2 = resolve_card_action(clar_side, "clarify", selected_option="Front Right")
			self.assertTrue(res2["already_resolved"])
			self.assertEqual(mock_enqueue.call_count, 1)

	def test_autonomous_actions_seam5_self_echo_and_compensating_undos(self):
		ro, _, vehicle = self._create_ro(approved=True)
		tkey = ensure_thread_for_ro(ro.name)
		source = self._insert_capture_event(tkey, "Autonomous actions test")
		part_item = self.create_test_item(
			f"TEST-CA-{frappe.generate_hash(length=5)}", is_stock_item=1, standard_rate=80.0
		)
		supplier_name = self._create_supplier("AUTO")

		# 1. LOG_RO_NOTE + Undo
		r_note = dispatch_orchestrator_decision(
			source,
			{
				"confidence": 0.95,
				"action": "LOG_RO_NOTE",
				"action_payload": {"repair_order": ro.name, "note": "Subframe bolts slightly corroded"},
			},
		)
		self.assertEqual(r_note["outcome"], "EXECUTED")
		ro.reload()
		self.assertIn("Subframe bolts slightly corroded", ro.override_notes or "")
		u_note = undo_receipt_action(r_note["receipts"][0], reason="Wrong RO")
		self.assertFalse(u_note["already_undone"])
		ro.reload()
		self.assertEqual(ro.override_notes or "", "")
		# Double-tap undo is idempotent
		u_note_2 = undo_receipt_action(r_note["receipts"][0])
		self.assertTrue(u_note_2["already_undone"])

		# 2. ATTACH_INSPECTION_MEDIA + Undo
		r_insp = dispatch_orchestrator_decision(
			source,
			{
				"confidence": 0.94,
				"action": "ATTACH_INSPECTION_MEDIA",
				"action_payload": {
					"repair_order": ro.name,
					"item_name": "Front Control Arm Bushings",
					"status": "Attention",
					"notes": "Torn bushing observed",
				},
			},
		)
		self.assertEqual(r_insp["outcome"], "EXECUTED")
		ro.reload()
		self.assertEqual(len(ro.get("inspection_items") or []), 1)
		undo_receipt_action(r_insp["receipts"][0])
		ro.reload()
		self.assertEqual(len(ro.get("inspection_items") or []), 0)

		# 3. ADD_SUPPLEMENT_DRAFT (with internal pricing & symmetrical opposite-side duplication) + Undo
		r_supp = dispatch_orchestrator_decision(
			source,
			{
				"confidence": 0.96,
				"action": "ADD_SUPPLEMENT_DRAFT",
				"action_payload": {
					"repair_order": ro.name,
					"service_title": "Front Upper Control Arm Replacement (LH)",
					"labor_hours": 1.5,
					"side": "both",
					"parts": [
						{
							"item_code": part_item.name,
							"qty": 1,
							"tesla_oem_price": 200.0,
							"cost": 80.0,
						}
					],
				},
			},
		)
		self.assertEqual(r_supp["outcome"], "EXECUTED")
		ro.reload()
		# Original 1 approved service + 2 supplement rows (LH + RH)
		self.assertEqual(len(ro.services), 3)
		supp_parts = json.loads(ro.services[1].parts)
		# Tesla OEM $200 * (1 - 30%) = $140 CAD internal pricing rule applied
		self.assertEqual(float(supp_parts[0]["rate"]), 140.0)
		undo_receipt_action(r_supp["receipts"][0])
		ro.reload()
		self.assertEqual(len(ro.services), 1)

		# 4. SET_PARTS_HOLD (Seam 5: dedup_key="board:parts_hold:<ro>" suppresses self-echo) + CLEAR_PARTS_HOLD + Undo
		before_state_mut_count = frappe.db.count(
			"Shop Board Event", {"thread": tkey, "payload_type": "STATE_MUTATION"}
		)
		r_hold = dispatch_orchestrator_decision(
			source,
			{
				"confidence": 0.95,
				"action": "SET_PARTS_HOLD",
				"action_payload": {"repair_order": ro.name, "reason": "Waiting on control arm delivery"},
			},
		)
		self.assertEqual(r_hold["outcome"], "EXECUTED")
		ro.reload()
		self.assertEqual(int(ro.manual_parts_hold), 1)
		sai_name = r_hold["results"][0]["result"]["action_item"]
		self.track("Shop Action Item", sai_name)
		self.assertEqual(
			frappe.db.get_value("Shop Action Item", sai_name, "dedup_key"),
			f"board:parts_hold:{ro.name}",
		)
		# Seam 5: verify NO self-echo STATE_MUTATION card was created on the thread!
		after_state_mut_count = frappe.db.count(
			"Shop Board Event", {"thread": tkey, "payload_type": "STATE_MUTATION"}
		)
		self.assertEqual(after_state_mut_count, before_state_mut_count)

		r_clear = dispatch_orchestrator_decision(
			source,
			{
				"confidence": 0.95,
				"action": "CLEAR_PARTS_HOLD",
				"action_payload": {"repair_order": ro.name},
			},
		)
		ro.reload()
		self.assertEqual(int(ro.manual_parts_hold), 0)
		self.assertEqual(frappe.db.get_value("Shop Action Item", sai_name, "status"), "Resolved")
		undo_receipt_action(r_clear["receipts"][0])
		ro.reload()
		self.assertEqual(int(ro.manual_parts_hold), 1)
		self.assertEqual(frappe.db.get_value("Shop Action Item", sai_name, "status"), "Open")
		undo_receipt_action(r_hold["receipts"][0])
		ro.reload()
		self.assertEqual(int(ro.manual_parts_hold), 0)

		# 5. TOGGLE_SERVICE_COMPLETION + Undo
		svc_row_name = ro.services[0].name
		r_toggle = dispatch_orchestrator_decision(
			source,
			{
				"confidence": 0.96,
				"action": "TOGGLE_SERVICE_COMPLETION",
				"action_payload": {
					"repair_order": ro.name,
					"service_row_id": svc_row_name,
					"is_completed": True,
				},
			},
		)
		ro.reload()
		self.assertEqual(int(ro.services[0].is_completed), 1)
		undo_receipt_action(r_toggle["receipts"][0])
		ro.reload()
		self.assertEqual(int(ro.services[0].is_completed), 0)

		# 6. UPDATE_JOB_STATUS + Undo (and verify authorization lock on unapproved RO)
		r_status = dispatch_orchestrator_decision(
			source,
			{
				"confidence": 0.96,
				"action": "UPDATE_JOB_STATUS",
				"action_payload": {"repair_order": ro.name, "action": "complete", "odometer_out": 10250},
			},
		)
		ro.reload()
		self.assertEqual(ro.status, "Work Completed")
		self.assertEqual(int(ro.odometer_out), 10250)
		undo_receipt_action(r_status["receipts"][0])
		ro.reload()
		self.assertEqual(ro.status, "Authorized - In Progress")

		unapproved_ro, _, _ = self._create_ro(approved=False)
		unapproved_tkey = ensure_thread_for_ro(unapproved_ro.name)
		unapproved_src = self._insert_capture_event(unapproved_tkey, "Start unapproved")
		with self.assertRaises(frappe.ValidationError):
			dispatch_orchestrator_decision(
				unapproved_src,
				{
					"confidence": 0.95,
					"action": "UPDATE_JOB_STATUS",
					"action_payload": {"repair_order": unapproved_ro.name, "action": "start"},
				},
			)

		# 7. ADD_DEFERRED_SERVICES + Undo
		veh_doc = frappe.get_doc("Repair Vehicle", vehicle.name)
		veh_doc.append(
			"deferred_services",
			{
				"job_title": "Cabin Air Filter Replacement",
				"estimated_cost": 120.0,
				"status": "Open Recommendation",
			},
		)
		veh_doc.save(ignore_permissions=True)
		r_def = dispatch_orchestrator_decision(
			source,
			{
				"confidence": 0.95,
				"action": "ADD_DEFERRED_SERVICES",
				"action_payload": {"repair_order": ro.name},
			},
		)
		self.assertEqual(r_def["outcome"], "EXECUTED")
		ro.reload()
		self.assertGreaterEqual(len(ro.services), 2)
		undo_receipt_action(r_def["receipts"][0])
		ro.reload()
		self.assertEqual(len(ro.services), 1)

		# 8. DRAFT_PURCHASE_ORDER -> creates Draft PO + auto-spawns Manager SUBMIT_PO gate + Undo
		r_po = dispatch_orchestrator_decision(
			source,
			{
				"confidence": 0.96,
				"action": "DRAFT_PURCHASE_ORDER",
				"action_payload": {
					"repair_order": ro.name,
					"parts_data": [
						{
							"item_code": part_item.name,
							"supplier": supplier_name,
							"qty": 1,
							"rate": 80.0,
							"order_mode": "Purchase Order",
						}
					],
				},
			},
		)
		self.assertEqual(r_po["outcome"], "EXECUTED")
		created_pos = r_po["results"][0]["result"]["data"]["purchase_orders"]
		self.assertEqual(len(created_pos), 1)
		po_name = created_pos[0]
		self.track("Purchase Order", po_name)
		self.assertEqual(int(frappe.db.get_value("Purchase Order", po_name, "docstatus")), 0)
		self.assertEqual(len(r_po["gates"]), 1)
		submit_gate = frappe.get_doc("Shop Board Event", r_po["gates"][0])
		self.assertEqual(json.loads(submit_gate.card_schema)["action_type"], "SUBMIT_PO")

		# Undo deletes Draft PO cleanly
		undo_receipt_action(r_po["receipts"][0])
		self.assertFalse(frappe.db.exists("Purchase Order", po_name))

	def test_manager_gates_seam2_sms_seam3_billing_and_double_tap_idempotency(self):
		# A. AMEND_QUOTE + SEND_QUOTE_APPROVAL on a Draft Estimate RO
		draft_ro, _, _ = self._create_ro(approved=False)
		draft_tkey = ensure_thread_for_ro(draft_ro.name)
		draft_src = self._insert_capture_event(draft_tkey, "Amend & send quote")

		g_amend = dispatch_orchestrator_decision(
			draft_src,
			{
				"confidence": 0.95,
				"action": "AMEND_QUOTE",
				"action_payload": {
					"repair_order": draft_ro.name,
					"add_services": [{"service_title": "Brake Fluid Flush", "labor_hours": 0.5}],
				},
			},
		)
		res_amend = resolve_card_action(g_amend["gates"][0], "approve", idempotency_key="amend-1")
		self.assertFalse(res_amend["already_resolved"])
		draft_ro.reload()
		self.assertEqual(len(draft_ro.services), 2)
		# Double-tap returns already_resolved=True without adding a 3rd service
		res_amend_dup = resolve_card_action(g_amend["gates"][0], "approve", idempotency_key="amend-2")
		self.assertTrue(res_amend_dup["already_resolved"])
		draft_ro.reload()
		self.assertEqual(len(draft_ro.services), 2)

		g_send = dispatch_orchestrator_decision(
			draft_src,
			{
				"confidence": 0.95,
				"action": "SEND_QUOTE_APPROVAL",
				"action_payload": {"repair_order": draft_ro.name, "communication_medium": "link"},
			},
		)
		res_send = resolve_card_action(g_send["gates"][0], "approve")
		self.assertTrue(res_send["execution_result"]["success"])
		self.assertTrue(res_send["execution_result"]["data"]["approval_url"])

		# B. Seam 2: DISPATCH_CUSTOMER_SMS (success + opt-out propagation + provider error) & EMAIL
		g_sms_ok = dispatch_orchestrator_decision(
			draft_src,
			{
				"confidence": 0.95,
				"action": "DISPATCH_CUSTOMER_SMS",
				"action_payload": {
					"repair_order": draft_ro.name,
					"phone_number": "+14165550199",
					"finding_summary": "Brake inspection complete.",
				},
			},
		)
		with patch(
			"ism.api.sms.dispatch_sms",
			return_value={"success": True, "message_id": "msg-ok-1", "error": None},
		) as mock_sms:
			res_sms = resolve_card_action(
				g_sms_ok["gates"][0],
				"approve",
				edited_message="Hi, your Tesla brake inspection is ready for review.",
			)
			mock_sms.assert_called_once_with(
				"+14165550199", "Hi, your Tesla brake inspection is ready for review."
			)
			self.assertEqual(res_sms["execution_result"]["delivery_status"], "SENT")
			self.assertEqual(res_sms["execution_result"]["message_id"], "msg-ok-1")

		g_sms_optout = dispatch_orchestrator_decision(
			draft_src,
			{
				"confidence": 0.95,
				"action": "DISPATCH_CUSTOMER_SMS",
				"action_payload": {
					"repair_order": draft_ro.name,
					"phone_number": "+14165550199",
					"finding_summary": "Followup",
				},
			},
		)
		with patch(
			"ism.api.sms.dispatch_sms",
			return_value={"success": False, "message_id": None, "error": "Recipient opted out via STOP"},
		):
			res_opt = resolve_card_action(g_sms_optout["gates"][0], "approve")
			self.assertFalse(res_opt["execution_result"]["success"])
			self.assertTrue(res_opt["execution_result"]["opted_out"])
			self.assertEqual(res_opt["execution_result"]["delivery_status"], "OPTED_OUT")

		g_email = dispatch_orchestrator_decision(
			draft_src,
			{
				"confidence": 0.95,
				"action": "DISPATCH_CUSTOMER_EMAIL",
				"action_payload": {
					"repair_order": draft_ro.name,
					"recipient": "customer@example.com",
					"subject": "Service Update",
				},
			},
		)
		with patch("frappe.sendmail") as mock_mail:
			res_mail = resolve_card_action(g_email["gates"][0], "approve", edited_message="Your vehicle is ready.")
			mock_mail.assert_called_once()
			self.assertEqual(res_mail["execution_result"]["delivery_status"], "SENT")

		# C. Seam 3: RECORD_DEPOSIT & CHECKOUT_REPAIR_ORDER writing canonical ERPNext Payment Entry + Sales Invoice
		ro_bill, _, _ = self._create_ro(approved=True)
		ro_bill.db_set("odometer_out", 10500, update_modified=False)
		bill_tkey = ensure_thread_for_ro(ro_bill.name)
		bill_src = self._insert_capture_event(bill_tkey, "Deposit and checkout")

		g_dep = dispatch_orchestrator_decision(
			bill_src,
			{
				"confidence": 0.96,
				"action": "RECORD_DEPOSIT",
				"action_payload": {"repair_order": ro_bill.name, "amount": 50.0},
			},
		)
		res_dep = resolve_card_action(g_dep["gates"][0], "approve", selected_option="Debit / Interac")
		dep_data = res_dep["execution_result"]["data"]
		if dep_data.get("payment_entry"):
			self.track("Payment Entry", dep_data["payment_entry"])
		self.assertAlmostEqual(float(dep_data["deposit_amount"]), 50.0, places=2)

		g_chk = dispatch_orchestrator_decision(
			bill_src,
			{
				"confidence": 0.96,
				"action": "CHECKOUT_REPAIR_ORDER",
				"action_payload": {"repair_order": ro_bill.name, "odometer_out": 10500},
			},
		)
		res_chk = resolve_card_action(
			g_chk["gates"][0],
			"approve",
			selected_option="Credit Card",
			form_patch={"odometer_out": 10525},
		)
		chk_data = res_chk["execution_result"]["data"]
		self.assertTrue(chk_data["sales_invoice"])
		self.track("Sales Invoice", chk_data["sales_invoice"])
		if chk_data.get("payment_entry"):
			self.track("Payment Entry", chk_data["payment_entry"])
		ro_bill.reload()
		self.assertEqual(ro_bill.status, "Closed / Released")
		self.assertEqual(int(ro_bill.odometer_out), 10525)

		# D. RESCHEDULE_APPOINTMENT with candidate slot & optional sanitized ETA SMS
		target_day = add_days(today(), 2)
		se = self.create_test_schedule_entry(
			repair_order=draft_ro.name,
			scheduled_date=target_day,
			scheduled_time="09:00:00",
			estimated_duration=60,
		)
		new_day = add_days(today(), 3)
		g_resched = dispatch_orchestrator_decision(
			draft_src,
			{
				"confidence": 0.95,
				"action": "RESCHEDULE_APPOINTMENT",
				"action_payload": {
					"repair_order": draft_ro.name,
					"schedule_entry": se.name,
					"candidate_slots": [{"scheduled_date": new_day, "start_time": "10:00"}],
					"send_customer_sms": True,
				},
			},
		)
		with patch(
			"ism.api.sms.dispatch_sms",
			return_value={"success": True, "message_id": "eta-1", "error": None},
		) as mock_eta_sms:
			res_rs = resolve_card_action(
				g_resched["gates"][0],
				"approve",
				selected_option="0",
				form_patch={"send_customer_sms": True},
			)
			self.assertTrue(res_rs["execution_result"]["success"])
			mock_eta_sms.assert_called_once()
		se.reload()
		self.assertEqual(str(se.scheduled_date), str(new_day))

	def test_phase2_intake_and_ap_receiving_gates(self):
		ensure_singleton_shop_threads()

		# 1. SHOP:INTAKE -> INTAKE_QUOTE_AND_BOOK -> creates RO + Schedule Entry + RO:<id> thread
		intake_src = self._insert_capture_event("SHOP:INTAKE", "Book Model Y brake check")
		intake_cust = self.create_test_customer()
		intake_veh = self.create_test_vehicle(customer=intake_cust.name, model="Model Y")
		book_day = add_days(today(), 4)

		g_intake = dispatch_orchestrator_decision(
			intake_src,
			{
				"confidence": 0.95,
				"action": "INTAKE_QUOTE_AND_BOOK",
				"action_payload": {
					"customer": intake_cust.name,
					"vehicle": intake_veh.name,
					"odometer_in": 45000,
					"services": [{"service_title": "Brake Service", "labor_hours": 1.0}],
					"candidate_slots": [{"scheduled_date": book_day, "start_time": "11:00"}],
				},
			},
		)
		res_intake = resolve_card_action(g_intake["gates"][0], "approve", selected_option="0")
		exec_in = res_intake["execution_result"]
		created_ro = exec_in["repair_order"]
		self.assertTrue(created_ro)
		self.track("Repair Order", created_ro)
		self.assertEqual(exec_in["ro_thread_key"], ro_thread_key(created_ro))
		self.assertTrue(frappe.db.exists("Shop Board Thread", exec_in["ro_thread_key"]))
		self.assertIn("/board?thread=", exec_in["board_url"])
		if exec_in.get("booking") and (exec_in["booking"].get("data") or {}).get("name"):
			self.track("Schedule Entry", exec_in["booking"]["data"]["name"])

		# 2. SHOP:INTAKE -> CONVERT_APPOINTMENT_TO_RO (requires odometer_in)
		provisional_se = self.create_test_schedule_entry(
			scheduled_date=add_days(today(), 5),
			scheduled_time="13:00:00",
			estimated_duration=60,
			provisional_customer_name=intake_cust.name,
			provisional_vehicle_info=intake_veh.vin,
		)
		g_conv = dispatch_orchestrator_decision(
			intake_src,
			{
				"confidence": 0.95,
				"action": "CONVERT_APPOINTMENT_TO_RO",
				"action_payload": {"schedule_entry": provisional_se.name},
			},
		)
		# Missing odometer_in raises ValidationError and keeps card PENDING
		with self.assertRaises(frappe.ValidationError):
			resolve_card_action(g_conv["gates"][0], "approve")
		self.assertEqual(
			frappe.db.get_value("Shop Board Event", g_conv["gates"][0], "resolution_state"),
			"PENDING",
		)
		res_conv = resolve_card_action(
			g_conv["gates"][0], "approve", form_patch={"odometer_in": 42100}
		)
		conv_ro = res_conv["execution_result"]["repair_order"]
		self.assertTrue(conv_ro)
		self.track("Repair Order", conv_ro)
		self.assertTrue(frappe.db.exists("Shop Board Thread", ro_thread_key(conv_ro)))

		# 3. SHOP:AP_RECEIVING -> SUBMIT_PO -> RECEIVE_PO -> POST_PURCHASE_INVOICE -> PAY_PURCHASE
		ap_src = self._insert_capture_event("SHOP:AP_RECEIVING", "Vendor box & bill arrived")
		ap_item = self.create_test_item(
			f"TEST-AP-{frappe.generate_hash(length=5)}", is_stock_item=1, standard_rate=90.0
		)
		ap_supplier = self._create_supplier("APREC")

		g_sub_po = dispatch_orchestrator_decision(
			ap_src,
			{
				"confidence": 0.96,
				"action": "SUBMIT_PO",
				"action_payload": {
					"parts_data": [
						{
							"item_code": ap_item.name,
							"supplier": ap_supplier,
							"qty": 2,
							"rate": 45.0,
							"order_mode": "Purchase Order",
						}
					]
				},
			},
		)
		res_sub_po = resolve_card_action(g_sub_po["gates"][0], "approve")
		po_name = res_sub_po["execution_result"]["data"]["purchase_orders"][0]
		self.track("Purchase Order", po_name)
		self.assertEqual(int(frappe.db.get_value("Purchase Order", po_name, "docstatus")), 1)

		g_rec = dispatch_orchestrator_decision(
			ap_src,
			{
				"confidence": 0.96,
				"action": "RECEIVE_PO",
				"action_payload": {"purchase_order": po_name},
			},
		)
		res_rec = resolve_card_action(g_rec["gates"][0], "approve")
		pr_name = res_rec["execution_result"]["data"]["purchase_receipt"]
		self.assertTrue(pr_name)
		self.track("Purchase Receipt", pr_name)

		g_pi = dispatch_orchestrator_decision(
			ap_src,
			{
				"confidence": 0.96,
				"action": "POST_PURCHASE_INVOICE",
				"action_payload": {
					"purchase_order": po_name,
					"bill_no": f"INV-{frappe.generate_hash(length=5)}",
					"bill_date": today(),
					"update_stock": False,
				},
			},
		)
		res_pi = resolve_card_action(g_pi["gates"][0], "approve")
		pi_name = res_pi["execution_result"]["purchase_invoice_result"]["data"]["purchase_invoice"]
		self.assertTrue(pi_name)
		self.track("Purchase Invoice", pi_name)

		g_pay = dispatch_orchestrator_decision(
			ap_src,
			{
				"confidence": 0.96,
				"action": "PAY_PURCHASE",
				"action_payload": {
					"purchase_invoice": pi_name,
					"reference_no": "CHQ-1001",
					"reference_date": today(),
				},
			},
		)
		res_pay = resolve_card_action(g_pay["gates"][0], "approve", selected_option="E-Transfer")
		pe_name = res_pay["execution_result"]["data"]["payment_entry"]
		self.assertTrue(pe_name)
		self.track("Payment Entry", pe_name)

	def test_rest_api_endpoints_media_proxy_urls_and_seam6_pin_vault(self):
		ro, _, _ = self._create_ro(approved=True)
		tkey = ensure_thread_for_ro(ro.name)

		# 1. build_ro_urls includes board_url and cockpit_url
		urls = build_ro_urls(ro.name)
		self.assertIn("board_url", urls)
		self.assertIn("cockpit_url", urls)
		self.assertTrue(urls["board_url"].endswith(f"/board?thread=RO%3A{ro.name}"))
		self.assertTrue(urls["cockpit_url"].endswith("/cockpit"))

		# 2. post_board_capture with multipart PNG + idempotency dedup
		png_bytes = _make_png_bytes()
		fake_file = FileStorage(
			stream=io.BytesIO(png_bytes),
			filename="bushing.png",
			content_type="image/png",
		)
		fake_req = types.SimpleNamespace(
			files=ImmutableMultiDict([("files", fake_file)]),
			headers={"Range": "bytes=0-15"},
		)
		with patch.object(frappe, "request", fake_req, create=True):
			cap1 = board_api.post_board_capture(
				repair_order=ro.name,
				text="Front bushing torn",
				intent_tag="finding",
				metadata={"skip_ai": True},
				idempotency_key="cap-idem-1",
			)
		self.assertTrue(cap1["ok"])
		self.assertFalse(cap1["duplicate"])
		ev_name = cap1["event"]["name"]
		self.track("Shop Board Event", ev_name)
		attachments = cap1["event"]["media_attachments"]
		self.assertEqual(len(attachments), 1)
		file_ref = attachments[0]["file_ref"]
		self.assertIn("ism.api.board.get_media", attachments[0]["url"])

		# Duplicate idempotency_key returns existing event without inserting another
		cap2 = board_api.post_board_capture(
			repair_order=ro.name,
			text="Front bushing torn",
			metadata={"skip_ai": True},
			idempotency_key="cap-idem-1",
		)
		self.assertTrue(cap2["duplicate"])
		self.assertEqual(cap2["event"]["name"], ev_name)

		# 3. get_media Range passthrough & IDOR rejection
		with patch.object(frappe, "request", fake_req, create=True):
			resp = board_api.get_media(ev_name, file_ref)
		self.assertEqual(resp.status_code, 206)
		self.assertEqual(len(resp.get_data()), 16)
		with self.assertRaises(frappe.PermissionError):
			board_api.get_media(ev_name, "m_invalid_idor_ref")

		# 4. list_board_threads, get_board_thread, get_thread_delta
		listed = board_api.list_board_threads(query=ro.name)
		self.assertTrue(listed["ok"])
		self.assertTrue(any(t["thread_key"] == tkey for t in listed["threads"]))

		thread_view = board_api.get_board_thread(repair_order=ro.name)
		self.assertTrue(thread_view["ok"])
		self.assertEqual(thread_view["thread"]["thread_key"], tkey)
		self.assertEqual(thread_view["repair_order_summary"]["name"], ro.name)

		# Seam 6 when P4 is absent
		with patch.dict(sys.modules, {"ism.portal.pin_vault": None}):
			absent_view = board_api.get_board_thread(repair_order=ro.name)
			self.assertEqual(
				absent_view["pin_state"],
				{
					"pin_feature_installed": False,
					"status": "Not Prompted",
					"has_pin": False,
					"can_reveal": False,
				},
			)
			absent_reveal = board_api.reveal_ro_pin(ro.name)
			self.assertFalse(absent_reveal["available"])
			self.assertFalse(absent_reveal["pin_feature_installed"])

		# Seam 6 when P4 is present (mocked ism.portal.pin_vault)
		fake_portal = types.ModuleType("ism.portal")
		fake_vault = types.ModuleType("ism.portal.pin_vault")
		fake_vault.get_ro_pin_state = MagicMock(
			return_value={
				"repair_order": ro.name,
				"pin_feature_installed": True,
				"status": "Provided",
				"has_pin": True,
				"can_reveal": True,
				"pin": "9999",  # Must be stripped by get_board_thread!
			}
		)
		fake_vault.get_ro_pin_for_staff = MagicMock(
			return_value={
				"repair_order": ro.name,
				"status": "Provided",
				"pin": "4321",
				"auto_mask_seconds": 15,
			}
		)
		with patch.dict(
			sys.modules,
			{"ism.portal": fake_portal, "ism.portal.pin_vault": fake_vault},
		):
			present_view = board_api.get_board_thread(repair_order=ro.name)
			self.assertTrue(present_view["pin_state"]["pin_feature_installed"])
			self.assertEqual(present_view["pin_state"]["status"], "Provided")
			self.assertTrue(present_view["pin_state"]["has_pin"])
			self.assertTrue(present_view["pin_state"]["can_reveal"])
			self.assertNotIn("pin", present_view["pin_state"])

			tech_email = self._create_user_with_roles("pin_tech_test@example.com", ["ISM Technician"])
			frappe.set_user(tech_email)
			revealed = board_api.reveal_ro_pin(ro.name)
			self.assertTrue(revealed["available"])
			self.assertEqual(revealed["pin"], "4321")
			self.assertEqual(revealed["auto_mask_seconds"], 15)
			fake_vault.get_ro_pin_for_staff.assert_called_once_with(ro.name, channel="Board")

			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				board_api.reveal_ro_pin(ro.name)

		frappe.set_user("Administrator")

		# 5. get_thread_delta
		delta = board_api.get_thread_delta(
			thread_key=tkey,
			after_event=ev_name,
			known_projection_version=thread_view["thread"]["projection_version"],
		)
		self.assertTrue(delta["ok"])
		self.assertFalse(delta["changed"])
		self.assertEqual(delta["events"], [])

		# 6. get_cockpit_dashboard & sync_cockpit_action (Manager ONLY)
		cockpit_res = board_api.get_cockpit_dashboard()
		self.assertTrue(cockpit_res["ok"])
		self.assertIn("gauges", cockpit_res)
		self.assertIn("action_queue", cockpit_res)

		sync_res = board_api.sync_cockpit_action("board:test-rule", "snooze", snooze_minutes=30, notes="Testing")
		self.assertTrue(sync_res["ok"])
		if (sync_res.get("data") or {}).get("name"):
			self.track("Shop Action Item", sync_res["data"]["name"])

	def test_followup_actions_chain_and_gate_rejection(self):
		ro, _, _ = self._create_ro(approved=True)
		tkey = ensure_thread_for_ro(ro.name)
		source = self._insert_capture_event(tkey, "Front left control arm torn")
		part_item = self.create_test_item(
			f"TEST-FU-{frappe.generate_hash(length=5)}", is_stock_item=1, standard_rate=75.0
		)
		supplier_name = self._create_supplier("FUP")

		out = dispatch_orchestrator_decision(
			source,
			{
				"intent": "supplement",
				"confidence": 0.96,
				"summary": "Add FL Control Arm supplement, draft PO, and stage customer SMS",
				"action": "ADD_SUPPLEMENT_DRAFT",
				"action_payload": {
					"repair_order": ro.name,
					"service_title": "Front Left Control Arm Replacement",
					"labor_hours": 1.5,
					"parts": [{"item_code": part_item.name, "qty": 1, "cost": 75.0}],
				},
				"followup_actions": [
					{
						"action": "DRAFT_PURCHASE_ORDER",
						"action_payload": {
							"repair_order": ro.name,
							"parts_data": [
								{
									"item_code": part_item.name,
									"supplier": supplier_name,
									"qty": 1,
									"rate": 75.0,
									"order_mode": "Purchase Order",
								}
							],
						},
					},
					{
						"action": "DISPATCH_CUSTOMER_SMS",
						"action_payload": {
							"repair_order": ro.name,
							"finding_summary": "Front left control arm bushing is worn and recommended for replacement.",
						},
					},
				],
			},
		)
		self.assertEqual(out["outcome"], "EXECUTED")
		# 2 autonomous receipts (ADD_SUPPLEMENT_DRAFT + DRAFT_PURCHASE_ORDER)
		self.assertEqual(len(out["receipts"]), 2)
		# 2 manager gates (auto-spawned SUBMIT_PO + followup DISPATCH_CUSTOMER_SMS)
		self.assertEqual(len(out["gates"]), 2)
		gate_types = [
			json.loads(frappe.db.get_value("Shop Board Event", g, "card_schema"))["action_type"]
			for g in out["gates"]
		]
		self.assertEqual(gate_types, ["SUBMIT_PO", "DISPATCH_CUSTOMER_SMS"])

		po_name = out["results"][1]["result"]["data"]["purchase_orders"][0]
		self.track("Purchase Order", po_name)
		self.assertEqual(int(frappe.db.get_value("Purchase Order", po_name, "docstatus")), 0)

		# Reject SUBMIT_PO gate -> zero submission, idempotent on second tap
		rej1 = resolve_card_action(out["gates"][0], "reject", notes="Use stock instead")
		self.assertFalse(rej1["already_resolved"])
		self.assertEqual(rej1["resolution_state"], "REJECTED")
		self.assertEqual(int(frappe.db.get_value("Purchase Order", po_name, "docstatus")), 0)
		rej2 = resolve_card_action(out["gates"][0], "reject")
		self.assertTrue(rej2["already_resolved"])
