# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint

from ism.comms.contracts import get_client_ip, normalize_phone_e164
from ism.comms.telnyx import process_telnyx_webhook_event, verify_webhook_request

_WEBHOOK_RATE_LIMIT_PREFIX = "ism:comms:webhook_rl"
_WEBHOOK_RATE_LIMIT_PER_MIN = 300
_WEBHOOK_RATE_LIMIT_WINDOW_SEC = 60
_OPT_OUT_READ_ROLES = frozenset({"System Manager", "ISM Manager", "ISM Service Advisor"})


def _check_webhook_rate_limit(client_ip: str) -> bool:
	"""Enforce a per-IP per-minute sanity rate limit using atomic Redis INCR + EXPIRE."""
	cache = frappe.cache()
	key = f"{_WEBHOOK_RATE_LIMIT_PREFIX}:ip:{client_ip}"
	try:
		full_key = cache.make_key(key) if hasattr(cache, "make_key") else key
		if hasattr(cache, "incr"):
			count = int(cache.incr(full_key))
			if count == 1 and hasattr(cache, "expire"):
				cache.expire(full_key, _WEBHOOK_RATE_LIMIT_WINDOW_SEC)
		else:
			count = cint(cache.get_value(key) or 0) + 1
			cache.set_value(key, count, expires_in_sec=_WEBHOOK_RATE_LIMIT_WINDOW_SEC)
		return count <= _WEBHOOK_RATE_LIMIT_PER_MIN
	except Exception:
		# Never drop verified carrier webhooks if Redis has a transient blip.
		return True


def clear_webhook_rate_limits() -> None:
	"""Clear webhook per-IP rate-limit keys from Redis cache (for hermetic test runs)."""
	cache = frappe.cache()
	if hasattr(cache, "delete_keys"):
		cache.delete_keys(f"{_WEBHOOK_RATE_LIMIT_PREFIX}:*")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def telnyx_webhook() -> dict[str, Any]:
	"""Receive and verify Telnyx v2 / custom HMAC SMS delivery and inbound compliance webhooks."""
	client_ip = get_client_ip()
	if not _check_webhook_rate_limit(client_ip):
		frappe.local.response["http_status_code"] = 429
		return {"ok": False, "error": "Rate limit exceeded."}

	req = getattr(frappe.local, "request", None)
	raw_body = req.get_data(cache=True, as_text=False) if req is not None else b""
	if isinstance(raw_body, str):
		raw_body = raw_body.encode("utf-8")
	headers = req.headers if req is not None and hasattr(req, "headers") else {}

	verified, _reason = verify_webhook_request(raw_body, headers)
	if not verified:
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "error": "Invalid webhook signature."}

	try:
		payload = json.loads(raw_body.decode("utf-8"))
	except (UnicodeDecodeError, json.JSONDecodeError):
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": "Invalid JSON payload."}

	if not isinstance(payload, dict):
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": "Invalid JSON payload."}

	result = process_telnyx_webhook_event(payload)
	return {"ok": True, **result}


@frappe.whitelist()
def get_sms_opt_out_status(phone_number: str) -> dict[str, Any]:
	"""Return SMS opt-out status for a normalized E.164 phone number (staff roles only)."""
	user_roles = set(frappe.get_roles(frappe.session.user))
	if not (user_roles & _OPT_OUT_READ_ROLES):
		frappe.throw(_("Not permitted to view SMS opt-out status."), frappe.PermissionError)

	settings = frappe.get_single("ISM Settings")
	default_cc = (getattr(settings, "sms_default_country_code", None) or "+1").strip() or "+1"
	try:
		phone_e164 = normalize_phone_e164(phone_number, default_country_code=default_cc)
	except ValueError as exc:
		frappe.throw(str(exc), frappe.ValidationError)

	row = frappe.db.get_value(
		"SMS Opt Out",
		phone_e164,
		["is_opted_out", "source", "last_keyword", "opted_out_at", "opted_in_at"],
		as_dict=True,
	)
	if not row:
		return {
			"phone_e164": phone_e164,
			"is_opted_out": False,
			"source": None,
			"last_keyword": None,
			"opted_out_at": None,
			"opted_in_at": None,
		}

	return {
		"phone_e164": phone_e164,
		"is_opted_out": bool(cint(row.is_opted_out)),
		"source": row.source,
		"last_keyword": row.last_keyword,
		"opted_out_at": str(row.opted_out_at) if row.opted_out_at else None,
		"opted_in_at": str(row.opted_in_at) if row.opted_in_at else None,
	}
