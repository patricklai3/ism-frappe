# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Optional manual admin CLI sweep for residual _TEST_ISM_ records.

Usage:
    bench --site <site> execute ism.tests.cleanup.cleanup_all_test_data

Automated tests rely on Frappe savepoint rollback and ISMTestCase.track()
isolation against a clean Golden DB and never invoke this module.
"""

import frappe

from ism.tests.constants import (
	CANONICAL_EQUIPMENT_TAGS,
	CANONICAL_SERVICE_BAYS,
	STANDARD_SCHEDULE_ENTRY_TYPES,
	TEST_CUSTOM_DOCPERM_ROLES,
	TEST_PREFIX,
	TEST_VINS,
	TEST_VIN_PREFIX,
)


def _has_test_prefix(value) -> bool:
	return bool(value and isinstance(value, str) and value.startswith(TEST_PREFIX))


def _is_test_vin(vin) -> bool:
	if not vin:
		return False
	return vin in TEST_VINS or vin.startswith(TEST_VIN_PREFIX) or _has_test_prefix(vin)


def _is_test_customer_party(party, test_customers) -> bool:
	if not party:
		return False
	return party in test_customers or _has_test_prefix(party)


def _is_test_supplier_party(supplier, test_suppliers) -> bool:
	if not supplier:
		return False
	return supplier in test_suppliers or _has_test_prefix(supplier)


def _delete_doc_safe(doctype, name, log, errors):
	if not frappe.db.exists(doctype, name):
		return
	try:
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		log(f"Deleted test {doctype}: {name}")
	except frappe.DoesNotExistError:
		pass
	except Exception as e:
		msg = f"{doctype} {name}: {e}"
		errors.append(msg)
		log(f"Failed deleting test {msg}")


def _cancel_referencing_payments(reference_doctype, reference_name, log, errors):
	pe_refs = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": reference_doctype, "reference_name": reference_name},
		pluck="parent",
	)
	for pe_name in set(pe_refs):
		_delete_doc_safe("Payment Entry", pe_name, log, errors)


def cleanup_all_test_data(verbose: bool = True):
	"""Opt-in manual admin sweep that purges residual _TEST_ISM_ records in reverse FK order."""
	frappe.db.rollback()

	def log(msg):
		if verbose:
			print(msg)

	errors = []
	test_customers = _find_test_customers()
	test_suppliers = _find_test_suppliers()
	stub_customers_created = _ensure_stub_customers_for_orphaned_transactions(test_customers, log, errors)
	test_ros = _find_test_repair_orders(test_customers)

	_cleanup_payment_transactions(test_customers, test_ros, log, errors)
	_cleanup_payment_entries(test_customers, test_suppliers, log, errors)
	_cleanup_purchase_receipts(test_suppliers, test_ros, log, errors)
	_cleanup_purchase_invoices(test_suppliers, test_ros, log, errors)
	_cleanup_sales_invoices(test_customers, log, errors)
	_cleanup_stock_entries(test_ros, log, errors)
	_cleanup_purchase_orders(test_suppliers, test_ros, log, errors)
	_cleanup_rfqs(test_suppliers, log, errors)
	_cleanup_sales_orders(test_customers, log, errors)
	_cleanup_quotations(test_customers, log, errors)
	_cleanup_schedule_entries(test_customers, test_ros, log, errors)
	_cleanup_shop_health_snapshots(log, errors)
	_cleanup_shop_action_items(test_customers, test_ros, log, errors)
	_cleanup_repair_orders(test_ros, log, errors)
	_cleanup_vehicle_checkins(test_customers, log, errors)
	_cleanup_leave_applications(log, errors)
	_cleanup_repair_vehicles(test_customers, log, errors)
	_cleanup_customers(test_customers, stub_customers_created, log, errors)
	_cleanup_suppliers(test_suppliers, log, errors)
	_cleanup_service_bays(log, errors)
	_cleanup_equipment_tags(log, errors)
	_cleanup_schedule_entry_types(log, errors)
	_cleanup_inspection_templates(log, errors)
	_cleanup_service_packages(log, errors)
	_cleanup_items(log, errors)
	_cleanup_custom_docperms(log, errors)
	_cleanup_files(log, errors)

	frappe.db.commit()
	if errors:
		summary = f"cleanup_all_test_data completed with {len(errors)} error(s): " + "; ".join(errors)
		frappe.log_error(message="\n".join(errors), title="ISM cleanup_all_test_data errors")
		raise RuntimeError(summary)

	log("Cleanup of test data completed successfully.")


def _find_test_customers():
	return {c for c in frappe.get_all("Customer", pluck="name") if _has_test_prefix(c)}


def _find_test_suppliers():
	if not frappe.db.exists("DocType", "Supplier"):
		return set()
	return {s for s in frappe.get_all("Supplier", pluck="name") if _has_test_prefix(s)}


def _find_test_repair_orders(test_customers):
	if not frappe.db.exists("DocType", "Repair Order"):
		return set()
	test_ros = set()
	for ro in frappe.get_all("Repair Order", fields=["name", "customer", "vehicle"]):
		if _is_test_customer_party(ro.customer, test_customers) or _is_test_vin(ro.vehicle):
			test_ros.add(ro.name)
	return test_ros


def _ensure_stub_customers_for_orphaned_transactions(known_test_customers, log, errors):
	"""Recreate missing _TEST_ISM_ party customers temporarily so ERPNext cancellation succeeds."""
	stubs = set()
	for dt, party_field in [("Sales Invoice", "customer"), ("Payment Entry", "party"), ("Repair Order", "customer")]:
		if not frappe.db.exists("DocType", dt):
			continue
		for row in frappe.get_all(dt, fields=[party_field]):
			party = row.get(party_field)
			if not party or not _has_test_prefix(party):
				continue
			known_test_customers.add(party)
			if not frappe.db.exists("Customer", party):
				try:
					frappe.get_doc({
						"doctype": "Customer",
						"customer_name": party,
						"customer_type": "Individual",
					}).insert(ignore_permissions=True)
					stubs.add(party)
				except Exception as e:
					msg = f"Stub Customer {party}: {e}"
					errors.append(msg)
					log(f"Failed creating {msg}")
	return stubs


def _cleanup_payment_transactions(test_customers, test_ros, log, errors):
	if not frappe.db.exists("DocType", "ISM Payment Transaction"):
		return
	for tx in frappe.get_all(
		"ISM Payment Transaction",
		fields=["name", "customer", "repair_order"],
	):
		if _is_test_customer_party(tx.customer, test_customers) or (tx.repair_order and tx.repair_order in test_ros):
			if tx.repair_order and frappe.db.exists("Repair Order", tx.repair_order):
				if frappe.db.has_column("Repair Order", "deposit_request_transaction"):
					frappe.db.set_value(
						"Repair Order",
						tx.repair_order,
						"deposit_request_transaction",
						None,
						update_modified=False,
					)
			prev_force_delete = getattr(frappe.flags, "force_delete", False)
			frappe.flags.force_delete = True
			try:
				_delete_doc_safe("ISM Payment Transaction", tx.name, log, errors)
			finally:
				frappe.flags.force_delete = prev_force_delete


def _cleanup_payment_entries(test_customers, test_suppliers, log, errors):
	if not frappe.db.exists("DocType", "Payment Entry"):
		return
	for pe in frappe.get_all("Payment Entry", fields=["name", "party_type", "party"]):
		is_test = (
			(pe.party_type == "Supplier" and _is_test_supplier_party(pe.party, test_suppliers))
			or (pe.party_type != "Supplier" and _is_test_customer_party(pe.party, test_customers))
		)
		if is_test:
			_delete_doc_safe("Payment Entry", pe.name, log, errors)


def _doc_matches_test_ro(row, test_ros) -> bool:
	for field in ("custom_repair_order", "repair_order"):
		val = row.get(field)
		if val and val in test_ros:
			return True
	return False


def _cleanup_purchase_receipts(test_suppliers, test_ros, log, errors):
	if not frappe.db.exists("DocType", "Purchase Receipt"):
		return
	fields = ["name", "supplier", "title"]
	for col in ("custom_repair_order", "repair_order"):
		if frappe.db.has_column("Purchase Receipt", col):
			fields.append(col)
	for pr in frappe.get_all("Purchase Receipt", fields=fields):
		if (
			_is_test_supplier_party(pr.supplier, test_suppliers)
			or _has_test_prefix(pr.get("title"))
			or _doc_matches_test_ro(pr, test_ros)
		):
			_delete_doc_safe("Purchase Receipt", pr.name, log, errors)


def _cleanup_purchase_invoices(test_suppliers, test_ros, log, errors):
	if not frappe.db.exists("DocType", "Purchase Invoice"):
		return
	fields = ["name", "supplier", "title"]
	for col in ("custom_repair_order", "repair_order"):
		if frappe.db.has_column("Purchase Invoice", col):
			fields.append(col)
	for pi in frappe.get_all("Purchase Invoice", fields=fields):
		if (
			_is_test_supplier_party(pi.supplier, test_suppliers)
			or _has_test_prefix(pi.get("title"))
			or _doc_matches_test_ro(pi, test_ros)
		):
			_cancel_referencing_payments("Purchase Invoice", pi.name, log, errors)
			_delete_doc_safe("Purchase Invoice", pi.name, log, errors)


def _cleanup_sales_invoices(test_customers, log, errors):
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return
	for si in frappe.get_all("Sales Invoice", fields=["name", "customer"]):
		if _is_test_customer_party(si.customer, test_customers):
			_cancel_referencing_payments("Sales Invoice", si.name, log, errors)
			_delete_doc_safe("Sales Invoice", si.name, log, errors)


def _cleanup_stock_entries(test_ros, log, errors):
	if not frappe.db.exists("DocType", "Stock Entry"):
		return
	fields = ["name", "title", "from_warehouse", "to_warehouse"]
	for col in ("custom_repair_order", "repair_order"):
		if frappe.db.has_column("Stock Entry", col):
			fields.append(col)
	for se in frappe.get_all("Stock Entry", fields=fields):
		if (
			_has_test_prefix(se.get("title"))
			or _has_test_prefix(se.get("from_warehouse"))
			or _has_test_prefix(se.get("to_warehouse"))
			or _doc_matches_test_ro(se, test_ros)
		):
			_delete_doc_safe("Stock Entry", se.name, log, errors)


def _cleanup_purchase_orders(test_suppliers, test_ros, log, errors):
	if not frappe.db.exists("DocType", "Purchase Order"):
		return
	fields = ["name", "supplier", "title"]
	for col in ("custom_repair_order", "repair_order"):
		if frappe.db.has_column("Purchase Order", col):
			fields.append(col)
	for po in frappe.get_all("Purchase Order", fields=fields):
		if (
			_is_test_supplier_party(po.supplier, test_suppliers)
			or _has_test_prefix(po.get("title"))
			or _doc_matches_test_ro(po, test_ros)
		):
			if frappe.db.exists("DocType", "Purchase Receipt Item"):
				for pr_name in set(
					frappe.get_all("Purchase Receipt Item", filters={"purchase_order": po.name}, pluck="parent")
				):
					_delete_doc_safe("Purchase Receipt", pr_name, log, errors)
			if frappe.db.exists("DocType", "Purchase Invoice Item"):
				for pi_name in set(
					frappe.get_all("Purchase Invoice Item", filters={"purchase_order": po.name}, pluck="parent")
				):
					_cancel_referencing_payments("Purchase Invoice", pi_name, log, errors)
					_delete_doc_safe("Purchase Invoice", pi_name, log, errors)
			_delete_doc_safe("Purchase Order", po.name, log, errors)


def _cleanup_rfqs(test_suppliers, log, errors):
	if not frappe.db.exists("DocType", "Request for Quotation"):
		return
	rfq_names = set()
	if frappe.db.exists("DocType", "Request for Quotation Supplier"):
		for row in frappe.get_all("Request for Quotation Supplier", fields=["parent", "supplier"]):
			if _is_test_supplier_party(row.supplier, test_suppliers):
				rfq_names.add(row.parent)
	if frappe.db.has_column("Request for Quotation", "title"):
		for rfq in frappe.get_all("Request for Quotation", fields=["name", "title"]):
			if _has_test_prefix(rfq.get("title")):
				rfq_names.add(rfq.name)
	for rfq_name in rfq_names:
		_delete_doc_safe("Request for Quotation", rfq_name, log, errors)


def _cleanup_sales_orders(test_customers, log, errors):
	if not frappe.db.exists("DocType", "Sales Order"):
		return
	for so in frappe.get_all("Sales Order", fields=["name", "customer"]):
		if _is_test_customer_party(so.customer, test_customers):
			_delete_doc_safe("Sales Order", so.name, log, errors)


def _cleanup_quotations(test_customers, log, errors):
	if not frappe.db.exists("DocType", "Quotation"):
		return
	for qtn in frappe.get_all("Quotation", fields=["name", "party_name"]):
		if _is_test_customer_party(qtn.party_name, test_customers):
			_delete_doc_safe("Quotation", qtn.name, log, errors)


def _cleanup_schedule_entries(test_customers, test_ros, log, errors):
	"""Purge residual test Schedule Entry records before parent Repair Orders and Bays are deleted."""
	if not frappe.db.exists("DocType", "Schedule Entry"):
		return

	fields = [
		"name",
		"repair_order",
		"customer",
		"repair_vehicle",
		"service_bay",
		"entry_type",
		"assigned_technician",
		"provisional_customer_name",
		"provisional_vehicle_info",
		"title",
	]
	for se in frappe.get_all("Schedule Entry", fields=fields):
		is_test = (
			(se.repair_order and se.repair_order in test_ros)
			or _is_test_customer_party(se.customer, test_customers)
			or _is_test_vin(se.repair_vehicle)
			or _has_test_prefix(se.service_bay)
			or _has_test_prefix(se.entry_type)
			or _has_test_prefix(se.assigned_technician)
			or _has_test_prefix(se.provisional_customer_name)
			or _has_test_prefix(se.provisional_vehicle_info)
			or _has_test_prefix(se.title)
		)
		if is_test:
			_delete_doc_safe("Schedule Entry", se.name, log, errors)


def _cleanup_shop_health_snapshots(log, errors):
	if not frappe.db.exists("DocType", "Shop Health Snapshot"):
		return
	for shs in frappe.get_all("Shop Health Snapshot", fields=["name", "metric_key", "verdict"]):
		if _has_test_prefix(shs.get("metric_key")) or _has_test_prefix(shs.get("verdict")):
			_delete_doc_safe("Shop Health Snapshot", shs.name, log, errors)


def _unlink_and_delete_board_events(event_names, log, errors):
	names = [n for n in set(event_names or ()) if n]
	if not names or not frappe.db.exists("DocType", "Shop Board Event"):
		return
	prev_board_td = getattr(frappe.flags, "in_board_teardown", False)
	frappe.flags.in_board_teardown = True
	try:
		if (
			frappe.db.exists("DocType", "Shop Action Item")
			and frappe.db.has_column("Shop Action Item", "board_event")
		):
			for sai_name in frappe.get_all(
				"Shop Action Item",
				filters={"board_event": ["in", names]},
				pluck="name",
			):
				frappe.db.set_value(
					"Shop Action Item", sai_name, "board_event", None, update_modified=False
				)
		if frappe.db.has_column("Shop Board Event", "action_item"):
			for ev_name in names:
				if frappe.db.exists("Shop Board Event", ev_name):
					frappe.db.set_value(
						"Shop Board Event", ev_name, "action_item", None, update_modified=False
					)
		for child_ev in frappe.get_all(
			"Shop Board Event",
			filters={"parent_event": ["in", names]},
			pluck="name",
		):
			frappe.db.set_value(
				"Shop Board Event", child_ev, "parent_event", None, update_modified=False
			)
		for ev_name in names:
			_delete_doc_safe("Shop Board Event", ev_name, log, errors)
	finally:
		frappe.flags.in_board_teardown = prev_board_td


def _cleanup_shop_action_items(test_customers, test_ros, log, errors):
	if not frappe.db.exists("DocType", "Shop Action Item"):
		return
	fields = ["name"]
	optional_cols = [
		"title",
		"dedup_key",
		"repair_order",
		"reference_doctype",
		"reference_name",
		"board_event",
		"customer",
		"vehicle",
	]
	available_cols = [c for c in optional_cols if frappe.db.has_column("Shop Action Item", c)]
	for sai in frappe.get_all("Shop Action Item", fields=fields + available_cols):
		if (
			_has_test_prefix(sai.get("title"))
			or _has_test_prefix(sai.get("dedup_key"))
			or (sai.get("repair_order") and sai.get("repair_order") in test_ros)
			or (
				sai.get("reference_doctype") == "Repair Order"
				and sai.get("reference_name") in test_ros
			)
			or _is_test_customer_party(sai.get("customer"), test_customers)
			or _is_test_vin(sai.get("vehicle"))
		):
			if "board_event" in available_cols and sai.get("board_event"):
				frappe.db.set_value(
					"Shop Action Item", sai.name, "board_event", None, update_modified=False
				)
			if frappe.db.exists("DocType", "Shop Board Event"):
				event_names = set(
					frappe.get_all(
						"Shop Board Event",
						filters={
							"idempotency_key": [
								"in",
								[
									f"action_item:{sai.name}",
									f"action_card:{sai.name}",
									f"action_resolved:{sai.name}",
								],
							]
						},
						pluck="name",
					)
				)
				event_names.update(
					frappe.get_all(
						"Shop Board Event",
						filters={"idempotency_key": ["like", f"action_resolved:{sai.name}:%"]},
						pluck="name",
					)
				)
				if frappe.db.has_column("Shop Board Event", "action_item"):
					event_names.update(
						frappe.get_all(
							"Shop Board Event",
							filters={"action_item": sai.name},
							pluck="name",
						)
					)
				_unlink_and_delete_board_events(event_names, log, errors)
			_delete_doc_safe("Shop Action Item", sai.name, log, errors)


def _cleanup_repair_orders(test_ros, log, errors):
	for ro_name in sorted(test_ros):
		if not frappe.db.exists("Repair Order", ro_name):
			continue
		try:
			if frappe.db.exists("DocType", "Schedule Entry"):
				for se_name in frappe.get_all("Schedule Entry", filters={"repair_order": ro_name}, pluck="name"):
					_delete_doc_safe("Schedule Entry", se_name, log, errors)

			if frappe.db.exists("DocType", "Shop Board Event"):
				event_names = set(
					frappe.get_all("Shop Board Event", filters={"repair_order": ro_name}, pluck="name")
				)
				event_names.update(
					frappe.get_all("Shop Board Event", filters={"thread": f"RO:{ro_name}"}, pluck="name")
				)
				_unlink_and_delete_board_events(event_names, log, errors)

			if frappe.db.exists("DocType", "Shop Board Thread"):
				thread_names = set(
					frappe.get_all("Shop Board Thread", filters={"repair_order": ro_name}, pluck="name")
				)
				ro_key = f"RO:{ro_name}"
				if frappe.db.exists("Shop Board Thread", ro_key):
					thread_names.add(ro_key)
				for thread_name in thread_names:
					_delete_doc_safe("Shop Board Thread", thread_name, log, errors)

			doc = frappe.get_doc("Repair Order", ro_name)
			if doc.sales_invoice:
				if frappe.db.exists("Sales Invoice", doc.sales_invoice):
					_cancel_referencing_payments("Sales Invoice", doc.sales_invoice, log, errors)
					_delete_doc_safe("Sales Invoice", doc.sales_invoice, log, errors)
				doc.db_set("sales_invoice", None)
				doc.reload()

			_delete_doc_safe("Repair Order", ro_name, log, errors)
		except frappe.DoesNotExistError:
			pass
		except Exception as e:
			msg = f"Repair Order {ro_name}: {e}"
			errors.append(msg)
			log(f"Failed deleting test {msg}")


def _cleanup_vehicle_checkins(test_customers, log, errors):
	if not frappe.db.exists("DocType", "Vehicle Check-in"):
		return
	for vci in frappe.get_all("Vehicle Check-in", fields=["name", "customer", "vehicle"]):
		if _is_test_customer_party(vci.customer, test_customers) or _is_test_vin(vci.vehicle):
			_delete_doc_safe("Vehicle Check-in", vci.name, log, errors)


def _cleanup_leave_applications(log, errors):
	if not frappe.db.exists("DocType", "Leave Application"):
		return
	fields = ["name", "employee", "employee_name"]
	if frappe.db.has_column("Leave Application", "description"):
		fields.append("description")
	for la in frappe.get_all("Leave Application", fields=fields):
		if (
			_has_test_prefix(la.employee)
			or _has_test_prefix(la.employee_name)
			or _has_test_prefix(la.get("description"))
		):
			_delete_doc_safe("Leave Application", la.name, log, errors)


def _cleanup_repair_vehicles(test_customers, log, errors):
	if not frappe.db.exists("DocType", "Repair Vehicle"):
		return
	for v in frappe.get_all("Repair Vehicle", fields=["name", "customer", "license_plate", "vin"]):
		is_test_vehicle = (
			_is_test_vin(v.name)
			or _is_test_vin(v.vin)
			or (v.customer and v.customer in test_customers)
			or _has_test_prefix(v.customer)
			or _has_test_prefix(v.license_plate)
		)
		if is_test_vehicle:
			if frappe.db.exists("DocType", "Schedule Entry"):
				for se_name in frappe.get_all("Schedule Entry", filters={"repair_vehicle": v.name}, pluck="name"):
					_delete_doc_safe("Schedule Entry", se_name, log, errors)
			if frappe.db.exists("DocType", "Vehicle Check-in"):
				for vci_name in frappe.get_all("Vehicle Check-in", filters={"vehicle": v.name}, pluck="name"):
					_delete_doc_safe("Vehicle Check-in", vci_name, log, errors)
			_delete_doc_safe("Repair Vehicle", v.name, log, errors)


def _cleanup_customers(test_customers, stub_customers_created, log, errors):
	all_to_clean = test_customers | stub_customers_created
	for cust in sorted(all_to_clean):
		if not _has_test_prefix(cust):
			continue
		try:
			if frappe.db.exists("DocType", "Schedule Entry"):
				for se_name in frappe.get_all("Schedule Entry", filters={"customer": cust}, pluck="name"):
					_delete_doc_safe("Schedule Entry", se_name, log, errors)
			if frappe.db.exists("DocType", "Repair Vehicle Customer"):
				frappe.db.delete("Repair Vehicle Customer", {"customer": cust})
			_delete_doc_safe("Customer", cust, log, errors)
		except Exception as e:
			msg = f"Customer {cust}: {e}"
			errors.append(msg)
			log(f"Failed deleting test {msg}")

	if frappe.db.exists("DocType", "Repair Vehicle Customer"):
		existing_customers = set(frappe.get_all("Customer", pluck="name"))
		orphan_rvc_names = [
			row.name
			for row in frappe.get_all("Repair Vehicle Customer", fields=["name", "customer"])
			if not row.customer or (_has_test_prefix(row.customer) and row.customer not in existing_customers)
		]
		if orphan_rvc_names:
			frappe.db.delete("Repair Vehicle Customer", {"name": ["in", orphan_rvc_names]})


def _cleanup_suppliers(test_suppliers, log, errors):
	if not frappe.db.exists("DocType", "Supplier"):
		return
	for sup in sorted(test_suppliers):
		if _has_test_prefix(sup):
			_delete_doc_safe("Supplier", sup, log, errors)


def _cleanup_service_bays(log, errors):
	"""Purge residual test Service Bay records matching TEST_PREFIX."""
	if not frappe.db.exists("DocType", "Service Bay"):
		return
	for bay in frappe.get_all("Service Bay", fields=["name", "bay_name"]):
		name = bay.name or bay.bay_name or ""
		if name in CANONICAL_SERVICE_BAYS or not _has_test_prefix(name):
			continue
		if frappe.db.exists("DocType", "Schedule Entry"):
			for se_name in frappe.get_all("Schedule Entry", filters={"service_bay": bay.name}, pluck="name"):
				_delete_doc_safe("Schedule Entry", se_name, log, errors)
		_delete_doc_safe("Service Bay", bay.name, log, errors)


def _cleanup_equipment_tags(log, errors):
	"""Purge residual test Equipment Tags matching TEST_PREFIX."""
	if not frappe.db.exists("DocType", "Equipment Tag"):
		return
	for tag in frappe.get_all("Equipment Tag", pluck="name"):
		if tag in CANONICAL_EQUIPMENT_TAGS or not _has_test_prefix(tag):
			continue
		if frappe.db.exists("DocType", "Service Bay Equipment"):
			frappe.db.delete("Service Bay Equipment", {"equipment_tag": tag})
		_delete_doc_safe("Equipment Tag", tag, log, errors)


def _cleanup_schedule_entry_types(log, errors):
	"""Purge residual test Schedule Entry Types matching TEST_PREFIX."""
	if not frappe.db.exists("DocType", "Schedule Entry Type"):
		return
	for entry_type in frappe.get_all("Schedule Entry Type", pluck="name"):
		if entry_type in STANDARD_SCHEDULE_ENTRY_TYPES or not _has_test_prefix(entry_type):
			continue
		if frappe.db.exists("DocType", "Schedule Entry"):
			for se_name in frappe.get_all("Schedule Entry", filters={"entry_type": entry_type}, pluck="name"):
				_delete_doc_safe("Schedule Entry", se_name, log, errors)
		_delete_doc_safe("Schedule Entry Type", entry_type, log, errors)


def _cleanup_inspection_templates(log, errors):
	if not frappe.db.exists("DocType", "Inspection Template"):
		return
	for tpl in frappe.get_all("Inspection Template", pluck="name"):
		if _has_test_prefix(tpl):
			_delete_doc_safe("Inspection Template", tpl, log, errors)


def _cleanup_service_packages(log, errors):
	if not frappe.db.exists("DocType", "Service Package"):
		return
	for pkg in frappe.get_all("Service Package", pluck="name"):
		if _has_test_prefix(pkg):
			_delete_doc_safe("Service Package", pkg, log, errors)


def _cleanup_items(log, errors):
	for item in frappe.get_all("Item", pluck="name"):
		if item == "LABOR-STD" or not _has_test_prefix(item):
			continue
		_delete_doc_safe("Item", item, log, errors)


def _cleanup_custom_docperms(log, errors):
	if not frappe.db.exists("DocType", "Custom DocPerm"):
		return
	affected_doctypes = set()
	for row in frappe.get_all("Custom DocPerm", fields=["name", "parent", "role"]):
		if row.role in TEST_CUSTOM_DOCPERM_ROLES or _has_test_prefix(row.role):
			_delete_doc_safe("Custom DocPerm", row.name, log, errors)
			if row.parent:
				affected_doctypes.add(row.parent)
	for dt in affected_doctypes:
		frappe.clear_cache(doctype=dt)


def _cleanup_files(log, errors):
	test_files = (
		frappe.get_all(
			"File",
			filters=[["attached_to_name", "like", f"{TEST_PREFIX}%"]],
			pluck="name",
		)
		+ frappe.get_all(
			"File",
			filters=[["file_name", "like", f"{TEST_PREFIX}%"]],
			pluck="name",
		)
	)
	for f_name in set(test_files):
		_delete_doc_safe("File", f_name, log, errors)
