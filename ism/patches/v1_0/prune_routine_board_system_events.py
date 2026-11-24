# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Idempotent post_model_sync migration pruning routine SYSTEM STATE_MUTATION board events.

1. Deletes routine ``SYSTEM`` ``STATE_MUTATION`` ``Shop Board Event`` rows
   (``idempotency_key LIKE 'action_item:%'`` OR ``intent_tag = 'shop_action_item'``),
   safely nullifying ``Shop Action Item.board_event`` and ``Shop Board Event.parent_event`` pointers first.
2. Recomputes ``last_event_at``, ``last_event_preview``, ``open_action_count``, and
   ``projection_version`` on all affected ``Shop Board Thread`` rows.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from ism.board.projections import recompute_thread_projection


def _fallback_recompute_thread(thread_key: str) -> None:
	"""Direct SQL projection recompute fallback if a thread's Repair Order no longer exists."""
	latest_events = frappe.get_all(
		"Shop Board Event",
		filters={
			"thread": thread_key,
			"payload_type": ["!=", "STATE_MUTATION"],
		},
		fields=["event_timestamp", "body_text", "transcript", "payload_type"],
		order_by="event_timestamp desc, creation desc",
		limit=1,
	)
	last_event_at = None
	last_event_preview = ""
	if latest_events:
		latest = latest_events[0]
		last_event_at = latest.get("event_timestamp")
		preview_src = latest.get("body_text") or latest.get("transcript") or latest.get("payload_type") or ""
		cleaned = " ".join(str(preview_src).split())
		last_event_preview = cleaned if len(cleaned) <= 140 else (cleaned[:137].rstrip() + "...")

	open_count = frappe.db.count(
		"Shop Board Event",
		filters={
			"thread": thread_key,
			"payload_type": "ACTION_CARD",
			"resolution_state": "PENDING",
		},
	)
	cur_version = cint(frappe.db.get_value("Shop Board Thread", thread_key, "projection_version") or 0)
	frappe.db.set_value(
		"Shop Board Thread",
		thread_key,
		{
			"last_event_at": last_event_at,
			"last_event_preview": last_event_preview,
			"open_action_count": open_count,
			"projection_version": cur_version + 1,
		},
		update_modified=True,
	)


def execute() -> None:
	if not frappe.db.table_exists("Shop Board Event") or not frappe.db.table_exists("Shop Board Thread"):
		return

	rows = frappe.db.sql(
		"""
		SELECT `name`, `thread`
		FROM `tabShop Board Event`
		WHERE `actor_type` = 'SYSTEM'
		  AND `payload_type` = 'STATE_MUTATION'
		  AND (
			  IFNULL(`idempotency_key`, '') LIKE %(prefix)s
			  OR IFNULL(`intent_tag`, '') = 'shop_action_item'
		  )
		""",
		{"prefix": "action_item:%"},
		as_dict=True,
	)
	if not rows:
		return

	event_names = tuple(row["name"] for row in rows if row.get("name"))
	affected_threads = sorted({row["thread"] for row in rows if row.get("thread")})
	if not event_names:
		return

	if frappe.db.table_exists("Shop Action Item") and frappe.db.has_column("Shop Action Item", "board_event"):
		frappe.db.sql(
			"""
			UPDATE `tabShop Action Item`
			SET `board_event` = NULL
			WHERE `board_event` IN %(names)s
			""",
			{"names": event_names},
		)

	frappe.db.sql(
		"""
		UPDATE `tabShop Board Event`
		SET `parent_event` = NULL
		WHERE `parent_event` IN %(names)s
		""",
		{"names": event_names},
	)

	frappe.db.sql(
		"""
		DELETE FROM `tabShop Board Event`
		WHERE `name` IN %(names)s
		""",
		{"names": event_names},
	)

	for thread_key in affected_threads:
		if not frappe.db.exists("Shop Board Thread", thread_key):
			continue
		try:
			recompute_thread_projection(thread_key, publish=False)
		except Exception:
			_fallback_recompute_thread(thread_key)
