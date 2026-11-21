# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Tests for Shop OS Multimodal AI Orchestrator, Providers, Context Firewall, and Seam 4."""

from __future__ import annotations

import base64
import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, now_datetime, today

from ism.board.ai_providers import (
	DEFAULT_GEMINI_ENDPOINT,
	DEFAULT_OPENAI_ENDPOINT,
	GeminiRestProvider,
	MockMultimodalProvider,
	MultimodalProvider,
	OpenAICompatibleProvider,
	get_ai_provider,
)
from ism.board.comms_firewall import (
	CommsFirewallViolationError,
	SanitizedCustomerFactPayload,
	draft_customer_message,
	resolve_customer_arrival_time,
	sanitize_for_customer_comms,
	validate_customer_message_clean,
)
from ism.board.contracts import ALL_ACTIONS, BoardContractError
from ism.board.media_storage import store_media_payload
from ism.board.orchestrator import (
	enqueue_board_event,
	gather_thread_context,
	process_board_event,
)
from ism.board.projections import ensure_singleton_shop_threads, ensure_thread_for_ro
from ism.tests.base import ISMTestCase

# Minimal valid 1x1 PNG bytes for media storage round-trip tests
_TINY_PNG = (
	b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
	b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
	b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)
# Minimal valid WAV header + PCM payload for audio streaming tests
_TINY_WAV = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00@\x1f\x00\x00@\x1f\x00\x00\x01\x00\x08\x00data\x00\x00\x00\x00"


def _has_real_gateways() -> bool:
	try:
		from ism.board import gateways  # noqa: F401

		return callable(getattr(gateways, "dispatch_orchestrator_decision", None))
	except ImportError:
		return False


class _RecordingDispatcher:
	"""Fallback decision recorder used only when ism.board.gateways is not yet present."""

	def __init__(self) -> None:
		self.calls: list[tuple[str, dict[str, Any]]] = []

	def __call__(self, source_event: Any, decision: dict[str, Any]) -> dict[str, Any]:
		self.calls.append((source_event.name, decision))
		return {
			"recorded": True,
			"source_event": source_event.name,
			"action": decision.get("action"),
			"has_clarification": bool(decision.get("clarification")),
		}


class TestBoardOrchestratorAndFirewall(ISMTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		ensure_singleton_shop_threads()

		self._orig_settings = {
			"board_ai_provider": frappe.db.get_single_value("ISM Settings", "board_ai_provider"),
			"media_storage_backend": frappe.db.get_single_value("ISM Settings", "media_storage_backend"),
		}
		self._set_ai_provider("Mock (Deterministic Test)")

	def tearDown(self):
		try:
			settings = frappe.get_single("ISM Settings")
			for field, val in self._orig_settings.items():
				if hasattr(settings, field):
					setattr(settings, field, val)
			settings.flags.ignore_permissions = True
			settings.save()
			frappe.clear_cache(doctype="ISM Settings")
		except Exception:
			pass
		super().tearDown()

	def _set_ai_provider(self, provider_name: str) -> None:
		settings = frappe.get_single("ISM Settings")
		settings.board_ai_provider = provider_name
		settings.media_storage_backend = "Frappe Private File"
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.clear_cache(doctype="ISM Settings")

	def _create_ro(self, **ro_kwargs):
		customer = self.create_test_customer(customer_name="Jane Driver")
		vehicle = self.create_test_vehicle(
			customer=customer.name,
			vin=ro_kwargs.pop("vin", f"5YJ3E1EA1NF{frappe.generate_hash(length=6).upper()}"),
			model="Model Y",
			model_year=2022,
			manufacturer="Tesla",
		)
		ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name, **ro_kwargs)
		return ro, customer, vehicle

	def _insert_capture_event(
		self,
		thread_key: str,
		*,
		body_text: str = "",
		intent_tag: str = "",
		actor_type: str = "TECH",
		payload_type: str = "TEXT",
		metadata: dict[str, Any] | None = None,
		media_attachments: list[dict[str, Any]] | None = None,
	):
		thread = frappe.get_doc("Shop Board Thread", thread_key)
		doc = frappe.get_doc(
			{
				"doctype": "Shop Board Event",
				"thread": thread.name,
				"thread_type": thread.thread_type,
				"repair_order": thread.repair_order,
				"event_timestamp": now_datetime(),
				"actor_type": actor_type,
				"actor_user": "Administrator",
				"intent_tag": intent_tag,
				"payload_type": payload_type,
				"target_role": "ALL",
				"body_text": body_text,
				"metadata_json": json.dumps(metadata or {}),
				"media_attachments": json.dumps(media_attachments or []),
				"resolution_state": "NONE",
				"ai_status": "NONE",
			}
		)
		doc.insert(ignore_permissions=True)
		self.track(doc)
		return doc

	def _dispatch_event(self, event_name: str, clarification_patch: dict[str, Any] | None = None):
		if _has_real_gateways():
			return process_board_event(event_name, clarification_patch=clarification_patch), None
		recorder = _RecordingDispatcher()
		res = process_board_event(
			event_name,
			clarification_patch=clarification_patch,
			decision_dispatcher=recorder,
		)
		return res, recorder

	# ------------------------------------------------------------------
	# 1. Context Firewall & Seam 4 Arrival Offset Tests
	# ------------------------------------------------------------------

	def test_seam4_resolve_customer_arrival_time_with_p3_present_and_absent(self):
		# A. With mocked ism.scheduling.arrival_policy (P3 installed: 08:00 -> 07:45)
		fake_module = types.ModuleType("ism.scheduling.arrival_policy")
		fake_module.to_customer_arrival_time = lambda t: "07:45" if str(t).startswith("08:00") else str(t)

		with patch.dict(sys.modules, {"ism.scheduling.arrival_policy": fake_module}):
			self.assertEqual(resolve_customer_arrival_time("08:00"), "07:45")
			self.assertEqual(resolve_customer_arrival_time("08:00:00"), "07:45")

			payload = sanitize_for_customer_comms(
				{"customer_name": "Alex Taylor", "vehicle_summary": "2023 Tesla Model 3", "name": "RO-TEST-1"},
				finding_summary="Your appointment has been updated",
				approved_items=[],
				eta_window="2026-11-24",
				internal_start_time="08:00",
			)
			self.assertIn("07:45", payload.eta_window or "")
			self.assertNotIn("08:00", payload.eta_window or "")

			msg = draft_customer_message(payload, channel="sms", provider=MockMultimodalProvider())
			self.assertIn("07:45", msg["body"])
			for forbidden_word in ("buffer", "leeway", "grace period", "arrive early", "offset"):
				self.assertNotIn(forbidden_word, msg["body"].lower())

		# B. With ism.scheduling.arrival_policy absent (ImportError -> fallback to raw 08:00)
		with patch.dict(sys.modules, {"ism.scheduling.arrival_policy": None}):
			self.assertEqual(resolve_customer_arrival_time("08:00"), "08:00")
			payload_fallback = sanitize_for_customer_comms(
				{"customer_name": "Alex Taylor", "vehicle_summary": "2023 Tesla Model 3", "name": "RO-TEST-1"},
				finding_summary="Your appointment is confirmed",
				eta_window="2026-11-24",
				internal_start_time="08:00",
			)
			self.assertIn("08:00", payload_fallback.eta_window or "")

	def test_context_firewall_zero_leak_and_post_generation_guard(self):
		ro, _cust, _veh = self._create_ro()
		ro.override_notes = "SECRET_OVERRIDE_NOTE_991: tech bypassed lift lock"
		ro.verbal_notes = "SECRET_VERBAL_NOTE_882: customer sounded rushed"
		ro.flags.ignore_permissions = True
		ro.save()

		dirty_items = [
			{
				"description": "Front Left Control Arm Replacement",
				"sell_price": 415.50,
				"cost": 110.00,
				"supplier_cost": 110.00,
				"margin_pct": 42.7,
				"tesla_oem_price": 240.00,
				"override_notes": "do not tell customer",
				"verbal_notes": "internal only",
				"bay": "BAY-2",
				"tech_id": "HR-EMP-00099",
			}
		]
		dirty_summary = (
			"Torn front left control arm bushing [INTERNAL: supplier cost $110 margin 42%]. "
			"Bay 2 TECH-04 raw note: Tesla OEM price $240."
		)
		dirty_photos = [
			{"file_ref": "m_cleanphoto1234", "gdrive_file_id": "secret_drive_id", "bay": "BAY-2"},
			"javascript:alert(1)",
		]

		payload = sanitize_for_customer_comms(
			ro,
			finding_summary=dirty_summary,
			approved_items=dirty_items,
			clean_photo_refs=dirty_photos,
			approval_url="https://shop.example.com/quote-approval?token=abc123",
			eta_window="2026-11-25 at 10:30",
		)
		self.assertIsInstance(payload, SanitizedCustomerFactPayload)
		serialized = json.dumps(payload.to_dict())

		# Verify strict allowlist: zero internal secrets present in SanitizedCustomerFactPayload
		for forbidden in (
			"SECRET_OVERRIDE_NOTE_991",
			"SECRET_VERBAL_NOTE_882",
			"110.0",
			"42.7",
			"240.0",
			"supplier_cost",
			"margin",
			"tesla_oem_price",
			"override_notes",
			"verbal_notes",
			"BAY-2",
			"Bay 2",
			"TECH-04",
			"HR-EMP-00099",
			"secret_drive_id",
			"javascript:",
		):
			self.assertNotIn(forbidden, serialized)

		self.assertEqual(payload.clean_photo_refs, ["m_cleanphoto1234"])
		self.assertEqual(len(payload.approved_items), 1)
		self.assertEqual(
			set(payload.approved_items[0].keys()),
			{"description", "price"},
		)
		self.assertEqual(payload.approved_items[0]["price"], 415.50)

		# Draft SMS and Email through Mock provider
		sms_draft = draft_customer_message(payload, channel="sms", provider=MockMultimodalProvider())
		self.assertEqual(sms_draft["channel"], "sms")
		self.assertEqual(sms_draft["subject"], "")
		self.assertEqual(sms_draft["sanitized_facts_hash"], payload.facts_hash())
		self.assertEqual(sms_draft["clean_photo_refs"], ["m_cleanphoto1234"])
		self.assertIn("415.50", sms_draft["body"])

		email_draft = draft_customer_message(payload, channel="email", provider=MockMultimodalProvider())
		self.assertEqual(email_draft["channel"], "email")
		self.assertTrue(email_draft["subject"])
		self.assertIn("415.50", email_draft["body"])

		# Reject raw dicts passed directly to draft_customer_message
		with self.assertRaises(BoardContractError):
			draft_customer_message({"customer_name": "Raw Dict"}, channel="sms")  # type: ignore[arg-type]

		# Post-generation leak guard MUST block any rogue provider output leaking internal data
		leak_samples = [
			"Your part is $168 (our supplier cost is $110).",
			"We keep a 35% margin on this control arm.",
			"Compared to Tesla OEM price $240, we charge $168.",
			"See override_notes for lift clearance.",
			"Customer approved per verbal_notes.",
			"Assigned to Bay 2 with HR-EMP-00001.",
			"Raw transcript: yeah this bushing is toast.",
			"Please arrive at 07:45 which includes a 15 minute buffer.",
		]
		for bad_body in leak_samples:
			rogue_provider = MagicMock()
			rogue_provider.draft_customer_comms.return_value = {"subject": "", "body": bad_body}
			with self.assertRaises(CommsFirewallViolationError, msg=f"Failed to block leak: {bad_body}"):
				draft_customer_message(payload, channel="sms", provider=rogue_provider)

		# Also verify explicit forbidden_values guard catches raw RO note values if injected
		with self.assertRaises(CommsFirewallViolationError):
			validate_customer_message_clean(
				f"Update: {ro.override_notes}",
				forbidden_values=[ro.override_notes, ro.verbal_notes],
			)
		with self.assertRaises(CommsFirewallViolationError):
			validate_customer_message_clean(
				f"Update: {ro.verbal_notes}",
				forbidden_values=[ro.override_notes, ro.verbal_notes],
			)

	# ------------------------------------------------------------------
	# 2. All Phase 1 & Phase 2 MockMultimodalProvider Flows
	# ------------------------------------------------------------------

	def test_p1_ambiguous_finding_clarification_then_multi_action_and_zero_leak_sms(self):
		ro, _cust, _veh = self._create_ro()
		tkey = ensure_thread_for_ro(ro.name)
		ev = self._insert_capture_event(
			tkey,
			body_text="Hey found a control arm bushing torn on the front suspension.",
			intent_tag="Parts",
		)

		# Step 1: Ambiguous side -> CLARIFICATION_CARD (Front Left / Front Right / Both)
		res1, _rec1 = self._dispatch_event(ev.name)
		self.assertEqual(res1["ai_status"], "COMPLETED")
		d1 = res1["decision"]
		self.assertEqual(d1["action"], "ADD_SUPPLEMENT_DRAFT")
		self.assertIn("side", d1["missing_fields"])
		self.assertIsNotNone(d1["clarification"])
		option_labels = [opt["label"] for opt in d1["clarification"]["options"]]
		self.assertEqual(option_labels, ["Front Left", "Front Right", "Both"])

		# Step 2: Post-clarification ("Front Left") -> ADD_SUPPLEMENT_DRAFT + DRAFT_PURCHASE_ORDER + DISPATCH_CUSTOMER_SMS
		res2, _rec2 = self._dispatch_event(ev.name, clarification_patch={"selected_option": "Front Left"})
		self.assertEqual(res2["ai_status"], "COMPLETED")
		d2 = res2["decision"]
		self.assertGreaterEqual(d2["confidence"], 0.90)
		self.assertEqual(d2["missing_fields"], [])
		self.assertEqual(d2["action"], "ADD_SUPPLEMENT_DRAFT")
		self.assertEqual(d2["action_payload"]["service_position"], "Front Left")

		followup_names = [f["action"] for f in d2["followup_actions"]]
		self.assertIn("DRAFT_PURCHASE_ORDER", followup_names)
		self.assertIn("DISPATCH_CUSTOMER_SMS", followup_names)

		# Verify customer_comms passes Context Firewall with zero cost/margin/OEM leakage
		comms = d2["customer_comms"]
		self.assertIsNotNone(comms)
		self.assertEqual(comms["channel"], "sms")
		validate_customer_message_clean(comms["body"])
		self.assertNotIn("110", comms["body"])
		self.assertNotIn("240", comms["body"])

		# Step 3: Post-clarification ("Both") sets duplicate_opposite_side=True
		res3, _rec3 = self._dispatch_event(ev.name, clarification_patch={"selected_option": "Both"})
		self.assertTrue(res3["decision"]["action_payload"]["duplicate_opposite_side"])

	def test_p1_noisy_audio_fyi_checkout_deposit_and_delay_reschedule_flows(self):
		ro, _cust, _veh = self._create_ro()
		tkey = ensure_thread_for_ro(ro.name)

		# Create a real Schedule Entry linked to RO so context includes active_schedule_entries
		se = self.create_test_schedule_entry(
			repair_order=ro.name,
			customer=ro.customer,
			repair_vehicle=ro.vehicle,
			scheduled_date=add_days(today(), 1),
			scheduled_time="08:00:00",
			estimated_duration=60,
		)

		# 1. Noisy audio -> audio_clarity_score == 0.60 clarification
		ev_noisy = self._insert_capture_event(
			tkey,
			body_text="noisy shop impact gun audio",
			metadata={"simulate_noisy_audio": True},
		)
		res_noisy, _ = self._dispatch_event(ev_noisy.name)
		self.assertEqual(res_noisy["decision"]["audio_clarity_score"], 0.60)
		self.assertLess(res_noisy["decision"]["confidence"], 0.75)
		self.assertIsNotNone(res_noisy["decision"]["clarification"])

		# 2. FYI -> LOG_RO_NOTE
		ev_fyi = self._insert_capture_event(
			tkey,
			body_text="FYI torqued lug nuts to 129 lb-ft and test drove.",
			intent_tag="FYI",
		)
		res_fyi, _ = self._dispatch_event(ev_fyi.name)
		self.assertEqual(res_fyi["decision"]["action"], "LOG_RO_NOTE")
		self.assertGreaterEqual(res_fyi["decision"]["confidence"], 0.90)

		# 3. Checkout WITHOUT odometer -> clarification requiring odometer_out
		ev_chk_missing = self._insert_capture_event(
			tkey,
			body_text="Job is done, ready for checkout.",
			intent_tag="Checkout",
		)
		res_chk_missing, _ = self._dispatch_event(ev_chk_missing.name)
		self.assertEqual(res_chk_missing["decision"]["action"], "CHECKOUT_REPAIR_ORDER")
		self.assertIn("odometer_out", res_chk_missing["decision"]["missing_fields"])
		self.assertEqual(res_chk_missing["decision"]["clarification"]["input_field"], "odometer_out")

		# 4. Checkout WITH odometer -> CHECKOUT_REPAIR_ORDER with odometer_out=64210
		ev_chk_ok = self._insert_capture_event(
			tkey,
			body_text="done, odometer 64210, customer paying by Interac",
			intent_tag="Checkout",
		)
		res_chk_ok, _ = self._dispatch_event(ev_chk_ok.name)
		self.assertEqual(res_chk_ok["decision"]["action"], "CHECKOUT_REPAIR_ORDER")
		self.assertEqual(res_chk_ok["decision"]["action_payload"]["odometer_out"], 64210)
		self.assertEqual(res_chk_ok["decision"]["action_payload"]["payment_method"], "Debit / Interac")
		self.assertEqual(res_chk_ok["decision"]["missing_fields"], [])

		# 5. $500 Deposit -> RECORD_DEPOSIT
		ev_dep = self._insert_capture_event(
			tkey,
			body_text="Customer paid $500 deposit via Credit Card.",
			actor_type="MANAGER",
		)
		res_dep, _ = self._dispatch_event(ev_dep.name)
		self.assertEqual(res_dep["decision"]["action"], "RECORD_DEPOSIT")
		self.assertEqual(res_dep["decision"]["action_payload"]["amount"], 500.0)
		self.assertEqual(res_dep["decision"]["action_payload"]["payment_method"], "Credit Card")

		# 6. Delay -> RESCHEDULE_APPOINTMENT populated with real slots from find_available_slots
		ev_delay = self._insert_capture_event(
			tkey,
			body_text="Hit a seized bolt, need extra 2 hours to finish.",
			intent_tag="Blocker",
		)
		res_delay, _ = self._dispatch_event(ev_delay.name)
		delay_dec = res_delay["decision"]
		self.assertEqual(delay_dec["action"], "RESCHEDULE_APPOINTMENT")
		self.assertEqual(delay_dec["action_payload"]["schedule_entry"], se.name)
		self.assertEqual(delay_dec["action_payload"]["extra_minutes"], 120)
		self.assertIsInstance(delay_dec["action_payload"]["candidate_slots"], list)
		self.assertIsNotNone(delay_dec["customer_comms"])
		validate_customer_message_clean(delay_dec["customer_comms"]["body"])

	def test_p2_intake_and_ap_receiving_flows_with_real_context_gathering(self):
		# A. SHOP:INTAKE -> INTAKE_QUOTE_AND_BOOK with VIN decode + real context slots
		ev_intake = self._insert_capture_event(
			"SHOP:INTAKE",
			body_text="Customer called for brake inspection on VIN 5YJ3E1EA1NF000001 next available slot.",
			intent_tag="New Quote",
			actor_type="MANAGER",
		)
		thread_intake = frappe.get_doc("Shop Board Thread", "SHOP:INTAKE")
		intake_ctx = gather_thread_context(thread_intake, ev_intake)
		self.assertEqual(intake_ctx["thread_type"], "INTAKE")
		self.assertEqual(intake_ctx["decoded_vin"].get("vin"), "5YJ3E1EA1NF000001")
		self.assertIsInstance(intake_ctx["candidate_slots"], list)

		res_intake, _ = self._dispatch_event(ev_intake.name)
		self.assertEqual(res_intake["ai_status"], "COMPLETED")
		d_intake = res_intake["decision"]
		self.assertEqual(d_intake["action"], "INTAKE_QUOTE_AND_BOOK")
		self.assertEqual(d_intake["action_payload"]["vin"], "5YJ3E1EA1NF000001")
		self.assertGreater(d_intake["action_payload"]["estimated_total"], 0)
		self.assertIsInstance(d_intake["action_payload"]["candidate_slots"], list)

		# Walk-in appointment conversion missing odometer_in -> clarification card
		ev_walkin = self._insert_capture_event(
			"SHOP:INTAKE",
			body_text="Customer arrived for walk-in appointment.",
			intent_tag="Walk-In",
			actor_type="MANAGER",
		)
		res_walkin, _ = self._dispatch_event(ev_walkin.name)
		self.assertEqual(res_walkin["decision"]["action"], "CONVERT_APPOINTMENT_TO_RO")
		self.assertIn("odometer_in", res_walkin["decision"]["missing_fields"])

		# B. SHOP:AP_RECEIVING -> context gathers real open submitted POs + matches RECEIVE_PO / POST_PURCHASE_INVOICE
		company = frappe.defaults.get_defaults().get("company") or frappe.db.get_value("Company", {}, "name")
		warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		supplier = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": f"Test Tesla Parts Supply {frappe.generate_hash(length=5)}",
				"supplier_group": frappe.db.get_value("Supplier Group", {}, "name") or "All Supplier Groups",
			}
		)
		supplier.insert(ignore_permissions=True)
		self.track(supplier)

		item = self.create_test_item(
			item_code=f"TEST_AP_ARM_{frappe.generate_hash(length=5)}",
			is_stock_item=1,
			standard_rate=110.0,
		)

		po = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"supplier": supplier.name,
				"company": company,
				"schedule_date": today(),
				"items": [
					{
						"item_code": item.name,
						"qty": 2,
						"rate": 110.0,
						"schedule_date": today(),
						"warehouse": warehouse,
					}
				],
			}
		)
		po.insert(ignore_permissions=True)
		po.submit()
		self.track(po)

		# Packing slip capture -> RECEIVE_PO matched against open PO
		ev_pack = self._insert_capture_event(
			"SHOP:AP_RECEIVING",
			body_text=f"Packing slip arrived for {po.name} from {supplier.supplier_name}.",
			intent_tag="Packing Slip",
			actor_type="MANAGER",
		)
		thread_ap = frappe.get_doc("Shop Board Thread", "SHOP:AP_RECEIVING")
		ap_ctx = gather_thread_context(thread_ap, ev_pack)
		po_names_in_ctx = [row["name"] for row in ap_ctx["open_purchase_orders"]]
		self.assertIn(po.name, po_names_in_ctx)

		res_pack, _ = self._dispatch_event(ev_pack.name)
		self.assertEqual(res_pack["decision"]["action"], "RECEIVE_PO")
		self.assertEqual(res_pack["decision"]["action_payload"]["purchase_order"], po.name)

		# Vendor invoice with bill_no -> POST_PURCHASE_INVOICE
		ev_bill = self._insert_capture_event(
			"SHOP:AP_RECEIVING",
			body_text=f"Vendor invoice #INV-90421 for {po.name} from {supplier.supplier_name}.",
			intent_tag="Vendor Bill",
			actor_type="MANAGER",
		)
		res_bill, _ = self._dispatch_event(ev_bill.name)
		self.assertEqual(res_bill["decision"]["action"], "POST_PURCHASE_INVOICE")
		self.assertEqual(res_bill["decision"]["action_payload"]["purchase_order"], po.name)
		self.assertEqual(res_bill["decision"]["action_payload"]["bill_no"], "INV-90421")
		self.assertFalse(res_bill["decision"]["action_payload"]["update_stock"])
		self.assertEqual(res_bill["decision"]["followup_actions"], [])

		# Combined packing slip + vendor invoice -> POST_PURCHASE_INVOICE with update_stock=True
		# and NO redundant RECEIVE_PO followup
		ev_combo = self._insert_capture_event(
			"SHOP:AP_RECEIVING",
			body_text=f"Packing slip and vendor invoice INV-90422 arrived for PO {po.name} from {supplier.supplier_name}.",
			intent_tag="Vendor Bill",
			actor_type="MANAGER",
		)
		res_combo, _ = self._dispatch_event(ev_combo.name)
		self.assertEqual(res_combo["decision"]["action"], "POST_PURCHASE_INVOICE")
		self.assertEqual(res_combo["decision"]["action_payload"]["purchase_order"], po.name)
		self.assertEqual(res_combo["decision"]["action_payload"]["bill_no"], "INV-90422")
		self.assertTrue(res_combo["decision"]["action_payload"]["update_stock"])
		self.assertEqual(res_combo["decision"]["followup_actions"], [])

		# Vendor invoice WITHOUT bill_no -> zero hallucination clarification card
		ev_bill_missing = self._insert_capture_event(
			"SHOP:AP_RECEIVING",
			body_text=f"Vendor invoice received for {po.name}, number unreadable.",
			intent_tag="Vendor Bill",
			actor_type="MANAGER",
		)
		res_bill_missing, _ = self._dispatch_event(ev_bill_missing.name)
		self.assertEqual(res_bill_missing["decision"]["action"], "POST_PURCHASE_INVOICE")
		self.assertIn("bill_no", res_bill_missing["decision"]["missing_fields"])

	# ------------------------------------------------------------------
	# 3. Media Streaming, Lifecycle, Realtime & Error Handling
	# ------------------------------------------------------------------

	def test_media_bytes_streaming_enqueue_sync_and_error_handling(self):
		ro, _cust, _veh = self._create_ro()
		tkey = ensure_thread_for_ro(ro.name)

		img_ref = store_media_payload(
			"damage.png",
			"image/png",
			_TINY_PNG,
			thread_key=tkey,
			repair_order=ro.name,
			thread_name=tkey,
		)
		aud_ref = store_media_payload(
			"note.wav",
			"audio/wav",
			_TINY_WAV,
			thread_key=tkey,
			repair_order=ro.name,
			thread_name=tkey,
		)

		ev = self._insert_capture_event(
			tkey,
			body_text="FYI captured damage photo and voice note.",
			payload_type="MEDIA_CAPTURE",
			media_attachments=[img_ref, aud_ref],
		)

		captured_blobs: list[dict[str, Any]] = []

		class _SpyProvider(MockMultimodalProvider):
			def analyze_capture(self, thread_type, context, text, media_blobs, clarification_patch=None):
				captured_blobs.extend(media_blobs)
				return super().analyze_capture(thread_type, context, text, media_blobs, clarification_patch)

		recorder = _RecordingDispatcher()
		realtime_events: list[tuple[str, dict[str, Any]]] = []
		orig_publish = frappe.publish_realtime

		def _capture_rt(event=None, message=None, *args, **kwargs):
			realtime_events.append((str(event), dict(message or {})))
			return orig_publish(event, message, *args, **kwargs)

		with patch.object(frappe, "publish_realtime", side_effect=_capture_rt):
			res = process_board_event(
				ev.name,
				provider=_SpyProvider(),
				decision_dispatcher=recorder,
			)

		self.assertEqual(res["ai_status"], "COMPLETED")
		self.assertEqual(len(captured_blobs), 2)
		self.assertEqual(captured_blobs[0]["data"], _TINY_PNG)
		self.assertEqual(captured_blobs[1]["data"], _TINY_WAV)

		reloaded = frappe.get_doc("Shop Board Event", ev.name)
		self.assertEqual(reloaded.ai_status, "COMPLETED")
		self.assertGreaterEqual(float(reloaded.confidence_score or 0.0), 0.90)
		self.assertTrue(reloaded.transcript)

		ai_status_broadcasts = [
			msg.get("ai_status") for ev_name, msg in realtime_events if ev_name == "ism_board_ai_status"
		]
		self.assertIn("PROCESSING", ai_status_broadcasts)
		self.assertIn("COMPLETED", ai_status_broadcasts)

		# Verify enqueue_board_event runs synchronously under frappe.flags.in_test
		ev_sync = self._insert_capture_event(tkey, body_text="FYI sync test under in_test.")
		if _has_real_gateways():
			sync_res = enqueue_board_event(ev_sync.name)
			self.assertTrue(sync_res.get("synchronous"))
			self.assertEqual(sync_res.get("ai_status"), "COMPLETED")
		else:
			with patch("ism.board.orchestrator.process_board_event") as mock_proc:
				mock_proc.return_value = {"event_name": ev_sync.name, "ai_status": "COMPLETED"}
				sync_res = enqueue_board_event(ev_sync.name)
				mock_proc.assert_called_once()
				self.assertTrue(sync_res["synchronous"])

		# Verify provider failure marks ai_status=FAILED, scrubs secrets, and appends SYSTEM error event
		ev_fail = self._insert_capture_event(tkey, body_text="FYI trigger failure")
		failing_provider = MagicMock()
		failing_provider.analyze_capture.side_effect = RuntimeError("Upstream error api_key=SUPER_SECRET_12345")

		fail_res = process_board_event(
			ev_fail.name,
			provider=failing_provider,
			decision_dispatcher=recorder,
		)
		self.assertEqual(fail_res["ai_status"], "FAILED")
		self.assertNotIn("SUPER_SECRET_12345", fail_res["ai_error"])
		self.assertEqual(frappe.db.get_value("Shop Board Event", ev_fail.name, "ai_status"), "FAILED")

		sys_errors = frappe.get_all(
			"Shop Board Event",
			filters={"parent_event": ev_fail.name, "actor_type": "SYSTEM", "payload_type": "STATE_MUTATION"},
			fields=["name", "body_text"],
		)
		self.assertTrue(sys_errors)
		self.assertIn("AI processing failed", sys_errors[0]["body_text"])

	# ------------------------------------------------------------------
	# 4. Gemini REST & OpenAI-Compatible Mocked HTTPS + Factory Tests
	# ------------------------------------------------------------------

	def test_gemini_and_openai_providers_mocked_https_and_factory(self):
		# Factory tests
		self.assertIsNone(get_ai_provider(provider_override="Disabled"))
		mock_p = get_ai_provider(provider_override="Mock (Deterministic Test)")
		self.assertIsInstance(mock_p, MockMultimodalProvider)
		self.assertIsInstance(mock_p, MultimodalProvider)

		with self.assertRaises(BoardContractError):
			get_ai_provider(provider_override="UnknownProviderXYZ")

		# GeminiRestProvider mocked HTTPS
		gemini = GeminiRestProvider(
			api_key="test-gemini-key",
			endpoint=DEFAULT_GEMINI_ENDPOINT,
			orchestrator_model="gemini-2.5-flash",
			comms_model="gemini-2.5-flash-lite",
		)
		decision_json = {
			"intent": "fyi_note",
			"confidence": 0.95,
			"audio_clarity_score": 0.99,
			"transcript": "FYI brake pads look good",
			"summary": "Logged brake pad inspection note",
			"action": "LOG_RO_NOTE",
			"action_payload": {"note": "Brake pads at 8mm"},
			"missing_fields": [],
			"clarification": None,
			"customer_comms": None,
			"followup_actions": [],
		}
		mock_resp = MagicMock()
		mock_resp.raise_for_status.return_value = None
		mock_resp.json.return_value = {
			"candidates": [{"content": {"parts": [{"text": json.dumps(decision_json)}]}}]
		}

		with patch("ism.board.ai_providers.requests.post", return_value=mock_resp) as mock_post:
			result = gemini.analyze_capture(
				thread_type="REPAIR_ORDER",
				context={"thread_key": "RO:RO-1"},
				text="FYI brake pads at 8mm",
				media_blobs=[
					{"content_type": "image/png", "data": _TINY_PNG},
					{"content_type": "audio/wav", "data": _TINY_WAV},
				],
			)

		self.assertEqual(result["action"], "LOG_RO_NOTE")
		self.assertEqual(result["confidence"], 0.95)
		called_url = mock_post.call_args.args[0]
		called_headers = mock_post.call_args.kwargs["headers"]
		called_body = mock_post.call_args.kwargs["json"]
		self.assertEqual(called_url, f"{DEFAULT_GEMINI_ENDPOINT}/models/gemini-2.5-flash:generateContent")
		self.assertEqual(called_headers["x-goog-api-key"], "test-gemini-key")
		self.assertEqual(called_body["generationConfig"]["responseMimeType"], "application/json")
		self.assertIn("responseSchema", called_body["generationConfig"])

		parts = called_body["contents"][0]["parts"]
		self.assertEqual(len(parts), 3)
		self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/png")
		self.assertEqual(parts[1]["inline_data"]["data"], base64.b64encode(_TINY_PNG).decode("ascii"))
		self.assertEqual(parts[2]["inline_data"]["mime_type"], "audio/wav")

		# Gemini isolated customer comms call uses comms_model
		comms_resp = MagicMock()
		comms_resp.raise_for_status.return_value = None
		comms_resp.json.return_value = {
			"candidates": [
				{
					"content": {
						"parts": [
							{
								"text": json.dumps(
									{"subject": "", "body": "Hi Jane, your Tesla Model Y brake check is complete."}
								)
							}
						]
					}
				}
			]
		}
		payload = sanitize_for_customer_comms(
			{"customer_name": "Jane Driver", "vehicle_summary": "2022 Tesla Model Y", "name": "RO-1"},
			finding_summary="Brake check complete",
		)
		with patch("ism.board.ai_providers.requests.post", return_value=comms_resp) as mock_comms_post:
			draft = draft_customer_message(payload, channel="sms", provider=gemini)
		self.assertEqual(
			mock_comms_post.call_args.args[0],
			f"{DEFAULT_GEMINI_ENDPOINT}/models/gemini-2.5-flash-lite:generateContent",
		)
		self.assertIn("brake check is complete", draft["body"])

		# OpenAICompatibleProvider mocked HTTPS
		openai_provider = OpenAICompatibleProvider(
			api_key="test-openai-key",
			endpoint=DEFAULT_OPENAI_ENDPOINT,
			orchestrator_model="gpt-4o-mini",
			comms_model="gpt-4o-mini",
		)
		oa_resp = MagicMock()
		oa_resp.raise_for_status.return_value = None
		oa_resp.json.return_value = {
			"choices": [{"message": {"content": json.dumps(decision_json)}}]
		}
		with patch("ism.board.ai_providers.requests.post", return_value=oa_resp) as mock_oa_post:
			oa_result = openai_provider.analyze_capture(
				thread_type="REPAIR_ORDER",
				context={"thread_key": "RO:RO-1"},
				text="FYI brake pads at 8mm",
				media_blobs=[
					{"content_type": "image/png", "data": _TINY_PNG},
					{"content_type": "audio/wav", "data": _TINY_WAV},
				],
			)
		self.assertEqual(oa_result["action"], "LOG_RO_NOTE")
		self.assertEqual(mock_oa_post.call_args.args[0], f"{DEFAULT_OPENAI_ENDPOINT}/chat/completions")
		self.assertEqual(mock_oa_post.call_args.kwargs["headers"]["Authorization"], "Bearer test-openai-key")
		self.assertEqual(mock_oa_post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})
		self.assertIn(oa_result["action"], ALL_ACTIONS)

	def test_mock_synchronous_execution_outside_in_test_and_intent_pill_aliases(self):
		ro, _cust, _veh = self._create_ro()
		tkey = ensure_thread_for_ro(ro.name)
		ev_sync_prod = self._insert_capture_event(tkey, body_text="FYI torqued bolts to spec.", intent_tag="FYI")

		# 1. With in_test=False and Mock provider, enqueue_board_event STILL executes synchronously
		orig_in_test = getattr(frappe.flags, "in_test", False)
		try:
			frappe.flags.in_test = False
			self._set_ai_provider("Mock (Deterministic Test)")
			res_mock = enqueue_board_event(ev_sync_prod.name)
			self.assertTrue(res_mock.get("synchronous"))
			self.assertFalse(res_mock.get("queued"))
			self.assertEqual(res_mock.get("ai_status"), "COMPLETED")

			# With Gemini REST API and in_test=False, enqueue_board_event uses frappe.enqueue asynchronously
			ev_async_gemini = self._insert_capture_event(tkey, body_text="FYI async gemini test.")
			self._set_ai_provider("Gemini REST API")
			with patch("frappe.enqueue") as mock_enq:
				res_gem = enqueue_board_event(ev_async_gemini.name)
				mock_enq.assert_called_once()
				self.assertTrue(res_gem.get("queued"))
				self.assertFalse(res_gem.get("synchronous"))
				self.assertEqual(res_gem.get("ai_status"), "QUEUED")
		finally:
			frappe.flags.in_test = orig_in_test
			self._set_ai_provider("Mock (Deterministic Test)")

		# 2. Intent pill normalization: WALK_IN, PACKING_SLIP, VENDOR_BILL, NEW_QUOTE, VIN_PHOTO
		mock_provider = MockMultimodalProvider()
		d_walkin = mock_provider.analyze_capture(
			"INTAKE",
			{"intent_tag": "WALK_IN", "open_appointments": [{"name": "SE-001"}]},
			"Customer is here at the counter.",
			[],
		)
		self.assertEqual(d_walkin["action"], "CONVERT_APPOINTMENT_TO_RO")
		self.assertIn("odometer_in", d_walkin["missing_fields"])

		d_new_quote = mock_provider.analyze_capture(
			"INTAKE",
			{
				"intent_tag": "NEW_QUOTE",
				"candidate_slots": [{"date": add_days(today(), 2), "start_time": "09:00", "available_bay_ids": ["Bay 1"]}],
			},
			"Need a brake inspection quote please.",
			[],
		)
		self.assertEqual(d_new_quote["action"], "INTAKE_QUOTE_AND_BOOK")
		self.assertEqual(d_new_quote["action_payload"]["odometer_in"], 0)
		self.assertTrue(d_new_quote["action_payload"]["services"])
		self.assertEqual(d_new_quote["action_payload"]["candidate_slots"][0]["scheduled_date"], add_days(today(), 2))
		self.assertEqual(d_new_quote["action_payload"]["candidate_slots"][0]["service_bay"], "Bay 1")

		d_pack = mock_provider.analyze_capture(
			"AP_RECEIVING",
			{"intent_tag": "PACKING_SLIP", "open_purchase_orders": [{"name": "PO-101", "supplier": "Acme", "items": []}]},
			"Box dropped at loading dock.",
			[],
		)
		self.assertEqual(d_pack["action"], "RECEIVE_PO")
		self.assertIn("parts_data", d_pack["action_payload"])

		d_bill = mock_provider.analyze_capture(
			"AP_RECEIVING",
			{
				"intent_tag": "VENDOR_BILL",
				"event_metadata": {"bill_no": "INV-5512"},
				"open_purchase_orders": [{"name": "PO-101", "supplier": "Acme", "items": [{"item_code": "X"}]}],
			},
			"Paperwork attached from vendor.",
			[],
		)
		self.assertEqual(d_bill["action"], "POST_PURCHASE_INVOICE")
		self.assertEqual(d_bill["action_payload"]["bill_no"], "INV-5512")
		self.assertEqual(d_bill["action_payload"]["parts_data"], [{"item_code": "X"}])

		d_paid = mock_provider.analyze_capture(
			"AP_RECEIVING",
			{"intent_tag": "PAID", "open_purchase_orders": [{"name": "PO-101", "supplier": "Acme", "grand_total": 250.0, "items": []}]},
			"Paid vendor via E-Transfer.",
			[],
		)
		self.assertEqual(d_paid["action"], "PAY_PURCHASE")
		self.assertEqual(d_paid["action_payload"]["amount"], 250.0)
		self.assertEqual(d_paid["action_payload"]["paid_amount"], 250.0)
