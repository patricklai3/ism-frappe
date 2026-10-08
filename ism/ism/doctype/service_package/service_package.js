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
		let url = frm.doc.tesla_manual_url;
		if (!url) {
			const model = frm.doc.applicable_model || "Model 3";
			const cleanModel = model.replace(/\s+/g, "");
			if (cleanModel === "Model3") {
				url = "https://service.tesla.com/docs/Model3/ServiceManual/en-us/";
			} else if (cleanModel === "ModelY") {
				url = "https://service.tesla.com/docs/ModelY/ServiceManual/en-us/";
			} else if (cleanModel === "ModelS") {
				url = "https://service.tesla.com/docs/ModelS/Palladium/ServiceManual/en-us/";
			} else if (cleanModel === "ModelX") {
				url = "https://service.tesla.com/docs/ModelX/Palladium/ServiceManual/en-us/";
			} else if (cleanModel === "Cybertruck") {
				url = "https://service.tesla.com/docs/Cybertruck/ServiceManual/en-us/";
			} else {
				url = "https://service.tesla.com/docs/";
			}
		}
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
						"Paste the URL of any procedure from the Tesla Service Manual (e.g. https://service.tesla.com/docs/Model3/ServiceManual/.../GUID-XXXX.html). Title, Correction Code, Flat Rate Time (FRT), and Category will be populated automatically."
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
							const title = d.title ? d.title.trim() : "";
							const pkgName = code && !title.startsWith(code) ? `${code} - ${title}` : title;

							frm.set_value("package_name", pkgName);
							if (code) frm.set_value("correction_code", code);
							if (d.frt) {
								frm.set_value("frt", flt(d.frt));
								frm.set_value("standard_labor_hours", flt(d.frt));
							}
							if (d.applicable_model) frm.set_value("applicable_model", d.applicable_model);
							if (d.applicable_generation) frm.set_value("applicable_generation", d.applicable_generation);
							if (d.gen_start_year_month) frm.set_value("gen_start_year_month", d.gen_start_year_month);
							if (d.gen_end_year_month) frm.set_value("gen_end_year_month", d.gen_end_year_month);
							if (d.category) frm.set_value("category", d.category);
							if (d.subcategory) frm.set_value("subcategory", d.subcategory);
							if (d.doc_guid) frm.set_value("doc_guid", d.doc_guid);
							frm.set_value("tesla_manual_url", url);
							if (d.description && !frm.doc.description) {
								frm.set_value("description", d.description);
							}

							frappe.show_alert({
								message: __("Loaded: {0} ({1} hrs)", [title || code, d.frt]),
								indicator: "green",
							}, 5);
						}
					},
				});
			},
		});
		dialog.show();
	},

	correction_code(frm) {
		if (frm.doc.correction_code) {
			frappe.call({
				method: "ism.utils.tesla_manual.get_category_for_correction_code",
				args: { correction_code: frm.doc.correction_code },
				callback(r) {
					if (r && r.message) {
						const [cat, subcat] = r.message;
						if (cat && (!frm.doc.category || frm.doc.category === "Maintenance")) {
							frm.set_value("category", cat);
						}
						if (subcat && !frm.doc.subcategory) {
							frm.set_value("subcategory", subcat);
						}
					}
				},
			});
		}
	},

	frt(frm) {
		if (flt(frm.doc.frt) > 0) {
			frm.set_value("standard_labor_hours", flt(frm.doc.frt));
			if (!frm.doc.is_flat_rate && flt(frm.doc.hourly_rate)) {
				frm.set_value("service_rate", flt(frm.doc.frt) * flt(frm.doc.hourly_rate));
			}
		}
	},

	is_flat_rate(frm) {
		frm.trigger("toggle_flat_rate_display");
		if (!frm.doc.is_flat_rate) {
			const hours = flt(frm.doc.standard_labor_hours) || flt(frm.doc.frt);
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
			const hours = flt(frm.doc.standard_labor_hours) || flt(frm.doc.frt);
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
