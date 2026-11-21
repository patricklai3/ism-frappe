# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Customer portal Sales Invoice summary, PDF rendering, and rate-limited self-email."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, get_url, validate_email_address

from ism.comms.email_dispatch import dispatch_transactional_email
from ism.portal.contracts import PORTAL_DEFAULT_INVOICE_PRINT_FORMAT, mask_email_address

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_HEADER_UNSAFE_RE = re.compile(r"[\r\n\x00]+")


def _clean_single_line(value: Any, default: str = "") -> str:
	if not isinstance(value, str):
		return default
	cleaned = _HEADER_UNSAFE_RE.sub(" ", value).strip()
	return cleaned or default


def sanitize_invoice_filename(raw_name: str | None) -> str:
	"""Sanitize an invoice PDF filename against CRLF, header injection, and path traversal."""
	base = _clean_single_line(raw_name or "", default="Invoice.pdf")
	# Strip directory components before character sanitization
	base = base.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
	sanitized = _SAFE_FILENAME_RE.sub("-", base)
	sanitized = re.sub(r"\.{2,}", "-", sanitized).strip(".-")
	if not sanitized:
		sanitized = "Invoice"
	if not sanitized.lower().endswith(".pdf"):
		sanitized = f"{sanitized}.pdf"
	return sanitized


def build_invoice_download_headers(filename: str) -> dict[str, str]:
	"""Return hardened HTTP response headers for portal invoice PDF downloads."""
	safe_filename = sanitize_invoice_filename(filename)
	return {
		"Content-Type": "application/pdf",
		"Content-Disposition": f'attachment; filename="{safe_filename}"',
		"Cache-Control": "private, no-store, max-age=0",
		"X-Content-Type-Options": "nosniff",
	}


def _resolve_ro_doc(repair_order: str | Any):
	if isinstance(repair_order, str):
		ro_name = repair_order.strip()
		if not ro_name:
			frappe.throw(_("Repair Order is required."), frappe.ValidationError)
		return frappe.get_doc("Repair Order", ro_name)
	if (
		repair_order is not None
		and getattr(repair_order, "doctype", None) == "Repair Order"
		and getattr(repair_order, "name", None)
	):
		return repair_order
	frappe.throw(_("Repair Order is required."), frappe.ValidationError)


def _parse_single_email(raw_email: Any) -> str | None:
	if not isinstance(raw_email, str):
		return None
	if "\r" in raw_email or "\n" in raw_email or "\x00" in raw_email:
		return None
	cleaned = raw_email.strip()
	if not cleaned or "," in cleaned or ";" in cleaned:
		return None
	validated = validate_email_address(cleaned, throw=False)
	if not validated:
		return None
	return validated.strip()


def _resolve_customer_default_email(ro_doc) -> str | None:
	customer = getattr(ro_doc, "customer", None)
	if customer:
		cust_email = frappe.db.get_value("Customer", customer, "email_id")
		parsed = _parse_single_email(cust_email)
		if parsed:
			return parsed

	contact_person = getattr(ro_doc, "contact_person", None)
	if contact_person:
		contact_email = frappe.db.get_value("Contact", contact_person, "email_id")
		parsed = _parse_single_email(contact_email)
		if parsed:
			return parsed

	return None


def _get_sales_invoice_row(si_name: str | None) -> dict[str, Any] | None:
	if not isinstance(si_name, str) or not si_name.strip():
		return None
	row = frappe.db.get_value(
		"Sales Invoice",
		si_name.strip(),
		[
			"name",
			"docstatus",
			"customer",
			"posting_date",
			"due_date",
			"currency",
			"grand_total",
			"rounded_total",
			"disable_rounded_total",
			"outstanding_amount",
			"company",
		],
		as_dict=True,
	)
	return dict(row) if row else None


def _resolve_validated_ro_and_invoice(repair_order: str | Any) -> tuple[Any, dict[str, Any]]:
	ro_doc = _resolve_ro_doc(repair_order)
	si_name = getattr(ro_doc, "sales_invoice", None)
	if not isinstance(si_name, str) or not si_name.strip():
		frappe.throw(_("No Sales Invoice is linked to this Repair Order."), frappe.DoesNotExistError)

	si_row = _get_sales_invoice_row(si_name)
	if not si_row:
		frappe.throw(_("Linked Sales Invoice was not found."), frappe.DoesNotExistError)

	if cint(si_row.get("docstatus")) != 1:
		frappe.throw(
			_("Sales Invoice must be submitted before it can be accessed."),
			frappe.ValidationError,
		)

	ro_customer = getattr(ro_doc, "customer", None)
	si_customer = si_row.get("customer")
	if not ro_customer or not si_customer or si_customer != ro_customer:
		frappe.throw(
			_("Linked Sales Invoice does not match the Repair Order customer."),
			frappe.ValidationError,
		)

	return ro_doc, si_row


def _resolve_portal_invoice_print_format() -> str:
	try:
		configured = frappe.db.get_single_value("ISM Settings", "portal_invoice_print_format")
	except Exception:
		configured = None

	candidate = _clean_single_line(configured, default=PORTAL_DEFAULT_INVOICE_PRINT_FORMAT)
	if candidate != PORTAL_DEFAULT_INVOICE_PRINT_FORMAT and not frappe.db.exists(
		"Print Format",
		{"name": candidate, "doc_type": "Sales Invoice", "disabled": 0},
	):
		return PORTAL_DEFAULT_INVOICE_PRINT_FORMAT
	return candidate or PORTAL_DEFAULT_INVOICE_PRINT_FORMAT


def _generate_invoice_pdf_bytes(si_name: str, print_format: str) -> bytes:
	prev_flag = getattr(frappe.local.flags, "ignore_print_permissions", False)
	frappe.local.flags.ignore_print_permissions = True
	try:
		try:
			pdf_data = frappe.get_print(
				"Sales Invoice",
				si_name,
				print_format=print_format,
				as_pdf=True,
			)
		except Exception:
			if print_format == PORTAL_DEFAULT_INVOICE_PRINT_FORMAT:
				raise
			pdf_data = frappe.get_print(
				"Sales Invoice",
				si_name,
				print_format=PORTAL_DEFAULT_INVOICE_PRINT_FORMAT,
				as_pdf=True,
			)
	finally:
		frappe.local.flags.ignore_print_permissions = prev_flag

	if isinstance(pdf_data, bytearray):
		pdf_data = bytes(pdf_data)
	if not isinstance(pdf_data, bytes) or not pdf_data.startswith(b"%PDF"):
		frappe.throw(_("Unable to generate Sales Invoice PDF."), frappe.ValidationError)
	return pdf_data


def _compute_invoice_amounts(si_row: dict[str, Any]) -> tuple[float, float, float, bool, str]:
	use_grand = cint(si_row.get("disable_rounded_total")) or not flt(si_row.get("rounded_total"))
	grand_total = flt(si_row.get("grand_total") if use_grand else si_row.get("rounded_total"), 2)
	outstanding_amount = max(0.0, flt(si_row.get("outstanding_amount"), 2))
	paid_amount = max(0.0, flt(grand_total - outstanding_amount, 2))
	is_paid = outstanding_amount <= 0.005
	if is_paid:
		payment_badge = "Paid"
	elif paid_amount > 0.005:
		payment_badge = "Partially Paid"
	else:
		payment_badge = "Unpaid"
	return grand_total, paid_amount, outstanding_amount, is_paid, payment_badge


def get_ro_invoice_summary(ro_doc) -> dict[str, Any]:
	"""Build customer-safe invoice metadata from ro_doc.sales_invoice.

	Unlocks immediately for any submitted Sales Invoice (docstatus == 1),
	including unpaid invoices (outstanding_amount > 0).
	"""
	default_email = _resolve_customer_default_email(ro_doc) if ro_doc is not None else None
	default_email_masked = mask_email_address(default_email)

	unavailable: dict[str, Any] = {
		"available": False,
		"sales_invoice": None,
		"posting_date": None,
		"due_date": None,
		"currency": "CAD",
		"grand_total": 0.0,
		"paid_amount": 0.0,
		"outstanding_amount": 0.0,
		"is_paid": False,
		"payment_badge": None,
		"default_email_masked": default_email_masked,
		"has_default_email": bool(default_email_masked),
		"download_url": None,
	}

	if ro_doc is None:
		return unavailable

	si_name = getattr(ro_doc, "sales_invoice", None)
	si_row = _get_sales_invoice_row(si_name)
	if not si_row:
		return unavailable
	if cint(si_row.get("docstatus")) != 1:
		return unavailable
	ro_customer = getattr(ro_doc, "customer", None)
	si_customer = si_row.get("customer")
	if not ro_customer or not si_customer or si_customer != ro_customer:
		return unavailable

	grand_total, paid_amount, outstanding_amount, is_paid, payment_badge = _compute_invoice_amounts(si_row)
	currency = _clean_single_line(si_row.get("currency"), default="CAD")
	posting_date = str(si_row["posting_date"]) if si_row.get("posting_date") else None
	due_date = str(si_row["due_date"]) if si_row.get("due_date") else None
	ro_name_encoded = quote(str(ro_doc.name), safe="")

	return {
		"available": True,
		"sales_invoice": si_row["name"],
		"posting_date": posting_date,
		"due_date": due_date,
		"currency": currency,
		"grand_total": grand_total,
		"paid_amount": paid_amount,
		"outstanding_amount": outstanding_amount,
		"is_paid": is_paid,
		"payment_badge": payment_badge,
		"default_email_masked": default_email_masked,
		"has_default_email": bool(default_email_masked),
		"download_url": f"/api/method/ism.api.portal.download_invoice_pdf?repair_order={ro_name_encoded}",
	}


def _render_pdf_for_invoice(si_name: str) -> tuple[str, bytes]:
	print_format = _resolve_portal_invoice_print_format()
	pdf_bytes = _generate_invoice_pdf_bytes(si_name, print_format)
	filename = sanitize_invoice_filename(f"Invoice-{si_name}.pdf")
	return filename, pdf_bytes


def render_sales_invoice_pdf(repair_order: str) -> tuple[str, bytes]:
	"""Validate RO/Sales Invoice linkage and render official PDF bytes."""
	_, si_row = _resolve_validated_ro_and_invoice(repair_order)
	return _render_pdf_for_invoice(si_row["name"])


def _validate_target_email(ro_doc, recipient_email: str | None) -> str:
	if recipient_email is not None:
		parsed = _parse_single_email(recipient_email)
		if not parsed:
			frappe.throw(_("Invalid recipient email address."), frappe.ValidationError)
		return parsed

	default_email = _resolve_customer_default_email(ro_doc)
	if not default_email:
		frappe.throw(
			_("No customer email address is on file. Please provide an email address."),
			frappe.ValidationError,
		)
	return default_email


def _build_invoice_email_html(
	*,
	si_name: str,
	shop_name: str,
	currency: str,
	grand_total: float,
	paid_amount: float,
	outstanding_amount: float,
	is_paid: bool,
	payment_badge: str,
	portal_url: str,
) -> str:
	safe_si = escape_html(si_name)
	safe_shop = escape_html(shop_name)
	safe_currency = escape_html(currency)
	safe_portal_url = escape_html(portal_url)
	safe_badge = escape_html(payment_badge)

	if is_paid:
		status_line = f"Status: <strong>{safe_badge} — Paid in Full</strong>"
		balance_line = f"Total Paid: <strong>{safe_currency} {grand_total:,.2f}</strong>"
	else:
		status_line = f"Status: <strong>{safe_badge} — Balance Due</strong>"
		balance_line = (
			f"Total: <strong>{safe_currency} {grand_total:,.2f}</strong> &middot; "
			f"Paid: <strong>{safe_currency} {paid_amount:,.2f}</strong> &middot; "
			f"Balance Due: <strong>{safe_currency} {outstanding_amount:,.2f}</strong>"
		)

	return (
		f"<p>Hello,</p>"
		f"<p>Please find your official invoice <strong>{safe_si}</strong> from <strong>{safe_shop}</strong> attached as a PDF.</p>"
		f"<p>{status_line}<br>{balance_line}</p>"
		f"<p>You can view your visit details or pay any remaining balance anytime at our "
		f"<a href=\"{safe_portal_url}\">Customer Portal</a>.</p>"
	)


def email_sales_invoice_copy(
	repair_order: str,
	*,
	recipient_email: str | None = None,
	client_ip: str | None = None,
) -> dict[str, Any]:
	"""Render official Sales Invoice PDF (unpaid or paid) and email via Project 1."""
	ro_doc, si_row = _resolve_validated_ro_and_invoice(repair_order)
	target_email = _validate_target_email(ro_doc, recipient_email)

	filename, pdf_bytes = _render_pdf_for_invoice(si_row["name"])
	grand_total, paid_amount, outstanding_amount, is_paid, payment_badge = _compute_invoice_amounts(si_row)
	currency = _clean_single_line(si_row.get("currency"), default="CAD")
	shop_name = _clean_single_line(
		si_row.get("company")
		or getattr(ro_doc, "company", None)
		or frappe.defaults.get_global_default("company"),
		default="ISM Auto",
	)
	safe_si_subject = _clean_single_line(si_row["name"], default="Invoice")
	portal_url = get_url("/portal")

	html_body = _build_invoice_email_html(
		si_name=si_row["name"],
		shop_name=shop_name,
		currency=currency,
		grand_total=grand_total,
		paid_amount=paid_amount,
		outstanding_amount=outstanding_amount,
		is_paid=is_paid,
		payment_badge=payment_badge,
		portal_url=portal_url,
	)

	dispatch_result = dispatch_transactional_email(
		recipient=target_email,
		subject=f"Invoice {safe_si_subject} — {shop_name}",
		html_body=html_body,
		email_type="portal_invoice",
		reference_doctype="Sales Invoice",
		reference_name=si_row["name"],
		attachments=[{"fname": filename, "fcontent": pdf_bytes}],
		client_ip=client_ip,
		bypass_rate_limit=False,
	)

	masked_recipient = mask_email_address(dispatch_result.recipient or target_email)
	if dispatch_result.success:
		message = _("Invoice {0} sent to {1}.").format(si_row["name"], masked_recipient or "")
	elif dispatch_result.error_code == "SMTP_ERROR":
		message = _("Unable to send invoice email. Please try again later.")
	else:
		message = dispatch_result.error or _("Unable to send invoice email.")

	return {
		"success": bool(dispatch_result.success),
		"sales_invoice": si_row["name"],
		"recipient_masked": masked_recipient,
		"rate_limited": bool(dispatch_result.rate_limited),
		"retry_after_seconds": int(dispatch_result.retry_after_seconds or 0),
		"error_code": dispatch_result.error_code,
		"message": message,
	}
