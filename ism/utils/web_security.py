# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Shared web page security headers and deterministic asset versioning helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import frappe

DEFAULT_SECURITY_HEADERS: dict[str, str] = {
	"X-Frame-Options": "DENY",
	"Content-Security-Policy": "frame-ancestors 'none'",
	"Referrer-Policy": "no-referrer",
	"Cache-Control": "no-store, private",
}

PORTAL_ASSET_PATHS: tuple[str, ...] = (
	"portal/portal.css",
	"portal/api.js",
	"portal/auth.js",
	"portal/quote_pin.js",
	"portal/reschedule.js",
	"portal/payments.js",
	"portal/invoices.js",
	"portal/app.js",
)

_PUBLIC_ROOT = Path(__file__).resolve().parent.parent / "public"


def apply_page_security_headers(
	context: Any,
	*,
	extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
	"""Enforce clickjacking, referrer, and no-store headers on Frappe response and page context."""
	headers = dict(DEFAULT_SECURITY_HEADERS)
	if extra_headers:
		headers.update(extra_headers)

	local_headers = getattr(frappe.local, "response_headers", None)
	if local_headers is None:
		frappe.local.response_headers = dict(headers)
	elif isinstance(local_headers, dict):
		local_headers.update(headers)
	else:
		for key, value in headers.items():
			local_headers[key] = value

	if context is not None:
		for attr in ("response_headers", "headers", "http_headers"):
			existing = context.get(attr)
			if isinstance(existing, dict):
				existing.update(headers)
			else:
				context[attr] = dict(headers)

	return dict(headers)


def compute_asset_version(asset_paths: Iterable[str]) -> str:
	"""Compute a deterministic 12-char cache-busting token from public asset mtimes and sizes."""
	digest = hashlib.sha256()
	found = False
	for raw_rel in asset_paths:
		rel = str(raw_rel or "").lstrip("/\\")
		if not rel:
			continue
		asset_path = _PUBLIC_ROOT / rel
		try:
			stat = asset_path.stat()
		except OSError:
			continue
		found = True
		digest.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size};".encode())
	return digest.hexdigest()[:12] if found else "1"
