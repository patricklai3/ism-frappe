# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

VALID_PART_SIDES = ("Both", "LH", "RH")


def normalize_part_side(side):
	"""Normalize a Service Package part side to 'Both', 'LH', or 'RH' (never 'None')."""
	if not side:
		return "Both"
	cleaned = str(side).strip()
	if cleaned.lower() in ("none", "null", ""):
		return "Both"
	if cleaned not in VALID_PART_SIDES:
		return "Both"
	return cleaned


def normalize_package_item_slot_and_side(row, is_symmetrical=1, enforce_package_symmetry=True):
	"""Normalize slot_number, component_slot label, and side on a Service Package Item row."""
	from ism.api.parts import format_slot_label, has_lhrh_designation, parse_slot_number

	slot_num = getattr(row, "slot_number", None)
	if slot_num is None or slot_num == "":
		slot_num = parse_slot_number(getattr(row, "component_slot", None))
	row.slot_number = frappe.utils.cint(slot_num)
	row.component_slot = format_slot_label(row.slot_number)

	row.side = normalize_part_side(getattr(row, "side", None))
	if enforce_package_symmetry and not is_symmetrical and row.side != "Both":
		part_ident = f"{getattr(row, 'item_code', '') or ''} {getattr(row, 'item_name', '') or ''}"
		if not has_lhrh_designation(part_ident):
			row.side = "Both"


class ServicePackageItem(Document):
	def validate(self):
		normalize_package_item_slot_and_side(self, is_symmetrical=1, enforce_package_symmetry=False)
