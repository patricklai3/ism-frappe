# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Optional Capability Pack step: Square Online Payments (CAD)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import frappe
import requests
from frappe.utils import cint

from ism.payments.contracts import (
	SQUARE_API_BASE_URLS,
	SQUARE_API_VERSION,
	SQUARE_CURRENCY,
	SQUARE_ENV_DISABLED,
	SQUARE_ENV_MOCK,
	SQUARE_ENV_PRODUCTION,
	SQUARE_ENV_SANDBOX,
)
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_INFO,
	CHECK_PASS,
	CHECK_WARN,
	PROFILE_STAGING,
	REDACTED_VALUE,
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

_SQUARE_VERIFY_TIMEOUT_SECONDS = 10


def _scrub_token(text: str, token: str | None) -> str:
	result = str(text or "")
	if token and token in result:
		result = result.replace(token, REDACTED_VALUE)
	return result


class PaymentsSquareStep(SettingsSectionStep):
	"""Configure Square CAD online payments, ledger mapping, and webhook verification."""

	meta = StepMeta(
		id="payments_square",
		title="Square Online Payments (CAD)",
		phase="integrations",
		order=140,
		schema_version=1,
		tier=TIER_OPTIONAL_PACK,
		depends_on=("erpnext_foundation", "customer_portal"),
		owner_domain="payments",
		managed_settings_sections=("sb_square_payments",),
		allow_skip=True,
		description=(
			"Configure Square Online Payments (CAD), Mode of Payment, CAD Bank Account ledger "
			"mapping, API credentials, and webhook signature verification."
		),
	)

	def _webhook_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"square_webhook": f"{ctx.site_url}/api/method/ism.api.payments.square_webhook",
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
				"label": "Square Mock (Safe Staging)",
				"description": "Deterministic offline Square CAD payment mode for staging and testing.",
				"payload": {
					"settings": {
						"square_environment": SQUARE_ENV_MOCK,
						"square_mode_of_payment": "Square",
					}
				},
			},
			{
				"id": "sandbox_cad",
				"label": "Square Sandbox (CAD)",
				"description": (
					"Square Sandbox CAD defaults (pass square_access_token and "
					"square_webhook_signature_key via secrets)."
				),
				"payload": {
					"settings": {
						"square_environment": SQUARE_ENV_SANDBOX,
						"square_mode_of_payment": "Square",
						"square_webhook_notification_url": self._webhook_urls(ctx)["square_webhook"],
					}
				},
			},
		]

	def inspect(self, ctx: SetupContext) -> StepInspection:
		settings = frappe.get_single("ISM Settings")
		env = str(settings.get("square_environment") or SQUARE_ENV_DISABLED).strip()
		currency = str(settings.get("square_currency") or "").strip().upper()
		app_id = str(settings.get("square_application_id") or "").strip()
		location_id = str(settings.get("square_location_id") or "").strip()
		mop = str(settings.get("square_mode_of_payment") or "").strip()
		bank_account = str(settings.get("square_bank_account") or "").strip()

		access_token_set = _is_password_field_set(settings, "square_access_token")
		webhook_sig_set = _is_password_field_set(settings, "square_webhook_signature_key")

		checks: list[CheckResult] = []

		if env == SQUARE_ENV_DISABLED:
			checks.append(
				CheckResult(
					code="square_environment",
					level=CHECK_FAIL,
					message="Square online payments environment is Disabled.",
					remediation=(
						"Select 'Sandbox', 'Production', or 'Mock (Deterministic Test)' in ISM Settings "
						"(or skip this optional pack)."
					),
				)
			)
		else:
			checks.append(
				CheckResult(
					code="square_environment",
					level=CHECK_PASS,
					message=f"Square environment is set to '{env}'.",
				)
			)

		if ctx.profile == PROFILE_STAGING and env == SQUARE_ENV_PRODUCTION:
			checks.append(
				CheckResult(
					code="square_staging_production_risk",
					level=CHECK_WARN,
					message="Square Production environment is selected while running in Staging profile.",
					remediation=(
						"Use 'Sandbox' or 'Mock (Deterministic Test)' in Staging to avoid live charges."
					),
				)
			)

		if currency == SQUARE_CURRENCY:
			checks.append(
				CheckResult(
					code="square_currency",
					level=CHECK_PASS,
					message="Square currency is set to CAD.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="square_currency",
					level=CHECK_FAIL,
					message=f"Square currency must be '{SQUARE_CURRENCY}', got '{currency or 'unset'}'.",
					remediation="Ensure ISM Settings.square_currency is set to 'CAD'.",
				)
			)

		if mop and frappe.db.exists("Mode of Payment", mop):
			checks.append(
				CheckResult(
					code="square_mode_of_payment",
					level=CHECK_PASS,
					message=f"Square Mode of Payment '{mop}' exists.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="square_mode_of_payment",
					level=CHECK_FAIL,
					message=(
						f"Square Mode of Payment '{mop}' does not exist."
						if mop
						else "Square Mode of Payment is not configured."
					),
					remediation="Set square_mode_of_payment to an existing Mode of Payment (e.g. 'Square').",
				)
			)

		if not bank_account:
			checks.append(
				CheckResult(
					code="square_bank_account",
					level=CHECK_FAIL,
					message="Square Bank Account is not configured.",
					remediation="Select an active leaf CAD Bank Account in ISM Settings.",
				)
			)
		else:
			acct = frappe.db.get_value(
				"Account",
				bank_account,
				["name", "account_type", "is_group", "disabled", "account_currency"],
				as_dict=True,
			)
			if not acct:
				checks.append(
					CheckResult(
						code="square_bank_account",
						level=CHECK_FAIL,
						message=f"Square Bank Account '{bank_account}' does not exist.",
						remediation="Select an existing active leaf CAD Bank Account.",
					)
				)
			else:
				acct_errors: list[str] = []
				if acct.get("account_type") != "Bank":
					acct_errors.append(
						f"account_type must be 'Bank' (got '{acct.get('account_type') or 'unset'}'; never Cash)"
					)
				if cint(acct.get("is_group")) != 0:
					acct_errors.append("must be a leaf account (is_group=0)")
				if cint(acct.get("disabled")) != 0:
					acct_errors.append("account is disabled")
				acct_currency = str(acct.get("account_currency") or "").strip().upper()
				if acct_currency and acct_currency != SQUARE_CURRENCY:
					acct_errors.append(f"account_currency must be CAD (got '{acct_currency}')")

				if acct_errors:
					checks.append(
						CheckResult(
							code="square_bank_account",
							level=CHECK_FAIL,
							message=f"Square Bank Account '{bank_account}' is invalid: {'; '.join(acct_errors)}.",
							remediation="Configure an active leaf CAD Account with account_type='Bank'.",
						)
					)
				else:
					checks.append(
						CheckResult(
							code="square_bank_account",
							level=CHECK_PASS,
							message=f"Square Bank Account '{bank_account}' is an active leaf CAD Bank account.",
						)
					)

		if env in (SQUARE_ENV_SANDBOX, SQUARE_ENV_PRODUCTION):
			missing_ids = [
				k
				for k, v in (
					("square_application_id", app_id),
					("square_location_id", location_id),
				)
				if not v
			]
			if missing_ids:
				checks.append(
					CheckResult(
						code="square_identifiers",
						level=CHECK_FAIL,
						message=f"Missing Square identifier(s): {', '.join(missing_ids)}.",
						remediation=f"Set {', '.join(missing_ids)} in ISM Settings.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="square_identifiers",
						level=CHECK_PASS,
						message="Square Application ID and Location ID are set.",
					)
				)

			checks.append(
				CheckResult(
					code="square_access_token",
					level=CHECK_PASS if access_token_set else CHECK_FAIL,
					message=(
						"Square access token is set."
						if access_token_set
						else "Square access token is not set."
					),
					remediation=(
						None
						if access_token_set
						else "Provide 'square_access_token' via the secrets payload."
					),
				)
			)
			checks.append(
				CheckResult(
					code="square_webhook_signature_key",
					level=CHECK_PASS if webhook_sig_set else CHECK_WARN,
					message=(
						"Square webhook signature key is set."
						if webhook_sig_set
						else "Square webhook signature key is not set."
					),
					remediation=(
						None
						if webhook_sig_set
						else "Provide 'square_webhook_signature_key' via the secrets payload for webhook verification."
					),
				)
			)
		elif env == SQUARE_ENV_MOCK:
			checks.append(
				CheckResult(
					code="square_mock_credentials",
					level=CHECK_INFO,
					message="Square Mock mode active; live Square API credentials are not required.",
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)
		if env == SQUARE_ENV_DISABLED or has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = (
				"Square Online Payments are disabled."
				if env == SQUARE_ENV_DISABLED
				else f"Square ({env}) configuration is incomplete."
			)
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = f"Square ({env}) is configured with warnings."
		else:
			status = STEP_STATUS_COMPLETE
			summary = f"Square Online Payments ({env}, CAD) are configured."

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"environment": env,
				"currency": currency,
				"access_token_set": access_token_set,
				"webhook_signature_key_set": webhook_sig_set,
			},
			desk_urls=self._desk_urls(ctx),
			webhook_urls=self._webhook_urls(ctx),
		)

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		inspection = self.inspect(ctx)
		checks = list(inspection.checks)

		settings = frappe.get_single("ISM Settings")
		env = str(settings.get("square_environment") or SQUARE_ENV_DISABLED).strip()

		if env == SQUARE_ENV_MOCK:
			checks.append(
				CheckResult(
					code="square_mock_verify",
					level=CHECK_PASS,
					message="Square Mock (Deterministic Test) verification passed deterministically.",
				)
			)
		elif env in (SQUARE_ENV_SANDBOX, SQUARE_ENV_PRODUCTION):
			base_url = SQUARE_API_BASE_URLS[env]
			location_id = str(settings.get("square_location_id") or "").strip()
			access_token = str(
				settings.get_password("square_access_token", raise_exception=False) or ""
			).strip()

			if not location_id or not access_token:
				checks.append(
					CheckResult(
						code="square_location_api",
						level=CHECK_FAIL,
						message="Cannot verify Square location: square_location_id or square_access_token is missing.",
						remediation="Configure square_location_id and square_access_token before verifying.",
					)
				)
			else:
				url = f"{base_url}/v2/locations/{quote(location_id, safe='')}"
				headers = {
					"Authorization": f"Bearer {access_token}",
					"Square-Version": SQUARE_API_VERSION,
					"Accept": "application/json",
				}
				try:
					resp = requests.get(
						url,
						headers=headers,
						timeout=_SQUARE_VERIFY_TIMEOUT_SECONDS,
					)
				except Exception as exc:
					checks.append(
						CheckResult(
							code="square_location_api",
							level=CHECK_FAIL,
							message=f"Square location lookup failed: {_scrub_token(str(exc), access_token)}",
							remediation="Verify network connectivity, square_environment, and credentials.",
						)
					)
				else:
					if resp.status_code != 200:
						body_snippet = _scrub_token((resp.text or "")[:200], access_token)
						checks.append(
							CheckResult(
								code="square_location_api",
								level=CHECK_FAIL,
								message=(
									f"Square location API returned HTTP {resp.status_code}: {body_snippet}"
								),
								remediation="Check square_access_token and square_location_id in Square Developer Dashboard.",
							)
						)
					else:
						try:
							data = resp.json()
						except Exception as exc:
							checks.append(
								CheckResult(
									code="square_location_api",
									level=CHECK_FAIL,
									message=f"Invalid JSON returned by Square location API: {exc}",
								)
							)
						else:
							loc = data.get("location") if isinstance(data, dict) else None
							if not isinstance(loc, dict):
								checks.append(
									CheckResult(
										code="square_location_api",
										level=CHECK_FAIL,
										message="Square location API response did not contain a 'location' object.",
									)
								)
							else:
								loc_currency = str(loc.get("currency") or "").strip().upper()
								loc_status = str(loc.get("status") or "").strip().upper()
								loc_name = str(loc.get("name") or location_id)
								loc_errors: list[str] = []
								if loc_currency != SQUARE_CURRENCY:
									loc_errors.append(
										f"location currency is '{loc_currency or 'unset'}' (expected CAD)"
									)
								if loc_status != "ACTIVE":
									loc_errors.append(
										f"location status is '{loc_status or 'unset'}' (expected ACTIVE)"
									)
								if loc_errors:
									checks.append(
										CheckResult(
											code="square_location_api",
											level=CHECK_FAIL,
											message=(
												f"Square location '{loc_name}' check failed: "
												f"{'; '.join(loc_errors)}."
											),
											remediation="Select an ACTIVE CAD Square location.",
										)
									)
								else:
									checks.append(
										CheckResult(
											code="square_location_api",
											level=CHECK_PASS,
											message=(
												f"Square location '{loc_name}' verified "
												"(currency=CAD, status=ACTIVE)."
											),
										)
									)

		passed = not any(c.level == CHECK_FAIL for c in checks)
		summary = (
			f"Square Online Payments ({env}) verification passed."
			if passed
			else f"Square Online Payments ({env}) verification failed."
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=summary,
			checks=tuple(checks),
		)
