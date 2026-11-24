# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Compatibility facade re-exporting ISM install and migration tasks from ism.setup_tasks."""

from ism.setup_tasks import (
	DEFAULT_EQUIPMENT_TAGS,
	LEGACY_FIELDS_TO_REMOVE,
	STANDARD_INSPECTION_ITEMS,
	STANDARD_SCHEDULE_ENTRY_TYPES,
	_company_has_default_hst_setup,
	_ensure_custom_fields_present,
	_ensure_default_tax_rule,
	backfill_repair_vehicle_generations,
	backfill_service_package_models,
	cleanup_legacy_custom_fields,
	create_standard_inspection_template,
	ensure_company_13_percent_hst_setup,
	ensure_doctype_creation_enabled,
	ensure_ism_roles,
	get_or_create_default_purchase_tax_template,
	get_or_create_default_sales_tax_template,
	seed_equipment_tags,
	seed_schedule_entry_types,
	setup_default_ism_settings,
	setup_default_taxes_and_payments,
	setup_item_custom_fields,
	setup_parts_item_group,
	setup_procurement_custom_fields,
)


def _run_setup_tasks(full=False):
	"""Execute idempotent setup steps; full=True forces tax and custom field refresh."""
	ensure_ism_roles()
	seed_equipment_tags()
	seed_schedule_entry_types()
	setup_default_ism_settings()
	create_standard_inspection_template()
	ensure_doctype_creation_enabled()
	setup_default_taxes_and_payments(only_if_missing=not full)
	setup_parts_item_group()
	setup_item_custom_fields(force_update=full)
	setup_procurement_custom_fields(force_update=full)


def after_migrate():
	"""Run lean idempotent setup tasks for ISM on site migrations."""
	_run_setup_tasks(full=False)


def after_install():
	"""Run full installation tasks for ISM on fresh site installs."""
	_run_setup_tasks(full=True)


__all__ = [
	"DEFAULT_EQUIPMENT_TAGS",
	"LEGACY_FIELDS_TO_REMOVE",
	"STANDARD_INSPECTION_ITEMS",
	"STANDARD_SCHEDULE_ENTRY_TYPES",
	"_company_has_default_hst_setup",
	"_ensure_custom_fields_present",
	"_ensure_default_tax_rule",
	"_run_setup_tasks",
	"after_install",
	"after_migrate",
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
