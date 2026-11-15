# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from typing import Optional

import frappe
from frappe.model.document import Document

from ism.ism.doctype.repair_vehicle.vehicle_queries import (
	_collect_vehicle_owners,
	_collect_vehicle_service_history,
	_summarize_deferred_services,
	build_vehicle_intelligence,
	build_vehicle_search_results,
	on_customer_trash,
	resolve_vehicle_identifier,
)
from ism.ism.doctype.repair_vehicle.vin_decoder import (
	ALL_WMI,
	BODY,
	DRIVE,
	FUEL,
	MODEL,
	PLANT,
	S_LEGACY,
	S_LEGACY_YEARS,
	WMI,
	YEAR,
	compute_vin_check_digit,
	decode_tesla_vin,
	get_derived_specs,
	get_hardware_generation,
	normalize_vin_string,
)

__all__ = [
	"ALL_WMI",
	"BODY",
	"DRIVE",
	"FUEL",
	"MODEL",
	"PLANT",
	"S_LEGACY",
	"S_LEGACY_YEARS",
	"WMI",
	"YEAR",
	"RepairVehicle",
	"_collect_vehicle_owners",
	"_collect_vehicle_service_history",
	"_summarize_deferred_services",
	"build_vehicle_intelligence",
	"build_vehicle_search_results",
	"compute_vin_check_digit",
	"decode_tesla_vin",
	"get_derived_specs",
	"get_hardware_generation",
	"normalize_vin_string",
	"on_customer_trash",
	"resolve_vehicle_identifier",
]


class RepairVehicle(Document):
	def before_insert(self):
		self.normalize_vin()

	def validate(self):
		if self.vin and getattr(self, "_normalized_vin_done", None) != self.vin:
			self.normalize_vin()
		self.decode_vin()
		self.sync_customers()

	def normalize_vin(self):
		if self.vin:
			normalized = normalize_vin_string(self.vin)
			if self.vin != normalized:
				self.vin = normalized
			self._normalized_vin_done = self.vin
		else:
			self._normalized_vin_done = None

	def decode_vin(self):
		if not self.vin or len(self.vin) != 17:
			self.clear_vin_fields()
			return

		decoded = decode_tesla_vin(self.vin)
		if not decoded["is_tesla_wmi"]:
			self.clear_vin_fields()
			self.is_valid_vin = 0
			frappe.throw(
				frappe._(
					'"{0}" is not a recognized Tesla manufacturer code. Only Tesla VINs are supported.'
				).format(decoded["wmi"])
			)

		for field in (
			"is_valid_vin", "manufacturer", "model", "model_year", "body_type",
			"trim", "drivetrain", "battery_type", "drive_unit",
			"assembly_plant", "production_sequence", "autopilot_hardware",
		):
			setattr(self, field, decoded.get(field))

	def sync_customers(self):
		"""Sync top-level primary customer with child customers table."""
		candidate_customers = {row.customer for row in (self.customers or []) if row.customer}
		if self.customer:
			candidate_customers.add(self.customer)

		existing_customers = set()
		if candidate_customers:
			existing_customers = set(
				frappe.get_all(
					"Customer",
					filters={"name": ["in", list(candidate_customers)]},
					pluck="name",
				)
			)

		if self.customers:
			self.customers = [
				row for row in self.customers if row.customer and row.customer in existing_customers
			]

		if self.customer and self.customer not in existing_customers:
			self.customer = None

		if not self.is_new() and not self.has_value_changed("customer"):
			primary_child = next((row.customer for row in (self.customers or []) if row.is_primary), None)
			if primary_child and primary_child != self.customer:
				self.customer = primary_child

		if self.customer:
			existing = {row.customer for row in (self.customers or [])}
			if self.customer not in existing:
				self.append("customers", {
					"customer": self.customer,
					"is_primary": 1 if not self.customers else 0,
				})

		if self.customers:
			if not self.customer:
				primary = next((row.customer for row in self.customers if row.is_primary), None)
				self.customer = primary or self.customers[0].customer
			for row in self.customers:
				row.is_primary = 1 if row.customer == self.customer else 0

	def add_customer(self, customer: str, is_primary: bool = False, relationship: Optional[str] = None):
		"""Link or update a customer on this vehicle."""
		if not customer or not frappe.db.exists("Customer", customer):
			frappe.throw(frappe._("Customer '{0}' does not exist.").format(customer))

		existing = {row.customer: row for row in (self.customers or [])}
		changed = False
		if customer not in existing:
			self.append("customers", {
				"customer": customer,
				"is_primary": 1 if (is_primary or not self.customers) else 0,
				"relationship": relationship,
			})
			if is_primary or not self.customer:
				self.customer = customer
			changed = True
		else:
			row = existing[customer]
			if relationship is not None and row.relationship != relationship:
				row.relationship = relationship
				changed = True
			if is_primary and self.customer != customer:
				self.customer = customer
				changed = True

		if changed:
			self.check_permission("write")
			self.save()

	def clear_vin_fields(self):
		fields = [
			"is_valid_vin", "manufacturer", "model", "model_year", "body_type",
			"trim", "drivetrain", "battery_type", "drive_unit",
			"assembly_plant", "production_sequence", "autopilot_hardware",
		]
		for f in fields:
			setattr(self, f, None)
