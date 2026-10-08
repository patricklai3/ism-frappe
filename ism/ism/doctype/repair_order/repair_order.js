// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.ui.form.on("Repair Order", {
	setup(frm) {
		frm.set_query("vehicle", () => {
			const filters = {};
			if (frm.doc.customer) {
				filters.customer = frm.doc.customer;
			}
			return {
				query: "ism.ism.doctype.repair_order.repair_order.get_filtered_vehicles",
				filters: filters,
			};
		});

		frm.set_query("vehicle_check_in", () => {
			const filters = {};
			if (frm.doc.vehicle) {
				filters.vehicle = frm.doc.vehicle;
			}
			if (frm.doc.customer) {
				filters.customer = frm.doc.customer;
			}
			return { filters: filters };
		});
	},

	refresh(frm) {
		frm.trigger("render_progress_indicator");

		if (!frm.is_new() && frm.doc.docstatus !== 2) {
			// 1. Service Packages & Supplements
			if (!frm.doc.vehicle_released) {
				frm.add_custom_button(__("Add Service Package"), () => {
					frm.trigger("open_service_package_dialog");
				}, __("Jobs & Packages"));

				frm.add_custom_button(__("Add Supplement"), () => {
					frm.trigger("open_add_supplement_dialog");
				}, __("Jobs & Packages"));
			}

			// 2. Bay Shop Floor Execution Buttons
			if (frm.doc.vehicle_location !== "In Bay / Being Serviced" && !frm.doc.vehicle_released) {
				frm.add_custom_button(__("Pick Up Vehicle (Into Bay)"), () => {
					frm.call("pick_up_vehicle").then(() => frm.reload_doc());
				}, __("Bay Execution"));
			}

			if (frm.doc.vehicle_location === "In Bay / Being Serviced" && !frm.doc.vehicle_released) {
				frm.add_custom_button(__("Park Vehicle (Lot / Ready)"), () => {
					frm.call("park_vehicle").then(() => frm.reload_doc());
				}, __("Bay Execution"));
			}

			// 3. Customer Approvals
			if (!frm.doc.vehicle_released) {
				frm.add_custom_button(__("Send Approval Link"), () => {
					frm.trigger("open_send_approval_dialog");
				}, __("Customer Approval"));

				frm.add_custom_button(__("In-Person Signature"), () => {
					frm.trigger("open_in_person_signature_dialog");
				}, __("Customer Approval"));

				frm.add_custom_button(__("Log Verbal Approval"), () => {
					frm.trigger("open_verbal_approval_dialog");
				}, __("Customer Approval"));
			}

			// 4. Procurement & Material Request
			if (!frm.doc.vehicle_released) {
				frm.add_custom_button(__("Create Material Request"), () => {
					frm.call("create_material_request").then((r) => {
						if (r && r.message && r.message.material_request) {
							frappe.show_alert({
								message: __("Material Request {0} created", [r.message.material_request]),
								indicator: "green",
							});
						}
					});
				}, __("Inventory"));
			}

			// 5. Billing & Release Action
			if (!frm.doc.vehicle_released && frm.doc.status !== "Closed / Released") {
				frm.add_custom_button(__("Process Payment & Release"), () => {
					frm.trigger("open_payment_modal");
				}).addClass("btn-primary");
			}
		}
	},

	vehicle(frm) {
		if (frm.doc.vehicle) {
			frappe.db.get_value("Repair Vehicle", frm.doc.vehicle, ["last_odometer"], (r) => {
				if (r && r.last_odometer && !frm.doc.odometer_in) {
					frm.set_value("odometer_in", r.last_odometer);
				}
			});

			// Check for previously deferred services
			frappe.call({
				method: "ism.ism.doctype.repair_order.repair_order.get_deferred_services_for_vehicle",
				args: { vehicle: frm.doc.vehicle },
				callback(r) {
					if (r.message && r.message.length > 0) {
						let deferred_items = r.message.map((d) => `• ${d.service_description} ($${d.estimated_cost})`).join("<br>");
						frappe.msgprint({
							title: __("Deferred Services History"),
							message: __("This vehicle has open deferred services from previous visits:<br><br>{0}", [deferred_items]),
							indicator: "orange",
						});
					}
				},
			});
		}
	},

	render_progress_indicator(frm) {
		if (frm.doc.docstatus === 0 && frm.doc.jobs && frm.doc.jobs.length > 0) {
			const progress = frm.doc.progress_percent || 0;
			const color = progress === 100 ? "green" : progress > 0 ? "blue" : "orange";
			frm.dashboard.set_headline(
				__("Location: <b>{0}</b> | Work Progress: <b>{1}%</b> | Status: <b>{2}</b>", [
					frm.doc.vehicle_location || "In Customer Lot",
					progress,
					frm.doc.status,
				]),
				color
			);
		}
	},

	open_service_package_dialog(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Select Service Package"),
			fields: [
				{
					fieldname: "service_package",
					fieldtype: "Link",
					label: __("Service Package"),
					options: "Service Package",
					reqd: 1,
				},
			],
			primary_action_label: __("Add to Order"),
			primary_action(values) {
				frm.call("add_service_package", { service_package: values.service_package }).then(() => {
					dialog.hide();
					frm.reload_doc();
				});
			},
		});
		dialog.show();
	},

	open_send_approval_dialog(frm) {
		frm.call("generate_approval_token").then((r) => {
			if (r && r.message) {
				const token = r.message.approval_token;
				const url = `${window.location.origin}/quote-approval?token=${token}`;
				frappe.msgprint({
					title: __("Customer Approval Link"),
					message: `
						<p>${__("Share the following tokenized authorization link with the customer via SMS or email:")}</p>
						<div class="input-group">
							<input type="text" class="form-control" value="${url}" readonly id="approval-link-input">
						</div>
					`,
					indicator: "blue",
				});
			}
		});
	},

	open_in_person_signature_dialog(frm) {
		let jobs_html = `<div style="margin-bottom: 15px;">`;
		frm.doc.jobs.forEach((j) => {
			jobs_html += `
				<div style="display:flex; justify-content:space-between; align-items:center; padding: 6px 0; border-bottom: 1px solid #f0f0f0;">
					<span><b>${j.job_title}</b> ($${j.estimated_total})</span>
					<select class="job-decision form-control input-sm" data-job-id="${j.job_id}" style="width: 140px;">
						<option value="Approved" ${j.status === 'Approved' ? 'selected' : ''}>${__("Approve")}</option>
						<option value="Declined" ${j.status === 'Declined' ? 'selected' : ''}>${__("Decline")}</option>
					</select>
				</div>
			`;
		});
		jobs_html += `</div>`;

		const d = new frappe.ui.Dialog({
			title: __("In-Person Customer Signature"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "jobs_table",
					options: jobs_html,
				},
				{
					fieldtype: "Data",
					fieldname: "signer_name",
					label: __("Customer Signer Name"),
					default: frm.doc.customer,
					reqd: 1,
				},
				{
					fieldtype: "HTML",
					fieldname: "signature_pad",
					label: __("Signature"),
					options: `
						<div style="border: 1px dashed #ccc; border-radius: 4px; background: #fff; padding: 5px;">
							<canvas id="signature-canvas" width="450" height="150" style="display:block; touch-action:none;"></canvas>
						</div>
						<button class="btn btn-default btn-xs" id="clear-sig-btn" style="margin-top: 5px;">${__("Clear Canvas")}</button>
					`,
				},
			],
			primary_action_label: __("Submit Customer Authorization"),
			primary_action(values) {
				const canvas = document.getElementById("signature-canvas");
				const sig_image = canvas.toDataURL("image/png");

				const decisions = {};
				d.$wrapper.find(".job-decision").each(function () {
					decisions[$(this).attr("data-job-id")] = $(this).val();
				});

				frm.call({
					method: "authorize_jobs",
					args: {
						approvals: decisions,
						channel: "In-Person Signature",
						signer_name: values.signer_name,
						signature_image: sig_image,
					},
					callback(r) {
						if (r.message && r.message.status === "success") {
							d.hide();
							frm.reload_doc();
						}
					},
				});
			},
		});

		d.show();

		// Setup HTML5 canvas signature
		setTimeout(() => {
			const canvas = document.getElementById("signature-canvas");
			if (!canvas) return;
			const ctx = canvas.getContext("2d");
			let drawing = false;

			function getPos(e) {
				const rect = canvas.getBoundingClientRect();
				const clientX = e.touches ? e.touches[0].clientX : e.clientX;
				const clientY = e.touches ? e.touches[0].clientY : e.clientY;
				return { x: clientX - rect.left, y: clientY - rect.top };
			}

			canvas.addEventListener("mousedown", (e) => {
				drawing = true;
				const pos = getPos(e);
				ctx.beginPath();
				ctx.moveTo(pos.x, pos.y);
			});
			canvas.addEventListener("mousemove", (e) => {
				if (!drawing) return;
				const pos = getPos(e);
				ctx.lineTo(pos.x, pos.y);
				ctx.stroke();
			});
			window.addEventListener("mouseup", () => { drawing = false; });

			canvas.addEventListener("touchstart", (e) => {
				drawing = true;
				const pos = getPos(e);
				ctx.beginPath();
				ctx.moveTo(pos.x, pos.y);
			});
			canvas.addEventListener("touchmove", (e) => {
				if (!drawing) return;
				const pos = getPos(e);
				ctx.lineTo(pos.x, pos.y);
				ctx.stroke();
				e.preventDefault();
			});
			window.addEventListener("touchend", () => { drawing = false; });

			document.getElementById("clear-sig-btn").addEventListener("click", (e) => {
				e.preventDefault();
				ctx.clearRect(0, 0, canvas.width, canvas.height);
			});
		}, 200);
	},

	open_verbal_approval_dialog(frm) {
		let jobs_html = `<div style="margin-bottom: 15px;">`;
		frm.doc.jobs.forEach((j) => {
			jobs_html += `
				<div style="display:flex; justify-content:space-between; align-items:center; padding: 6px 0; border-bottom: 1px solid #f0f0f0;">
					<span><b>${j.job_title}</b> ($${j.estimated_total})</span>
					<select class="job-decision form-control input-sm" data-job-id="${j.job_id}" style="width: 140px;">
						<option value="Approved" ${j.status === 'Approved' ? 'selected' : ''}>${__("Approve")}</option>
						<option value="Declined" ${j.status === 'Declined' ? 'selected' : ''}>${__("Decline")}</option>
					</select>
				</div>
			`;
		});
		jobs_html += `</div>`;

		const d = new frappe.ui.Dialog({
			title: __("Log Verbal Phone Authorization"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "jobs_table",
					options: jobs_html,
				},
				{
					fieldtype: "Data",
					fieldname: "authorized_by",
					label: __("Caller / Authorizer Name"),
					default: frm.doc.customer,
					reqd: 1,
				},
				{
					fieldtype: "Small Text",
					fieldname: "verbal_notes",
					label: __("Notes (Call details, timestamp, spoken agreement)"),
					reqd: 1,
				},
			],
			primary_action_label: __("Log Authorization"),
			primary_action(values) {
				const decisions = {};
				d.$wrapper.find(".job-decision").each(function () {
					decisions[$(this).attr("data-job-id")] = $(this).val();
				});

				frm.call({
					method: "authorize_jobs",
					args: {
						approvals: decisions,
						channel: "Verbal Phone Authorization",
						signer_name: values.authorized_by,
						notes: values.verbal_notes,
					},
					callback(r) {
						if (r.message && r.message.status === "success") {
							d.hide();
							frm.reload_doc();
						}
					},
				});
			},
		});
		d.show();
	},

	open_payment_modal(frm) {
		const d = new frappe.ui.Dialog({
			title: __("Process Payment & Release Vehicle"),
			fields: [
				{
					fieldtype: "Int",
					fieldname: "odometer_out",
					label: __("Odometer Out"),
					default: frm.doc.odometer_out || frm.doc.odometer_in,
					reqd: 1,
				},
				{
					fieldtype: "Select",
					fieldname: "payment_method",
					label: __("Payment Method"),
					options: "Debit / Interac\nCredit Card\nCash\nOn Account / Fleet",
					default: "Debit / Interac",
					reqd: 1,
				},
				{
					fieldtype: "Currency",
					fieldname: "amount_to_pay",
					label: __("Amount to Pay"),
					default: frm.doc.grand_total,
					reqd: 1,
				},
			],
			primary_action_label: __("Submit Invoice & Release"),
			primary_action(values) {
				frm.call({
					method: "process_payment_and_release",
					args: {
						payment_method: values.payment_method,
						paid_amount: values.amount_to_pay,
						odometer_out: values.odometer_out,
					},
					callback(r) {
						if (r.message && r.message.status === "success") {
							frappe.show_alert({
								message: __("Sales Invoice {0} submitted & Vehicle Released!", [r.message.sales_invoice]),
								indicator: "green",
							});
							d.hide();
							frm.reload_doc();
						}
					},
				});
			},
		});
		d.show();
	},

	open_add_supplement_dialog(frm) {
		const d = new frappe.ui.Dialog({
			title: __("Add Supplemental Repair Job"),
			fields: [
				{
					fieldtype: "Data",
					fieldname: "job_title",
					label: __("Job Title / Operation"),
					reqd: 1,
				},
				{
					fieldtype: "Select",
					fieldname: "job_category",
					label: __("Category"),
					options: "Maintenance\nDiagnostic\nMechanical\nElectrical\nTires\nBody\nOther",
					default: "Mechanical",
					reqd: 1,
				},
				{
					fieldtype: "Column Break",
				},
				{
					fieldtype: "Float",
					fieldname: "estimated_labor_hours",
					label: __("Est. Labor Hours"),
					default: 1.0,
				},
				{
					fieldtype: "Currency",
					fieldname: "labor_rate",
					label: __("Labor Rate ($/hr)"),
					default: 120.0,
				},
				{
					fieldtype: "Section Break",
					label: __("Findings & Reason for Supplement"),
				},
				{
					fieldtype: "Small Text",
					fieldname: "advisor_notes",
					label: __("Advisor Notes / Discovery Reason"),
					placeholder: __("E.g. Discovered seized caliper slide pins while replacing brake pads"),
				},
			],
			primary_action_label: __("Add Supplement"),
			primary_action(values) {
				frm.call({
					method: "add_supplement",
					args: {
						job_title: values.job_title,
						job_category: values.job_category,
						estimated_labor_hours: values.estimated_labor_hours,
						labor_rate: values.labor_rate,
						notes: values.advisor_notes,
					},
					callback(r) {
						if (r.message && r.message.status === "success") {
							frappe.show_alert({
								message: __("Supplement {0} added to Repair Order", [r.message.job_id]),
								indicator: "green",
							});
							d.hide();
							frm.reload_doc();
						}
					},
				});
			},
		});
		d.show();
	},
});

frappe.ui.form.on("Repair Order Job", {
	is_completed(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frm.call("toggle_job_completion", { job_id: row.job_id, is_completed: row.is_completed }).then(() => {
			frm.reload_doc();
		});
	},
});

frappe.ui.form.on("Repair Order Item", {
	qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, "amount", flt(row.qty) * flt(row.rate));
	},
	rate(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, "amount", flt(row.qty) * flt(row.rate));
	},
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.item_code) {
			frappe.db.get_value("Item", row.item_code, ["item_name", "stock_uom", "standard_rate"], (r) => {
				if (r) {
					frappe.model.set_value(cdt, cdn, "item_name", r.item_name || row.item_code);
					frappe.model.set_value(cdt, cdn, "uom", r.stock_uom);
					if (!row.rate && r.standard_rate) {
						frappe.model.set_value(cdt, cdn, "rate", r.standard_rate);
					}
				}
			});
		}
	},
});
