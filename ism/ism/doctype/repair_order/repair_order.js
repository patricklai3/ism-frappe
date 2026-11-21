// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

const CLOSED_RO_STATUSES = ["Cancelled", "Closed / Released", "Billed / Paid"];
const WORKSHOP_RO_STATUSES = [
	"Authorized - In Progress",
	"Waiting on Parts",
	"Work Completed",
	...CLOSED_RO_STATUSES,
];
const BILLING_RO_STATUSES = ["Work Completed", ...CLOSED_RO_STATUSES];
const UNAPPROVED_ROW_STATUSES = ["", "Draft", "Estimating", "Pending Approval"];
const PAYMENT_METHODS = ["Debit / Interac", "Credit Card", "Cash", "E-Transfer", "Cheque"];
const SERVICE_CATEGORIES = [
	"Maintenance",
	"Diagnostics",
	"Brakes",
	"Suspension",
	"Drivetrain",
	"Battery / HV",
	"Body / Glass",
	"Tires / Alignment",
	"Other",
];

function ro_flt(val, precision = 2) {
	if (typeof flt === "function") return flt(val, precision);
	const num = Number(val) || 0;
	const factor = Math.pow(10, precision);
	return Math.round(num * factor) / factor;
}

function parse_service_parts(raw_parts) {
	if (Array.isArray(raw_parts)) return raw_parts.map(normalize_part_entry);
	if (typeof raw_parts === "string" && raw_parts.trim()) {
		try {
			const parsed = JSON.parse(raw_parts);
			if (Array.isArray(parsed)) return parsed.map(normalize_part_entry);
		} catch (e) {
			return [];
		}
	}
	return [];
}

function normalize_part_entry(part = {}) {
	const part_source = part.part_source || "OEM";
	const qty = ro_flt(part.qty != null ? part.qty : 1, 2);
	const rate = part_source === "Customer Supplied" ? 0 : ro_flt(part.rate || 0, 2);
	return {
		...part,
		item_code: part.item_code || "",
		item_name: part.item_name || "",
		part_source,
		part_condition: part.part_condition || "New",
		oem_part_number: part.oem_part_number || "",
		warranty_period: part.warranty_period || "12 Months / 20,000 km",
		qty,
		rate,
		amount: ro_flt(qty * rate, 2),
	};
}

function get_unapproved_service_rows(frm) {
	return (frm.doc.services || []).filter((row) => {
		const row_status = row.approval_status || row.status || "";
		return UNAPPROVED_ROW_STATUSES.includes(row_status);
	});
}

function recalculate_service_row_totals(frm, cdt, cdn, options = {}) {
	const row = locals[cdt]?.[cdn];
	if (!row) return;

	const parts = parse_service_parts(row.parts);
	const parts_total = ro_flt(
		parts.reduce((sum, p) => sum + ro_flt(p.amount, 2), 0),
		2
	);

	let labor_amount;
	if (options.use_flat_rate && row.is_flat_rate && options.service_rate != null) {
		labor_amount = ro_flt(options.service_rate, 2);
	} else {
		labor_amount = ro_flt(ro_flt(row.labor_hours, 2) * ro_flt(row.labor_rate, 2), 2);
	}

	const discount = ro_flt(row.discount_amount || 0, 2);
	const total_amount = ro_flt(Math.max(0, labor_amount + parts_total - discount), 2);

	frappe.model.set_value(cdt, cdn, "parts_total", parts_total);
	frappe.model.set_value(cdt, cdn, "labor_amount", labor_amount);
	frappe.model.set_value(cdt, cdn, "total_amount", total_amount);
}

function run_job_status_action(frm, args, success_message, indicator) {
	frappe.call({
		method: "ism.api.agent.update_job_status",
		args: {
			repair_order: frm.doc.name,
			...args,
		},
		freeze: true,
		callback: (r) => {
			if (r.message && r.message.success) {
				frappe.show_alert({ message: success_message, indicator });
				frm.reload_doc();
			}
		},
	});
}

function open_send_authorization_dialog(frm) {
	frappe.prompt(
		[
			{
				fieldname: "method",
				fieldtype: "Select",
				label: __("Medium"),
				options: "SMS\nEmail\nCopy Link Only",
				default: "SMS",
			},
			{
				fieldname: "recipient",
				fieldtype: "Data",
				label: __("Recipient (Mobile or Email)"),
				description: __("Leave blank to use customer's default contact info"),
			},
		],
		(values) => {
			frappe.call({
				method: "ism.api.agent.send_quote_for_approval",
				args: {
					repair_order: frm.doc.name,
					communication_medium: values.method.toLowerCase(),
					recipient: values.recipient || null,
				},
				freeze: true,
				freeze_message: __("Dispatching authorization request..."),
				callback: (r) => {
					if (r.message && r.message.success) {
						frappe.show_alert({
							message: __("Authorization link prepared!"),
							indicator: "green",
						});
						const approval_url = r.message.urls && r.message.urls.approval;
						if (approval_url) {
							frappe.utils.copy_to_clipboard(approval_url);
							frappe.msgprint(
								__("Link copied to clipboard:<br><a href='{0}' target='_blank'>{0}</a>", [
									frappe.utils.escape_html(approval_url),
								])
							);
						}
						frm.reload_doc();
					}
				},
			});
		},
		__("Request Customer Approval")
	);
}

function open_in_person_approval_dialog(frm) {
	const pending_rows = get_unapproved_service_rows(frm);
	if (!pending_rows.length) {
		frappe.msgprint(__("No pending or draft service rows require authorization."));
		return;
	}

	const table_rows = pending_rows.map((s) => ({
		row_name: s.name,
		service_title: s.service_title || s.service_package || s.name,
		amount: ro_flt(s.total_amount, 2),
		decision: "Approved",
	}));

	const d = new frappe.ui.Dialog({
		title: __("In-Person / Verbal Authorization"),
		size: "large",
		fields: [
			{
				fieldname: "channel",
				fieldtype: "Select",
				label: __("Authorization Channel"),
				options: "In-Person Signature\nPhone / Verbal",
				default: "In-Person Signature",
				reqd: 1,
			},
			{
				fieldname: "signer_name",
				fieldtype: "Data",
				label: __("Authorizing Person / Customer Name"),
				default: frm.doc.customer || "",
				reqd: 1,
			},
			{
				fieldname: "decisions",
				fieldtype: "Table",
				label: __("Service Decisions"),
				cannot_add_rows: true,
				cannot_delete_rows: true,
				data: table_rows,
				fields: [
					{
						fieldname: "row_name",
						fieldtype: "Data",
						label: __("Row ID"),
						read_only: 1,
						hidden: 1,
					},
					{
						fieldname: "service_title",
						fieldtype: "Data",
						label: __("Service"),
						read_only: 1,
						in_list_view: 1,
						columns: 5,
					},
					{
						fieldname: "amount",
						fieldtype: "Currency",
						label: __("Total"),
						read_only: 1,
						in_list_view: 1,
						columns: 2,
					},
					{
						fieldname: "decision",
						fieldtype: "Select",
						label: __("Decision"),
						options: "Approved\nDeclined",
						default: "Approved",
						in_list_view: 1,
						columns: 3,
					},
				],
			},
			{
				fieldname: "notes",
				fieldtype: "Small Text",
				label: __("Authorization / Verbal Notes"),
			},
		],
		primary_action_label: __("Record Authorization"),
		primary_action(values) {
			const approvals = {};
			(values.decisions || []).forEach((item) => {
				if (item.row_name) {
					approvals[item.row_name] = item.decision || "Approved";
				}
			});
			frm.call({
				method: "authorize_jobs",
				doc: frm.doc,
				args: {
					approvals,
					channel: values.channel,
					signer_name: values.signer_name,
					notes: values.notes || null,
				},
				freeze: true,
				freeze_message: __("Recording authorization..."),
				callback: (r) => {
					if (!r.exc) {
						d.hide();
						frappe.show_alert({
							message: __("Customer authorization recorded."),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
	});
	d.show();
}

function open_record_deposit_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Record Deposit"),
		fields: [
			{
				fieldname: "amount",
				fieldtype: "Currency",
				label: __("Deposit Amount"),
				reqd: 1,
			},
			{
				fieldname: "payment_method",
				fieldtype: "Select",
				label: __("Payment Method"),
				options: PAYMENT_METHODS.join("\n"),
				default: "Debit / Interac",
				reqd: 1,
			},
			{
				fieldname: "reference_no",
				fieldtype: "Data",
				label: __("Reference / Transaction #"),
			},
		],
		primary_action_label: __("Record Deposit"),
		primary_action(values) {
			if (ro_flt(values.amount, 2) <= 0) {
				frappe.msgprint(__("Deposit amount must be greater than 0."));
				return;
			}
			frm.call({
				method: "record_deposit",
				doc: frm.doc,
				args: {
					amount: ro_flt(values.amount, 2),
					payment_method: values.payment_method,
					reference_no: values.reference_no || null,
				},
				freeze: true,
				freeze_message: __("Recording deposit..."),
				callback: (r) => {
					if (!r.exc) {
						d.hide();
						frappe.show_alert({
							message: __("Deposit recorded."),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
	});
	d.show();
}

function open_checkout_release_dialog(frm) {
	const grand_total = ro_flt(frm.doc.grand_total || 0, 2);
	const paid_so_far = ro_flt(frm.doc.paid_amount || 0, 2);
	const deposit_amount = ro_flt(frm.doc.deposit_amount || 0, 2);
	const remaining = ro_flt(
		frm.doc.outstanding_amount != null
			? frm.doc.outstanding_amount
			: Math.max(0, grand_total - paid_so_far),
		2
	);

	const summary_html = `
		<div class="p-3 mb-3 rounded" style="background: var(--bg-light-gray); border: 1px solid var(--border-color);">
			<div class="row text-center">
				<div class="col-4">
					<div class="text-muted small">${__("Grand Total")}</div>
					<div class="font-weight-bold">${format_currency(grand_total, frm.doc.currency)}</div>
				</div>
				<div class="col-4">
					<div class="text-muted small">${__("Deposit / Paid")}</div>
					<div class="font-weight-bold text-success">${format_currency(Math.max(paid_so_far, deposit_amount), frm.doc.currency)}</div>
				</div>
				<div class="col-4">
					<div class="text-muted small">${__("Remaining Balance")}</div>
					<div class="font-weight-bold text-primary">${format_currency(remaining, frm.doc.currency)}</div>
				</div>
			</div>
		</div>
	`;

	const d = new frappe.ui.Dialog({
		title: __("Checkout & Release Vehicle"),
		size: "large",
		fields: [
			{
				fieldname: "summary",
				fieldtype: "HTML",
				options: summary_html,
			},
			{
				fieldname: "odometer_out",
				fieldtype: "Int",
				label: __("Final Odometer Out"),
				default: frm.doc.odometer_out || frm.doc.odometer_in || 0,
				reqd: 1,
			},
			{
				fieldname: "tender_mode",
				fieldtype: "Select",
				label: __("Tender Mode"),
				options: "Single Tender\nSplit Tenders",
				default: "Single Tender",
				reqd: 1,
			},
			{
				fieldname: "payment_method",
				fieldtype: "Select",
				label: __("Payment Method"),
				options: PAYMENT_METHODS.join("\n"),
				default: "Debit / Interac",
				depends_on: "eval:doc.tender_mode=='Single Tender'",
			},
			{
				fieldname: "paid_amount",
				fieldtype: "Currency",
				label: __("Amount Paid"),
				default: remaining,
				depends_on: "eval:doc.tender_mode=='Single Tender'",
			},
			{
				fieldname: "payments",
				fieldtype: "Table",
				label: __("Split Tender Payments"),
				depends_on: "eval:doc.tender_mode=='Split Tenders'",
				data: [
					{
						payment_method: "Debit / Interac",
						amount: remaining,
						reference_no: "",
					},
				],
				fields: [
					{
						fieldname: "payment_method",
						fieldtype: "Select",
						label: __("Method"),
						options: PAYMENT_METHODS.join("\n"),
						default: "Debit / Interac",
						in_list_view: 1,
						columns: 4,
					},
					{
						fieldname: "amount",
						fieldtype: "Currency",
						label: __("Amount"),
						in_list_view: 1,
						columns: 3,
					},
					{
						fieldname: "reference_no",
						fieldtype: "Data",
						label: __("Reference #"),
						in_list_view: 1,
						columns: 3,
					},
				],
			},
		],
		primary_action_label: __("Process Payment & Release"),
		primary_action(values) {
			const args = { odometer_out: values.odometer_out };
			if (values.tender_mode === "Split Tenders") {
				const payments = (values.payments || [])
					.filter((p) => ro_flt(p.amount, 2) > 0)
					.map((p) => ({
						payment_method: p.payment_method || "Debit / Interac",
						amount: ro_flt(p.amount, 2),
						reference_no: p.reference_no || "",
					}));
				if (!payments.length) {
					frappe.msgprint(__("Please enter at least one split payment row with an amount > 0."));
					return;
				}
				args.payments = payments;
				args.payment_method = payments[0].payment_method;
				args.paid_amount = ro_flt(
					payments.reduce((sum, p) => sum + p.amount, 0),
					2
				);
			} else {
				args.payment_method = values.payment_method || "Debit / Interac";
				args.paid_amount = ro_flt(values.paid_amount, 2);
			}

			frm.call({
				method: "process_payment_and_release",
				doc: frm.doc,
				args,
				freeze: true,
				freeze_message: __("Processing payment and releasing vehicle..."),
				callback: (r) => {
					if (!r.exc) {
						d.hide();
						frappe.show_alert({
							message: __("Payment processed and vehicle released."),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
	});
	d.show();
}

function open_add_supplement_dialog(frm) {
	const default_rate = frm.doc.services?.[0]?.labor_rate || 150;
	const d = new frappe.ui.Dialog({
		title: __("Add Supplemental Service"),
		fields: [
			{
				fieldname: "service_title",
				fieldtype: "Data",
				label: __("Service Title"),
				reqd: 1,
			},
			{
				fieldname: "category",
				fieldtype: "Select",
				label: __("Category"),
				options: SERVICE_CATEGORIES.join("\n"),
				default: "Maintenance",
			},
			{
				fieldname: "labor_hours",
				fieldtype: "Float",
				label: __("Labor Hours"),
				default: 1.0,
				reqd: 1,
			},
			{
				fieldname: "labor_rate",
				fieldtype: "Currency",
				label: __("Labor Rate"),
				default: default_rate,
			},
			{
				fieldname: "notes",
				fieldtype: "Small Text",
				label: __("Advisor / Technician Notes"),
			},
		],
		primary_action_label: __("Add Supplement"),
		primary_action(values) {
			frm.call({
				method: "add_supplement",
				doc: frm.doc,
				args: {
					service_title: values.service_title,
					category: values.category,
					labor_hours: ro_flt(values.labor_hours, 2),
					labor_rate: values.labor_rate != null ? ro_flt(values.labor_rate, 2) : null,
					notes: values.notes || null,
				},
				freeze: true,
				freeze_message: __("Adding supplement..."),
				callback: (r) => {
					if (!r.exc) {
						d.hide();
						frappe.show_alert({
							message: __("Supplement added."),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
	});
	d.show();
}

function open_duplicate_opposite_side_dialog(frm) {
	const services = frm.doc.services || [];
	if (!services.length) {
		frappe.msgprint(__("No services available to duplicate."));
		return;
	}

	const options = services.map((s, idx) => ({
		label: `${idx + 1}. ${s.service_title || s.service_package || s.name} (${s.service_position || "None"})`,
		value: s.name,
	}));

	const d = new frappe.ui.Dialog({
		title: __("Duplicate Service for Opposite Side"),
		fields: [
			{
				fieldname: "service_row_id",
				fieldtype: "Select",
				label: __("Source Service Row"),
				options,
				default: options[0].value,
				reqd: 1,
			},
		],
		primary_action_label: __("Duplicate"),
		primary_action(values) {
			frm.call({
				method: "duplicate_service_for_opposite_side",
				doc: frm.doc,
				args: { service_row_id: values.service_row_id },
				freeze: true,
				freeze_message: __("Duplicating service..."),
				callback: (r) => {
					if (!r.exc) {
						d.hide();
						frappe.show_alert({
							message: __("Opposite-side service created."),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
	});
	d.show();
}

function open_import_tesla_procedure_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Import Tesla Procedure"),
		size: "large",
		fields: [
			{
				fieldname: "url",
				fieldtype: "Data",
				label: __("Tesla Service Manual URL"),
				description: __("Paste a service.tesla.com procedure URL, or use offline HTML/Markdown below."),
			},
			{
				fieldname: "side",
				fieldtype: "Select",
				label: __("Side (if symmetrical)"),
				options: "\nLH\nRH",
				default: "",
			},
			{
				fieldname: "on_conflict",
				fieldtype: "Select",
				label: __("If Procedure Already Exists"),
				options: "skip\nupdate\nerror",
				default: "skip",
			},
			{
				fieldname: "offline_sec",
				fieldtype: "Section Break",
				label: __("Offline Paste Fallback (Optional)"),
				collapsible: 1,
			},
			{
				fieldname: "raw_html",
				fieldtype: "Code",
				label: __("Raw HTML"),
			},
			{
				fieldname: "raw_markdown",
				fieldtype: "Small Text",
				label: __("Raw Markdown"),
			},
		],
		primary_action_label: __("Import Procedure"),
		primary_action(values) {
			if (!values.url && !values.raw_html && !values.raw_markdown) {
				frappe.msgprint(__("Provide either a Tesla Service URL or offline Raw HTML / Raw Markdown."));
				return;
			}
			frappe.call({
				method: "ism.api.tesla.import_tesla_procedure",
				args: {
					repair_order: frm.doc.name,
					url: values.url || undefined,
					raw_html: values.raw_html || undefined,
					raw_markdown: values.raw_markdown || undefined,
					side: values.side || undefined,
					on_conflict: values.on_conflict || "skip",
				},
				freeze: true,
				freeze_message: __("Importing Tesla procedure..."),
				callback: (r) => {
					if (!r.exc && r.message) {
						d.hide();
						frappe.show_alert({
							message: r.message.message || __("Tesla procedure imported."),
							indicator: r.message.status === "skipped" ? "orange" : "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
	});
	d.show();
}

function open_apply_inspection_template_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Apply Inspection Template"),
		fields: [
			{
				fieldname: "template_name",
				fieldtype: "Link",
				options: "Inspection Template",
				label: __("Inspection Template"),
				default: frm.doc.inspection_template || "",
				reqd: 1,
			},
			{
				fieldname: "replace_existing",
				fieldtype: "Check",
				label: __("Replace Existing Inspection Items"),
				default: 0,
			},
		],
		primary_action_label: __("Apply Template"),
		primary_action(values) {
			frappe.call({
				method: "ism.api.agent.apply_inspection_template",
				args: {
					repair_order: frm.doc.name,
					template_name: values.template_name,
					replace_existing: values.replace_existing ? 1 : 0,
				},
				freeze: true,
				freeze_message: __("Applying inspection template..."),
				callback: (r) => {
					if (!r.exc && r.message) {
						d.hide();
						frappe.show_alert({
							message: __("Inspection template applied."),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
	});
	d.show();
}

function open_manage_parts_dialog(frm, initial_row_name = null) {
	const services = frm.doc.services || [];
	if (!services.length) {
		frappe.msgprint(__("Add at least one service row before managing parts."));
		return;
	}

	let active_row =
		services.find((s) => s.name === initial_row_name) || services[0];
	let parts_state = parse_service_parts(active_row.parts);
	let stock_map = {};
	let link_controls = [];

	const service_options = services.map((s, idx) => ({
		label: `${idx + 1}. ${s.service_title || s.service_package || s.name}`,
		value: s.name,
	}));

	const d = new frappe.ui.Dialog({
		title: __("Manage Service Parts"),
		size: "extra-large",
		fields: [
			{
				fieldname: "service_row_name",
				fieldtype: "Select",
				label: __("Service Row"),
				options: service_options,
				default: active_row.name,
				onchange() {
					const chosen = d.get_value("service_row_name");
					const next_row = services.find((s) => s.name === chosen);
					if (next_row && next_row.name !== active_row.name) {
						active_row = next_row;
						parts_state = parse_service_parts(active_row.parts);
						render_parts_table();
						fetch_live_stock();
					}
				},
			},
			{
				fieldname: "parts_container",
				fieldtype: "HTML",
			},
		],
		primary_action_label: __("Save Parts"),
		primary_action() {
			const normalized = parts_state
				.filter((p) => p.item_code || p.item_name)
				.map(normalize_part_entry);

			const cdt = active_row.doctype || "Repair Order Service";
			const cdn = active_row.name;
			frappe.model.set_value(cdt, cdn, "parts", JSON.stringify(normalized)).then(() => {
				recalculate_service_row_totals(frm, cdt, cdn);
				frm.refresh_field("services");
				frm.dirty();
				d.hide();
				frappe.show_alert({
					message: __("Service parts updated. Save Repair Order to persist."),
					indicator: "green",
				});
			});
		},
	});

	function get_stock_badge_html(part) {
		if (!part.item_code) {
			return `<span class="badge badge-secondary">${__("No Item")}</span>`;
		}
		const info = stock_map[part.item_code];
		if (!info) {
			return `<span class="badge badge-light">${__("Checking...")}</span>`;
		}
		const avail = ro_flt(info.available_qty, 2);
		const needed = ro_flt(part.qty, 2);
		let cls = "badge-success";
		if (avail <= 0) cls = "badge-danger";
		else if (avail < needed) cls = "badge-warning";
		return `<span class="badge ${cls}" title="Actual: ${info.actual_qty}, Committed: ${info.committed_qty}">${__("Avail")}: ${avail}</span>`;
	}

	function fetch_live_stock() {
		const item_codes = [...new Set(parts_state.map((p) => p.item_code).filter(Boolean))];
		if (!item_codes.length) {
			stock_map = {};
			update_stock_badges();
			return;
		}
		frappe.call({
			method: "ism.api.parts.get_repair_order_parts_stock",
			args: {
				ro_name: frm.is_new() ? undefined : frm.doc.name,
				items: JSON.stringify(item_codes),
				company: frm.doc.company || undefined,
			},
			callback: (r) => {
				stock_map = r.message || {};
				update_stock_badges();
			},
		});
	}

	function update_stock_badges() {
		const $wrap = d.fields_dict.parts_container.$wrapper;
		parts_state.forEach((part, idx) => {
			$wrap.find(`[data-stock-idx="${idx}"]`).html(get_stock_badge_html(part));
		});
	}

	function update_row_totals_dom(idx) {
		const part = parts_state[idx];
		const $wrap = d.fields_dict.parts_container.$wrapper;
		$wrap.find(`[data-amount-idx="${idx}"]`).text(format_currency(part.amount, frm.doc.currency));
		const total = parts_state.reduce((sum, p) => sum + ro_flt(p.amount, 2), 0);
		$wrap.find("[data-parts-grand-total]").text(format_currency(total, frm.doc.currency));
	}

	function open_alternates_picker(idx) {
		const part = parts_state[idx];
		frappe.call({
			method: "ism.api.parts.get_component_alternates",
			args: {
				service_package: active_row.service_package || undefined,
				component_slot: part.component_slot || undefined,
				slot_number: part.slot_number || undefined,
				oem_part_number: part.oem_part_number || undefined,
				item_code: part.item_code || undefined,
			},
			freeze: true,
			callback: (r) => {
				const alts = r.message || [];
				if (!alts.length) {
					frappe.msgprint(__("No compatible alternate parts found."));
					return;
				}
				const alt_dialog = new frappe.ui.Dialog({
					title: __("Select Alternate Part"),
					size: "large",
					fields: [{ fieldname: "alt_list", fieldtype: "HTML" }],
				});
				const rows_html = alts
					.map(
						(a, a_idx) => `
						<tr>
							<td><b>${frappe.utils.escape_html(a.item_code)}</b><br><small class="text-muted">${frappe.utils.escape_html(a.item_name || "")}</small></td>
							<td>${frappe.utils.escape_html(a.part_source || "OEM")} / ${frappe.utils.escape_html(a.part_condition || "New")}</td>
							<td class="text-right">${format_currency(ro_flt(a.rate, 2), frm.doc.currency)}</td>
							<td class="text-right">${ro_flt(a.stock_qty, 2)}</td>
							<td class="text-right">
								<button type="button" class="btn btn-xs btn-primary" data-pick-alt="${a_idx}">${__("Use")}</button>
							</td>
						</tr>`
					)
					.join("");
				alt_dialog.fields_dict.alt_list.$wrapper.html(`
					<table class="table table-bordered table-sm">
						<thead>
							<tr>
								<th>${__("Item")}</th>
								<th>${__("Source / Condition")}</th>
								<th class="text-right">${__("Rate")}</th>
								<th class="text-right">${__("Stock")}</th>
								<th></th>
							</tr>
						</thead>
						<tbody>${rows_html}</tbody>
					</table>
				`);
				alt_dialog.fields_dict.alt_list.$wrapper.on("click", "[data-pick-alt]", function () {
					const chosen = alts[Number($(this).attr("data-pick-alt"))];
					if (!chosen) return;
					const cur = parts_state[idx];
					cur.item_code = chosen.item_code;
					cur.item_name = chosen.item_name || chosen.item_code;
					cur.part_source = chosen.part_source || "OEM";
					cur.part_condition = chosen.part_condition || "New";
					cur.oem_part_number = chosen.oem_part_number || cur.oem_part_number || "";
					cur.warranty_period = chosen.warranty_period || cur.warranty_period || "12 Months / 20,000 km";
					cur.rate = cur.part_source === "Customer Supplied" ? 0 : ro_flt(chosen.rate || 0, 2);
					cur.amount = ro_flt(cur.qty * cur.rate, 2);
					alt_dialog.hide();
					render_parts_table();
					fetch_live_stock();
				});
				alt_dialog.show();
			},
		});
	}

	function render_parts_table() {
		link_controls = [];
		const $wrap = d.fields_dict.parts_container.$wrapper;
		const total = parts_state.reduce((sum, p) => sum + ro_flt(p.amount, 2), 0);

		const rows_html = parts_state.length
			? parts_state
					.map(
						(p, idx) => `
				<tr data-part-row="${idx}">
					<td style="min-width: 180px;"><div data-item-link="${idx}"></div></td>
					<td style="min-width: 160px;">
						<input type="text" class="form-control input-sm" data-field="item_name" data-idx="${idx}" value="${frappe.utils.escape_html(p.item_name || "")}" />
					</td>
					<td style="width: 145px;">
						<select class="form-control input-sm" data-field="part_source" data-idx="${idx}">
							<option value="OEM" ${p.part_source === "OEM" ? "selected" : ""}>OEM</option>
							<option value="Aftermarket" ${p.part_source === "Aftermarket" ? "selected" : ""}>Aftermarket</option>
							<option value="Customer Supplied" ${p.part_source === "Customer Supplied" ? "selected" : ""}>Customer Supplied</option>
						</select>
					</td>
					<td style="width: 80px;">
						<input type="number" min="0" step="1" class="form-control input-sm text-right" data-field="qty" data-idx="${idx}" value="${ro_flt(p.qty, 2)}" />
					</td>
					<td style="width: 105px;">
						<input type="number" min="0" step="0.01" class="form-control input-sm text-right" data-field="rate" data-idx="${idx}" value="${ro_flt(p.rate, 2)}" ${p.part_source === "Customer Supplied" ? "disabled" : ""} />
					</td>
					<td class="text-right align-middle" style="width: 100px;" data-amount-idx="${idx}">${format_currency(p.amount, frm.doc.currency)}</td>
					<td class="text-center align-middle" style="width: 100px;" data-stock-idx="${idx}">${get_stock_badge_html(p)}</td>
					<td class="text-right align-middle" style="white-space: nowrap;">
						<button type="button" class="btn btn-xs btn-default" data-alt-idx="${idx}" title="${__("Swap Alternate")}">⇄</button>
						<button type="button" class="btn btn-xs btn-danger" data-del-idx="${idx}" title="${__("Remove")}">×</button>
					</td>
				</tr>`
					)
					.join("")
			: `<tr><td colspan="8" class="text-center text-muted py-3">${__("No parts added for this service.")}</td></tr>`;

		$wrap.html(`
			<div class="table-responsive">
				<table class="table table-bordered table-sm mb-2">
					<thead>
						<tr>
							<th>${__("Item Code")}</th>
							<th>${__("Item Name")}</th>
							<th>${__("Source")}</th>
							<th class="text-right">${__("Qty")}</th>
							<th class="text-right">${__("Rate")}</th>
							<th class="text-right">${__("Amount")}</th>
							<th class="text-center">${__("FIFO Stock")}</th>
							<th></th>
						</tr>
					</thead>
					<tbody>${rows_html}</tbody>
					<tfoot>
						<tr>
							<td colspan="5" class="text-right font-weight-bold">${__("Parts Total")}</td>
							<td class="text-right font-weight-bold" data-parts-grand-total>${format_currency(total, frm.doc.currency)}</td>
							<td colspan="2"></td>
						</tr>
					</tfoot>
				</table>
			</div>
			<button type="button" class="btn btn-xs btn-secondary" data-add-part>+ ${__("Add Part")}</button>
		`);

		parts_state.forEach((p, idx) => {
			const $mount = $wrap.find(`[data-item-link="${idx}"]`);
			const ctrl = frappe.ui.form.make_control({
				df: {
					fieldtype: "Link",
					fieldname: `item_code_${idx}`,
					options: "Item",
					placeholder: __("Select Item"),
					onchange() {
						const val = ctrl.get_value();
						if (val === parts_state[idx]?.item_code) return;
						parts_state[idx].item_code = val || "";
						if (!val) {
							fetch_live_stock();
							return;
						}
						frappe.db.get_value(
							"Item",
							val,
							["item_name", "standard_rate", "part_source", "part_condition", "oem_part_number", "warranty_period"],
							(r) => {
								if (!r) return;
								const row_part = parts_state[idx];
								row_part.item_name = r.item_name || val;
								row_part.part_source = r.part_source || row_part.part_source || "OEM";
								row_part.part_condition = r.part_condition || "New";
								row_part.oem_part_number = r.oem_part_number || "";
								row_part.warranty_period = r.warranty_period || "12 Months / 20,000 km";
								row_part.rate = row_part.part_source === "Customer Supplied" ? 0 : ro_flt(r.standard_rate || 0, 2);
								row_part.amount = ro_flt(row_part.qty * row_part.rate, 2);
								render_parts_table();
								fetch_live_stock();
							}
						);
					},
				},
				parent: $mount,
				render_input: true,
			});
			ctrl.set_value(p.item_code || "");
			link_controls.push(ctrl);
		});

		$wrap.off("change input click");
		$wrap.on("input change", "[data-field]", function () {
			const idx = Number($(this).attr("data-idx"));
			const field = $(this).attr("data-field");
			const part = parts_state[idx];
			if (!part) return;

			if (field === "item_name") {
				part.item_name = $(this).val();
			} else if (field === "part_source") {
				part.part_source = $(this).val();
				const $rate_input = $wrap.find(`[data-field="rate"][data-idx="${idx}"]`);
				if (part.part_source === "Customer Supplied") {
					part.rate = 0;
					$rate_input.val(0).prop("disabled", true);
				} else {
					$rate_input.prop("disabled", false);
				}
				part.amount = ro_flt(part.qty * part.rate, 2);
				update_row_totals_dom(idx);
			} else if (field === "qty") {
				part.qty = Math.max(0, ro_flt($(this).val(), 2));
				part.amount = ro_flt(part.qty * part.rate, 2);
				update_row_totals_dom(idx);
				update_stock_badges();
			} else if (field === "rate") {
				part.rate = part.part_source === "Customer Supplied" ? 0 : Math.max(0, ro_flt($(this).val(), 2));
				part.amount = ro_flt(part.qty * part.rate, 2);
				update_row_totals_dom(idx);
			}
		});

		$wrap.on("click", "[data-alt-idx]", function () {
			open_alternates_picker(Number($(this).attr("data-alt-idx")));
		});

		$wrap.on("click", "[data-del-idx]", function () {
			parts_state.splice(Number($(this).attr("data-del-idx")), 1);
			render_parts_table();
			fetch_live_stock();
		});

		$wrap.on("click", "[data-add-part]", function () {
			parts_state.push(normalize_part_entry({ qty: 1, rate: 0, part_source: "OEM" }));
			render_parts_table();
		});
	}

	d.show();
	render_parts_table();
	fetch_live_stock();
}

frappe.ui.form.on("Repair Order", {
	setup(frm) {
		frm.set_query("vehicle", () => ({
			query: "ism.ism.doctype.repair_order.repair_order.get_filtered_vehicles",
			filters: frm.doc.customer ? { customer: frm.doc.customer } : {},
		}));

		frm.set_query("taxes_and_charges", () => {
			if (!frm.doc.company) return {};
			return { filters: { company: frm.doc.company } };
		});
	},

	refresh(frm) {
		frm.trigger("update_tab_visibility");
		frm.trigger("setup_fast_action_bar");
		frm.trigger("setup_navigation_buttons");
	},

	update_tab_visibility(frm) {
		if (!frm?.doc || !frm.layout?.tabs) return;

		const status = frm.doc.status;
		const is_approved = ["Approved", "Partially Approved"].includes(frm.doc.approval_status);
		const show_workshop = is_approved || WORKSHOP_RO_STATUSES.includes(status);
		const show_billing =
			BILLING_RO_STATUSES.includes(status) ||
			frm.doc.vehicle_location === "Completed / Parked in Lot" ||
			Boolean(frm.doc.vehicle_released);

		const toggle_tab = (fieldname, show) => {
			const tab = frm.layout.tabs.find((t) => t.df && t.df.fieldname === fieldname);
			if (tab) tab.toggle(show);
		};

		toggle_tab("tab_workshop", show_workshop);
		toggle_tab("tab_billing", show_billing);
	},

	setup_fast_action_bar(frm) {
		if (frm.is_new() || frm.doc.docstatus === 2) return;

		const status = frm.doc.status;
		if (CLOSED_RO_STATUSES.includes(status)) return;

		const is_approved = ["Approved", "Partially Approved"].includes(frm.doc.approval_status);
		const unapproved_rows = get_unapproved_service_rows(frm);
		const has_unapproved_services = unapproved_rows.length > 0;
		const has_services = (frm.doc.services || []).length > 0;

		// 1. Authorization Dispatch Action (Item 3: also shown for Partially Approved with unapproved supplements)
		if (status !== "Work Completed" && (!is_approved || has_unapproved_services)) {
			frm.add_custom_button(__("📲 Send for Customer Authorization"), () => {
				open_send_authorization_dialog(frm);
			}, __("Actions"));
		}

		if (has_unapproved_services) {
			frm.add_custom_button(__("✍ In-Person / Verbal Approval"), () => {
				open_in_person_approval_dialog(frm);
			}, __("Actions"));
		}

		if (has_services) {
			frm.add_custom_button(__("🔩 Manage Service Parts"), () => {
				open_manage_parts_dialog(frm);
			}, __("Actions"));

			frm.add_custom_button(__("⇄ Duplicate Opposite Side"), () => {
				open_duplicate_opposite_side_dialog(frm);
			}, __("Actions"));
		}

		frm.add_custom_button(__("➕ Add Supplement"), () => {
			open_add_supplement_dialog(frm);
		}, __("Actions"));

		frm.add_custom_button(__("⚡ Import Tesla Procedure"), () => {
			open_import_tesla_procedure_dialog(frm);
		}, __("Actions"));

		frm.add_custom_button(__("📋 Apply Inspection Template"), () => {
			open_apply_inspection_template_dialog(frm);
		}, __("Actions"));

		if (!frm.doc.vehicle_released) {
			frm.add_custom_button(__("❌ Cancel Repair Order"), () => {
				frappe.confirm(
					__("Cancel Repair Order {0}? This cannot be undone.", [frm.doc.name]),
					() => {
						frm.call({
							method: "cancel_repair_order",
							doc: frm.doc,
							freeze: true,
							freeze_message: __("Cancelling Repair Order..."),
							callback: (r) => {
								if (!r.exc) {
									frappe.show_alert({
										message: __("Repair Order cancelled."),
										indicator: "red",
									});
									frm.reload_doc();
								}
							},
						});
					}
				);
			}, __("Actions"));
		}

		// 2. Billing Actions
		if (!frm.doc.sales_invoice && frm.doc.payment_status !== "Paid") {
			frm.add_custom_button(__("💵 Record Deposit"), () => {
				open_record_deposit_dialog(frm);
			}, __("Billing"));
		}

		if (!frm.doc.sales_invoice && !CLOSED_RO_STATUSES.includes(status)) {
			if (frm.doc.deposit_request_status !== "Requested") {
				frm.add_custom_button(__("Request Online Deposit"), () => {
					const d = new frappe.ui.Dialog({
						title: __("Request Online Deposit"),
						fields: [
							{
								fieldname: "amount",
								fieldtype: "Currency",
								label: __("Deposit Amount (CAD)"),
								reqd: 1,
							},
							{
								fieldname: "note",
								fieldtype: "Data",
								label: __("Note (Optional)"),
							},
						],
						primary_action_label: __("Send Request"),
						primary_action(values) {
							if (ro_flt(values.amount, 2) <= 0) {
								frappe.msgprint(__("Deposit amount must be greater than 0."));
								return;
							}
							frappe.call({
								method: "ism.api.payments.request_ro_deposit",
								args: {
									ro_name: frm.doc.name,
									amount: ro_flt(values.amount, 2),
									note: values.note || null,
								},
								freeze: true,
								freeze_message: __("Requesting online deposit..."),
								callback: (r) => {
									if (!r.exc) {
										d.hide();
										frappe.show_alert({
											message: __("Online deposit requested."),
											indicator: "green",
										});
										frm.reload_doc();
									}
								},
							});
						},
					});
					d.show();
				}, __("Billing"));
			} else {
				frm.add_custom_button(__("Cancel Deposit Request"), () => {
					frappe.prompt(
						[
							{
								fieldname: "reason",
								fieldtype: "Data",
								label: __("Reason (Optional)"),
							},
						],
						(values) => {
							frappe.call({
								method: "ism.api.payments.cancel_ro_deposit_request",
								args: {
									ro_name: frm.doc.name,
									reason: values.reason || null,
								},
								freeze: true,
								freeze_message: __("Cancelling deposit request..."),
								callback: (r) => {
									if (!r.exc) {
										frappe.show_alert({
											message: __("Deposit request cancelled."),
											indicator: "orange",
										});
										frm.reload_doc();
									}
								},
							});
						},
						__("Cancel Deposit Request"),
						__("Cancel Request")
					);
				}, __("Billing"));
			}
		}

		if (status === "Work Completed" && !frm.doc.vehicle_released) {
			frm.add_custom_button(__("💳 Checkout & Release"), () => {
				open_checkout_release_dialog(frm);
			}).addClass("btn-primary font-weight-bold");
		}

		// 3. Workshop Bay & Technician 1-Tap Fast-Action Bar
		if (is_approved && status !== "Work Completed") {
			if (frm.doc.vehicle_location !== "In Bay / Being Serviced") {
				frm.add_custom_button(__("🚗 Pick Up Vehicle"), () => {
					frm.call({
						method: "pick_up_vehicle",
						doc: frm.doc,
						freeze: true,
						callback: (r) => {
							if (!r.exc) {
								frappe.show_alert({ message: __("Vehicle moved to bay."), indicator: "blue" });
								frm.reload_doc();
							}
						},
					});
				}, __("Workshop"));
			} else {
				frm.add_custom_button(__("🅿 Park Vehicle"), () => {
					frm.call({
						method: "park_vehicle",
						doc: frm.doc,
						freeze: true,
						callback: (r) => {
							if (!r.exc) {
								frappe.show_alert({ message: __("Vehicle parked in lot."), indicator: "green" });
								frm.reload_doc();
							}
						},
					});
				}, __("Workshop"));
			}

			if (status !== "Authorized - In Progress") {
				frm.add_custom_button(__("▶ Start Work"), () => {
					run_job_status_action(
						frm,
						{ action: "start" },
						__("Work Started! Status updated."),
						"blue"
					);
				}).addClass("btn-primary font-weight-bold");
			}

			if (status !== "Waiting on Parts") {
				frm.add_custom_button(__("⏸ Hold - Parts"), () => {
					frappe.prompt(
						[
							{
								fieldname: "reason",
								fieldtype: "Small Text",
								label: __("Hold Reason / Required Parts"),
								reqd: 1,
							},
						],
						(values) => {
							run_job_status_action(
								frm,
								{ action: "hold", notes: values.reason },
								__("Order placed on hold."),
								"orange"
							);
						},
						__("Put Job On Hold")
					);
				});
			}

			frm.add_custom_button(__("✔ Complete Work"), () => {
				frappe.prompt(
					[
						{
							fieldname: "odometer_out",
							fieldtype: "Int",
							label: __("Final Odometer Out"),
							default: frm.doc.odometer_in,
							reqd: 1,
						},
						{
							fieldname: "notes",
							fieldtype: "Small Text",
							label: __("Technician Completion Notes"),
						},
					],
					(values) => {
						run_job_status_action(
							frm,
							{
								action: "complete",
								odometer_out: values.odometer_out,
								notes: values.notes,
							},
							__("Repair Order marked complete!"),
							"green"
						);
					},
					__("Complete Work Order")
				);
			}).addClass("btn-success");
		}
	},

	setup_navigation_buttons(frm) {
		if (frm.is_new()) return;

		const encoded_name = encodeURIComponent(frm.doc.name);

		frm.add_custom_button(__("📄 Legal Print Preview"), () => {
			window.open(
				`/printview?doctype=Repair+Order&name=${encoded_name}&format=Automotive+Repair+Invoice`,
				"_blank"
			);
		}, __("Navigation"));

		frm.add_custom_button(__("🔧 Technician Job Card"), () => {
			window.open(
				`/printview?doctype=Repair+Order&name=${encoded_name}&format=Technician+Bay+Job+Card`,
				"_blank"
			);
		}, __("Navigation"));

		frm.add_custom_button(__("📅 Ambient Shop Calendar"), () => {
			frappe.set_route("List", "Schedule Entry", "Calendar");
		}, __("Navigation"));

		if (frm.doc.approval_token) {
			frm.add_custom_button(__("🔗 Customer Approval Portal"), () => {
				window.open(
					`/quote-approval?token=${encodeURIComponent(frm.doc.approval_token)}`,
					"_blank"
				);
			}, __("Navigation"));
		}
	},

	vehicle(frm) {
		if (!frm.doc.vehicle) return;

		frappe.db.get_value(
			"Repair Vehicle",
			frm.doc.vehicle,
			["customer", "manufacturer", "model", "license_plate"],
			(r) => {
				if (r && !frm.doc.customer && r.customer) {
					frm.set_value("customer", r.customer);
				}
			}
		);
	},
});

frappe.ui.form.on("Repair Order Service", {
	form_render(frm, cdt, cdn) {
		const row = locals[cdt]?.[cdn];
		const grid_row = frm.fields_dict.services?.grid?.grid_rows_by_docname?.[cdn];
		if (!row || !grid_row?.grid_form) return;

		const $wrapper = grid_row.grid_form.wrapper;
		if (!$wrapper.find(".ism-manage-parts-btn").length) {
			const $btn = $(
				`<button type="button" class="btn btn-xs btn-default ism-manage-parts-btn mt-2 mb-2">🔩 ${__("Manage Parts")}</button>`
			);
			$btn.on("click", () => open_manage_parts_dialog(frm, cdn));
			const $parts_field = $wrapper.find('[data-fieldname="parts"]');
			if ($parts_field.length) {
				$parts_field.prepend($btn);
			}
		}
	},

	service_package(frm, cdt, cdn) {
		const row = locals[cdt]?.[cdn];
		if (!row || !row.service_package) return;

		frappe.call({
			method: "ism.ism.doctype.repair_order.repair_order.get_service_package_details",
			args: {
				service_package: row.service_package,
				side: row.service_position && row.service_position !== "None" ? row.service_position : undefined,
			},
			callback: (r) => {
				const pkg = r.message;
				if (!pkg || !pkg.package_name) return;

				const labor_hours = ro_flt(pkg.labor_hours ?? pkg.standard_labor_hours ?? 0, 2);
				const labor_rate = ro_flt(pkg.hourly_rate ?? 0, 2);
				const normalized_parts = (pkg.parts || []).map(normalize_part_entry);

				frappe.model.set_value(cdt, cdn, "service_title", pkg.package_name || "");
				frappe.model.set_value(cdt, cdn, "correction_code", pkg.correction_code || "");
				frappe.model.set_value(cdt, cdn, "applicable_model", pkg.applicable_model || "");
				frappe.model.set_value(cdt, cdn, "is_flat_rate", pkg.is_flat_rate ? 1 : 0);
				frappe.model.set_value(cdt, cdn, "is_mobile_capable", pkg.is_mobile_capable ? 1 : 0);
				frappe.model.set_value(cdt, cdn, "tesla_manual_url", pkg.tesla_manual_url || "");
				if (pkg.service_position) {
					frappe.model.set_value(cdt, cdn, "service_position", pkg.service_position);
				}
				frappe.model.set_value(cdt, cdn, "labor_hours", labor_hours);
				frappe.model.set_value(cdt, cdn, "labor_rate", labor_rate);
				frappe.model.set_value(cdt, cdn, "parts", JSON.stringify(normalized_parts)).then(() => {
					recalculate_service_row_totals(frm, cdt, cdn, {
						use_flat_rate: Boolean(pkg.is_flat_rate),
						service_rate: pkg.service_rate,
					});
					frm.refresh_field("services");
				});
			},
		});
	},

	labor_hours(frm, cdt, cdn) {
		recalculate_service_row_totals(frm, cdt, cdn);
	},

	labor_rate(frm, cdt, cdn) {
		recalculate_service_row_totals(frm, cdt, cdn);
	},

	discount_amount(frm, cdt, cdn) {
		recalculate_service_row_totals(frm, cdt, cdn);
	},
});
