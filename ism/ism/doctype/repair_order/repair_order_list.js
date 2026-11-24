// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.listview_settings["Repair Order"] = {
	has_indicator_for_draft: true,
	has_indicator_for_cancelled: true,
	add_fields: [
		"status",
		"approval_status",
		"vehicle_location",
		"customer",
		"vehicle",
		"vehicle_display",
		"grand_total",
		"docstatus",
	],
	formatters: {
		vehicle_display: function (val, df, doc) {
			const label = (val || doc.vehicle || "").trim();
			if (!label) return "";
			const safe_label = frappe.utils.escape_html(label);
			const vin = (doc.vehicle || "").trim();
			if (vin && vin !== label) {
				const safe_vin = frappe.utils.escape_html(vin);
				return `<span title="VIN: ${safe_vin}">${safe_label}</span>`;
			}
			return safe_label;
		},
	},
	get_indicator: function (doc) {
		const status_colors = {
			"Checked In": "blue",
			"Draft Estimate": "orange",
			"Pending Authorization": "yellow",
			"Authorized - In Progress": "blue",
			"Waiting on Parts": "orange",
			"Work Completed": "purple",
			"Billed / Paid": "green",
			"Closed / Released": "gray",
			"Cancelled": "red",
		};
		const status = doc.status || (doc.docstatus === 2 ? "Cancelled" : "Draft Estimate");
		const color = status_colors[status] || "blue";

		return [__(status), color, `status,=,${status}`];
	},
};
