// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

(function () {
	function add_purchasing_parts_utility(frm) {
		// Only allow adding parts to draft/editable documents
		if (frm.doc.docstatus !== 0) return;

		// 1. Add top-level toolbar button under Tools menu
		frm.add_custom_button(__("Quick Add / Import Part"), () => {
			open_purchasing_parts_importer(frm);
		}, __("Tools"));

		// 2. Add action button directly on Items child table grid
		if (frm.fields_dict.items && frm.fields_dict.items.grid && frm.fields_dict.items.grid.grid_buttons) {
			frm.fields_dict.items.grid.add_custom_button(__("Quick Add / Import Part"), () => {
				open_purchasing_parts_importer(frm);
			});
		}
	}

	function resolve_warehouse(frm, done) {
		const doc_wh = frm.doc.set_warehouse
			|| frm.doc.set_from_warehouse
			|| frm.doc.warehouse;

		if (doc_wh) {
			return done(doc_wh);
		}

		frappe.call({
			method: "ism.api.parts.get_default_warehouse",
			args: { company: frm.doc.company || "" },
			callback: (r) => {
				const wh = (r && r.message)
					|| (frappe.boot && frappe.boot.sysdefaults && frappe.boot.sysdefaults.default_warehouse)
					|| "";
				done(wh);
			},
		});
	}

	function open_purchasing_parts_importer(frm) {
		// Extract any previously linked services from existing items on this document
		const existing_services = [];
		(frm.doc.items || []).forEach((r) => {
			if (r.description) {
				const match = r.description.match(/\[For:\s*([^\]]+)\]/);
				if (match && match[1]) {
					match[1].split(",").forEach((s) => {
						const trimmed = s.trim();
						if (trimmed && !existing_services.includes(trimmed)) {
							existing_services.push(trimmed);
						}
					});
				}
			}
		});

		const launch = () => {
			ism.parts.show_quick_importer({
				default_mode: "single",
				existing_services: existing_services,
				callback: (created_items) => {
					if (!created_items || created_items.length === 0) return;

					resolve_warehouse(frm, (default_wh) => {
						const schedule_date = frm.doc.schedule_date || frappe.datetime.get_today();

						created_items.forEach((it) => {
							const row = frm.add_child("items");
							row.item_code = it.item_code;
							row.item_name = it.item_name || it.item_code;
							const for_text = (it.service_packages && it.service_packages.length)
								? ` [For: ${it.service_packages.join(", ")}]`
								: "";
							row.description = `${it.item_name || it.item_code}${for_text}`;
							row.qty = flt(it.qty) || 1.0;
							row.stock_qty = row.qty;
							row.uom = it.stock_uom || "Nos";
							row.stock_uom = it.stock_uom || "Nos";
							row.conversion_factor = 1.0;

							// Cost / valuation rate takes precedence for purchasing documents
							const cost_rate = flt(it.valuation_rate != null && flt(it.valuation_rate) > 0 ? it.valuation_rate : (it.unit_rate != null ? it.unit_rate : it.rate)) || 0.0;

							if (frm.doc.doctype === "Material Request") {
								if (frm.doc.material_request_type === "Purchase") {
									row.rate = cost_rate;
									row.amount = row.qty * cost_rate;
								}
								row.warehouse = default_wh;
								row.schedule_date = schedule_date;
							} else if (frm.doc.doctype === "Purchase Order") {
								row.rate = cost_rate;
								row.amount = row.qty * cost_rate;
								row.warehouse = default_wh;
								row.schedule_date = schedule_date;
							} else if (frm.doc.doctype === "Purchase Receipt") {
								row.rate = cost_rate;
								row.amount = row.qty * cost_rate;
								row.warehouse = default_wh;
							} else if (frm.doc.doctype === "Purchase Invoice") {
								row.rate = cost_rate;
								row.amount = row.qty * cost_rate;
								if (frm.doc.update_stock) {
									row.warehouse = default_wh;
								}
							} else if (frm.doc.doctype === "Supplier Quotation") {
								row.rate = cost_rate;
								row.amount = row.qty * cost_rate;
							}
						});

						frm.refresh_field("items");

						// Recalculate taxes and totals if standard ERPNext calculator is present
						if (frm.cscript && typeof frm.cscript.calculate_taxes_and_totals === "function") {
							frm.cscript.calculate_taxes_and_totals();
						}

						frm.dirty();
						frappe.show_alert({
							message: __("Added {0} part(s) to {1}", [created_items.length, frm.doc.doctype]),
							indicator: "green",
						});
					});
				},
			});
		};

		if (window.ism && ism.parts && ism.parts.show_quick_importer) {
			launch();
		} else {
			frappe.require("/assets/ism/js/parts_manager.js", launch);
		}
	}

	// Register on standard ERPNext purchasing DocTypes
	const purchasing_doctypes = [
		"Material Request",
		"Purchase Order",
		"Purchase Receipt",
		"Purchase Invoice",
		"Supplier Quotation",
	];

	purchasing_doctypes.forEach((dt) => {
		frappe.ui.form.on(dt, {
			refresh(frm) {
				add_purchasing_parts_utility(frm);
			},
		});
	});

	if (
		window.cur_frm &&
		purchasing_doctypes.includes(cur_frm.doctype) &&
		cur_frm.fields_dict &&
		cur_frm.fields_dict.items &&
		cur_frm.fields_dict.items.grid &&
		cur_frm.fields_dict.items.grid.grid_buttons
	) {
		add_purchasing_parts_utility(cur_frm);
	}
})();
