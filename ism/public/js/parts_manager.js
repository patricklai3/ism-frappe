// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.provide("ism.parts");

/**
 * Format Item Code strictly according to standard [Source]-[PartNumber]-[Condition]
 */
ism.parts.format_item_code = function (source, part_number, condition) {
	const src = (source || "OEM").toUpperCase().startsWith("OEM") ? "OEM" : "AFT";
	const cond_str = (condition || "New").toUpperCase().trim();
	let cond = "NEW";
	if (cond_str.includes("USE")) {
		cond = "USED";
	} else if (cond_str.includes("REC") || cond_str.includes("RECON") || cond_str.includes("RCN") || cond_str.includes("REB")) {
		cond = "RCN";
	}

	const cleaned = (part_number || "").toUpperCase().replace(/[^A-Z0-9_\-]/g, "").trim();
	return `${src}-${cleaned || "PART"}-${cond}`;
};

/**
 * Render compact colored badge for vehicle model (Model 3, Model Y, etc.)
 */
ism.parts.get_model_badge = function (model, extra_class = "") {
	if (!model) return "";
	const m = String(model).trim();
	if (!m) return "";
	let bg = "#f1f5f9", color = "#334155", border = "#cbd5e1";
	if (m.includes("Model 3")) {
		bg = "#eff6ff"; color = "#1d4ed8"; border = "#bfdbfe";
	} else if (m.includes("Model Y")) {
		bg = "#ecfdf5"; color = "#047857"; border = "#a7f3d0";
	} else if (m.includes("Model S")) {
		bg = "#fef2f2"; color = "#b91c1c"; border = "#fecaca";
	} else if (m.includes("Model X")) {
		bg = "#f5f3ff"; color = "#6d28d9"; border = "#ddd6fe";
	} else if (m.includes("Cybertruck")) {
		bg = "#f8fafc"; color = "#0f172a"; border = "#94a3b8";
	}
	return `<span class="badge ${extra_class}" style="background-color: ${bg}; color: ${color}; border: 1px solid ${border}; font-size: 11px; padding: 2px 7px; border-radius: 4px; font-weight: 600;"><i class="fa fa-car mr-1" style="opacity: 0.7;"></i>${frappe.utils.escape_html(m)}</span>`;
};

/**
 * Parse integer slot number from string or int (e.g. 1, "Slot 1", "Unassigned" -> 0)
 */
ism.parts.parse_slot_number = function (val) {
	if (val == null || val === "") return 0;
	const s = String(val).trim();
	if (["unassigned", "none", "0", ""].includes(s.toLowerCase())) return 0;
	const match = s.match(/slot\s*(\d+)/i);
	if (match) return parseInt(match[1], 10);
	const num = parseInt(s, 10);
	return (!isNaN(num) && num > 0) ? num : 0;
};

/**
 * Format slot display label ("Slot 1", "Slot 2", "Unassigned")
 */
ism.parts.format_slot_label = function (slot_number) {
	const num = parseInt(slot_number, 10);
	if (!num || isNaN(num) || num <= 0) return __("Unassigned");
	return __("Slot {0}", [num]);
};

/**
 * Open Multi-Mode Quick Creator, Catalog Picker & Bulk Import Dialog
 * Enforces linked Service Packages, visual numbered slots with live previews, and multi-service compatibility.
 */
ism.parts.show_quick_importer = function (opts = {}) {
	const {
		service_package = null,
		service_packages = [],
		existing_services = [],
		default_model = "",
		slot_number = null,
		component_slot = null,
		default_part_number = "",
		default_description = "",
		side = "Both",
		default_mode = "single",
		callback = null,
	} = opts;

	let current_mode = default_mode; // "single" | "catalog" | "bulk"

	let selected_packages = [];
	let package_models = {};
	if (service_package) {
		selected_packages.push(service_package);
	}
	if (Array.isArray(service_packages)) {
		service_packages.forEach((pkg) => {
			if (pkg && !selected_packages.includes(pkg)) {
				selected_packages.push(pkg);
			}
		});
	}

	let selected_slot_number = (slot_number != null && !isNaN(parseInt(slot_number, 10)))
		? parseInt(slot_number, 10)
		: (component_slot ? ism.parts.parse_slot_number(component_slot) : null);
	let package_slots_data = { slots: [], unassigned: [], next_slot_number: 1 };

	let d = new frappe.ui.Dialog({
		title: __("Add / Import Part"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "mode_switcher_html",
			},
			{
				fieldtype: "HTML",
				fieldname: "sec_services_html",
			},
			{
				fieldtype: "HTML",
				fieldname: "sec_slots_html",
			},

			// ================== MODE 1: SINGLE FAST CREATOR ==================
			{
				fieldtype: "Section Break",
				fieldname: "sec_single_info",
				label: __("Part Identification & Compliance"),
			},
			{
				fieldname: "part_number",
				fieldtype: "Data",
				label: __("Part Number"),
				reqd: 1,
				default: default_part_number,
				description: __("Supplier or OEM catalog part number"),
				onchange() {
					update_preview();
				},
			},
			{
				fieldname: "part_name",
				fieldtype: "Data",
				label: __("Part Description / Name"),
				default: default_description,
			},
			{
				fieldtype: "Column Break",
			},
			{
				fieldname: "part_source",
				fieldtype: "Select",
				label: __("Source (Required by Law)"),
				options: ["OEM", "Aftermarket"],
				default: "OEM",
				reqd: 1,
				onchange() {
					const src = d.get_value("part_source");
					if (src === "OEM" && !d.get_value("brand")) {
						d.set_value("brand", "Tesla");
					}
					update_preview();
				},
			},
			{
				fieldname: "part_condition",
				fieldtype: "Select",
				label: __("Condition (Required by Law)"),
				options: ["New", "Used", "Reconditioned"],
				default: "New",
				reqd: 1,
				onchange() {
					update_preview();
				},
			},
			{
				fieldname: "revision",
				fieldtype: "Data",
				label: __("Revision"),
				placeholder: __("e.g. J, Rev B"),
			},
			{
				fieldname: "brand",
				fieldtype: "Data",
				label: __("Brand / Manufacturer"),
				default: "Tesla",
			},
			{
				fieldname: "warranty_period",
				fieldtype: "Data",
				label: __("Warranty Period (Required by Law)"),
				default: "12 Months / 20,000 km",
				reqd: 1,
			},

			{
				fieldtype: "Section Break",
				fieldname: "sec_single_pricing",
				label: __("Pricing & Quantities"),
			},
			{
				fieldname: "unit_rate",
				fieldtype: "Currency",
				label: __("Customer Selling Rate ($)"),
				default: 0.0,
			},
			{
				fieldname: "qty",
				fieldtype: "Float",
				label: __("Quantity"),
				default: 1.0,
			},
			{
				fieldtype: "Column Break",
			},
			{
				fieldname: "valuation_rate",
				fieldtype: "Currency",
				label: __("Unit Cost / Valuation Rate ($)"),
				default: 0.0,
			},

			{
				fieldtype: "Section Break",
				fieldname: "sec_single_preview",
			},
			{
				fieldtype: "HTML",
				fieldname: "preview_html",
			},

			// ================== MODE 2: FROM CATALOG ==================
			{
				fieldtype: "Section Break",
				fieldname: "sec_catalog",
				label: __("Choose Existing Catalog Item"),
			},
			{
				fieldname: "catalog_item_code",
				fieldtype: "Link",
				options: "Item",
				label: __("Select Item"),
				get_query: () => ({ filters: { is_stock_item: 1 } }),
				onchange() {
					const ic = d.get_value("catalog_item_code");
					if (ic) {
						frappe.db.get_value("Item", ic, [
							"item_name", "standard_rate", "valuation_rate", "part_source", "part_condition",
							"warranty_period", "oem_part_number", "stock_uom"
						], (r) => {
							if (r) {
								d.set_value("catalog_item_name", r.item_name || ic);
								d.set_value("catalog_rate", flt(r.standard_rate) || 0.0);
								d.set_value("catalog_source", r.part_source || "OEM");
								d.set_value("catalog_condition", r.part_condition || "New");
								d.set_value("catalog_warranty", r.warranty_period || "12 Months / 20,000 km");
								d.set_value("catalog_oem_num", r.oem_part_number || "");
							}
						});
					}
				},
			},
			{
				fieldname: "catalog_item_name",
				fieldtype: "Data",
				label: __("Item Description"),
			},
			{
				fieldtype: "Column Break",
			},
			{
				fieldname: "catalog_qty",
				fieldtype: "Float",
				label: __("Quantity"),
				default: 1.0,
			},
			{
				fieldname: "catalog_rate",
				fieldtype: "Currency",
				label: __("Unit Rate ($)"),
				default: 0.0,
			},
			{
				fieldtype: "Section Break",
				fieldname: "sec_catalog_details",
				label: __("Catalog Item Compliance"),
			},
			{
				fieldname: "catalog_source",
				fieldtype: "Select",
				label: __("Part Source"),
				options: ["OEM", "Aftermarket"],
				default: "OEM",
			},
			{
				fieldname: "catalog_condition",
				fieldtype: "Select",
				label: __("Condition"),
				options: ["New", "Used", "Reconditioned"],
				default: "New",
			},
			{
				fieldtype: "Column Break",
			},
			{
				fieldname: "catalog_warranty",
				fieldtype: "Data",
				label: __("Warranty"),
				default: "12 Months / 20,000 km",
			},
			{
				fieldname: "catalog_oem_num",
				fieldtype: "Data",
				label: __("OEM Reference #"),
			},

			// ================== MODE 3: BULK IMPORT ==================
			{
				fieldtype: "Section Break",
				fieldname: "sec_bulk",
				label: __("Bulk Paste Import"),
			},
			{
				fieldtype: "HTML",
				fieldname: "bulk_help",
				options: `
					<div class="alert alert-info" style="font-size: 12px; margin-bottom: 12px;">
						<b>${__("Paste Lines from Spreadsheets or Quotes:")}</b><br>
						${__("Format:")} <code>Part# | Description | Source (OEM/AFT) | Condition (New/Used/RCN) | Rate | Cost | Qty</code><br>
						<i>${__("Supports Tab-separated (Excel / Google Sheets), Pipe (|), or Comma (CSV). Header row is automatically detected and skipped.")}</i>
					</div>
				`,
			},
			{
				fieldname: "bulk_text",
				fieldtype: "Small Text",
				label: __("Paste Data"),
				placeholder: "1044321-00-J\tFront Upper Control Arm LH\tOEM\tNew\t185.00\t110.00\t1\n1044326-00-J\tFront Upper Control Arm RH\tOEM\tNew\t185.00\t110.00\t1",
			},
		],
		primary_action_label: __("Create & Attach Part"),
		primary_action() {
			// Enforce service selection before allowing part import
			if (!selected_packages || selected_packages.length === 0) {
				frappe.msgprint({
					title: __("Service Selection Required"),
					indicator: "orange",
					message: __("Please select or create at least one Service Package to link this part to before importing."),
				});
				return;
			}

			const target_slot = selected_slot_number != null ? selected_slot_number : 1;
			const target_slot_label = ism.parts.format_slot_label(target_slot);

			if (current_mode === "bulk") {
				// Bulk import mode
				const text = d.get_value("bulk_text");
				if (!text || !text.trim()) {
					frappe.msgprint(__("Please paste data to import."));
					return;
				}

				frappe.call({
					method: "ism.api.parts.bulk_import_parts",
					args: {
						raw_text: text,
						service_packages: selected_packages,
						service_package: selected_packages[0],
						slot_number: target_slot,
						component_slot: target_slot_label,
					},
					freeze: true,
					freeze_message: __("Importing and linking parts..."),
					callback(r) {
						if (r && r.message) {
							const res = r.message;
							(res.created || []).forEach(it => {
								it.service_packages = selected_packages;
								it.slot_number = it.slot_number != null ? it.slot_number : target_slot;
								it.component_slot = it.component_slot || target_slot_label;
							});
							if (res.errors && res.errors.length) {
								frappe.msgprint({
									title: __("Import Notes"),
									message: __("Created {0} items, but encountered {1} warnings.", [res.total_created, res.errors.length]),
									indicator: "orange",
								});
							} else {
								frappe.show_alert({
									message: __("Successfully imported {0} parts linked to {1} service(s).", [res.total_created, selected_packages.length]),
									indicator: "green",
								});
							}
							if (callback) {
								callback(res.created);
							}
							d.hide();
						}
					},
				});
			} else if (current_mode === "catalog") {
				// Existing catalog mode
				const ic = d.get_value("catalog_item_code");
				if (!ic) {
					frappe.msgprint(__("Please select an Item from the catalog."));
					return;
				}

				const qty = flt(d.get_value("catalog_qty")) || 1.0;
				const rate = flt(d.get_value("catalog_rate")) || 0.0;
				const iname = d.get_value("catalog_item_name") || ic;
				const source = d.get_value("catalog_source") || "OEM";
				const cond = d.get_value("catalog_condition") || "New";
				const warranty = d.get_value("catalog_warranty") || "12 Months / 20,000 km";
				const oem_num = d.get_value("catalog_oem_num") || "";

				const item_obj = {
					slot_number: target_slot,
					component_slot: target_slot_label,
					item_code: ic,
					item_name: iname,
					part_source: source,
					part_condition: cond,
					warranty_period: warranty,
					oem_part_number: oem_num,
					qty: qty,
					unit_rate: rate,
					rate: rate,
					amount: qty * rate,
					stock_uom: "Nos",
					service_packages: selected_packages,
				};

				// Link to all selected Service Packages
				frappe.call({
					method: "ism.api.parts.link_part_to_service_packages",
					args: {
						item_code: ic,
						service_packages: selected_packages,
						slot_number: target_slot,
						component_slot: target_slot_label,
						qty: qty,
						rate: rate,
						side: side,
					},
					freeze: true,
					freeze_message: __("Linking part to service packages..."),
					callback() {
						frappe.show_alert({
							message: __("Added Part {0} to {1} and linked to {2} service(s)", [ic, target_slot_label, selected_packages.length]),
							indicator: "green",
						});
						if (callback) {
							callback([item_obj]);
						}
						d.hide();
					},
				});
			} else {
				// Single part fast creator
				const pnum = d.get_value("part_number");
				if (!pnum || !pnum.trim()) {
					frappe.msgprint(__("Part Number is required."));
					return;
				}

				frappe.call({
					method: "ism.api.parts.quick_create_part",
					args: {
						part_number: pnum,
						part_name: d.get_value("part_name") || pnum,
						part_source: d.get_value("part_source"),
						part_condition: d.get_value("part_condition"),
						slot_number: target_slot,
						component_slot: target_slot_label,
						revision: d.get_value("revision"),
						brand: d.get_value("brand"),
						warranty_period: d.get_value("warranty_period"),
						unit_rate: d.get_value("unit_rate"),
						valuation_rate: d.get_value("valuation_rate"),
						service_packages: selected_packages,
						service_package: selected_packages[0],
						side: side,
					},
					freeze: true,
					freeze_message: __("Creating and linking part..."),
					callback(r) {
						if (r && r.message) {
							const item = r.message;
							item.qty = flt(d.get_value("qty")) || 1.0;
							item.amount = item.qty * (flt(item.unit_rate) || 0.0);
							item.service_packages = selected_packages;
							item.slot_number = item.slot_number != null ? item.slot_number : target_slot;
							item.component_slot = item.component_slot || target_slot_label;

							frappe.show_alert({
								message: __("Created Part {0} in {1} and linked to {2} service(s)", [item.item_code, target_slot_label, selected_packages.length]),
								indicator: "green",
							});

							if (callback) {
								callback([item]);
							}
							d.hide();
						}
					},
				});
			}
		},
	});

	function set_mode(mode) {
		current_mode = mode;
		const is_single = mode === "single";
		const is_catalog = mode === "catalog";
		const is_bulk = mode === "bulk";

		const update_btn = ($btn, active) => {
			if (active) {
				$btn.addClass("active").removeClass("text-muted")
					.css({ background: "#fff", border: "1px solid #cbd5e1", color: "#1e293b", boxShadow: "0 1px 2px rgba(0,0,0,0.06)" });
			} else {
				$btn.removeClass("active").addClass("text-muted")
					.css({ background: "transparent", border: "1px solid transparent", color: "#64748b", boxShadow: "none" });
			}
		};

		update_btn(d.$wrapper.find(".btn-mode-single"), is_single);
		update_btn(d.$wrapper.find(".btn-mode-catalog"), is_catalog);
		update_btn(d.$wrapper.find(".btn-mode-bulk"), is_bulk);

		// Single sections
		["sec_single_info", "sec_single_pricing", "sec_single_preview"].forEach((f) => {
			if (d.fields_dict[f] && d.fields_dict[f].wrapper) {
				d.fields_dict[f].wrapper.toggle(is_single);
			}
		});

		// Catalog sections
		["sec_catalog", "sec_catalog_details"].forEach((f) => {
			if (d.fields_dict[f] && d.fields_dict[f].wrapper) {
				d.fields_dict[f].wrapper.toggle(is_catalog);
			}
		});

		// Bulk sections
		if (d.fields_dict.sec_bulk && d.fields_dict.sec_bulk.wrapper) {
			d.fields_dict.sec_bulk.wrapper.toggle(is_bulk);
		}

		// Adjust reqd property
		if (d.fields_dict.part_number && d.fields_dict.part_number.df) {
			d.fields_dict.part_number.df.reqd = is_single ? 1 : 0;
			d.fields_dict.part_number.refresh && d.fields_dict.part_number.refresh();
		}
		if (d.fields_dict.catalog_item_code && d.fields_dict.catalog_item_code.df) {
			d.fields_dict.catalog_item_code.df.reqd = is_catalog ? 1 : 0;
			d.fields_dict.catalog_item_code.refresh && d.fields_dict.catalog_item_code.refresh();
		}

		const btn_label = is_single ? __("Create & Attach Part") : (is_catalog ? __("Add to Service") : __("Import & Attach Parts"));
		if (d.get_primary_btn) {
			d.get_primary_btn().html(btn_label);
		}
	}

	function update_preview() {
		if (!d.fields_dict.preview_html) return;
		const src = d.get_value("part_source") || "OEM";
		const pnum = d.get_value("part_number") || "";
		const cond = d.get_value("part_condition") || "New";
		const code = ism.parts.format_item_code(src, pnum, cond);

		const cond_color = cond === "New" ? "#15803d" : cond === "Used" ? "#b45309" : "#0e7490";
		const src_color = src === "OEM" ? "#1e40af" : "#7e22ce";

		const html = `
			<div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px 14px; margin-top: 5px;">
				<div style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: bold; margin-bottom: 4px;">
					${__("Automated Standard Item Code Preview:")}
				</div>
				<div style="display: flex; align-items: center; gap: 8px;">
					<code style="font-size: 14px; font-weight: bold; color: #0f172a; background: #f1f5f9; padding: 3px 8px; border-radius: 4px; border: 1px solid #e2e8f0;">
						${code}
					</code>
					<span class="badge" style="background: ${src_color}; color: #fff; font-size: 11px; padding: 3px 7px;">${src}</span>
					<span class="badge" style="background: ${cond_color}; color: #fff; font-size: 11px; padding: 3px 7px;">${cond}</span>
				</div>
			</div>
		`;
		const $wrap = d.fields_dict.preview_html.$wrapper || $(d.fields_dict.preview_html.wrapper);
		if ($wrap && $wrap.length) {
			$wrap.html(html);
		}
	}

	function load_package_slots() {
		const primary_pkg = selected_packages[0];
		if (!primary_pkg) {
			package_slots_data = { slots: [], unassigned: [], next_slot_number: 1 };
			render_slots_section();
			return;
		}
		frappe.call({
			method: "ism.api.parts.get_service_package_slots",
			args: { service_package: primary_pkg },
			callback(r) {
				if (r && r.message) {
					package_slots_data = r.message;
					if (selected_slot_number == null) {
						if (package_slots_data.slots && package_slots_data.slots.length > 0) {
							selected_slot_number = package_slots_data.slots[0].slot_number;
						} else {
							selected_slot_number = 1;
						}
					}
					render_slots_section();
				}
			}
		});
	}

	function render_slots_section() {
		const $wrap = d.fields_dict.sec_slots_html.$wrapper || $(d.fields_dict.sec_slots_html.wrapper);
		if (!$wrap || !$wrap.length) return;

		const primary_pkg = selected_packages[0];
		if (!primary_pkg) {
			$wrap.html("");
			return;
		}

		const next_num = package_slots_data.next_slot_number || 1;
		let slot_cards_html = "";

		(package_slots_data.slots || []).forEach((slot) => {
			const is_sel = selected_slot_number === slot.slot_number;
			let parts_html = "";
			(slot.parts || []).slice(0, 3).forEach((p) => {
				const src = (p.part_source || "OEM").toUpperCase().startsWith("OEM") ? "OEM" : "AFT";
				const cond = p.part_condition || "New";
				parts_html += `
					<div class="text-truncate mt-1" style="font-size: 11px; line-height: 1.3;" title="${frappe.utils.escape_html(p.item_name || p.item_code)}">
						<span class="badge ${src === 'OEM' ? 'badge-primary' : 'badge-dark'}" style="font-size: 9px; padding: 1px 3px;">${src}</span>
						<span class="badge badge-light border" style="font-size: 9px; padding: 1px 3px;">${cond}</span>
						<span style="font-weight: 500; color: #334155;">${frappe.utils.escape_html(p.item_code)}</span>
					</div>
				`;
			});
			if ((slot.parts || []).length > 3) {
				parts_html += `<div class="text-muted mt-1" style="font-size: 10px;">+ ${slot.parts.length - 3} more variant(s)...</div>`;
			}
			if ((slot.parts || []).length === 0) {
				parts_html = `<div class="text-muted mt-1" style="font-size: 11px; font-style: italic;">${__("Empty slot")}</div>`;
			}

			slot_cards_html += `
				<div class="slot-card p-2 mr-2 mb-2 ${is_sel ? 'slot-selected' : ''}" data-slot="${slot.slot_number}" style="border: 2px solid ${is_sel ? '#2563eb' : '#e2e8f0'}; background: ${is_sel ? '#eff6ff' : '#fff'}; border-radius: 8px; cursor: pointer; min-width: 170px; flex: 1 1 180px; max-width: 250px; transition: all 0.15s ease;">
					<div class="d-flex justify-content-between align-items-center mb-1">
						<b style="font-size: 13px; color: ${is_sel ? '#1d4ed8' : '#1e293b'};">
							${is_sel ? '<i class="fa fa-check-circle text-primary mr-1"></i>' : '<i class="fa fa-cubes text-muted mr-1"></i>'}
							Slot ${slot.slot_number}
						</b>
						<span class="badge badge-secondary" style="font-size: 10px;">${slot.parts.length}</span>
					</div>
					<div class="slot-parts-list">
						${parts_html}
					</div>
				</div>
			`;
		});

		// New Slot card
		const is_new_sel = selected_slot_number === next_num;
		slot_cards_html += `
			<div class="slot-card p-2 mr-2 mb-2 ${is_new_sel ? 'slot-selected' : ''}" data-slot="${next_num}" style="border: 2px dashed ${is_new_sel ? '#2563eb' : '#94a3b8'}; background: ${is_new_sel ? '#eff6ff' : '#f8fafc'}; border-radius: 8px; cursor: pointer; min-width: 140px; flex: 0 1 160px; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; min-height: 75px; transition: all 0.15s ease;">
				<div style="font-weight: 600; font-size: 12px; color: ${is_new_sel ? '#1d4ed8' : '#334155'};">
					<i class="fa fa-plus-circle text-primary mr-1"></i> ${__("New Slot {0}", [next_num])}
				</div>
				<div style="font-size: 10px; color: #64748b; margin-top: 2px;">${__("Create next slot")}</div>
			</div>
		`;

		// Unassigned card
		const is_unassigned_sel = selected_slot_number === 0;
		const unassigned_count = (package_slots_data.unassigned || []).length;
		slot_cards_html += `
			<div class="slot-card p-2 mb-2 ${is_unassigned_sel ? 'slot-selected' : ''}" data-slot="0" style="border: 2px solid ${is_unassigned_sel ? '#f59e0b' : '#e2e8f0'}; background: ${is_unassigned_sel ? '#fffbeb' : '#fff'}; border-radius: 8px; cursor: pointer; min-width: 140px; flex: 0 1 160px; transition: all 0.15s ease;">
				<div class="d-flex justify-content-between align-items-center mb-1">
					<b style="font-size: 12px; color: ${is_unassigned_sel ? '#b45309' : '#64748b'};">
						${is_unassigned_sel ? '<i class="fa fa-check-circle text-warning mr-1"></i>' : '<i class="fa fa-question-circle text-muted mr-1"></i>'}
						${__("Unassigned")}
					</b>
					<span class="badge badge-warning" style="font-size: 10px;">${unassigned_count}</span>
				</div>
				<div style="font-size: 10px; color: #64748b; margin-top: 2px;">${__("Assign later in RO")}</div>
			</div>
		`;

		const sec_html = `
			<div class="slots-box p-3 mb-3" style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px;">
				<div class="d-flex justify-content-between align-items-center mb-1">
					<label class="control-label mb-0 font-weight-bold" style="font-size: 13px; color: #0f172a;">
						<i class="fa fa-th-large mr-1 text-primary"></i> ${__("Component Slot:")}
						<span class="text-primary font-weight-bold ml-1">${selected_slot_number > 0 ? __('Slot {0}', [selected_slot_number]) : __('Unassigned')}</span>
					</label>
					<span class="text-muted" style="font-size: 11px;">
						${__("Primary:")} <b>${frappe.utils.escape_html(primary_pkg)}</b>
					</span>
				</div>
				<div class="text-muted mb-2" style="font-size: 11px;">
					${__("Select which numbered slot this part belongs to. Existing parts in each slot are previewed below.")}
					${selected_packages.length > 1 ? `<span class="text-info font-weight-bold ml-1">(${__("Secondary services will store it as Unassigned until used in a repair order")})</span>` : ''}
				</div>
				<div class="slots-container d-flex flex-wrap align-items-stretch" style="gap: 6px;">
					${slot_cards_html}
				</div>
			</div>
		`;

		$wrap.html(sec_html);

		$wrap.find(".slot-card").on("click", function(e) {
			e.preventDefault();
			selected_slot_number = parseInt($(this).attr("data-slot"), 10);
			render_slots_section();
		});
	}

	const render_services_section = () => {
		const $sec = d.fields_dict.sec_services_html.$wrapper || $(d.fields_dict.sec_services_html.wrapper);
		if (!$sec || !$sec.length) return;

		let pills_html = "";
		selected_packages.forEach((pkg_name) => {
			const model = package_models[pkg_name] || "";
			const model_badge = model ? ism.parts.get_model_badge(model, "mr-1") : "";
			pills_html += `
				<span class="badge badge-primary py-1 px-2 d-inline-flex align-items-center mr-1 mb-1" style="font-size: 12px; font-weight: 500; border-radius: 6px; background-color: #2563eb; color: #fff;">
					${model_badge}
					<i class="fa fa-check-circle mr-1 text-white"></i>
					<span>${frappe.utils.escape_html(pkg_name)}</span>
					<button type="button" class="btn btn-link text-white p-0 ml-2 btn-remove-service" data-name="${frappe.utils.escape_html(pkg_name)}" style="line-height: 1; text-decoration: none; font-size: 14px; opacity: 0.85;">&times;</button>
				</span>
			`;
		});

		if (selected_packages.length === 0) {
			pills_html = `<span class="text-danger" style="font-size: 12px; font-style: italic;"><i class="fa fa-exclamation-triangle mr-1"></i> ${__("No service linked yet. Search and select at least one compatible service package below.")}</span>`;
		}

		let quick_add_html = "";
		const suggestions = [];
		if (opts.existing_services && Array.isArray(opts.existing_services)) {
			opts.existing_services.forEach(s => {
				if (s && !selected_packages.includes(s) && !suggestions.includes(s)) suggestions.push(s);
			});
		}

		if (suggestions.length > 0) {
			quick_add_html = `
				<div class="quick-suggested-services mt-2 d-flex flex-wrap align-items-center" style="font-size: 11px;">
					<span class="text-muted mr-1 font-weight-bold">${__("From this Order:")}</span>
					${suggestions.map(s => `
						<button type="button" class="btn btn-xs btn-outline-secondary btn-quick-add-service mr-1 mb-1" data-name="${frappe.utils.escape_html(s)}" style="border-radius: 4px;">
							+ ${frappe.utils.escape_html(s)}
						</button>
					`).join("")}
				</div>
			`;
		}

		const section_html = `
			<div class="linked-services-box p-3 mb-3" style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px;">
				<div class="d-flex justify-content-between align-items-center mb-2">
					<label class="control-label mb-0 font-weight-bold" style="font-size: 13px; color: #0f172a;">
						<i class="fa fa-wrench mr-1 text-primary"></i> ${__("Compatible Services & Packages")} <span class="text-danger">*</span>
					</label>
					<button type="button" class="btn btn-xs btn-outline-primary btn-quick-create-service" style="font-weight: 600;">
						<i class="fa fa-plus mr-1"></i> ${__("New Service Package")}
					</button>
				</div>
				<div class="text-muted mb-2" style="font-size: 11px; line-height: 1.4;">
					${__("Select all services compatible with this part (e.g. Model 3 LH/RH/Pair, Model Y LH/RH/Pair). Parts are permanently linked to all selected services for future repair orders.")}
				</div>
				<div class="selected-services-container d-flex flex-wrap align-items-center mb-2" style="min-height: 28px;">
					${pills_html}
				</div>
				<div class="position-relative">
					<input type="text" class="form-control form-control-sm service-search-input" placeholder="${__("Type to search and add compatible services (e.g. Control Arm, Brake, Model 3, Model Y)...")}" autocomplete="off" />
					<div class="service-search-results list-group position-absolute w-100 shadow-sm" style="display: none; z-index: 1050; max-height: 220px; overflow-y: auto; top: 100%; left: 0;"></div>
				</div>
				${quick_add_html}
			</div>
		`;

		$sec.html(section_html);

		// Event handlers
		$sec.find(".btn-remove-service").on("click", function(e) {
			e.preventDefault();
			const name = $(this).attr("data-name");
			selected_packages = selected_packages.filter(p => p !== name);
			render_services_section();
			load_package_slots();
		});

		$sec.find(".btn-quick-add-service").on("click", function(e) {
			e.preventDefault();
			const name = $(this).attr("data-name");
			if (name && !selected_packages.includes(name)) {
				selected_packages.push(name);
				render_services_section();
				load_package_slots();
			}
		});

		$sec.find(".btn-quick-create-service").on("click", function(e) {
			e.preventDefault();
			frappe.prompt([
				{
					fieldname: "package_name",
					fieldtype: "Data",
					label: __("Service Package Title"),
					reqd: 1,
					default: d.get_value("part_name") || "",
					description: __("e.g. Front Upper Control Arm - Pair (Remove and Replace)"),
				},
				{
					fieldname: "applicable_model",
					fieldtype: "Select",
					label: __("Applicable Vehicle Model"),
					options: ["", "Model 3", "Model Y", "Model S", "Model X", "Cybertruck", "Roadster", "All Models"],
					default: default_model || "",
				},
				{
					fieldname: "correction_code",
					fieldtype: "Data",
					label: __("Correction Code (Optional)"),
				},
				{
					fieldname: "standard_labor_hours",
					fieldtype: "Float",
					label: __("Flat Rate Hours (FRT)"),
					default: 1.0,
				},
				{
					fieldname: "hourly_rate",
					fieldtype: "Currency",
					label: __("Hourly Rate ($)"),
					default: 120.0,
				},
			], (vals) => {
				frappe.call({
					method: "ism.api.parts.quick_create_service_package",
					args: vals,
					callback(r) {
						if (r && r.message && r.message.name) {
							const new_name = r.message.name;
							if (!selected_packages.includes(new_name)) {
								selected_packages.push(new_name);
							}
							if (r.message.applicable_model) {
								package_models[new_name] = r.message.applicable_model;
							}
							render_services_section();
							load_package_slots();
							frappe.show_alert({
								message: __("Created & linked Service Package: {0}", [new_name]),
								indicator: "green",
							});
						}
					}
				});
			}, __("Quick Create Service Package"), __("Create & Link"));
		});

		const $input = $sec.find(".service-search-input");
		const $results = $sec.find(".service-search-results");

		let search_timer = null;
		$input.on("input", function() {
			const q = $(this).val().trim();
			clearTimeout(search_timer);
			if (q.length < 1) {
				$results.hide().empty();
				return;
			}
			search_timer = setTimeout(() => {
				frappe.call({
					method: "ism.api.parts.search_service_packages",
					args: { query: q, limit: 15 },
					callback(r) {
						const items = (r && r.message) || [];
						if (items.length === 0) {
							$results.html(`<div class="list-group-item list-group-item-action text-muted" style="font-size: 12px;"><i>${__("No matching service packages found. Use '+ New Service Package' above to create it.")}</i></div>`).show();
							return;
						}
						let res_html = "";
						items.forEach((it) => {
							const is_already = selected_packages.includes(it.name);
							const code_badge = it.correction_code ? `<span class="badge badge-light border ml-1 font-monospace" style="font-size: 10px;">${frappe.utils.escape_html(it.correction_code)}</span>` : "";
							const model_badge = ism.parts.get_model_badge(it.applicable_model, "mr-1");
							res_html += `
								<a href="#" class="list-group-item list-group-item-action py-2 px-3 service-search-item ${is_already ? 'disabled text-muted' : ''}" data-name="${frappe.utils.escape_html(it.name)}" data-model="${frappe.utils.escape_html(it.applicable_model || '')}" style="font-size: 12px; display: block; text-decoration: none;">
									<div class="d-flex justify-content-between align-items-center">
										<div class="d-flex align-items-center flex-wrap">
											${model_badge}
											<b class="mr-1">${frappe.utils.escape_html(it.package_name || it.name)}</b> ${code_badge}
										</div>
										<span class="badge ${is_already ? 'badge-secondary' : 'badge-primary'}">${is_already ? __('Added') : __('+ Select')}</span>
									</div>
								</a>
							`;
						});
						$results.html(res_html).show();
					}
				});
			}, 200);
		});

		$sec.on("click", ".service-search-item:not(.disabled)", function(e) {
			e.preventDefault();
			const name = $(this).attr("data-name");
			const model = $(this).attr("data-model");
			if (name && !selected_packages.includes(name)) {
				selected_packages.push(name);
				if (model) {
					package_models[name] = model;
				}
			}
			$input.val("");
			$results.hide().empty();
			render_services_section();
			load_package_slots();
		});

		$(document).on("click.service_search", function(e) {
			if (!$(e.target).closest(".linked-services-box").length) {
				$results.hide();
			}
		});
	};

	d.show();
	render_services_section();
	load_package_slots();

	// Pre-fetch models for initially selected packages if missing
	if (selected_packages.length > 0) {
		const unpopulated = selected_packages.filter(p => !package_models[p]);
		if (unpopulated.length > 0) {
			frappe.db.get_list("Service Package", {
				filters: { name: ["in", unpopulated] },
				fields: ["name", "applicable_model"]
			}).then((records) => {
				if (records && records.length) {
					records.forEach(rec => {
						if (rec.applicable_model) {
							package_models[rec.name] = rec.applicable_model;
						}
					});
					render_services_section();
				}
			});
		}
	}

	const nav_html = `
		<div class="parts-importer-nav d-flex mb-3" style="background: #f1f5f9; padding: 4px; border-radius: 8px; border: 1px solid #e2e8f0; gap: 4px;">
			<button type="button" class="btn btn-sm btn-mode-single flex-fill active" style="border-radius: 6px; font-weight: 600; padding: 6px 10px; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,0.06); border: 1px solid #cbd5e1; color: #1e293b;">
				<i class="fa fa-bolt mr-1 text-primary"></i> ${__("Fast Creator")}
			</button>
			<button type="button" class="btn btn-sm btn-mode-catalog flex-fill text-muted" style="border-radius: 6px; font-weight: 600; padding: 6px 10px; background: transparent; border: 1px solid transparent; color: #64748b;">
				<i class="fa fa-search mr-1 text-success"></i> ${__("From Catalog")}
			</button>
			<button type="button" class="btn btn-sm btn-mode-bulk flex-fill text-muted" style="border-radius: 6px; font-weight: 600; padding: 6px 10px; background: transparent; border: 1px solid transparent; color: #64748b;">
				<i class="fa fa-table mr-1 text-info"></i> ${__("Bulk Import")}
			</button>
		</div>
	`;
	const $mode_wrapper = d.fields_dict.mode_switcher_html.$wrapper || $(d.fields_dict.mode_switcher_html.wrapper);
	if ($mode_wrapper && $mode_wrapper.length) {
		$mode_wrapper.html(nav_html);
	}

	d.$wrapper.find(".btn-mode-single").on("click", (e) => { e.preventDefault(); set_mode("single"); });
	d.$wrapper.find(".btn-mode-catalog").on("click", (e) => { e.preventDefault(); set_mode("catalog"); });
	d.$wrapper.find(".btn-mode-bulk").on("click", (e) => { e.preventDefault(); set_mode("bulk"); });

	set_mode(default_mode);
	update_preview();
	return d;
};

/**
 * Open Alternates / Variant Swap Picker Dialog
 */
ism.parts.show_alternates_picker = function (opts = {}) {
	const {
		service_package = null,
		slot_number = null,
		component_slot = null,
		item_code = null,
		oem_part_number = null,
		callback = null,
	} = opts;

	const target_slot_num = slot_number != null && !isNaN(parseInt(slot_number, 10))
		? parseInt(slot_number, 10)
		: (ism.parts.parse_slot_number(component_slot) || 1);
	const target_slot_label = ism.parts.format_slot_label(target_slot_num);

	frappe.call({
		method: "ism.api.parts.get_component_alternates",
		args: {
			service_package: service_package,
			slot_number: target_slot_num,
			component_slot: target_slot_label,
			item_code: item_code,
			oem_part_number: oem_part_number,
		},
		freeze: true,
		freeze_message: __("Loading compatible variants..."),
		callback(r) {
			const items = (r && r.message) || [];
			render_dialog(items);
		},
	});

	function render_dialog(items) {
		let rows_html = "";
		items.forEach((it, idx) => {
			const is_current = it.item_code === item_code;
			const is_unassigned = cint(it.is_unassigned) === 1;

			const stock_badge = it.stock_qty > 0
				? `<span class="badge badge-success" style="font-size: 11px;">${it.stock_qty} in stock</span>`
				: `<span class="badge badge-secondary" style="font-size: 11px;">0 in stock</span>`;

			const src_badge = (it.part_source || "OEM").toUpperCase().startsWith("OEM")
				? `<span class="badge" style="background: #1e40af; color: #fff; font-size: 10px;">OEM</span>`
				: `<span class="badge" style="background: #7e22ce; color: #fff; font-size: 10px;">AFT</span>`;

			const cond_badge = it.part_condition === "New"
				? `<span class="badge badge-success" style="font-size: 10px;">New</span>`
				: it.part_condition === "Used"
				? `<span class="badge badge-warning" style="font-size: 10px;">Used</span>`
				: `<span class="badge badge-info" style="font-size: 10px;">Recon</span>`;

			const slot_badge = is_unassigned
				? `<span class="badge badge-warning" style="font-size: 10px;"><i class="fa fa-question-circle mr-1"></i>${__("Unassigned")}</span>`
				: `<span class="badge badge-light border" style="font-size: 10px;">Slot ${it.slot_number || target_slot_num}</span>`;

			let action_btn = "";
			if (is_current) {
				action_btn = `<button class="btn btn-xs btn-default" disabled>${__("Active")}</button>`;
			} else if (is_unassigned) {
				action_btn = `
					<button class="btn btn-xs btn-warning btn-assign-and-use" data-item="${frappe.utils.escape_html(it.item_code)}" data-idx="${idx}" title="${__("Permanently assign to Slot {0} in Service Package and use", [target_slot_num])}">
						<i class="fa fa-link mr-1"></i> ${__("Assign & Use")}
					</button>
				`;
			} else {
				action_btn = `<button class="btn btn-xs btn-primary btn-select-alternate" data-idx="${idx}">${__("Select")}</button>`;
			}

			rows_html += `
				<tr style="${is_current ? 'background: #eff6ff;' : (is_unassigned ? 'background: #fffdf5;' : '')}">
					<td style="vertical-align: middle;">
						<div class="d-flex align-items-center flex-wrap">
							<b class="mr-1">${frappe.utils.escape_html(it.item_code)}</b>
							${slot_badge}
							${is_current ? '<span class="badge badge-info ml-1" style="font-size: 10px;">Current</span>' : ''}
						</div>
						<div style="font-size: 12px; color: #475569;">${frappe.utils.escape_html(it.item_name || it.item_code)}</div>
					</td>
					<td style="vertical-align: middle; text-align: center;">
						${src_badge} ${cond_badge}
					</td>
					<td style="vertical-align: middle; text-align: center;">
						${stock_badge}
					</td>
					<td style="vertical-align: middle; text-align: right; font-weight: bold;">
						$${flt(it.rate).toFixed(2)}
					</td>
					<td style="vertical-align: middle; text-align: center; width: 110px;">
						${action_btn}
					</td>
				</tr>
			`;
		});

		if (items.length === 0) {
			rows_html = `<tr><td colspan="5" class="text-center text-muted p-4"><i>${__("No compatible variants recorded yet. Use '+ Add New Variant' below.")}</i></td></tr>`;
		}

		const d = new frappe.ui.Dialog({
			title: __("Slot {0} Compatible Variants: {1}", [target_slot_num, service_package || "Service"]),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "alternates_html",
					options: `
						<div class="alert alert-light border mb-2 py-2 px-3 d-flex justify-content-between align-items-center" style="font-size: 12px;">
							<div>
								<i class="fa fa-info-circle text-primary mr-1"></i>
								${__("Viewing variants in <b>Slot {0}</b>. Unassigned parts linked to this service can be permanently assigned to this slot.", [target_slot_num])}
							</div>
						</div>
						<table class="table table-bordered table-hover" style="font-size: 13px; margin-bottom: 10px;">
							<thead>
								<tr style="background: #f8fafc;">
									<th>${__("Part Code & Description")}</th>
									<th style="text-align: center; width: 110px;">${__("Status")}</th>
									<th style="text-align: center; width: 100px;">${__("Inventory")}</th>
									<th style="text-align: right; width: 90px;">${__("Rate ($)")}</th>
									<th style="text-align: center; width: 110px;">${__("Action")}</th>
								</tr>
							</thead>
							<tbody>
								${rows_html}
							</tbody>
						</table>
					`,
				},
			],
			secondary_action_label: __("+ Add New Variant for Slot {0}", [target_slot_num]),
			secondary_action() {
				d.hide();
				ism.parts.show_quick_importer({
					service_package: service_package,
					slot_number: target_slot_num,
					component_slot: target_slot_label,
					default_description: "",
					callback: (created) => {
						if (created && created.length && callback) {
							callback(created[0]);
						}
					},
				});
			},
		});

		d.$wrapper.find(".btn-select-alternate").on("click", function () {
			const idx = parseInt($(this).attr("data-idx"), 10);
			const sel = items[idx];
			if (callback && sel) {
				sel.slot_number = target_slot_num;
				sel.component_slot = target_slot_label;
				callback(sel);
			}
			d.hide();
		});

		d.$wrapper.find(".btn-assign-and-use").on("click", function () {
			const idx = parseInt($(this).attr("data-idx"), 10);
			const sel = items[idx];
			if (!sel) return;

			frappe.call({
				method: "ism.api.parts.assign_part_to_slot",
				args: {
					service_package: service_package,
					item_code: sel.item_code,
					slot_number: target_slot_num,
				},
				freeze: true,
				freeze_message: __("Permanently assigning part to slot..."),
				callback(r) {
					frappe.show_alert({
						message: __("Permanently assigned {0} to Slot {1} in {2}", [sel.item_code, target_slot_num, service_package]),
						indicator: "green",
					});
					sel.slot_number = target_slot_num;
					sel.component_slot = target_slot_label;
					sel.is_unassigned = 0;
					if (callback) {
						callback(sel);
					}
					d.hide();
				}
			});
		});

		d.show();
	}
};
