# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Optional Capability Pack step: Customer Portal & Vehicle Access Vault."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_PASS,
	CHECK_WARN,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_WARNING,
	TIER_OPTIONAL_PACK,
	CheckResult,
	SetupContext,
	StepInspection,
	StepMeta,
	StepVerifyResult,
)
from ism.setup_wizard.settings_helper import SettingsSectionStep, _is_password_field_set

_PIN_KEY_ROTATION_WARNING = (
	"Never casually rotate portal_pin_encryption_key: it encrypts both PIN-to-Drive "
	"and Tesla Phone Key vaults, and rotating it invalidates existing encrypted vault records."
)


def _is_valid_invoice_print_format(print_format: str) -> bool:
	cleaned = (print_format or "").strip()
	if not cleaned or cleaned == "Standard":
		return True
	return bool(
		frappe.db.exists(
			"Print Format",
			{"name": cleaned, "doc_type": "Sales Invoice"},
		)
	)


class CustomerPortalStep(SettingsSectionStep):
	"""Configure Customer Portal branding, disclaimers, invoice print format, and vault encryption."""

	meta = StepMeta(
		id="customer_portal",
		title="Customer Portal & Vehicle Vault",
		phase="integrations",
		order=120,
		schema_version=1,
		tier=TIER_OPTIONAL_PACK,
		depends_on=("site_environment", "erpnext_foundation"),
		owner_domain="portal",
		managed_settings_sections=("sb_customer_portal",),
		allow_skip=True,
		description=(
			"Configure the hosted /portal customer experience, branding, disclaimers, "
			"Sales Invoice print format, and verify the Fernet vault encryption key."
		),
	)

	def _desk_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"ism_settings": f"{ctx.site_url}/app/ism-settings",
			"portal": f"{ctx.site_url}/portal",
		}

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		schema = super().get_schema(ctx)
		schema["desk_urls"] = self._desk_urls(ctx)
		return schema

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "ontario_ev_portal",
				"label": "Ontario EV Shop Portal Defaults",
				"description": "Enables Customer Portal with standard disclaimers and Standard invoice print format.",
				"payload": {
					"settings": {
						"enable_customer_portal": 1,
						"portal_invoice_print_format": "Standard",
						"portal_booking_token_ttl_days": 30,
						"portal_pin_prompt_disclaimer": (
							"Your PIN-to-Drive is encrypted at rest and only accessible to assigned "
							"shop staff while your vehicle is in service."
						),
						"portal_phone_key_disclaimer": (
							"Temporary Tesla Phone Key credentials are encrypted at rest and revoked "
							"when your Repair Order is completed."
						),
					}
				},
			}
		]

	def inspect(self, ctx: SetupContext) -> StepInspection:
		settings = frappe.get_single("ISM Settings")
		enabled = bool(cint(settings.get("enable_customer_portal")))
		brand_name = str(settings.get("portal_brand_name") or "").strip()
		support_phone = str(settings.get("portal_support_phone") or "").strip()
		pickup_hours = str(settings.get("portal_pickup_hours_text") or "").strip()
		pin_key_set = _is_password_field_set(settings, "portal_pin_encryption_key")
		pin_disclaimer = str(settings.get("portal_pin_prompt_disclaimer") or "").strip()
		phone_key_disclaimer = str(settings.get("portal_phone_key_disclaimer") or "").strip()
		print_format = str(settings.get("portal_invoice_print_format") or "Standard").strip() or "Standard"

		checks: list[CheckResult] = []

		if enabled:
			checks.append(
				CheckResult(
					code="portal_enabled",
					level=CHECK_PASS,
					message="Customer Portal is enabled.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="portal_enabled",
					level=CHECK_FAIL,
					message="Customer Portal (enable_customer_portal) is disabled.",
					remediation="Enable 'enable_customer_portal' in ISM Settings (or skip this optional pack).",
				)
			)

		missing_branding = [
			label
			for label, val in (
				("portal_brand_name", brand_name),
				("portal_support_phone", support_phone),
				("portal_pickup_hours_text", pickup_hours),
			)
			if not val
		]
		if not missing_branding:
			checks.append(
				CheckResult(
					code="portal_branding",
					level=CHECK_PASS,
					message="Portal brand name, support phone, and pickup hours are configured.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="portal_branding",
					level=CHECK_WARN,
					message=f"Customer Portal branding field(s) missing: {', '.join(missing_branding)}.",
					remediation=f"Set {', '.join(missing_branding)} in ISM Settings.",
				)
			)

		if pin_key_set:
			checks.append(
				CheckResult(
					code="portal_pin_encryption_key",
					level=CHECK_PASS,
					message=f"portal_pin_encryption_key is set. {_PIN_KEY_ROTATION_WARNING}",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="portal_pin_encryption_key",
					level=CHECK_WARN,
					message=(
						"portal_pin_encryption_key is not yet initialized (will be lazily generated on "
						f"first vault use or verify). {_PIN_KEY_ROTATION_WARNING}"
					),
					remediation="Run verify on customer_portal to initialize and validate the Fernet key.",
				)
			)

		missing_disclaimers = [
			label
			for label, val in (
				("portal_pin_prompt_disclaimer", pin_disclaimer),
				("portal_phone_key_disclaimer", phone_key_disclaimer),
			)
			if not val
		]
		if not missing_disclaimers:
			checks.append(
				CheckResult(
					code="portal_disclaimers",
					level=CHECK_PASS,
					message="PIN-to-Drive and Tesla Phone Key disclaimers are configured.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="portal_disclaimers",
					level=CHECK_WARN,
					message=f"Portal vault disclaimer(s) missing: {', '.join(missing_disclaimers)}.",
					remediation=f"Configure {', '.join(missing_disclaimers)} in ISM Settings.",
				)
			)

		if _is_valid_invoice_print_format(print_format):
			checks.append(
				CheckResult(
					code="portal_invoice_print_format",
					level=CHECK_PASS,
					message=f"Portal invoice print format '{print_format}' is valid for Sales Invoice.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="portal_invoice_print_format",
					level=CHECK_FAIL,
					message=(
						f"Portal invoice print format '{print_format}' does not exist for Sales Invoice."
					),
					remediation=(
						"Set portal_invoice_print_format to 'Standard' or an existing Sales Invoice Print Format."
					),
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)
		if not enabled or has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = (
				"Customer Portal is disabled."
				if not enabled
				else "Customer Portal configuration has blocking issues."
			)
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = "Customer Portal is enabled with optional branding/disclaimer warnings."
		else:
			status = STEP_STATUS_COMPLETE
			summary = "Customer Portal and vehicle vault settings are configured."

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"enabled": enabled,
				"pin_encryption_key_set": pin_key_set,
				"invoice_print_format": print_format,
			},
			desk_urls=self._desk_urls(ctx),
		)

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		settings = frappe.get_single("ISM Settings")
		enabled = bool(cint(settings.get("enable_customer_portal")))
		print_format = str(settings.get("portal_invoice_print_format") or "Standard").strip() or "Standard"

		checks: list[CheckResult] = []

		if not enabled:
			checks.append(
				CheckResult(
					code="portal_enabled",
					level=CHECK_FAIL,
					message="Customer Portal is disabled.",
					remediation="Enable 'enable_customer_portal' in ISM Settings.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="portal_enabled",
					level=CHECK_PASS,
					message="Customer Portal is enabled.",
				)
			)

		try:
			from ism.portal.pin_vault import _get_pin_fernet

			fernet = _get_pin_fernet()
			if fernet is None:
				raise ValueError("Fernet vault cipher could not be initialized.")
		except Exception:
			checks.append(
				CheckResult(
					code="portal_fernet_key_valid",
					level=CHECK_FAIL,
					message="Failed to load a valid Fernet key from portal_pin_encryption_key.",
					remediation=(
						"Verify portal_pin_encryption_key is a valid URL-safe base64 32-byte Fernet key. "
						f"{_PIN_KEY_ROTATION_WARNING}"
					),
				)
			)
		else:
			checks.append(
				CheckResult(
					code="portal_fernet_key_valid",
					level=CHECK_PASS,
					message="portal_pin_encryption_key loaded a valid Fernet cipher.",
				)
			)

		if _is_valid_invoice_print_format(print_format):
			checks.append(
				CheckResult(
					code="portal_invoice_print_format",
					level=CHECK_PASS,
					message=f"Portal invoice print format '{print_format}' is valid for Sales Invoice.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="portal_invoice_print_format",
					level=CHECK_FAIL,
					message=(
						f"Portal invoice print format '{print_format}' does not exist for Sales Invoice."
					),
					remediation=(
						"Set portal_invoice_print_format to 'Standard' or an existing Sales Invoice Print Format."
					),
				)
			)

		passed = not any(c.level == CHECK_FAIL for c in checks)
		summary = (
			"Customer Portal verification passed (Fernet vault key and invoice print format valid)."
			if passed
			else "Customer Portal verification failed."
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=summary,
			checks=tuple(checks),
		)
