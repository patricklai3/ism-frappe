# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Shop OS Blackboard & Cockpit package."""

from ism.board.contracts import (
	ACTOR_TYPES,
	AI_STATUSES,
	ALL_ACTIONS,
	AUTONOMOUS_ACTIONS,
	BOARD_STATUSES,
	CLOSED_RO_STATUSES,
	MANAGER_GATED_ACTIONS,
	PAYLOAD_TYPES,
	PENDING_DECISION_PAYLOAD_TYPES,
	RESOLUTION_STATES,
	SELF_ECHO_DEDUP_PREFIX,
	SINGLETON_THREADS,
	TARGET_ROLES,
	THREAD_TYPES,
	BoardContractError,
	parse_ro_from_thread_key,
	ro_thread_key,
	validate_approval_gate_card,
	validate_clarification_card,
	validate_receipt_card,
)

__all__ = [
	"ACTOR_TYPES",
	"AI_STATUSES",
	"ALL_ACTIONS",
	"AUTONOMOUS_ACTIONS",
	"BOARD_STATUSES",
	"CLOSED_RO_STATUSES",
	"MANAGER_GATED_ACTIONS",
	"PAYLOAD_TYPES",
	"PENDING_DECISION_PAYLOAD_TYPES",
	"RESOLUTION_STATES",
	"SELF_ECHO_DEDUP_PREFIX",
	"SINGLETON_THREADS",
	"TARGET_ROLES",
	"THREAD_TYPES",
	"BoardContractError",
	"parse_ro_from_thread_key",
	"ro_thread_key",
	"validate_approval_gate_card",
	"validate_clarification_card",
	"validate_receipt_card",
]
