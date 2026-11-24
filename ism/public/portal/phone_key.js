/**
 * ISM Customer Portal Tesla Phone Key Top Banner & Privacy-First Modal (Vanilla ES Module).
 * - Strictly optional, non-blocking convenience for Tesla vehicles
 * - Write-only encrypted vault: invite URL is never echoed back from the server
 * - Clear release confirmation banner once the vehicle is released
 */

import { escapeHtml, removeTeslaPhoneKey, submitTeslaPhoneKey } from "./api.js";

const TESLA_URL_EXTRACT_RE = /https:\/\/[^\s<>"']+/i;

function isAllowedTeslaHost(hostname) {
	const host = String(hostname || "").trim().toLowerCase().replace(/\.$/, "");
	if (!host) return false;
	if (host === "ts.la" || host === "www.ts.la" || host === "tesla.com") {
		return true;
	}
	return host.endsWith(".tesla.com") && host.length > ".tesla.com".length;
}

function extractAndValidateTeslaUrl(rawInput) {
	const text = String(rawInput || "").trim();
	if (!text) {
		return { ok: false, error: "Please paste your Tesla Phone Key invitation link." };
	}
	const match = text.match(TESLA_URL_EXTRACT_RE);
	if (!match) {
		return {
			ok: false,
			error: "Invitation link must start with https:// (for example, https://ts.la/...).",
		};
	}
	const candidate = match[0].replace(/[.,;:!?)]+$/, "");
	let parsed;
	try {
		parsed = new URL(candidate);
	} catch {
		return { ok: false, error: "Please paste a valid Tesla invitation URL." };
	}
	if (parsed.protocol !== "https:") {
		return { ok: false, error: "Tesla invitation links must use HTTPS." };
	}
	if (parsed.username || parsed.password) {
		return { ok: false, error: "Invitation link must not include embedded credentials." };
	}
	if (!isAllowedTeslaHost(parsed.hostname)) {
		return {
			ok: false,
			error: "Only official Tesla invitation links (ts.la or tesla.com) are accepted.",
		};
	}
	if (!parsed.pathname || parsed.pathname === "/") {
		return {
			ok: false,
			error: "Please paste the complete Tesla driver invite link, not just the homepage URL.",
		};
	}
	return { ok: true, url: candidate };
}

function formatFriendlyTimestamp(isoLike) {
	if (!isoLike) return "";
	try {
		const normalized = String(isoLike).replace(" ", "T");
		const dt = new Date(normalized);
		if (Number.isNaN(dt.getTime())) {
			return String(isoLike);
		}
		return dt.toLocaleString(undefined, {
			month: "short",
			day: "numeric",
			year: "numeric",
			hour: "numeric",
			minute: "2-digit",
		});
	} catch {
		return String(isoLike);
	}
}

const ICON_KEY_LOCK = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>`;
const ICON_SHIELD_CHECK = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path><polyline points="9 12 11 14 15 10"></polyline></svg>`;

export function renderPhoneKeyTopBanner(container, { visit, onHubUpdated, notify }) {
	if (!container) return;

	const pk = visit?.phone_key;
	const hasTarget = Boolean(visit?.repair_order || visit?.schedule_entry);
	if (!hasTarget || !pk || !pk.is_tesla) {
		container.innerHTML = "";
		return;
	}

	const status = pk.status || "Not Provided";
	const isReadOnly = Boolean(visit.is_read_only);
	const canSubmit = Boolean(pk.can_submit && !isReadOnly);
	const canUpdate = Boolean(pk.can_update && !isReadOnly);

	// Hide banner completely on released/cancelled visits where customer never shared a phone key
	if (status === "Not Provided" && !canSubmit) {
		container.innerHTML = "";
		return;
	}

	let variantClass = "portal-pk-banner--optional";
	let iconHtml = ICON_KEY_LOCK;
	let badgeHtml = `<span class="portal-pk-pill portal-pk-pill--optional">Optional</span>`;
	let titleText = "Share a Tesla Phone Key (Optional)";
	let copyHtml =
		"Want smoother drop-off & service? You can send a temporary Tesla driver invite link — not required for service, encrypted at rest, and removed when your Tesla is released.";
	let actionHtml = `<button type="button" class="portal-btn portal-btn--outline portal-btn--sm" id="portal-pk-open-btn">Share Invite Link</button>`;

	if (status === "Provided") {
		variantClass = "portal-pk-banner--encrypted";
		iconHtml = ICON_SHIELD_CHECK;
		badgeHtml = `<span class="portal-pk-pill portal-pk-pill--encrypted">Encrypted on File</span>`;
		titleText = "Tesla Phone Key Invite Stored Securely";
		copyHtml =
			"Stored in our write-only encrypted vault for authorized service advisors only. Automatically removed from our shop phone and permanently deleted upon vehicle release.";
		actionHtml = canUpdate
			? `<button type="button" class="portal-btn portal-btn--outline portal-btn--sm" id="portal-pk-open-btn">Manage Link</button>`
			: `<span class="portal-pk-lock-note">Locked awaiting pickup</span>`;
	} else if (status === "Released") {
		variantClass = "portal-pk-banner--released";
		iconHtml = ICON_SHIELD_CHECK;
		badgeHtml = `<span class="portal-pk-pill portal-pk-pill--released">Key Released</span>`;
		titleText = "Tesla Phone Key Released & Removed";
		const releasedWhen = formatFriendlyTimestamp(pk.released_at || pk.purged_at);
		copyHtml = `Your Tesla Phone Key was removed from our shop device and your invitation link was permanently deleted${
			releasedWhen ? ` on <strong>${escapeHtml(releasedWhen)}</strong>` : ""
		}. You can also verify driver access anytime under <strong>Security &amp; Drivers</strong> in your Tesla app.`;
		actionHtml = "";
	} else if (status === "Purged") {
		variantClass = "portal-pk-banner--purged";
		iconHtml = ICON_KEY_LOCK;
		badgeHtml = `<span class="portal-pk-pill portal-pk-pill--purged">Permanently Deleted</span>`;
		titleText = "Tesla Phone Key Invite Erased";
		const purgedWhen = formatFriendlyTimestamp(pk.purged_at);
		copyHtml = `Your Tesla Phone Key invitation link was permanently deleted from our encrypted vault${
			purgedWhen ? ` on <strong>${escapeHtml(purgedWhen)}</strong>` : ""
		}.`;
		actionHtml = "";
	}

	container.innerHTML = `
		<aside class="portal-pk-banner ${variantClass}" id="portal-phone-key-banner" aria-label="Tesla Phone Key privacy status">
			<div class="portal-pk-banner__icon">${iconHtml}</div>
			<div class="portal-pk-banner__body">
				<div class="portal-pk-banner__head">
					<strong class="portal-pk-banner__title">${escapeHtml(titleText)}</strong>
					${badgeHtml}
				</div>
				<p class="portal-pk-banner__copy">${copyHtml}</p>
			</div>
			${actionHtml ? `<div class="portal-pk-banner__action">${actionHtml}</div>` : ""}
		</aside>
	`;

	const openBtn = container.querySelector("#portal-pk-open-btn");
	if (openBtn) {
		openBtn.addEventListener("click", () => {
			openPhoneKeyModal({
				repairOrder: visit.repair_order || null,
				scheduleEntry: visit.schedule_entry || null,
				phoneKeyState: pk,
				onHubUpdated,
				notify,
			});
		});
	}
}

export function openPhoneKeyModal({
	repairOrder = null,
	scheduleEntry = null,
	phoneKeyState,
	onHubUpdated,
	notify,
}) {
	const existing = document.querySelector("#portal-pk-modal-backdrop");
	if (existing) {
		existing.remove();
	}

	const isUpdate = phoneKeyState?.status === "Provided";
	const canRemove = Boolean(phoneKeyState?.can_remove && isUpdate);

	const backdrop = document.createElement("div");
	backdrop.id = "portal-pk-modal-backdrop";
	backdrop.className = "portal-modal-backdrop";
	backdrop.setAttribute("role", "dialog");
	backdrop.setAttribute("aria-modal", "true");
	backdrop.setAttribute("aria-labelledby", "portal-pk-modal-title");

	backdrop.innerHTML = `
		<div class="portal-modal portal-pk-modal">
			<div class="portal-modal__head">
				<div>
					<div class="portal-pk-trust-kicker">${ICON_SHIELD_CHECK}<span>Write-Only Encrypted Vault</span></div>
					<h2 class="portal-modal__title" id="portal-pk-modal-title">${
						isUpdate ? "Manage Tesla Phone Key Invite" : "Share a Tesla Phone Key (Optional)"
					}</h2>
				</div>
				<button type="button" class="portal-btn portal-btn--outline portal-btn--sm" id="portal-pk-close-btn" aria-label="Close">Close</button>
			</div>

			<div class="portal-pk-note">
				<strong>Optional convenience:</strong> Unlike PIN-to-Drive, sharing a Phone Key is <em>not</em> required to service your Tesla. It simply lets our advisor open &amp; move your car without swapping key cards.
			</div>

			<ol class="portal-pk-steps">
				<li>Open the <strong>Tesla app</strong> on your phone and select your vehicle.</li>
				<li>Tap <strong>Security &amp; Drivers → Manage Drivers → Add Driver</strong>.</li>
				<li>Copy the invitation link (<code>https://ts.la/...</code>) and paste it securely below.</li>
			</ol>

			<div id="portal-pk-error" class="portal-alert portal-alert--danger is-hidden" role="alert"></div>

			<form id="portal-pk-form" novalidate>
				<div class="portal-field">
					<label for="portal-pk-input">${
						isUpdate ? "New Tesla Driver Invitation Link" : "Tesla Driver Invitation Link"
					}</label>
					<input
						id="portal-pk-input"
						class="portal-input portal-pk-input"
						type="url"
						autocomplete="off"
						autocorrect="off"
						autocapitalize="off"
						spellcheck="false"
						placeholder="https://ts.la/..."
					/>
					<p class="portal-help" id="portal-pk-live-hint" style="margin:6px 0 0;">
						${
							isUpdate
								? "Your previously saved link is write-only and hidden for your security. Paste a new link below to replace it."
								: "You can paste the full Tesla share message — we'll extract only the official HTTPS invite link."
						}
					</p>
				</div>

				<div class="portal-pk-guarantees">
					<div class="portal-pk-guarantees__title">Your Privacy Protections</div>
					<ul>
						<li><strong>Encrypted &amp; write-only:</strong> Encrypted at rest and never shown again on this portal after saving.</li>
						<li><strong>Advisor-only audited access:</strong> Only authorized service advisors &amp; managers can open the invite, and every access is logged.</li>
						<li><strong>Removed at release + SMS confirmation:</strong> Permanently deleted from our system and removed from our shop phone when your Tesla is released, with an SMS confirmation sent to you.</li>
					</ul>
				</div>

				<div class="portal-modal__actions portal-pk-actions">
					<button type="submit" class="portal-btn portal-btn--primary portal-btn--block" id="portal-pk-save-btn">
						${isUpdate ? "Encrypt & Update Invite Link" : "Encrypt & Share Invite Link"}
					</button>
					${
						canRemove
							? `<button type="button" class="portal-btn portal-btn--danger-outline portal-btn--block" id="portal-pk-remove-btn">Remove Saved Invite Link</button>`
							: `<button type="button" class="portal-btn portal-btn--outline portal-btn--block" id="portal-pk-skip-btn">Not Now</button>`
					}
				</div>
			</form>
		</div>
	`;

	document.body.appendChild(backdrop);

	const close = () => backdrop.remove();
	const errorEl = backdrop.querySelector("#portal-pk-error");
	const inputEl = backdrop.querySelector("#portal-pk-input");
	const hintEl = backdrop.querySelector("#portal-pk-live-hint");
	const saveBtn = backdrop.querySelector("#portal-pk-save-btn");
	const skipBtn = backdrop.querySelector("#portal-pk-skip-btn");
	const removeBtn = backdrop.querySelector("#portal-pk-remove-btn");

	backdrop.querySelector("#portal-pk-close-btn").addEventListener("click", close);
	if (skipBtn) {
		skipBtn.addEventListener("click", close);
	}
	backdrop.addEventListener("click", (e) => {
		if (e.target === backdrop) {
			close();
		}
	});

	inputEl.addEventListener("input", () => {
		const raw = (inputEl.value || "").trim();
		if (!raw) {
			errorEl.classList.add("is-hidden");
			return;
		}
		const check = extractAndValidateTeslaUrl(raw);
		if (check.ok) {
			errorEl.classList.add("is-hidden");
			hintEl.textContent = "Verified official Tesla HTTPS invitation link ready to encrypt.";
		}
	});

	inputEl.focus();

	backdrop.querySelector("#portal-pk-form").addEventListener("submit", async (e) => {
		e.preventDefault();
		errorEl.classList.add("is-hidden");
		const validation = extractAndValidateTeslaUrl(inputEl.value);
		if (!validation.ok) {
			errorEl.textContent = validation.error;
			errorEl.classList.remove("is-hidden");
			return;
		}

		saveBtn.disabled = true;
		saveBtn.textContent = "Encrypting & Saving...";
		try {
			const res = await submitTeslaPhoneKey({
				repair_order: repairOrder || undefined,
				schedule_entry: !repairOrder ? scheduleEntry || undefined : undefined,
				invite_url: validation.url,
			});
			close();
			if (notify) {
				notify("Tesla Phone Key invite link encrypted and saved.", "success");
			}
			if (res && res.hub) {
				onHubUpdated(res.hub);
			}
		} catch (err) {
			saveBtn.disabled = false;
			saveBtn.textContent = isUpdate
				? "Encrypt & Update Invite Link"
				: "Encrypt & Share Invite Link";
			errorEl.textContent = err.message || "Unable to save Tesla Phone Key invitation link.";
			errorEl.classList.remove("is-hidden");
		}
	});

	if (removeBtn) {
		removeBtn.addEventListener("click", async () => {
			errorEl.classList.add("is-hidden");
			removeBtn.disabled = true;
			removeBtn.textContent = "Removing...";
			try {
				const res = await removeTeslaPhoneKey({
					repair_order: repairOrder || undefined,
					schedule_entry: !repairOrder ? scheduleEntry || undefined : undefined,
				});
				close();
				if (notify) {
					notify(
						"Tesla Phone Key invite link deleted. If our shop already accepted it, you can also revoke it in your Tesla app.",
						"success"
					);
				}
				if (res && res.hub) {
					onHubUpdated(res.hub);
				}
			} catch (err) {
				removeBtn.disabled = false;
				removeBtn.textContent = "Remove Saved Invite Link";
				errorEl.textContent = err.message || "Unable to remove Tesla Phone Key link.";
				errorEl.classList.remove("is-hidden");
			}
		});
	}
}
