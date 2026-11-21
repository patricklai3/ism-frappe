"""Tests for Wave 2C rate-limited transactional email dispatch."""

from __future__ import annotations

from unittest.mock import patch

import frappe

from ism.comms.contracts import EmailDispatchResult
from ism.comms.email_dispatch import clear_email_rate_limits, dispatch_transactional_email
from ism.tests.base import ISMTestCase


class TestCommsEmailDispatch(ISMTestCase):
	"""Verify validation, CRLF guards, Redis hourly rate limits, attachments, and error containment."""

	def setUp(self) -> None:
		super().setUp()
		clear_email_rate_limits()

	def tearDown(self) -> None:
		clear_email_rate_limits()
		super().tearDown()

	def test_valid_dispatch_without_and_with_pdf_attachments(self) -> None:
		pdf_attachment = [
			{
				"fname": "repair-order-RO-0001.pdf",
				"fcontent": b"%PDF-1.4\n%fake-pdf\n",
			}
		]
		with patch("frappe.sendmail") as mock_sendmail:
			res_plain = dispatch_transactional_email(
				recipient="Customer@Example.com",
				subject="Your Estimate is Ready",
				html_body="<p>Hello</p>",
				email_type="estimate_ready",
				reference_doctype="Repair Order",
				reference_name="RO-0001",
				client_ip="198.51.100.10",
			)
			res_pdf = dispatch_transactional_email(
				recipient="customer@example.com",
				subject="Your Invoice PDF",
				html_body="<p>Attached</p>",
				email_type="invoice_receipt",
				attachments=pdf_attachment,
				delayed=True,
				client_ip="198.51.100.10",
			)

		self.assertIsInstance(res_plain, EmailDispatchResult)
		self.assertTrue(res_plain.success)
		self.assertFalse(res_plain.rate_limited)
		self.assertEqual(res_plain.recipient, "Customer@Example.com")
		self.assertEqual(res_plain.email_type, "estimate_ready")

		self.assertTrue(res_pdf.success)
		self.assertEqual(mock_sendmail.call_count, 2)

		first_kwargs = mock_sendmail.call_args_list[0].kwargs
		self.assertEqual(first_kwargs["recipients"], ["Customer@Example.com"])
		self.assertEqual(first_kwargs["subject"], "Your Estimate is Ready")
		self.assertEqual(first_kwargs["message"], "<p>Hello</p>")
		self.assertIsNone(first_kwargs["attachments"])
		self.assertEqual(first_kwargs["reference_doctype"], "Repair Order")
		self.assertEqual(first_kwargs["reference_name"], "RO-0001")
		self.assertTrue(first_kwargs["now"])

		second_kwargs = mock_sendmail.call_args_list[1].kwargs
		self.assertEqual(second_kwargs["attachments"], pdf_attachment)
		self.assertFalse(second_kwargs["now"])

	def test_crlf_header_injection_and_invalid_email_rejected(self) -> None:
		with patch("frappe.sendmail") as mock_sendmail:
			res_subj_lf = dispatch_transactional_email(
				recipient="ok@example.com",
				subject="Hello\nBcc: evil@example.com",
				html_body="<p>Hi</p>",
			)
			res_subj_cr = dispatch_transactional_email(
				recipient="ok@example.com",
				subject="Hello\r\nBcc: evil@example.com",
				html_body="<p>Hi</p>",
			)
			res_rcpt_crlf = dispatch_transactional_email(
				recipient="ok@example.com\r\nBcc: evil@example.com",
				subject="Hello",
				html_body="<p>Hi</p>",
			)
			res_bad_email = dispatch_transactional_email(
				recipient="not-an-email",
				subject="Hello",
				html_body="<p>Hi</p>",
			)
			res_empty_rcpt = dispatch_transactional_email(
				recipient="   ",
				subject="Hello",
				html_body="<p>Hi</p>",
			)
			res_multi_rcpt = dispatch_transactional_email(
				recipient="a@example.com, b@example.com",
				subject="Hello",
				html_body="<p>Hi</p>",
			)
			res_empty_subj = dispatch_transactional_email(
				recipient="ok@example.com",
				subject="  ",
				html_body="<p>Hi</p>",
			)
			res_empty_body = dispatch_transactional_email(
				recipient="ok@example.com",
				subject="Hello",
				html_body="",
			)

		mock_sendmail.assert_not_called()
		self.assertFalse(res_subj_lf.success)
		self.assertEqual(res_subj_lf.error_code, "HEADER_INJECTION")
		self.assertFalse(res_subj_cr.success)
		self.assertEqual(res_subj_cr.error_code, "HEADER_INJECTION")
		self.assertFalse(res_rcpt_crlf.success)
		self.assertEqual(res_rcpt_crlf.error_code, "HEADER_INJECTION")
		self.assertFalse(res_bad_email.success)
		self.assertEqual(res_bad_email.error_code, "INVALID_RECIPIENT")
		self.assertFalse(res_empty_rcpt.success)
		self.assertEqual(res_empty_rcpt.error_code, "INVALID_RECIPIENT")
		self.assertFalse(res_multi_rcpt.success)
		self.assertEqual(res_multi_rcpt.error_code, "INVALID_RECIPIENT")
		self.assertFalse(res_empty_subj.success)
		self.assertEqual(res_empty_subj.error_code, "INVALID_SUBJECT")
		self.assertFalse(res_empty_body.success)
		self.assertEqual(res_empty_body.error_code, "INVALID_BODY")

	def test_per_recipient_hourly_rate_limit_and_retry_after(self) -> None:
		settings = frappe.get_single("ISM Settings")
		settings.transactional_email_rate_limit_per_hour = 3
		settings.transactional_email_ip_rate_limit_per_hour = 20
		settings.save(ignore_permissions=True)

		with patch("frappe.sendmail") as mock_sendmail:
			for idx in range(3):
				res = dispatch_transactional_email(
					recipient="LimitUser@Example.com" if idx % 2 == 0 else "limituser@example.com",
					subject=f"Notice {idx}",
					html_body="<p>Test</p>",
					client_ip="198.51.100.20",
				)
				self.assertTrue(res.success, f"Call {idx} should succeed: {res}")

			blocked = dispatch_transactional_email(
				recipient="limituser@example.com",
				subject="Notice 4",
				html_body="<p>Blocked</p>",
				client_ip="198.51.100.20",
			)

		self.assertEqual(mock_sendmail.call_count, 3)
		self.assertFalse(blocked.success)
		self.assertTrue(blocked.rate_limited)
		self.assertEqual(blocked.error_code, "RATE_LIMITED")
		self.assertGreater(blocked.retry_after_seconds, 0)
		self.assertLessEqual(blocked.retry_after_seconds, 3600)

	def test_per_ip_hourly_rate_limit_across_distinct_recipients(self) -> None:
		settings = frappe.get_single("ISM Settings")
		settings.transactional_email_rate_limit_per_hour = 5
		settings.transactional_email_ip_rate_limit_per_hour = 4
		settings.save(ignore_permissions=True)

		with patch("frappe.sendmail") as mock_sendmail:
			for idx in range(4):
				res = dispatch_transactional_email(
					recipient=f"user{idx}@example.com",
					subject="Hello",
					html_body="<p>Test</p>",
					client_ip="203.0.113.42",
				)
				self.assertTrue(res.success)

			blocked = dispatch_transactional_email(
				recipient="user4@example.com",
				subject="Hello",
				html_body="<p>Blocked</p>",
				client_ip="203.0.113.42",
			)
			other_ip = dispatch_transactional_email(
				recipient="user4@example.com",
				subject="Hello",
				html_body="<p>Allowed</p>",
				client_ip="203.0.113.99",
			)

		self.assertEqual(mock_sendmail.call_count, 5)
		self.assertFalse(blocked.success)
		self.assertTrue(blocked.rate_limited)
		self.assertEqual(blocked.error_code, "RATE_LIMITED")
		self.assertGreater(blocked.retry_after_seconds, 0)
		self.assertTrue(other_ip.success)

	def test_ip_blocked_request_does_not_consume_recipient_quota(self) -> None:
		settings = frappe.get_single("ISM Settings")
		settings.transactional_email_rate_limit_per_hour = 1
		settings.transactional_email_ip_rate_limit_per_hour = 1
		settings.save(ignore_permissions=True)

		with patch("frappe.sendmail") as mock_sendmail:
			res0 = dispatch_transactional_email(
				recipient="user0@example.com",
				subject="Hello",
				html_body="<p>First</p>",
				client_ip="203.0.113.42",
			)
			blocked = dispatch_transactional_email(
				recipient="user1@example.com",
				subject="Hello",
				html_body="<p>Blocked</p>",
				client_ip="203.0.113.42",
			)
			other_ip = dispatch_transactional_email(
				recipient="user1@example.com",
				subject="Hello",
				html_body="<p>Allowed</p>",
				client_ip="203.0.113.99",
			)

		self.assertTrue(res0.success)
		self.assertFalse(blocked.success)
		self.assertTrue(blocked.rate_limited)
		self.assertEqual(blocked.error_code, "RATE_LIMITED")
		self.assertGreater(blocked.retry_after_seconds, 0)
		self.assertTrue(other_ip.success)
		self.assertEqual(mock_sendmail.call_count, 2)

	def test_bypass_rate_limit_allows_staff_emails(self) -> None:
		settings = frappe.get_single("ISM Settings")
		settings.transactional_email_rate_limit_per_hour = 1
		settings.transactional_email_ip_rate_limit_per_hour = 1
		settings.save(ignore_permissions=True)

		with patch("frappe.sendmail") as mock_sendmail:
			res1 = dispatch_transactional_email(
				recipient="staff-target@example.com",
				subject="First",
				html_body="<p>1</p>",
				client_ip="198.51.100.55",
			)
			res_blocked = dispatch_transactional_email(
				recipient="staff-target@example.com",
				subject="Second",
				html_body="<p>2</p>",
				client_ip="198.51.100.55",
			)
			res_bypass = dispatch_transactional_email(
				recipient="staff-target@example.com",
				subject="Staff Override",
				html_body="<p>3</p>",
				client_ip="198.51.100.55",
				bypass_rate_limit=True,
			)

		self.assertTrue(res1.success)
		self.assertFalse(res_blocked.success)
		self.assertTrue(res_blocked.rate_limited)
		self.assertTrue(res_bypass.success)
		self.assertFalse(res_bypass.rate_limited)
		self.assertEqual(mock_sendmail.call_count, 2)

	def test_sendmail_exception_contained_as_smtp_error(self) -> None:
		with patch("frappe.sendmail", side_effect=RuntimeError("SMTP connection refused")):
			res = dispatch_transactional_email(
				recipient="customer@example.com",
				subject="Receipt",
				html_body="<p>Receipt</p>",
				email_type="receipt",
				client_ip="198.51.100.77",
			)

		self.assertFalse(res.success)
		self.assertFalse(res.rate_limited)
		self.assertEqual(res.error_code, "SMTP_ERROR")
		self.assertIn("SMTP connection refused", res.error or "")
