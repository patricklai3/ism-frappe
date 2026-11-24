// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

let _generationCatalogPromise = null;

function load_generation_catalog() {
	if (!_generationCatalogPromise) {
		_generationCatalogPromise = frappe
			.call({ method: "ism.api.tesla.get_vehicle_generation_catalog" })
			.then((r) => r?.message || {})
			.catch(() => ({}));
	}
	return _generationCatalogPromise;
}

function update_generation_helper_text(frm) {
	load_generation_catalog().then((catalog) => {
		const model = frm.doc.applicable_model || "";
		const entries = model && catalog[model] ? catalog[model] : [];
		if (entries.length) {
			const names = entries.map((e) => e.generation_name).join(" | ");
			frm.set_df_property(
				"applicable_generation",
				"description",
				__("Options for {0}: {1}", [model, names])
			);
		} else {
			frm.set_df_property(
				"applicable_generation",
				"description",
				__("Canonical Tesla generation(s), e.g. Model 3 Jan 2024")
			);
		}
	});
}

function apply_generation_entries_to_form(frm, entries) {
	if (!Array.isArray(entries) || !entries.length) return;
	frm.clear_table("generation_compatibility");
	entries.forEach((entry) => {
		const row = frm.add_child("generation_compatibility");
		row.applicable_model = entry.applicable_model || frm.doc.applicable_model || "";
		row.generation_name = entry.generation_name || "";
		row.date_range = entry.date_range || "";
		row.year_from = entry.year_from || null;
		row.month_from = entry.month_from || null;
		row.year_to = entry.year_to || null;
		row.month_to = entry.month_to || null;
		row.manual_base_url = entry.manual_base_url || "";
	});
	frm.refresh_field("generation_compatibility");
}

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
		update_generation_helper_text(frm);

		frm.add_custom_button(__("Tesla Manual"), () => {
			frm.trigger("open_tesla_manual");
		});

		frm.add_custom_button(__("Select Generations"), () => {
			frm.trigger("open_generation_picker");
		});

		frm.add_custom_button(__("Import from Tesla URL"), () => {
			frm.trigger("open_import_dialog");
		});
	},

	applicable_model(frm) {
		update_generation_helper_text(frm);
	},

	open_generation_picker(frm) {
		load_generation_catalog().then((catalog) => {
			const model = frm.doc.applicable_model;
			const pool = model && catalog[model]
				? catalog[model]
				: Object.values(catalog).flat();
			if (!pool.length) {
				frappe.msgprint(__("No Tesla generations configured for the selected model."));
				return;
			}
			const current = new Set(
				String(frm.doc.applicable_generation || "")
					.split(/[,;]+/)
					.map((s) => s.trim())
					.filter(Boolean)
			);
			const fields = pool.map((entry, idx) => ({
				fieldtype: "Check",
				fieldname: `gen_${idx}`,
				label: `${entry.generation_name} (${entry.date_range || ""})`,
				default: current.has(entry.generation_name) ? 1 : 0,
			}));
			const dlg = new frappe.ui.Dialog({
				title: __("Select Compatible Generations"),
				fields,
				primary_action_label: __("Apply"),
				primary_action(values) {
					const chosen = pool.filter((_, idx) => Boolean(values[`gen_${idx}`]));
					frm.set_value(
						"applicable_generation",
						chosen.map((e) => e.generation_name).join(", ")
					);
					if (!frm.doc.applicable_model && chosen.length) {
						const models = [...new Set(chosen.map((e) => e.applicable_model))];
						frm.set_value(
							"applicable_model",
							models.length === 1 ? models[0] : "All Models"
						);
					}
					if (chosen.length) {
						apply_generation_entries_to_form(frm, chosen);
					} else {
						frm.clear_table("generation_compatibility");
						frm.refresh_field("generation_compatibility");
					}
					dlg.hide();
				},
			});
			dlg.show();
		});
	},

	open_tesla_manual(frm) {
		if (frm.doc.tesla_manual_url) {
			window.open(frm.doc.tesla_manual_url, "_blank");
			return;
		}
		load_generation_catalog().then((catalog) => {
			const model = frm.doc.applicable_model || "";
			const entries = model && catalog[model] ? catalog[model] : [];
			const rawGens = String(frm.doc.applicable_generation || "")
				.split(/[,;]+/)
				.map((s) => s.trim())
				.filter(Boolean);
			const matched = entries.find((e) => rawGens.includes(e.generation_name)) || entries[0];
			const url = matched?.manual_base_url || "https://service.tesla.com/docs/";
			window.open(url, "_blank");
		});
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
						if (Array.isArray(d.generation_entries) && d.generation_entries.length) {
							apply_generation_entries_to_form(frm, d.generation_entries);
						}
						if (Array.isArray(d.suggested_equipment_tags) && d.suggested_equipment_tags.length) {
							const existingTags = new Set(
								(frm.doc.equipment_tags || []).map((r) => r.equipment_tag).filter(Boolean)
							);
							let added = false;
							d.suggested_equipment_tags.forEach((tag) => {
								if (tag && !existingTags.has(tag)) {
									const row = frm.add_child("equipment_tags");
									row.equipment_tag = tag;
									existingTags.add(tag);
									added = true;
								}
							});
							if (added) {
								frm.refresh_field("equipment_tags");
							}
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
