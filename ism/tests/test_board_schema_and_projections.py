# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from ism.board.contracts import (
	AUTONOMOUS_ACTIONS,
	MANAGER_GATED_ACTIONS,
	SINGLETON_THREADS,
	BoardContractError,
	parse_ro_from_thread_key,
	ro_thread_key,
	validate_approval_gate_card,
	validate_clarification_card,
	validate_receipt_card,
)
from ism.board.projections import (
	ensure_singleton_shop_threads,
	ensure_thread_for_ro,
	on_shop_action_item_after_insert,
	project_external_domain_event,
	recompute_thread_projection,
	resolve_thread_key,
)
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

	def test_contracts_and_card_validators(self):
		self.assertEqual(len(AUTONOMOUS_ACTIONS), 9)
		self.assertEqual(len(MANAGER_GATED_ACTIONS), 13)
		self.assertEqual(ro_thread_key("RO-2026-00001"), "RO:RO-2026-00001")
		self.assertEqual(ro_thread_key("RO:RO-2026-00001"), "RO:RO-2026-00001")
		self.assertEqual(parse_ro_from_thread_key("RO:RO-2026-00001"), "RO-2026-00001")
		self.assertIsNone(parse_ro_from_thread_key("SHOP:INTAKE"))

		with self.assertRaises(BoardContractError):
			ro_thread_key("   ")

		clar = validate_clarification_card(
			{
				"prompt": "Which side control arm?",
				"options": ["Front Left", "Front Right", "Both"],
				"action_type": "ADD_SUPPLEMENT_DRAFT",
			}
		)
		self.assertEqual(len(clar["options"]), 3)
		self.assertEqual(clar["options"][0], {"label": "Front Left", "value": "Front Left"})

		clar_input = validate_clarification_card(
			{"prompt": "Enter odometer out", "input_field": "odometer_out"}
		)
		self.assertEqual(clar_input["input_field"], "odometer_out")

		with self.assertRaises(BoardContractError):
			validate_clarification_card({"prompt": "Missing options and input"})

		gate = validate_approval_gate_card(
			{
				"action_type": "SUBMIT_PO",
				"title": "Submit PO for Front Left Control Arm",
				"params": {"po_name": "PUR-ORD-0001"},
			}
		)
		self.assertEqual(gate["target_role"], "MANAGER")

		with self.assertRaises(BoardContractError):
			validate_approval_gate_card(
				{"action_type": "LOG_RO_NOTE", "title": "Autonomous action cannot be gate"}
			)

		receipt = validate_receipt_card(
			{
				"action_type": "LOG_RO_NOTE",
				"summary": "Logged tech note on RO",
				"undo_spec": {"previous_note": ""},
			}
		)
		self.assertTrue(receipt["reversible"])

		with self.assertRaises(BoardContractError):
			validate_receipt_card({"action_type": "UNKNOWN_ACTION", "summary": "Bad"})

	def test_singleton_threads_and_seed_defaults_preserve_operator_values(self):
		ensured = ensure_singleton_shop_threads()
		self.assertEqual(set(ensured), set(SINGLETON_THREADS.keys()))
		for key, (expected_type, expected_title) in SINGLETON_THREADS.items():
			row = frappe.db.get_value(
				"Shop Board Thread",
				key,
				["thread_type", "title", "board_status", "repair_order"],
				as_dict=True,
			)
			self.assertIsNotNone(row)
			self.assertEqual(row.thread_type, expected_type)
			self.assertEqual(row.title, expected_title)
			self.assertEqual(row.board_status, "ACTIVE")
			self.assertIsNone(row.repair_order)

		# Calling twice must be idempotent
		self.assertEqual(set(ensure_singleton_shop_threads()), set(SINGLETON_THREADS.keys()))

		# Seed defaults match rules/parts_pricing.json and never clobber explicit 0 or operator config
		settings = frappe.get_single("ISM Settings")
		original_provider = settings.board_ai_provider
		original_baseline = settings.parts_pricing_baseline_discount_pct
		original_auto = settings.board_ai_auto_process
		try:
			settings.board_ai_provider = "Mock (Deterministic Test)"
			settings.parts_pricing_baseline_discount_pct = 0.0
			settings.board_ai_auto_process = 0
			settings.save(ignore_permissions=True)

			setup_default_ism_settings()

			reloaded = frappe.get_single("ISM Settings")
			self.assertEqual(reloaded.board_ai_provider, "Mock (Deterministic Test)")
			self.assertEqual(float(reloaded.parts_pricing_baseline_discount_pct), 0.0)
			self.assertEqual(int(reloaded.board_ai_auto_process), 0)
			self.assertEqual(reloaded.media_storage_backend, "Frappe Private File")
			self.assertEqual(reloaded.board_ai_orchestrator_model, "gemini-2.5-flash")
			self.assertEqual(float(reloaded.parts_pricing_small_ticket_max_tesla_price), 20.0)
			self.assertEqual(float(reloaded.parts_pricing_min_sell_price), 5.0)
			self.assertEqual(float(reloaded.parts_pricing_min_cost_markup_pct), 25.0)
			self.assertEqual(float(reloaded.parts_pricing_min_cost_margin_dollars), 5.0)
			self.assertEqual(float(reloaded.parts_pricing_fallback_cost_markup_pct), 40.0)
			self.assertEqual(float(reloaded.parts_pricing_rounding_increment), 0.01)
		finally:
			settings.reload()
			settings.board_ai_provider = original_provider or "Disabled"
			settings.parts_pricing_baseline_discount_pct = (
				original_baseline if original_baseline is not None else 30.0
			)
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

		# resolve_thread_key accepts canonical key, bare RO name, repair_order kwarg, and singletons
		self.assertEqual(resolve_thread_key(thread_key=expected_key), expected_key)
		self.assertEqual(resolve_thread_key(thread_key=ro.name), expected_key)
		self.assertEqual(resolve_thread_key(repair_order=ro.name), expected_key)
		self.assertEqual(resolve_thread_key(thread_key="SHOP:INTAKE"), "SHOP:INTAKE")

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
			payload_type="APPROVAL_GATE",
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
		ev.resolution_payload = json.dumps({"decision": "approve"})
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

		# 4. Pending APPROVAL_GATE + CLARIFICATION_CARD -> BLOCKED_DECISION & preserves blocked_since
		gate_ev = self._insert_event(
			thread_key,
			actor_type="AI",
			payload_type="APPROVAL_GATE",
			target_role="MANAGER",
			resolution_state="PENDING",
			body_text="Approve PO",
		)
		clar_ev = self._insert_event(
			thread_key,
			actor_type="AI",
			payload_type="CLARIFICATION_CARD",
			target_role="TECH",
			resolution_state="PENDING",
			body_text="Confirm LH or RH",
		)
		proj_dec = recompute_thread_projection(thread_key, publish=False)
		self.assertEqual(proj_dec["board_status"], "BLOCKED_DECISION")
		self.assertEqual(proj_dec["open_action_count"], 2)
		self.assertEqual(proj_dec["open_approval_count"], 1)
		self.assertEqual(proj_dec["open_clarification_count"], 1)
		self.assertEqual(set(proj_dec["open_action_card_ids"]), {gate_ev.name, clar_ev.name})
		self.assertEqual(
			str(frappe.db.get_value("Shop Board Thread", thread_key, "blocked_since"))[:19],
			str(anchor_blocked)[:19],
		)

		# 5. Clear cards, parts hold, and approval -> ACTIVE & clears blocked_since
		gate_ev.reload()
		gate_ev.resolution_state = "RESOLVED"
		gate_ev.save(ignore_permissions=True)
		clar_ev.reload()
		clar_ev.resolution_state = "RESOLVED"
		clar_ev.save(ignore_permissions=True)
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

		# 7. Singleton thread transitions ACTIVE <-> BLOCKED_DECISION and never CLOSED
		ensure_singleton_shop_threads()
		intake_card = self._insert_event(
			"SHOP:INTAKE",
			actor_type="AI",
			payload_type="APPROVAL_GATE",
			target_role="MANAGER",
			resolution_state="PENDING",
			body_text="Approve Intake Quote & Booking",
		)
		intake_blocked = recompute_thread_projection("SHOP:INTAKE", publish=False)
		self.assertEqual(intake_blocked["board_status"], "BLOCKED_DECISION")
		self.assertEqual(intake_blocked["open_approval_count"], 1)

		intake_card.reload()
		intake_card.resolution_state = "RESOLVED"
		intake_card.save(ignore_permissions=True)
		intake_active = recompute_thread_projection("SHOP:INTAKE", publish=False)
		self.assertEqual(intake_active["board_status"], "ACTIVE")

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
		# Trigger on_update hook via save
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

	def test_seam5_shop_action_item_projection_idempotency_and_self_echo(self):
		ensure_singleton_shop_threads()
		ro = self._create_ro()
		thread_key = ro_thread_key(ro.name)

		# 1. RO-linked Shop Action Item projects onto RO:<id>
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

		# 2. Unlinked Schedule Entry Shop Action Item projects onto SHOP:INTAKE
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

		intake_ev_name = frappe.db.get_value(
			"Shop Board Event",
			{"idempotency_key": f"action_item:{sai_intake.name}"},
			"name",
		)
		self.assertIsNotNone(intake_ev_name)
		self.track("Shop Board Event", intake_ev_name)
		self.assertEqual(
			frappe.db.get_value("Shop Board Event", intake_ev_name, "thread"),
			"SHOP:INTAKE",
		)

		# 3. Self-echo suppression: dedup_key starting with "board:" or source == "Rule Snooze"
		sai_echo = frappe.get_doc(
			{
				"doctype": "Shop Action Item",
				"title": "Board parts hold self-echo",
				"category": "Inventory / Parts",
				"priority": "High",
				"status": "Open",
				"source": "Agent",
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

		# 4. project_external_domain_event helper idempotency
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
