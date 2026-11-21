/**
 * Invoice PDF Download & Rate-Limited Self-Email.
 * Unlocks immediately for BOTH unpaid and paid submitted Sales Invoices.
 */

import { emailInvoiceCopy, escapeHtml, formatCurrency } from "./api.js";

export function renderInvoicesSection(container, { visit, notify }) {
	const invoice = visit?.invoice;
	if (!visit?.repair_order || !invoice || !invoice.available) {
		container.innerHTML = "";
		return;
	}

	const currency = invoice.currency || "CAD";
	const badge = invoice.payment_badge || (invoice.is_paid ? "Paid" : "Unpaid");
	const badgeClass =
		badge === "Paid"
			? "portal-badge--success"
			: badge === "Partially Paid"
				? "portal-badge--warning"
				: "portal-badge--danger";

	container.innerHTML = `
		<section class="portal-card" id="portal-invoice-section">
			<div class="portal-card__header">
				<div>
					<h2 class="portal-card__title">Invoice ${escapeHtml(invoice.sales_invoice || "")}</h2>
					<p class="portal-card__subtitle">
						${invoice.posting_date ? `Issued ${escapeHtml(invoice.posting_date)}` : "Official Sales Invoice"}
						${invoice.due_date && !invoice.is_paid ? ` • Due ${escapeHtml(invoice.due_date)}` : ""}
					</p>
				</div>
				<span class="portal-badge ${badgeClass}">${escapeHtml(badge)}</span>
			</div>

			<div class="portal-grid-2" style="margin-bottom: 14px;">
				<div class="portal-kv">
					<span class="portal-kv__label">Invoice Total</span>
					<span class="portal-kv__value">${formatCurrency(invoice.grand_total || 0, currency)}</span>
				</div>
				<div class="portal-kv">
					<span class="portal-kv__label">Amount Paid</span>
					<span class="portal-kv__value">${formatCurrency(invoice.paid_amount || 0, currency)}</span>
				</div>
				<div class="portal-kv">
					<span class="portal-kv__label">Balance Due</span>
					<span class="portal-kv__value">${formatCurrency(invoice.outstanding_amount || 0, currency)}</span>
				</div>
			</div>

			${
				!invoice.is_paid
					? `<div class="portal-alert portal-alert--warning">This invoice is currently <strong>${escapeHtml(badge)}</strong>. You can download or email a PDF copy now and pay the remaining balance above.</div>`
					: ""
			}

			<div class="portal-inline-actions" style="margin-bottom: 16px;">
				${
					invoice.download_url
						? `<a href="${escapeHtml(invoice.download_url)}" class="portal-btn portal-btn--primary" target="_blank" rel="noopener">Download PDF Invoice</a>`
						: ""
				}
			</div>

			<form id="portal-invoice-email-form" novalidate>
				<div class="portal-field">
					<label for="portal-invoice-email-input">Email Copy to Myself</label>
					<input
						id="portal-invoice-email-input"
						class="portal-input"
						type="email"
						autocomplete="email"
						placeholder="${escapeHtml(invoice.has_default_email && invoice.default_email_masked ? `Leave blank to use ${invoice.default_email_masked}` : "Enter email address")}"
					/>
				</div>
				<div id="portal-invoice-email-feedback" class="portal-alert is-hidden" role="status"></div>
				<button type="submit" class="portal-btn portal-btn--outline" id="portal-invoice-email-btn">
					Email PDF Copy to Myself
				</button>
			</form>
		</section>
	`;

	const form = container.querySelector("#portal-invoice-email-form");
	const emailInput = container.querySelector("#portal-invoice-email-input");
	const feedbackEl = container.querySelector("#portal-invoice-email-feedback");
	const submitBtn = container.querySelector("#portal-invoice-email-btn");
	let rateLimitTimer = null;

	const startCountdown = (seconds) => {
		if (rateLimitTimer) {
			window.clearInterval(rateLimitTimer);
		}
		let remaining = Math.max(1, Number(seconds || 60));
		submitBtn.disabled = true;
		submitBtn.textContent = `Try again in ${remaining}s`;
		rateLimitTimer = window.setInterval(() => {
			remaining -= 1;
			if (remaining <= 0) {
				window.clearInterval(rateLimitTimer);
				rateLimitTimer = null;
				submitBtn.disabled = false;
				submitBtn.textContent = "Email PDF Copy to Myself";
			} else {
				submitBtn.textContent = `Try again in ${remaining}s`;
			}
		}, 1000);
	};

	form.addEventListener("submit", async (e) => {
		e.preventDefault();
		feedbackEl.classList.add("is-hidden");
		const recipientEmail = (emailInput.value || "").trim();

		if (!recipientEmail && !invoice.has_default_email) {
			feedbackEl.className = "portal-alert portal-alert--danger";
			feedbackEl.textContent = "Please enter an email address to receive your PDF invoice.";
			return;
		}

		submitBtn.disabled = true;
		submitBtn.textContent = "Sending PDF Invoice...";
		try {
			const res = await emailInvoiceCopy({
				repair_order: visit.repair_order,
				recipient_email: recipientEmail || undefined,
			});

			if (res && res.rate_limited) {
				const retrySec = Number(res.retry_after_seconds || 60);
				feedbackEl.className = "portal-alert portal-alert--warning";
				feedbackEl.textContent =
					res.error ||
					res.message ||
					`Email limit reached. Please wait ${retrySec} seconds before requesting another copy.`;
				startCountdown(retrySec);
				return;
			}

			if (res && (res.sent || res.success)) {
				feedbackEl.className = "portal-alert portal-alert--success";
				feedbackEl.textContent = `Invoice PDF sent to ${res.recipient_masked || "your email"}.`;
				if (notify) {
					notify("Invoice PDF sent.", "success");
				}
			} else {
				feedbackEl.className = "portal-alert portal-alert--danger";
				feedbackEl.textContent = res?.error || res?.message || "Unable to email invoice copy.";
			}
			submitBtn.disabled = false;
			submitBtn.textContent = "Email PDF Copy to Myself";
		} catch (err) {
			feedbackEl.className = "portal-alert portal-alert--danger";
			feedbackEl.textContent = err.message || "Unable to email invoice copy right now.";
			submitBtn.disabled = false;
			submitBtn.textContent = "Email PDF Copy to Myself";
		}
	});
}
