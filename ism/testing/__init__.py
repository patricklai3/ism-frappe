# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Reusable ISM seeding, lifecycle builder, and simulation framework."""

from ism.testing.builder import SimBuilder, SimContext, VALID_RO_STAGES, seed_historical_telemetry
from ism.testing.catalog import (
	CATALOG_SEED,
	PACKAGE_EQUIPMENT_TAGS,
	compute_catalog_part_price,
	generate_oem_part_number,
	generate_synthetic_vin,
	get_master_manifest,
	seed_all,
	seed_catalog_manifest,
)
from ism.testing.manifest import MANIFEST_SCHEMA_VERSION, SimManifest
from ism.testing.monte_carlo import monte_carlo, seed_monte_carlo
from ism.testing.profiles import (
	VALID_PROFILES,
	build_profile,
	provision_agent_api_keys,
	seed_clean_master,
	seed_eval_standard,
	seed_profile,
	seed_swarm_crunch_day,
)

__all__ = [
	"CATALOG_SEED",
	"MANIFEST_SCHEMA_VERSION",
	"PACKAGE_EQUIPMENT_TAGS",
	"SimBuilder",
	"SimContext",
	"SimManifest",
	"VALID_PROFILES",
	"VALID_RO_STAGES",
	"build_profile",
	"compute_catalog_part_price",
	"generate_oem_part_number",
	"generate_synthetic_vin",
	"get_master_manifest",
	"monte_carlo",
	"provision_agent_api_keys",
	"seed_all",
	"seed_catalog_manifest",
	"seed_clean_master",
	"seed_eval_standard",
	"seed_historical_telemetry",
	"seed_monte_carlo",
	"seed_profile",
	"seed_swarm_crunch_day",
]
