# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Idempotent post_model_sync migration backfilling vehicle and Service Package generations."""

from __future__ import annotations

from ism.setup_tasks.fields import (
	backfill_repair_vehicle_generations,
	backfill_service_package_models,
)


def execute():
	backfill_service_package_models()
	backfill_repair_vehicle_generations()
