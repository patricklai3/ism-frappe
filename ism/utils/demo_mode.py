# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Shared helpers and static fallback catalog for Customer Journey Demo Mode."""

from __future__ import annotations

import re
from typing import Any, Sequence

import frappe
from frappe import _
from frappe.utils import cint, flt

DEMO_PACKAGE_ID_PREFIX: str = "demo-"

_DEMO_PACKAGES: tuple[dict[str, Any], ...] = (
	{
		"package_id": "demo-tire-rotation",
		"title": "Tire Rotation & Brake Inspection",
		"summary": "Four-wheel tire rotation, tread depth check, and brake pad/caliper inspection.",
		"applicable_model": "All Models",
		"applicable_generation": "",
		"is_mobile_capable": 1,
		"sort_order": 10,
		"price_display_mode": "Exact Price",
		"currency": "CAD",
		"price_min": 89.0,
		"price_max": 89.0,
		"price_display_label": "$89.00 CAD",
		"duration_minutes": 45,
	},
	{
		"package_id": "demo-cabin-filter",
		"title": "Cabin Air Filter & Wiper Service",
		"summary": "HEPA/cabin air filter replacement, evaporator refresh, and wiper blade check.",
		"applicable_model": "All Models",
		"applicable_generation": "",
		"is_mobile_capable": 1,
		"sort_order": 20,
		"price_display_mode": "Starting At",
		"currency": "CAD",
		"price_min": 125.0,
		"price_max": None,
		"price_display_label": "Starting at $125.00 CAD",
		"duration_minutes": 30,
	},
	{
		"package_id": "demo-diagnostic",
		"title": "General Vehicle Diagnostic",
		"summary": "Comprehensive alert scan, road test, and technician inspection.",
		"applicable_model": "All Models",
		"applicable_generation": "",
		"is_mobile_capable": 0,
		"sort_order": 30,
		"price_display_mode": "Exact Price",
		"currency": "CAD",
		"price_min": 140.0,
		"price_max": 140.0,
		"price_display_label": "$140.00 CAD",
		"duration_minutes": 60,
	},
	{
		"package_id": "demo-alignment",
		"title": "Four-Wheel Alignment Check",
		"summary": "Precision camber, caster, and toe measurement and adjustment.",
		"applicable_model": "All Models",
		"applicable_generation": "",
		"is_mobile_capable": 0,
		"sort_order": 40,
		"price_display_mode": "Estimate Range",
		"currency": "CAD",
		"price_min": 180.0,
		"price_max": 240.0,
		"price_display_label": "$180.00 CAD – $240.00 CAD",
		"duration_minutes": 60,
	},
)

_DEMO_PACKAGES_BY_ID: dict[str, dict[str, Any]] = {
	pkg["package_id"]: pkg for pkg in _DEMO_PACKAGES
}

_DEMO_PUBLIC_CARD_KEYS: tuple[str, ...] = (
	"package_id",
	"title",
	"summary",
	"applicable_model",
	"applicable_generation",
	"is_mobile_capable",
	"sort_order",
	"price_display_mode",
	"currency",
	"price_min",
	"price_max",
	"price_display_label",
)


def is_demo_mode(settings: Any = None) -> bool:
	"""Return True iff Customer Journey Demo Mode is enabled in ISM Settings."""
	if settings is not None:
		return bool(cint(getattr(settings, "customer_journey_demo_mode", 0) if not isinstance(settings, dict) else settings.get("customer_journey_demo_mode", 0)))

	try:
		if not frappe.db or not frappe.db.exists("DocType", "ISM Settings"):
			return False
		return bool(cint(frappe.db.get_single_value("ISM Settings", "customer_journey_demo_mode") or 0))
	except Exception:
		return False


def enable_demo_mode() -> dict[str, Any]:
	"""Enable Customer Journey Demo Mode in ISM Settings (for local/slot walkthroughs)."""
	settings = frappe.get_single("ISM Settings")
	settings.flags.ignore_self_serve_sms_warning = True
	settings.customer_journey_demo_mode = 1
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	return {"customer_journey_demo_mode": cint(settings.customer_journey_demo_mode)}


def is_demo_package_id(package_id: str | None) -> bool:
	"""Return True if package_id refers to one of the static demo fallback packages."""
	return isinstance(package_id, str) and package_id.strip() in _DEMO_PACKAGES_BY_ID


def _to_public_card(pkg: dict[str, Any]) -> dict[str, Any]:
	return {k: pkg[k] for k in _DEMO_PUBLIC_CARD_KEYS}


def list_demo_packages(
	*,
	vehicle_model: str | None = None,
	search: str | None = None,
) -> list[dict[str, Any]]:
	"""Return sanitized customer-facing fallback packages when Demo Mode is active."""
	del vehicle_model  # Demo fallback packages apply across all Tesla models.
	term = re.sub(r"\s+", " ", search.strip().lower()) if isinstance(search, str) and search.strip() else None
	cards: list[dict[str, Any]] = []
	for pkg in _DEMO_PACKAGES:
		if term and term not in f"{pkg['title']} {pkg['summary']}".lower():
			continue
		cards.append(_to_public_card(pkg))
	return cards


def compute_demo_package_entry(package_id: str) -> dict[str, Any]:
	"""Return (card, snapshot, duration_minutes) metadata for a single demo package ID."""
	pkg = _DEMO_PACKAGES_BY_ID.get(package_id)
	if not pkg:
		frappe.throw(_("Service Package '{0}' was not found.").format(package_id))
	card = _to_public_card(pkg)
	snapshot = {
		"package_id": card["package_id"],
		"title": card["title"],
		"price_display_mode": card["price_display_mode"],
		"currency": card["currency"],
		"price_min": card["price_min"],
		"price_max": card["price_max"],
		"price_display_label": card["price_display_label"],
	}
	return {
		"card": card,
		"snapshot": snapshot,
		"duration_minutes": cint(pkg["duration_minutes"]) or 45,
	}


def compute_demo_packages_requirements(package_ids: Sequence[str]) -> dict[str, Any]:
	"""Resolve demo-only package_ids into booking requirements matching bookable_catalog."""
	cards: list[dict[str, Any]] = []
	snapshot: list[dict[str, Any]] = []
	total_duration = 0
	for pkg_id in package_ids:
		entry = compute_demo_package_entry(pkg_id)
		cards.append(entry["card"])
		snapshot.append(entry["snapshot"])
		total_duration += flt(entry["duration_minutes"])

	return {
		"duration_minutes": max(15, int(total_duration or 45)),
		"required_tags": [],
		"items_summary": "\n".join(f"- {card['title']}" for card in cards),
		"booked_packages_snapshot": snapshot,
		"packages": cards,
	}
