/**
 * Hosted Self-Serve Booking Funnel UI Controller (/book).
 * Strictly XSS-safe vanilla ES module: builds all DOM nodes via createElement + textContent.
 */

import {
	BookingApiError,
	confirmSelfServeBooking,
	fetchPackages,
	fetchSlots,
	optOutBookingPinToDrive,
	requestBookingOtp,
	submitBookingPinToDrive,
} from "./api.js";
import { createBookingStore } from "./state.js";

function el(tag, attrs = null, ...children) {
	const node = document.createElement(tag);
	if (attrs) {
		for (const [key, value] of Object.entries(attrs)) {
			if (value === undefined || value === null || value === false) continue;
			if (key === "className") {
				node.className = value;
			} else if (key === "textContent") {
				node.textContent = String(value);
			} else if (key === "value") {
				node.value = String(value);
			} else if (key === "disabled") {
				node.disabled = Boolean(value);
			} else if (key === "required") {
				node.required = Boolean(value);
			} else if (key.startsWith("on") && typeof value === "function") {
				node.addEventListener(key.slice(2).toLowerCase(), value);
			} else {
				node.setAttribute(key, value === true ? "" : String(value));
			}
		}
	}
	for (const child of children) {
		if (child === undefined || child === null || child === false) continue;
		if (Array.isArray(child)) {
			for (const nested of child) {
				if (nested !== undefined && nested !== null && nested !== false) {
					node.append(typeof nested === "string" ? document.createTextNode(nested) : nested);
				}
			}
		} else {
			node.append(typeof child === "string" ? document.createTextNode(child) : child);
		}
	}
	return node;
}

function formatDatePillLabel(isoDate) {
	if (!isoDate || typeof isoDate !== "string") return { top: "", bottom: "" };
	const parts = isoDate.split("-").map(Number);
	if (parts.length !== 3 || parts.some(Number.isNaN)) {
		return { top: isoDate, bottom: "" };
	}
	const dt = new Date(parts[0], parts[1] - 1, parts[2]);
	const weekday = dt.toLocaleDateString(undefined, { weekday: "short" });
	const monthDay = dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
	return { top: weekday, bottom: monthDay };
}

function formatFullDateLabel(isoDate) {
	if (!isoDate || typeof isoDate !== "string") return "";
	const parts = isoDate.split("-").map(Number);
	if (parts.length !== 3 || parts.some(Number.isNaN)) return isoDate;
	const dt = new Date(parts[0], parts[1] - 1, parts[2]);
	return dt.toLocaleDateString(undefined, {
		weekday: "short",
		month: "short",
		day: "numeric",
		year: "numeric",
	});
}

function readBootConfig() {
	const scriptEl = document.getElementById("ism-booking-boot");
	if (!scriptEl || !scriptEl.textContent) return null;
	try {
		return JSON.parse(scriptEl.textContent);
	} catch {
		return null;
	}
}

function readHoneypotValue(bootConfig) {
	const fieldName = (bootConfig && bootConfig.honeypot_field) || "website_url";
	const byId = document.getElementById("ism-hp-website");
	if (byId && "value" in byId) return String(byId.value || "");
	const byName = document.querySelector(`input[name="${fieldName}"]`);
	return byName && "value" in byName ? String(byName.value || "") : "";
}

function initBookingApp() {
	const mountEl = document.getElementById("ism-booking-app");
	const stepperEl = document.getElementById("ism-booking-stepper");
	const statusEl = document.getElementById("ism-booking-status");
	const bootConfig = readBootConfig();

	if (!mountEl || !bootConfig || !bootConfig.enabled) {
		return;
	}

	const policy = bootConfig.policy || {};
	const shop = bootConfig.shop || {};
	const portalUrl = bootConfig.portal_url || "/portal";
	const maxPackages = Math.max(1, Number(policy.max_packages_per_booking) || 5);
	const store = createBookingStore(bootConfig);

	let resendTimerId = null;
	let pendingFocusId = null;

	function stopResendTimer() {
		if (resendTimerId !== null) {
			window.clearInterval(resendTimerId);
			resendTimerId = null;
		}
	}

	function startResendTimer() {
		stopResendTimer();
		if (store.getState().otp.resend_seconds_left <= 0) return;
		resendTimerId = window.setInterval(() => {
			store.tickResendCountdown();
			if (store.getState().otp.resend_seconds_left <= 0) {
				stopResendTimer();
			}
		}, 1000);
	}

	function selectedPackagesList(state) {
		const byId = new Map(state.packagesCatalog.map((pkg) => [pkg.package_id, pkg]));
		return state.selectedPackageIds.map((id) => byId.get(id)).filter(Boolean);
	}

	const VEHICLE_COLOR_PRESETS = [
		"Pearl White",
		"Solid Black",
		"Stealth Grey",
		"Midnight Silver",
		"Deep Blue",
		"Ultra Red",
		"Quicksilver",
	];
	let step1Attempted = false;

	function matchingGenerationsForVehicle(model, year) {
		const map = (bootConfig && bootConfig.vehicle_generations) || {};
		const list = model && Array.isArray(map[model]) ? map[model] : [];
		if (!list.length) return [];
		if (!year) return list;
		return list.filter((entry) => {
			const yf = entry.year_from ? Number(entry.year_from) : null;
			const yt = entry.year_to ? Number(entry.year_to) : null;
			if (yf && year < yf) return false;
			if (yt && year > yt) return false;
			return true;
		});
	}

	function vehicleSummaryText(state) {
		if (window.ISMVehicleDisplay) {
			return window.ISMVehicleDisplay.formatText(state.vehicle, { fallback: "" });
		}
		const parts = [];
		if (state.vehicle.year) parts.push(String(state.vehicle.year));
		if (state.vehicle.color) parts.push(state.vehicle.color.trim());
		if (state.vehicle.model) parts.push(state.vehicle.model);
		const core = parts.join(" ");
		const plate = (state.vehicle.license_plate || "").trim();
		return core && plate ? `${core} (${plate})` : core || plate;
	}

	function renderVehicleDisplayNode(vehicleObj, { showVin = false, extraClass = "ism-veh--responsive-tight" } = {}) {
		const norm = window.ISMVehicleDisplay
			? window.ISMVehicleDisplay.normalize(vehicleObj)
			: {
					year: vehicleObj && vehicleObj.year ? String(vehicleObj.year) : "",
					color: (vehicleObj && vehicleObj.color) || "",
					model: (vehicleObj && vehicleObj.model) || "",
					manufacturer: (vehicleObj && vehicleObj.manufacturer) || "",
					plate: (vehicleObj && (vehicleObj.license_plate || vehicleObj.plate)) || "",
					vin: (vehicleObj && vehicleObj.vin) || "",
					display: typeof vehicleObj === "string" ? vehicleObj : "",
			  };
		const tooltip = window.ISMVehicleDisplay
			? window.ISMVehicleDisplay.formatTooltip(norm)
			: vehicleSummaryText({ vehicle: norm });
		const modelPart = norm.model || norm.manufacturer;
		const children = [];
		if (norm.year) children.push(el("span", { className: "ism-veh__year", textContent: norm.year }));
		if (norm.color) children.push(el("span", { className: "ism-veh__color", textContent: norm.color }));
		if (modelPart) children.push(el("span", { className: "ism-veh__model", textContent: modelPart }));
		if (norm.plate) children.push(el("span", { className: "ism-veh__plate", textContent: norm.plate }));
		if (showVin && norm.vin) {
			children.push(el("span", { className: "ism-veh__vin", textContent: `VIN: ${norm.vin}` }));
		}
		if (!children.length) {
			const fb = norm.display || norm.vin || "Vehicle";
			children.push(el("span", { className: "ism-veh__fallback", textContent: fb }));
		}
		return el(
			"span",
			{
				className: `ism-veh${extraClass ? ` ${extraClass}` : ""}`,
				title: tooltip || undefined,
			},
			children
		);
	}

	function renderVehicleSummaryPill(state) {
		return el("span", { className: "ism-summary-pill ism-summary-pill--vehicle" }, renderVehicleDisplayNode(state.vehicle));
	}

	function canProceedStep1(state) {
		return Boolean(
			state.vehicle.model &&
				state.vehicle.color &&
				state.vehicle.color.trim()
		);
	}

	async function loadPackagesForVehicle() {
		const state = store.getState();
		step1Attempted = true;
		if (!state.vehicle.model) {
			store.setError("Please select your vehicle model to continue.");
			return;
		}
		if (!state.vehicle.color || !state.vehicle.color.trim()) {
			pendingFocusId = "ism-vehicle-color";
			store.setError("Please select or enter your vehicle color to continue.");
			return;
		}
		store.clearError();
		store.setLoading("packages", true);
		try {
			const res = await fetchPackages({
				vehicle_model: state.vehicle.model,
				vehicle_year: state.vehicle.year,
				vehicle_generation: state.vehicle.generation || undefined,
			});
			store.setPackagesCatalog(res.packages || []);
			store.setStep(2);
		} catch (err) {
			handleApiError(err);
		} finally {
			store.setLoading("packages", false);
		}
	}

	async function loadSlots({ append = false } = {}) {
		const state = store.getState();
		if (state.selectedPackageIds.length === 0) {
			store.setError("Please select at least one service to continue.");
			return;
		}
		store.clearError();
		const loadingKey = append ? "moreSlots" : "slots";
		store.setLoading(loadingKey, true);
		try {
			const res = await fetchSlots({
				package_ids: state.selectedPackageIds,
				start_date: append ? state.slotsWindow.nextStartDate : undefined,
				days_ahead: 14,
				vehicle_model: state.vehicle.model,
				vehicle_year: state.vehicle.year,
				vehicle_generation: state.vehicle.generation || undefined,
			});
			store.setSlotsResult(res, { append });
			if (!append) {
				store.setStep(3);
			}
		} catch (err) {
			handleApiError(err);
		} finally {
			store.setLoading(loadingKey, false);
		}
	}

	function buildBookingRequestPayload(state) {
		const color = (state.vehicle.color || "").trim();
		const licensePlate = (state.vehicle.license_plate || "").trim().toUpperCase();
		const vin = (state.vehicle.vin || "").trim().toUpperCase();
		return {
			package_ids: state.selectedPackageIds,
			target_date: state.selectedSlot ? state.selectedSlot.date : "",
			customer_arrival_time: state.selectedSlot ? state.selectedSlot.customer_arrival_time : "",
			customer_name: state.customer.customer_name.trim(),
			phone: state.customer.phone.trim(),
			email: state.customer.email.trim() || undefined,
			vehicle_model: state.vehicle.model || undefined,
			vehicle_year: state.vehicle.year || undefined,
			vehicle_color: color || undefined,
			license_plate: licensePlate || undefined,
			vin: vin || undefined,
			vehicle_generation: state.vehicle.generation || undefined,
			vin_or_plate: vin || licensePlate || undefined,
			notes: state.customer.notes.trim() || undefined,
			waiter_preference: policy.enable_waiter_preference
				? state.customer.waiter_preference
				: undefined,
			website_url: readHoneypotValue(bootConfig),
			form_nonce: bootConfig.form_nonce || undefined,
		};
	}

	function validateCustomerDetails(state) {
		if (!state.customer.customer_name.trim()) {
			store.setError("Please enter your full name.");
			return false;
		}
		if (!state.customer.phone.trim()) {
			store.setError("Please enter your mobile phone number.");
			return false;
		}
		if (policy.require_email && !state.customer.email.trim()) {
			store.setError("Please enter your email address.");
			return false;
		}
		return true;
	}

	async function handleSendOtp() {
		const state = store.getState();
		if (!validateCustomerDetails(state)) return;
		if (!state.selectedSlot) {
			store.setError("Please choose a drop-off time first.");
			return;
		}
		store.clearError();
		store.setLoading("otp", true);
		try {
			const res = await requestBookingOtp(buildBookingRequestPayload(state));
			store.setOtpRequested(res);
			startResendTimer();
			pendingFocusId = "ism-otp-code-input";
			render(store.getState());
		} catch (err) {
			if (err instanceof BookingApiError && err.errorCode === "SLOT_UNAVAILABLE") {
				store.setError(err.message, { code: err.errorCode });
				await loadSlots({ append: false });
				return;
			}
			if (err instanceof BookingApiError && err.errorCode === "VEHICLE_CONFLICT") {
				store.setError(err.message, { code: err.errorCode, showPortalLink: true });
				return;
			}
			handleApiError(err);
		} finally {
			store.setLoading("otp", false);
		}
	}

	async function handleConfirmBooking({ skipOtpCode = false } = {}) {
		const state = store.getState();
		if (!validateCustomerDetails(state)) return;
		if (!state.selectedSlot) {
			store.setError("Please choose a drop-off time first.");
			return;
		}
		if (!state.otp.challenge_id) {
			store.setError("Please send and enter your 6-digit SMS verification code first.");
			return;
		}
		const otpCode = skipOtpCode || state.otp.otp_verified ? null : state.otp.code.trim();
		if (!skipOtpCode && !state.otp.otp_verified && (!otpCode || otpCode.length < 4)) {
			store.setError("Please enter the 6-digit verification code sent to your mobile phone.");
			return;
		}

		store.clearError();
		store.setLoading("confirm", true);
		try {
			const payload = {
				...buildBookingRequestPayload(state),
				challenge_id: state.otp.challenge_id,
				otp_code: otpCode,
				idempotency_key: state.idempotency_key,
			};
			const res = await confirmSelfServeBooking(payload);
			stopResendTimer();
			store.setConfirmation(res);
		} catch (err) {
			if (err instanceof BookingApiError) {
				if (err.errorCode === "SLOT_UNAVAILABLE" && err.otpVerified) {
					store.markOtpVerifiedForSlotRetry(err.challengeId);
					await loadSlots({ append: false });
					return;
				}
				if (err.errorCode === "INVALID_CODE") {
					store.setOtpFailure({ attemptsRemaining: err.attemptsRemaining });
					const suffix =
						typeof err.attemptsRemaining === "number"
							? ` (${err.attemptsRemaining} ${err.attemptsRemaining === 1 ? "attempt" : "attempts"} remaining)`
							: "";
					store.setError(`${err.message}${suffix}`, { code: err.errorCode });
					return;
				}
				if (err.errorCode === "LOCKED_OUT" || err.errorCode === "RATE_LIMITED" || err.errorCode === "COOLDOWN_ACTIVE") {
					store.setOtpFailure({
						lockedUntil: err.lockedUntil,
						retryAfterSeconds: err.retryAfterSeconds,
					});
					if (err.retryAfterSeconds) {
						startResendTimer();
					}
					const waitNote = err.retryAfterSeconds
						? ` Please wait ${err.retryAfterSeconds} seconds before trying again.`
						: "";
					store.setError(`${err.message}${waitNote}`, { code: err.errorCode });
					return;
				}
				if (err.errorCode === "VEHICLE_CONFLICT") {
					if (err.otpVerified) {
						store.markOtpVerifiedForSlotRetry(err.challengeId);
						store.setError(err.message, { code: err.errorCode, showPortalLink: true });
						await loadSlots({ append: false });
						return;
					}
					store.setError(err.message, { code: err.errorCode, showPortalLink: true });
					return;
				}
				if (err.errorCode === "DUPLICATE_BOOKING") {
					store.setError(err.message, { code: err.errorCode, showPortalLink: true });
					return;
				}
			}
			handleApiError(err);
		} finally {
			store.setLoading("confirm", false);
		}
	}

	function handleApiError(err) {
		if (err instanceof BookingApiError) {
			if (err.errorCode === "DUPLICATE_BOOKING" || err.errorCode === "VEHICLE_CONFLICT") {
				store.setError(err.message, { code: err.errorCode, showPortalLink: true });
				return;
			}
			if (err.errorCode === "FORM_EXPIRED") {
				store.setError(`${err.message} Please refresh this page to continue.`, {
					code: err.errorCode,
				});
				return;
			}
			if (err.retryAfterSeconds) {
				store.setOtpFailure({ retryAfterSeconds: err.retryAfterSeconds });
				startResendTimer();
			}
			store.setError(err.message, { code: err.errorCode });
			return;
		}
		store.setError("An unexpected error occurred. Please try again or call the shop.");
	}

	async function handleSaveBookingPin() {
		const state = store.getState();
		const bookingRef = state.confirmation && state.confirmation.booking_reference;
		if (!bookingRef) return;
		const pinValue = (state.pin && state.pin.code ? state.pin.code : "").trim();
		if (!/^\d{4}$/.test(pinValue)) {
			store.setPinError("Please enter an exact 4-digit numeric PIN (0000–9999).");
			return;
		}
		store.setPinSubmitting(true);
		try {
			const res = await submitBookingPinToDrive({
				schedule_entry: bookingRef,
				pin: pinValue,
			});
			store.setPinUpdated(res && res.pin_to_drive, "PIN-to-Drive saved securely.");
		} catch (err) {
			store.setPinError((err && err.message) || "Unable to save PIN-to-Drive. Please try again.");
		}
	}

	async function handleOptOutBookingPin() {
		const state = store.getState();
		const bookingRef = state.confirmation && state.confirmation.booking_reference;
		if (!bookingRef) return;
		store.setPinSubmitting(true);
		try {
			const res = await optOutBookingPinToDrive({ schedule_entry: bookingRef });
			store.setPinUpdated(res && res.pin_to_drive, "Recorded: No PIN-to-Drive enabled.");
		} catch (err) {
			store.setPinError(
				(err && err.message) || "Unable to update PIN-to-Drive preference. Please try again."
			);
		}
	}

	function updateStepper(step) {
		if (!stepperEl) return;
		const items = stepperEl.querySelectorAll(".ism-stepper__item");
		for (const item of items) {
			const itemStep = Number(item.getAttribute("data-step"));
			item.classList.toggle("is-active", itemStep === step);
			item.classList.toggle("is-completed", itemStep < step);
			if (itemStep === step) {
				item.setAttribute("aria-current", "step");
			} else {
				item.removeAttribute("aria-current");
			}
		}
	}

	function renderStatusRegion(state) {
		if (!statusEl) return;
		const nodes = [];
		if (state.errorBanner) {
			const banner = el(
				"div",
				{ className: "ism-banner ism-banner--error", role: "alert" },
				el("div", { textContent: state.errorBanner.message }),
				state.errorBanner.showPortalLink
					? el("a", {
							className: "ism-banner__link",
							href: portalUrl,
							textContent: "Manage your existing appointment",
						})
					: null
			);
			nodes.push(banner);
		}
		if (state.infoBanner) {
			nodes.push(
				el(
					"div",
					{ className: "ism-banner ism-banner--info" },
					el("div", { textContent: state.infoBanner })
				)
			);
		}
		statusEl.replaceChildren(...nodes);
	}

	function renderStep1(state) {
		const models = Array.isArray(bootConfig.vehicle_models) ? bootConfig.vehicle_models : [];
		const minYear = Number(bootConfig.vehicle_year_min) || 2008;
		const maxYear = Number(bootConfig.vehicle_year_max) || new Date().getFullYear() + 1;

		const yearOptions = [el("option", { value: "", textContent: "Select model year (optional)" })];
		for (let y = maxYear; y >= minYear; y -= 1) {
			yearOptions.push(
				el("option", {
					value: String(y),
					selected: state.vehicle.year === y ? "selected" : null,
					textContent: String(y),
				})
			);
		}

		const modelButtons = models.map((modelName) => {
			const isSelected = state.vehicle.model === modelName;
			return el(
				"button",
				{
					type: "button",
					className: `ism-model-card${isSelected ? " is-selected" : ""}`,
					"aria-pressed": isSelected ? "true" : "false",
					onClick: () => store.setVehicleModel(modelName),
				},
				el("span", { textContent: modelName }),
				el("span", {
					className: "ism-model-card__check",
					"aria-hidden": "true",
					textContent: isSelected ? "✓" : "",
				})
			);
		});

		const currentColorLower = (state.vehicle.color || "").trim().toLowerCase();
		const colorChips = VEHICLE_COLOR_PRESETS.map((preset) => {
			const isSelected = currentColorLower === preset.toLowerCase();
			return el("button", {
				type: "button",
				className: `ism-color-chip${isSelected ? " is-selected" : ""}`,
				"aria-pressed": isSelected ? "true" : "false",
				onClick: () => store.setVehicleColor(preset),
				textContent: preset,
			});
		});

		const missingModel = step1Attempted && !state.vehicle.model;
		const missingColor = step1Attempted && !(state.vehicle.color && state.vehicle.color.trim());

		return el(
			"section",
			{ className: "ism-card", "aria-labelledby": "ism-step1-title" },
			el("h1", { id: "ism-step1-title", className: "ism-card__title", textContent: "Select Your Vehicle" }),
			el("p", {
				className: "ism-card__text",
				textContent:
					"Choose your model and color (and license plate if handy) so our team can identify your car easily at drop-off.",
			}),
			el("div", { className: "ism-model-grid", role: "group", "aria-label": "Vehicle model" }, modelButtons),
			missingModel
				? el("div", { className: "ism-field__error", role: "alert", textContent: "Please select a vehicle model." })
				: null,
			el(
				"div",
				{ className: "ism-field-grid" },
				el(
					"div",
					{ className: "ism-field" },
					el(
						"label",
						{ className: "ism-field__label", for: "ism-vehicle-color" },
						"Vehicle Color ",
						el("span", { className: "ism-field__required", textContent: "*" })
					),
					el("div", { className: "ism-color-chips", role: "group", "aria-label": "Common vehicle colors" }, colorChips),
					el("input", {
						id: "ism-vehicle-color",
						className: `ism-input${missingColor ? " is-invalid" : ""}`,
						type: "text",
						maxlength: "60",
						required: true,
						"aria-invalid": missingColor ? "true" : null,
						placeholder: "e.g. Pearl White, Stealth Grey, or custom wrap color",
						value: state.vehicle.color,
						onInput: (e) => store.setVehicleColor(e.target.value),
					}),
					missingColor
						? el("span", { className: "ism-field__error", role: "alert", textContent: "Vehicle color is required." })
						: null
				)
			),
			el(
				"div",
				{ className: "ism-field-grid ism-field-grid--two" },
				el(
					"div",
					{ className: "ism-field" },
					el("label", { className: "ism-field__label", for: "ism-vehicle-year", textContent: "Model Year" }),
					el(
						"select",
						{
							id: "ism-vehicle-year",
							className: "ism-select",
							value: state.vehicle.year ? String(state.vehicle.year) : "",
							onChange: (e) => store.setVehicleYear(e.target.value),
						},
						yearOptions
					)
				),
				el(
					"div",
					{ className: "ism-field" },
					el("label", {
						className: "ism-field__label",
						for: "ism-vehicle-plate",
						textContent: "License Plate (Optional)",
					}),
					el("input", {
						id: "ism-vehicle-plate",
						className: "ism-input ism-input--upper",
						type: "text",
						maxlength: "20",
						autocomplete: "off",
						placeholder: "e.g. ABCD 123",
						value: state.vehicle.license_plate,
						onInput: (e) => store.setLicensePlate(e.target.value),
					})
				)
			),
			(() => {
				const genMatches = matchingGenerationsForVehicle(state.vehicle.model, state.vehicle.year);
				if (!genMatches.length) return null;
				if (genMatches.length === 1 && state.vehicle.year) {
					return el(
						"div",
						{ className: "ism-field" },
						el("span", {
							className: "ism-price-pill",
							textContent: `Generation: ${genMatches[0].short_label || genMatches[0].generation_name}`,
						})
					);
				}
				const genOptions = [
					el("option", {
						value: "",
						textContent: state.vehicle.year
							? "Select vehicle generation (split model year — optional)"
							: "All generations (optional)",
					}),
				];
				genMatches.forEach((entry) => {
					genOptions.push(
						el("option", {
							value: entry.generation_name,
							selected: state.vehicle.generation === entry.generation_name ? "selected" : null,
							textContent: entry.short_label || entry.generation_name,
						})
					);
				});
				return el(
					"div",
					{ className: "ism-field" },
					el("label", {
						className: "ism-field__label",
						for: "ism-vehicle-generation",
						textContent: "Vehicle Generation",
					}),
					el(
						"select",
						{
							id: "ism-vehicle-generation",
							className: "ism-select",
							value: state.vehicle.generation || "",
							onChange: (e) => store.setVehicleGeneration(e.target.value),
						},
						genOptions
					)
				);
			})(),
			el(
				"div",
				{ className: "ism-field-grid" },
				el(
					"div",
					{ className: "ism-field" },
					el("label", {
						className: "ism-field__label",
						for: "ism-vehicle-vin",
						textContent: "VIN (Optional)",
					}),
					el("input", {
						id: "ism-vehicle-vin",
						className: "ism-input ism-input--upper",
						type: "text",
						maxlength: "17",
						autocomplete: "off",
						placeholder: "e.g. 5YJ3E1EA... (17 characters)",
						value: state.vehicle.vin,
						onInput: (e) => store.setVin(e.target.value),
					}),
					el("span", {
						className: "ism-field__hint",
						textContent: "Optional — helps us pre-check exact parts compatibility.",
					})
				)
			),
			el(
				"div",
				{ className: "ism-actions-row" },
				el("span", {
					className: "ism-field__hint",
					textContent: canProceedStep1(state) ? "" : "Model and color are required.",
				}),
				el("button", {
					type: "button",
					className: "ism-btn ism-btn-primary",
					disabled: state.loading.packages,
					onClick: () => loadPackagesForVehicle(),
					textContent: state.loading.packages ? "Loading Services..." : "Continue to Services",
				})
			)
		);
	}

	function renderStep2(state) {
		const query = state.packageSearch.trim().toLowerCase();
		const filtered = state.packagesCatalog.filter((pkg) => {
			if (!query) return true;
			const haystack = `${pkg.title || ""} ${pkg.summary || ""}`.toLowerCase();
			return haystack.includes(query);
		});
		const selectedCount = state.selectedPackageIds.length;
		const atCap = selectedCount >= maxPackages;

		let listContent;
		if (state.loading.packages) {
			listContent = el("div", { className: "ism-loading-state", textContent: "Loading compatible services..." });
		} else if (filtered.length === 0) {
			listContent = el(
				"div",
				{ className: "ism-empty-state" },
				el("p", {
					className: "ism-card__text",
					textContent:
						state.packagesCatalog.length === 0
							? "No online service packages matched this vehicle. Please call us and our team will be happy to schedule your visit."
							: "No services matched your search. Try a different keyword or clear the filter.",
				}),
				shop.phone
					? el("a", {
							className: "ism-btn ism-btn-primary",
							href: `tel:${String(shop.phone).replace(/\s+/g, "")}`,
							textContent: `Call ${shop.phone}`,
						})
					: null
			);
		} else {
			listContent = el(
				"div",
				{ className: "ism-package-list" },
				filtered.map((pkg) => {
					const isSelected = state.selectedPackageIds.includes(pkg.package_id);
					const isDisabled = !isSelected && atCap;
					return el(
						"button",
						{
							type: "button",
							className: `ism-package-card${isSelected ? " is-selected" : ""}`,
							"aria-pressed": isSelected ? "true" : "false",
							disabled: isDisabled,
							onClick: () => store.togglePackage(pkg.package_id),
						},
						el(
							"div",
							{ className: "ism-package-card__body" },
							el("div", { className: "ism-package-card__title", textContent: pkg.title || "" }),
							pkg.applicable_generation
								? el("div", {
										className: "ism-package-card__summary",
										textContent: `Generation: ${pkg.applicable_generation}`,
									})
								: null,
							pkg.summary
								? el("div", { className: "ism-package-card__summary", textContent: pkg.summary })
								: null
						),
						el(
							"div",
							{ className: "ism-package-card__meta" },
							pkg.price_display_label
								? el("span", { className: "ism-price-pill", textContent: pkg.price_display_label })
								: null,
							el("span", {
								className: "ism-check-indicator",
								"aria-hidden": "true",
								textContent: isSelected ? "✓" : "",
							})
						)
					);
				})
			);
		}

		const stickyFooter = el(
			"div",
			{ className: "ism-sticky-bar" },
			el(
				"div",
				{ className: "ism-sticky-bar__inner" },
				el("span", {
					className: "ism-sticky-bar__count",
					textContent:
						selectedCount === 1 ? "1 service selected" : `${selectedCount} services selected`,
				}),
				el("button", {
					type: "button",
					className: "ism-btn ism-btn-primary",
					disabled: selectedCount === 0 || state.loading.slots,
					onClick: () => loadSlots({ append: false }),
					textContent: state.loading.slots ? "Checking Times..." : "Choose Drop-Off Time",
				})
			)
		);

		return [
			el(
				"section",
				{ className: "ism-card", "aria-labelledby": "ism-step2-title" },
				el(
					"div",
					{ className: "ism-card__header" },
					el(
						"div",
						null,
						el("h1", { id: "ism-step2-title", className: "ism-card__title", textContent: "Choose Services" }),
						el("p", {
							className: "ism-card__text",
							textContent: `Select up to ${maxPackages} services for your visit.`,
						})
					),
					el("button", {
						type: "button",
						className: "ism-btn-link",
						onClick: () => store.setStep(1),
						textContent: "Change Vehicle",
					})
				),
				el(
					"div",
					{ className: "ism-summary-strip" },
					renderVehicleSummaryPill(state)
				),
				state.packagesCatalog.length > 0
					? el(
							"div",
							{ className: "ism-search-row" },
							el("input", {
								id: "ism-package-search",
								className: "ism-input ism-search-input",
								type: "search",
								placeholder: "Search services...",
								"aria-label": "Search services",
								value: state.packageSearch,
								onInput: (e) => store.setPackageSearch(e.target.value),
							}),
							el("span", {
								className: "ism-limit-note",
								textContent: `${selectedCount} / ${maxPackages} selected`,
							})
						)
					: null,
				listContent
			),
			stickyFooter,
		];
	}

	function renderStep3(state) {
		const days = state.slotsWindow.days || [];
		const activeDay = days.find((d) => d.date === state.selectedDate) || days[0] || null;
		const chosenPackages = selectedPackagesList(state);
		const isDirectConfirm = Boolean(state.otp.otp_verified && state.otp.challenge_id);

		let slotsContent;
		if (state.loading.slots) {
			slotsContent = el("div", { className: "ism-loading-state", textContent: "Finding open drop-off times..." });
		} else if (days.length === 0) {
			slotsContent = el(
				"div",
				{ className: "ism-empty-state" },
				el("p", {
					className: "ism-card__text",
					textContent: "No open drop-off times were found in this date range.",
				}),
				!state.slotsWindow.exhausted
					? el("button", {
							type: "button",
							className: "ism-btn ism-btn-secondary",
							disabled: state.loading.moreSlots,
							onClick: () => loadSlots({ append: true }),
							textContent: state.loading.moreSlots ? "Loading..." : "Load More Dates",
						})
					: shop.phone
						? el("a", {
								className: "ism-btn ism-btn-primary",
								href: `tel:${String(shop.phone).replace(/\s+/g, "")}`,
								textContent: `Call ${shop.phone}`,
							})
						: null
			);
		} else {
			const datePills = days.map((day) => {
				const label = formatDatePillLabel(day.date);
				const isActive = activeDay && day.date === activeDay.date;
				return el(
					"button",
					{
						type: "button",
						className: `ism-date-pill${isActive ? " is-active" : ""}`,
						"aria-pressed": isActive ? "true" : "false",
						onClick: () => store.selectDate(day.date),
					},
					el("span", { textContent: label.top }),
					el("span", { className: "ism-date-pill__sub", textContent: label.bottom })
				);
			});

			const timeChips = (activeDay ? activeDay.slots || [] : []).map((slot) => {
				const isSelected =
					state.selectedSlot &&
					state.selectedSlot.date === slot.date &&
					state.selectedSlot.customer_arrival_time === slot.customer_arrival_time;
				return el("button", {
					type: "button",
					className: `ism-slot-chip${isSelected ? " is-selected" : ""}`,
					"aria-pressed": isSelected ? "true" : "false",
					onClick: () => store.selectSlot(slot),
					textContent: slot.display_time || slot.customer_arrival_hhmm || "",
				});
			});

			slotsContent = [
				el("div", { className: "ism-date-strip", role: "group", "aria-label": "Available dates" }, datePills),
				el("div", { className: "ism-slot-grid", role: "group", "aria-label": "Available drop-off times" }, timeChips),
				!state.slotsWindow.exhausted
					? el(
							"div",
							{ className: "ism-load-more-wrap" },
							el("button", {
								type: "button",
								className: "ism-btn ism-btn-secondary",
								disabled: state.loading.moreSlots,
								onClick: () => loadSlots({ append: true }),
								textContent: state.loading.moreSlots ? "Loading More Dates..." : "Load More Dates",
							})
						)
					: null,
			];
		}

		return el(
			"section",
			{ className: "ism-card", "aria-labelledby": "ism-step3-title" },
			el(
				"div",
				{ className: "ism-card__header" },
				el(
					"div",
					null,
					el("h1", { id: "ism-step3-title", className: "ism-card__title", textContent: "Choose Drop-Off Time" }),
					el("p", {
						className: "ism-card__text",
						textContent: "Pick a date and drop-off time that works best for you.",
					})
				),
				el("button", {
					type: "button",
					className: "ism-btn-link",
					onClick: () => store.setStep(2),
					textContent: "Edit Services",
				})
			),
			el(
				"div",
				{ className: "ism-summary-strip" },
				renderVehicleSummaryPill(state),
				chosenPackages.map((pkg) =>
					el("span", { className: "ism-summary-pill", textContent: pkg.title })
				)
			),
			slotsContent,
			el(
				"div",
				{ className: "ism-actions-row" },
				el("button", {
					type: "button",
					className: "ism-btn ism-btn-secondary",
					onClick: () => store.setStep(2),
					textContent: "Back",
				}),
				isDirectConfirm
					? el("button", {
							type: "button",
							className: "ism-btn ism-btn-primary",
							disabled: !state.selectedSlot || state.loading.confirm,
							onClick: () => handleConfirmBooking({ skipOtpCode: true }),
							textContent: state.loading.confirm
								? "Confirming Appointment..."
								: "Confirm New Drop-Off Time",
						})
					: el("button", {
							type: "button",
							className: "ism-btn ism-btn-primary",
							disabled: !state.selectedSlot,
							onClick: () => store.setStep(4),
							textContent: "Continue to Details",
						})
			)
		);
	}

	function renderStep4(state) {
		const chosenPackages = selectedPackagesList(state);
		const slotText = state.selectedSlot
			? `${formatFullDateLabel(state.selectedSlot.date)} at ${state.selectedSlot.display_time}`
			: "";

		const waiterControl = policy.enable_waiter_preference
			? el(
					"div",
					{ className: "ism-field" },
					el("span", { className: "ism-field__label", textContent: "Visit Preference" }),
					el(
						"div",
						{ className: "ism-segmented", role: "group", "aria-label": "Visit preference" },
						["Drop-Off", "Waiter"].map((option) => {
							const selected = state.customer.waiter_preference === option;
							return el("button", {
								type: "button",
								className: `ism-segmented__btn${selected ? " is-selected" : ""}`,
								"aria-pressed": selected ? "true" : "false",
								onClick: () => store.setCustomerField("waiter_preference", option),
								textContent: option === "Waiter" ? "Waiting at Shop" : "Drop-Off",
							});
						})
					)
				)
			: null;

		const otpPanel = state.otp.sent
			? el(
					"div",
					{ className: "ism-otp-panel", "aria-labelledby": "ism-otp-heading" },
					el("h2", {
						id: "ism-otp-heading",
						className: "ism-otp-panel__title",
						textContent: "Verify Your Mobile Number",
					}),
					el("p", {
						className: "ism-otp-panel__sub",
						textContent: state.otp.phone_masked
							? `Enter the 6-digit code sent via SMS to ${state.otp.phone_masked}.`
							: "Enter the 6-digit code sent via SMS to your mobile phone.",
					}),
					el(
						"div",
						{ className: "ism-field" },
						el("label", {
							className: "ism-field__label",
							for: "ism-otp-code-input",
							textContent: "6-Digit Verification Code",
						}),
						el("input", {
							id: "ism-otp-code-input",
							className: "ism-input ism-otp-input",
							type: "text",
							inputmode: "numeric",
							autocomplete: "one-time-code",
							maxlength: "6",
							placeholder: "000000",
							value: state.otp.code,
							onInput: (e) => store.setOtpCode(e.target.value),
						})
					),
					el(
						"div",
						{ className: "ism-otp-actions" },
						el("button", {
							type: "button",
							className: "ism-btn ism-btn-primary",
							disabled: state.loading.confirm || state.otp.code.length < 4,
							onClick: () => handleConfirmBooking(),
							textContent: state.loading.confirm
								? "Verifying & Confirming..."
								: "Verify & Confirm Booking",
						}),
						el("button", {
							type: "button",
							className: "ism-btn-link",
							disabled: state.loading.otp || state.otp.resend_seconds_left > 0,
							onClick: () => handleSendOtp(),
							textContent:
								state.otp.resend_seconds_left > 0
									? `Resend code in ${state.otp.resend_seconds_left}s`
									: state.loading.otp
										? "Sending..."
										: "Resend Code",
						})
					)
				)
			: null;

		return el(
			"section",
			{ className: "ism-card", "aria-labelledby": "ism-step4-title" },
			el(
				"div",
				{ className: "ism-card__header" },
				el(
					"div",
					null,
					el("h1", { id: "ism-step4-title", className: "ism-card__title", textContent: "Your Details & Verification" }),
					el("p", {
						className: "ism-card__text",
						textContent: "We will text a 6-digit code to confirm your appointment.",
					})
				),
				el("button", {
					type: "button",
					className: "ism-btn-link",
					onClick: () => store.setStep(3),
					textContent: "Change Time",
				})
			),
			el(
				"div",
				{ className: "ism-summary-strip" },
				renderVehicleSummaryPill(state),
				slotText ? el("span", { className: "ism-summary-pill", textContent: slotText }) : null,
				chosenPackages.map((pkg) =>
					el("span", { className: "ism-summary-pill", textContent: pkg.title })
				)
			),
			el(
				"form",
				{
					className: "ism-field-grid",
					onSubmit: (e) => {
						e.preventDefault();
						if (state.otp.sent) {
							handleConfirmBooking();
						} else {
							handleSendOtp();
						}
					},
				},
				el(
					"div",
					{ className: "ism-field" },
					el(
						"label",
						{ className: "ism-field__label", for: "ism-customer-name" },
						"Full Name",
						el("span", { className: "ism-field__required", "aria-hidden": "true", textContent: "*" })
					),
					el("input", {
						id: "ism-customer-name",
						className: "ism-input",
						type: "text",
						required: true,
						autocomplete: "name",
						placeholder: "Jane Doe",
						value: state.customer.customer_name,
						onInput: (e) => store.setCustomerField("customer_name", e.target.value),
					})
				),
				el(
					"div",
					{ className: "ism-field-grid ism-field-grid--two" },
					el(
						"div",
						{ className: "ism-field" },
						el(
							"label",
							{ className: "ism-field__label", for: "ism-customer-phone" },
							"Mobile Phone",
							el("span", { className: "ism-field__required", "aria-hidden": "true", textContent: "*" })
						),
						el("input", {
							id: "ism-customer-phone",
							className: "ism-input",
							type: "tel",
							required: true,
							autocomplete: "tel",
							placeholder: "(555) 234-5678",
							value: state.customer.phone,
							onInput: (e) => store.setCustomerField("phone", e.target.value),
						}),
						el("span", {
							className: "ism-field__hint",
							textContent: "SMS verification code will be sent to this number.",
						})
					),
					el(
						"div",
						{ className: "ism-field" },
						el(
							"label",
							{ className: "ism-field__label", for: "ism-customer-email" },
							policy.require_email ? "Email Address" : "Email Address (Optional)",
							policy.require_email
								? el("span", { className: "ism-field__required", "aria-hidden": "true", textContent: "*" })
								: null
						),
						el("input", {
							id: "ism-customer-email",
							className: "ism-input",
							type: "email",
							required: Boolean(policy.require_email),
							autocomplete: "email",
							placeholder: "you@example.com",
							value: state.customer.email,
							onInput: (e) => store.setCustomerField("email", e.target.value),
						})
					)
				),
				waiterControl,
				el(
					"div",
					{ className: "ism-field" },
					el("label", {
						className: "ism-field__label",
						for: "ism-customer-notes",
						textContent: "Visit Notes (Optional)",
					}),
					el("textarea", {
						id: "ism-customer-notes",
						className: "ism-textarea",
						maxlength: "500",
						placeholder: "Share any helpful details about your visit...",
						value: state.customer.notes,
						onInput: (e) => store.setCustomerField("notes", e.target.value),
					})
				),
				!state.otp.sent
					? el(
							"div",
							{ className: "ism-actions-row" },
							el("button", {
								type: "button",
								className: "ism-btn ism-btn-secondary",
								onClick: () => store.setStep(3),
								textContent: "Back",
							}),
							el("button", {
								type: "submit",
								className: "ism-btn ism-btn-primary",
								disabled: state.loading.otp,
								textContent: state.loading.otp
									? "Sending Code..."
									: "Send Verification Code",
							})
						)
					: null
			),
			otpPanel
		);
	}

	function renderStep5PinCard(state, conf) {
		const sessionReady = Boolean(conf.portal_handoff && conf.portal_handoff.session_established);
		if (!conf.booking_reference || !sessionReady || !conf.pin_to_drive) {
			return null;
		}

		const pin = state.pin || {};
		const status = pin.status || "Not Prompted";
		const disclaimer =
			pin.disclaimer ||
			"Your 4-digit PIN-to-Drive is encrypted at rest, accessible only to authorized shop technicians while your vehicle is in service, and permanently purged as soon as your vehicle is released.";
		const showForm = status === "Not Prompted" || Boolean(pin.editing);

		let bodyNode;
		if (showForm) {
			bodyNode = el(
				"form",
				{
					className: "ism-pin-form",
					onSubmit: (e) => {
						e.preventDefault();
						handleSaveBookingPin();
					},
				},
				el(
					"div",
					{ className: "ism-field" },
					el("label", {
						className: "ism-field__label",
						for: "ism-booking-pin-input",
						textContent: "4-Digit Numeric PIN-to-Drive",
					}),
					el("input", {
						id: "ism-booking-pin-input",
						className: "ism-input ism-pin-input",
						type: "password",
						inputmode: "numeric",
						pattern: "\\d{4}",
						maxlength: "4",
						autocomplete: "off",
						placeholder: "••••",
						value: pin.code || "",
						disabled: Boolean(pin.submitting),
						onInput: (e) => store.setPinCode(e.target.value),
					})
				),
				el(
					"div",
					{ className: "ism-pin-actions" },
					el("button", {
						type: "submit",
						id: "ism-booking-pin-save-btn",
						className: "ism-btn ism-btn-primary",
						disabled: Boolean(pin.submitting) || (pin.code || "").length !== 4,
						textContent: pin.submitting ? "Encrypting & Saving..." : "Save 4-Digit PIN",
					}),
					el("button", {
						type: "button",
						id: "ism-booking-pin-optout-btn",
						className: "ism-btn ism-btn-secondary",
						disabled: Boolean(pin.submitting),
						onClick: () => handleOptOutBookingPin(),
						textContent: "No PIN-to-Drive enabled",
					}),
					pin.editing
						? el("button", {
								type: "button",
								className: "ism-btn-link",
								disabled: Boolean(pin.submitting),
								onClick: () => store.setPinEditing(false),
								textContent: "Cancel",
							})
						: null
				)
			);
		} else if (status === "Provided") {
			bodyNode = el(
				"div",
				{ className: "ism-pin-resolved" },
				el(
					"div",
					null,
					el("div", {
						className: "ism-pin-masked",
						"aria-label": "PIN-to-Drive stored",
						textContent: pin.masked_display || "••••",
					}),
					el("p", {
						className: "ism-pin-note",
						textContent:
							"Stored encrypted for shop technicians only. Automatically erased when your vehicle is released.",
					})
				),
				el("button", {
					type: "button",
					className: "ism-btn ism-btn-secondary",
					onClick: () => {
						pendingFocusId = "ism-booking-pin-input";
						store.setPinEditing(true);
					},
					textContent: "Update PIN",
				})
			);
		} else if (status === "Opted Out") {
			bodyNode = el(
				"div",
				{ className: "ism-pin-resolved" },
				el("p", {
					className: "ism-pin-note",
					textContent: "You indicated that No PIN-to-Drive is enabled on this vehicle.",
				}),
				el("button", {
					type: "button",
					className: "ism-btn ism-btn-secondary",
					onClick: () => {
						pendingFocusId = "ism-booking-pin-input";
						store.setPinEditing(true);
					},
					textContent: "Add 4-Digit PIN",
				})
			);
		} else {
			bodyNode = null;
		}

		return el(
			"section",
			{ id: "ism-booking-pin-card", className: "ism-pin-card", "aria-labelledby": "ism-booking-pin-heading" },
			el(
				"div",
				{ className: "ism-pin-card__head" },
				el("h2", {
					id: "ism-booking-pin-heading",
					className: "ism-pin-card__title",
					textContent:
						status === "Not Prompted"
							? "Does Your Vehicle Use PIN-to-Drive?"
							: "Vehicle PIN-to-Drive",
				}),
				el("span", {
					className: `ism-status-badge${status === "Provided" ? " ism-status-badge--ok" : ""}`,
					textContent: status === "Not Prompted" ? "Action Requested" : status,
				})
			),
			el("p", { className: "ism-pin-card__disclaimer", textContent: disclaimer }),
			pin.error
				? el("div", { className: "ism-banner ism-banner--error", role: "alert", textContent: pin.error })
				: null,
			pin.feedback
				? el("div", { className: "ism-banner ism-banner--info", role: "status", textContent: pin.feedback })
				: null,
			bodyNode
		);
	}

	function renderStep5(state) {
		const conf = state.confirmation || {};
		const appt = conf.appointment || {};
		const pkgs = Array.isArray(conf.packages) ? conf.packages : [];
		const notes = conf.notifications || {};
		const handoffUrl = (conf.portal_handoff && conf.portal_handoff.portal_url) || portalUrl;

		const badges = [
			el("span", {
				className: `ism-status-badge${notes.sms_sent ? " ism-status-badge--ok" : ""}`,
				textContent: notes.sms_sent ? "SMS Confirmation Sent" : "SMS Confirmation Pending",
			}),
		];
		if (notes.email_requested) {
			badges.push(
				el("span", {
					className: `ism-status-badge${notes.email_sent ? " ism-status-badge--ok" : ""}`,
					textContent: notes.email_sent ? "Email Confirmation Sent" : "Email Confirmation Pending",
				})
			);
		}

		return el(
			"section",
			{ className: "ism-card", "aria-labelledby": "ism-step5-title" },
			el("div", { className: "ism-confirm-badge", textContent: "✓ Appointment Confirmed" }),
			el("h1", { id: "ism-step5-title", className: "ism-card__title", textContent: "You Are All Set" }),
			el("p", {
				className: "ism-card__text",
				textContent: "Your appointment has been reserved. Keep your reference number handy for check-in.",
			}),
			el(
				"div",
				{ className: "ism-confirm-datetime" },
				el("div", { className: "ism-confirm-datetime__label", textContent: "Confirmed Drop-Off Time" }),
				el("div", {
					className: "ism-confirm-datetime__value",
					textContent: appt.display_datetime || "",
				})
			),
			el(
				"div",
				{ className: "ism-receipt-grid" },
				el(
					"div",
					{ className: "ism-receipt-row" },
					el("span", { className: "ism-receipt-row__label", textContent: "Booking Reference" }),
					el("span", { className: "ism-receipt-row__value", textContent: conf.booking_reference || "" })
				),
				conf.vehicle_info || conf.vehicle_display || state.vehicle.model
					? el(
							"div",
							{ className: "ism-receipt-row" },
							el("span", { className: "ism-receipt-row__label", textContent: "Vehicle" }),
							el(
								"span",
								{ className: "ism-receipt-row__value" },
								renderVehicleDisplayNode(conf.vehicle_info || state.vehicle || conf.vehicle_display, {
									showVin: false,
								})
							)
						)
					: null,
				conf.waiter_preference
					? el(
							"div",
							{ className: "ism-receipt-row" },
							el("span", { className: "ism-receipt-row__label", textContent: "Visit Preference" }),
							el("span", { className: "ism-receipt-row__value", textContent: conf.waiter_preference })
						)
					: null
			),
			pkgs.length > 0
				? el(
						"ul",
						{ className: "ism-receipt-services", "aria-label": "Selected services" },
						pkgs.map((pkg) =>
							el(
								"li",
								{ className: "ism-receipt-services__item" },
								el("span", { textContent: pkg.title || "" }),
								pkg.price_display_label
									? el("span", { textContent: pkg.price_display_label })
									: null
							)
						)
					)
				: null,
			el("div", { className: "ism-status-badges" }, badges),
			renderStep5PinCard(state, conf),
			el(
				"div",
				{ className: "ism-actions-row" },
				el("a", {
					href: handoffUrl,
					className: "ism-btn ism-btn-primary",
					textContent: "Manage Your Appointment",
				}),
				el("button", {
					type: "button",
					className: "ism-btn ism-btn-secondary",
					onClick: () => {
						stopResendTimer();
						step1Attempted = false;
						store.resetForNewBooking();
					},
					textContent: "Book Another Appointment",
				})
			)
		);
	}

	function render(state) {
		const activeEl = document.activeElement;
		const activeId = activeEl && activeEl.id ? activeEl.id : null;
		const isTextInput =
			activeEl && (activeEl.tagName === "INPUT" || activeEl.tagName === "TEXTAREA");
		const selStart = isTextInput && typeof activeEl.selectionStart === "number" ? activeEl.selectionStart : null;
		const selEnd = isTextInput && typeof activeEl.selectionEnd === "number" ? activeEl.selectionEnd : null;

		updateStepper(state.step);
		renderStatusRegion(state);

		let stepContent;
		if (state.step === 1) {
			stepContent = renderStep1(state);
		} else if (state.step === 2) {
			stepContent = renderStep2(state);
		} else if (state.step === 3) {
			stepContent = renderStep3(state);
		} else if (state.step === 4) {
			stepContent = renderStep4(state);
		} else {
			stepContent = renderStep5(state);
		}

		if (Array.isArray(stepContent)) {
			mountEl.replaceChildren(...stepContent.filter(Boolean));
		} else if (stepContent) {
			mountEl.replaceChildren(stepContent);
		}

		const targetFocusId = pendingFocusId || activeId;
		pendingFocusId = null;
		if (targetFocusId) {
			const nextFocus = document.getElementById(targetFocusId);
			if (nextFocus && typeof nextFocus.focus === "function") {
				nextFocus.focus({ preventScroll: true });
				if (
					selStart !== null &&
					selEnd !== null &&
					targetFocusId === activeId &&
					typeof nextFocus.setSelectionRange === "function"
				) {
					try {
						nextFocus.setSelectionRange(selStart, selEnd);
					} catch {
						// Some input types (e.g. email) do not support setSelectionRange.
					}
				}
			}
		}
	}

	store.subscribe(render);
	render(store.getState());
}

if (document.readyState === "loading") {
	document.addEventListener("DOMContentLoaded", initBookingApp);
} else {
	initBookingApp();
}
