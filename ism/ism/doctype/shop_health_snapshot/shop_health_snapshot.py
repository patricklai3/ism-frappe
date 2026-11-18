# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document

_VALID_GRADES = ("Healthy", "Watch", "Critical")


class ShopHealthSnapshot(Document):
	def validate(self):
		if not self.metric_key or not isinstance(self.metric_key, str) or not self.metric_key.strip():
			frappe.throw(_("Metric Key is required."), frappe.ValidationError)
		self.metric_key = self.metric_key.strip()

		if isinstance(self.score, bool) or self.score is None:
			frappe.throw(_("Score must be an integer between 0 and 100."), frappe.ValidationError)
		try:
			score_int = int(self.score)
		except (TypeError, ValueError):
			frappe.throw(_("Score must be an integer between 0 and 100."), frappe.ValidationError)
		if float(self.score) != float(score_int) or score_int < 0 or score_int > 100:
			frappe.throw(_("Score must be an integer between 0 and 100."), frappe.ValidationError)
		self.score = score_int

		if self.grade not in _VALID_GRADES:
			frappe.throw(
				_("Grade must be one of: {0}.").format(", ".join(_VALID_GRADES)),
				frappe.ValidationError,
			)

		if not self.verdict or not isinstance(self.verdict, str) or not self.verdict.strip():
			frappe.throw(_("Verdict is required."), frappe.ValidationError)
		self.verdict = self.verdict.strip()

		if self.details_json is not None and str(self.details_json).strip():
			try:
				parsed = json.loads(self.details_json) if isinstance(self.details_json, str) else self.details_json
			except (TypeError, ValueError) as exc:
				frappe.throw(_("Details JSON must be valid JSON: {0}").format(exc), frappe.ValidationError)
			if not isinstance(parsed, (dict, list)):
				frappe.throw(_("Details JSON must be a JSON object or array."), frappe.ValidationError)
			if not isinstance(self.details_json, str):
				self.details_json = json.dumps(parsed)

		if not self.evaluated_by:
			self.evaluated_by = frappe.session.user
