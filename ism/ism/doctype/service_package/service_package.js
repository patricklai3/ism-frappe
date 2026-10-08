// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.ui.form.on("Service Package", {
	refresh(frm) {
		frm.trigger("toggle_flat_rate_display");

		// Top-level standalone action buttons
		frm.add_custom_button(__("Tesla Manual"), () => {
			frm.trigger("open_tesla_manual");
		});

		frm.add_custom_button(__("Import from Tesla URL"), () => {
			frm.trigger("open_import_dialog");
		});
	},

	btn_open_tesla_manual(frm) {
		frm.trigger("open_tesla_manual");
	},

	btn_import_from_url(frm) {
		frm.trigger("open_import_dialog");
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
					args: { url: url },
					freeze: true,
					freeze_message: __("Fetching procedure from Tesla manual..."),
					callback(r) {
						if (r && r.message) {
							const d = r.message;
							dialog.hide();

							const code = d.correction_code ? d.correction_code.trim() : "";
							let title = d.title ? d.title.trim() : "";
							if (code && title.startsWith(code)) {
								title = title.replace(new RegExp(`^${code}\\s*[-–—:]?\\s*`), "").trim();
							}

							frm.set_value("package_name", title);
							if (code) frm.set_value("correction_code", code);
							if (d.frt) {
								frm.set_value("standard_labor_hours", flt(d.frt));
							}
							if (d.doc_guid) frm.set_value("doc_guid", d.doc_guid);
							if (d.is_mobile_capable !== undefined) frm.set_value("is_mobile_capable", d.is_mobile_capable ? 1 : 0);
							frm.set_value("tesla_manual_url", url);

							frappe.show_alert({
								message: __("Loaded: {0} ({1} hrs)", [title || code, d.frt || frm.doc.standard_labor_hours]),
								indicator: "green",
							}, 5);
						}
					},
				});
			},
		});
		dialog.show();
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
			frm.set_df_property("service_rate", "description", __("Calculated as FRT × Hourly Rate."));
			frm.set_df_property("hourly_rate", "description", "");
		}
	},
});
