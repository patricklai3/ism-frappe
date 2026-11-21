/**
 * Ontario-Compliant Quote Authorization + Always-Prompt 4-Digit PIN-to-Drive Workflow.
 * - Per-service Approve/Decline, part disclosures (source/condition/warranty/OEM#), legal disclaimer
 * - HTML5 touch/mouse PNG signature pad
 * - Immediate post-approval PIN-to-Drive modal with 1-tap "No PIN-to-Drive enabled" opt-out
 * - Masked write-only PIN display (••••) and pre-work Update PIN
 */

import {
	escapeHtml,
	formatCurrency,
	optOutPinToDrive,
	submitPinToDrive,
	submitPortalQuoteApproval,
} from "./api.js";

function renderPartDisclosures(part) {
	const chips = [];
	if (part.part_source) {
		chips.push(`<span class="portal-badge portal-badge--primary">${escapeHtml(part.part_source)}</span>`);
	}
	if (part.part_condition) {
		const tone =
			part.part_condition === "New"
				? "success"
				: part.part_condition === "Used"
					? "warning"
					: "neutral";
		chips.push(`<span class="portal-badge portal-badge--${tone}">${escapeHtml(part.part_condition)}</span>`);
	}
	if (part.oem_part_number) {
		chips.push(`<span>OEM #: ${escapeHtml(part.oem_part_number)}</span>`);
	}
	if (part.warranty_period) {
		chips.push(`<span>Warranty: ${escapeHtml(part.warranty_period)}</span>`);
	}
	return chips.length ? `<div class="portal-part-meta">${chips.join("")}</div>` : "";
}

function renderApprovalLogs(logs, auditHash) {
	if ((!logs || !logs.length) && !auditHash) {
		return "";
	}
	const logHtml = (logs || [])
		.map(
			(log) => `
			<div class="portal-summary-box" style="margin: 8px 0;">
				<div class="portal-summary-row">
					<strong>${escapeHtml(log.signer_name || "Customer")} <span class="portal-badge portal-badge--neutral">${escapeHtml(log.channel || "Portal")}</span></strong>
					<span class="portal-help">${escapeHtml(log.approval_datetime || "")}</span>
				</div>
				<div class="portal-help">Authorized Amount: <strong>${formatCurrency(log.authorized_amount || 0)}</strong></div>
				${log.approved_services && log.approved_services !== "None" ? `<div class="portal-help" style="color:#15803d;">Approved: ${escapeHtml(log.approved_services)}</div>` : ""}
				${log.declined_services && log.declined_services !== "None" ? `<div class="portal-help" style="color:#b91c1c;">Declined: ${escapeHtml(log.declined_services)}</div>` : ""}
			</div>
		`
		)
		.join("");

	const hashHtml = auditHash
		? `<div class="portal-help" style="margin-top:8px;">Verification Digest (SHA-256): <code>${escapeHtml(auditHash)}</code></div>`
		: "";

	return `
		<div style="margin-top: 16px;">
			<h3 class="portal-card__title" style="font-size: 0.96rem; margin-bottom: 8px;">Authorization Audit History</h3>
			${logHtml}
			${hashHtml}
		</div>
	`;
}

export function renderQuoteSection(container, { visit, onHubUpdated, onPromptPin, notify }) {
	const quote = visit?.quote;
	if (!quote || !Array.isArray(quote.services) || quote.services.length === 0) {
		container.innerHTML = "";
		return;
	}

	const canAuthorize = Boolean(quote.can_authorize && !visit.is_read_only);
	const decisions = {};
	let priorAuthorized = 0;
	let hasLocked = false;

	for (const svc of quote.services) {
		if (svc.is_locked) {
			hasLocked = true;
			if (svc.status === "Approved") {
				priorAuthorized += Number(svc.authorized_amount ?? svc.total_amount ?? 0);
			}
		} else if (canAuthorize) {
			decisions[svc.name] = svc.status === "Declined" ? "Declined" : "Approved";
		}
	}

	const servicesHtml = quote.services
		.map((svc, idx) => {
			const isLocked = Boolean(svc.is_locked || !canAuthorize);
			const currentStatus = isLocked ? svc.status : decisions[svc.name] || "Approved";
			const cardState = currentStatus === "Declined" ? "is-declined" : "is-approved";
			const parts = Array.isArray(svc.parts) ? svc.parts : [];
			const hasLines = parts.length > 0 || Number(svc.labor_amount || 0) > 0 || Number(svc.labor_hours || 0) > 0;

			return `
				<div class="portal-service-card ${cardState}" data-service-id="${escapeHtml(svc.name)}" data-idx="${idx}" data-amount="${Number(svc.total_amount || 0)}" data-locked="${isLocked ? "true" : "false"}">
					<div class="portal-service-card__head">
						<div>
							${svc.category ? `<span class="portal-badge portal-badge--neutral">${escapeHtml(svc.category)}</span> ` : ""}
							<strong>${escapeHtml(svc.service_title || "Repair Service")}</strong>
							${svc.is_supplement ? ` <span class="portal-badge portal-badge--warning">Supplement</span>` : ""}
						</div>
						<strong>${formatCurrency(svc.total_amount || 0)}</strong>
					</div>
					<div class="portal-service-card__body">
						${
							hasLines
								? `
							<table class="portal-table">
								<thead>
									<tr>
										<th>Operation / Part</th>
										<th class="is-right">Qty</th>
										<th class="is-right">Amount</th>
									</tr>
								</thead>
								<tbody>
									${
										Number(svc.labor_amount || 0) > 0 || Number(svc.labor_hours || 0) > 0
											? `
										<tr>
											<td>Labor <span class="portal-badge portal-badge--neutral">Shop Labor</span></td>
											<td class="is-right">${Number(svc.labor_hours || 0).toFixed(2)} hrs</td>
											<td class="is-right">${formatCurrency(svc.labor_amount || 0)}</td>
										</tr>
									`
											: ""
									}
									${parts
										.map(
											(part) => `
										<tr>
											<td>
												<div>${escapeHtml(part.item_name || part.item_code || "Part")}</div>
												${renderPartDisclosures(part)}
											</td>
											<td class="is-right">${escapeHtml(part.qty ?? 1)}</td>
											<td class="is-right">${formatCurrency(part.amount || 0)}</td>
										</tr>
									`
										)
										.join("")}
								</tbody>
							</table>
						`
								: ""
						}
						<div class="portal-decision-row">
							${
								isLocked
									? `
								<span class="portal-help">Decision recorded</span>
								<span class="portal-badge ${svc.status === "Approved" ? "portal-badge--success" : "portal-badge--neutral"}">
									${svc.status === "Approved" ? `Approved (${formatCurrency(svc.authorized_amount ?? svc.total_amount ?? 0)})` : "Declined / Deferred"}
								</span>
							`
									: `
								<span class="portal-help"><strong>Action Required:</strong> Select your decision</span>
								<div class="portal-decision-group">
									<button type="button" class="portal-decision-btn ${currentStatus === "Approved" ? "is-approve-active" : ""}" data-decision-for="${escapeHtml(svc.name)}" data-decision="Approved">
										Approve
									</button>
									<button type="button" class="portal-decision-btn ${currentStatus === "Declined" ? "is-decline-active" : ""}" data-decision-for="${escapeHtml(svc.name)}" data-decision="Declined">
										Decline
									</button>
								</div>
							`
							}
						</div>
					</div>
				</div>
			`;
		})
		.join("");

	const tolerancePct = Number(quote.cost_tolerance_percentage || 0);
	const toleranceFlat = Number(quote.cost_tolerance_flat_amount || 0);
	const toleranceNotice =
		tolerancePct || toleranceFlat
			? `<div class="portal-help" style="margin-top:6px;">Statutory Estimate Variance Cap: final charges cannot exceed the authorized estimate by more than ${tolerancePct.toFixed(0)}%${toleranceFlat ? ` (or ${formatCurrency(toleranceFlat)})` : ""} without prior customer re-authorization.</div>`
			: "";

	container.innerHTML = `
		<section class="portal-card" id="portal-quote-section">
			<div class="portal-card__header">
				<div>
					<h2 class="portal-card__title">Repair Estimate &amp; Authorization</h2>
					<p class="portal-card__subtitle">Estimate #${escapeHtml(visit.repair_order || "")}</p>
				</div>
				<span class="portal-badge ${canAuthorize ? "portal-badge--warning" : "portal-badge--success"}">
					${canAuthorize ? "Authorization Needed" : "Authorization Recorded"}
				</span>
			</div>
			${servicesHtml}
			<div class="portal-summary-box">
				${
					hasLocked && canAuthorize
						? `
					<div class="portal-summary-row">
						<span>Prior Authorized Amount</span>
						<strong id="portal-quote-prior-total">${formatCurrency(priorAuthorized)}</strong>
					</div>
					<div class="portal-summary-row">
						<span>Newly Authorized (This Session)</span>
						<strong id="portal-quote-new-total">$0.00 CAD</strong>
					</div>
				`
						: ""
				}
				<div class="portal-summary-row portal-summary-row--total">
					<span>Total Authorized Amount</span>
					<span id="portal-quote-grand-total">${formatCurrency(quote.net_total ?? quote.grand_total ?? 0)}</span>
				</div>
				<div class="portal-help">Plus applicable Ontario HST and shop fees upon final invoicing.</div>
				${toleranceNotice}
			</div>
			${
				canAuthorize
					? `
				<form id="portal-quote-form" novalidate>
					<div class="portal-field">
						<label for="portal-signer-name">Authorized Customer Full Name *</label>
						<input id="portal-signer-name" class="portal-input" type="text" value="${escapeHtml(quote.customer_name || "")}" required />
					</div>
					<div class="portal-field">
						<label>Draw Your Signature with Finger, Stylus, or Mouse *</label>
						<div class="portal-sig-frame">
							<canvas id="portal-sig-canvas" class="portal-sig-canvas" width="700" height="160"></canvas>
						</div>
						<div>
							<button type="button" class="portal-btn portal-btn--outline portal-btn--sm" id="portal-sig-clear-btn">Clear Signature</button>
						</div>
					</div>
					<div class="portal-field">
						<label style="display:flex; align-items:flex-start; gap:10px; font-weight:400;">
							<input type="checkbox" id="portal-quote-disclaimer" style="margin-top:4px; width:18px; height:18px;" required />
							<span class="portal-help">${escapeHtml(quote.disclaimer_text || "I authorize the selected repair services and parts disclosures above.")}</span>
						</label>
					</div>
					<div id="portal-quote-error" class="portal-alert portal-alert--danger is-hidden" role="alert"></div>
					<button type="submit" class="portal-btn portal-btn--primary portal-btn--block" id="portal-quote-submit-btn">
						Submit Authorization
					</button>
				</form>
			`
					: ""
			}
			${renderApprovalLogs(quote.approval_logs, quote.audit_hash)}
		</section>
	`;

	if (!canAuthorize) {
		return;
	}

	const updateTotals = () => {
		let newTotal = 0;
		for (const svc of quote.services) {
			if (!svc.is_locked && decisions[svc.name] === "Approved") {
				newTotal += Number(svc.total_amount || 0);
			}
		}
		const newEl = container.querySelector("#portal-quote-new-total");
		if (newEl) {
			newEl.textContent = formatCurrency(newTotal);
		}
		const grandEl = container.querySelector("#portal-quote-grand-total");
		if (grandEl) {
			grandEl.textContent = formatCurrency(hasLocked ? priorAuthorized + newTotal : newTotal);
		}
	};

	container.querySelectorAll("[data-decision-for]").forEach((btn) => {
		btn.addEventListener("click", () => {
			const serviceId = btn.dataset.decisionFor;
			const decision = btn.dataset.decision;
			decisions[serviceId] = decision;
			const card = container.querySelector(`.portal-service-card[data-service-id="${CSS.escape(serviceId)}"]`);
			if (card) {
				card.classList.toggle("is-approved", decision === "Approved");
				card.classList.toggle("is-declined", decision === "Declined");
				card.querySelectorAll("[data-decision-for]").forEach((sib) => {
					sib.classList.toggle("is-approve-active", sib.dataset.decision === "Approved" && decision === "Approved");
					sib.classList.toggle("is-decline-active", sib.dataset.decision === "Declined" && decision === "Declined");
				});
			}
			updateTotals();
		});
	});

	updateTotals();

	// HTML5 Touch/Mouse Signature Pad
	const canvas = container.querySelector("#portal-sig-canvas");
	const ctx = canvas.getContext("2d");
	let isDrawing = false;
	let hasDrawn = false;

	const getPos = (e) => {
		const rect = canvas.getBoundingClientRect();
		const point = e.touches && e.touches[0] ? e.touches[0] : e;
		return {
			x: (point.clientX - rect.left) * (canvas.width / rect.width),
			y: (point.clientY - rect.top) * (canvas.height / rect.height),
		};
	};

	const startDraw = (e) => {
		isDrawing = true;
		hasDrawn = true;
		const pos = getPos(e);
		ctx.beginPath();
		ctx.moveTo(pos.x, pos.y);
		if (e.type.startsWith("touch")) {
			e.preventDefault();
		}
	};

	const moveDraw = (e) => {
		if (!isDrawing) {
			return;
		}
		const pos = getPos(e);
		ctx.lineWidth = 2.5;
		ctx.lineCap = "round";
		ctx.strokeStyle = "#0f172a";
		ctx.lineTo(pos.x, pos.y);
		ctx.stroke();
		if (e.type.startsWith("touch")) {
			e.preventDefault();
		}
	};

	const stopDraw = () => {
		isDrawing = false;
	};

	canvas.addEventListener("mousedown", startDraw);
	canvas.addEventListener("mousemove", moveDraw);
	window.addEventListener("mouseup", stopDraw);
	canvas.addEventListener("touchstart", startDraw, { passive: false });
	canvas.addEventListener("touchmove", moveDraw, { passive: false });
	window.addEventListener("touchend", stopDraw);

	container.querySelector("#portal-sig-clear-btn").addEventListener("click", () => {
		ctx.clearRect(0, 0, canvas.width, canvas.height);
		hasDrawn = false;
	});

	const errorEl = container.querySelector("#portal-quote-error");
	const showError = (msg) => {
		errorEl.textContent = msg;
		errorEl.classList.remove("is-hidden");
	};

	const form = container.querySelector("#portal-quote-form");
	const submitBtn = container.querySelector("#portal-quote-submit-btn");

	form.addEventListener("submit", async (e) => {
		e.preventDefault();
		errorEl.classList.add("is-hidden");

		const disclaimerChecked = container.querySelector("#portal-quote-disclaimer").checked;
		if (!disclaimerChecked) {
			showError("Please accept the authorization disclaimer before submitting.");
			return;
		}

		const signerName = (container.querySelector("#portal-signer-name").value || "").trim();
		if (!signerName) {
			showError("Please enter your full name.");
			return;
		}

		const approvalsList = Object.entries(decisions).map(([job_id, status]) => ({ job_id, status }));
		const hasApproved = approvalsList.some((item) => item.status === "Approved");
		let signatureData = null;

		if (hasApproved) {
			if (!hasDrawn) {
				showError("Please draw your signature to authorize approved repairs.");
				return;
			}
			signatureData = canvas.toDataURL("image/png");
		}

		submitBtn.disabled = true;
		submitBtn.textContent = "Submitting Authorization...";
		try {
			const res = await submitPortalQuoteApproval({
				repair_order: visit.repair_order,
				approvals: approvalsList,
				signer_name: signerName,
				signature_data: signatureData,
			});
			if (notify) {
				notify("Repair authorization recorded.", "success");
			}
			if (res && res.hub) {
				onHubUpdated(res.hub);
			}
			if (res && res.pin_prompt_required && onPromptPin) {
				onPromptPin(visit.repair_order);
			}
		} catch (err) {
			submitBtn.disabled = false;
			submitBtn.textContent = "Submit Authorization";
			showError(err.message || "Unable to submit authorization. Please try again.");
		}
	});
}

export function renderPinSection(container, { visit, onHubUpdated, notify }) {
	const pin = visit?.pin_to_drive;
	if (!visit?.repair_order || !pin) {
		container.innerHTML = "";
		return;
	}

	const status = pin.status || "Not Prompted";
	const canSubmit = Boolean(pin.can_submit && !visit.is_read_only);
	const canUpdate = Boolean(pin.can_update && !visit.is_read_only);

	let bodyHtml = "";
	let badgeClass = "portal-badge--neutral";

	if (status === "Provided") {
		badgeClass = "portal-badge--success";
		bodyHtml = `
			<div style="display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:12px;">
				<div>
					<div class="portal-pin-masked" aria-label="PIN-to-Drive stored">${escapeHtml(pin.masked_display || "••••")}</div>
					<p class="portal-help" style="margin:6px 0 0;">Stored encrypted for shop technicians only. Automatically erased when your vehicle is released.</p>
				</div>
				${
					canUpdate
						? `<button type="button" class="portal-btn portal-btn--outline portal-btn--sm" id="portal-pin-open-btn">Update PIN</button>`
						: `<span class="portal-help">Locked once work begins in bay.</span>`
				}
			</div>
		`;
	} else if (status === "Opted Out") {
		badgeClass = "portal-badge--neutral";
		bodyHtml = `
			<div style="display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:12px;">
				<p class="portal-help" style="margin:0;">You indicated that <strong>No PIN-to-Drive is enabled</strong> on this vehicle.</p>
				${
					canUpdate
						? `<button type="button" class="portal-btn portal-btn--outline portal-btn--sm" id="portal-pin-open-btn">Add 4-Digit PIN</button>`
						: ""
				}
			</div>
		`;
	} else if (status === "Purged") {
		badgeClass = "portal-badge--neutral";
		bodyHtml = `<p class="portal-help" style="margin:0;">Your PIN-to-Drive was permanently erased upon vehicle release.</p>`;
	} else {
		badgeClass = pin.prompt_required ? "portal-badge--warning" : "portal-badge--neutral";
		bodyHtml = `
			<div style="display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:12px;">
				<p class="portal-help" style="margin:0;">${escapeHtml(pin.disclaimer || "If your vehicle uses PIN-to-Drive, enter the 4-digit PIN so technicians can move your vehicle safely into the service bay.")}</p>
				${
					canSubmit || canUpdate
						? `<button type="button" class="portal-btn portal-btn--primary portal-btn--sm" id="portal-pin-open-btn">Set PIN-to-Drive</button>`
						: ""
				}
			</div>
		`;
	}

	container.innerHTML = `
		<section class="portal-card" id="portal-pin-section">
			<div class="portal-card__header">
				<div>
					<h2 class="portal-card__title">Vehicle PIN-to-Drive</h2>
					<p class="portal-card__subtitle">Write-only encrypted vault</p>
				</div>
				<span class="portal-badge ${badgeClass}">${escapeHtml(status)}</span>
			</div>
			${bodyHtml}
		</section>
	`;

	const openBtn = container.querySelector("#portal-pin-open-btn");
	if (openBtn) {
		openBtn.addEventListener("click", () => {
			openPinModal({
				repairOrder: visit.repair_order,
				pinState: pin,
				isUpdate: status === "Provided",
				onHubUpdated,
				notify,
			});
		});
	}
}

export function openPinModal({ repairOrder, pinState, isUpdate = false, onHubUpdated, notify }) {
	const existing = document.querySelector("#portal-pin-modal-backdrop");
	if (existing) {
		existing.remove();
	}

	const backdrop = document.createElement("div");
	backdrop.id = "portal-pin-modal-backdrop";
	backdrop.className = "portal-modal-backdrop";
	backdrop.setAttribute("role", "dialog");
	backdrop.setAttribute("aria-modal", "true");
	backdrop.innerHTML = `
		<div class="portal-modal">
			<div class="portal-modal__head">
				<h2 class="portal-modal__title">${isUpdate ? "Update Vehicle PIN-to-Drive" : "Does Your Vehicle Use PIN-to-Drive?"}</h2>
				<button type="button" class="portal-btn portal-btn--outline portal-btn--sm" id="portal-pin-close-btn" aria-label="Close">Close</button>
			</div>
			<p class="portal-help" style="margin-top:0;">
				${escapeHtml(pinState?.disclaimer || "Your 4-digit PIN-to-Drive is encrypted at rest, never shown again after submission, and automatically purged when your vehicle is released.")}
			</p>
			<div id="portal-pin-error" class="portal-alert portal-alert--danger is-hidden" role="alert"></div>
			<form id="portal-pin-form" novalidate>
				<div class="portal-field">
					<label for="portal-pin-input">4-Digit Numeric PIN-to-Drive</label>
					<input
						id="portal-pin-input"
						class="portal-input portal-input--pin"
						type="password"
						inputmode="numeric"
						pattern="\\d{4}"
						maxlength="4"
						autocomplete="off"
						placeholder="••••"
					/>
				</div>
				<div class="portal-modal__actions">
					<button type="submit" class="portal-btn portal-btn--primary portal-btn--block" id="portal-pin-save-btn">
						Save 4-Digit PIN
					</button>
					<button type="button" class="portal-btn portal-btn--outline portal-btn--block" id="portal-pin-optout-btn">
						No PIN-to-Drive enabled
					</button>
				</div>
			</form>
		</div>
	`;
	document.body.appendChild(backdrop);

	const close = () => backdrop.remove();
	const errorEl = backdrop.querySelector("#portal-pin-error");
	const pinInput = backdrop.querySelector("#portal-pin-input");
	const saveBtn = backdrop.querySelector("#portal-pin-save-btn");
	const optOutBtn = backdrop.querySelector("#portal-pin-optout-btn");

	backdrop.querySelector("#portal-pin-close-btn").addEventListener("click", close);
	backdrop.addEventListener("click", (e) => {
		if (e.target === backdrop) {
			close();
		}
	});

	pinInput.focus();

	backdrop.querySelector("#portal-pin-form").addEventListener("submit", async (e) => {
		e.preventDefault();
		errorEl.classList.add("is-hidden");
		const pinValue = (pinInput.value || "").trim();
		if (!/^\d{4}$/.test(pinValue)) {
			errorEl.textContent = "Please enter an exact 4-digit numeric PIN (0000–9999).";
			errorEl.classList.remove("is-hidden");
			return;
		}
		saveBtn.disabled = true;
		saveBtn.textContent = "Encrypting & Saving...";
		try {
			const res = await submitPinToDrive({ repair_order: repairOrder, pin: pinValue });
			close();
			if (notify) {
				notify("PIN-to-Drive saved securely.", "success");
			}
			if (res && res.hub) {
				onHubUpdated(res.hub);
			}
		} catch (err) {
			saveBtn.disabled = false;
			saveBtn.textContent = "Save 4-Digit PIN";
			errorEl.textContent = err.message || "Unable to save PIN-to-Drive.";
			errorEl.classList.remove("is-hidden");
		}
	});

	optOutBtn.addEventListener("click", async () => {
		errorEl.classList.add("is-hidden");
		optOutBtn.disabled = true;
		optOutBtn.textContent = "Saving...";
		try {
			const res = await optOutPinToDrive({ repair_order: repairOrder });
			close();
			if (notify) {
				notify("Recorded: No PIN-to-Drive enabled.", "success");
			}
			if (res && res.hub) {
				onHubUpdated(res.hub);
			}
		} catch (err) {
			optOutBtn.disabled = false;
			optOutBtn.textContent = "No PIN-to-Drive enabled";
			errorEl.textContent = err.message || "Unable to update PIN-to-Drive preference.";
			errorEl.classList.remove("is-hidden");
		}
	});
}
