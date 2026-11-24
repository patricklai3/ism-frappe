# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import flt

from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestPricingAndDiscounts(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.customer = self.create_test_customer()
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)

	def _create_ro(self, services=None, **kwargs):
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": 45000,
			"services": services or [],
			**kwargs,
		})
		return ro

	def test_server_side_package_auto_expansion_on_new_row(self):
		"""Adding a Service Package row with empty parts auto-expands parts, code, hours, and rate."""
		part = self.create_test_item(
			item_code=f"{TEST_PREFIX}PKG-PAD-1",
			is_stock_item=0,
			standard_rate=95.0,
		)
		if not frappe.db.exists("Equipment Tag", "Lift"):
			lift_tag = frappe.get_doc({"doctype": "Equipment Tag", "tag_name": "Lift"}).insert(ignore_permissions=True)
			self.track(lift_tag)
		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": f"{TEST_PREFIX}Auto Expand Brake Pkg",
			"correction_code": "33-01-02-00",
			"standard_labor_hours": 2.5,
			"hourly_rate": 150.0,
			"equipment_tags": [{"equipment_tag": "Lift"}],
			"items": [{"item_code": part.name, "qty": 2, "rate": 95.0}],
		}).insert(ignore_permissions=True)
		self.track(pkg)

		# 1. Initial insert with parts=None
		ro = self._create_ro(services=[{"service_package": pkg.name}])
		ro.insert(ignore_permissions=True)
		self.track(ro)

		srv = ro.services[0]
		self.assertEqual(srv.service_title, pkg.package_name)
		self.assertEqual(srv.correction_code, "33-01-02-00")
		self.assertEqual(flt(srv.labor_hours), 2.5)
		self.assertEqual(flt(srv.labor_rate), 150.0)
		self.assertEqual(flt(srv.labor_amount), 375.0)
		self.assertEqual(srv.required_equipment_tags, "Lift")
		self.assertEqual(ro.required_equipment_tags, "Lift")

		parts_data = json.loads(srv.parts)
		self.assertEqual(len(parts_data), 1)
		self.assertEqual(parts_data[0]["item_code"], part.name)
		self.assertEqual(flt(parts_data[0]["qty"]), 2.0)
		self.assertEqual(flt(parts_data[0]["amount"]), 190.0)
		self.assertEqual(flt(srv.parts_total), 190.0)
		self.assertEqual(flt(srv.total_amount), 565.0)

		# 2. Appending a row with parts="" on an existing RO also auto-expands
		ro.append("services", {"service_package": pkg.name, "parts": ""})
		ro.save(ignore_permissions=True)

		srv2 = ro.services[1]
		self.assertEqual(srv2.correction_code, "33-01-02-00")
		self.assertEqual(flt(srv2.labor_hours), 2.5)
		self.assertEqual(flt(srv2.labor_rate), 150.0)
		self.assertEqual(flt(srv2.parts_total), 190.0)
		self.assertEqual(flt(srv2.total_amount), 565.0)

	def test_package_auto_expansion_preserves_explicit_overrides(self):
		"""Explicit user overrides on code, hours, rate, or parts=[] must not be clobbered."""
		part = self.create_test_item(
			item_code=f"{TEST_PREFIX}OVR-PART-1",
			is_stock_item=0,
			standard_rate=60.0,
		)
		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": f"{TEST_PREFIX}Override Test Pkg",
			"correction_code": "10-20-30-00",
			"standard_labor_hours": 2.0,
			"hourly_rate": 140.0,
			"items": [{"item_code": part.name, "qty": 1, "rate": 60.0}],
		}).insert(ignore_permissions=True)
		self.track(pkg)

		custom_part = self.create_test_item(
			item_code=f"{TEST_PREFIX}CUST-OVR-PART",
			is_stock_item=0,
			standard_rate=40.0,
		)

		ro = self._create_ro(
			services=[
				# Row 0: explicit code, hours, and rate override, with parts unset -> expands parts only
				{
					"service_package": pkg.name,
					"service_title": "Custom Rate Override Job",
					"correction_code": "CUSTOM-99",
					"labor_hours": 1.25,
					"labor_rate": 185.0,
				},
				# Row 1: explicit parts="[]" -> does NOT expand package parts
				{
					"service_package": pkg.name,
					"service_title": "Labor Only Override",
					"labor_hours": 1.5,
					"labor_rate": 120.0,
					"parts": "[]",
				},
				# Row 2: explicit non-empty parts -> preserves custom parts
				{
					"service_package": pkg.name,
					"service_title": "Custom Parts Override",
					"labor_hours": 1.0,
					"labor_rate": 130.0,
					"parts": json.dumps([{"item_code": custom_part.name, "qty": 2, "rate": 40.0}]),
				},
			]
		)
		ro.insert(ignore_permissions=True)
		self.track(ro)

		row0, row1, row2 = ro.services
		self.assertEqual(row0.service_title, "Custom Rate Override Job")
		self.assertEqual(row0.correction_code, "CUSTOM-99")
		self.assertEqual(flt(row0.labor_hours), 1.25)
		self.assertEqual(flt(row0.labor_rate), 185.0)
		self.assertEqual(flt(row0.labor_amount), flt(1.25 * 185.0, 2))
		self.assertEqual(flt(row0.parts_total), 60.0)
		self.assertEqual(len(json.loads(row0.parts)), 1)

		# Subsequent save after parts are populated preserves explicit $0 labor_rate
		row0.labor_rate = 0.0
		ro.save(ignore_permissions=True)
		self.assertEqual(flt(ro.services[0].labor_rate), 0.0)
		self.assertEqual(flt(ro.services[0].labor_amount), 0.0)

		self.assertEqual(json.loads(row1.parts), [])
		self.assertEqual(flt(row1.parts_total), 0.0)
		self.assertEqual(flt(row1.total_amount), 180.0)

		row2_parts = json.loads(row2.parts)
		self.assertEqual(len(row2_parts), 1)
		self.assertEqual(row2_parts[0]["item_code"], custom_part.name)
		self.assertEqual(flt(row2.parts_total), 80.0)
		self.assertEqual(flt(row2.total_amount), 210.0)

	def test_package_auto_expansion_flat_rate_and_symmetrical(self):
		"""Auto-expansion handles flat-rate and symmetrical LH/RH packages cleanly."""
		lh_part = self.create_test_item(item_code=f"{TEST_PREFIX}SYM-LH-1", is_stock_item=0, standard_rate=110.0)
		rh_part = self.create_test_item(item_code=f"{TEST_PREFIX}SYM-RH-1", is_stock_item=0, standard_rate=120.0)

		flat_pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": f"{TEST_PREFIX}Flat Auto Pkg",
			"correction_code": "17-01-00-00",
			"is_flat_rate": 1,
			"standard_labor_hours": 0.5,
			"hourly_rate": 0.0,
			"service_rate": 220.0,
			"items": [{"item_code": lh_part.name, "qty": 1, "rate": 110.0}],
		}).insert(ignore_permissions=True)
		self.track(flat_pkg)

		sym_pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": f"{TEST_PREFIX}Control Arm (LH)",
			"correction_code": "31-01-LH",
			"rh_correction_code": "31-01-RH",
			"is_symmetrical": 1,
			"standard_labor_hours": 1.5,
			"hourly_rate": 140.0,
			"items": [
				{"item_code": lh_part.name, "qty": 1, "rate": 110.0, "side": "LH"},
				{"item_code": rh_part.name, "qty": 1, "rate": 120.0, "side": "RH"},
			],
		}).insert(ignore_permissions=True)
		self.track(sym_pkg)

		ro = self._create_ro(
			services=[
				{"service_package": flat_pkg.name},
				{"service_package": sym_pkg.name, "service_position": "RH"},
			]
		)
		ro.insert(ignore_permissions=True)
		self.track(ro)

		flat_row, rh_row = ro.services
		self.assertEqual(flat_row.is_flat_rate, 1)
		self.assertEqual(flt(flat_row.labor_amount), 220.0)
		self.assertEqual(flt(flat_row.parts_total), 110.0)
		self.assertEqual(flt(flat_row.total_amount), 330.0)

		self.assertEqual(rh_row.service_position, "RH")
		rh_parts = json.loads(rh_row.parts)
		self.assertEqual(len(rh_parts), 1)
		self.assertEqual(rh_parts[0]["item_code"], rh_part.name)
		self.assertEqual(flt(rh_row.parts_total), 120.0)

	def test_customer_supplied_parts_force_zero_price(self):
		"""Customer Supplied parts must force rate=0.0 and amount=0.0 while keeping parts normalized."""
		oem_part = self.create_test_item(item_code=f"{TEST_PREFIX}OEM-P1", is_stock_item=0, standard_rate=75.0)
		cust_part = self.create_test_item(item_code=f"{TEST_PREFIX}CUST-P1", is_stock_item=0, standard_rate=90.0)

		ro = self._create_ro(
			services=[
				{
					"service_title": "Brake Service w/ Customer Pads",
					"labor_hours": 1.5,
					"labor_rate": 120.0,
					"parts": json.dumps([
						{
							"item_code": oem_part.name,
							"item_name": "OEM Rotor",
							"part_source": "OEM",
							"qty": 2,
							"rate": 75.0,
						},
						{
							"item_code": cust_part.name,
							"item_name": "Customer Brake Pads",
							"part_source": "Customer Supplied",
							"qty": 3,
							"rate": 90.0,
							"amount": 270.0,
						},
					]),
				}
			]
		)
		ro.insert(ignore_permissions=True)
		self.track(ro)

		srv = ro.services[0]
		parts_data = json.loads(srv.parts)
		self.assertEqual(len(parts_data), 2)
		self.assertEqual(parts_data[0]["part_source"], "OEM")
		self.assertEqual(flt(parts_data[0]["rate"]), 75.0)
		self.assertEqual(flt(parts_data[0]["amount"]), 150.0)

		self.assertEqual(parts_data[1]["part_source"], "Customer Supplied")
		self.assertEqual(flt(parts_data[1]["qty"]), 3.0)
		self.assertEqual(flt(parts_data[1]["rate"]), 0.0)
		self.assertEqual(flt(parts_data[1]["amount"]), 0.0)
		self.assertTrue(bool(parts_data[1]["warehouse"]))

		self.assertEqual(flt(srv.parts_total), 150.0)
		self.assertEqual(flt(srv.total_amount), 330.0)

		# Also verify add_supplement preserves part_source="Customer Supplied" and forces $0.00
		ro.add_supplement(
			service_title="Supplement Customer Part",
			labor_hours=1.0,
			labor_rate=100.0,
			parts=[
				{"item_code": cust_part.name, "qty": 1, "rate": 90.0, "part_source": "Customer Supplied"}
			],
		)
		ro.reload()
		supp_parts = json.loads(ro.services[1].parts)
		self.assertEqual(supp_parts[0]["part_source"], "Customer Supplied")
		self.assertEqual(flt(supp_parts[0]["rate"]), 0.0)
		self.assertEqual(flt(supp_parts[0]["amount"]), 0.0)
		self.assertEqual(flt(ro.services[1].parts_total), 0.0)
		self.assertEqual(flt(ro.services[1].total_amount), 100.0)

	def test_row_discount_amount_and_zero_clamp(self):
		"""Row discount_amount reduces service total_amount and clamps at >= 0."""
		part = self.create_test_item(item_code=f"{TEST_PREFIX}DISC-P1", is_stock_item=0, standard_rate=60.0)
		ro = self._create_ro(
			services=[
				{
					"service_title": "Row Discount Service",
					"labor_hours": 1.0,
					"labor_rate": 140.0,
					"parts": json.dumps([{"item_code": part.name, "qty": 1, "rate": 60.0}]),
				},
				{
					"service_title": "Over-Discounted Service",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"parts": "[]",
				},
			]
		)
		ro.services[0].discount_amount = 50.0
		ro.services[1].discount_amount = 250.0
		ro.insert(ignore_permissions=True)
		self.track(ro)

		self.assertEqual(flt(ro.services[0].total_amount), 150.0)
		self.assertEqual(flt(ro.services[1].total_amount), 0.0)
		self.assertEqual(flt(ro.net_total), 150.0)

		# Negative row discount should be rejected
		ro.services[0].discount_amount = -10.0
		with self.assertRaises(frappe.ValidationError):
			ro.validate()

	def test_order_discount_shop_supplies_cap_and_environmental_fee(self):
		"""Verify shop_supplies_percentage, cap, environmental_fee_amount, order discount, tax, and clamp."""
		mock_settings = SimpleNamespace(shop_supplies_percentage=10.0, shop_supplies_cap=30.0)

		with patch("ism.ism.doctype.repair_order.pricing.frappe.get_single", return_value=mock_settings):
			# 1. Uncapped shop supplies (10% of 200 = 20 < cap 30) + env fee 15 - order discount 25 = 210
			ro = self._create_ro(
				services=[
					{
						"service_title": "Approved Alignment",
						"status": "Approved",
						"labor_hours": 2.0,
						"labor_rate": 100.0,
					},
					{
						"service_title": "Declined Wipers",
						"status": "Declined",
						"labor_hours": 1.0,
						"labor_rate": 100.0,
					},
				],
				approval_status="Approved",
			)
			ro.environmental_fee_amount = 15.0
			ro.discount_amount = 25.0
			ro.paid_amount = 100.0
			ro.insert(ignore_permissions=True)
			self.track(ro)

			self.assertEqual(flt(ro.shop_supplies_amount), 20.0)
			self.assertEqual(flt(ro.net_total), 210.0)
			expected_tax = flt(210.0 * 0.13, 2)
			expected_grand = flt(210.0 + expected_tax, 2)
			self.assertEqual(flt(ro.total_taxes_and_charges), expected_tax)
			self.assertEqual(flt(ro.grand_total), expected_grand)
			self.assertEqual(flt(ro.outstanding_amount), flt(expected_grand - 100.0, 2))

			# 2. Capped shop supplies (10% of 500 = 50, capped at 30)
			ro2 = self._create_ro(
				services=[
					{
						"service_title": "Major Battery Service",
						"labor_hours": 5.0,
						"labor_rate": 100.0,
					}
				]
			)
			ro2.insert(ignore_permissions=True)
			self.track(ro2)
			self.assertEqual(flt(ro2.shop_supplies_amount), 30.0)
			self.assertEqual(flt(ro2.net_total), 530.0)

			# 3. Order discount exceeding subtotal + fees clamps net_total, tax, grand_total, outstanding to 0
			ro2.discount_amount = 1000.0
			ro2.validate()
			self.assertEqual(flt(ro2.net_total), 0.0)
			self.assertEqual(flt(ro2.total_taxes_and_charges), 0.0)
			self.assertEqual(flt(ro2.grand_total), 0.0)
			self.assertEqual(flt(ro2.outstanding_amount), 0.0)

		# 4. Uncapped when shop_supplies_cap=0
		uncapped_settings = SimpleNamespace(shop_supplies_percentage=10.0, shop_supplies_cap=0.0)
		with patch("ism.ism.doctype.repair_order.pricing.frappe.get_single", return_value=uncapped_settings):
			ro3 = self._create_ro(
				services=[{"service_title": "Uncapped Service", "labor_hours": 5.0, "labor_rate": 100.0}]
			)
			ro3.insert(ignore_permissions=True)
			self.track(ro3)
			self.assertEqual(flt(ro3.shop_supplies_amount), 50.0)
			self.assertEqual(flt(ro3.net_total), 550.0)

		# 5. Disabled shop_supplies_percentage preserves manual ro.shop_supplies_amount
		disabled_settings = SimpleNamespace(shop_supplies_percentage=0.0, shop_supplies_cap=0.0)
		with patch("ism.ism.doctype.repair_order.pricing.frappe.get_single", return_value=disabled_settings):
			ro4 = self._create_ro(
				services=[{"service_title": "Manual Fee Service", "labor_hours": 1.0, "labor_rate": 100.0}]
			)
			ro4.shop_supplies_amount = 18.0
			ro4.environmental_fee_amount = 7.0
			ro4.validate()
			self.assertEqual(flt(ro4.shop_supplies_amount), 18.0)
			self.assertEqual(flt(ro4.net_total), 125.0)

			# Negative fees / order discount rejected
			ro4.environmental_fee_amount = -5.0
			with self.assertRaises(frappe.ValidationError):
				ro4.validate()
			ro4.environmental_fee_amount = 0.0
			ro4.discount_amount = -5.0
			with self.assertRaises(frappe.ValidationError):
				ro4.validate()
