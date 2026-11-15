# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import ipaddress
import json
from urllib.parse import urlparse

import frappe
from frappe import _
import requests


SUPPORTED_SMS_PROVIDERS = ("Disabled", "Custom HTTP Webhook")
_BLOCKED_SMS_HOSTS = frozenset({"metadata.google.internal", "169.254.169.254"})


def _is_valid_sms_webhook_url(endpoint: str) -> bool:
	"""Allow HTTPS webhook URLs, permitting plain HTTP only on loopback hosts for local dev/test."""
	if not endpoint or any(ch.isspace() for ch in endpoint):
		return False

	parsed = urlparse(endpoint)
	scheme = (parsed.scheme or "").lower()
	hostname = (parsed.hostname or "").strip().lower()
	if scheme not in ("http", "https") or not hostname:
		return False
	if parsed.username or parsed.password:
		return False
	if hostname in _BLOCKED_SMS_HOSTS:
		return False

	is_loopback = hostname in ("localhost", "localhost.localdomain")
	try:
		ip = ipaddress.ip_address(hostname)
	except ValueError:
		ip = None

	if ip is not None:
		if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
			return False
		is_loopback = ip.is_loopback

	if scheme == "http" and not is_loopback:
		return False
	return True


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
			allow_redirects=False,
		)
		if response.status_code in (200, 201, 202):
			try:
				res_data = response.json()
				msg_id = res_data.get("message_id") or res_data.get("id") or str(response.status_code)
			except Exception:
				msg_id = str(response.status_code)

			return {"success": True, "message_id": msg_id, "error": None}

		frappe.log_error(
			title="ISM SMS Webhook Failure",
			message=f"HTTP {response.status_code}: {response.text[:200]}",
		)
		return {
			"success": False,
			"message_id": None,
			"error": f"HTTP {response.status_code} from SMS gateway.",
		}
	except Exception:
		frappe.log_error(
			title="ISM SMS Dispatch Exception",
			message=frappe.get_traceback(),
		)
		return {
			"success": False,
			"message_id": None,
			"error": _("SMS dispatch failed due to a gateway communication error."),
		}


def get_sms_unconfigured_reason(settings=None) -> str | None:
	"""Return a human-readable reason if SMS dispatch is disabled or misconfigured."""
	if settings is None:
		settings = frappe.get_single("ISM Settings")

	provider = (getattr(settings, "sms_gateway_provider", None) or "Disabled").strip()
	if provider == "Disabled":
		return _("SMS Gateway is disabled in ISM Settings. Please enable and configure a provider.")

	if provider == "Custom HTTP Webhook":
		endpoint = (getattr(settings, "sms_gateway_endpoint", None) or "").strip()
		if not _is_valid_sms_webhook_url(endpoint):
			return _("Valid HTTPS SMS Gateway Endpoint URL is required (HTTP allowed only for loopback).")
		return None

	return _("Unsupported SMS provider: {0}").format(provider)
