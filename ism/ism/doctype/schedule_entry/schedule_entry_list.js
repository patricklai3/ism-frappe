// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.provide("frappe.listview_settings");

frappe.listview_settings["Schedule Entry"] = {
	add_fields: [
		"scheduled_date",
		"scheduled_time",
		"estimated_duration",
		"service_bay",
		"assigned_technician",
		"customer",
		"repair_vehicle",
		"repair_order",
		"status",
	],

	get_indicator: function (doc) {
		const status_colors = {
			"Draft": "gray",
			"Scheduled": "blue",
			"Confirmed": "green",
			"In Progress": "orange",
			"Completed": "cyan",
			"Cancelled": "red",
			"No Show": "dark",
		};
		const color = status_colors[doc.status] || "gray";
		return [__(doc.status), color, `status,=,${doc.status}`];
	},

	formatters: {
		service_bay: function (val) {
			if (!val) {
				return `<span class="text-muted" style="font-style: italic;">${__("Unassigned")}</span>`;
			}
			return `<span class="badge badge-secondary" style="font-weight: 500; padding: 3px 7px;">${frappe.utils.escape_html(val)}</span>`;
		},

		assigned_technician: function (val) {
			if (!val) {
				return `<span class="badge badge-warning" style="background-color: #feebc8; color: #744210; border: 1px solid #fbd38d; font-weight: 500; padding: 3px 7px;">${__("Unassigned")}</span>`;
			}
			return `<span class="badge badge-info" style="background-color: #ebf8ff; color: #2b6cb0; border: 1px solid #bee3f8; font-weight: 500; padding: 3px 7px;">${frappe.utils.escape_html(val)}</span>`;
		},

		estimated_duration: function (val) {
			if (val === null || val === undefined) return "";
			return `<span style="font-family: monospace; font-size: 11px;">${val} min</span>`;
		},
	},

	onload: function (listview) {
		const group_name = __("Quick Filters");

		const apply_filter = function (filters) {
			listview.filter_area.clear().then(() => {
				if (filters && filters.length) {
					listview.filter_area.add(filters);
				} else {
					listview.refresh();
				}
			});
		};

		listview.page.add_inner_button(__("All Entries"), function () {
			apply_filter([]);
		}, group_name);

		listview.page.add_inner_button(__("Today's Jobs"), function () {
			apply_filter([
				["Schedule Entry", "scheduled_date", "=", frappe.datetime.get_today()],
			]);
		}, group_name);

		listview.page.add_inner_button(__("Unassigned Tech"), function () {
			apply_filter([
				["Schedule Entry", "assigned_technician", "is", "not set"],
			]);
		}, group_name);

		listview.page.add_inner_button(__("In Progress"), function () {
			apply_filter([
				["Schedule Entry", "status", "=", "In Progress"],
			]);
		}, group_name);

		// Intercept standard "Add Schedule Entry" dialog / shortcut key
		listview.new_doctype_dialog = function () {
			if (typeof ism !== "undefined" && ism.SchedulingDialog) {
				new ism.SchedulingDialog({
					on_success: () => listview.refresh(),
				}).show();
			} else {
				frappe.new_doc("Schedule Entry");
			}
		};
	},

	refresh: function (listview) {
		// Ensure primary action button triggers the Shop Capacity Scheduling Dialog
		listview.page.set_primary_action(__("Schedule Work Allocation"), function () {
			if (typeof ism !== "undefined" && ism.SchedulingDialog) {
				new ism.SchedulingDialog({
					on_success: () => listview.refresh(),
				}).show();
			} else {
				frappe.new_doc("Schedule Entry");
			}
		});

		// Also provide a dedicated inner action button on the list page
		listview.page.add_inner_button(__("Capacity Assistant"), function () {
			if (typeof ism !== "undefined" && ism.SchedulingDialog) {
				new ism.SchedulingDialog({
					on_success: () => listview.refresh(),
				}).show();
			} else {
				frappe.new_doc("Schedule Entry");
			}
		});
	},
};
