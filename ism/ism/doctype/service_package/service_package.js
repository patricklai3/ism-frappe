// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.ui.form.on("Service Package", {
	refresh(frm) {
		frm.trigger("toggle_flat_rate_display");
	},

	is_flat_rate(frm) {
		frm.trigger("toggle_flat_rate_display");
		if (!frm.doc.is_flat_rate) {
			const hours = flt(frm.doc.standard_labor_hours);
			const rate = flt(frm.doc.hourly_rate);
			if (hours && rate) {
				frm.set_value("service_rate", hours * rate);
			}
		}
	},

	standard_labor_hours(frm) {
		if (!frm.doc.is_flat_rate) {
			const hours = flt(frm.doc.standard_labor_hours);
			const rate = flt(frm.doc.hourly_rate);
			if (hours && rate) {
				frm.set_value("service_rate", hours * rate);
			}
		}
	},

	hourly_rate(frm) {
		if (!frm.doc.is_flat_rate) {
			const hours = flt(frm.doc.standard_labor_hours);
			const rate = flt(frm.doc.hourly_rate);
			frm.set_value("service_rate", hours * rate);
		}
	},

	toggle_flat_rate_display(frm) {
		if (frm.doc.is_flat_rate) {
			frm.set_df_property("service_rate", "description", __("Fixed flat rate labor/service amount billed to customer."));
			frm.set_df_property("hourly_rate", "description", __("Optional: Hourly rate is ignored for flat rate services."));
		} else {
			frm.set_df_property("service_rate", "description", __("Calculated as Labor Hours × Hourly Rate."));
			frm.set_df_property("hourly_rate", "description", "");
		}
	},
});
