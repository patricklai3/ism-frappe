/**
 * Path 2 SMS-Only OTP Gate, Persistent Sign-Out, and Optional Account Scope Upgrade.
 * Strictly zero email OTP option and zero pre-OTP PII.
 */

import { escapeHtml, requestLoginOtp, signOut, verifyLoginOtp } from "./api.js";

function buildSmsOtpFlow(container, { shop, title, subtitle, tokenError, upgradeExistingSession, onSuccess, onCancel }) {
	const codeLength = Number(shop?.otp_code_length || 6);
	let challengeId = null;
	let submittedPhone = "";
	let cooldownTimer = null;

	const clearTimer = () => {
		if (cooldownTimer) {
			window.clearInterval(cooldownTimer);
			cooldownTimer = null;
		}
	};

	const renderPhoneStep = (errorMsg = "") => {
		clearTimer();
		container.innerHTML = `
			<div class="portal-card">
				<div class="portal-card__header">
					<div>
						<h2 class="portal-card__title">${escapeHtml(title)}</h2>
						<p class="portal-card__subtitle">${escapeHtml(subtitle)}</p>
					</div>
				</div>
				${tokenError ? `<div class="portal-alert portal-alert--warning" role="alert">${escapeHtml(tokenError)}</div>` : ""}
				${errorMsg ? `<div class="portal-alert portal-alert--danger" role="alert">${escapeHtml(errorMsg)}</div>` : ""}
				<form id="portal-otp-request-form" novalidate>
					<div class="portal-field">
						<label for="portal-phone-input">Mobile Phone Number</label>
						<input
							id="portal-phone-input"
							class="portal-input"
							type="tel"
							inputmode="tel"
							autocomplete="tel"
							placeholder="(416) 555-0199"
							value="${escapeHtml(submittedPhone)}"
							required
						/>
						<span class="portal-help">We will text a one-time SMS verification code to your mobile number.</span>
					</div>
					<div class="portal-inline-actions">
						<button type="submit" class="portal-btn portal-btn--primary portal-btn--block" id="portal-send-otp-btn">
							Send SMS Verification Code
						</button>
						${onCancel ? `<button type="button" class="portal-btn portal-btn--outline portal-btn--block" id="portal-otp-cancel-btn">Cancel</button>` : ""}
					</div>
				</form>
			</div>
		`;

		const form = container.querySelector("#portal-otp-request-form");
		const phoneInput = container.querySelector("#portal-phone-input");
		const submitBtn = container.querySelector("#portal-send-otp-btn");
		const cancelBtn = container.querySelector("#portal-otp-cancel-btn");
		if (cancelBtn && onCancel) {
			cancelBtn.addEventListener("click", () => {
				clearTimer();
				onCancel();
			});
		}

		form.addEventListener("submit", async (e) => {
			e.preventDefault();
			const rawPhone = (phoneInput.value || "").trim();
			if (!rawPhone) {
				renderPhoneStep("Please enter your mobile phone number.");
				return;
			}
			if (rawPhone.includes("@")) {
				renderPhoneStep("Please enter a mobile phone number. Sign-in codes are sent by SMS only.");
				return;
			}
			submittedPhone = rawPhone;
			submitBtn.disabled = true;
			submitBtn.textContent = "Sending SMS Code...";
			try {
				const res = await requestLoginOtp({ phone: rawPhone });
				challengeId = res.challenge_id;
				renderCodeStep(res);
			} catch (err) {
				renderPhoneStep(err.message || "Unable to send verification code. Please try again.");
			}
		});
	};

	const renderCodeStep = (challenge, errorMsg = "") => {
		clearTimer();
		let remaining = Number(
			challenge?.resend_cooldown_seconds ?? challenge?.resend_available_in_seconds ?? 30
		);
		if (!Number.isFinite(remaining) || remaining < 0) {
			remaining = 30;
		}

		container.innerHTML = `
			<div class="portal-card">
				<div class="portal-card__header">
					<div>
						<h2 class="portal-card__title">Enter SMS Verification Code</h2>
						<p class="portal-card__subtitle">
							${escapeHtml(challenge?.message || `If an account matches ${challenge?.phone_masked || "this number"}, a ${codeLength}-digit SMS code has been sent.`)}
						</p>
					</div>
				</div>
				${errorMsg ? `<div class="portal-alert portal-alert--danger" role="alert">${escapeHtml(errorMsg)}</div>` : ""}
				<form id="portal-otp-verify-form" novalidate>
					<div class="portal-field">
						<label for="portal-otp-code-input">${codeLength}-Digit SMS Code</label>
						<input
							id="portal-otp-code-input"
							class="portal-input portal-input--otp"
							type="text"
							inputmode="numeric"
							autocomplete="one-time-code"
							maxlength="${codeLength}"
							placeholder="${"•".repeat(codeLength)}"
							required
						/>
					</div>
					<div class="portal-modal__actions">
						<button type="submit" class="portal-btn portal-btn--primary portal-btn--block" id="portal-verify-otp-btn">
							Verify &amp; Continue
						</button>
						<button type="button" class="portal-btn portal-btn--outline portal-btn--block" id="portal-resend-otp-btn" ${remaining > 0 ? "disabled" : ""}>
							${remaining > 0 ? `Resend SMS in ${remaining}s` : "Resend SMS Code"}
						</button>
						<button type="button" class="portal-btn portal-btn--outline portal-btn--block" id="portal-change-phone-btn">
							Use a Different Phone Number
						</button>
						${onCancel ? `<button type="button" class="portal-btn portal-btn--outline portal-btn--block" id="portal-otp-cancel-btn">Cancel</button>` : ""}
					</div>
				</form>
			</div>
		`;

		const form = container.querySelector("#portal-otp-verify-form");
		const codeInput = container.querySelector("#portal-otp-code-input");
		const verifyBtn = container.querySelector("#portal-verify-otp-btn");
		const resendBtn = container.querySelector("#portal-resend-otp-btn");
		const changePhoneBtn = container.querySelector("#portal-change-phone-btn");
		const cancelBtn = container.querySelector("#portal-otp-cancel-btn");

		codeInput.focus();

		if (remaining > 0) {
			cooldownTimer = window.setInterval(() => {
				remaining -= 1;
				if (remaining <= 0) {
					clearTimer();
					resendBtn.disabled = false;
					resendBtn.textContent = "Resend SMS Code";
				} else {
					resendBtn.textContent = `Resend SMS in ${remaining}s`;
				}
			}, 1000);
		}

		resendBtn.addEventListener("click", async () => {
			resendBtn.disabled = true;
			resendBtn.textContent = "Sending...";
			try {
				const next = await requestLoginOtp({ phone: submittedPhone });
				challengeId = next.challenge_id;
				renderCodeStep(next);
			} catch (err) {
				renderCodeStep(challenge, err.message || "Unable to resend SMS code.");
			}
		});

		changePhoneBtn.addEventListener("click", () => {
			renderPhoneStep();
		});

		if (cancelBtn && onCancel) {
			cancelBtn.addEventListener("click", () => {
				clearTimer();
				onCancel();
			});
		}

		form.addEventListener("submit", async (e) => {
			e.preventDefault();
			const code = (codeInput.value || "").replace(/\s+/g, "");
			if (!code) {
				renderCodeStep(challenge, "Please enter the SMS verification code.");
				return;
			}
			verifyBtn.disabled = true;
			verifyBtn.textContent = "Verifying...";
			try {
				const res = await verifyLoginOtp({
					challenge_id: challengeId,
					code,
					phone: submittedPhone,
					upgrade_existing_session: Boolean(upgradeExistingSession),
				});
				if (!res || !res.verified || !res.hub) {
					renderCodeStep(challenge, res?.error || "Invalid or expired verification code.");
					return;
				}
				clearTimer();
				onSuccess(res.hub);
			} catch (err) {
				renderCodeStep(challenge, err.message || "Invalid or expired verification code.");
			}
		});
	};

	renderPhoneStep();
}

export function renderAuthGate(container, { shop, tokenError, onAuthenticated }) {
	const shopName = shop?.company_name || "Customer Portal";
	const supportPhone = shop?.support_phone || "";
	const pickupHours = shop?.pickup_hours_text || "";

	container.innerHTML = `
		<div class="portal-container">
			<header class="portal-header">
				<div class="portal-header__brand">
					<h1>${escapeHtml(shopName)}</h1>
					<div class="portal-header__meta">
						${supportPhone ? `<span>Support: ${escapeHtml(supportPhone)}</span>` : ""}
						${pickupHours ? `<span>Hours: ${escapeHtml(pickupHours)}</span>` : ""}
					</div>
				</div>
			</header>
			<div id="portal-auth-gate-slot"></div>
		</div>
	`;

	const slot = container.querySelector("#portal-auth-gate-slot");
	buildSmsOtpFlow(slot, {
		shop,
		title: "Sign In to Your Visit Portal",
		subtitle: "Enter your mobile phone number to receive a one-time SMS sign-in code.",
		tokenError,
		upgradeExistingSession: false,
		onSuccess: onAuthenticated,
	});
}

export function renderHeaderAuthControls(container, { session, onSignOut, onOpenUpgrade, notify }) {
	const canUpgrade = Boolean(session?.can_upgrade_to_account_view);
	container.innerHTML = `
		${
			canUpgrade
				? `<button type="button" class="portal-btn portal-btn--ghost-light" id="portal-upgrade-scope-btn">View all visits on your phone</button>`
				: ""
		}
		<button type="button" class="portal-btn portal-btn--ghost-light" id="portal-sign-out-btn">
			Sign out on this device
		</button>
	`;

	const upgradeBtn = container.querySelector("#portal-upgrade-scope-btn");
	if (upgradeBtn && onOpenUpgrade) {
		upgradeBtn.addEventListener("click", () => onOpenUpgrade());
	}

	const signOutBtn = container.querySelector("#portal-sign-out-btn");
	signOutBtn.addEventListener("click", async () => {
		signOutBtn.disabled = true;
		signOutBtn.textContent = "Signing out...";
		try {
			await signOut();
			if (notify) {
				notify("Signed out on this device.", "success");
			}
			onSignOut();
		} catch (err) {
			signOutBtn.disabled = false;
			signOutBtn.textContent = "Sign out on this device";
			if (notify) {
				notify(err.message || "Unable to sign out right now.", "error");
			}
		}
	});
}

export function openScopeUpgradeModal({ shop, onUpgraded, notify }) {
	const backdrop = document.createElement("div");
	backdrop.className = "portal-modal-backdrop";
	backdrop.setAttribute("role", "dialog");
	backdrop.setAttribute("aria-modal", "true");
	backdrop.innerHTML = `<div class="portal-modal" id="portal-upgrade-modal-body"></div>`;
	document.body.appendChild(backdrop);

	const close = () => {
		backdrop.remove();
	};

	backdrop.addEventListener("click", (e) => {
		if (e.target === backdrop) {
			close();
		}
	});

	const modalBody = backdrop.querySelector("#portal-upgrade-modal-body");
	buildSmsOtpFlow(modalBody, {
		shop,
		title: "View All Visits on Your Phone",
		subtitle: "Verify your mobile number via SMS to see all service visits and bookings linked to your phone.",
		tokenError: null,
		upgradeExistingSession: true,
		onCancel: close,
		onSuccess: (hub) => {
			close();
			if (notify) {
				notify("All visits linked to your phone are now unlocked.", "success");
			}
			onUpgraded(hub);
		},
	});
}
