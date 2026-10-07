// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (!frm.is_new() && frm.doc.docstatus === 1) {
			frm.add_custom_button(__("Create Supplement"), () => {
				frappe.confirm(
					__("Create a Supplemental Quotation for additional teardown discoveries on this Repair Order?"),
					() => {
						frappe.call({
							method: "ism.api.approval.create_supplemental_quotation",
							args: { sales_order_name: frm.doc.name },
							callback(r) {
								if (r && r.message && r.message.quotation) {
									frappe.show_alert({
										message: r.message.message,
										indicator: "green",
									});
									frappe.set_route("Form", "Quotation", r.message.quotation);
								}
							},
						});
					}
				);
			}, __("Actions"));
		}
	},
});
