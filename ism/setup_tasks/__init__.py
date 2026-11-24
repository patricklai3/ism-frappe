# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from ism.setup_tasks.fields import (
	LEGACY_FIELDS_TO_REMOVE,
	_ensure_custom_fields_present,
	backfill_repair_vehicle_generations,
	backfill_service_package_models,
	cleanup_legacy_custom_fields,
	ensure_doctype_creation_enabled,
	setup_item_custom_fields,
	setup_procurement_custom_fields,
)
from ism.setup_tasks.seed import (
	DEFAULT_EQUIPMENT_TAGS,
	STANDARD_INSPECTION_ITEMS,
	STANDARD_SCHEDULE_ENTRY_TYPES,
	create_standard_inspection_template,
	ensure_ism_roles,
	seed_equipment_tags,
	seed_schedule_entry_types,
	setup_default_ism_settings,
	setup_parts_item_group,
)
from ism.setup_tasks.taxes import (
	_company_has_default_hst_setup,
	_ensure_default_tax_rule,
	ensure_company_13_percent_hst_setup,
	get_or_create_default_purchase_tax_template,
	get_or_create_default_sales_tax_template,
	setup_default_taxes_and_payments,
)

__all__ = [
	"DEFAULT_EQUIPMENT_TAGS",
	"LEGACY_FIELDS_TO_REMOVE",
	"STANDARD_INSPECTION_ITEMS",
	"STANDARD_SCHEDULE_ENTRY_TYPES",
	"_company_has_default_hst_setup",
	"_ensure_custom_fields_present",
	"_ensure_default_tax_rule",
	"backfill_repair_vehicle_generations",
	"backfill_service_package_models",
	"cleanup_legacy_custom_fields",
	"create_standard_inspection_template",
	"ensure_company_13_percent_hst_setup",
	"ensure_doctype_creation_enabled",
	"ensure_ism_roles",
	"get_or_create_default_purchase_tax_template",
	"get_or_create_default_sales_tax_template",
	"seed_equipment_tags",
	"seed_schedule_entry_types",
	"setup_default_ism_settings",
	"setup_default_taxes_and_payments",
	"setup_item_custom_fields",
	"setup_parts_item_group",
	"setup_procurement_custom_fields",
]
