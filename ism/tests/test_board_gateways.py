# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Active-RO Shop Board REST API, RBAC, Action Feed, Agent Context, Delta, Media & PIN tests."""

from __future__ import annotations

import importlib
import io
import json
import sys
import types
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import now_datetime
from werkzeug.datastructures import FileStorage, ImmutableMultiDict

from ism.api import board as board_api
from ism.api.agent_serializers import build_ro_urls
from ism.board.contracts import BoardContractError, ro_thread_key
from ism.board.gateways import dispatch_board_ai_result
from ism.board.projections import ensure_thread_for_ro
from ism.tests.base import ISMTestCase


def _make_png_bytes() -> bytes:
	return (
		b"\x89PNG\r\n\x1a\n"
		b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
		b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
		b"\x00\x00\x00\x00IEND\xaeB`\x82"
	)


class TestBoardGateways(ISMTestCase):
	"""Comprehensive tests for Active-RO Board gateways, REST APIs, RBAC, and Seams."""

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _create_user_with_roles(self, email: str, roles: list[str]) -> str:
		if not frappe.db.exists("User", email):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					"enabled": 1,
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": r} for r in roles],
				}
			).insert(ignore_permissions=True)
			self.track(user)
		return email

	def _create_ro(self, *, approved: bool = False, with_service: bool = True, **ro_kwargs):
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(
			customer=customer.name,
			model_year=2023,
			model="Model 3",
			manufacturer="Tesla",
		)
		services = (
			[
				{
					"service_title": "Brake Inspection",
					"status": "Approved" if approved else "Pending Approval",
					"labor_hours": 1.0,
					"labor_rate": 150.0,
					"labor_amount": 150.0,
					"parts_total": 0.0,
					"total_amount": 150.0,
				}
			]
			if with_service
			else []
		)
		ro = self.create_test_repair_order(
			customer.name,
			vehicle.name,
			status=ro_kwargs.pop("status", "Authorized - In Progress" if approved else "Draft Estimate"),
			approval_status=ro_kwargs.pop("approval_status", "Approved" if approved else "Not Sent"),
			services=services,
			**ro_kwargs,
		)
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

	def _track_ro_board_artifacts(self, ro_name: str) -> None:
		tkey = ro_thread_key(ro_name)
		for sai_name in frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro_name},
			pluck="name",
		):
			self.track("Shop Action Item", sai_name)
		for ev_name in frappe.get_all("Shop Board Event", filters={"thread": tkey}, pluck="name"):
			self.track("Shop Board Event", ev_name)

	def test_removed_endpoints_and_modules_absent(self):
		for removed_endpoint in ("resolve_board_card", "undo_board_receipt", "price_part"):
			self.assertFalse(
				hasattr(board_api, removed_endpoint),
				f"Removed endpoint {removed_endpoint} must not exist on ism.api.board",
			)
		for removed_mod in ("ism.board.comms_firewall", "ism.board.pricing"):
			with self.assertRaises(ModuleNotFoundError):
				importlib.import_module(removed_mod)

	def test_list_board_threads_active_ro_only_and_capture_guards(self):
		ro_open, _, _ = self._create_ro(approved=True, status="Authorized - In Progress")
		ro_closed, _, _ = self._create_ro(approved=True, status="Closed / Released")
		ro_cancelled, _, _ = self._create_ro(approved=True, status="Cancelled")
		ro_billed, _, _ = self._create_ro(approved=True, status="Billed / Paid")

		tkey_open = ensure_thread_for_ro(ro_open.name)
		tkey_closed = ensure_thread_for_ro(ro_closed.name)
		tkey_cancelled = ensure_thread_for_ro(ro_cancelled.name)
		tkey_billed = ensure_thread_for_ro(ro_billed.name)

		# Default status='OPEN' excludes CLOSED threads
		open_res = board_api.list_board_threads()
		self.assertTrue(open_res["ok"])
		open_keys = {t["thread_key"] for t in open_res["threads"]}
		self.assertIn(tkey_open, open_keys)
		self.assertNotIn(tkey_closed, open_keys)
		self.assertNotIn(tkey_cancelled, open_keys)
		for t in open_res["threads"]:
			self.assertEqual(t["thread_type"], "REPAIR_ORDER")

		# status='CLOSED' returns closed threads
		closed_res = board_api.list_board_threads(status="CLOSED")
		closed_keys = {t["thread_key"] for t in closed_res["threads"]}
		self.assertIn(tkey_closed, closed_keys)
		self.assertNotIn(tkey_open, closed_keys)

		# status='ALL' includes both
		all_res = board_api.list_board_threads(status="ALL")
		all_keys = {t["thread_key"] for t in all_res["threads"]}
		self.assertIn(tkey_open, all_keys)
		self.assertIn(tkey_closed, all_keys)

		# Create an ACTION_CARD on ro_open -> status='NEEDS_ACTION' returns it
		src = self._insert_capture_event(tkey_open, "Need supplement for front left bushing")
		dispatch_board_ai_result(
			src,
			{
				"transcript": "",
				"summary": "Front left bushing supplement",
				"followup_question": None,
				"action_items": [
					{
						"title": "Add front left bushing supplement",
						"details": "Cracked bushing on front left control arm.",
						"category": "Quote / Supplement",
						"priority": "High",
						"suggested_action": "ADD_SUPPLEMENT",
						"clearing_condition_type": "ro_has_supplement_or_service",
					}
				],
			},
		)
		self._track_ro_board_artifacts(ro_open.name)
		needs_res = board_api.list_board_threads(status="NEEDS_ACTION")
		self.assertIn(tkey_open, {t["thread_key"] for t in needs_res["threads"]})

		# post_board_capture rejects legacy singleton keys
		with self.assertRaises((BoardContractError, frappe.ValidationError)):
			board_api.post_board_capture(thread_key="SHOP:INTAKE", text="Should fail", metadata={"skip_ai": True})

		# post_board_capture rejects Closed / Released & Cancelled ROs
		with self.assertRaises(frappe.ValidationError):
			board_api.post_board_capture(
				thread_key=tkey_closed,
				text="Cannot post on closed RO",
				metadata={"skip_ai": True},
			)
		with self.assertRaises(frappe.ValidationError):
			board_api.post_board_capture(
				thread_key=tkey_cancelled,
				text="Cannot post on cancelled RO",
				metadata={"skip_ai": True},
			)

		# Billed / Paid ROs still allow post_board_capture (vehicle not yet released)
		billed_cap = board_api.post_board_capture(
			thread_key=tkey_billed,
			text="Final wipe-down before customer pickup",
			metadata={"skip_ai": True},
		)
		self.assertTrue(billed_cap["ok"])
		self.track("Shop Board Event", billed_cap["event"]["name"])

		# get_board_thread exposes can_post=False on closed RO and can_post=True on open/billed RO
		closed_view = board_api.get_board_thread(thread_key=tkey_closed)
		self.assertFalse(closed_view["can_post"])
		self.assertTrue(bool(closed_view.get("read_only_reason")))
		open_view = board_api.get_board_thread(thread_key=tkey_open)
		self.assertTrue(open_view["can_post"])

	def test_action_feed_update_card_rbac_and_agent_context_bundle(self):
		tech_email = self._create_user_with_roles("board_gw_tech@example.test", ["ISM Technician"])
		mgr_email = self._create_user_with_roles("board_gw_mgr@example.test", ["ISM Manager"])

		ro, _, _ = self._create_ro(approved=True, status="Authorized - In Progress")
		tkey = ensure_thread_for_ro(ro.name)

		# Post capture with PNG attachment so get_action_cards_context can surface media proxy URLs
		png_bytes = _make_png_bytes()
		fake_file = FileStorage(stream=io.BytesIO(png_bytes), filename="crack.png", content_type="image/png")
		fake_req = types.SimpleNamespace(files=ImmutableMultiDict([("files", fake_file)]), headers={})
		with patch.object(frappe, "request", fake_req, create=True):
			cap_res = board_api.post_board_capture(
				repair_order=ro.name,
				text="Front left bushing cracked and need to notify customer",
				metadata={"skip_ai": True},
			)
		src_name = cap_res["event"]["name"]
		self.track("Shop Board Event", src_name)
		src_doc = frappe.get_doc("Shop Board Event", src_name)
		src_doc.db_set("transcript", "Front left bushing cracked and notify customer", update_modified=False)
		src_doc.reload()

		dispatch_res = dispatch_board_ai_result(
			src_doc,
			{
				"transcript": "Front left bushing cracked and notify customer",
				"summary": "Supplement + customer update",
				"followup_question": None,
				"action_items": [
					{
						"title": "Add front left control arm supplement",
						"details": "Bushing cracked on front left.",
						"category": "Quote / Supplement",
						"priority": "High",
						"suggested_action": "ADD_SUPPLEMENT",
						"clearing_condition_type": "ro_has_supplement_or_service",
					},
					{
						"title": "Send customer ETA update",
						"details": "Let customer know about extra hour.",
						"category": "Customer Comms",
						"priority": "Medium",
						"suggested_action": "NOTIFY_CUSTOMER",
						"clearing_condition_type": "ro_sms_or_email_sent",
					},
				],
			},
		)
		self._track_ro_board_artifacts(ro.name)
		act_ids = dispatch_res["action_items"]
		self.assertEqual(len(act_ids), 2)
		act1, act2 = act_ids[0], act_ids[1]

		# 1. list_board_action_feed returns active RO open cards enriched with clearing_condition + thread_key/board_url
		feed = board_api.list_board_action_feed(status="Open", repair_order=ro.name)
		self.assertTrue(feed["ok"])
		feed_by_id = {it["name"]: it for it in feed["items"]}
		self.assertIn(act1, feed_by_id)
		self.assertIn(act2, feed_by_id)
		self.assertEqual(feed_by_id[act1]["thread_key"], tkey)
		self.assertIn(ro.name, feed_by_id[act1]["board_url"])
		self.assertEqual(feed_by_id[act1]["clearing_condition_type"], "ro_has_supplement_or_service")
		self.assertIsInstance(feed_by_id[act1]["clearing_condition"], str)
		self.assertIsInstance(feed_by_id[act1]["clearing_condition_params"], dict)

		# 2. RBAC: ISM Technician blocked from update_board_action_card and get_action_cards_context
		frappe.set_user(tech_email)
		with self.assertRaises(frappe.PermissionError):
			board_api.update_board_action_card(act1, "dismiss", notes="Tech should not dismiss")
		with self.assertRaises(frappe.PermissionError):
			board_api.get_action_cards_context(action_items=[act1])

		# 3. Manager/Advisor get_action_cards_context: single card, multi-card, and by repair_order
		frappe.set_user(mgr_email)
		ctx_single = board_api.get_action_cards_context(action_items=act1)
		self.assertTrue(ctx_single["ok"])
		self.assertEqual(ctx_single["count"], 1)
		item1 = ctx_single["items"][0]
		self.assertEqual(item1["name"], act1)
		self.assertEqual(item1["clearing_condition_type"], "ro_has_supplement_or_service")
		self.assertIsInstance(item1["clearing_condition"], dict)
		self.assertIsInstance(item1["clearing_condition_description"], str)
		self.assertIn("Front left bushing cracked", item1["triggering_event"]["transcript"] or "")
		self.assertGreaterEqual(len(item1["triggering_event"]["media_attachments"]), 1)
		self.assertIn("ism.api.board.get_media", item1["triggering_event"]["media_attachments"][0]["url"])

		ro_bundle = ctx_single["threads_by_ro"][ro.name]
		self.assertEqual(ro_bundle["repair_order"]["name"], ro.name)
		self.assertGreaterEqual(len(ro_bundle["services"]), 1)
		self.assertGreaterEqual(len(ro_bundle["recent_events"]), 1)

		ctx_multi = board_api.get_action_cards_context(action_items=[act1, act2])
		self.assertEqual(ctx_multi["count"], 2)

		ctx_by_ro = board_api.get_action_cards_context(repair_order=ro.name, status="Open")
		self.assertEqual(ctx_by_ro["count"], 2)

		# 4. Manager dismiss / reopen / resolve on update_board_action_card (silent, ZERO confirmation pill)
		state_mut_before = frappe.db.count(
			"Shop Board Event", {"thread": tkey, "payload_type": "STATE_MUTATION"}
		)
		dismiss_res = board_api.update_board_action_card(act2, "dismiss", notes="Not needed")
		self.assertTrue(dismiss_res["ok"])
		card2_name = frappe.db.get_value(
			"Shop Board Event", {"payload_type": "ACTION_CARD", "action_item": act2}, "name"
		)
		self.assertEqual(frappe.db.get_value("Shop Board Event", card2_name, "resolution_state"), "REJECTED")
		self.assertEqual(
			frappe.db.count("Shop Board Event", {"thread": tkey, "payload_type": "STATE_MUTATION"}),
			state_mut_before,
		)

		reopen_res = board_api.update_board_action_card(act2, "reopen")
		self.assertTrue(reopen_res["ok"])
		self.assertEqual(frappe.db.get_value("Shop Board Event", card2_name, "resolution_state"), "PENDING")

		resolve_res = board_api.update_board_action_card(act2, "resolve", notes="Handled manually")
		self.assertTrue(resolve_res["ok"])
		self.assertEqual(frappe.db.get_value("Shop Board Event", card2_name, "resolution_state"), "RESOLVED")
		self.assertEqual(
			frappe.db.count("Shop Board Event", {"thread": tkey, "payload_type": "STATE_MUTATION"}),
			state_mut_before,
		)

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
				intent_tag="Supplement",
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

		# 4. Seam 6 PIN reveal on active RO (absent & present)
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

		fake_portal = types.ModuleType("ism.portal")
		fake_vault = types.ModuleType("ism.portal.pin_vault")
		fake_vault.get_ro_pin_state = MagicMock(
			return_value={
				"repair_order": ro.name,
				"pin_feature_installed": True,
				"status": "Provided",
				"has_pin": True,
				"can_reveal": True,
				"pin": "9999",
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
		with patch.dict(sys.modules, {"ism.portal": fake_portal, "ism.portal.pin_vault": fake_vault}):
			present_view = board_api.get_board_thread(repair_order=ro.name)
			self.assertTrue(present_view["pin_state"]["pin_feature_installed"])
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

	def test_get_thread_delta_returns_updated_cards_on_auto_clear_dismiss_and_transcript(self):
		ro, _, _ = self._create_ro(approved=True, status="Authorized - In Progress")
		tkey = ensure_thread_for_ro(ro.name)
		src = self._insert_capture_event(tkey, "Voice capture pending transcript")

		res = dispatch_board_ai_result(
			src,
			{
				"transcript": "",
				"summary": "Two cards for delta test",
				"followup_question": None,
				"action_items": [
					{
						"title": "Add rear caliper supplement",
						"details": "Rear caliper seized.",
						"category": "Quote / Supplement",
						"priority": "High",
						"suggested_action": "ADD_SUPPLEMENT",
						"clearing_condition_type": "ro_has_supplement_or_service",
					},
					{
						"title": "Check customer callback",
						"details": "Optional manual follow-up.",
						"category": "Customer Comms",
						"priority": "Low",
						"suggested_action": "MANUAL_REVIEW",
						"clearing_condition_type": "manual",
					},
				],
			},
		)
		self._track_ro_board_artifacts(ro.name)
		act_auto, act_dismiss = res["action_items"][0], res["action_items"][1]
		card_auto = frappe.db.get_value(
			"Shop Board Event", {"payload_type": "ACTION_CARD", "action_item": act_auto}, "name"
		)
		card_dismiss = frappe.db.get_value(
			"Shop Board Event", {"payload_type": "ACTION_CARD", "action_item": act_dismiss}, "name"
		)

		ver_before = frappe.db.get_value("Shop Board Thread", tkey, "projection_version") or 1
		after_event = card_dismiss

		# 1. Auto-clear card_auto by adding a service row to the RO
		ro.reload()
		ro.append(
			"services",
			{
				"service_title": "Rear Caliper Replacement",
				"status": "Pending Approval",
				"labor_hours": 1.0,
				"labor_rate": 150.0,
				"labor_amount": 150.0,
				"parts_total": 0.0,
				"total_amount": 150.0,
			},
		)
		ro.save(ignore_permissions=True)

		# 2. Dismiss card_dismiss manually
		board_api.update_board_action_card(act_dismiss, "dismiss", notes="Handled")

		# 3. Complete transcript on capture event
		src.reload()
		src.ai_status = "COMPLETED"
		src.transcript = "Rear caliper is seized, need supplement"
		src.save(ignore_permissions=True)

		delta = board_api.get_thread_delta(
			thread_key=tkey,
			after_event=after_event,
			known_projection_version=ver_before,
		)
		self.assertTrue(delta["ok"])
		self.assertTrue(delta["changed"])
		updated_by_name = {c["name"]: c for c in delta["updated_cards"]}

		self.assertIn(card_auto, updated_by_name)
		self.assertEqual(updated_by_name[card_auto]["resolution_state"], "RESOLVED")

		self.assertIn(card_dismiss, updated_by_name)
		self.assertEqual(updated_by_name[card_dismiss]["resolution_state"], "REJECTED")

		self.assertIn(src.name, updated_by_name)
		self.assertEqual(updated_by_name[src.name]["transcript"], "Rear caliper is seized, need supplement")
