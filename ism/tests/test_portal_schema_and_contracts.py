# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 1 tests for Project 4 Customer Portal frozen contracts, schema, seeding, and hooks."""

from __future__ import annotations

import hashlib

import frappe
from cryptography.fernet import Fernet

import ism.hooks as ism_hooks
import ism.portal as portal_pkg
from ism.portal import (
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
from ism.portal.pin_vault import on_repair_order_update_purge_pin, purge_stale_closed_ro_pins
from ism.setup_tasks.seed import setup_default_ism_settings
from ism.tests.base import ISMTestCase


class TestPortalSchemaAndContracts(ISMTestCase):
	"""Verify Wave 1 frozen portal contracts, disjoint DocType schemas, Fernet seeding, and hooks."""

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()
		super().tearDown()

	def test_frozen_constants_and_package_exports(self):
		self.assertEqual(
			PORTAL_STATUS_LABELS,
			{
				PORTAL_STATUS_SCHEDULED: "Scheduled",
				PORTAL_STATUS_BEING_WORKED_ON: "Being Worked On",
				PORTAL_STATUS_READY_FOR_PICKUP: "Done & Ready for Pickup",
				PORTAL_STATUS_CANCELLED: "Cancelled",
			},
		)
		self.assertEqual(
			PIN_VALID_STATUSES,
			(
				PIN_STATUS_NOT_PROMPTED,
				PIN_STATUS_PROVIDED,
				PIN_STATUS_OPTED_OUT,
				PIN_STATUS_PURGED,
			),
		)
		self.assertEqual(
			PIN_STAFF_ALLOWED_ROLES,
			frozenset({"ISM Technician", "ISM Service Advisor", "ISM Manager", "System Manager"}),
		)
		self.assertEqual(PIN_AUTO_MASK_SECONDS, 15)
		self.assertEqual(RO_CLOSED_LOCKED_STATUSES, frozenset({"Closed / Released", "Cancelled"}))
		self.assertEqual(RO_PIN_PURGE_STATUSES, frozenset({"Closed / Released", "Cancelled"}))
		self.assertEqual(
			RO_PIN_MUTATION_LOCKED_STATUSES,
			frozenset({"Work Completed", "Billed / Paid", "Closed / Released", "Cancelled"}),
		)
		for expected_key in (
			"eta",
			"progress_percent",
			"service_bay",
			"assigned_technician",
			"internal_start",
			"arrival_offset_minutes",
			"buffer_minutes",
			"signature_image",
			"pin_to_drive_encrypted",
			"approval_token",
			"portal_booking_token_hash",
			"cookie_value",
		):
			self.assertIn(expected_key, PORTAL_FORBIDDEN_PAYLOAD_KEYS)

		for symbol in (
			"PORTAL_STATUS_SCHEDULED",
			"PORTAL_STATUS_LABELS",
			"PIN_VALID_STATUSES",
			"PIN_STAFF_ALLOWED_ROLES",
			"PORTAL_FORBIDDEN_PAYLOAD_KEYS",
			"validate_pin_format",
			"mask_email_address",
			"hash_portal_token",
			"find_forbidden_portal_keys",
		):
			self.assertIn(symbol, portal_pkg.__all__)

	def test_validate_pin_format(self):
		self.assertEqual(validate_pin_format("0042"), "0042")
		self.assertEqual(validate_pin_format("0000"), "0000")
		self.assertEqual(validate_pin_format(" 0427 "), "0427")

		for invalid in ("123", "12345", "12a4", "abcd", "12-34", "", "   ", 427, 1234, None, True, "٠١٢٣"):
			with self.assertRaises(frappe.ValidationError, msg=f"Should reject {invalid!r}"):
				validate_pin_format(invalid)

	def test_mask_email_address(self):
		self.assertEqual(mask_email_address("driver@example.com"), "d***r@example.com")
		self.assertEqual(mask_email_address("  ab@example.com  "), "a***@example.com")
		self.assertEqual(mask_email_address("x@example.com"), "x***@example.com")
		self.assertIsNone(mask_email_address(None))
		self.assertIsNone(mask_email_address(""))
		self.assertIsNone(mask_email_address("not-an-email"))

	def test_hash_portal_token(self):
		raw = "  tok_abc123_secret  "
		expected = hashlib.sha256(b"tok_abc123_secret").hexdigest()
		self.assertEqual(hash_portal_token(raw), expected)
		self.assertEqual(len(hash_portal_token(raw)), 64)

		for invalid in ("", "   ", None, 12345):
			with self.assertRaises(frappe.ValidationError):
				hash_portal_token(invalid)  # type: ignore[arg-type]

	def test_find_forbidden_portal_keys_recursive(self):
		clean_payload = {
			"visit_status": PORTAL_STATUS_BEING_WORKED_ON,
			"visit_status_label": PORTAL_STATUS_LABELS[PORTAL_STATUS_BEING_WORKED_ON],
			"services": [{"description": "Brake Flush", "status": "Approved"}],
		}
		self.assertEqual(find_forbidden_portal_keys(clean_payload), [])

		dirty_payload = {
			"visit_status": PORTAL_STATUS_BEING_WORKED_ON,
			"eta": "2026-11-21 15:00:00",
			"nested": {
				"progress_percent": 50,
				"items": [
					{"service_bay": "Bay 1", "internal_start": "08:00:00"},
					{"pin_to_drive_encrypted": "gAAAA..."},
				],
			},
		}
		found = find_forbidden_portal_keys(dirty_payload)
		self.assertEqual(
			found,
			[
				"eta",
				"nested.progress_percent",
				"nested.items[0].service_bay",
				"nested.items[0].internal_start",
				"nested.items[1].pin_to_drive_encrypted",
			],
		)

	def test_ism_settings_customer_portal_schema_and_seeding(self):
		meta = frappe.get_meta("ISM Settings")
		expected_fields = {
			"sb_customer_portal": "Section Break",
			"enable_customer_portal": "Check",
			"portal_brand_name": "Data",
			"portal_support_phone": "Data",
			"portal_pickup_hours_text": "Small Text",
			"cb_customer_portal": "Column Break",
			"portal_booking_token_ttl_days": "Int",
			"portal_invoice_print_format": "Data",
			"portal_pin_encryption_key": "Password",
			"portal_pin_prompt_disclaimer": "Small Text",
		}
		for fieldname, fieldtype in expected_fields.items():
			df = meta.get_field(fieldname)
			self.assertIsNotNone(df, f"ISM Settings missing field {fieldname}")
			self.assertEqual(df.fieldtype, fieldtype)

		pin_key_df = meta.get_field("portal_pin_encryption_key")
		self.assertEqual(int(pin_key_df.read_only or 0), 1)
		self.assertEqual(int(pin_key_df.no_copy or 0), 1)

		setup_default_ism_settings()
		settings = frappe.get_single("ISM Settings")
		self.assertEqual(int(settings.enable_customer_portal), 1)
		self.assertEqual(int(settings.portal_booking_token_ttl_days), PORTAL_BOOKING_TOKEN_DEFAULT_TTL_DAYS)
		self.assertEqual(settings.portal_invoice_print_format, PORTAL_DEFAULT_INVOICE_PRINT_FORMAT)
		self.assertTrue(settings.portal_pin_prompt_disclaimer)
		self.assertEqual(settings.portal_pin_prompt_disclaimer, DEFAULT_PIN_PROMPT_DISCLAIMER)

		fernet_key = settings.get_password("portal_pin_encryption_key", raise_exception=False)
		self.assertTrue(fernet_key, "portal_pin_encryption_key must be auto-seeded")
		cipher = Fernet(fernet_key.encode("ascii"))
		token = cipher.encrypt(b"0427")
		self.assertEqual(cipher.decrypt(token), b"0427")

		# Idempotency: re-running setup_default_ism_settings must preserve explicit 0 and existing Fernet key
		frappe.db.set_single_value("ISM Settings", "enable_customer_portal", 0)
		frappe.clear_cache(doctype="ISM Settings")
		setup_default_ism_settings()
		reloaded = frappe.get_single("ISM Settings")
		self.assertEqual(int(reloaded.enable_customer_portal), 0)
		self.assertEqual(
			reloaded.get_password("portal_pin_encryption_key", raise_exception=False),
			fernet_key,
		)

	def test_ism_settings_portal_booking_token_ttl_validation(self):
		for invalid_ttl in (0, -5, PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS + 1):
			settings = frappe.new_doc("ISM Settings")
			settings.portal_booking_token_ttl_days = invalid_ttl
			with self.assertRaises(frappe.ValidationError, msg=f"Should reject TTL {invalid_ttl}"):
				settings.validate()

		for valid_ttl in (
			PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS,
			PORTAL_BOOKING_TOKEN_DEFAULT_TTL_DAYS,
			PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS,
		):
			settings = frappe.new_doc("ISM Settings")
			settings.portal_booking_token_ttl_days = valid_ttl
			settings.validate()

	def test_repair_order_pin_to_drive_schema(self):
		meta = frappe.get_meta("Repair Order")
		field_names = [df.fieldname for df in meta.fields]
		self.assertLess(field_names.index("tab_workshop"), field_names.index("sec_pin_to_drive"))
		self.assertLess(field_names.index("sec_bay"), field_names.index("sec_pin_to_drive"))
		self.assertLess(field_names.index("sec_pin_to_drive"), field_names.index("sec_schedule_allocations"))
		self.assertLess(field_names.index("sec_pin_to_drive"), field_names.index("tab_billing"))
		# Preserve P2 sec_deposit_request
		self.assertIn("sec_deposit_request", field_names)

		expected = {
			"sec_pin_to_drive": "Section Break",
			"pin_to_drive_status": "Select",
			"pin_to_drive_encrypted": "Password",
			"pin_to_drive_submitted_at": "Datetime",
			"pin_to_drive_updated_at": "Datetime",
			"cb_pin_to_drive": "Column Break",
			"pin_to_drive_opted_out_at": "Datetime",
			"pin_to_drive_purged_at": "Datetime",
			"pin_to_drive_last_viewed_by": "Link",
			"pin_to_drive_last_viewed_at": "Datetime",
			"pin_to_drive_view_count": "Int",
		}
		for fieldname, fieldtype in expected.items():
			df = meta.get_field(fieldname)
			self.assertIsNotNone(df, f"Repair Order missing field {fieldname}")
			self.assertEqual(df.fieldtype, fieldtype)

		status_df = meta.get_field("pin_to_drive_status")
		self.assertEqual(status_df.default, PIN_STATUS_NOT_PROMPTED)
		self.assertEqual(tuple((status_df.options or "").split("\n")), PIN_VALID_STATUSES)
		self.assertEqual(int(status_df.read_only or 0), 1)
		self.assertEqual(int(status_df.allow_on_submit or 0), 1)
		self.assertEqual(int(status_df.in_standard_filter or 0), 1)

		enc_df = meta.get_field("pin_to_drive_encrypted")
		self.assertEqual(enc_df.fieldtype, "Password")
		self.assertEqual(int(enc_df.hidden or 0), 1)
		self.assertEqual(int(enc_df.read_only or 0), 1)
		self.assertEqual(int(enc_df.no_copy or 0), 1)
		self.assertEqual(int(enc_df.print_hide or 0), 1)
		self.assertEqual(int(enc_df.report_hide or 0), 1)
		self.assertEqual(int(enc_df.allow_on_submit or 0), 1)

		viewed_by_df = meta.get_field("pin_to_drive_last_viewed_by")
		self.assertEqual(viewed_by_df.options, "User")

	def test_schedule_entry_portal_link_schema(self):
		meta = frappe.get_meta("Schedule Entry")
		field_names = [df.fieldname for df in meta.fields]
		self.assertIn("section_break_self_serve", field_names)
		self.assertLess(
			field_names.index("section_break_self_serve"),
			field_names.index("section_break_portal_link"),
		)
		self.assertLess(
			field_names.index("section_break_portal_link"),
			field_names.index("section_break_notes"),
		)

		hash_df = meta.get_field("portal_booking_token_hash")
		self.assertIsNotNone(hash_df)
		self.assertEqual(hash_df.fieldtype, "Data")
		self.assertEqual(int(hash_df.length or 0), 64)
		self.assertEqual(int(hash_df.hidden or 0), 1)
		self.assertEqual(int(hash_df.read_only or 0), 1)
		self.assertEqual(int(hash_df.no_copy or 0), 1)
		self.assertEqual(int(hash_df.search_index or 0), 1)

		exp_df = meta.get_field("portal_booking_token_expires_at")
		self.assertIsNotNone(exp_df)
		self.assertEqual(exp_df.fieldtype, "Datetime")
		self.assertEqual(int(exp_df.read_only or 0), 1)
		self.assertEqual(int(exp_df.no_copy or 0), 1)

	def test_hooks_registration_and_placeholder_handlers(self):
		routes = {(r["from_route"], r["to_route"]) for r in ism_hooks.website_route_rules}
		self.assertIn(("/portal", "portal"), routes)
		self.assertIn(("/quote-approval", "quote_approval"), routes)
		self.assertIn(("/board", "board"), routes)
		self.assertIn(("/cockpit", "cockpit"), routes)

		ro_on_update = ism_hooks.doc_events["Repair Order"]["on_update"]
		self.assertIsInstance(ro_on_update, list)
		self.assertIn("ism.board.projections.on_repair_order_update", ro_on_update)
		self.assertIn("ism.portal.pin_vault.on_repair_order_update_purge_pin", ro_on_update)

		daily_jobs = ism_hooks.scheduler_events["daily"]
		self.assertIn("ism.comms.otp.cleanup_expired_otp_challenges", daily_jobs)
		self.assertIn("ism.comms.sessions.cleanup_expired_portal_sessions", daily_jobs)
		self.assertIn("ism.portal.pin_vault.purge_stale_closed_ro_pins", daily_jobs)

		# Placeholder handlers are callable safe no-ops in Wave 1
		self.assertIsNone(on_repair_order_update_purge_pin(None))
		self.assertIsNone(purge_stale_closed_ro_pins())
