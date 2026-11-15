# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.utils import now_datetime


def generate_safe_series_name(doctype: str, series_pattern: str, doc=None) -> str:
	"""Fallback series generator when MariaDB 1020 contention hits tabSeries."""
	now = now_datetime()
	prefix_parts = []
	digits = 5

	for part in (series_pattern or "").split("."):
		if part.startswith("#"):
			digits = len(part)
			break
		if part == "YYYY":
			prefix_parts.append(now.strftime("%Y"))
		elif part == "YY":
			prefix_parts.append(now.strftime("%y"))
		elif part == "MM":
			prefix_parts.append(now.strftime("%m"))
		elif part == "DD":
			prefix_parts.append(now.strftime("%d"))
		elif doc is not None and hasattr(doc, part) and getattr(doc, part):
			prefix_parts.append(str(getattr(doc, part)))
		else:
			prefix_parts.append(part)

	prefix = "".join(prefix_parts)
	escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	dt = frappe.qb.DocType(doctype)
	existing = (
		frappe.qb.from_(dt)
		.select(dt.name)
		.where(dt.name.like(f"{escaped_prefix}%"))
		.orderby(dt.name, order=frappe.qb.desc)
		.limit(1)
		.for_update()
		.run(pluck=True)
	)

	next_num = 1
	if existing and existing[0].startswith(prefix):
		suffix = existing[0][len(prefix):]
		match = re.search(r"(\d+)", suffix)
		if match:
			try:
				next_num = int(match.group(1)) + 1
			except ValueError:
				next_num = 1

	candidate = f"{prefix}{next_num:0{digits}d}"
	while frappe.db.exists(doctype, candidate):
		next_num += 1
		candidate = f"{prefix}{next_num:0{digits}d}"
	return candidate
