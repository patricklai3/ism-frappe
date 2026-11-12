// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.provide("frappe.listview_settings");
frappe.listview_settings["Repair Order"] = frappe.listview_settings["Repair Order"] || {
	has_indicator_for_draft: true,
	has_indicator_for_cancelled: true,
	add_fields: [
		"status",
		"approval_status",
		"vehicle_location",
		"customer",
		"vehicle",
		"grand_total",
		"docstatus",
	],
	get_indicator: function (doc) {
		const status_colors = {
			"Checked In": "blue",
			"Draft Estimate": "orange",
			"Pending Authorization": "yellow",
			"Authorized - In Progress": "blue",
			"Waiting on Parts": "orange",
			"Work Completed": "purple",
			"Billed / Paid": "green",
			"Closed / Released": "gray",
			"Cancelled": "red",
		};
		const status = doc.status || (doc.docstatus === 2 ? "Cancelled" : "Draft Estimate");
		const color = status_colors[status] || "blue";
		return [__(status), color, `status,=,${status}`];
	},
};

frappe.ui.form.on("Repair Order", {
	setup(frm) {
		frm.set_query("vehicle", function () {
			if (frm.doc.customer) {
				return {
					filters: {
						customer: frm.doc.customer,
					},
				};
			}
			return {};
		});
	},

	refresh(frm) {
		frm.trigger("update_tab_visibility");
		frm.trigger("setup_fast_action_bar");
		frm.trigger("setup_navigation_buttons");
	},

	update_tab_visibility(frm) {
		if (!frm || !frm.doc || !frm.layout || !frm.layout.tabs) return;

		const status = frm.doc.status;
		const is_approved = ["Approved", "Partially Approved"].includes(frm.doc.approval_status);
		const is_in_progress = ["Authorized - In Progress", "Waiting on Parts"].includes(status);
		const is_completed = ["Work Completed", "Billed / Paid", "Closed / Released"].includes(status);

		const show_workshop = is_approved || is_in_progress || is_completed;
		const show_billing = is_completed;

		const workshop_tab = frm.layout.tabs.find((t) => t.df && t.df.fieldname === "tab_workshop");
		if (workshop_tab) workshop_tab.toggle(show_workshop);

		const billing_tab = frm.layout.tabs.find((t) => t.df && t.df.fieldname === "tab_billing");
		if (billing_tab) billing_tab.toggle(show_billing);
	},

	setup_fast_action_bar(frm) {
		if (frm.is_new()) return;

		const status = frm.doc.status;
		const approval = frm.doc.approval_status;
		const is_approved = ["Approved", "Partially Approved"].includes(approval);

		// 1. Authorization Dispatch Action for Draft Estimates
		if (status === "Draft Estimate" || approval === "Estimating") {
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
							fieldname: "phone",
							fieldtype: "Data",
							label: __("Mobile Number (for SMS)"),
							description: __("Leave blank to use customer's master mobile number"),
						},
					],
					(values) => {
						frappe.call({
							method: "ism.api.agent.send_quote_for_approval",
							args: {
								repair_order: frm.doc.name,
								communication_medium: values.method.toLowerCase(),
								phone_number: values.phone || null,
							},
							freeze: true,
							freeze_message: __("Dispatching authorization request..."),
							callback: (r) => {
								if (r.message && r.message.success) {
									frappe.show_alert({
										message: __("Authorization link prepared!"),
										indicator: "green",
									});
									if (r.message.urls && r.message.urls.approval) {
										frappe.utils.copy_to_clipboard(r.message.urls.approval);
										frappe.msgprint(
											__("Link copied to clipboard:<br><a href='{0}' target='_blank'>{0}</a>", [r.message.urls.approval])
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
		if (is_approved && status !== "Work Completed" && status !== "Closed / Released") {
			// [Start Work] Button
			if (status !== "Authorized - In Progress") {
				frm.add_custom_button(__("▶ Start Work"), () => {
					frappe.call({
						method: "ism.api.agent.update_job_status",
						args: {
							repair_order: frm.doc.name,
							action: "start",
						},
						freeze: true,
						callback: (r) => {
							if (r.message && r.message.success) {
								frappe.show_alert({ message: __("Work Started! Status updated."), indicator: "blue" });
								frm.reload_doc();
							}
						},
					});
				}).addClass("btn-primary font-weight-bold");
			}

			// [Hold - Waiting on Parts] Button
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
							frappe.call({
								method: "ism.api.agent.update_job_status",
								args: {
									repair_order: frm.doc.name,
									action: "hold",
									notes: values.reason,
								},
								freeze: true,
								callback: (r) => {
									if (r.message && r.message.success) {
										frappe.show_alert({ message: __("Order placed on hold."), indicator: "orange" });
										frm.reload_doc();
									}
								},
							});
						},
						__("Put Job On Hold")
					);
				});
			}

			// [Complete Work] Button
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
						frappe.call({
							method: "ism.api.agent.update_job_status",
							args: {
								repair_order: frm.doc.name,
								action: "complete",
								odometer_out: values.odometer_out,
								notes: values.notes,
							},
							freeze: true,
							callback: (r) => {
								if (r.message && r.message.success) {
									frappe.show_alert({ message: __("Repair Order marked complete!"), indicator: "green" });
									frm.reload_doc();
								}
							},
						});
					},
					__("Complete Work Order")
				);
			}).addClass("btn-success");
		}
	},

	setup_navigation_buttons(frm) {
		if (frm.is_new()) return;

		// Legal Print Preview
		frm.add_custom_button(__("📄 Legal Print Preview"), () => {
			const print_url = `/printview?doctype=Repair+Order&name=${frm.doc.name}&format=Automotive+Repair+Invoice`;
			window.open(print_url, "_blank");
		}, __("Navigation"));

		// Ambient Shop Calendar
		frm.add_custom_button(__("📅 Ambient Shop Calendar"), () => {
			frappe.set_route("schedule-entry", "view", "calendar");
		}, __("Navigation"));

		// Customer Approval Portal Link
		if (frm.doc.approval_token) {
			frm.add_custom_button(__("🔗 Customer Approval Portal"), () => {
				const portal_url = `/quote-approval?token=${frm.doc.approval_token}`;
				window.open(portal_url, "_blank");
			}, __("Navigation"));
		}
	},

	customer(frm) {
		if (frm.doc.customer) {
			frappe.db.get_value("Customer", frm.doc.customer, ["mobile_no", "email_id"], (r) => {
				if (r) {
					if (!frm.doc.contact_person && r.mobile_no) {
						// Information display
					}
				}
			});
		}
	},

	vehicle(frm) {
		if (frm.doc.vehicle) {
			frappe.db.get_value("Repair Vehicle", frm.doc.vehicle, ["customer", "make", "model", "license_plate"], (r) => {
				if (r && !frm.doc.customer && r.customer) {
					frm.set_value("customer", r.customer);
				}
			});
		}
	},
});
