# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Idempotent pre_model_sync migration dropping residual unique index on tabService Package.package_name."""

from __future__ import annotations

import re

import frappe


def execute():
	if not frappe.db.table_exists("Service Package"):
		return

	indexes = frappe.db.sql(
		"""
		SHOW INDEX FROM `tabService Package`
		WHERE Column_name = 'package_name' AND Non_unique = 0
		""",
		as_dict=True,
	)
	seen = set()
	for idx in indexes:
		key_name = idx.get("Key_name")
		if not key_name or key_name == "PRIMARY" or key_name in seen:
			continue
		seen.add(key_name)
		if re.fullmatch(r"[A-Za-z0-9_]+", key_name):
			try:
				frappe.db.sql_ddl(f"ALTER TABLE `tabService Package` DROP INDEX `{key_name}`")
			except Exception:
				pass
