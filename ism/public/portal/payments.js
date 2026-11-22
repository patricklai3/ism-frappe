/**
 * Inline Square Web Payments SDK & Receipt History (CAD).
 * - Dynamically loads sdk_config.sdk_url in sandbox/production modes.
 * - Renders deterministic offline Mock Card widget in mock mode (tokenizes mock_tok_ok, zero external calls).
 * - Uses fresh per-attempt UUID idempotency_key; never sends raw card fields or authoritative amounts.
 */

import {
	escapeHtml,
	formatCurrency,
	generateIdempotencyKey,
	submitPortalPayment,
} from "./api.js";

const loadedSdkUrls = new Map();

function loadSquareSdkScript(sdkUrl) {
	if (!sdkUrl) {
		return Promise.reject(new Error("Square Web Payments SDK URL is not configured."));
	}
	if (window.Square && typeof window.Square.payments === "function") {
		return Promise.resolve(window.Square);
	}
	if (loadedSdkUrls.has(sdkUrl)) {
		return loadedSdkUrls.get(sdkUrl);
	}

	const promise = new Promise((resolve, reject) => {
		const script = document.createElement("script");
		script.src = sdkUrl;
		script.async = true;
		script.onload = () => {
			if (window.Square && typeof window.Square.payments === "function") {
				resolve(window.Square);
			} else {
				loadedSdkUrls.delete(sdkUrl);
				script.remove();
				reject(new Error("Square Web Payments SDK failed to initialize."));
			}
		};
		script.onerror = () => {
			loadedSdkUrls.delete(sdkUrl);
			script.remove();
			reject(new Error("Unable to load Square Web Payments SDK."));
		};
		document.head.appendChild(script);
	});

	loadedSdkUrls.set(sdkUrl, promise);
	return promise;
}

function getBadgeClass(badge) {
	if (badge === "Paid in Full" || badge === "Deposit Paid") {
		return "portal-badge--success";
	}
	if (badge === "Manual Deposit Requested" || badge === "Balance Due") {
		return "portal-badge--warning";
	}
	return "portal-badge--neutral";
}

function normalizePaymentIntent(intent) {
	return String(intent || "")
		.trim()
		.toLowerCase()
		.replace(/\s+/g, "_");
}

function isManualDepositIntent(intent) {
	return normalizePaymentIntent(intent) === "manual_deposit";
}

function isInvoiceBalanceIntent(intent) {
	return normalizePaymentIntent(intent) === "invoice_balance";
}

function renderTransactionsList(transactions, currency) {
	if (!Array.isArray(transactions) || transactions.length === 0) {
		return "";
	}
	return `
		<div style="margin-top: 16px;">
			<h3 class="portal-card__title" style="font-size: 0.96rem; margin-bottom: 6px;">Payment Receipts</h3>
			${transactions
				.map((tx) => {
					const intentLabel = isManualDepositIntent(tx.payment_intent)
						? "Deposit"
						: isInvoiceBalanceIntent(tx.payment_intent)
							? "Invoice Balance"
							: tx.payment_intent || "Payment";
					const cardText =
						tx.card_brand || tx.card_last4
							? `${escapeHtml(tx.card_brand || "Card")} •••• ${escapeHtml(tx.card_last4 || "")}`
							: "";
					return `
						<div class="portal-tx-row">
							<div>
								<strong>${escapeHtml(intentLabel)}</strong>
								<span class="portal-badge portal-badge--neutral" style="margin-left:6px;">${escapeHtml(tx.status || "")}</span>
								<div class="portal-help">${cardText}${cardText && tx.posted_at ? " • " : ""}${escapeHtml(tx.posted_at || "")}</div>
							</div>
							<div style="text-align:right;">
								<div><strong>${formatCurrency(tx.amount || 0, currency)}</strong></div>
								${
									tx.receipt_url
										? `<a href="${escapeHtml(tx.receipt_url)}" target="_blank" rel="noopener noreferrer" class="portal-help">Square Receipt</a>`
										: ""
								}
							</div>
						</div>
					`;
				})
				.join("")}
		</div>
	`;
}

export function renderPaymentsSection(container, { visit, onHubUpdated, notify }) {
	const payment = visit?.payment;
	if (!visit?.repair_order || !payment || !payment.state) {
		container.innerHTML = "";
		return;
	}

	const state = payment.state;
	const sdkConfig = payment.sdk_config || {};
	const currency = state.currency || sdkConfig.currency || "CAD";
	const badge = state.portal_payment_badge || "No Payment Due";
	const canPayOnline = Boolean(
		state.payable_intent &&
			Number(state.payable_amount || 0) > 0 &&
			state.online_payments_enabled &&
			sdkConfig.enabled &&
			!state.has_in_flight_payment
	);

	const payHeading = isManualDepositIntent(state.payable_intent)
		? `Pay Requested Deposit (${formatCurrency(state.payable_amount, currency)})`
		: `Pay Invoice Balance (${formatCurrency(state.payable_amount, currency)})`;

	container.innerHTML = `
		<section class="portal-card" id="portal-payments-section">
			<div class="portal-card__header">
				<div>
					<h2 class="portal-card__title">Payments &amp; Deposits</h2>
					<p class="portal-card__subtitle">Processed securely in ${escapeHtml(currency)}</p>
				</div>
				<span class="portal-badge ${getBadgeClass(badge)}">${escapeHtml(badge)}</span>
			</div>

			<div class="portal-grid-2" style="margin-bottom: 12px;">
				<div class="portal-kv">
					<span class="portal-kv__label">Estimated / Invoiced Total</span>
					<span class="portal-kv__value">${formatCurrency(state.invoice_submitted ? state.invoice_grand_total : state.grand_total, currency)}</span>
				</div>
				<div class="portal-kv">
					<span class="portal-kv__label">Paid / Deposits</span>
					<span class="portal-kv__value">${formatCurrency(state.paid_amount || state.deposit_amount || 0, currency)}</span>
				</div>
				<div class="portal-kv">
					<span class="portal-kv__label">Outstanding Balance</span>
					<span class="portal-kv__value">${formatCurrency(state.invoice_submitted ? state.invoice_outstanding_amount : state.outstanding_amount, currency)}</span>
				</div>
			</div>

			${
				state.deposit_request?.status === "Requested" && state.deposit_request?.note
					? `<div class="portal-alert portal-alert--info">Deposit note: ${escapeHtml(state.deposit_request.note)}</div>`
					: ""
			}

			${
				state.has_in_flight_payment
					? `<div class="portal-alert portal-alert--info">A payment is currently being finalized. Refresh in a moment to view the updated receipt.</div>`
					: ""
			}

			${
				canPayOnline
					? `
				<div class="portal-square-box" id="portal-square-checkout">
					<h3 class="portal-card__title" style="font-size: 0.98rem; margin-bottom: 10px;">${escapeHtml(payHeading)}</h3>
					<div id="portal-square-card-mount" class="portal-square-card-mount"></div>
					<div class="portal-field">
						<label for="portal-payment-email">Receipt Email (optional)</label>
						<input id="portal-payment-email" class="portal-input" type="email" autocomplete="email" placeholder="name@example.com" />
					</div>
					<div id="portal-payment-error" class="portal-alert portal-alert--danger is-hidden" role="alert"></div>
					<button type="button" class="portal-btn portal-btn--success portal-btn--block" id="portal-pay-submit-btn">
						${escapeHtml(payHeading)}
					</button>
				</div>
			`
					: ""
			}

			${renderTransactionsList(state.transactions, currency)}
		</section>
	`;

	if (!canPayOnline) {
		return;
	}

	const mountEl = container.querySelector("#portal-square-card-mount");
	const errorEl = container.querySelector("#portal-payment-error");
	const emailInput = container.querySelector("#portal-payment-email");
	const payBtn = container.querySelector("#portal-pay-submit-btn");

	const showError = (msg, { allowSdkRetry = false } = {}) => {
		errorEl.innerHTML = "";
		const msgSpan = document.createElement("span");
		msgSpan.textContent = msg;
		errorEl.appendChild(msgSpan);
		if (allowSdkRetry) {
			const retryBtn = document.createElement("button");
			retryBtn.type = "button";
			retryBtn.className = "portal-btn portal-btn--outline portal-btn--sm";
			retryBtn.id = "portal-retry-square-btn";
			retryBtn.style.marginLeft = "10px";
			retryBtn.textContent = "Retry Loading Payment Form";
			retryBtn.addEventListener("click", () => {
				errorEl.classList.add("is-hidden");
				mountSquareCard();
			});
			errorEl.appendChild(retryBtn);
		}
		errorEl.classList.remove("is-hidden");
	};

	let squareCard = null;
	const isMock = sdkConfig.mode === "mock";

	const mountSquareCard = () => {
		mountEl.innerHTML = `<p class="portal-help" style="margin:0;">Loading secure Square card input...</p>`;
		payBtn.disabled = true;
		return loadSquareSdkScript(sdkConfig.sdk_url)
			.then(async (Square) => {
				const payments = Square.payments(sdkConfig.application_id, sdkConfig.location_id);
				squareCard = await payments.card();
				mountEl.innerHTML = "";
				await squareCard.attach(mountEl);
				payBtn.disabled = false;
			})
			.catch((err) => {
				squareCard = null;
				mountEl.innerHTML = "";
				showError(err.message || "Unable to load secure payment form.", {
					allowSdkRetry: true,
				});
			});
	};

	if (isMock) {
		mountEl.innerHTML = `
			<div class="portal-mock-card" data-mock-square-widget="true">
				<strong>Square Test Card (Offline Mock Mode)</strong>
				<div class="portal-help">Visa •••• 1111 • Zero external network calls</div>
			</div>
		`;
	} else {
		mountSquareCard();
	}

	payBtn.addEventListener("click", async () => {
		errorEl.classList.add("is-hidden");
		payBtn.disabled = true;
		payBtn.textContent = "Processing Payment...";

		try {
			let sourceId = "mock_tok_ok";
			if (!isMock) {
				if (!squareCard) {
					throw new Error("Payment form is not ready yet.");
				}
				const tokenResult = await squareCard.tokenize();
				if (!tokenResult || tokenResult.status !== "OK" || !tokenResult.token) {
					const errMsg =
						tokenResult?.errors?.map((e) => e.message).join("; ") ||
						"Please check your card details and try again.";
					throw new Error(errMsg);
				}
				sourceId = tokenResult.token;
			}

			const idempotencyKey = generateIdempotencyKey();
			const buyerEmail = (emailInput.value || "").trim() || undefined;

			const res = await submitPortalPayment({
				repair_order: visit.repair_order,
				payment_intent: state.payable_intent,
				source_id: sourceId,
				idempotency_key: idempotencyKey,
				expected_amount_cents: state.payable_amount_cents,
				buyer_email: buyerEmail,
			});

			if (!res || res.success === false) {
				payBtn.disabled = false;
				payBtn.textContent = payHeading;
				showError(
					res?.error_message ||
						res?.error ||
						res?.message ||
						"Payment could not be completed. Please try again."
				);
				return;
			}

			if (res.status === "Pending") {
				if (notify) {
					notify("Payment is finalizing. Please check back in a moment for your receipt.", "info");
				}
			} else if (notify) {
				notify("Payment confirmed and receipt recorded.", "success");
			}
			if (res.hub) {
				onHubUpdated(res.hub);
			}
		} catch (err) {
			payBtn.disabled = false;
			payBtn.textContent = payHeading;
			showError(err.message || "Payment could not be completed. Please try again.");
		}
	});
}
