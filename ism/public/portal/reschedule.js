/**
 * Appointment Card & Self-Serve Reschedule Drawer.
 * Strictly displays Project 3 silent earlier drop-off times (zero buffer wording).
 */

import { escapeHtml, getRescheduleOptions, rescheduleAppointment } from "./api.js";

function groupSlotsByDate(options) {
	if (options?.slots_by_date && typeof options.slots_by_date === "object") {
		const entries = Object.entries(options.slots_by_date)
			.map(([dateKey, list]) => ({
				date: dateKey,
				display_date: (list && list[0] && list[0].display_date) || dateKey,
				slots: Array.isArray(list) ? list.filter((s) => s.available !== false) : [],
			}))
			.filter((g) => g.slots.length > 0);
		if (entries.length > 0) {
			return entries;
		}
	}

	const byDate = new Map();
	for (const slot of Array.isArray(options?.slots) ? options.slots : []) {
		if (slot.available === false) {
			continue;
		}
		if (!byDate.has(slot.date)) {
			byDate.set(slot.date, {
				date: slot.date,
				display_date: slot.display_date || slot.date,
				slots: [],
			});
		}
		byDate.get(slot.date).slots.push(slot);
	}
	return Array.from(byDate.values());
}

export function renderAppointmentSection(container, { visit, onHubUpdated, notify }) {
	const appointment = visit?.appointment;
	if (!appointment || !appointment.schedule_entry) {
		container.innerHTML = "";
		return;
	}

	const display = appointment.display || {};
	const eligibility = appointment.eligibility || {};
	const canReschedule = Boolean(eligibility.allowed && !visit.is_read_only);

	container.innerHTML = `
		<section class="portal-card" id="portal-appointment-section">
			<div class="portal-card__header">
				<div>
					<h2 class="portal-card__title">Drop-Off Appointment</h2>
					<p class="portal-card__subtitle">Reference #${escapeHtml(appointment.schedule_entry)}</p>
				</div>
				${
					canReschedule
						? `<button type="button" class="portal-btn portal-btn--outline portal-btn--sm" id="portal-reschedule-open-btn">Reschedule</button>`
						: ""
				}
			</div>
			<div class="portal-grid-2">
				<div class="portal-kv">
					<span class="portal-kv__label">Drop-Off Date</span>
					<span class="portal-kv__value">${escapeHtml(display.display_date || display.appointment_date || "Scheduled")}</span>
				</div>
				<div class="portal-kv">
					<span class="portal-kv__label">Drop-Off Time</span>
					<span class="portal-kv__value">${escapeHtml(display.display_time || display.customer_arrival_hhmm || "")}</span>
				</div>
			</div>
			${
				!canReschedule && eligibility.reason_message
					? `<p class="portal-help" style="margin:12px 0 0;">${escapeHtml(eligibility.reason_message)}</p>`
					: ""
			}
			${
				canReschedule && typeof eligibility.remaining_reschedules === "number"
					? `<p class="portal-help" style="margin:10px 0 0;">Self-serve reschedules remaining: ${eligibility.remaining_reschedules}</p>`
					: ""
			}
		</section>
	`;

	const btn = container.querySelector("#portal-reschedule-open-btn");
	if (btn) {
		btn.addEventListener("click", () => {
			openRescheduleDrawer({
				scheduleEntry: appointment.schedule_entry,
				currentDisplay: display,
				onHubUpdated,
				notify,
			});
		});
	}
}

export async function openRescheduleDrawer({ scheduleEntry, currentDisplay, onHubUpdated, notify }) {
	const existing = document.querySelector("#portal-reschedule-backdrop");
	if (existing) {
		existing.remove();
	}

	const backdrop = document.createElement("div");
	backdrop.id = "portal-reschedule-backdrop";
	backdrop.className = "portal-modal-backdrop";
	backdrop.setAttribute("role", "dialog");
	backdrop.setAttribute("aria-modal", "true");
	backdrop.innerHTML = `
		<div class="portal-modal">
			<div class="portal-modal__head">
				<h2 class="portal-modal__title">Reschedule Drop-Off</h2>
				<button type="button" class="portal-btn portal-btn--outline portal-btn--sm" id="portal-reschedule-close-btn">Close</button>
			</div>
			<p class="portal-help" style="margin-top:0;">
				Current drop-off: <strong>${escapeHtml(currentDisplay?.display_datetime || `${currentDisplay?.display_date || ""} ${currentDisplay?.display_time || ""}`)}</strong>
			</p>
			<div id="portal-reschedule-body">
				<p class="portal-help">Loading available drop-off times...</p>
			</div>
		</div>
	`;
	document.body.appendChild(backdrop);

	const close = () => backdrop.remove();
	backdrop.querySelector("#portal-reschedule-close-btn").addEventListener("click", close);
	backdrop.addEventListener("click", (e) => {
		if (e.target === backdrop) {
			close();
		}
	});

	const body = backdrop.querySelector("#portal-reschedule-body");

	try {
		const options = await getRescheduleOptions({ schedule_entry: scheduleEntry, days_ahead: 14 });
		if (options?.eligibility && !options.eligibility.allowed) {
			body.innerHTML = `<div class="portal-alert portal-alert--warning">${escapeHtml(options.eligibility.reason_message || "Online rescheduling is currently unavailable for this appointment.")}</div>`;
			return;
		}

		const dateGroups = groupSlotsByDate(options);
		if (!dateGroups.length) {
			body.innerHTML = `<div class="portal-alert portal-alert--info">No open drop-off times are available in the upcoming window. Please call the shop for assistance.</div>`;
			return;
		}

		let selectedDateIdx = 0;
		let selectedSlot = null;

		const renderPicker = (errorMsg = "") => {
			const activeGroup = dateGroups[selectedDateIdx] || dateGroups[0];
			body.innerHTML = `
				${errorMsg ? `<div class="portal-alert portal-alert--danger" role="alert">${escapeHtml(errorMsg)}</div>` : ""}
				<div class="portal-field">
					<label>1. Choose a Date</label>
					<div class="portal-date-tabs">
						${dateGroups
							.map(
								(group, idx) => `
							<button type="button" class="portal-chip ${idx === selectedDateIdx ? "is-selected" : ""}" data-date-idx="${idx}">
								${escapeHtml(group.display_date)}
							</button>
						`
							)
							.join("")}
					</div>
				</div>
				<div class="portal-field">
					<label>2. Choose a Drop-Off Time</label>
					<div class="portal-slot-grid">
						${activeGroup.slots
							.map((slot, sIdx) => {
								const isSelected =
									selectedSlot &&
									selectedSlot.date === slot.date &&
									selectedSlot.customer_arrival_time === slot.customer_arrival_time;
								return `
									<button type="button" class="portal-chip ${isSelected ? "is-selected" : ""}" data-slot-idx="${sIdx}">
										${escapeHtml(slot.display_time || slot.customer_arrival_time)}
									</button>
								`;
							})
							.join("")}
					</div>
				</div>
				<div class="portal-field">
					<label for="portal-reschedule-reason">Note for the shop (optional)</label>
					<input id="portal-reschedule-reason" class="portal-input" type="text" maxlength="160" placeholder="Optional note" />
				</div>
				<button type="button" class="portal-btn portal-btn--primary portal-btn--block" id="portal-reschedule-confirm-btn" ${selectedSlot ? "" : "disabled"}>
					Confirm New Drop-Off Time
				</button>
			`;

			body.querySelectorAll("[data-date-idx]").forEach((btn) => {
				btn.addEventListener("click", () => {
					selectedDateIdx = Number(btn.dataset.dateIdx);
					selectedSlot = null;
					renderPicker();
				});
			});

			body.querySelectorAll("[data-slot-idx]").forEach((btn) => {
				btn.addEventListener("click", () => {
					selectedSlot = activeGroup.slots[Number(btn.dataset.slotIdx)] || null;
					renderPicker();
				});
			});

			const confirmBtn = body.querySelector("#portal-reschedule-confirm-btn");
			confirmBtn.addEventListener("click", async () => {
				if (!selectedSlot) {
					return;
				}
				const reason = (body.querySelector("#portal-reschedule-reason").value || "").trim();
				confirmBtn.disabled = true;
				confirmBtn.textContent = "Rescheduling...";
				try {
					const res = await rescheduleAppointment({
						schedule_entry: scheduleEntry,
						target_date: selectedSlot.date,
						customer_arrival_time: selectedSlot.customer_arrival_time,
						reason,
					});
					close();
					if (notify) {
						notify("Drop-off appointment rescheduled. Confirmation SMS sent.", "success");
					}
					if (res && res.hub) {
						onHubUpdated(res.hub);
					}
				} catch (err) {
					renderPicker(err.message || "Unable to reschedule to that drop-off time. Please pick another slot.");
				}
			});
		};

		renderPicker();
	} catch (err) {
		body.innerHTML = `<div class="portal-alert portal-alert--danger" role="alert">${escapeHtml(err.message || "Unable to load available drop-off times.")}</div>`;
	}
}
