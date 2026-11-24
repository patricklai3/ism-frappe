# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.utils import flt, nowdate

from ism.api.parts import get_default_warehouse
from ism.ism.doctype.repair_order.billing import _resolve_paid_to_account
from ism.tests.base import ISMTestCase
from ism.tests.constants import TEST_PREFIX


class TestBillingAndDeposits(ISMTestCase):
	"""Comprehensive tests for RO deposits, split tenders, lifecycle hooks, stock guard, and pricing parity."""

	def setUp(self):
		super().setUp()
		self.customer = self.create_test_customer()
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)

	def _create_approved_ro(self, services=None, odometer_in=40000, tax_rate=13.0, **extra):
		ro_dict = {
			"doctype": "Repair Order",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"odometer_in": odometer_in,
			"approval_status": "Approved",
			"status": "Work Completed",
			"tax_rate": tax_rate,
			"services": services
			or [
				{
					"service_title": "Brake Inspection & Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
				}
			],
		}
		ro_dict.update(extra)
		ro = frappe.get_doc(ro_dict).insert(ignore_permissions=True)
		self.track(ro)
		return ro

	def _receive_stock(self, item_code: str, qty: float, warehouse: str, company: str, rate: float = 25.0):
		se = frappe.get_doc({
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": company,
			"items": [
				{
					"item_code": item_code,
					"qty": qty,
					"t_warehouse": warehouse,
					"basic_rate": rate,
				}
			],
		}).insert(ignore_permissions=True)
		se.submit()
		self.track(se)
		return se

	def _get_bin_qty(self, item_code: str, warehouse: str) -> float:
		return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))

	def test_record_deposit_validation_and_checkout_reconciliation(self):
		"""Upfront deposits survive reload, isolate per RO, and reconcile onto Sales Invoice at checkout."""
		ro = self._create_approved_ro(
			services=[
				{
					"service_title": "Diagnostic Service",
					"status": "Approved",
					"labor_hours": 2.0,
					"labor_rate": 100.0,
				}
			]
		)
		# $200 + 13% HST = $226.00
		self.assertEqual(flt(ro.grand_total, 2), 226.0)

		# Validation guards
		for bad_amount in (0, -25, True, "bad", None):
			with self.assertRaises(frappe.ValidationError):
				ro.record_deposit(amount=bad_amount, payment_method="Cash")

		with self.assertRaises(frappe.ValidationError):
			ro.record_deposit(amount=50, payment_method="NonExistentPayMethod")

		# Record two deposits against RO-1
		dep1 = ro.record_deposit(amount=60.0, payment_method="Cash", reference_no="CASH-DEP-1")
		self.track("Payment Entry", dep1["payment_entry"])
		self.assertEqual(dep1["status"], "success")
		self.assertEqual(dep1["deposit_amount"], 60.0)
		self.assertEqual(dep1["paid_amount"], 60.0)
		self.assertEqual(dep1["outstanding_amount"], 166.0)
		self.assertEqual(dep1["payment_status"], "Partially Paid")

		dep2 = ro.record_deposit(amount=40.0, payment_method="Debit / Interac", reference_no="INT-DEP-2")
		self.track("Payment Entry", dep2["payment_entry"])
		self.assertEqual(dep2["deposit_amount"], 100.0)
		self.assertEqual(dep2["paid_amount"], 100.0)
		self.assertEqual(dep2["outstanding_amount"], 126.0)
		self.assertEqual(dep2["payment_status"], "Partially Paid")

		# Create a second RO for the SAME customer and put a separate deposit on it
		ro_other = self._create_approved_ro()
		dep_other = ro_other.record_deposit(amount=50.0, payment_method="Cash", reference_no="OTHER-DEP")
		self.track("Payment Entry", dep_other["payment_entry"])

		# Reload primary RO from DB (even if deposit_amount column is unmigrated) and checkout
		ro_reloaded = frappe.get_doc("Repair Order", ro.name)
		self.assertEqual(flt(ro_reloaded.paid_amount, 2), 100.0)

		chk = ro_reloaded.process_payment_and_release(payment_method="Cash", odometer_out=40015)
		if chk.get("sales_invoice"):
			self.track("Sales Invoice", chk["sales_invoice"])
		for pe_name in chk.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		self.assertEqual(chk["status"], "success")
		self.assertEqual(chk["deposit_allocated"], 100.0)
		self.assertEqual(chk["paid_amount"], 226.0)
		self.assertEqual(chk["outstanding_amount"], 0.0)
		self.assertEqual(chk["payment_status"], "Paid")
		self.assertEqual(len(chk["payment_entries"]), 1)

		si = frappe.get_doc("Sales Invoice", chk["sales_invoice"])
		self.assertEqual(flt(si.total_advance, 2), 100.0)
		self.assertEqual(flt(si.outstanding_amount, 2), 0.0)

		# Verify ro_other's deposit remained completely untouched!
		other_pe = frappe.get_doc("Payment Entry", dep_other["payment_entry"])
		self.assertEqual(flt(other_pe.unallocated_amount, 2), 50.0)

		# Reject deposit after checkout / release
		ro_reloaded.reload()
		with self.assertRaises(frappe.ValidationError):
			ro_reloaded.record_deposit(amount=25.0, payment_method="Cash")

	def test_full_deposit_coverage_skips_checkout_payment_entry(self):
		"""When advance deposits cover 100% of grand_total, checkout creates no extra Payment Entry."""
		ro = self._create_approved_ro(tax_rate=0)
		self.assertEqual(flt(ro.grand_total, 2), 100.0)

		dep = ro.record_deposit(amount=100.0, payment_method="Cash")
		self.track("Payment Entry", dep["payment_entry"])
		self.assertEqual(dep["payment_status"], "Paid")
		self.assertEqual(dep["outstanding_amount"], 0.0)

		chk = ro.process_payment_and_release(odometer_out=40010)
		if chk.get("sales_invoice"):
			self.track("Sales Invoice", chk["sales_invoice"])
		self.assertEqual(chk["deposit_allocated"], 100.0)
		self.assertEqual(chk["payment_entries"], [])
		self.assertIsNone(chk["payment_entry"])
		self.assertEqual(chk["paid_amount"], 100.0)
		self.assertEqual(chk["outstanding_amount"], 0.0)
		self.assertEqual(chk["payment_status"], "Paid")

		si = frappe.get_doc("Sales Invoice", chk["sales_invoice"])
		self.assertEqual(flt(si.outstanding_amount, 2), 0.0)

	def test_split_tender_payments_and_validation(self):
		"""Checkout supports split-tender payments (with optional deposit) and rejects mismatched totals."""
		ro = self._create_approved_ro(
			services=[
				{
					"service_title": "Alignment",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 200.0,
				}
			],
			tax_rate=0,
		)
		dep = ro.record_deposit(amount=50.0, payment_method="Cash")
		self.track("Payment Entry", dep["payment_entry"])

		# Underpayment rejected
		with self.assertRaises(frappe.ValidationError):
			ro.process_payment_and_release(
				odometer_out=40020,
				payments=[{"payment_method": "Cash", "amount": 100.0}],
			)

		# Overpayment rejected
		with self.assertRaises(frappe.ValidationError):
			ro.process_payment_and_release(
				odometer_out=40020,
				payments=[{"payment_method": "Cash", "amount": 200.0}],
			)

		# Zero / negative tender rejected
		with self.assertRaises(frappe.ValidationError):
			ro.process_payment_and_release(
				odometer_out=40020,
				payments=[
					{"payment_method": "Cash", "amount": 150.0},
					{"payment_method": "Debit / Interac", "amount": 0},
				],
			)

		# Valid split tenders via JSON string: $50 deposit + $60 Cash + $90 Debit = $200
		split_json = json.dumps([
			{"payment_method": "Cash", "amount": 60.0, "reference_no": "SPLIT-CASH"},
			{"payment_method": "Debit / Interac", "amount": 90.0, "reference_no": "SPLIT-DEBIT"},
		])
		chk = ro.process_payment_and_release(odometer_out=40020, payments=split_json)
		if chk.get("sales_invoice"):
			self.track("Sales Invoice", chk["sales_invoice"])
		for pe_name in chk.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		self.assertEqual(len(chk["payment_entries"]), 2)
		self.assertEqual(chk["payment_entry"], chk["payment_entries"][0])
		self.assertEqual(chk["deposit_allocated"], 50.0)
		self.assertEqual(chk["paid_amount"], 200.0)
		self.assertEqual(chk["outstanding_amount"], 0.0)
		self.assertEqual(chk["payment_status"], "Paid")

		si = frappe.get_doc("Sales Invoice", chk["sales_invoice"])
		self.assertEqual(flt(si.outstanding_amount, 2), 0.0)

	def test_sales_invoice_cancel_and_external_payment_entry_hooks(self):
		"""Sales Invoice on_cancel unlinks/reopens RO; external Payment Entry hooks sync RO payment state."""
		ro = self._create_approved_ro(tax_rate=0)
		# Checkout unpaid first ($0) so SI exists with $100 outstanding
		chk = ro.process_payment_and_release(paid_amount=0, odometer_out=40030)
		si_name = chk["sales_invoice"]
		self.track("Sales Invoice", si_name)
		ro.reload()
		self.assertEqual(ro.payment_status, "Unpaid")
		self.assertEqual(flt(ro.outstanding_amount, 2), 100.0)

		# External partial Payment Entry submitted against si_name
		si = frappe.get_doc("Sales Invoice", si_name)
		paid_to = _resolve_paid_to_account(ro.company, "Cash")
		pe_ext = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": ro.customer,
			"company": ro.company,
			"posting_date": nowdate(),
			"mode_of_payment": "Cash",
			"paid_to": paid_to,
			"paid_amount": 40.0,
			"received_amount": 40.0,
			"references": [
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si.name,
					"total_amount": si.grand_total,
					"outstanding_amount": si.outstanding_amount,
					"allocated_amount": 40.0,
				}
			],
		}).insert(ignore_permissions=True)
		pe_ext.submit()
		self.track(pe_ext)

		ro.reload()
		self.assertEqual(flt(ro.paid_amount, 2), 40.0)
		self.assertEqual(flt(ro.outstanding_amount, 2), 60.0)
		self.assertEqual(ro.payment_status, "Partially Paid")

		# Cancel external Payment Entry -> reverts RO to Unpaid
		pe_ext.cancel()
		ro.reload()
		self.assertEqual(flt(ro.paid_amount, 2), 0.0)
		self.assertEqual(flt(ro.outstanding_amount, 2), 100.0)
		self.assertEqual(ro.payment_status, "Unpaid")

		# Cancel Sales Invoice -> unlinks RO, reopens to Work Completed, preserves any active deposit
		dep_ext = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": ro.customer,
			"company": ro.company,
			"posting_date": nowdate(),
			"mode_of_payment": "Cash",
			"paid_to": paid_to,
			"paid_amount": 30.0,
			"received_amount": 30.0,
			"custom_remarks": 1,
			"remarks": f"External deposit [RO:{ro.name}]",
		}).insert(ignore_permissions=True)
		dep_ext.submit()
		self.track(dep_ext)

		ro.reload()
		self.assertEqual(flt(ro.paid_amount, 2), 30.0)
		self.assertEqual(ro.payment_status, "Partially Paid")

		si.reload()
		si.cancel()
		ro.reload()
		self.assertIsNone(ro.sales_invoice)
		self.assertEqual(ro.status, "Work Completed")
		self.assertEqual(ro.vehicle_released, 0)
		self.assertEqual(flt(ro.paid_amount, 2), 30.0)
		self.assertEqual(flt(ro.outstanding_amount, 2), 70.0)
		self.assertEqual(ro.payment_status, "Partially Paid")

		# Re-checkout reopened RO succeeds and reconciles the $30 deposit
		chk2 = ro.process_payment_and_release(payment_method="Cash", odometer_out=40035)
		self.track("Sales Invoice", chk2["sales_invoice"])
		for pe_name in chk2.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)
		self.assertEqual(chk2["deposit_allocated"], 30.0)
		self.assertEqual(chk2["paid_amount"], 100.0)
		self.assertEqual(chk2["payment_status"], "Paid")

		# If unlink_payment_on_cancellation_of_invoice is enabled (or after cancelling checkout PE),
		# cancelling SI with an allocated advance PE must unlink SI and keep the deposit intact
		orig_unlink = frappe.db.get_single_value("Accounts Settings", "unlink_payment_on_cancellation_of_invoice")
		frappe.db.set_single_value("Accounts Settings", "unlink_payment_on_cancellation_of_invoice", 1)
		try:
			for pe_name in chk2.get("payment_entries") or []:
				frappe.get_doc("Payment Entry", pe_name).cancel()
			si2 = frappe.get_doc("Sales Invoice", chk2["sales_invoice"])
			si2.cancel()
		finally:
			frappe.db.set_single_value("Accounts Settings", "unlink_payment_on_cancellation_of_invoice", orig_unlink or 0)

		ro.reload()
		dep_ext.reload()
		self.assertIsNone(ro.sales_invoice)
		self.assertEqual(ro.status, "Work Completed")
		self.assertEqual(flt(dep_ext.unallocated_amount, 2), 30.0)
		self.assertEqual(flt(ro.paid_amount, 2), 30.0)
		self.assertEqual(flt(ro.outstanding_amount, 2), 70.0)
		self.assertEqual(ro.payment_status, "Partially Paid")

	def test_pre_checkout_stock_deficit_guard(self):
		"""Checkout throws actionable stock deficit error before creating Sales Invoice when Bin is short."""
		part_code = f"{TEST_PREFIX}STOCK_PAD_{frappe.generate_hash(length=5)}"
		self.create_test_item(part_code, is_stock_item=1, standard_rate=40.0)
		ro = self._create_approved_ro(
			tax_rate=0,
			services=[
				{
					"service_title": "Front Pads",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"parts": json.dumps([
						{"item_code": part_code, "item_name": "Brake Pad", "qty": 2, "rate": 40.0}
					]),
				},
				{
					"service_title": "Spare Pad",
					"status": "Approved",
					"labor_hours": 0.0,
					"labor_rate": 0.0,
					"parts": json.dumps([
						{"item_code": part_code, "item_name": "Brake Pad", "qty": 1, "rate": 40.0}
					]),
				},
			],
		)
		wh = get_default_warehouse(ro.company)
		self._receive_stock(part_code, qty=1.0, warehouse=wh, company=ro.company)

		with self.assertRaises(frappe.ValidationError) as ctx_err:
			ro.process_payment_and_release(payment_method="Cash", odometer_out=40040)

		err_msg = str(ctx_err.exception)
		self.assertIn(part_code, err_msg)
		self.assertIn(wh, err_msg)
		self.assertIn("required 3", err_msg)
		self.assertIn("available 1", err_msg)
		self.assertIn("shortfall 2", err_msg)
		ro.reload()
		self.assertFalse(ro.sales_invoice)

		# Receive remaining 2 units -> checkout now succeeds and consumes stock
		self._receive_stock(part_code, qty=2.0, warehouse=wh, company=ro.company)
		chk = ro.process_payment_and_release(payment_method="Cash", odometer_out=40040)
		self.track("Sales Invoice", chk["sales_invoice"])
		for pe_name in chk.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)
		self.assertEqual(self._get_bin_qty(part_code, wh), 0.0)

	def test_customer_supplied_part_skips_stock_deduction_and_warranty_tag(self):
		"""Customer Supplied parts never deduct warehouse stock, bill $0, and omit warranty disclosures."""
		part_code = f"{TEST_PREFIX}CUST_SUP_{frappe.generate_hash(length=5)}"
		self.create_test_item(part_code, is_stock_item=1, standard_rate=75.0)
		ro = self._create_approved_ro(
			tax_rate=0,
			services=[
				{
					"service_title": "Install Customer Rotor",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 120.0,
					"parts": json.dumps([
						{
							"item_code": part_code,
							"item_name": "Customer Rotor",
							"part_source": "Customer Supplied",
							"warranty_period": "24 Months",
							"qty": 2,
							"rate": 0.0,
						}
					]),
				}
			],
		)
		wh = get_default_warehouse(ro.company)
		self._receive_stock(part_code, qty=2.0, warehouse=wh, company=ro.company)

		chk = ro.process_payment_and_release(payment_method="Cash", odometer_out=40050)
		self.track("Sales Invoice", chk["sales_invoice"])
		for pe_name in chk.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		# Stock untouched in warehouse
		self.assertEqual(self._get_bin_qty(part_code, wh), 2.0)

		si = frappe.get_doc("Sales Invoice", chk["sales_invoice"])
		self.assertEqual(flt(si.grand_total, 2), 120.0)
		self.assertNotIn(part_code, [row.item_code for row in si.items])
		cust_rows = [row for row in si.items if row.item_code == "CUST-SUPPLIED-PART"]
		self.assertEqual(len(cust_rows), 1)
		self.assertEqual(flt(cust_rows[0].amount, 2), 0.0)
		self.assertNotIn("Warranty", cust_rows[0].description or "")

	def test_discounts_shop_supplies_and_environmental_fees_invoice_parity(self):
		"""Sales Invoice reflects service row discount, order discount, shop supplies, and environmental fees."""
		ro = self._create_approved_ro(
			tax_rate=13.0,
			services=[
				{
					"service_title": "Tire & Brake Package",
					"status": "Approved",
					"labor_hours": 2.0,
					"labor_rate": 100.0,
				}
			],
		)
		# Set Swarm 1 pricing fields directly in memory (works both before and after Swarm 1 migration)
		ro.services[0].discount_amount = 20.0
		ro.discount_amount = 10.0
		ro.shop_supplies_amount = 15.0
		ro.environmental_fee_amount = 5.0

		# Gross: 200 labor + 15 shop supplies + 5 env fee = 220
		# Discounts: 20 row + 10 order = 30
		# Net total: 220 - 30 = 190.00
		# HST 13%: 24.70
		# Grand total: 214.70
		chk = ro.process_payment_and_release(payment_method="Cash", odometer_out=40060)
		self.track("Sales Invoice", chk["sales_invoice"])
		for pe_name in chk.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		si = frappe.get_doc("Sales Invoice", chk["sales_invoice"])
		item_codes = [row.item_code for row in si.items]
		self.assertIn("SHOP-SUPPLIES", item_codes)
		self.assertIn("ENV-DISPOSAL-FEE", item_codes)
		self.assertEqual(flt(si.discount_amount, 2), 30.0)
		self.assertEqual(flt(si.net_total, 2), 190.0)
		self.assertEqual(flt(si.total_taxes_and_charges, 2), 24.70)
		self.assertEqual(flt(si.grand_total, 2), 214.70)
		self.assertEqual(flt(si.outstanding_amount, 2), 0.0)
		self.assertEqual(chk["paid_amount"], 214.70)
		self.assertEqual(chk["outstanding_amount"], 0.0)
		self.assertEqual(chk["payment_status"], "Paid")

	def test_checkout_multi_bin_auto_allocation_and_shared_item_parity(self):
		"""RO checkout auto-allocates Home Bin first, splits SI rows across bins, and avoids double-allocating shared items."""
		from ism.api.parts_stock import (
			adjust_bin_stock,
			create_bin_location,
			get_bin_dimension_meta,
			get_items_bin_stock_batch,
			set_item_default_bin,
		)
		from ism.setup import setup_inventory_dimensions

		setup_inventory_dimensions()
		dim_meta = get_bin_dimension_meta(use_cache=False)
		self.assertTrue(dim_meta["enabled"])
		dim_field = dim_meta["source_fieldname"]

		suffix = frappe.generate_hash(length=5).upper()
		part_code = f"{TEST_PREFIX}MBIN_PAD_{suffix}"
		self.create_test_item(part_code, is_stock_item=1, standard_rate=50.0)
		ro = self._create_approved_ro(
			tax_rate=0,
			services=[
				{
					"service_title": "Front Brake Assembly",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"parts": json.dumps([
						{"item_code": part_code, "item_name": "Multi-Bin Brake Pad", "qty": 3, "rate": 50.0}
					]),
				},
				{
					"service_title": "Rear Brake Assembly",
					"status": "Approved",
					"labor_hours": 0.5,
					"labor_rate": 100.0,
					"parts": json.dumps([
						{"item_code": part_code, "item_name": "Multi-Bin Brake Pad", "qty": 2, "rate": 50.0}
					]),
				},
			],
		)
		wh = get_default_warehouse(ro.company)
		bin_home = create_bin_location(bin_code=f"A2-{suffix}", warehouse=wh, zone="A")["bin_code"]
		bin_over = create_bin_location(bin_code=f"OF-{suffix}", warehouse=wh, zone="O", bin_type="Overflow")["bin_code"]
		self.track("Storage Bin", bin_home)
		self.track("Storage Bin", bin_over)
		set_item_default_bin(part_code, bin_home)

		# Stock 2 in Home Bin and 3 in Overflow Bin (total 5)
		se1 = adjust_bin_stock("receipt", [{"item_code": part_code, "qty": 2, "rate": 25.0}], bin_location=bin_home, warehouse=wh)
		se2 = adjust_bin_stock("receipt", [{"item_code": part_code, "qty": 3, "rate": 25.0}], bin_location=bin_over, warehouse=wh)
		self.track("Stock Entry", se1["stock_entry"])
		self.track("Stock Entry", se2["stock_entry"])

		chk = ro.process_payment_and_release(payment_method="Cash", odometer_out=40070)
		self.track("Sales Invoice", chk["sales_invoice"])
		for pe_name in chk.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		si = frappe.get_doc("Sales Invoice", chk["sales_invoice"])
		part_rows = [row for row in si.items if row.item_code == part_code]
		# Service 1 splits across bin_home (2) + bin_over (1); Service 2 consumes remaining bin_over (2)
		self.assertEqual(len(part_rows), 3)
		self.assertEqual([(getattr(r, dim_field), flt(r.qty)) for r in part_rows], [
			(bin_home, 2.0),
			(bin_over, 1.0),
			(bin_over, 2.0),
		])
		# Exact invoice total parity: labor ($100 + $50) + parts (5 * $50) = $400.00
		self.assertEqual(flt(si.grand_total, 2), 400.0)
		self.assertEqual(flt(ro.grand_total, 2), 400.0)

		# Verify zero negative or leftover bin balances
		post_bins = get_items_bin_stock_batch([part_code], warehouse=wh)[part_code]
		self.assertEqual(post_bins["bins"], [])
		self.assertEqual(post_bins["total_binned_qty"], 0.0)
		self.assertEqual(self._get_bin_qty(part_code, wh), 0.0)

	def test_checkout_legacy_unbinned_stock_and_pinned_bin_guard(self):
		"""Legacy unbinned stock checks out cleanly under validate_negative_stock=1; short pinned bin fails pre-checkout."""
		from ism.api.parts_stock import adjust_bin_stock, create_bin_location, get_items_bin_stock_batch
		from ism.setup import setup_inventory_dimensions

		setup_inventory_dimensions()
		suffix = frappe.generate_hash(length=5).upper()

		# 1. Legacy unbinned stock checkout succeeds cleanly
		legacy_code = f"{TEST_PREFIX}UNBIN_{suffix}"
		self.create_test_item(legacy_code, is_stock_item=1, standard_rate=30.0)
		ro_legacy = self._create_approved_ro(
			tax_rate=0,
			services=[
				{
					"service_title": "Legacy Wiper Install",
					"status": "Approved",
					"labor_hours": 0.5,
					"labor_rate": 100.0,
					"parts": json.dumps([{"item_code": legacy_code, "item_name": "Legacy Wiper", "qty": 2, "rate": 30.0}]),
				}
			],
		)
		wh = get_default_warehouse(ro_legacy.company)
		self._receive_stock(legacy_code, qty=2.0, warehouse=wh, company=ro_legacy.company)
		chk_leg = ro_legacy.process_payment_and_release(payment_method="Cash", odometer_out=40080)
		self.track("Sales Invoice", chk_leg["sales_invoice"])
		for pe_name in chk_leg.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)
		self.assertEqual(self._get_bin_qty(legacy_code, wh), 0.0)

		# 2. Pinned bin with insufficient stock fails pre-checkout BEFORE creating Sales Invoice
		pinned_code = f"{TEST_PREFIX}PIN_{suffix}"
		self.create_test_item(pinned_code, is_stock_item=1, standard_rate=45.0)
		bin_short = create_bin_location(bin_code=f"PA-{suffix}", warehouse=wh)["bin_code"]
		bin_full = create_bin_location(bin_code=f"PB-{suffix}", warehouse=wh)["bin_code"]
		self.track("Storage Bin", bin_short)
		self.track("Storage Bin", bin_full)

		se_s = adjust_bin_stock("receipt", [{"item_code": pinned_code, "qty": 1, "rate": 20.0}], bin_location=bin_short, warehouse=wh)
		se_f = adjust_bin_stock("receipt", [{"item_code": pinned_code, "qty": 4, "rate": 20.0}], bin_location=bin_full, warehouse=wh)
		self.track("Stock Entry", se_s["stock_entry"])
		self.track("Stock Entry", se_f["stock_entry"])

		ro_pin = self._create_approved_ro(
			tax_rate=0,
			services=[
				{
					"service_title": "Pinned Bin Service",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
					"parts": json.dumps([
						{
							"item_code": pinned_code,
							"item_name": "Pinned Rotor",
							"qty": 2,
							"rate": 45.0,
							"bin_location": bin_short,
						}
					]),
				}
			],
		)
		with self.assertRaises(frappe.ValidationError) as pin_err:
			ro_pin.process_payment_and_release(payment_method="Cash", odometer_out=40090)
		self.assertIn(bin_short, str(pin_err.exception))
		self.assertIn("shortfall 1", str(pin_err.exception))
		ro_pin.reload()
		self.assertFalse(ro_pin.sales_invoice)

		# Switch pin to bin_full (qty 4) -> checkout succeeds and deducts strictly from bin_full
		ro_pin.services[0].parts = json.dumps([
			{
				"item_code": pinned_code,
				"item_name": "Pinned Rotor",
				"qty": 2,
				"rate": 45.0,
				"bin_location": bin_full,
			}
		])
		ro_pin.save(ignore_permissions=True)
		chk_pin = ro_pin.process_payment_and_release(payment_method="Cash", odometer_out=40090)
		self.track("Sales Invoice", chk_pin["sales_invoice"])
		for pe_name in chk_pin.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		post = get_items_bin_stock_batch([pinned_code], warehouse=wh)[pinned_code]
		post_map = {b["bin_location"]: b["qty"] for b in post["bins"]}
		self.assertEqual(post_map[bin_short], 1.0)
		self.assertEqual(post_map[bin_full], 2.0)
