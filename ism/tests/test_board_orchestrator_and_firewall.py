# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Tests for simplified Active-RO Board AI (STT + Action Item Feed + natural-language follow-up)."""

from __future__ import annotations

import base64
import importlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import now_datetime

from ism.board import media_storage
from ism.board.ai_providers import (
	BOARD_AI_SCHEMA,
	DEFAULT_GEMINI_ENDPOINT,
	DEFAULT_GEMINI_MODEL,
	DEFAULT_GEMINI_THINKING_LEVEL,
	DEFAULT_HTTP_TIMEOUT_SECONDS,
	DEFAULT_OPENAI_ENDPOINT,
	BoardAIProvider,
	GeminiRestProvider,
	MockBoardAIProvider,
	OpenAICompatibleProvider,
	get_ai_provider,
)
from ism.board.contracts import BoardContractError, ro_thread_key
from ism.board.orchestrator import (
	enqueue_board_event,
	gather_ro_comms_context,
	process_board_event,
	should_skip_ai_for_capture,
)
from ism.tests.base import ISMTestCase

_TINY_PNG = (
	b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
	b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
	b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)

_TINY_WAV = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"


def _assert_no_empty_object_nodes(schema: Any, path: str = "$") -> None:
	"""Recursively verify Gemini v1beta responseSchema has zero empty OBJECT nodes."""
	if not isinstance(schema, dict):
		return
	if schema.get("type") == "OBJECT":
		props = schema.get("properties")
		if not isinstance(props, dict) or not props:
			raise AssertionError(f"Empty OBJECT schema node at {path}")
		for key, child in props.items():
			_assert_no_empty_object_nodes(child, f"{path}.{key}")
	if "items" in schema and isinstance(schema["items"], dict):
		_assert_no_empty_object_nodes(schema["items"], f"{path}[]")


class TestBoardOrchestratorAndFirewall(ISMTestCase):
	def setUp(self):
		super().setUp()
		settings = frappe.get_single("ISM Settings")
		self._orig_provider = settings.board_ai_provider
		self._orig_model = settings.board_ai_model
		self._orig_auto = settings.board_ai_auto_process
		self._orig_media_backend = settings.media_storage_backend
		self._set_ai_provider("Mock (Deterministic Test)")

	def tearDown(self):
		settings = frappe.get_single("ISM Settings")
		settings.board_ai_provider = self._orig_provider or "Disabled"
		settings.board_ai_model = self._orig_model or DEFAULT_GEMINI_MODEL
		settings.board_ai_auto_process = self._orig_auto if self._orig_auto is not None else 0
		settings.media_storage_backend = self._orig_media_backend or "Frappe Private File"
		settings.save(ignore_permissions=True)
		super().tearDown()

	def _set_ai_provider(self, provider_name: str) -> None:
		settings = frappe.get_single("ISM Settings")
		settings.board_ai_provider = provider_name
		settings.board_ai_auto_process = 1
		settings.media_storage_backend = "Frappe Private File"
		settings.save(ignore_permissions=True)

	def _create_ro(self, **ro_kwargs):
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(
			customer=customer.name,
			model_year=2023,
			model="Model Y",
			manufacturer="Tesla",
		)
		return self.create_test_repair_order(customer.name, vehicle.name, **ro_kwargs)

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
		meta = dict(metadata or {})
		if intent_tag:
			meta["intent_tag"] = intent_tag
		doc = frappe.get_doc(
			{
				"doctype": "Shop Board Event",
				"thread": thread_key,
				"event_timestamp": now_datetime(),
				"actor_type": actor_type,
				"actor_user": frappe.session.user,
				"payload_type": payload_type,
				"intent_tag": intent_tag or None,
				"body_text": body_text,
				"metadata_json": json.dumps(meta) if meta else None,
				"media_attachments": json.dumps(media_attachments) if media_attachments else None,
				"resolution_state": "NONE",
				"ai_status": "QUEUED",
				"idempotency_key": f"test-orch-{frappe.generate_hash(length=10)}",
			}
		).insert(ignore_permissions=True)
		return self.track(doc)

	def _track_ro_board_artifacts(self, ro_name: str) -> None:
		thread_key = ro_thread_key(ro_name)
		for sai_name in frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro_name},
			pluck="name",
		):
			self.track("Shop Action Item", sai_name)
		for ev_name in frappe.get_all(
			"Shop Board Event",
			filters={"thread": thread_key},
			pluck="name",
		):
			self.track("Shop Board Event", ev_name)

	def test_deleted_comms_firewall_module_does_not_exist(self):
		with self.assertRaises(ModuleNotFoundError):
			importlib.import_module("ism.board.comms_firewall")

	# ------------------------------------------------------------------
	# 1. Audio STT + Action Item Feed + Inline ACTION_CARD
	# ------------------------------------------------------------------

	def test_audio_capture_transcribes_and_creates_linked_action_card(self):
		ro = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		thread_key = ro_thread_key(ro.name)

		settings = frappe.get_single("ISM Settings")
		audio_ref = media_storage.store_media_payload(
			filename="tech_voice.wav",
			content_type="audio/wav",
			data_bytes=_TINY_WAV,
			thread_key=thread_key,
			repair_order=ro.name,
			settings=settings,
		)
		self.track("File", audio_ref["storage_id"])

		ev = self._insert_capture_event(
			thread_key,
			payload_type="MEDIA_CAPTURE",
			body_text="",
			intent_tag="Parts",
			metadata={"mock_transcript": "We are short one front left brake pad sensor, please order parts"},
			media_attachments=[audio_ref],
		)

		res = process_board_event(ev.name)
		self._track_ro_board_artifacts(ro.name)

		ev.reload()
		self.assertEqual(ev.ai_status, "COMPLETED")
		self.assertIn("brake pad sensor", (ev.transcript or "").lower())

		sais = frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name, "board_event": ev.name},
			fields=["name", "source", "category", "clearing_condition_type", "status"],
		)
		self.assertEqual(len(sais), 1)
		sai = sais[0]
		self.assertEqual(sai["source"], "Board AI")
		self.assertEqual(sai["status"], "Open")
		self.assertTrue(sai["clearing_condition_type"])
		self.assertNotEqual(sai["clearing_condition_type"], "manual")

		cards = frappe.get_all(
			"Shop Board Event",
			filters={"thread": thread_key, "payload_type": "ACTION_CARD", "action_item": sai["name"]},
			fields=["name", "actor_type", "resolution_state", "parent_event"],
		)
		self.assertEqual(len(cards), 1)
		self.assertEqual(cards[0]["actor_type"], "AI")
		self.assertEqual(cards[0]["resolution_state"], "PENDING")
		self.assertEqual(cards[0]["parent_event"], ev.name)
		self.assertEqual(res.get("outcome"), "ACTION_CARDS")

	# ------------------------------------------------------------------
	# 2. 2-Turn Natural-Language Technician Follow-Up (Zero Custom Card)
	# ------------------------------------------------------------------

	def test_two_turn_natural_language_followup_resolves_ambiguous_side(self):
		ro = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		thread_key = ro_thread_key(ro.name)

		# Turn 1: Ambiguous symmetrical part -> plain AI TEXT follow-up question, 0 action cards
		ev1 = self._insert_capture_event(
			thread_key,
			body_text="Upper control arm bushing is cracked, need supplement",
			intent_tag="Supplement",
		)
		res1 = process_board_event(ev1.name)
		self._track_ro_board_artifacts(ro.name)

		self.assertEqual(res1.get("outcome"), "FOLLOWUP_QUESTION")
		self.assertEqual(
			frappe.db.count("Shop Action Item", {"reference_doctype": "Repair Order", "reference_name": ro.name}),
			0,
		)
		self.assertEqual(
			frappe.db.count("Shop Board Event", {"thread": thread_key, "payload_type": "ACTION_CARD"}),
			0,
		)

		ai_questions = frappe.get_all(
			"Shop Board Event",
			filters={"thread": thread_key, "actor_type": "AI", "payload_type": "TEXT", "parent_event": ev1.name},
			fields=["name", "body_text", "resolution_state"],
		)
		self.assertEqual(len(ai_questions), 1)
		self.assertEqual(ai_questions[0]["resolution_state"], "NONE")
		q_lower = (ai_questions[0]["body_text"] or "").lower()
		self.assertTrue("left" in q_lower and "right" in q_lower)

		# Turn 2: Tech replies naturally "Front left" -> AI reads recent_messages and emits complete action item
		ev2 = self._insert_capture_event(
			thread_key,
			body_text="Front left",
		)
		res2 = process_board_event(ev2.name)
		self._track_ro_board_artifacts(ro.name)

		self.assertEqual(res2.get("outcome"), "ACTION_CARDS")
		sais = frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name},
			fields=["name", "title", "category", "source", "clearing_condition_type", "board_event"],
		)
		self.assertEqual(len(sais), 1)
		self.assertEqual(sais[0]["source"], "Board AI")
		self.assertEqual(sais[0]["category"], "Quote / Supplement")
		self.assertEqual(sais[0]["clearing_condition_type"], "ro_has_supplement_or_service")
		self.assertEqual(sais[0]["board_event"], ev2.name)

		cards = frappe.get_all(
			"Shop Board Event",
			filters={"thread": thread_key, "payload_type": "ACTION_CARD", "action_item": sais[0]["name"]},
			pluck="name",
		)
		self.assertEqual(len(cards), 1)

	# ------------------------------------------------------------------
	# 3. Multi-Item Capture Emits Multiple Action Items + ACTION_CARDs
	# ------------------------------------------------------------------

	def test_multi_item_capture_emits_multiple_action_items_and_cards(self):
		ro = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		thread_key = ro_thread_key(ro.name)

		ev = self._insert_capture_event(
			thread_key,
			body_text=(
				"Front left control arm bushing is cracked so we need a supplement, "
				"and please order the missing sway bar link and call the customer about the delay."
			),
		)
		res = process_board_event(ev.name)
		self._track_ro_board_artifacts(ro.name)

		self.assertEqual(res.get("outcome"), "ACTION_CARDS")
		sais = frappe.get_all(
			"Shop Action Item",
			filters={"reference_doctype": "Repair Order", "reference_name": ro.name, "board_event": ev.name},
			pluck="name",
		)
		self.assertGreaterEqual(len(sais), 2)
		cards = frappe.get_all(
			"Shop Board Event",
			filters={"thread": thread_key, "payload_type": "ACTION_CARD", "parent_event": ev.name},
			pluck="name",
		)
		self.assertEqual(len(cards), len(sais))

	# ------------------------------------------------------------------
	# 4. Cost-Saving Zero-AI Skip + Compact RO Context
	# ------------------------------------------------------------------

	def test_cost_saving_zero_ai_skip_and_compact_ro_context(self):
		# Direct unit checks for should_skip_ai_for_capture
		self.assertTrue(should_skip_ai_for_capture("FYI brake pads at 8mm", "FYI", [], {}))
		self.assertTrue(should_skip_ai_for_capture("ok", None, [], {}))
		self.assertTrue(should_skip_ai_for_capture("Got it!", None, [], {}))
		self.assertTrue(should_skip_ai_for_capture("thanks", None, [], {}))
		self.assertTrue(should_skip_ai_for_capture("Need parts", "Parts", [], {"skip_ai": True}))
		# Audio attachments must NOT skip AI even with FYI tag (STT still needed)
		self.assertFalse(
			should_skip_ai_for_capture(
				"",
				"FYI",
				[{"content_type": "audio/webm", "file_ref": "m_1"}],
				{},
			)
		)
		self.assertFalse(should_skip_ai_for_capture("Front left bushing cracked", "Supplement", [], {}))

		# End-to-end enqueue skip: zero provider calls, ai_status='NONE'
		ro = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		thread_key = ro_thread_key(ro.name)
		thread_doc = frappe.get_doc("Shop Board Thread", thread_key)

		with patch("ism.board.orchestrator.get_ai_provider") as mock_factory:
			for text, intent, meta in (
				("Just letting you know lift 2 is free", "FYI", {}),
				("ok", "", {}),
				("got it", "", {}),
				("thanks", "", {}),
				("Explicit skip note", "Parts", {"skip_ai": True}),
			):
				ev = self._insert_capture_event(thread_key, body_text=text, intent_tag=intent, metadata=meta)
				enqueue_board_event(ev.name)
				ev.reload()
				self.assertEqual(ev.ai_status, "NONE")
			mock_factory.assert_not_called()

		# Compact context verification: <=4 recent_messages, no heavy ERP blobs
		target_ev = self._insert_capture_event(thread_key, body_text="Front left caliper leaking")
		ctx = gather_ro_comms_context(thread_doc, target_ev)
		self.assertEqual(ctx.get("ro_name"), ro.name)
		self.assertIn("recent_messages", ctx)
		self.assertLessEqual(len(ctx["recent_messages"]), 4)
		for heavy_key in ("parts_stock", "candidate_slots", "pricing_config", "open_purchase_orders"):
			self.assertNotIn(heavy_key, ctx)

	# ------------------------------------------------------------------
	# 5. Gemini REST & OpenAI Mocked HTTPS + Factory + Failure Handling
	# ------------------------------------------------------------------

	def test_gemini_and_openai_providers_mocked_https_and_factory(self):
		# Schema check: zero empty OBJECT nodes in BOARD_AI_SCHEMA
		_assert_no_empty_object_nodes(BOARD_AI_SCHEMA)

		# Factory tests
		self.assertIsNone(get_ai_provider(provider_override="Disabled"))
		mock_p = get_ai_provider(provider_override="Mock (Deterministic Test)")
		self.assertIsInstance(mock_p, MockBoardAIProvider)
		self.assertIsInstance(mock_p, BoardAIProvider)

		with self.assertRaises(BoardContractError):
			get_ai_provider(provider_override="UnknownProviderXYZ")

		self.assertEqual(DEFAULT_GEMINI_MODEL, "gemini-3.6-flash")
		self.assertEqual(DEFAULT_GEMINI_THINKING_LEVEL, "LOW")
		self.assertGreaterEqual(DEFAULT_HTTP_TIMEOUT_SECONDS, 90)

		# GeminiRestProvider via factory (uses board_ai_model default gemini-3.6-flash)
		gemini_settings = SimpleNamespace(
			board_ai_provider="Gemini REST API",
			board_ai_api_key="test-gemini-secret-key",
			board_ai_endpoint=DEFAULT_GEMINI_ENDPOINT,
			board_ai_model=DEFAULT_GEMINI_MODEL,
		)
		gemini = get_ai_provider(settings=gemini_settings)
		self.assertIsInstance(gemini, GeminiRestProvider)

		ai_payload = {
			"transcript": "Front left upper control arm bushing is cracked",
			"summary": "Cracked front left upper control arm bushing",
			"followup_question": None,
			"action_items": [
				{
					"title": "Add front left upper control arm supplement",
					"details": "Tech found cracked bushing on front left upper control arm.",
					"category": "Quote / Supplement",
					"priority": "High",
					"suggested_action": "ADD_SUPPLEMENT",
					"clearing_condition_type": "ro_has_supplement_or_service",
					"clearing_condition_description": "When supplement is added to RO",
					"part_names": ["Front Left Upper Control Arm"],
					"side": "Front Left",
					"odometer": None,
				}
			],
		}
		mock_resp = MagicMock()
		mock_resp.raise_for_status.return_value = None
		mock_resp.json.return_value = {
			"candidates": [
				{
					"content": {
						"parts": [
							{"thought": True, "text": "internal thought trace not JSON"},
							{"text": json.dumps(ai_payload)},
						]
					}
				}
			]
		}

		with patch("ism.board.ai_providers.requests.post", return_value=mock_resp) as mock_post:
			result = gemini.analyze_capture(
				context={"ro_name": "RO-TEST-1", "recent_messages": []},
				text="Front left upper control arm bushing cracked",
				media_blobs=[
					{"content_type": "image/png", "data": _TINY_PNG},
					{"content_type": "audio/wav", "data": _TINY_WAV},
				],
			)

		self.assertEqual(len(result["action_items"]), 1)
		self.assertEqual(result["action_items"][0]["clearing_condition_type"], "ro_has_supplement_or_service")
		self.assertIsNone(result["followup_question"])

		called_url = mock_post.call_args.args[0]
		called_headers = mock_post.call_args.kwargs["headers"]
		called_body = mock_post.call_args.kwargs["json"]
		self.assertEqual(called_url, f"{DEFAULT_GEMINI_ENDPOINT}/models/gemini-3.6-flash:generateContent")
		self.assertEqual(mock_post.call_args.kwargs["timeout"], DEFAULT_HTTP_TIMEOUT_SECONDS)
		self.assertEqual(called_headers["x-goog-api-key"], "test-gemini-secret-key")
		self.assertEqual(called_body["generationConfig"]["responseMimeType"], "application/json")
		self.assertIn("responseSchema", called_body["generationConfig"])
		self.assertEqual(
			called_body["generationConfig"]["thinkingConfig"],
			{"thinkingLevel": "LOW"},
		)
		parts = called_body["contents"][0]["parts"]
		self.assertEqual(len(parts), 3)
		self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/png")
		self.assertEqual(parts[1]["inline_data"]["data"], base64.b64encode(_TINY_PNG).decode("ascii"))
		self.assertEqual(parts[2]["inline_data"]["mime_type"], "audio/wav")

		# OpenAICompatibleProvider mocked HTTPS via factory (reads board_ai_model)
		oa_settings = SimpleNamespace(
			board_ai_provider="OpenAI Compatible",
			board_ai_api_key="test-openai-key",
			board_ai_endpoint=DEFAULT_OPENAI_ENDPOINT,
			board_ai_model="gpt-4o-mini",
		)
		openai_provider = get_ai_provider(settings=oa_settings)
		self.assertIsInstance(openai_provider, OpenAICompatibleProvider)

		oa_resp = MagicMock()
		oa_resp.raise_for_status.return_value = None
		oa_resp.json.return_value = {"choices": [{"message": {"content": json.dumps(ai_payload)}}]}
		with patch("ism.board.ai_providers.requests.post", return_value=oa_resp) as mock_oa_post:
			oa_result = openai_provider.analyze_capture(
				context={"ro_name": "RO-TEST-1", "recent_messages": []},
				text="Front left upper control arm bushing cracked",
				media_blobs=[
					{"content_type": "image/png", "data": _TINY_PNG},
					{"content_type": "audio/wav", "data": _TINY_WAV},
				],
			)
		self.assertEqual(len(oa_result["action_items"]), 1)
		self.assertEqual(mock_oa_post.call_args.args[0], f"{DEFAULT_OPENAI_ENDPOINT}/chat/completions")
		self.assertEqual(mock_oa_post.call_args.kwargs["headers"]["Authorization"], "Bearer test-openai-key")
		self.assertEqual(mock_oa_post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})

	def test_provider_failure_sets_failed_status_with_scrubbed_error(self):
		ro = self._create_ro(status="Authorized - In Progress", approval_status="Approved")
		thread_key = ro_thread_key(ro.name)
		ev = self._insert_capture_event(thread_key, body_text="Front right strut leaking oil")

		failing_provider = MagicMock()
		failing_provider.analyze_capture.side_effect = RuntimeError(
			"Upstream 500 error with Bearer sk-secret-token-99999"
		)
		with patch("ism.board.orchestrator.get_ai_provider", return_value=failing_provider):
			process_board_event(ev.name)
		self._track_ro_board_artifacts(ro.name)

		ev.reload()
		self.assertEqual(ev.ai_status, "FAILED")
		self.assertTrue(bool(ev.ai_error))
		self.assertNotIn("sk-secret-token-99999", ev.ai_error or "")
