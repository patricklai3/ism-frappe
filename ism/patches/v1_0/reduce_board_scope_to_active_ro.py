# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Idempotent post_model_sync migration reducing Shop Board scope to active ROs.

1. Copies legacy ``ISM Settings.board_ai_orchestrator_model`` -> ``board_ai_model`` if unset.
2. Deletes legacy non-REPAIR_ORDER ``Shop Board Event`` and ``Shop Board Thread`` rows.
3. Normalizes ``BLOCKED_DECISION`` -> ``NEEDS_ACTION`` on ``Shop Board Thread``.
4. Normalizes legacy ``Shop Board Event`` payload types and resolution states.
"""

from __future__ import annotations

import frappe

_LEGACY_PAYLOAD_TYPES = (
	"CLARIFICATION_CARD",
	"APPROVAL_GATE",
	"AI_RECEIPT",
	"CARD_RESOLUTION",
)


def _migrate_board_ai_model() -> None:
	if not frappe.db.exists("DocType", "ISM Settings"):
		return
	old_rows = frappe.db.sql(
		"""
		SELECT `value`
		FROM `tabSingles`
		WHERE `doctype` = 'ISM Settings' AND `field` = 'board_ai_orchestrator_model'
		LIMIT 1
		"""
	)
	old_val = (old_rows[0][0] or "").strip() if old_rows and old_rows[0] else ""
	if not old_val:
		return

	cur_rows = frappe.db.sql(
		"""
		SELECT `value`
		FROM `tabSingles`
		WHERE `doctype` = 'ISM Settings' AND `field` = 'board_ai_model'
		LIMIT 1
		"""
	)
	cur_val = (cur_rows[0][0] or "").strip() if cur_rows and cur_rows[0] else ""
	if not cur_val:
		frappe.db.set_single_value("ISM Settings", "board_ai_model", old_val)


def _prune_legacy_non_ro_threads_and_events() -> None:
	has_threads = frappe.db.table_exists("Shop Board Thread")
	has_events = frappe.db.table_exists("Shop Board Event")
	if not has_threads and not has_events:
		return

	legacy_threads: list[str] = []
	if has_threads:
		legacy_threads = frappe.db.sql_list(
			"""
			SELECT `name`
			FROM `tabShop Board Thread`
			WHERE IFNULL(`thread_type`, '') != 'REPAIR_ORDER'
			   OR IFNULL(`repair_order`, '') = ''
			"""
		)

	if has_events:
		if legacy_threads:
			legacy_events = frappe.db.sql_list(
				"""
				SELECT `name`
				FROM `tabShop Board Event`
				WHERE IFNULL(`thread_type`, '') != 'REPAIR_ORDER'
				   OR `thread` IN %s
				""",
				(tuple(legacy_threads),),
			)
		else:
			legacy_events = frappe.db.sql_list(
				"""
				SELECT `name`
				FROM `tabShop Board Event`
				WHERE IFNULL(`thread_type`, '') != 'REPAIR_ORDER'
				"""
			)

		if legacy_events:
			if (
				frappe.db.table_exists("Shop Action Item")
				and frappe.db.has_column("Shop Action Item", "board_event")
			):
				frappe.db.sql(
					"""
					UPDATE `tabShop Action Item`
					SET `board_event` = NULL
					WHERE `board_event` IN %s
					""",
					(tuple(legacy_events),),
				)

			frappe.db.sql(
				"""
				UPDATE `tabShop Board Event`
				SET `parent_event` = NULL
				WHERE `parent_event` IN %s
				""",
				(tuple(legacy_events),),
			)
			frappe.db.sql(
				"""
				DELETE FROM `tabShop Board Event`
				WHERE `name` IN %s
				""",
				(tuple(legacy_events),),
			)

	if has_threads and legacy_threads:
		frappe.db.sql(
			"""
			DELETE FROM `tabShop Board Thread`
			WHERE `name` IN %s
			""",
			(tuple(legacy_threads),),
		)


def _normalize_thread_statuses() -> None:
	if not frappe.db.table_exists("Shop Board Thread"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabShop Board Thread`
		SET `board_status` = 'NEEDS_ACTION'
		WHERE `board_status` = 'BLOCKED_DECISION'
		"""
	)


def _normalize_event_payloads_and_states() -> None:
	if not frappe.db.table_exists("Shop Board Event"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabShop Board Event`
		SET `payload_type` = 'STATE_MUTATION',
		    `resolution_state` = CASE
		        WHEN `resolution_state` IN ('PENDING', 'EXECUTING', 'EXPIRED') THEN 'NONE'
		        WHEN `resolution_state` = 'UNDONE' THEN 'RESOLVED'
		        ELSE `resolution_state`
		    END
		WHERE `payload_type` IN %s
		""",
		(_LEGACY_PAYLOAD_TYPES,),
	)
	frappe.db.sql(
		"""
		UPDATE `tabShop Board Event`
		SET `resolution_state` = 'RESOLVED'
		WHERE `resolution_state` = 'UNDONE'
		"""
	)
	frappe.db.sql(
		"""
		UPDATE `tabShop Board Event`
		SET `resolution_state` = 'NONE'
		WHERE `resolution_state` IN ('EXECUTING', 'EXPIRED')
		"""
	)


def execute() -> None:
	_migrate_board_ai_model()
	_prune_legacy_non_ro_threads_and_events()
	_normalize_thread_statuses()
	_normalize_event_payloads_and_states()
