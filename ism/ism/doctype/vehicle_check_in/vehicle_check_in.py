# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc


class VehicleCheckin(Document):
	def validate(self):
		self.validate_mileage()
		self.validate_single_active_check_in()
		self.handle_status_transition()

	def on_update(self):
		self.link_vehicle_to_customer()

	def validate_mileage(self):
		if self.intake_mileage is not None and self.intake_mileage < 0:
			frappe.throw(frappe._("Intake mileage cannot be negative."))

	def validate_single_active_check_in(self):
		if self.status == "Checked In" and self.vehicle:
			filters = [
				["vehicle", "=", self.vehicle],
				["status", "=", "Checked In"],
			]
			if not self.is_new():
				filters.append(["name", "!=", self.name])

			existing = frappe.db.get_value("Vehicle Check-in", filters, "name")
			if existing:
				frappe.throw(
					frappe._(
						"Vehicle {0} already has an active check-in record ({1}). Check out the vehicle before creating a new check-in."
					).format(self.vehicle, existing)
				)

	def handle_status_transition(self):
		if self.status == "Checked Out" and not self.checkout_date:
			self.checkout_date = frappe.utils.now_datetime()
		elif self.status == "Checked In":
			self.checkout_date = None

	def link_vehicle_to_customer(self):
		"""Link check-in customer to vehicle if not already associated."""
		if not (self.vehicle and self.customer):
			return

		# Check primary customer first (fast indexed lookup)
		if frappe.db.get_value("Repair Vehicle", self.vehicle, "customer") == self.customer:
			return

		# Check child customers table
		if frappe.db.exists("Repair Vehicle Customer", {"parent": self.vehicle, "customer": self.customer}):
			return

		if frappe.db.exists("Repair Vehicle", self.vehicle):
			vehicle_doc = frappe.get_doc("Repair Vehicle", self.vehicle)
			vehicle_doc.add_customer(self.customer)


@frappe.whitelist()
def get_vehicle_customers(vehicle: str):
	"""Return list of all customers linked to this vehicle."""
	if not isinstance(vehicle, str) or not vehicle.strip():
		return []

	frappe.has_permission("Repair Vehicle", "read", throw=True)

	if not frappe.db.exists("Repair Vehicle", vehicle):
		return []

	vehicle_doc = frappe.get_doc("Repair Vehicle", vehicle)
	customer_ids = []
	if vehicle_doc.customer:
		customer_ids.append(vehicle_doc.customer)
	for row in (vehicle_doc.customers or []):
		if row.customer:
			customer_ids.append(row.customer)

	if not customer_ids:
		return []

	customer_names = {
		c.name: c.customer_name
		for c in frappe.get_all(
			"Customer",
			filters={"name": ["in", customer_ids]},
			fields=["name", "customer_name"],
		)
	}

	customers = []
	seen = set()
	if vehicle_doc.customer:
		customers.append({
			"customer": vehicle_doc.customer,
			"customer_name": customer_names.get(vehicle_doc.customer, vehicle_doc.customer),
			"is_primary": 1,
		})
		seen.add(vehicle_doc.customer)

	for row in (vehicle_doc.customers or []):
		if row.customer not in seen:
			customers.append({
				"customer": row.customer,
				"customer_name": row.customer_name or customer_names.get(row.customer, row.customer),
				"is_primary": row.is_primary,
				"relationship": row.relationship,
			})
			seen.add(row.customer)

	return customers


@frappe.whitelist()
def check_out_vehicle(check_in_name: str):
	"""Mark vehicle check-in as checked out."""
	if not isinstance(check_in_name, str) or not check_in_name.strip():
		frappe.throw(frappe._("Invalid check_in_name parameter"))

	doc = frappe.get_doc("Vehicle Check-in", check_in_name)
	doc.check_permission("write")
	if doc.status == "Checked Out":
		frappe.throw(frappe._("Vehicle Check-in {0} is already checked out.").format(check_in_name))

	doc.status = "Checked Out"
	doc.checkout_date = frappe.utils.now_datetime()
	doc.save()
	return doc


@frappe.whitelist()
def make_quotation(source_name: str, target_doc=None):
	"""Create a draft Quotation from a Vehicle Check-in document."""
	def set_missing_values(source, target):
		target.quotation_to = "Customer"
		target.party_name = source.customer
		target.custom_vehicle = source.vehicle
		target.custom_vehicle_check_in = source.name
		target.custom_odometer_in = source.intake_mileage
		target.custom_customer_concern = source.customer_concern
		target.status = "Draft"

	doc = get_mapped_doc(
		"Vehicle Check-in",
		source_name,
		{
			"Vehicle Check-in": {
				"doctype": "Quotation",
				"field_map": {
					"customer": "party_name",
					"vehicle": "custom_vehicle",
					"name": "custom_vehicle_check_in",
					"intake_mileage": "custom_odometer_in",
					"customer_concern": "custom_customer_concern",
				},
				"field_no_map": ["status", "naming_series"],
			}
		},
		target_doc,
		set_missing_values,
	)
	return doc
