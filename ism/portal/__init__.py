# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Public facade exports for Project 4 Customer Portal."""

from ism.portal.auth import create_booking_portal_link
from ism.portal.contracts import (
	DEFAULT_PIN_PROMPT_DISCLAIMER,
	PIN_AUTO_MASK_SECONDS,
	PIN_STAFF_ALLOWED_ROLES,
	PIN_STATUS_NOT_PROMPTED,
	PIN_STATUS_OPTED_OUT,
	PIN_STATUS_PROVIDED,
	PIN_STATUS_PURGED,
	PIN_VALID_STATUSES,
	PORTAL_BOOKING_TOKEN_DEFAULT_TTL_DAYS,
	PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS,
	PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS,
	PORTAL_DEFAULT_INVOICE_PRINT_FORMAT,
	PORTAL_FORBIDDEN_PAYLOAD_KEYS,
	PORTAL_STATUS_BEING_WORKED_ON,
	PORTAL_STATUS_CANCELLED,
	PORTAL_STATUS_LABELS,
	PORTAL_STATUS_READY_FOR_PICKUP,
	PORTAL_STATUS_SCHEDULED,
	RO_CLOSED_LOCKED_STATUSES,
	RO_PIN_MUTATION_LOCKED_STATUSES,
	RO_PIN_PURGE_STATUSES,
	find_forbidden_portal_keys,
	hash_portal_token,
	mask_email_address,
	validate_pin_format,
)
from ism.portal.invoices import (
	email_sales_invoice_copy,
	get_ro_invoice_summary,
	render_sales_invoice_pdf,
)
from ism.portal.pin_vault import (
	get_ro_pin_for_staff,
	get_ro_pin_state,
	opt_out_ro_pin,
	purge_ro_pin,
	submit_ro_pin,
)
from ism.portal.visit_view import (
	build_portal_hub_payload,
	derive_binary_visit_status,
)

__all__ = [
	"DEFAULT_PIN_PROMPT_DISCLAIMER",
	"PIN_AUTO_MASK_SECONDS",
	"PIN_STAFF_ALLOWED_ROLES",
	"PIN_STATUS_NOT_PROMPTED",
	"PIN_STATUS_OPTED_OUT",
	"PIN_STATUS_PROVIDED",
	"PIN_STATUS_PURGED",
	"PIN_VALID_STATUSES",
	"PORTAL_BOOKING_TOKEN_DEFAULT_TTL_DAYS",
	"PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS",
	"PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS",
	"PORTAL_DEFAULT_INVOICE_PRINT_FORMAT",
	"PORTAL_FORBIDDEN_PAYLOAD_KEYS",
	"PORTAL_STATUS_BEING_WORKED_ON",
	"PORTAL_STATUS_CANCELLED",
	"PORTAL_STATUS_LABELS",
	"PORTAL_STATUS_READY_FOR_PICKUP",
	"PORTAL_STATUS_SCHEDULED",
	"RO_CLOSED_LOCKED_STATUSES",
	"RO_PIN_MUTATION_LOCKED_STATUSES",
	"RO_PIN_PURGE_STATUSES",
	"build_portal_hub_payload",
	"create_booking_portal_link",
	"derive_binary_visit_status",
	"email_sales_invoice_copy",
	"find_forbidden_portal_keys",
	"get_ro_invoice_summary",
	"get_ro_pin_for_staff",
	"get_ro_pin_state",
	"hash_portal_token",
	"mask_email_address",
	"opt_out_ro_pin",
	"purge_ro_pin",
	"render_sales_invoice_pdf",
	"submit_ro_pin",
	"validate_pin_format",
]
