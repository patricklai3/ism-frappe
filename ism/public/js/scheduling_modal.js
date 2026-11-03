// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.provide("ism");

ism.SchedulingDialog = class SchedulingDialog {
	constructor(opts = {}) {
		this.opts = opts;
		this.frm = opts.frm;

		// Resolve Repair Order context
		let ro = opts.repair_order;
		if (!ro && this.frm && this.frm.doc) {
			if (this.frm.doc.doctype === "Repair Order") {
				ro = this.frm.is_new() ? null : this.frm.doc.name;
			} else if (this.frm.doc.doctype === "Schedule Entry") {
				ro = this.frm.doc.repair_order || null;
			}
		}
		this.repair_order = (ro && !String(ro).startsWith("new-")) ? ro : null;
		this.schedule_entry_name = (this.frm && this.frm.doc && this.frm.doc.doctype === "Schedule Entry" && !this.frm.is_new())
			? this.frm.doc.name
			: (opts.schedule_entry_name || null);

		this.current_date = opts.scheduled_date || (this.frm && this.frm.doc && this.frm.doc.scheduled_date) || frappe.datetime.get_today();
		this.selected_time = opts.scheduled_time || (this.frm && this.frm.doc && this.frm.doc.scheduled_time) || null;
		this.estimated_duration = opts.estimated_duration || (this.frm && this.frm.doc && this.frm.doc.estimated_duration) || 60;
		this.entry_type = opts.entry_type || (this.frm && this.frm.doc && this.frm.doc.entry_type) || (this.repair_order ? "Repair" : "Diagnostic");
		this.service_bay = opts.service_bay || (this.frm && this.frm.doc && this.frm.doc.service_bay) || null;
		this.assigned_technician = opts.assigned_technician || (this.frm && this.frm.doc && this.frm.doc.assigned_technician) || null;
		this.notes = opts.notes || (this.frm && this.frm.doc && this.frm.doc.notes) || "";

		this.unscheduled_services = [];
		this.selected_service_names = [];
		this.on_success = opts.on_success;
		this.dialog = null;

		this.init();
	}

	init() {
		this.dialog = new frappe.ui.Dialog({
			title: __("Schedule Work Allocation"),
			fields: [
				{
					fieldname: "col_left",
					fieldtype: "Column Break",
				},
				{
					fieldname: "repair_order",
					fieldtype: "Link",
					label: __("Repair Order"),
					options: "Repair Order",
					default: this.repair_order,
					change: () => this.on_ro_change(),
				},
				{
					fieldname: "services_html",
					fieldtype: "HTML",
				},
				{
					fieldname: "entry_type",
					fieldtype: "Link",
					label: __("Entry Type"),
					options: "Schedule Entry Type",
					default: this.entry_type,
					reqd: 1,
				},
				{
					fieldname: "estimated_duration",
					fieldtype: "Int",
					label: __("Duration (minutes)"),
					default: this.estimated_duration,
					reqd: 1,
					change: () => this.refresh_slots(),
				},
				{
					fieldname: "sec_alloc",
					fieldtype: "Section Break",
					label: __("Capacity Reservation"),
				},
				{
					fieldname: "window_summary_html",
					fieldtype: "HTML",
				},
				{
					fieldname: "service_bay",
					fieldtype: "Link",
					label: __("Service Bay"),
					options: "Service Bay",
					default: this.service_bay,
					reqd: 1,
					change: () => this.on_resource_change(),
				},
				{
					fieldname: "assigned_technician",
					fieldtype: "Link",
					label: __("Assigned Technician"),
					options: "Employee",
					default: this.assigned_technician,
					change: () => this.on_resource_change(),
				},
				{
					fieldname: "conflict_warning_html",
					fieldtype: "HTML",
				},
				{
					fieldname: "notes",
					fieldtype: "Small Text",
					label: __("Work / Allocation Notes"),
					default: this.notes,
				},

				{
					fieldname: "col_right",
					fieldtype: "Column Break",
				},
				{
					fieldname: "date_nav_html",
					fieldtype: "HTML",
				},
				{
					fieldname: "slots_html",
					fieldtype: "HTML",
				},
			],
			primary_action_label: __("Confirm Schedule Entry"),
			primary_action: () => this.allocate_entry(),
		});

		// Widen modal dialog for comfortable two-column dispatch layout
		this.dialog.$wrapper.find(".modal-dialog").css({
			"max-width": "980px",
			"width": "92%",
		});

		this.setup_bay_queries();
		this.render_date_nav();
		this.render_window_summary();

		if (this.repair_order) {
			this.toggle_field("entry_type", false);
			this.load_unscheduled_services(this.repair_order);
		} else {
			this.toggle_field("services_html", false);
			this.toggle_field("entry_type", true);
			this.refresh_slots();
		}
	}

	show() {
		if (this.dialog) {
			this.dialog.show();
		}
	}

	toggle_field(fieldname, show) {
		if (!this.dialog) return;
		const field = this.dialog.get_field(fieldname);
		if (field) {
			field.df.hidden = show ? 0 : 1;
			if (field.refresh) {
				field.refresh();
			}
			if (field.$wrapper) {
				field.$wrapper.toggle(Boolean(show));
			}
		}
	}

	setup_bay_queries() {
		this.dialog.fields_dict.service_bay.get_query = () => {
			return {
				filters: { is_active: 1 },
			};
		};
	}

	render_date_nav() {
		const $wrapper = $(this.dialog.fields_dict.date_nav_html.wrapper);
		const today = frappe.datetime.get_today();
		const is_today = this.current_date === today;

		const nav_html = `
			<div class="date-navigator-container p-2 mb-3 bg-light rounded border d-flex align-items-center justify-content-between" style="background: var(--bg-light-gray, #f8f9fa);">
				<div class="d-flex align-items-center gap-1">
					<button type="button" class="btn btn-xs btn-default btn-prev-day" title="${__("Previous Day")}">
						<i class="fa fa-chevron-left"></i>
					</button>
					<button type="button" class="btn btn-xs ${is_today ? "btn-primary" : "btn-default"} btn-today">
						${__("Today")}
					</button>
					<button type="button" class="btn btn-xs btn-default btn-next-day" title="${__("Next Day")}">
						<i class="fa fa-chevron-right"></i>
					</button>
				</div>
				<div class="d-flex align-items-center gap-2">
					<span class="font-weight-bold text-muted small">${__("Date")}:</span>
					<input type="date" class="form-control form-control-sm input-nav-date" style="width: 145px; height: 28px;" value="${this.current_date}" />
				</div>
			</div>
		`;

		$wrapper.html(nav_html);

		$wrapper.find(".btn-prev-day").on("click", () => {
			this.current_date = frappe.datetime.add_days(this.current_date, -1);
			this.render_date_nav();
			this.render_window_summary();
			this.refresh_slots();
		});

		$wrapper.find(".btn-next-day").on("click", () => {
			this.current_date = frappe.datetime.add_days(this.current_date, 1);
			this.render_date_nav();
			this.render_window_summary();
			this.refresh_slots();
		});

		$wrapper.find(".btn-today").on("click", () => {
			this.current_date = frappe.datetime.get_today();
			this.render_date_nav();
			this.render_window_summary();
			this.refresh_slots();
		});

		$wrapper.find(".input-nav-date").on("change", (e) => {
			const val = $(e.currentTarget).val();
			if (val) {
				this.current_date = val;
				this.render_date_nav();
				this.render_window_summary();
				this.refresh_slots();
			}
		});
	}

	on_ro_change() {
		const ro = this.dialog.get_value("repair_order");
		if (ro && !String(ro).startsWith("new-")) {
			this.repair_order = ro;
			this.toggle_field("entry_type", false);
			this.toggle_field("services_html", true);
			this.load_unscheduled_services(ro);
		} else {
			this.repair_order = null;
			this.unscheduled_services = [];
			this.selected_service_names = [];
			this.toggle_field("entry_type", true);
			this.toggle_field("services_html", false);
			this.refresh_slots();
		}
	}

	load_unscheduled_services(ro_name) {
		const $wrapper = $(this.dialog.fields_dict.services_html.wrapper);
		$wrapper.html(`<div class="text-muted small py-2"><i class="fa fa-spinner fa-spin"></i> ${__("Loading services for allocation...")}</div>`);

		frappe.call({
			method: "ism.api.scheduling.get_unscheduled_services",
			args: { repair_order: ro_name },
			callback: (r) => {
				this.unscheduled_services = r.message || [];
				this.selected_service_names = this.unscheduled_services.map((s) => s.name);
				this.render_services_checklist();
				this.recalculate_services_duration();
			},
		});
	}

	render_services_checklist() {
		const $wrapper = $(this.dialog.fields_dict.services_html.wrapper);
		if (!this.unscheduled_services.length) {
			$wrapper.html(`
				<div class="alert alert-info small p-2 mb-2">
					${__("All services for this Repair Order have already been allocated, or no services exist. You can allocate a custom time block below.")}
				</div>
			`);
			return;
		}

		let html = `
			<div class="services-checklist border rounded p-2 mb-2 bg-white" style="max-height: 160px; overflow-y: auto;">
				<div class="d-flex justify-content-between align-items-center mb-1 pb-1 border-bottom">
					<span class="font-weight-bold small text-muted text-uppercase">${__("Unscheduled Services & Supplements")}</span>
					<span class="badge badge-light text-muted small">${this.unscheduled_services.length} ${__("available")}</span>
				</div>
		`;

		this.unscheduled_services.forEach((s) => {
			const is_checked = this.selected_service_names.includes(s.name);
			const supp_badge = s.is_supplement ? `<span class="badge badge-warning text-dark ml-1" style="font-size: 10px;">${__("Supplement")}</span>` : "";
			html += `
				<div class="form-check d-flex align-items-center justify-content-between py-1 border-bottom" style="font-size: 12px;">
					<label class="form-check-label d-flex align-items-center cursor-pointer m-0 text-truncate" style="gap: 6px;">
						<input type="checkbox" class="form-check-input chk-service-item" data-name="${s.name}" data-mins="${s.labor_minutes}" ${is_checked ? "checked" : ""}>
						<span class="text-truncate">${frappe.utils.escape_html(s.service_title)}${supp_badge}</span>
					</label>
					<span class="text-muted font-weight-bold ml-2 text-nowrap">${s.labor_hours.toFixed(1)} hrs</span>
				</div>
			`;
		});

		html += `</div>`;
		$wrapper.html(html);

		$wrapper.find(".chk-service-item").on("change", (e) => {
			const $chk = $(e.currentTarget);
			const name = $chk.attr("data-name");
			if ($chk.is(":checked")) {
				if (!this.selected_service_names.includes(name)) {
					this.selected_service_names.push(name);
				}
			} else {
				this.selected_service_names = this.selected_service_names.filter((n) => n !== name);
			}
			this.recalculate_services_duration();
		});
	}

	recalculate_services_duration() {
		let total_mins = 0;
		this.unscheduled_services.forEach((s) => {
			if (this.selected_service_names.includes(s.name)) {
				total_mins += s.labor_minutes || 0;
			}
		});

		if (total_mins <= 0) {
			total_mins = 60;
		}

		this.estimated_duration = total_mins;
		this.dialog.set_value("estimated_duration", total_mins);
		this.render_window_summary();
		this.refresh_slots();
	}

	render_window_summary() {
		const $wrapper = $(this.dialog.fields_dict.window_summary_html.wrapper);
		const duration = this.dialog.get_value("estimated_duration") || this.estimated_duration || 60;

		if (!this.selected_time) {
			$wrapper.html(`
				<div class="alert alert-secondary text-muted small p-2 mb-2 d-flex align-items-center" style="gap: 8px;">
					<i class="fa fa-info-circle text-info"></i>
					<span>${__("Select an available capacity slot on the right to set the schedule window.")}</span>
				</div>
			`);
			return;
		}

		// Calculate end time
		const start_parts = this.selected_time.split(":");
		const start_mins = parseInt(start_parts[0]) * 60 + parseInt(start_parts[1]);
		const end_mins = start_mins + duration;
		const end_h = Math.floor(end_mins / 60) % 24;
		const end_m = end_mins % 60;
		const end_time_str = `${String(end_h).padStart(2, "0")}:${String(end_m).padStart(2, "0")}`;

		$wrapper.html(`
			<div class="card bg-light p-2 mb-2 border" style="border-left: 4px solid var(--primary, #171717) !important;">
				<div class="d-flex justify-content-between align-items-center">
					<div>
						<div class="font-weight-bold" style="font-size: 13px;">
							<i class="fa fa-calendar-check-o text-success mr-1"></i> ${this.current_date} • ${this.selected_time.slice(0, 5)} - ${end_time_str}
						</div>
						<div class="text-muted small">
							${__("Duration")}: ${duration} ${__("minutes")} (${(duration / 60).toFixed(1)} ${__("hrs")})
						</div>
					</div>
					<span class="badge badge-primary px-2 py-1">${__("Selected")}</span>
				</div>
			</div>
		`);
	}

	refresh_slots() {
		const duration = this.dialog.get_value("estimated_duration") || 60;
		const bay = this.dialog.get_value("service_bay");
		const date_val = this.current_date;

		if (!date_val) return;

		const $wrapper = $(this.dialog.fields_dict.slots_html.wrapper);
		$wrapper.html(`<div class="text-muted text-center py-4"><i class="fa fa-spinner fa-spin"></i> ${__("Checking shop capacity...")}</div>`);

		frappe.call({
			method: "ism.api.scheduling.get_available_slots",
			args: {
				date: date_val,
				duration_minutes: duration,
				service_bay: bay || undefined,
			},
			callback: (r) => {
				const slots = Array.isArray(r.message) ? r.message : ((r.message && r.message.slots) || []);
				if (!slots.length) {
					this.handle_zero_slots($wrapper, date_val, duration);
					return;
				}

				// Partition into Morning (< 12:00) and Afternoon (>= 12:00)
				const morning_slots = [];
				const afternoon_slots = [];

				slots.forEach((slot) => {
					const time = slot.start_time || "";
					const hour = parseInt(time.split(":")[0]) || 0;
					if (hour < 12) {
						morning_slots.push(slot);
					} else {
						afternoon_slots.push(slot);
					}
				});

				let html = `<div class="slots-container" style="max-height: 380px; overflow-y: auto; padding-right: 4px;">`;

				if (morning_slots.length) {
					html += `
						<div class="mb-3">
							<div class="font-weight-bold text-muted small text-uppercase mb-2 d-flex align-items-center" style="gap: 6px;">
								<i class="fa fa-sun-o text-warning"></i> ${__("Morning Slots (08:00 - 12:00)")}
							</div>
							<div class="d-flex flex-wrap gap-2" style="gap: 8px;">
					`;
					morning_slots.forEach((slot) => {
						html += this.render_slot_button(slot);
					});
					html += `</div></div>`;
				}

				if (afternoon_slots.length) {
					html += `
						<div class="mb-2">
							<div class="font-weight-bold text-muted small text-uppercase mb-2 d-flex align-items-center" style="gap: 6px;">
								<i class="fa fa-coffee text-info"></i> ${__("Afternoon Slots (12:30 - 17:00)")}
							</div>
							<div class="d-flex flex-wrap gap-2" style="gap: 8px;">
					`;
					afternoon_slots.forEach((slot) => {
						html += this.render_slot_button(slot);
					});
					html += `</div></div>`;
				}

				html += `</div>`;
				$wrapper.html(html);

				$wrapper.find(".btn-slot-block").on("click", (e) => {
					const $btn = $(e.currentTarget);
					const time = $btn.attr("data-time");
					const slot_bay = $btn.attr("data-bay");

					this.selected_time = time.length === 5 ? `${time}:00` : time;
					if (slot_bay && !this.dialog.get_value("service_bay")) {
						this.dialog.set_value("service_bay", slot_bay);
					} else if (!this.dialog.get_value("service_bay")) {
						this.auto_assign_resource();
					}

					$wrapper.find(".btn-slot-block").removeClass("btn-primary").addClass("btn-outline-primary");
					$btn.removeClass("btn-outline-primary").addClass("btn-primary");

					this.render_window_summary();
					this.check_conflicts();
				});
			},
		});
	}

	render_slot_button(slot) {
		const time = slot.start_time || "";
		const time_label = time.slice(0, 5);
		const is_selected = this.selected_time && this.selected_time.slice(0, 5) === time_label;
		const cap_label = slot.effective_capacity != null
			? `${slot.effective_capacity} open`
			: (slot.available_bays ? `${slot.available_bays} bay${slot.available_bays > 1 ? "s" : ""}` : "");

		return `
			<button type="button" class="btn btn-sm ${is_selected ? "btn-primary" : "btn-outline-primary"} btn-slot-block text-left" 
				data-time="${time}" data-bay="${slot.service_bay || ""}" style="padding: 6px 10px; border-radius: 6px;">
				<div class="font-weight-bold" style="font-size: 13px;">${time_label}</div>
				<div class="small opacity-75" style="font-size: 10px;">${cap_label}</div>
			</button>
		`;
	}

	handle_zero_slots($wrapper, date_val, duration) {
		$wrapper.html(`
			<div class="alert alert-warning text-center p-3 mb-3">
				<div class="font-weight-bold mb-1"><i class="fa fa-exclamation-triangle"></i> ${__("Shop Capacity Full")}</div>
				<div class="small text-muted">${__("No available capacity blocks for {0} minutes on {1}.", [duration, date_val])}</div>
				<div class="next-avail-spinner mt-2 text-muted small"><i class="fa fa-spinner fa-spin"></i> ${__("Searching next available date...")}</div>
			</div>
		`);

		frappe.call({
			method: "ism.api.scheduling.find_next_available_slot",
			args: {
				from_date: date_val,
				duration_minutes: duration,
				service_bay: this.dialog.get_value("service_bay") || undefined,
			},
			callback: (r) => {
				const res = r.message;
				const $spinner = $wrapper.find(".next-avail-spinner");
				if (res && res.date) {
					$spinner.replaceWith(`
						<div class="mt-2">
							<button type="button" class="btn btn-sm btn-primary btn-jump-date w-100">
								<i class="fa fa-arrow-right mr-1"></i> ${__("Jump to Next Available")}: ${res.date} @ ${res.first_slot.start_time.slice(0, 5)}
							</button>
						</div>
					`);
					$wrapper.find(".btn-jump-date").on("click", () => {
						this.current_date = res.date;
						this.selected_time = res.first_slot.start_time;
						this.render_date_nav();
						this.render_window_summary();
						this.refresh_slots();
					});
				} else {
					$spinner.replaceWith(`
						<div class="mt-2 text-muted small">${__("No available capacity found within scheduling horizon.")}</div>
					`);
				}
			},
		});
	}

	auto_assign_resource() {
		const duration = this.dialog.get_value("estimated_duration") || 60;
		if (!this.selected_time) return;

		frappe.call({
			method: "ism.api.scheduling.get_available_bays",
			args: {
				date: this.current_date,
				start_time: this.selected_time,
				duration_minutes: duration,
			},
			callback: (r) => {
				const bays = r.message || [];
				if (bays.length && !this.dialog.get_value("service_bay")) {
					this.dialog.set_value("service_bay", bays[0].bay_name);
				}
			},
		});
	}

	on_resource_change() {
		this.check_conflicts();
	}

	check_conflicts() {
		const bay = this.dialog.get_value("service_bay");
		const $warn_wrapper = $(this.dialog.fields_dict.conflict_warning_html.wrapper);
		const duration = this.dialog.get_value("estimated_duration") || 60;

		if (!bay || !this.selected_time || !this.current_date) {
			$warn_wrapper.empty();
			return;
		}

		frappe.call({
			method: "ism.api.scheduling.check_bay_availability",
			args: {
				service_bay: bay,
				date: this.current_date,
				start_time: this.selected_time,
				duration_minutes: duration,
				exclude_entry: this.schedule_entry_name || undefined,
			},
			callback: (r) => {
				const is_free = Boolean(r.message);
				if (!is_free) {
					$warn_wrapper.html(`
						<div class="alert alert-warning small p-2 mb-2 d-flex align-items-start" style="gap: 8px;">
							<i class="fa fa-exclamation-triangle text-warning mt-1"></i>
							<div>
								<strong>${__("Capacity Conflict:")}</strong> ${__("Bay {0} is already reserved for part of this time window. You may proceed if intentionally double-booking.", [bay])}
							</div>
						</div>
					`);
				} else {
					$warn_wrapper.empty();
				}
			},
		});
	}

	allocate_entry() {
		if (!this.selected_time) {
			frappe.msgprint({
				title: __("Slot Selection Required"),
				message: __("Please select an available capacity slot from the right panel before confirming."),
				indicator: "orange",
			});
			return;
		}

		const values = this.dialog.get_values();
		if (!values) return;

		const payload = {
			repair_order: this.repair_order || values.repair_order || null,
			entry_type: values.entry_type || this.entry_type,
			scheduled_date: this.current_date,
			scheduled_time: this.selected_time,
			estimated_duration: values.estimated_duration,
			service_bay: values.service_bay,
			assigned_technician: values.assigned_technician || null,
			notes: values.notes || null,
			selected_services: this.selected_service_names,
			schedule_entry_name: this.schedule_entry_name || null,
		};

		frappe.call({
			method: "ism.api.scheduling.allocate_schedule_entry",
			args: payload,
			freeze: true,
			freeze_message: __("Allocating shop capacity..."),
			callback: (r) => {
				if (r.message && r.message.name) {
					const entry_name = r.message.name;
					frappe.show_alert({
						message: __("Schedule Entry {0} confirmed.", [entry_name]),
						indicator: "green",
					});
					this.dialog.hide();

					if (this.on_success) {
						this.on_success(r.message);
					} else if (this.frm) {
						if (this.frm.doc.doctype === "Repair Order") {
							this.frm.reload_doc();
						} else if (this.frm.doc.doctype === "Schedule Entry") {
							if (this.frm.is_new()) {
								this.frm.dirty(false);
								frappe.set_route("Form", "Schedule Entry", entry_name);
							} else {
								this.frm.reload_doc();
							}
						}
					}
				}
			},
		});
	}
};

ism.open_scheduling_dialog = function (opts = {}) {
	return new ism.SchedulingDialog(opts).show();
};
