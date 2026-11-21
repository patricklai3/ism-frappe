"""Server-side deterministic Tesla-offset parts pricing engine (internal-only)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import quote_plus

import frappe

DEFAULT_CURRENCY = "CAD"
DEFAULT_TESLA_SEARCH_URL_TEMPLATE = "https://parts.tesla.com/en-CA/find-part?searchTerm={query}"
DEFAULT_CONDITION_DISCOUNT_OVERRIDES: dict[str, float | None] = {
	"New": None,
	"Used": None,
	"Reconditioned": None,
}


@dataclass(frozen=True)
class ServerPricingConfig:
	"""Internal parts pricing parameters loaded from ISM Settings with parts_pricing.json defaults."""

	currency: str = DEFAULT_CURRENCY
	tesla_search_url_template: str = DEFAULT_TESLA_SEARCH_URL_TEMPLATE
	baseline_discount_pct: float = 30.0
	condition_discount_overrides: dict[str, float | None] = field(
		default_factory=lambda: dict(DEFAULT_CONDITION_DISCOUNT_OVERRIDES)
	)
	small_ticket_max_tesla_price: float = 20.0
	small_ticket_discount_pct: float = 0.0
	min_sell_price: float = 5.0
	min_cost_markup_pct: float = 25.0
	min_cost_margin_dollars: float = 5.0
	fallback_cost_markup_pct: float = 40.0
	rounding_increment: float = 0.01

	def validate(self) -> None:
		"""Validate numeric bounds so invalid config values fail loudly."""
		for name, val in (
			("baseline_discount_pct", self.baseline_discount_pct),
			("small_ticket_discount_pct", self.small_ticket_discount_pct),
		):
			if not (0.0 <= val < 100.0):
				raise ValueError(f"{name} must be in [0, 100), got {val}")

		for cond, override in self.condition_discount_overrides.items():
			if override is not None and not (0.0 <= override < 100.0):
				raise ValueError(
					f"condition_discount_overrides[{cond!r}] must be in [0, 100), got {override}"
				)

		for name, val in (
			("small_ticket_max_tesla_price", self.small_ticket_max_tesla_price),
			("min_sell_price", self.min_sell_price),
			("min_cost_markup_pct", self.min_cost_markup_pct),
			("min_cost_margin_dollars", self.min_cost_margin_dollars),
			("fallback_cost_markup_pct", self.fallback_cost_markup_pct),
		):
			if val < 0.0:
				raise ValueError(f"{name} must be >= 0, got {val}")

		if self.rounding_increment <= 0.0:
			raise ValueError(f"rounding_increment must be > 0, got {self.rounding_increment}")
		if "{query}" not in self.tesla_search_url_template:
			raise ValueError("tesla_search_url_template must include '{query}' placeholder.")


def _get_float_setting(settings: Any, fieldname: str, default: float, *, allow_zero: bool = True) -> float:
	if settings is None:
		return default
	val = getattr(settings, fieldname, None)
	if val is None or val == "":
		return default
	num = float(val)
	if not allow_zero and num == 0.0:
		return default
	return num


def _parse_condition_overrides(settings: Any) -> dict[str, float | None]:
	if settings is None:
		return dict(DEFAULT_CONDITION_DISCOUNT_OVERRIDES)
	raw = getattr(settings, "parts_pricing_condition_overrides_json", None)
	if not raw:
		return dict(DEFAULT_CONDITION_DISCOUNT_OVERRIDES)
	parsed = json.loads(raw) if isinstance(raw, str) else raw
	if not isinstance(parsed, dict):
		raise ValueError("parts_pricing_condition_overrides_json must be a JSON object.")
	return {str(k): (float(v) if v is not None else None) for k, v in parsed.items()}


def load_server_pricing_config(settings: Any = None) -> ServerPricingConfig:
	"""Load and validate ServerPricingConfig defensively from ISM Settings."""
	if settings is None and getattr(frappe, "db", None):
		try:
			settings = frappe.get_cached_doc("ISM Settings")
		except Exception:
			settings = None

	raw_currency = getattr(settings, "currency", None) if settings is not None else None
	currency = str(raw_currency).strip() if raw_currency else DEFAULT_CURRENCY

	raw_tpl = getattr(settings, "parts_pricing_tesla_search_url_template", None) if settings is not None else None
	tesla_tpl = str(raw_tpl).strip() if raw_tpl and str(raw_tpl).strip() else DEFAULT_TESLA_SEARCH_URL_TEMPLATE

	cfg = ServerPricingConfig(
		currency=currency or DEFAULT_CURRENCY,
		tesla_search_url_template=tesla_tpl,
		baseline_discount_pct=_get_float_setting(settings, "parts_pricing_baseline_discount_pct", 30.0),
		condition_discount_overrides=_parse_condition_overrides(settings),
		small_ticket_max_tesla_price=_get_float_setting(
			settings, "parts_pricing_small_ticket_max_tesla_price", 20.0
		),
		small_ticket_discount_pct=_get_float_setting(
			settings, "parts_pricing_small_ticket_discount_pct", 0.0
		),
		min_sell_price=_get_float_setting(settings, "parts_pricing_min_sell_price", 5.0),
		min_cost_markup_pct=_get_float_setting(settings, "parts_pricing_min_cost_markup_pct", 25.0),
		min_cost_margin_dollars=_get_float_setting(settings, "parts_pricing_min_cost_margin_dollars", 5.0),
		fallback_cost_markup_pct=_get_float_setting(settings, "parts_pricing_fallback_cost_markup_pct", 40.0),
		rounding_increment=_get_float_setting(
			settings, "parts_pricing_rounding_increment", 0.01, allow_zero=False
		),
	)
	cfg.validate()
	return cfg


def build_tesla_part_search_url(query: str, config: ServerPricingConfig | None = None) -> str:
	"""Build a Tesla Canada parts catalog search URL for a part number or part name."""
	cleaned = (query or "").strip()
	if not cleaned:
		raise ValueError("Search query cannot be empty.")
	cfg = config or load_server_pricing_config()
	return cfg.tesla_search_url_template.format(query=quote_plus(cleaned))


def _coerce_positive_or_none(value: float | None, label: str) -> float | None:
	if value is None:
		return None
	num = float(value)
	if num < 0.0:
		raise ValueError(f"{label} cannot be negative (got {num}).")
	return num if num > 0.0 else None


def _round_to_increment(value: float, increment: float) -> float:
	inc_dec = Decimal(str(increment))
	val_dec = Decimal(str(value))
	rounded = (val_dec / inc_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * inc_dec
	return float(rounded)


def _compute_margin(sell_price: float, cost: float | None) -> tuple[float | None, float | None]:
	if cost is None or cost <= 0.0:
		return None, None
	margin_dollars = round(sell_price - cost, 2)
	margin_pct = round(((sell_price - cost) / cost) * 100.0, 2)
	return margin_dollars, margin_pct


def calculate_part_price(
	*,
	tesla_oem_price: float | None = None,
	cost: float | None = None,
	part_source: str = "Aftermarket",
	part_condition: str = "New",
	override_rate: float | None = None,
	part_number: str | None = None,
	part_name: str | None = None,
	config: ServerPricingConfig | None = None,
) -> dict[str, Any]:
	"""Calculate internal customer selling price in CAD using Tesla offset and floor rules."""
	cfg = config or load_server_pricing_config()
	cfg.validate()
	tesla_price = _coerce_positive_or_none(tesla_oem_price, "tesla_oem_price")
	purchase_cost = _coerce_positive_or_none(cost, "cost")
	explicit_override = _coerce_positive_or_none(override_rate, "override_rate")

	norm_source = "OEM" if str(part_source or "").strip().upper().startswith("OEM") else "Aftermarket"
	norm_condition = str(part_condition or "New").strip() or "New"

	search_query = (part_number or "").strip() or (part_name or "").strip()
	tesla_search_url = build_tesla_part_search_url(search_query, cfg) if search_query else None
	warnings: list[str] = []

	if explicit_override is not None:
		sell_price = _round_to_increment(explicit_override, cfg.rounding_increment)
		rule_applied = "manual_override"
	elif tesla_price is None and purchase_cost is None:
		raise ValueError("Either tesla_oem_price or cost must be greater than zero.")
	elif tesla_price is None:
		assert purchase_cost is not None
		raw_sell = max(
			purchase_cost
			+ max(purchase_cost * (cfg.fallback_cost_markup_pct / 100.0), cfg.min_cost_margin_dollars),
			cfg.min_sell_price,
		)
		sell_price = _round_to_increment(raw_sell, cfg.rounding_increment)
		rule_applied = "cost_plus_fallback"
		warnings.append("Tesla OEM price unavailable — cost-plus fallback applied.")
	else:
		cond_override = cfg.condition_discount_overrides.get(norm_condition)
		if tesla_price <= cfg.small_ticket_max_tesla_price:
			tesla_based = tesla_price * (1.0 - cfg.small_ticket_discount_pct / 100.0)
			base_rule = "small_ticket_match"
		elif cond_override is not None:
			tesla_based = tesla_price * (1.0 - cond_override / 100.0)
			base_rule = "condition_override"
		else:
			tesla_based = tesla_price * (1.0 - cfg.baseline_discount_pct / 100.0)
			base_rule = "baseline_offset"

		if tesla_based < cfg.min_sell_price:
			tesla_based = cfg.min_sell_price
			base_rule = "min_sell_floor"

		if purchase_cost is not None:
			cost_floor = purchase_cost + max(
				purchase_cost * (cfg.min_cost_markup_pct / 100.0),
				cfg.min_cost_margin_dollars,
			)
			if cost_floor > tesla_based:
				raw_sell = cost_floor
				rule_applied = "cost_floor"
			else:
				raw_sell = tesla_based
				rule_applied = base_rule
		else:
			raw_sell = tesla_based
			rule_applied = base_rule
			warnings.append("Cost unknown — verify supplier cost before ordering.")

		sell_price = _round_to_increment(raw_sell, cfg.rounding_increment)

	above_tesla = bool(tesla_price is not None and sell_price > tesla_price)
	if above_tesla and tesla_price is not None:
		warnings.append(
			f"Calculated price (${sell_price:.2f}) exceeds Tesla OEM price (${tesla_price:.2f}) to protect margin."
		)

	margin_dollars, margin_pct = _compute_margin(sell_price, purchase_cost)
	return {
		"internal_only": True,
		"currency": cfg.currency,
		"sell_price": sell_price,
		"rule_applied": rule_applied,
		"pricing_regime": rule_applied,
		"tesla_oem_price": tesla_price,
		"cost": purchase_cost,
		"part_source": norm_source,
		"part_condition": norm_condition,
		"margin_dollars": margin_dollars,
		"margin_pct": margin_pct,
		"above_tesla": above_tesla,
		"warnings": warnings,
		"tesla_search_url": tesla_search_url,
	}
