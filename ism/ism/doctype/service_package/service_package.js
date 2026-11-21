// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

function recalculate_service_rate(frm) {
	if (frm.doc.is_flat_rate) return;

	const hours = flt(frm.doc.standard_labor_hours);
	const rate = flt(frm.doc.hourly_rate);
	if (hours && rate) {
		frm.set_value("service_rate", hours * rate);
	}
}

frappe.ui.form.on("Service Package", {
	refresh(frm) {
		frm.trigger("toggle_flat_rate_display");
		frm.trigger("toggle_self_serve_guidance");

		frm.add_custom_button(__("Tesla Manual"), () => {
			frm.trigger("open_tesla_manual");
		});

		frm.add_custom_button(__("Import from Tesla URL"), () => {
			frm.trigger("open_import_dialog");
		});
	},

	open_tesla_manual(frm) {
		const url = frm.doc.tesla_manual_url || "https://service.tesla.com/docs/";
		window.open(url, "_blank");
	},

	open_import_dialog(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Import from Tesla Service Manual"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "help_text",
					options: `<div class="text-muted small mb-2">${__(
						"Paste the URL of any procedure from the Tesla Service Manual (e.g. https://service.tesla.com/docs/Model3/ServiceManual/.../GUID-XXXX.html). Title, Correction Code, and Flat Rate Time (FRT) will be populated automatically."
					)}</div>`,
				},
				{
					fieldname: "tesla_url",
					fieldtype: "Data",
					label: __("Tesla Procedure URL"),
					reqd: 1,
					default: frm.doc.tesla_manual_url || "",
					placeholder: "https://service.tesla.com/docs/...",
				},
			],
			primary_action_label: __("Fetch & Populate"),
			primary_action(values) {
				const url = values.tesla_url.trim();
				frappe.call({
					method: "ism.api.tesla.fetch_and_parse_tesla_procedure",
					args: { url },
					freeze: true,
					freeze_message: __("Fetching procedure from Tesla manual..."),
					callback(r) {
						if (!r?.message) return;

						const d = r.message;
						dialog.hide();

						const code = d.correction_code ? d.correction_code.trim() : "";
						let title = d.title ? d.title.trim() : "";
						if (code && title.startsWith(code)) {
							const escaped_code = code.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
							title = title
								.replace(new RegExp(`^${escaped_code}\\s*[-–—:]?\\s*`), "")
								.trim();
						}

						frm.set_value("package_name", title);
						if (code) frm.set_value("correction_code", code);
						if (d.frt) frm.set_value("standard_labor_hours", flt(d.frt));
						if (d.doc_guid) frm.set_value("doc_guid", d.doc_guid);
						if (d.applicable_model) frm.set_value("applicable_model", d.applicable_model);
						if (d.applicable_generation) {
							frm.set_value("applicable_generation", d.applicable_generation);
						}
						if (d.is_mobile_capable !== undefined) {
							frm.set_value("is_mobile_capable", d.is_mobile_capable ? 1 : 0);
						}
						frm.set_value("tesla_manual_url", url);

						frappe.show_alert(
							{
								message: __("Loaded: {0} ({1} hrs)", [
									title || code,
									d.frt || frm.doc.standard_labor_hours,
								]),
								indicator: "green",
							},
							5
						);
					},
				});
			},
		});
		dialog.show();
	},

	is_flat_rate(frm) {
		frm.trigger("toggle_flat_rate_display");
		recalculate_service_rate(frm);
	},

	standard_labor_hours(frm) {
		recalculate_service_rate(frm);
	},

	hourly_rate(frm) {
		recalculate_service_rate(frm);
	},

	toggle_flat_rate_display(frm) {
		if (frm.doc.is_flat_rate) {
			frm.set_df_property(
				"service_rate",
				"description",
				__("Fixed flat rate labor/service amount billed to customer.")
			);
			frm.set_df_property(
				"hourly_rate",
				"description",
				__("Optional: Hourly rate is ignored for flat rate services.")
			);
		} else {
			frm.set_df_property("service_rate", "description", __("Calculated as FRT × Hourly Rate."));
			frm.set_df_property("hourly_rate", "description", "");
		}
	},

	allow_self_serve_booking(frm) {
		frm.trigger("toggle_self_serve_guidance");
	},

	customer_price_display_mode(frm) {
		frm.trigger("toggle_self_serve_guidance");
	},

	toggle_self_serve_guidance(frm) {
		if (!frm.doc.allow_self_serve_booking) {
			frm.set_df_property("customer_title", "description", "");
			frm.set_df_property("customer_price_display_mode", "description", "");
			frm.set_df_property("customer_estimate_min", "description", "");
			frm.set_df_property("customer_estimate_max", "description", "");
			frm.set_df_property("self_serve_duration_minutes", "description", "");
			frm.set_df_property("self_serve_required_tags", "description", "");
			return;
		}

		frm.set_df_property(
			"customer_title",
			"description",
			__("Shown on the public booking page. Falls back to Service / Job Title when blank.")
		);
		frm.set_df_property(
			"self_serve_duration_minutes",
			"description",
			__("Leave 0 to compute shop duration automatically from FRT.")
		);
		frm.set_df_property(
			"self_serve_required_tags",
			"description",
			__("Comma-separated Equipment Tag names (e.g., Lift, Alignment Rack) used for bay gating.")
		);

		const mode = frm.doc.customer_price_display_mode || "Exact Price";
		const mode_help = {
			"Exact Price": __("Displays a single fixed price (falls back to Service Rate + default parts if 0)."),
			"Starting At": __("Displays 'Starting at $X' using Customer Estimate Min (or calculated rate if 0)."),
			"Estimate Range": __("Displays a min–max estimate range; Max must be >= Min."),
			"Contact for Quote": __("Hides numeric pricing and prompts the customer to request a quote."),
		};
		frm.set_df_property("customer_price_display_mode", "description", mode_help[mode] || "");
		frm.set_df_property(
			"customer_estimate_min",
			"description",
			mode === "Estimate Range"
				? __("Lower bound of the customer estimate (CAD).")
				: __("Customer-facing price (CAD). Leave 0 to derive from Service Rate + default parts.")
		);
		frm.set_df_property(
			"customer_estimate_max",
			"description",
			__("Upper bound of the customer estimate (CAD). Must be >= Min.")
		);
	},
});
