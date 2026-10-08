# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import requests
import frappe
from frappe import _


def dispatch_sms(phone_number: str, message: str) -> dict:
	"""Dispatch an SMS message using the provider configured in ISM Settings.

	Args:
		phone_number (str): Destination phone number in E.164 or national format.
		message (str): SMS message content.

	Returns:
		dict: {"success": bool, "message_id": str or None, "error": str or None}
	"""
	if not phone_number or not message:
		return {"success": False, "message_id": None, "error": _("Phone number and message are required.")}

	settings = frappe.get_single("ISM Settings")
	provider = settings.sms_gateway_provider or "Disabled"

	if provider == "Disabled":
		return {
			"success": False,
			"message_id": None,
			"error": _("SMS Gateway is disabled in ISM Settings. Please enable and configure a provider."),
		}

	if provider == "Custom HTTP Webhook":
		endpoint = (settings.sms_gateway_endpoint or "").strip()
		if not endpoint or not endpoint.startswith(("http://", "https://")):
			return {
				"success": False,
				"message_id": None,
				"error": _("Valid HTTP or HTTPS SMS Gateway Endpoint URL is required."),
			}

		headers = {
			"Content-Type": "application/json",
		}
		if settings.sms_gateway_api_key:
			headers["Authorization"] = f"Bearer {settings.get_password('sms_gateway_api_key')}"

		payload = {
			"to": phone_number.strip(),
			"from": (settings.sms_sender_id or "ISM").strip(),
			"body": message.strip(),
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
			else:
				err_msg = f"HTTP {response.status_code}: {response.text[:200]}"
				frappe.log_error(title="ISM SMS Webhook Failure", message=err_msg)
				return {"success": False, "message_id": None, "error": err_msg}
		except Exception as e:
			err_msg = str(e)
			frappe.log_error(title="ISM SMS Dispatch Exception", message=err_msg)
			return {"success": False, "message_id": None, "error": err_msg}

	# Fallback if unhandled provider
	return {
		"success": False,
		"message_id": None,
		"error": _("Unsupported SMS provider: {0}").format(provider),
	}
