# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 2C tests for Project 4 Customer Portal Sales Invoice summary, PDF, and self-email."""

from __future__ import annotations

from unittest.mock import patch

import frappe

from ism.comms.email_dispatch import clear_email_rate_limits
from ism.portal.contracts import find_forbidden_portal_keys
from ism.portal.invoices import (
	build_invoice_download_headers,
	email_sales_invoice_copy,
	get_ro_invoice_summary,
	render_sales_invoice_pdf,
	sanitize_invoice_filename,
)
from ism.tests.base import ISMTestCase


class TestPortalInvoices(ISMTestCase):
	"""Verify unpaid/paid Sales Invoice unlock, rejection guards, PDF headers, and email rate limits."""

	def setUp(self):
		super().setUp()
		clear_email_rate_limits()
		self._orig_print_format = frappe.db.get_single_value("ISM Settings", "portal_invoice_print_format")
		self._orig_rcpt_limit = frappe.db.get_single_value(
			"ISM Settings", "transactional_email_rate_limit_per_hour"
		)
		self._orig_ip_limit = frappe.db.get_single_value(
			"ISM Settings", "transactional_email_ip_rate_limit_per_hour"
		)
		self.customer = self.create_test_customer(email_id="driver@example.com")
		self.vehicle = self.create_test_vehicle(customer=self.customer.name)
		self.item_code = self.get_or_create_test_item()

	def tearDown(self):
		clear_email_rate_limits()
		frappe.db.set_single_value("ISM Settings", "portal_invoice_print_format", self._orig_print_format)
		frappe.db.set_single_value(
			"ISM Settings", "transactional_email_rate_limit_per_hour", self._orig_rcpt_limit
		)
		frappe.db.set_single_value(
			"ISM Settings", "transactional_email_ip_rate_limit_per_hour", self._orig_ip_limit
		)
		frappe.clear_cache(doctype="ISM Settings")
		super().tearDown()

	def _create_ro(self, *, customer=None, vehicle=None, status="Work Completed", **extra):
		cust_name = customer or self.customer.name
		veh_name = vehicle or self.vehicle.name
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": cust_name,
			"vehicle": veh_name,
			"odometer_in": 45000,
			"approval_status": "Approved",
			"status": status,
			"tax_rate": 13.0,
			"services": [
				{
					"service_title": "Brake Inspection",
					"status": "Approved",
					"labor_hours": 1.0,
					"labor_rate": 100.0,
				}
			],
			**extra,
		}).insert(ignore_permissions=True)
		return self.track(ro)

	def _create_sales_invoice(self, *, customer=None, rate=100.0, submit=True, cancel=False):
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"customer": customer or self.customer.name,
			"currency": "CAD",
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1,
					"rate": rate,
				}
			],
		}).insert(ignore_permissions=True)
		self.track(si)
		if submit:
			si.submit()
		if cancel:
			si.cancel()
		return si

	def test_rejects_missing_draft_cancelled_and_mismatched_customer_invoices(self):
		"""Unlinked, draft (0), cancelled (2), and customer-mismatched invoices must be rejected."""
		ro = self._create_ro()

		# 1. No invoice linked
		summary_none = get_ro_invoice_summary(ro)
		self.assertFalse(summary_none["available"])
		self.assertIsNone(summary_none["sales_invoice"])
		self.assertIsNone(summary_none["download_url"])
		self.assertIsNone(summary_none["payment_badge"])
		self.assertTrue(summary_none["has_default_email"])
		self.assertEqual(summary_none["default_email_masked"], "d***r@example.com")

		with self.assertRaises(frappe.DoesNotExistError):
			render_sales_invoice_pdf(ro.name)
		with self.assertRaises(frappe.DoesNotExistError):
			email_sales_invoice_copy(ro.name)

		# 2. Draft invoice (docstatus == 0)
		draft_si = self._create_sales_invoice(submit=False)
		ro.db_set("sales_invoice", draft_si.name, update_modified=False)
		ro.reload()

		self.assertFalse(get_ro_invoice_summary(ro)["available"])
		with self.assertRaises(frappe.ValidationError):
			render_sales_invoice_pdf(ro.name)
		with self.assertRaises(frappe.ValidationError):
			email_sales_invoice_copy(ro.name)

		# 3. Cancelled invoice (docstatus == 2)
		cancelled_si = self._create_sales_invoice(submit=True, cancel=True)
		ro.db_set("sales_invoice", cancelled_si.name, update_modified=False)
		ro.reload()

		self.assertFalse(get_ro_invoice_summary(ro)["available"])
		with self.assertRaises(frappe.ValidationError):
			render_sales_invoice_pdf(ro.name)
		with self.assertRaises(frappe.ValidationError):
			email_sales_invoice_copy(ro.name)

		# 4. Customer mismatch
		other_customer = self.create_test_customer(email_id="other@example.com")
		other_si = self._create_sales_invoice(customer=other_customer.name, submit=True)
		ro.db_set("sales_invoice", other_si.name, update_modified=False)
		ro.reload()

		self.assertFalse(get_ro_invoice_summary(ro)["available"])
		with self.assertRaises(frappe.ValidationError):
			render_sales_invoice_pdf(ro.name)
		with self.assertRaises(frappe.ValidationError):
			email_sales_invoice_copy(ro.name)

	def test_unpaid_submitted_invoice_unlocks_summary_pdf_and_email(self):
		"""Submitted UNPAID Sales Invoice (outstanding_amount > 0) unlocks summary, PDF, and email."""
		ro = self._create_ro(approval_token="secret-approval-tok-123")
		unpaid_si = self._create_sales_invoice(rate=150.0, submit=True)
		ro.db_set("sales_invoice", unpaid_si.name, update_modified=False)
		ro.reload()

		summary = get_ro_invoice_summary(ro)
		self.assertTrue(summary["available"])
		self.assertEqual(summary["sales_invoice"], unpaid_si.name)
		self.assertEqual(summary["currency"], "CAD")
		self.assertGreater(summary["grand_total"], 0.0)
		self.assertEqual(summary["paid_amount"], 0.0)
		self.assertEqual(summary["outstanding_amount"], summary["grand_total"])
		self.assertFalse(summary["is_paid"])
		self.assertEqual(summary["payment_badge"], "Unpaid")
		self.assertTrue(summary["has_default_email"])
		self.assertEqual(summary["default_email_masked"], "d***r@example.com")
		self.assertEqual(
			summary["download_url"],
			f"/api/method/ism.api.portal.download_invoice_pdf?repair_order={ro.name}",
		)
		self.assertEqual(find_forbidden_portal_keys(summary), [])

		filename, pdf_bytes = render_sales_invoice_pdf(ro.name)
		self.assertTrue(filename.startswith("Invoice-"))
		self.assertTrue(filename.endswith(".pdf"))
		self.assertTrue(pdf_bytes.startswith(b"%PDF"))

		with patch("frappe.sendmail") as mock_sendmail:
			res = email_sales_invoice_copy(ro.name, client_ip="203.0.113.10")

		self.assertTrue(res["success"])
		self.assertEqual(res["sales_invoice"], unpaid_si.name)
		self.assertEqual(res["recipient_masked"], "d***r@example.com")
		self.assertFalse(res["rate_limited"])
		self.assertEqual(res["retry_after_seconds"], 0)
		mock_sendmail.assert_called_once()
		call_kwargs = mock_sendmail.call_args.kwargs
		self.assertEqual(call_kwargs["recipients"], ["driver@example.com"])
		self.assertEqual(call_kwargs["reference_doctype"], "Sales Invoice")
		self.assertEqual(call_kwargs["reference_name"], unpaid_si.name)
		self.assertEqual(len(call_kwargs["attachments"]), 1)
		self.assertEqual(call_kwargs["attachments"][0]["fname"], filename)
		self.assertTrue(call_kwargs["attachments"][0]["fcontent"].startswith(b"%PDF"))

		html_body = call_kwargs["message"]
		subject = call_kwargs["subject"]
		self.assertIn("Balance Due", html_body)
		self.assertIn("/portal", html_body)
		self.assertNotIn("secret-approval-tok-123", html_body)
		self.assertNotIn("secret-approval-tok-123", subject)
		self.assertNotIn("pin_to_drive", html_body.lower())
		self.assertNotIn("pin_to_drive", subject.lower())

	def test_paid_submitted_invoice_unlocks_and_reports_paid_in_full(self):
		"""Submitted PAID Sales Invoice unlocks cleanly with Paid badge and Paid in Full email."""
		ro = self._create_ro()
		chk = ro.process_payment_and_release(payment_method="Cash", odometer_out=45010)
		si_name = chk["sales_invoice"]
		self.track("Sales Invoice", si_name)
		for pe_name in chk.get("payment_entries") or []:
			self.track("Payment Entry", pe_name)

		ro.reload()
		summary = get_ro_invoice_summary(ro)
		self.assertTrue(summary["available"])
		self.assertEqual(summary["sales_invoice"], si_name)
		self.assertTrue(summary["is_paid"])
		self.assertEqual(summary["payment_badge"], "Paid")
		self.assertEqual(summary["outstanding_amount"], 0.0)
		self.assertGreater(summary["paid_amount"], 0.0)

		filename, pdf_bytes = render_sales_invoice_pdf(ro.name)
		self.assertTrue(pdf_bytes.startswith(b"%PDF"))
		self.assertIn(si_name, filename)

		with patch("frappe.sendmail") as mock_sendmail:
			res = email_sales_invoice_copy(
				ro.name,
				recipient_email="override@example.com",
				client_ip="203.0.113.11",
			)

		self.assertTrue(res["success"])
		self.assertEqual(res["recipient_masked"], "o***e@example.com")
		mock_sendmail.assert_called_once()
		self.assertEqual(mock_sendmail.call_args.kwargs["recipients"], ["override@example.com"])
		self.assertIn("Paid in Full", mock_sendmail.call_args.kwargs["message"])

	def test_print_format_fallback_and_hardened_pdf_headers(self):
		"""Missing ISM Settings print format falls back to Standard; headers block CRLF & sniffing."""
		ro = self._create_ro()
		si = self._create_sales_invoice(rate=120.0, submit=True)
		ro.db_set("sales_invoice", si.name, update_modified=False)

		frappe.db.set_single_value("ISM Settings", "portal_invoice_print_format", "NonExistentFormat999")
		frappe.clear_cache(doctype="ISM Settings")

		filename, pdf_bytes = render_sales_invoice_pdf(ro.name)
		self.assertTrue(pdf_bytes.startswith(b"%PDF"))
		self.assertTrue(filename.endswith(".pdf"))

		# Verify CRLF/path traversal filename sanitization (including embedded '..') & security headers
		for unsafe_name in (
			"../../evil\r\nSet-Cookie: pwn=1; filename=bad\".pdf",
			"../../evil..name.pdf",
			"Invoice-..bad.pdf",
		):
			clean_name = sanitize_invoice_filename(unsafe_name)
			self.assertNotIn("\r", clean_name)
			self.assertNotIn("\n", clean_name)
			self.assertNotIn("..", clean_name)
			self.assertNotIn("/", clean_name)
			self.assertNotIn('"', clean_name)
			self.assertTrue(clean_name.endswith(".pdf"))

		headers = build_invoice_download_headers("../../evil\r\nSet-Cookie: pwn=1; filename=bad\".pdf")
		self.assertEqual(headers["Content-Type"], "application/pdf")
		self.assertTrue(headers["Content-Disposition"].startswith('attachment; filename="'))
		self.assertNotIn("\r", headers["Content-Disposition"])
		self.assertNotIn("\n", headers["Content-Disposition"])
		self.assertEqual(headers["Cache-Control"], "private, no-store, max-age=0")
		self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

	def test_email_validation_and_project1_rate_limit_propagation(self):
		"""Reject CRLF/invalid/missing emails and propagate P1 per-recipient & per-IP rate limits."""
		no_email_customer = self.create_test_customer(email_id=None)
		no_email_vehicle = self.create_test_vehicle(customer=no_email_customer.name)
		ro = self._create_ro(customer=no_email_customer.name, vehicle=no_email_vehicle.name)
		si = self._create_sales_invoice(customer=no_email_customer.name, rate=95.0, submit=True)
		ro.db_set("sales_invoice", si.name, update_modified=False)

		# Missing default email without override
		with self.assertRaises(frappe.ValidationError):
			email_sales_invoice_copy(ro.name)

		# CRLF injection & malformed emails rejected
		for bad_email in (
			"driver@example.com\r\nBcc: attacker@evil.com",
			"driver@example.com\nX-Test: 1",
			"a@example.com, b@example.com",
			"not-an-email",
			"",
		):
			with self.assertRaises(frappe.ValidationError):
				email_sales_invoice_copy(ro.name, recipient_email=bad_email)

		# Per-recipient rate limit propagation via P1 dispatch_transactional_email
		frappe.db.set_single_value("ISM Settings", "transactional_email_rate_limit_per_hour", 2)
		frappe.db.set_single_value("ISM Settings", "transactional_email_ip_rate_limit_per_hour", 10)
		frappe.clear_cache(doctype="ISM Settings")

		with patch("frappe.sendmail") as mock_sendmail:
			r1 = email_sales_invoice_copy(
				ro.name, recipient_email="rltest@example.com", client_ip="198.51.100.1"
			)
			r2 = email_sales_invoice_copy(
				ro.name, recipient_email="rltest@example.com", client_ip="198.51.100.2"
			)
			r3 = email_sales_invoice_copy(
				ro.name, recipient_email="rltest@example.com", client_ip="198.51.100.3"
			)

		self.assertTrue(r1["success"])
		self.assertTrue(r2["success"])
		self.assertFalse(r3["success"])
		self.assertTrue(r3["rate_limited"])
		self.assertGreater(r3["retry_after_seconds"], 0)
		self.assertEqual(r3["error_code"], "RATE_LIMITED")
		self.assertEqual(mock_sendmail.call_count, 2)

		# Per-IP rate limit propagation via P1 dispatch_transactional_email
		clear_email_rate_limits()
		frappe.db.set_single_value("ISM Settings", "transactional_email_rate_limit_per_hour", 10)
		frappe.db.set_single_value("ISM Settings", "transactional_email_ip_rate_limit_per_hour", 1)
		frappe.clear_cache(doctype="ISM Settings")

		with patch("frappe.sendmail") as mock_sendmail:
			ip1 = email_sales_invoice_copy(
				ro.name, recipient_email="ip1@example.com", client_ip="198.51.100.99"
			)
			ip2 = email_sales_invoice_copy(
				ro.name, recipient_email="ip2@example.com", client_ip="198.51.100.99"
			)

		self.assertTrue(ip1["success"])
		self.assertFalse(ip2["success"])
		self.assertTrue(ip2["rate_limited"])
		self.assertGreater(ip2["retry_after_seconds"], 0)
		self.assertEqual(ip2["error_code"], "RATE_LIMITED")
		self.assertEqual(mock_sendmail.call_count, 1)

	def test_guest_execution_and_smtp_error_masking(self):
		"""Guest user can render PDF and send invoice email; raw SMTP errors are masked."""
		ro = self._create_ro()
		si = self._create_sales_invoice(rate=110.0, submit=True)
		ro.db_set("sales_invoice", si.name, update_modified=False)
		ro.reload()

		prev_user = frappe.session.user
		frappe.set_user("Guest")
		try:
			summary = get_ro_invoice_summary(ro)
			self.assertTrue(summary["available"])
			self.assertEqual(find_forbidden_portal_keys(summary), [])

			filename, pdf_bytes = render_sales_invoice_pdf(ro.name)
			self.assertTrue(filename.endswith(".pdf"))
			self.assertTrue(pdf_bytes.startswith(b"%PDF"))

			with patch("frappe.sendmail") as mock_sendmail:
				ok_res = email_sales_invoice_copy(ro.name, client_ip="203.0.113.55")
			self.assertTrue(ok_res["success"])
			mock_sendmail.assert_called_once()

			with patch("frappe.sendmail", side_effect=RuntimeError("smtp://internal-secret-host:587 failed")):
				err_res = email_sales_invoice_copy(ro.name, client_ip="203.0.113.56")
			self.assertFalse(err_res["success"])
			self.assertEqual(err_res["error_code"], "SMTP_ERROR")
			self.assertNotIn("internal-secret-host", err_res["message"])
		finally:
			frappe.set_user(prev_user)
