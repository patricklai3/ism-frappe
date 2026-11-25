# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Optional Capability Pack step: Outbound SMS & Telnyx Comms."""

from __future__ import annotations

from typing import Any

import frappe

from ism.comms.providers import get_sms_unconfigured_reason
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_INFO,
	CHECK_PASS,
	CHECK_WARN,
	PROFILE_STAGING,
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


class CommsTelnyxSmsStep(SettingsSectionStep):
	"""Configure outbound SMS gateway (Telnyx / Custom Webhook / Mock) and OTP & portal sessions."""

	meta = StepMeta(
		id="comms_telnyx_sms",
		title="Outbound SMS & Telnyx Comms",
		phase="integrations",
		order=110,
		schema_version=1,
		tier=TIER_OPTIONAL_PACK,
		depends_on=("site_environment",),
		owner_domain="comms",
		managed_settings_sections=("sb_sms_settings", "sb_otp_and_sessions"),
		allow_skip=True,
		description=(
			"Configure outbound SMS provider (Telnyx, Custom HTTP Webhook, or Mock), "
			"inbound webhook verification, and OTP / portal session security settings."
		),
	)

	def _webhook_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"telnyx_webhook": f"{ctx.site_url}/api/method/ism.api.comms_webhooks.telnyx_webhook",
		}

	def _desk_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"ism_settings": f"{ctx.site_url}/app/ism-settings",
		}

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		schema = super().get_schema(ctx)
		schema["desk_urls"] = self._desk_urls(ctx)
		schema["webhook_urls"] = self._webhook_urls(ctx)
		return schema

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "mock_staging",
				"label": "Mock SMS (Safe Staging)",
				"description": "Deterministic offline SMS provider for staging and automated testing.",
				"payload": {
					"settings": {
						"sms_gateway_provider": "Mock (Deterministic Test)",
						"sms_default_country_code": "+1",
						"sms_enforce_opt_out": 1,
						"enable_ready_for_pickup_sms": 1,
					}
				},
			},
			{
				"id": "telnyx_production",
				"label": "Telnyx Production Defaults",
				"description": (
					"Standard North American Telnyx settings (pass telnyx_api_key and "
					"portal_session_signing_secret via secrets)."
				),
				"payload": {
					"settings": {
						"sms_gateway_provider": "Telnyx",
						"sms_default_country_code": "+1",
						"sms_enforce_opt_out": 1,
						"enable_ready_for_pickup_sms": 1,
						"portal_session_secure_cookie": 1,
						"portal_session_cookie_samesite": "Lax",
					}
				},
			},
		]

	def inspect(self, ctx: SetupContext) -> StepInspection:
		settings = frappe.get_single("ISM Settings")
		provider = str(settings.get("sms_gateway_provider") or "Disabled").strip()
		unconfigured_reason = get_sms_unconfigured_reason(settings)

		telnyx_api_key_set = _is_password_field_set(settings, "telnyx_api_key")
		hmac_secret_set = _is_password_field_set(settings, "sms_webhook_hmac_secret")
		portal_signing_secret_set = _is_password_field_set(settings, "portal_session_signing_secret")
		telnyx_pub_key_set = bool(str(settings.get("telnyx_webhook_public_key") or "").strip())

		checks: list[CheckResult] = []

		if provider == "Disabled":
			checks.append(
				CheckResult(
					code="sms_provider_disabled",
					level=CHECK_FAIL,
					message="Outbound SMS gateway provider is Disabled.",
					remediation=(
						"Select 'Telnyx', 'Custom HTTP Webhook', or 'Mock (Deterministic Test)' in "
						"ISM Settings (or skip this optional pack if SMS is not needed)."
					),
				)
			)
		elif unconfigured_reason:
			checks.append(
				CheckResult(
					code="sms_provider_config",
					level=CHECK_FAIL,
					message=unconfigured_reason,
					remediation=(
						f"Complete the required credentials/fields for '{provider}' in ISM Settings."
					),
				)
			)
		else:
			checks.append(
				CheckResult(
					code="sms_provider_config",
					level=CHECK_PASS,
					message=f"SMS gateway provider '{provider}' is configured.",
				)
			)

		if ctx.profile == PROFILE_STAGING and provider == "Telnyx":
			checks.append(
				CheckResult(
					code="sms_staging_live_carrier_risk",
					level=CHECK_WARN,
					message=(
						"Telnyx live SMS provider is active in the Staging profile (live carrier risk)."
					),
					remediation=(
						"Switch sms_gateway_provider to 'Mock (Deterministic Test)' in Staging to "
						"avoid sending live carrier SMS messages."
					),
				)
			)

		if provider == "Telnyx":
			checks.append(
				CheckResult(
					code="sms_telnyx_api_key",
					level=CHECK_PASS if telnyx_api_key_set else CHECK_FAIL,
					message=(
						"Telnyx API key is set."
						if telnyx_api_key_set
						else "Telnyx API key is not set."
					),
					remediation=(
						None
						if telnyx_api_key_set
						else "Provide 'telnyx_api_key' via the secrets payload."
					),
				)
			)
			if telnyx_pub_key_set or hmac_secret_set:
				checks.append(
					CheckResult(
						code="sms_webhook_verification",
						level=CHECK_PASS,
						message="Inbound SMS webhook signature verification credential is configured.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="sms_webhook_verification",
						level=CHECK_WARN,
						message=(
							"Neither telnyx_webhook_public_key nor sms_webhook_hmac_secret is configured."
						),
						remediation=(
							"Configure telnyx_webhook_public_key (Ed25519) or sms_webhook_hmac_secret "
							"and point Telnyx webhooks to the webhook URL."
						),
					)
				)
		elif provider == "Custom HTTP Webhook":
			checks.append(
				CheckResult(
					code="sms_webhook_hmac_secret",
					level=CHECK_PASS if hmac_secret_set else CHECK_WARN,
					message=(
						"Custom SMS webhook HMAC secret is set."
						if hmac_secret_set
						else "Custom SMS webhook HMAC secret is not set."
					),
					remediation=(
						None
						if hmac_secret_set
						else "Provide 'sms_webhook_hmac_secret' via the secrets payload to sign inbound webhooks."
					),
				)
			)

		if portal_signing_secret_set:
			checks.append(
				CheckResult(
					code="portal_session_signing_secret",
					level=CHECK_PASS,
					message="Portal session signing secret is set.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="portal_session_signing_secret",
					level=CHECK_WARN,
					message="Portal session signing secret is not set.",
					remediation=(
						"Set 'portal_session_signing_secret' via the secrets payload for signed "
						"customer portal session cookies."
					),
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)
		if provider == "Disabled" or has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = (
				"Outbound SMS is disabled."
				if provider == "Disabled"
				else f"SMS provider '{provider}' configuration is incomplete."
			)
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = f"SMS provider '{provider}' is configured with warnings."
		else:
			status = STEP_STATUS_COMPLETE
			summary = f"SMS provider '{provider}' and OTP/session settings are configured."

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"provider": provider,
				"telnyx_api_key_set": telnyx_api_key_set,
				"sms_webhook_hmac_secret_set": hmac_secret_set,
				"portal_session_signing_secret_set": portal_signing_secret_set,
			},
			desk_urls=self._desk_urls(ctx),
			webhook_urls=self._webhook_urls(ctx),
		)

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		opts = dict(options) if isinstance(options, dict) else {}
		settings = frappe.get_single("ISM Settings")
		provider = str(settings.get("sms_gateway_provider") or "Disabled").strip()
		unconfigured_reason = get_sms_unconfigured_reason(settings)

		checks: list[CheckResult] = []
		if unconfigured_reason:
			checks.append(
				CheckResult(
					code="sms_config_valid",
					level=CHECK_FAIL,
					message=unconfigured_reason,
					remediation="Configure a valid SMS gateway provider and required credentials.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="sms_config_valid",
					level=CHECK_PASS,
					message=f"SMS configuration validated for provider '{provider}'.",
				)
			)

		test_phone = str(opts.get("test_phone") or "").strip()
		if test_phone:
			if ctx.profile == PROFILE_STAGING and not bool(opts.get("force")):
				checks.append(
					CheckResult(
						code="sms_test_dispatch_refused_staging",
						level=CHECK_FAIL,
						message=(
							"Refused to dispatch test SMS in Staging profile without force=True."
						),
						remediation=(
							"Pass options={'test_phone': '<number>', 'force': True} to explicitly "
							"authorize a test SMS in Staging."
						),
					)
				)
			elif not unconfigured_reason:
				from ism.api.sms import dispatch_sms

				try:
					res = dispatch_sms(
						test_phone,
						"ISM Setup Wizard SMS verification",
						message_type="Notification",
						respect_opt_out=False,
					)
				except Exception as exc:
					checks.append(
						CheckResult(
							code="sms_test_dispatch",
							level=CHECK_FAIL,
							message=f"Test SMS dispatch raised an error: {exc}",
						)
					)
				else:
					if res.get("success"):
						msg_id = res.get("message_id") or "n/a"
						checks.append(
							CheckResult(
								code="sms_test_dispatch",
								level=CHECK_PASS,
								message=(
									f"Test SMS dispatched successfully via '{provider}' "
									f"(message_id={msg_id})."
								),
							)
						)
					else:
						checks.append(
							CheckResult(
								code="sms_test_dispatch",
								level=CHECK_FAIL,
								message=(
									f"Test SMS dispatch failed: {res.get('error') or 'Unknown error'}."
								),
							)
						)
		else:
			checks.append(
				CheckResult(
					code="sms_test_dispatch_skipped",
					level=CHECK_INFO,
					message="No test_phone option provided; skipped live test SMS dispatch.",
				)
			)

		passed = not any(c.level == CHECK_FAIL for c in checks)
		summary = (
			f"SMS verification passed for provider '{provider}'."
			if passed
			else f"SMS verification failed for provider '{provider}'."
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=summary,
			checks=tuple(checks),
		)
