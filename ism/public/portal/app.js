/**
 * ISM Unified Customer Portal Bootstrap & Visit Hub Controller (Vanilla ES Module).
 * - Strips ?token / ?booking_token immediately on boot via window.history.replaceState
 * - Routes between Path 2 SMS-only OTP Gate and Authenticated Visit Hub
 * - Strictly binary status banners (Scheduled, Being Worked On, Done & Ready for Pickup, Cancelled)
 *   with ZERO ETA, countdowns, progress bars, or mid-work checkmarks
 */

import {
	escapeHtml,
	getPortalBootstrap,
	getPortalHub,
	setCsrfToken,
	stripQueryTokens,
} from "./api.js";
import { openScopeUpgradeModal, renderAuthGate, renderHeaderAuthControls } from "./auth.js";
import { renderInvoicesSection } from "./invoices.js";
import { renderPaymentsSection } from "./payments.js";
import { openPinModal, renderPinSection, renderQuoteSection } from "./quote_pin.js";
import { renderAppointmentSection } from "./reschedule.js";

function ensureToastStack() {
	let stack = document.querySelector("#portal-toast-stack");
	if (!stack) {
		stack = document.createElement("div");
		stack.id = "portal-toast-stack";
		stack.className = "portal-toast-stack";
		stack.setAttribute("aria-live", "polite");
		document.body.appendChild(stack);
	}
	return stack;
}

function notify(message, tone = "info") {
	const stack = ensureToastStack();
	const toast = document.createElement("div");
	toast.className = `portal-toast ${tone === "success" ? "portal-toast--success" : tone === "error" ? "portal-toast--error" : ""}`;
	toast.textContent = message;
	stack.appendChild(toast);
	window.setTimeout(() => {
		toast.remove();
	}, 4500);
}

function readBootConfig(root) {
	try {
		const raw = root.dataset.portalBoot;
		return raw ? JSON.parse(raw) : {};
	} catch {
		return {};
	}
}

function renderBinaryStatusBanner(status, shop) {
	const code = status?.code || "scheduled";
	const label = status?.label || "Scheduled";
	let detail = "";

	if (code === "scheduled" && status?.appointment) {
		const dt =
			status.appointment.display_datetime ||
			`${status.appointment.display_date || ""} ${status.appointment.display_time || ""}`.trim();
		detail = dt ? `Drop-off: ${dt}` : "";
	} else if (code === "ready_for_pickup") {
		const pickupHours = status?.pickup_hours || shop?.pickup_hours_text || "";
		if (status?.vehicle_released) {
			detail = "Vehicle released. Payment receipts and invoice copies remain available below.";
		} else {
			detail = pickupHours ? `Pickup hours: ${pickupHours}` : "Your vehicle is ready for pickup.";
		}
	} else if (code === "being_worked_on") {
		detail = "Your vehicle is currently being serviced by our team.";
	} else if (code === "cancelled") {
		detail = "This visit has been cancelled.";
	}

	return `
		<section class="portal-status-banner portal-status--${escapeHtml(code)}" aria-label="Visit status">
			<div class="portal-status-banner__kicker">Current Visit Status</div>
			<h2 class="portal-status-banner__label">${escapeHtml(label)}</h2>
			${detail ? `<p class="portal-status-banner__detail">${escapeHtml(detail)}</p>` : ""}
		</section>
	`;
}

function renderVehicleCard(visit) {
	const vehicle = visit?.vehicle;
	if (!vehicle) {
		return "";
	}
	const label =
		[vehicle.model_year, vehicle.manufacturer, vehicle.model].filter(Boolean).join(" ") ||
		vehicle.label ||
		"Vehicle";

	return `
		<section class="portal-card">
			<div class="portal-card__header">
				<div>
					<h2 class="portal-card__title">${escapeHtml(label)}</h2>
					<p class="portal-card__subtitle">
						${visit.repair_order ? `Repair Order #${escapeHtml(visit.repair_order)}` : `Booking #${escapeHtml(visit.schedule_entry || "")}`}
					</p>
				</div>
			</div>
			<div class="portal-grid-2">
				${
					vehicle.license_plate
						? `<div class="portal-kv"><span class="portal-kv__label">License Plate</span><span class="portal-kv__value">${escapeHtml(vehicle.license_plate)}</span></div>`
						: ""
				}
				${
					vehicle.vin
						? `<div class="portal-kv"><span class="portal-kv__label">VIN</span><span class="portal-kv__value">${escapeHtml(vehicle.vin)}</span></div>`
						: ""
				}
			</div>
		</section>
	`;
}

function renderVisitHub(root, hub, { boot, state }) {
	const shop = hub?.shop || boot.shop || {};
	const visits = Array.isArray(hub?.visits) ? hub.visits : [];
	const activeVisit = hub?.active_visit || null;

	const onHubUpdated = (nextHub) => {
		if (!nextHub) {
			return;
		}
		state.hub = nextHub;
		renderVisitHub(root, nextHub, { boot, state });
	};

	const onSignedOut = () => {
		state.hub = null;
		renderAuthGate(root, {
			shop,
			tokenError: null,
			onAuthenticated: (nextHub) => {
				state.hub = nextHub;
				renderVisitHub(root, nextHub, { boot, state });
			},
		});
	};

	const switcherHtml =
		visits.length > 1
			? `
		<nav class="portal-visit-switcher" aria-label="Your service visits">
			${visits
				.map((item) => {
					const isActive = activeVisit && item.visit_key === activeVisit.visit_key;
					const badges = Array.isArray(item.action_required_badges) ? item.action_required_badges : [];
					return `
						<button
							type="button"
							class="portal-visit-pill ${isActive ? "is-active" : ""}"
							data-visit-ro="${escapeHtml(item.repair_order || "")}"
							data-visit-se="${escapeHtml(item.schedule_entry || "")}"
						>
							<span class="portal-visit-pill__title">${escapeHtml(item.vehicle_label || item.repair_order || item.schedule_entry || "Visit")}</span>
							<span class="portal-visit-pill__meta">${escapeHtml(item.status_label || "")}</span>
							${badges.length ? `<span class="portal-badge portal-badge--warning">${escapeHtml(badges[0])}</span>` : ""}
						</button>
					`;
				})
				.join("")}
		</nav>
	`
			: "";

	const readOnlyBannerHtml =
		activeVisit && activeVisit.is_read_only
			? `
		<div class="portal-readonly-banner" role="status">
			<strong>Closed / Released (Read-Only):</strong> Mutating visit actions are locked. Your payment receipts and invoice PDF download/email remain available below.
		</div>
	`
			: "";

	root.innerHTML = `
		<div class="portal-container">
			<header class="portal-header">
				<div class="portal-header__brand">
					<h1>${escapeHtml(shop.company_name || "Customer Portal")}</h1>
					<div class="portal-header__meta">
						${shop.support_phone ? `<span>Support: ${escapeHtml(shop.support_phone)}</span>` : ""}
						${hub?.session?.phone_masked ? `<span>Signed in: ${escapeHtml(hub.session.phone_masked)}</span>` : ""}
					</div>
				</div>
				<div class="portal-header__actions" id="portal-header-auth-controls"></div>
			</header>

			${switcherHtml}

			${
				activeVisit
					? `
				${readOnlyBannerHtml}
				${renderBinaryStatusBanner(activeVisit.status, shop)}
				${renderVehicleCard(activeVisit)}
				<div id="portal-slot-appointment"></div>
				<div id="portal-slot-pin"></div>
				<div id="portal-slot-quote"></div>
				<div id="portal-slot-payments"></div>
				<div id="portal-slot-invoices"></div>
			`
					: `
				<section class="portal-card">
					<h2 class="portal-card__title">No Active Visits Found</h2>
					<p class="portal-help">There are currently no service visits or bookings linked to this session.</p>
				</section>
			`
			}
		</div>
	`;

	const headerControls = root.querySelector("#portal-header-auth-controls");
	renderHeaderAuthControls(headerControls, {
		session: hub?.session,
		onSignOut: onSignedOut,
		onOpenUpgrade: () =>
			openScopeUpgradeModal({
				shop,
				onUpgraded: onHubUpdated,
				notify,
			}),
		notify,
	});

	root.querySelectorAll(".portal-visit-pill").forEach((pill) => {
		pill.addEventListener("click", async () => {
			const nextRo = pill.dataset.visitRo || undefined;
			const nextSe = pill.dataset.visitSe || undefined;
			try {
				const nextHub = await getPortalHub({
					active_ro: nextRo,
					active_schedule_entry: nextSe,
				});
				onHubUpdated(nextHub);
			} catch (err) {
				notify(err.message || "Unable to switch visits right now.", "error");
			}
		});
	});

	if (!activeVisit) {
		return;
	}

	const triggerPinPrompt = (visit) => {
		const currentVisit = visit || state.hub?.active_visit || activeVisit;
		openPinModal({
			repairOrder: currentVisit.repair_order || null,
			scheduleEntry: currentVisit.schedule_entry || null,
			pinState: currentVisit.pin_to_drive,
			isUpdate: false,
			onHubUpdated,
			notify,
		});
	};

	renderAppointmentSection(root.querySelector("#portal-slot-appointment"), {
		visit: activeVisit,
		onHubUpdated,
		notify,
	});

	renderPinSection(root.querySelector("#portal-slot-pin"), {
		visit: activeVisit,
		onHubUpdated,
		notify,
	});

	renderQuoteSection(root.querySelector("#portal-slot-quote"), {
		visit: activeVisit,
		onHubUpdated,
		notify,
	});

	renderPaymentsSection(root.querySelector("#portal-slot-payments"), {
		visit: activeVisit,
		onHubUpdated,
		notify,
	});

	renderInvoicesSection(root.querySelector("#portal-slot-invoices"), {
		visit: activeVisit,
		notify,
	});

	// Auto-prompt PIN-to-Drive once per confirmed/scheduled visit when PIN has not been submitted yet
	const promptVisitKey = activeVisit.visit_key || activeVisit.repair_order || activeVisit.schedule_entry;
	if (
		promptVisitKey &&
		activeVisit.pin_to_drive?.prompt_required &&
		!activeVisit.is_read_only &&
		!state.pinPromptedVisits.has(promptVisitKey)
	) {
		state.pinPromptedVisits.add(promptVisitKey);
		triggerPinPrompt(activeVisit);
	}
}

async function bootPortal() {
	const root = document.querySelector("#ism-portal-root");
	if (!root) {
		return;
	}

	const boot = readBootConfig(root);
	if (boot.csrf_token) {
		setCsrfToken(boot.csrf_token);
	}

	// Immediately remove raw ?token / ?booking_token from URL bar
	stripQueryTokens();

	const state = {
		hub: null,
		pinPromptedVisits: new Set(),
	};

	if (!boot.authenticated) {
		renderAuthGate(root, {
			shop: boot.shop || {},
			tokenError: boot.token_error || null,
			onAuthenticated: (hub) => {
				state.hub = hub;
				renderVisitHub(root, hub, { boot, state });
			},
		});
		return;
	}

	try {
		const res = await getPortalBootstrap({
			active_ro: boot.active_ro || undefined,
			active_schedule_entry: boot.active_schedule_entry || undefined,
		});
		if (res && res.authenticated && res.hub) {
			state.hub = res.hub;
			renderVisitHub(root, res.hub, { boot, state });
		} else {
			renderAuthGate(root, {
				shop: (res && res.shop) || boot.shop || {},
				tokenError: boot.token_error || null,
				onAuthenticated: (hub) => {
					state.hub = hub;
					renderVisitHub(root, hub, { boot, state });
				},
			});
		}
	} catch (err) {
		renderAuthGate(root, {
			shop: boot.shop || {},
			tokenError: err.message || "Unable to restore your portal session. Please sign in with your mobile number.",
			onAuthenticated: (hub) => {
				state.hub = hub;
				renderVisitHub(root, hub, { boot, state });
			},
		});
	}
}

if (document.readyState === "loading") {
	document.addEventListener("DOMContentLoaded", bootPortal);
} else {
	bootPortal();
}
