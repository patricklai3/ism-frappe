# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import requests
import frappe
from frappe import _


SUPPORTED_SMS_PROVIDERS = ("Disabled", "Custom HTTP Webhook")


def dispatch_sms(phone_number: str, message: str) -> dict:
	"""Dispatch an SMS message using the provider configured in ISM Settings.

	Args:
		phone_number (str): Destination phone number in E.164 or national format.
		message (str): SMS message content.

	Returns:
		dict: {"success": bool, "message_id": str or None, "error": str or None}
	"""
	target_phone = phone_number.strip() if isinstance(phone_number, str) else ""
	body_text = message.strip() if isinstance(message, str) else ""
	if not target_phone or not body_text:
		return {"success": False, "message_id": None, "error": _("Phone number and message are required.")}

	settings = frappe.get_single("ISM Settings")
	unconfigured_reason = get_sms_unconfigured_reason(settings)
	if unconfigured_reason:
		return {"success": False, "message_id": None, "error": unconfigured_reason}

	endpoint = (settings.sms_gateway_endpoint or "").strip()
	headers = {
		"Content-Type": "application/json",
	}
	if settings.sms_gateway_api_key:
		headers["Authorization"] = f"Bearer {settings.get_password('sms_gateway_api_key')}"

	payload = {
		"to": target_phone,
		"from": (settings.sms_sender_id or "ISM").strip(),
		"body": body_text,
	}

	try:
		response = requests.post(
			endpoint,
			headers=headers,
			data=json.dumps(payload),
			timeout=10,
		)
		if response.status_code in (200, 201, 202):
			try:
				res_data = response.json()
				msg_id = res_data.get("message_id") or res_data.get("id") or str(response.status_code)
			except Exception:
				msg_id = str(response.status_code)

			return {"success": True, "message_id": msg_id, "error": None}

		err_msg = f"HTTP {response.status_code}: {response.text[:200]}"
		frappe.log_error(title="ISM SMS Webhook Failure", message=err_msg)
		return {"success": False, "message_id": None, "error": err_msg}
	except Exception as e:
		err_msg = str(e)
		frappe.log_error(title="ISM SMS Dispatch Exception", message=err_msg)
		return {"success": False, "message_id": None, "error": err_msg}


def get_sms_unconfigured_reason(settings=None) -> str | None:
	"""Return a human-readable reason if SMS dispatch is disabled or misconfigured."""
	if settings is None:
		settings = frappe.get_single("ISM Settings")

	provider = (getattr(settings, "sms_gateway_provider", None) or "Disabled").strip()
	if provider == "Disabled":
		return _("SMS Gateway is disabled in ISM Settings. Please enable and configure a provider.")

	if provider == "Custom HTTP Webhook":
		endpoint = (getattr(settings, "sms_gateway_endpoint", None) or "").strip()
		if not endpoint or not endpoint.startswith(("http://", "https://")):
			return _("Valid HTTP or HTTPS SMS Gateway Endpoint URL is required.")
		return None

	return _("Unsupported SMS provider: {0}").format(provider)
