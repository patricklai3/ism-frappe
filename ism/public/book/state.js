/**
 * Pure 5-step booking funnel state machine (vanilla ES module).
 * Zero DOM side-effects and strictly in-memory state (no browser storage persistence).
 */

function generateIdempotencyKey() {
	if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
		return crypto.randomUUID();
	}
	if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
		const bytes = new Uint8Array(16);
		crypto.getRandomValues(bytes);
		bytes[6] = (bytes[6] & 0x0f) | 0x40;
		bytes[8] = (bytes[8] & 0x3f) | 0x80;
		const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
		return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
	}
	return `bk-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

function addDaysIso(isoDate, deltaDays) {
	if (!isoDate || typeof isoDate !== "string") return null;
	const parts = isoDate.split("-").map(Number);
	if (parts.length !== 3 || parts.some(Number.isNaN)) return null;
	const dt = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2] + deltaDays));
	const y = dt.getUTCFullYear();
	const m = String(dt.getUTCMonth() + 1).padStart(2, "0");
	const d = String(dt.getUTCDate()).padStart(2, "0");
	return `${y}-${m}-${d}`;
}

function todayIsoLocal() {
	const now = new Date();
	const y = now.getFullYear();
	const m = String(now.getMonth() + 1).padStart(2, "0");
	const d = String(now.getDate()).padStart(2, "0");
	return `${y}-${m}-${d}`;
}

function emptySlotsWindow(horizonDays) {
	return {
		days: [],
		startDate: null,
		endDate: null,
		nextStartDate: null,
		horizonDays: Number(horizonDays) || 30,
		exhausted: false,
	};
}

function emptyOtpState() {
	return {
		challenge_id: null,
		phone_masked: "",
		code: "",
		resend_seconds_left: 0,
		expires_in_seconds: 0,
		otp_verified: false,
		attempts_remaining: null,
		locked_until: null,
		sent: false,
	};
}

function emptyPinState(initial = null) {
	const status = (initial && initial.status) || "Not Prompted";
	return {
		status,
		masked_display: (initial && initial.masked_display) || null,
		disclaimer: (initial && initial.disclaimer) || "",
		code: "",
		editing: false,
		modalOpen: Boolean(initial && status === "Not Prompted"),
		submitting: false,
		error: null,
		feedback: null,
	};
}

export function createBookingStore(bootConfig) {
	const policy = (bootConfig && bootConfig.policy) || {};
	const maxPackages = Math.max(1, Number(policy.max_packages_per_booking) || 5);
	const defaultHorizon = Math.max(1, Number(policy.horizon_days) || 30);

	function resolveMatchingGenerations(model, year) {
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

	function buildInitialState() {
		return {
			step: 1,
			vehicle: {
				model: "",
				year: null,
				color: "",
				license_plate: "",
				vin: "",
				generation: "",
				vin_or_plate: "",
			},
			packagesCatalog: [],
			packagesLoaded: false,
			packageSearch: "",
			selectedPackageIds: [],
			slotsWindow: emptySlotsWindow(defaultHorizon),
			selectedDate: null,
			selectedSlot: null,
			customer: {
				customer_name: "",
				phone: "",
				email: "",
				notes: "",
				waiter_preference: "Drop-Off",
			},
			otp: emptyOtpState(),
			idempotency_key: generateIdempotencyKey(),
			loading: {
				packages: false,
				slots: false,
				moreSlots: false,
				otp: false,
				confirm: false,
			},
			errorBanner: null,
			infoBanner: null,
			confirmation: null,
			pin: emptyPinState(),
		};
	}

	let state = buildInitialState();
	const listeners = new Set();

	function notify() {
		for (const listener of listeners) {
			listener(state);
		}
	}

	function setState(updater) {
		state = typeof updater === "function" ? updater(state) : { ...state, ...updater };
		notify();
	}

	return {
		getState() {
			return state;
		},

		subscribe(listener) {
			listeners.add(listener);
			return () => listeners.delete(listener);
		},

		setStep(step) {
			const clamped = Math.min(5, Math.max(1, Number(step) || 1));
			setState((prev) => ({
				...prev,
				step: clamped,
				errorBanner: null,
			}));
		},

		setLoading(key, value) {
			setState((prev) => ({
				...prev,
				loading: { ...prev.loading, [key]: Boolean(value) },
			}));
		},

		setError(message, { code = null, showPortalLink = false } = {}) {
			setState((prev) => ({
				...prev,
				errorBanner: message ? { code, message, showPortalLink: Boolean(showPortalLink) } : null,
			}));
		},

		clearError() {
			if (!state.errorBanner) return;
			setState((prev) => ({ ...prev, errorBanner: null }));
		},

		setInfo(message) {
			setState((prev) => ({ ...prev, infoBanner: message || null }));
		},

		clearInfo() {
			if (!state.infoBanner) return;
			setState((prev) => ({ ...prev, infoBanner: null }));
		},

		setVehicleModel(model) {
			const clean = String(model || "").trim();
			if (clean === state.vehicle.model) return;
			const matches = resolveMatchingGenerations(clean, state.vehicle.year);
			const autoGen = matches.length === 1 ? String(matches[0].generation_name || "") : "";
			setState((prev) => ({
				...prev,
				vehicle: { ...prev.vehicle, model: clean, generation: autoGen },
				packagesCatalog: [],
				packagesLoaded: false,
				selectedPackageIds: [],
				slotsWindow: emptySlotsWindow(defaultHorizon),
				selectedDate: null,
				selectedSlot: null,
				idempotency_key: generateIdempotencyKey(),
				errorBanner: null,
			}));
		},

		setVehicleYear(year) {
			const parsed = year ? Number(year) : null;
			const clean = Number.isInteger(parsed) ? parsed : null;
			if (clean === state.vehicle.year) return;
			const matches = resolveMatchingGenerations(state.vehicle.model, clean);
			const autoGen = matches.length === 1 ? String(matches[0].generation_name || "") : "";
			setState((prev) => ({
				...prev,
				vehicle: { ...prev.vehicle, year: clean, generation: autoGen },
				packagesCatalog: [],
				packagesLoaded: false,
				selectedPackageIds: [],
				slotsWindow: emptySlotsWindow(defaultHorizon),
				selectedDate: null,
				selectedSlot: null,
				idempotency_key: generateIdempotencyKey(),
				errorBanner: null,
			}));
		},

		setVehicleGeneration(generation) {
			const clean = String(generation || "").trim();
			if (clean === state.vehicle.generation) return;
			setState((prev) => ({
				...prev,
				vehicle: { ...prev.vehicle, generation: clean },
				packagesCatalog: [],
				packagesLoaded: false,
				selectedPackageIds: [],
				slotsWindow: emptySlotsWindow(defaultHorizon),
				selectedDate: null,
				selectedSlot: null,
				idempotency_key: generateIdempotencyKey(),
				errorBanner: null,
			}));
		},

		setVehicleColor(val) {
			setState((prev) => ({
				...prev,
				vehicle: { ...prev.vehicle, color: String(val || "") },
				errorBanner: null,
			}));
		},

		setLicensePlate(val) {
			const plate = String(val || "").toUpperCase();
			setState((prev) => ({
				...prev,
				vehicle: {
					...prev.vehicle,
					license_plate: plate,
					vin_or_plate: prev.vehicle.vin || plate,
				},
				errorBanner: null,
			}));
		},

		setVin(val) {
			const vin = String(val || "").toUpperCase();
			setState((prev) => ({
				...prev,
				vehicle: {
					...prev.vehicle,
					vin: vin,
					vin_or_plate: vin || prev.vehicle.license_plate,
				},
			}));
		},

		setVinOrPlate(val) {
			setState((prev) => ({
				...prev,
				vehicle: { ...prev.vehicle, vin_or_plate: String(val || "") },
			}));
		},

		setPackagesCatalog(packages) {
			const list = Array.isArray(packages) ? packages : [];
			const validIds = new Set(list.map((p) => p.package_id));
			setState((prev) => ({
				...prev,
				packagesCatalog: list,
				packagesLoaded: true,
				selectedPackageIds: prev.selectedPackageIds.filter((id) => validIds.has(id)),
			}));
		},

		setPackageSearch(query) {
			setState((prev) => ({
				...prev,
				packageSearch: String(query || ""),
			}));
		},

		togglePackage(packageId) {
			const id = String(packageId || "");
			if (!id) return false;
			const exists = state.selectedPackageIds.includes(id);
			if (!exists && state.selectedPackageIds.length >= maxPackages) {
				return false;
			}
			const nextSelected = exists
				? state.selectedPackageIds.filter((item) => item !== id)
				: [...state.selectedPackageIds, id];
			setState((prev) => ({
				...prev,
				selectedPackageIds: nextSelected,
				slotsWindow: emptySlotsWindow(defaultHorizon),
				selectedDate: null,
				selectedSlot: null,
				idempotency_key: generateIdempotencyKey(),
				errorBanner: null,
			}));
			return true;
		},

		setSlotsResult(result, { append = false } = {}) {
			const incomingDays = Array.isArray(result && result.days) ? result.days : [];
			const horizon = Number((result && result.horizon_days) || defaultHorizon);
			const endDate = (result && result.end_date) || null;
			const startDate = (result && result.start_date) || null;
			const nextStartDate = endDate ? addDaysIso(endDate, 1) : null;
			const maxHorizonDate = addDaysIso(todayIsoLocal(), horizon);
			const exhausted =
				!nextStartDate || (Boolean(maxHorizonDate) && nextStartDate > maxHorizonDate);

			setState((prev) => {
				const byDate = new Map();
				if (append) {
					for (const day of prev.slotsWindow.days) {
						byDate.set(day.date, day);
					}
				}
				for (const day of incomingDays) {
					if (day && day.date && Array.isArray(day.slots) && day.slots.length > 0) {
						byDate.set(day.date, day);
					}
				}
				const mergedDays = Array.from(byDate.values()).sort((a, b) =>
					a.date < b.date ? -1 : a.date > b.date ? 1 : 0
				);
				const activeDate =
					prev.selectedDate && mergedDays.some((d) => d.date === prev.selectedDate)
						? prev.selectedDate
						: mergedDays.length > 0
							? mergedDays[0].date
							: null;

				return {
					...prev,
					slotsWindow: {
						days: mergedDays,
						startDate: append ? prev.slotsWindow.startDate || startDate : startDate,
						endDate: endDate || prev.slotsWindow.endDate,
						nextStartDate,
						horizonDays: horizon,
						exhausted,
					},
					selectedDate: activeDate,
					selectedSlot: append ? prev.selectedSlot : null,
				};
			});
		},

		selectDate(isoDate) {
			setState((prev) => ({
				...prev,
				selectedDate: isoDate || null,
			}));
		},

		selectSlot(slot) {
			setState((prev) => ({
				...prev,
				selectedSlot: slot
					? {
							date: slot.date,
							customer_arrival_time: slot.customer_arrival_time,
							display_time: slot.display_time,
						}
					: null,
				idempotency_key: generateIdempotencyKey(),
				errorBanner: null,
			}));
		},

		setCustomerField(field, value) {
			const strVal = String(value ?? "");
			setState((prev) => {
				const nextCustomer = { ...prev.customer, [field]: strVal };
				const phoneChanged = field === "phone" && strVal !== prev.customer.phone;
				return {
					...prev,
					customer: nextCustomer,
					otp: phoneChanged ? emptyOtpState() : prev.otp,
					errorBanner: phoneChanged ? null : prev.errorBanner,
				};
			});
		},

		setOtpCode(code) {
			const digitsOnly = String(code || "").replace(/\D/g, "").slice(0, 6);
			setState((prev) => ({
				...prev,
				otp: { ...prev.otp, code: digitsOnly },
			}));
		},

		setOtpRequested(res) {
			setState((prev) => ({
				...prev,
				otp: {
					...prev.otp,
					challenge_id: res.challenge_id || null,
					phone_masked: res.phone_masked || "",
					expires_in_seconds: Number(res.expires_in_seconds) || 0,
					resend_seconds_left: Math.max(0, Number(res.resend_available_in_seconds) || 0),
					otp_verified: false,
					attempts_remaining: null,
					locked_until: null,
					code: "",
					sent: true,
				},
				errorBanner: null,
			}));
		},

		tickResendCountdown() {
			if (state.otp.resend_seconds_left <= 0) return;
			setState((prev) => ({
				...prev,
				otp: {
					...prev.otp,
					resend_seconds_left: Math.max(0, prev.otp.resend_seconds_left - 1),
				},
			}));
		},

		setOtpFailure({ attemptsRemaining = null, lockedUntil = null, retryAfterSeconds = null } = {}) {
			setState((prev) => ({
				...prev,
				otp: {
					...prev.otp,
					attempts_remaining:
						typeof attemptsRemaining === "number"
							? attemptsRemaining
							: prev.otp.attempts_remaining,
					locked_until: lockedUntil || prev.otp.locked_until,
					resend_seconds_left:
						typeof retryAfterSeconds === "number" && retryAfterSeconds > 0
							? retryAfterSeconds
							: prev.otp.resend_seconds_left,
				},
			}));
		},

		markOtpVerifiedForSlotRetry(challengeId) {
			setState((prev) => ({
				...prev,
				step: 3,
				selectedSlot: null,
				idempotency_key: generateIdempotencyKey(),
				otp: {
					...prev.otp,
					challenge_id: challengeId || prev.otp.challenge_id,
					otp_verified: true,
					code: "",
					sent: true,
				},
				errorBanner: null,
				infoBanner:
					"Your phone is already verified — choose a new drop-off time to confirm immediately.",
			}));
		},

		setConfirmation(payload) {
			setState((prev) => ({
				...prev,
				step: 5,
				confirmation: payload,
				pin: emptyPinState(payload && payload.pin_to_drive),
				errorBanner: null,
				infoBanner: null,
			}));
		},

		setPinCode(code) {
			const digitsOnly = String(code || "").replace(/\D/g, "").slice(0, 4);
			setState((prev) => ({
				...prev,
				pin: { ...prev.pin, code: digitsOnly, error: null },
			}));
		},

		setPinSubmitting(val) {
			setState((prev) => ({
				...prev,
				pin: { ...prev.pin, submitting: Boolean(val) },
			}));
		},

		setPinError(message) {
			setState((prev) => ({
				...prev,
				pin: { ...prev.pin, submitting: false, error: message || null },
			}));
		},

		setPinEditing(editing) {
			setState((prev) => ({
				...prev,
				pin: { ...prev.pin, editing: Boolean(editing), modalOpen: Boolean(editing), code: "", error: null },
			}));
		},

		setPinModalOpen(open) {
			setState((prev) => ({
				...prev,
				pin: { ...prev.pin, modalOpen: Boolean(open), editing: Boolean(open), code: "", error: null },
			}));
		},

		setPinUpdated(pinState, feedback) {
			setState((prev) => ({
				...prev,
				pin: {
					...prev.pin,
					status: (pinState && pinState.status) || prev.pin.status,
					masked_display:
						pinState && "masked_display" in pinState ? pinState.masked_display : prev.pin.masked_display,
					disclaimer: (pinState && pinState.disclaimer) || prev.pin.disclaimer,
					code: "",
					editing: false,
					modalOpen: false,
					submitting: false,
					error: null,
					feedback: feedback || null,
				},
			}));
		},

		resetForNewBooking() {
			state = buildInitialState();
			notify();
		},
	};
}
