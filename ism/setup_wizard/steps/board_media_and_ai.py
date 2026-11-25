# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Optional Capability Pack step: Shop Board Media Storage & AI."""

from __future__ import annotations

from typing import Any

import frappe

from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_INFO,
	CHECK_PASS,
	CHECK_WARN,
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


def _scrub_secret(text: str, secret: str | None) -> str:
	result = str(text or "")
	if secret and secret in result:
		result = result.replace(secret, REDACTED_VALUE)
	return result


class BoardMediaAndAiStep(SettingsSectionStep):
	"""Configure Shop Board media storage (Frappe Private File / Google Drive) and Board AI provider."""

	meta = StepMeta(
		id="board_media_and_ai",
		title="Shop Board Media Storage & AI",
		phase="integrations",
		order=150,
		schema_version=1,
		tier=TIER_OPTIONAL_PACK,
		depends_on=("site_environment", "users_roles_employees"),
		owner_domain="board",
		managed_settings_sections=("sb_media_storage", "sb_board_ai"),
		allow_skip=True,
		description=(
			"Configure media storage backend for floor photo/voice captures and optional "
			"Board AI transcription/triage provider (Gemini REST API, OpenAI Compatible, or Mock)."
		),
	)

	def _desk_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"board": f"{ctx.site_url}/board",
			"cockpit": f"{ctx.site_url}/cockpit",
			"tech": f"{ctx.site_url}/tech",
			"ism_settings": f"{ctx.site_url}/app/ism-settings",
		}

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		schema = super().get_schema(ctx)
		schema["desk_urls"] = self._desk_urls(ctx)
		return schema

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "local_and_mock_ai",
				"label": "Frappe Private File + Mock AI (Staging)",
				"description": "Local private file storage with deterministic offline Mock Board AI.",
				"payload": {
					"settings": {
						"media_storage_backend": "Frappe Private File",
						"board_ai_provider": "Mock (Deterministic Test)",
						"board_ai_auto_process": 1,
					}
				},
			},
			{
				"id": "local_and_gemini",
				"label": "Frappe Private File + Gemini REST API",
				"description": (
					"Local private file storage with Gemini REST API (pass board_ai_api_key via secrets)."
				),
				"payload": {
					"settings": {
						"media_storage_backend": "Frappe Private File",
						"board_ai_provider": "Gemini REST API",
						"board_ai_model": "gemini-3.6-flash",
						"board_ai_auto_process": 1,
					}
				},
			},
		]

	def inspect(self, ctx: SetupContext) -> StepInspection:
		settings = frappe.get_single("ISM Settings")
		media_backend = str(settings.get("media_storage_backend") or "Frappe Private File").strip()
		gdrive_auth_mode = str(settings.get("gdrive_auth_mode") or "OAuth2 Refresh Token").strip()
		gdrive_root_folder = str(settings.get("gdrive_root_folder_id") or "").strip()
		gdrive_client_id = str(settings.get("gdrive_client_id") or "").strip()
		gdrive_client_secret_set = _is_password_field_set(settings, "gdrive_client_secret")
		gdrive_refresh_token_set = _is_password_field_set(settings, "gdrive_refresh_token")
		gdrive_sa_json_set = _is_password_field_set(settings, "gdrive_service_account_json")

		ai_provider = str(settings.get("board_ai_provider") or "Disabled").strip()
		ai_api_key_set = _is_password_field_set(settings, "board_ai_api_key")
		ai_model = str(settings.get("board_ai_model") or "").strip()

		checks: list[CheckResult] = []

		# Media storage checks
		if media_backend == "Google Drive":
			if gdrive_root_folder:
				checks.append(
					CheckResult(
						code="media_gdrive_root_folder",
						level=CHECK_PASS,
						message="Google Drive root folder ID is configured.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="media_gdrive_root_folder",
						level=CHECK_FAIL,
						message="Google Drive root folder ID (gdrive_root_folder_id) is not set.",
						remediation="Set gdrive_root_folder_id in ISM Settings.",
					)
				)

			if gdrive_auth_mode == "Service Account JSON":
				checks.append(
					CheckResult(
						code="media_gdrive_credentials",
						level=CHECK_PASS if gdrive_sa_json_set else CHECK_FAIL,
						message=(
							"Google Drive Service Account JSON credential is set."
							if gdrive_sa_json_set
							else "Google Drive Service Account JSON credential is not set."
						),
						remediation=(
							None
							if gdrive_sa_json_set
							else "Provide 'gdrive_service_account_json' via the secrets payload."
						),
					)
				)
			else:
				missing_oauth: list[str] = []
				if not gdrive_client_id:
					missing_oauth.append("gdrive_client_id")
				if not gdrive_client_secret_set:
					missing_oauth.append("gdrive_client_secret")
				if not gdrive_refresh_token_set:
					missing_oauth.append("gdrive_refresh_token")

				if not missing_oauth:
					checks.append(
						CheckResult(
							code="media_gdrive_credentials",
							level=CHECK_PASS,
							message="Google Drive OAuth2 credentials are set.",
						)
					)
				else:
					checks.append(
						CheckResult(
							code="media_gdrive_credentials",
							level=CHECK_FAIL,
							message=f"Missing Google Drive OAuth2 credential(s): {', '.join(missing_oauth)}.",
							remediation=(
								"Set gdrive_client_id and provide gdrive_client_secret / "
								"gdrive_refresh_token via secrets."
							),
						)
					)
		else:
			checks.append(
				CheckResult(
					code="media_storage_backend",
					level=CHECK_PASS,
					message=f"Media storage backend '{media_backend}' is ready.",
				)
			)

		# Board AI checks
		if ai_provider == "Disabled":
			checks.append(
				CheckResult(
					code="board_ai_provider",
					level=CHECK_INFO,
					message="Board AI provider is Disabled (manual triage only).",
				)
			)
		elif ai_provider == "Mock (Deterministic Test)":
			checks.append(
				CheckResult(
					code="board_ai_provider",
					level=CHECK_PASS,
					message="Board AI provider is set to Mock (Deterministic Test).",
				)
			)
		elif ai_provider in ("Gemini REST API", "OpenAI Compatible"):
			checks.append(
				CheckResult(
					code="board_ai_api_key",
					level=CHECK_PASS if ai_api_key_set else CHECK_FAIL,
					message=(
						f"Board AI API key is set for '{ai_provider}'."
						if ai_api_key_set
						else f"Board AI API key is not set for '{ai_provider}'."
					),
					remediation=(
						None
						if ai_api_key_set
						else "Provide 'board_ai_api_key' via the secrets payload."
					),
				)
			)
			checks.append(
				CheckResult(
					code="board_ai_model",
					level=CHECK_PASS if ai_model else CHECK_FAIL,
					message=(
						f"Board AI model '{ai_model}' is configured."
						if ai_model
						else "Board AI model (board_ai_model) is not configured."
					),
					remediation=(
						None
						if ai_model
						else "Set board_ai_model (e.g. 'gemini-3.6-flash') in ISM Settings."
					),
				)
			)
		else:
			checks.append(
				CheckResult(
					code="board_ai_provider",
					level=CHECK_FAIL,
					message=f"Unsupported Board AI provider '{ai_provider}'.",
					remediation="Select Disabled, Mock (Deterministic Test), Gemini REST API, or OpenAI Compatible.",
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)
		if has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = "Shop Board media storage or AI provider configuration is incomplete."
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = "Shop Board media storage and AI configured with warnings."
		else:
			status = STEP_STATUS_COMPLETE
			summary = f"Shop Board media ({media_backend}) and AI ({ai_provider}) are configured."

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"media_storage_backend": media_backend,
				"board_ai_provider": ai_provider,
				"board_ai_api_key_set": ai_api_key_set,
			},
			desk_urls=self._desk_urls(ctx),
		)

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		settings = frappe.get_single("ISM Settings")
		media_backend = str(settings.get("media_storage_backend") or "Frappe Private File").strip()
		ai_provider = str(settings.get("board_ai_provider") or "Disabled").strip()
		checks: list[CheckResult] = []

		# Media backend verification
		if media_backend == "Google Drive":
			try:
				from ism.board.media_storage import test_google_drive_connection

				gd_res = test_google_drive_connection(settings=settings)
			except Exception as exc:
				checks.append(
					CheckResult(
						code="media_gdrive_connection",
						level=CHECK_FAIL,
						message=f"Google Drive connection check raised an error: {exc}",
						remediation="Verify Google Drive credentials and root folder permissions.",
					)
				)
			else:
				if gd_res.get("ok"):
					folder_name = gd_res.get("root_folder_name") or gd_res.get("root_folder_id") or ""
					checks.append(
						CheckResult(
							code="media_gdrive_connection",
							level=CHECK_PASS,
							message=f"Google Drive connection verified (root folder: '{folder_name}').",
						)
					)
				else:
					checks.append(
						CheckResult(
							code="media_gdrive_connection",
							level=CHECK_FAIL,
							message=str(gd_res.get("error") or "Google Drive connection check failed."),
							remediation="Verify Google Drive credentials and gdrive_root_folder_id.",
						)
					)
		else:
			checks.append(
				CheckResult(
					code="media_local_backend",
					level=CHECK_PASS,
					message="Frappe Private File local media backend is ready.",
				)
			)

		# Board AI verification
		if ai_provider == "Disabled":
			checks.append(
				CheckResult(
					code="board_ai_verify",
					level=CHECK_INFO,
					message="Board AI provider is Disabled; no AI connectivity check required.",
				)
			)
		elif ai_provider == "Mock (Deterministic Test)":
			checks.append(
				CheckResult(
					code="board_ai_verify",
					level=CHECK_PASS,
					message="Board AI Mock (Deterministic Test) verification passed deterministically.",
				)
			)
		elif ai_provider in ("Gemini REST API", "OpenAI Compatible"):
			raw_api_key = str(
				settings.get_password("board_ai_api_key", raise_exception=False) or ""
			).strip()
			model_name = str(settings.get("board_ai_model") or "").strip()
			if not raw_api_key or not model_name:
				checks.append(
					CheckResult(
						code="board_ai_verify",
						level=CHECK_FAIL,
						message=(
							f"Board AI ({ai_provider}) is missing board_ai_api_key or board_ai_model."
						),
						remediation="Configure board_ai_api_key (via secrets) and board_ai_model.",
					)
				)
			else:
				try:
					from ism.board.ai_providers import get_ai_provider

					provider = get_ai_provider(settings)
					if provider is None:
						raise ValueError("Board AI provider resolved to None.")
					ping_res = provider.analyze_capture(
						{"mode": "setup_wizard_ping"},
						"Connectivity check: respond briefly.",
						[],
					)
				except Exception as exc:
					checks.append(
						CheckResult(
							code="board_ai_verify",
							level=CHECK_FAIL,
							message=(
								f"Board AI ({ai_provider}) connectivity check failed: "
								f"{_scrub_secret(str(exc), raw_api_key)}"
							),
							remediation="Verify board_ai_api_key, board_ai_model, and board_ai_endpoint.",
						)
					)
				else:
					if isinstance(ping_res, dict):
						checks.append(
							CheckResult(
								code="board_ai_verify",
								level=CHECK_PASS,
								message=(
									f"Board AI ({ai_provider}, model '{model_name}') connectivity check passed."
								),
							)
						)
					else:
						checks.append(
							CheckResult(
								code="board_ai_verify",
								level=CHECK_FAIL,
								message=f"Board AI ({ai_provider}) returned an unexpected response type.",
							)
						)
		else:
			checks.append(
				CheckResult(
					code="board_ai_verify",
					level=CHECK_FAIL,
					message=f"Unsupported Board AI provider '{ai_provider}'.",
				)
			)

		passed = not any(c.level == CHECK_FAIL for c in checks)
		summary = (
			f"Shop Board media ({media_backend}) and AI ({ai_provider}) verification passed."
			if passed
			else "Shop Board media or AI verification failed."
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=summary,
			checks=tuple(checks),
		)
