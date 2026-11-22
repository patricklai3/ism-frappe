# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frappe web page controller for the hosted self-serve booking funnel (/book)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from ism.booking.contracts import (
	ALLOWED_VEHICLE_MODELS,
	DEFAULT_BOOKING_TITLE,
	DEFAULT_MAX_PACKAGES_PER_BOOKING,
	ERR_BOOKING_DISABLED,
	HONEYPOT_FIELD_NAME,
	PORTAL_ROUTE,
	VEHICLE_MIN_YEAR,
)
from ism.booking.service import get_booking_boot_config
from ism.comms.contracts import get_client_ip

no_cache = 1

SECURITY_HEADERS: dict[str, str] = {
	"X-Frame-Options": "DENY",
	"Content-Security-Policy": "frame-ancestors 'none'",
}

_BOOK_ASSET_FILES: tuple[str, ...] = (
	"book.css",
	"api.js",
	"state.js",
	"app.js",
)


def _safe_json_for_script(payload: Any) -> str:
	"""Serialize JSON safely for inline <script type='application/json'> embedding."""
	return (
		json.dumps(payload, default=str, ensure_ascii=False)
		.replace("</", "<\\/")
		.replace("<", "\\u003c")
		.replace(">", "\\u003e")
		.replace("&", "\\u0026")
		.replace("\u2028", "\\u2028")
		.replace("\u2029", "\\u2029")
	)


def _compute_asset_version() -> str:
	"""Compute a short deterministic cache-busting token from ism/public/book/* mtimes."""
	book_dir = Path(__file__).resolve().parent.parent / "public" / "book"
	digest = hashlib.sha256()
	found = False
	for filename in _BOOK_ASSET_FILES:
		asset_path = book_dir / filename
		try:
			stat = asset_path.stat()
		except OSError:
			continue
		found = True
		digest.update(f"{filename}:{stat.st_mtime_ns}:{stat.st_size};".encode())
	return digest.hexdigest()[:12] if found else "1"


def _apply_security_headers(context: Any) -> dict[str, str]:
	"""Enforce anti-clickjacking headers on Frappe response and page context."""
	local_headers = getattr(frappe.local, "response_headers", None)
	if local_headers is None:
		frappe.local.response_headers = dict(SECURITY_HEADERS)
	else:
		for key, value in SECURITY_HEADERS.items():
			local_headers[key] = value

	for attr in ("response_headers", "headers", "http_headers"):
		existing = context.get(attr)
		if isinstance(existing, dict):
			existing.update(SECURITY_HEADERS)
		else:
			context[attr] = dict(SECURITY_HEADERS)

	return dict(SECURITY_HEADERS)


def _fallback_disabled_boot_config() -> dict[str, Any]:
	"""Return a safe, zero-PII disabled boot payload when config loading fails."""
	return {
		"enabled": False,
		"disabled_reason": ERR_BOOKING_DISABLED,
		"title": DEFAULT_BOOKING_TITLE,
		"intro": "",
		"shop": {"name": "Our Service Center", "phone": "", "email": ""},
		"policy": {
			"max_packages_per_booking": DEFAULT_MAX_PACKAGES_PER_BOOKING,
			"require_email": False,
			"enable_waiter_preference": True,
			"horizon_days": 30,
			"min_lead_time_minutes": 120,
			"currency": "USD",
		},
		"vehicle_models": list(ALLOWED_VEHICLE_MODELS),
		"vehicle_year_min": VEHICLE_MIN_YEAR,
		"vehicle_year_max": now_datetime().year + 1,
		"honeypot_enabled": True,
		"honeypot_field": HONEYPOT_FIELD_NAME,
		"form_nonce": None,
		"portal_url": PORTAL_ROUTE,
	}


def get_context(context):
	"""Populate context for /book with sanitized boot config, CSRF token, and security headers."""
	context.no_cache = 1
	context.show_sidebar = False
	context.no_breadcrumbs = True
	context.full_width = True

	_apply_security_headers(context)

	try:
		boot_config = get_booking_boot_config(client_ip=get_client_ip())
	except Exception:
		frappe.log_error(
			title="Self-Serve Booking Page Boot Error",
			message=frappe.get_traceback(),
		)
		boot_config = _fallback_disabled_boot_config()

	csrf_token = ""
	try:
		csrf_token = frappe.sessions.get_csrf_token() or ""
	except Exception:
		csrf_token = ""

	context.boot_config = boot_config
	context.boot_config_json = _safe_json_for_script(boot_config)
	context.csrf_token = csrf_token
	context.title = boot_config.get("title") or _("Book Your Service Appointment")
	context.asset_version = _compute_asset_version()
	return context
