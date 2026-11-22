# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime, nowdate

from ism.board.ai_providers import DEFAULT_GEMINI_MODEL
from ism.board.contracts import (
	BOARD_STATUSES,
	PAYLOAD_TYPES,
	RESOLUTION_STATES,
	SUGGESTED_ACTION_TO_CLEARING_CONDITION,
	THREAD_TYPES,
	BoardContractError,
	parse_ro_from_thread_key,
	ro_thread_key,
	validate_action_card,
)
from ism.board.projections import (
	ensure_thread_for_ro,
	on_shop_action_item_after_insert,
	project_external_domain_event,
	recompute_thread_projection,
	resolve_thread_key,
)
from ism.ism.doctype.shop_action_item.clearing import (
	build_clearing_condition_for_ro,
	evaluate_clearing_condition,
)
from ism.ism.doctype.shop_action_item.shop_action_item import create_shop_action_item
from ism.setup_tasks.seed import setup_default_ism_settings
from ism.tests.base import ISMTestCase


class TestBoardSchemaAndProjections(ISMTestCase):
	def _create_ro(self, **ro_kwargs):
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(
			customer=customer.name,
			model_year=2023,
			model="Model Y",
			manufacturer="Tesla",
		)
		return self.create_test_repair_order(customer.name, vehicle.name, **ro_kwargs)

	def _insert_event(self, thread_key, **kwargs):
		doc = frappe.get_doc(
			{
				"doctype": "Shop Board Event",
				"thread": thread_key,
				"event_timestamp": now_datetime(),
				"actor_type": kwargs.pop("actor_type", "TECH"),
				"payload_type": kwargs.pop("payload_type", "TEXT"),
				"body_text": kwargs.pop("body_text", "Test capture"),
				"resolution_state": kwargs.pop("resolution_state", "NONE"),
				**kwargs,
			}
		).insert(ignore_permissions=True)
		return self.track(doc)

	def _create_supplier(self, suffix: str) -> str:
		sup_name = f"BoardClearingSup-{suffix}-{frappe.generate_hash(length=5)}"
		sup = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": sup_name,
				"supplier_group": frappe.db.get_value("Supplier Group", {}, "name") or "All Supplier Groups",
			}
		).insert(ignore_permissions=True)
		self.track(sup)
		return sup.name

	def test_contracts_and_action_card_validator(self):
		self.assertEqual(THREAD_TYPES, ("REPAIR_ORDER",))
		self.assertIn("NEEDS_ACTION", BOARD_STATUSES)
		self.assertNotIn("BLOCKED_DECISION", BOARD_STATUSES)
		self.assertIn("ACTION_CARD", PAYLOAD_TYPES)
		self.assertEqual(RESOLUTION_STATES, ("NONE", "PENDING", "RESOLVED", "REJECTED"))
		self.assertEqual(
			SUGGESTED_ACTION_TO_CLEARING_CONDITION["ADD_SUPPLEMENT"],
			"ro_has_supplement_or_service",
		)
		self.assertEqual(
			SUGGESTED_ACTION_TO_CLEARING_CONDITION["ORDER_PARTS"],
			"po_submitted_or_received",
		)
		self.assertEqual(
			SUGGESTED_ACTION_TO_CLEARING_CONDITION["NOTIFY_CUSTOMER"],
			"ro_sms_or_email_sent",
		)

		self.assertEqual(ro_thread_key("RO-2026-00001"), "RO:RO-2026-00001")
		self.assertEqual(ro_thread_key("RO:RO-2026-00001"), "RO:RO-2026-00001")
		self.assertEqual(parse_ro_from_thread_key("RO:RO-2026-00001"), "RO-2026-00001")
		self.assertIsNone(parse_ro_from_thread_key("SHOP:INTAKE"))

		with self.assertRaises(BoardContractError):
			ro_thread_key("   ")

		card = validate_action_card(
			{
				"title": "Add front left upper control arm supplement",
				"details": "Bushing cracked on front left upper control arm.",
				"category": "Quote / Supplement",
				"priority": "High",
				"suggested_action": "add_supplement",
				"part_names": ["Front Left Upper Control Arm"],
			}
		)
		self.assertEqual(card["target_role"], "MANAGER")
		self.assertEqual(card["suggested_action"], "ADD_SUPPLEMENT")
		self.assertEqual(card["clearing_condition_type"], "ro_has_supplement_or_service")
		self.assertEqual(card["part_names"], ["Front Left Upper Control Arm"])

		with self.assertRaises(BoardContractError):
			validate_action_card({"title": "   "})
		with self.assertRaises(BoardContractError):
			validate_action_card({"title": "Bad cat", "category": "Not A Category"})
		with self.assertRaises(BoardContractError):
			validate_action_card({"title": "Bad prio", "priority": "Urgent"})
		with self.assertRaises(BoardContractError):
			validate_action_card({"title": "Bad cond", "clearing_condition_type": "not_a_condition"})

	def test_reject_legacy_singletons_and_validate_active_ro_threads(self):
		# Legacy singleton keys must be rejected by resolve_thread_key
		for legacy_key in ("SHOP:INTAKE", "SHOP:AP_RECEIVING", "SHOP:DISPATCH", "INTAKE"):
			with self.assertRaises(BoardContractError):
				resolve_thread_key(thread_key=legacy_key)

		# ShopBoardThread.validate() must reject non-REPAIR_ORDER thread_type or empty repair_order
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Shop Board Thread",
					"thread_key": "SHOP:INTAKE",
					"thread_type": "INTAKE",
					"title": "Legacy Intake",
				}
			).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Shop Board Thread",
					"thread_key": "RO:ORPHAN-NO-RO",
					"thread_type": "REPAIR_ORDER",
					"repair_order": None,
					"title": "Missing RO Link",
				}
			).insert(ignore_permissions=True)

	def test_seed_defaults_preserve_operator_values_and_upgrade_board_ai_model(self):
		# Ensure legacy singleton threads are not created by setup_default_ism_settings
		settings = frappe.get_single("ISM Settings")
		original_provider = settings.board_ai_provider
		original_model = settings.board_ai_model
		original_auto = settings.board_ai_auto_process
		try:
			settings.board_ai_provider = "Mock (Deterministic Test)"
			settings.board_ai_model = "gemini-2.5-flash"
			settings.board_ai_auto_process = 0
			settings.save(ignore_permissions=True)

			setup_default_ism_settings()

			reloaded = frappe.get_single("ISM Settings")
			self.assertEqual(reloaded.board_ai_provider, "Mock (Deterministic Test)")
			self.assertEqual(int(reloaded.board_ai_auto_process), 0)
			self.assertEqual(reloaded.media_storage_backend, "Frappe Private File")
			self.assertEqual(reloaded.board_ai_model, DEFAULT_GEMINI_MODEL)

			# Removed ISM Settings fields must not exist in DocType meta
			meta_fields = {df.fieldname for df in frappe.get_meta("ISM Settings").fields}
			self.assertNotIn("board_ai_comms_model", meta_fields)
			self.assertNotIn("board_ai_confidence_threshold", meta_fields)
			self.assertNotIn("parts_pricing_baseline_discount_pct", meta_fields)

			# Custom operator-chosen model must NOT be clobbered by setup_default_ism_settings()
			reloaded.board_ai_model = "gemini-3.8-flash"
			reloaded.save(ignore_permissions=True)
			setup_default_ism_settings()
			custom_reloaded = frappe.get_single("ISM Settings")
			self.assertEqual(custom_reloaded.board_ai_model, "gemini-3.8-flash")
		finally:
			settings.reload()
			settings.board_ai_provider = original_provider or "Disabled"
			settings.board_ai_model = original_model or DEFAULT_GEMINI_MODEL
			settings.board_ai_auto_process = original_auto if original_auto is not None else 0
			settings.save(ignore_permissions=True)

	def test_auto_provision_on_ro_insert_and_resolve_thread_key(self):
		ro = self._create_ro()
		expected_key = ro_thread_key(ro.name)
		self.assertTrue(frappe.db.exists("Shop Board Thread", expected_key))

		thread = frappe.get_doc("Shop Board Thread", expected_key)
		self.assertEqual(thread.thread_type, "REPAIR_ORDER")
		self.assertEqual(thread.repair_order, ro.name)
		self.assertEqual(thread.customer, ro.customer)
		self.assertEqual(thread.vehicle, ro.vehicle)
		self.assertEqual(thread.board_status, "ACTIVE")
		self.assertIn(ro.name, thread.title)

		# resolve_thread_key accepts canonical key, bare RO name, and repair_order kwarg
		self.assertEqual(resolve_thread_key(thread_key=expected_key), expected_key)
		self.assertEqual(resolve_thread_key(thread_key=ro.name), expected_key)
		self.assertEqual(resolve_thread_key(repair_order=ro.name), expected_key)

		# Lazy ensure works if thread was missing
		frappe.delete_doc("Shop Board Thread", expected_key, force=True, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Shop Board Thread", expected_key))
		self.assertEqual(ensure_thread_for_ro(ro.name), expected_key)
		self.assertTrue(frappe.db.exists("Shop Board Thread", expected_key))

	def test_event_immutability_and_trash_guard(self):
		ro = self._create_ro()
		thread_key = ro_thread_key(ro.name)
		ev = self._insert_event(
			thread_key,
			body_text="Original capture text",
			idempotency_key=f"idem-{frappe.generate_hash(length=8)}",
			resolution_state="PENDING",
			payload_type="ACTION_CARD",
			target_role="MANAGER",
		)

		# Deleting must fail even under in_test unless in_board_teardown is active
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Shop Board Event", ev.name, ignore_permissions=True)

		# Editing immutable core field must fail
		ev.reload()
		ev.body_text = "Tampered text"
		with self.assertRaises(frappe.ValidationError):
			ev.save(ignore_permissions=True)

		# Updating server-controlled resolution/AI fields must succeed
		ev.reload()
		ev.resolution_state = "RESOLVED"
		ev.resolved_by = "Administrator"
		ev.resolved_at = now_datetime()
		ev.resolution_payload = json.dumps({"status": "Resolved", "auto_cleared": 1})
		ev.ai_status = "COMPLETED"
		ev.transcript = "Transcribed audio"
		ev.confidence_score = 0.96
		ev.save(ignore_permissions=True)

		ev.reload()
		self.assertEqual(ev.resolution_state, "RESOLVED")
		self.assertEqual(ev.ai_status, "COMPLETED")
		self.assertEqual(ev.body_text, "Original capture text")

	def test_projection_state_transitions_and_blocked_since_preservation(self):
		ro = self._create_ro()
		thread_key = ro_thread_key(ro.name)

		# 1. Start ACTIVE
		proj = recompute_thread_projection(thread_key, publish=False)
		self.assertEqual(proj["board_status"], "ACTIVE")
		self.assertIsNone(proj["blocked_since"])

		# 2. Transition to WAITING_CUSTOMER via Pending Approval
		ro.db_set("approval_status", "Pending Approval")
		proj_wait = recompute_thread_projection(thread_key, publish=False)
		self.assertEqual(proj_wait["board_status"], "WAITING_CUSTOMER")
		self.assertIsNotNone(proj_wait["blocked_since"])

		# Backdate blocked_since to verify preservation across continuous blocked states
		anchor_blocked = now_datetime() - timedelta(hours=2)
		frappe.db.set_value("Shop Board Thread", thread_key, "blocked_since", anchor_blocked)

		# 3. Transition to BLOCKED_PARTS via manual_parts_hold -> preserves blocked_since
		ro.db_set("manual_parts_hold", 1)
		proj_parts = recompute_thread_projection(thread_key, publish=False)
		self.assertEqual(proj_parts["board_status"], "BLOCKED_PARTS")
		self.assertEqual(
			str(frappe.db.get_value("Shop Board Thread", thread_key, "blocked_since"))[:19],
			str(anchor_blocked)[:19],
		)

		# 4. Clear parts hold, add pending ACTION_CARD -> NEEDS_ACTION & preserves blocked_since
		ro.db_set("manual_parts_hold", 0)
		card_ev = self._insert_event(
			thread_key,
			actor_type="AI",
			payload_type="ACTION_CARD",
			target_role="MANAGER",
			resolution_state="PENDING",
			body_text="Order Front Left Control Arm",
		)
		proj_action = recompute_thread_projection(thread_key, publish=False)
		self.assertEqual(proj_action["board_status"], "NEEDS_ACTION")
		self.assertEqual(proj_action["open_action_count"], 1)
		self.assertEqual(proj_action["open_action_card_ids"], [card_ev.name])
		self.assertEqual(
			str(frappe.db.get_value("Shop Board Thread", thread_key, "blocked_since"))[:19],
			str(anchor_blocked)[:19],
		)

		# 5. Clear card and approval -> ACTIVE & clears blocked_since
		card_ev.reload()
		card_ev.resolution_state = "RESOLVED"
		card_ev.save(ignore_permissions=True)
		ro.db_set({"manual_parts_hold": 0, "approval_status": "Not Sent"})

		proj_active = recompute_thread_projection(thread_key, publish=False)
		self.assertEqual(proj_active["board_status"], "ACTIVE")
		self.assertEqual(proj_active["open_action_count"], 0)
		self.assertIsNone(proj_active["blocked_since"])

		# 6. Closed RO -> CLOSED
		ro.db_set("status", "Closed / Released")
		proj_closed = recompute_thread_projection(thread_key, publish=False)
		self.assertEqual(proj_closed["board_status"], "CLOSED")
		self.assertIsNone(proj_closed["blocked_since"])

	def test_schedule_entry_sync_and_realtime_broadcast(self):
		manifest = self.get_manifest()
		ro = self._create_ro()
		thread_key = ro_thread_key(ro.name)
		bay = next(iter(manifest.bays.values()))
		tech = next(iter(manifest.techs.values()))

		se = self.create_test_schedule_entry(
			repair_order=ro.name,
			customer=ro.customer,
			repair_vehicle=ro.vehicle,
			entry_type="Repair",
			service_bay=bay,
			assigned_technician=tech,
		)
		with patch("frappe.publish_realtime") as mock_rt:
			se.notes = "Updated bay assignment"
			se.save(ignore_permissions=True)

			thread = frappe.get_doc("Shop Board Thread", thread_key)
			self.assertEqual(thread.bay, bay)
			self.assertEqual(thread.assigned_technician, tech)
			self.assertIn(bay, thread.title)

			rt_events = [call.args[0] for call in mock_rt.call_args_list]
			self.assertIn("ism_board_projection", rt_events)
			self.assertIn("ism_board_thread_summary", rt_events)
			for call in mock_rt.call_args_list:
				if call.args[0] in ("ism_board_projection", "ism_board_thread_summary"):
					self.assertTrue(call.kwargs.get("after_commit"))

	def test_structured_clearing_conditions_and_silent_in_place_card_sync(self):
		# A. ro_has_supplement_or_service + silent ACTION_CARD auto-clear (ZERO confirmation pill)
		ro = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		thread_key = ro_thread_key(ro.name)

		ctype, cparams = build_clearing_condition_for_ro("ro_has_supplement_or_service", ro.name)
		sai_supp = create_shop_action_item(
			title="Add front left upper control arm supplement",
			category="Quote / Supplement",
			priority="High",
			source="Board AI",
			reference_doctype="Repair Order",
			reference_name=ro.name,
			dedup_key=f"board:test_supp:{ro.name}",
			clearing_condition_type=ctype,
			clearing_condition_json=json.dumps(cparams),
		)
		self.track("Shop Action Item", sai_supp)

		card_ev = self._insert_event(
			thread_key,
			actor_type="AI",
			payload_type="ACTION_CARD",
			action_item=sai_supp,
			target_role="MANAGER",
			resolution_state="PENDING",
			body_text="Add front left upper control arm supplement",
		)
		proj_before = recompute_thread_projection(thread_key, publish=False)
		self.assertEqual(proj_before["open_action_count"], 1)
		self.assertEqual(proj_before["board_status"], "NEEDS_ACTION")

		state_mut_count_before = frappe.db.count(
			"Shop Board Event", {"thread": thread_key, "payload_type": "STATE_MUTATION"}
		)

		# Adding a service row to the RO fires on_repair_order_update_clear_actions
		ro.reload()
		ro.append(
			"services",
			{
				"service_title": "Front Left Upper Control Arm Replacement",
				"status": "Pending Approval",
				"labor_hours": 1.5,
				"labor_rate": 150.0,
				"labor_amount": 225.0,
				"parts_total": 0.0,
				"total_amount": 225.0,
			},
		)
		ro.save(ignore_permissions=True)

		sai_supp_doc = frappe.get_doc("Shop Action Item", sai_supp)
		self.assertEqual(sai_supp_doc.status, "Resolved")
		self.assertEqual(int(sai_supp_doc.auto_cleared), 1)
		self.assertIn("Auto-cleared:", sai_supp_doc.resolution_notes or "")

		card_ev.reload()
		self.assertEqual(card_ev.resolution_state, "RESOLVED")
		self.assertEqual(
			frappe.db.get_value("Shop Board Thread", thread_key, "open_action_count"),
			0,
		)
		# ZERO STATE_MUTATION confirmation pill inserted on resolution!
		self.assertEqual(
			frappe.db.count("Shop Board Event", {"thread": thread_key, "payload_type": "STATE_MUTATION"}),
			state_mut_count_before,
		)

		# B. ro_status_in clears automatically when RO reaches Work Completed
		ctype_st, cparams_st = build_clearing_condition_for_ro(
			"ro_status_in",
			ro.name,
			overrides={"statuses": ["Work Completed", "Billed / Paid", "Closed / Released", "Cancelled"]},
		)
		sai_status = create_shop_action_item(
			title="Complete vehicle inspection and mark Work Completed",
			category="General",
			priority="Medium",
			source="Board AI",
			reference_doctype="Repair Order",
			reference_name=ro.name,
			dedup_key=f"board:test_status:{ro.name}",
			clearing_condition_type=ctype_st,
			clearing_condition_json=json.dumps(cparams_st),
		)
		self.track("Shop Action Item", sai_status)

		ro.reload()
		ro.status = "Work Completed"
		ro.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Shop Action Item", sai_status, "status"), "Resolved")
		self.assertEqual(int(frappe.db.get_value("Shop Action Item", sai_status, "auto_cleared")), 1)

		# C. po_submitted_or_received clears automatically on Purchase Order submit referencing RO
		ro_po = self._create_ro(status="Waiting on Parts", approval_status="Approved")
		item = self.create_test_item()
		supplier = self._create_supplier("po-clear")
		ctype_po, cparams_po = build_clearing_condition_for_ro("po_submitted_or_received", ro_po.name)
		sai_po = create_shop_action_item(
			title="Order replacement brake sensor",
			category="Inventory / Parts",
			priority="High",
			source="Board AI",
			reference_doctype="Repair Order",
			reference_name=ro_po.name,
			item_code=item.name,
			dedup_key=f"board:test_po:{ro_po.name}",
			clearing_condition_type=ctype_po,
			clearing_condition_json=json.dumps(cparams_po),
		)
		self.track("Shop Action Item", sai_po)

		company = frappe.defaults.get_global_default("company") or frappe.db.get_value("Company", {}, "name")
		warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		po = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"supplier": supplier,
				"company": company,
				"transaction_date": nowdate(),
				"schedule_date": nowdate(),
				"items": [
					{
						"item_code": item.name,
						"qty": 1,
						"rate": 45.0,
						"schedule_date": nowdate(),
						"warehouse": warehouse,
					}
				],
			}
		)
		if frappe.db.has_column("Purchase Order", "repair_order"):
			po.repair_order = ro_po.name
		elif frappe.db.has_column("Purchase Order", "custom_repair_order"):
			po.custom_repair_order = ro_po.name
		po.insert(ignore_permissions=True)
		self.track(po)
		po.submit()

		self.assertEqual(frappe.db.get_value("Shop Action Item", sai_po, "status"), "Resolved")
		self.assertEqual(int(frappe.db.get_value("Shop Action Item", sai_po, "auto_cleared")), 1)

		# D. ro_sms_or_email_sent clears automatically on outbound SMS Audit Log insert for RO
		ctype_sms, cparams_sms = build_clearing_condition_for_ro("ro_sms_or_email_sent", ro_po.name)
		sai_sms = create_shop_action_item(
			title="Notify customer about parts ETA",
			category="Customer Comms",
			priority="Medium",
			source="Board AI",
			reference_doctype="Repair Order",
			reference_name=ro_po.name,
			dedup_key=f"board:test_sms:{ro_po.name}",
			clearing_condition_type=ctype_sms,
			clearing_condition_json=json.dumps(cparams_sms),
		)
		self.track("Shop Action Item", sai_sms)

		sms_log = frappe.get_doc(
			{
				"doctype": "SMS Audit Log",
				"direction": "Outbound",
				"phone_e164": "+14165550191",
				"provider": "Mock (Deterministic Test)",
				"status": "Sent",
				"message_type": "Notification",
				"reference_doctype": "Repair Order",
				"reference_name": ro_po.name,
				"body_preview": "Parts ETA update sent",
			}
		).insert(ignore_permissions=True)
		self.track(sms_log)

		self.assertEqual(frappe.db.get_value("Shop Action Item", sai_sms, "status"), "Resolved")
		self.assertEqual(int(frappe.db.get_value("Shop Action Item", sai_sms, "auto_cleared")), 1)

		# E. schedule_converted_to_ro clears automatically when Schedule Entry links to an RO
		se = self.create_test_schedule_entry(
			provisional_customer_name="Unlinked Caller",
			provisional_phone="+14165550192",
			entry_type="Diagnostic",
		)
		sai_sched = create_shop_action_item(
			title="Convert self-serve booking to RO",
			category="General",
			priority="Medium",
			source="Agent",
			reference_doctype="Schedule Entry",
			reference_name=se.name,
			dedup_key=f"sched_conv:{se.name}",
			clearing_condition_type="schedule_converted_to_ro",
		)
		self.track("Shop Action Item", sai_sched)

		se.reload()
		se.repair_order = ro_po.name
		se.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Shop Action Item", sai_sched, "status"), "Resolved")
		self.assertEqual(int(frappe.db.get_value("Shop Action Item", sai_sched, "auto_cleared")), 1)

		# F. RO reaching Closed / Released auto-clears open RO items (including manual),
		# unless keep_open_on_close=True
		sai_manual = create_shop_action_item(
			title="Manual follow-up item",
			category="General",
			priority="Low",
			source="Manual",
			reference_doctype="Repair Order",
			reference_name=ro_po.name,
			dedup_key=f"board:manual_close:{ro_po.name}",
			clearing_condition_type="manual",
		)
		self.track("Shop Action Item", sai_manual)

		sai_keep_open = create_shop_action_item(
			title="Keep open after RO close",
			category="General",
			priority="Low",
			source="Manual",
			reference_doctype="Repair Order",
			reference_name=ro_po.name,
			dedup_key=f"board:keep_open:{ro_po.name}",
			clearing_condition_type="manual",
			clearing_condition_json=json.dumps({"keep_open_on_close": True}),
		)
		self.track("Shop Action Item", sai_keep_open)

		ro_po.reload()
		ro_po.status = "Closed / Released"
		ro_po.save(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("Shop Action Item", sai_manual, "status"), "Resolved")
		self.assertEqual(int(frappe.db.get_value("Shop Action Item", sai_manual, "auto_cleared")), 1)
		self.assertEqual(frappe.db.get_value("Shop Action Item", sai_keep_open, "status"), "Open")
		should_clear, _ = evaluate_clearing_condition(
			frappe.get_doc("Shop Action Item", sai_keep_open), ro_doc=ro_po
		)
		self.assertFalse(should_clear)

	def test_seam5_shop_action_item_projection_idempotency_and_self_echo(self):
		ro = self._create_ro()
		thread_key = ro_thread_key(ro.name)

		# 1. RO-linked Shop Action Item projects SYSTEM pill onto RO:<id>
		sai_ro = frappe.get_doc(
			{
				"doctype": "Shop Action Item",
				"title": "Customer approved quote online",
				"category": "General",
				"priority": "High",
				"status": "Open",
				"source": "Agent",
				"reference_doctype": "Repair Order",
				"reference_name": ro.name,
				"details": "Signed via Customer Portal",
			}
		).insert(ignore_permissions=True)
		self.track(sai_ro)

		ev_name = frappe.db.get_value(
			"Shop Board Event",
			{"idempotency_key": f"action_item:{sai_ro.name}"},
			"name",
		)
		self.assertIsNotNone(ev_name)
		self.track("Shop Board Event", ev_name)
		ev = frappe.get_doc("Shop Board Event", ev_name)
		self.assertEqual(ev.thread, thread_key)
		self.assertEqual(ev.actor_type, "SYSTEM")
		self.assertEqual(ev.payload_type, "STATE_MUTATION")
		meta = json.loads(ev.metadata_json)
		self.assertEqual(meta["action_item"], sai_ro.name)

		# Idempotent re-invocation returns same event without duplicating
		dup_name = on_shop_action_item_after_insert(sai_ro)
		self.assertEqual(dup_name, ev_name)
		self.assertEqual(
			frappe.db.count("Shop Board Event", {"idempotency_key": f"action_item:{sai_ro.name}"}),
			1,
		)

		# 2. Unlinked Schedule Entry Shop Action Item creates ZERO board thread/event
		se = self.create_test_schedule_entry(
			provisional_customer_name="Website Caller",
			provisional_phone="+14165550199",
			entry_type="Diagnostic",
		)
		sai_intake = frappe.get_doc(
			{
				"doctype": "Shop Action Item",
				"title": "New website self-serve booking",
				"category": "General",
				"priority": "Medium",
				"status": "Open",
				"source": "Agent",
				"reference_doctype": "Schedule Entry",
				"reference_name": se.name,
			}
		).insert(ignore_permissions=True)
		self.track(sai_intake)

		self.assertIsNone(
			frappe.db.get_value(
				"Shop Board Event",
				{"idempotency_key": f"action_item:{sai_intake.name}"},
				"name",
			)
		)
		self.assertFalse(frappe.db.exists("Shop Board Thread", "SHOP:INTAKE"))

		# 3. Self-echo suppression: dedup_key starting with "board:" or source == "Rule Snooze"
		sai_echo = frappe.get_doc(
			{
				"doctype": "Shop Action Item",
				"title": "Board parts hold self-echo",
				"category": "Inventory / Parts",
				"priority": "High",
				"status": "Open",
				"source": "Board AI",
				"dedup_key": f"board:parts_hold:{ro.name}",
				"reference_doctype": "Repair Order",
				"reference_name": ro.name,
			}
		).insert(ignore_permissions=True)
		self.track(sai_echo)
		self.assertIsNone(
			frappe.db.get_value(
				"Shop Board Event",
				{"idempotency_key": f"action_item:{sai_echo.name}"},
				"name",
			)
		)

		sai_snooze = frappe.get_doc(
			{
				"doctype": "Shop Action Item",
				"title": "Rule snooze marker",
				"category": "General",
				"priority": "Low",
				"status": "Open",
				"source": "Rule Snooze",
				"reference_doctype": "Repair Order",
				"reference_name": ro.name,
			}
		).insert(ignore_permissions=True)
		self.track(sai_snooze)
		self.assertIsNone(
			frappe.db.get_value(
				"Shop Board Event",
				{"idempotency_key": f"action_item:{sai_snooze.name}"},
				"name",
			)
		)

		# 4. project_external_domain_event helper idempotency + non-RO clean None
		ext_key = f"ext:{frappe.generate_hash(length=8)}"
		ev1 = project_external_domain_event(
			"portal_pin_submitted",
			{"repair_order": ro.name, "summary": "Customer submitted PIN-to-Drive"},
			idempotency_key=ext_key,
		)
		ev2 = project_external_domain_event(
			"portal_pin_submitted",
			{"repair_order": ro.name, "summary": "Customer submitted PIN-to-Drive"},
			idempotency_key=ext_key,
		)
		self.assertIsNotNone(ev1)
		self.track("Shop Board Event", ev1)
		self.assertEqual(ev1, ev2)
		self.assertEqual(frappe.db.get_value("Shop Board Event", ev1, "thread"), thread_key)

		non_ro_ev = project_external_domain_event(
			"self_serve_booking_created",
			{"schedule_entry": se.name, "summary": "Unlinked booking"},
			idempotency_key=f"ext-non-ro:{frappe.generate_hash(length=8)}",
		)
		self.assertIsNone(non_ro_ev)
