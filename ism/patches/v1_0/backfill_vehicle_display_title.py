# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Idempotent post_model_sync migration backfilling human-friendly vehicle display fields.

Populates:
1. ``Repair Vehicle.display_title``
2. ``Repair Order.vehicle_display``
3. ``Shop Board Thread.vehicle_display``
"""

from __future__ import annotations

import frappe

from ism.utils.vehicle_display import format_vehicle_display


def execute() -> None:
	display_map: dict[str, str] = {}

	if frappe.db.table_exists("Repair Vehicle"):
		has_dt = frappe.db.has_column("Repair Vehicle", "display_title")
		fields = ["name", "vin", "model_year", "manufacturer", "model", "color", "license_plate"]
		if has_dt:
			fields.append("display_title")
		vehicles = frappe.get_all("Repair Vehicle", fields=fields, ignore_permissions=True)
		for veh in vehicles:
			display = format_vehicle_display(veh)[:140]
			display_map[veh["name"]] = display
			if has_dt and veh.get("display_title") != display:
				frappe.db.set_value(
					"Repair Vehicle",
					veh["name"],
					"display_title",
					display,
					update_modified=False,
				)

	if frappe.db.table_exists("Repair Order") and frappe.db.has_column("Repair Order", "vehicle_display"):
		ros = frappe.get_all(
			"Repair Order",
			fields=["name", "vehicle", "vehicle_display"],
			ignore_permissions=True,
		)
		for ro in ros:
			vehicle_name = ro.get("vehicle")
			if not vehicle_name:
				continue
			display = display_map.get(vehicle_name) or format_vehicle_display(vehicle_name)[:140]
			if ro.get("vehicle_display") != display:
				frappe.db.set_value(
					"Repair Order",
					ro["name"],
					"vehicle_display",
					display,
					update_modified=False,
				)

	if (
		frappe.db.table_exists("Shop Board Thread")
		and frappe.db.has_column("Shop Board Thread", "vehicle_display")
	):
		threads = frappe.get_all(
			"Shop Board Thread",
			fields=["name", "vehicle", "vehicle_display"],
			ignore_permissions=True,
		)
		for thread in threads:
			vehicle_name = thread.get("vehicle")
			if not vehicle_name:
				continue
			display = display_map.get(vehicle_name) or format_vehicle_display(vehicle_name)[:140]
			if thread.get("vehicle_display") != display:
				frappe.db.set_value(
					"Shop Board Thread",
					thread["name"],
					"vehicle_display",
					display,
					update_modified=False,
				)
