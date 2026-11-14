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

		// 1. Authorization Dispatch Action prior to approval/completion
		if (!is_approved && status !== "Work Completed") {
			frm.add_custom_button(__("📲 Send for Customer Authorization"), () => {
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
			}, __("Actions"));
		}

		// 2. Technician 1-Tap Fast-Action Bar
		if (!is_approved || status === "Work Completed") return;

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
