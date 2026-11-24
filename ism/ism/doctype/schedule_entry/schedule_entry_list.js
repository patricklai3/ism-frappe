// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.provide("frappe.listview_settings");

frappe.listview_settings["Schedule Entry"] = {
	add_fields: [
		"title",
		"scheduled_date",
		"scheduled_time",
		"estimated_duration",
		"service_bay",
		"assigned_technician",
		"customer",
		"repair_vehicle",
		"provisional_vehicle_info",
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

		repair_vehicle: function (val, df, doc) {
			let label = "";
			if (doc && doc.title && doc.title.includes(" | ")) {
				label = doc.title.split(" | ").slice(1).join(" | ").trim();
			}
			label = label || (doc && doc.provisional_vehicle_info) || val || "";
			if (!label) return "";
			const safe_label = frappe.utils.escape_html(label);
			if (val && val !== label) {
				const safe_vin = frappe.utils.escape_html(val);
				return `<span title="VIN: ${safe_vin}">${safe_label}</span>`;
			}
			return safe_label;
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

		listview.page.add_inner_button(__("Calendar View"), function () {
			frappe.set_route("List", "Schedule Entry", "Calendar");
		});
	},
};
