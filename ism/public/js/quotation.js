// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.ui.form.on("Quotation", {
	refresh(frm) {
		frm.set_query("custom_vehicle", () => {
			const filters = {};
			if (frm.doc.party_name && frm.doc.quotation_to === "Customer") {
				filters.customer = frm.doc.party_name;
			}
			return {
				query: "ism.api.quotation.get_active_checked_in_vehicles",
				filters: filters,
			};
		});

		frm.set_query("custom_vehicle_check_in", () => {
			const filters = { status: "Checked In" };
			if (frm.doc.custom_vehicle) {
				filters.vehicle = frm.doc.custom_vehicle;
			}
			if (frm.doc.party_name && frm.doc.quotation_to === "Customer") {
				filters.customer = frm.doc.party_name;
			}
			return { filters: filters };
		});

		if (!frm.is_new()) {
			// Action group for Customer Authorization
			frm.add_custom_button(__("Send Approval Link"), () => {
				open_send_approval_dialog(frm);
			}, __("Customer Approval"));

			frm.add_custom_button(__("In-Person Signature"), () => {
				open_in_person_signature_dialog(frm);
			}, __("Customer Approval"));

			frm.add_custom_button(__("Log Verbal Approval"), () => {
				open_verbal_approval_dialog(frm);
			}, __("Customer Approval"));

			// Quick Package Insert
			if (frm.doc.docstatus === 0) {
				frm.add_custom_button(__("Add Service Package"), () => {
					open_service_package_dialog(frm);
				});
			}
		}
	},

	custom_vehicle(frm) {
		if (frm.doc.custom_vehicle) {
			frappe.call({
				method: "ism.api.quotation.get_active_check_in_for_vehicle",
				args: { vehicle: frm.doc.custom_vehicle },
				callback(r) {
					if (r && r.message) {
						const ci = r.message;
						frm._setting_from_vehicle = true;
						frm.set_value("custom_vehicle_check_in", ci.name);
						if (!frm.doc.custom_odometer_in && ci.intake_mileage) {
							frm.set_value("custom_odometer_in", ci.intake_mileage);
						}
						if (!frm.doc.custom_customer_concern && ci.customer_concern) {
							frm.set_value("custom_customer_concern", ci.customer_concern);
						}
						if (!frm.doc.party_name && ci.customer) {
							frm.set_value("quotation_to", "Customer");
							frm.set_value("party_name", ci.customer);
						}
						frm._setting_from_vehicle = false;
					}
				},
			});
		}
	},

	custom_vehicle_check_in(frm) {
		if (frm._setting_from_vehicle) {
			return;
		}
		if (frm.doc.custom_vehicle_check_in) {
			frappe.db.get_value(
				"Vehicle Check-in",
				frm.doc.custom_vehicle_check_in,
				["vehicle", "customer", "intake_mileage", "customer_concern"]
			).then((r) => {
				if (r && r.message) {
					const data = r.message;
					if (data.vehicle && frm.doc.custom_vehicle !== data.vehicle) {
						frm.set_value("custom_vehicle", data.vehicle);
					}
					if (data.customer && frm.doc.party_name !== data.customer) {
						frm.set_value("quotation_to", "Customer");
						frm.set_value("party_name", data.customer);
					}
					if (!frm.doc.custom_odometer_in && data.intake_mileage) {
						frm.set_value("custom_odometer_in", data.intake_mileage);
					}
					if (!frm.doc.custom_customer_concern && data.customer_concern) {
						frm.set_value("custom_customer_concern", data.customer_concern);
					}
				}
			});
		}
	},
});


function open_send_approval_dialog(frm) {
	if (frm.doc.docstatus === 0) {
		frappe.confirm(
			__("The quotation must be submitted to freeze the estimate before presenting it to the customer. Submit now?"),
			() => {
				frm.savesubmit();
			}
		);
		return;
	}

	const d = new frappe.ui.Dialog({
		title: __("Send Approval Request"),
		fields: [
			{
				label: __("Channel"),
				fieldname: "channel",
				fieldtype: "Select",
				options: "sms\nemail\nlink",
				default: "sms",
				reqd: 1,
			},
			{
				label: __("Recipient (Phone or Email)"),
				fieldname: "recipient",
				fieldtype: "Data",
				description: __("Leave blank to use default customer contact details"),
			},
		],
		primary_action_label: __("Send / Generate Link"),
		primary_action(values) {
			d.hide();
			frappe.call({
				method: "ism.api.approval.send_approval_request",
				args: {
					quotation_name: frm.doc.name,
					channel: values.channel,
					recipient: values.recipient,
				},
				callback(r) {
					if (r && r.message) {
						const res = r.message;
						if (values.channel === "link" || res.approval_url) {
							frappe.utils.copy_to_clipboard(res.approval_url);
							frappe.msgprint({
								title: __("Approval Link Copied"),
								message: __("Link copied to clipboard:<br><code>{0}</code>").format(res.approval_url),
								indicator: "green",
							});
						} else {
							frappe.show_alert({
								message: res.message || __("Request dispatched."),
								indicator: "green",
							});
						}
						frm.reload_doc();
					}
				},
			});
		},
	});
	d.show();
}


function open_in_person_signature_dialog(frm) {
	if (frm.doc.docstatus === 0) {
		frappe.confirm(
			__("The quotation must be submitted to freeze the estimate before capturing customer signature. Submit now?"),
			() => {
				frm.savesubmit();
			}
		);
		return;
	}

	const jobs = frm.doc.custom_jobs || [];
	if (!jobs.length) {
		frappe.msgprint(__("Please configure at least one Repair Job before capturing customer signature."));
		return;
	}

	let job_html = `<table class="table table-bordered table-sm">
		<thead>
			<tr>
				<th>${__("Job")}</th>
				<th>${__("Est. Total")}</th>
				<th>${__("Decision")}</th>
			</tr>
		</thead>
		<tbody>`;

	jobs.forEach((j) => {
		job_html += `<tr>
			<td><b>${frappe.utils.escape_html(j.job_title)}</b></td>
			<td>$${frappe.format(j.estimated_total, { fieldtype: "Currency" })}</td>
			<td>
				<select class="form-control form-control-sm job-decision" data-job="${j.name}">
					<option value="Approved" ${j.status === "Approved" ? "selected" : ""}>${__("Approve")}</option>
					<option value="Declined" ${j.status === "Declined" ? "selected" : ""}>${__("Decline")}</option>
				</select>
			</td>
		</tr>`;
	});
	job_html += `</tbody></table>`;

	const d = new frappe.ui.Dialog({
		title: __("In-Person Customer Authorization"),
		fields: [
			{
				label: __("Signer Name"),
				fieldname: "signer_name",
				fieldtype: "Data",
				default: frm.doc.customer_name || frm.doc.party_name,
				reqd: 1,
			},
			{
				fieldname: "job_table_html",
				fieldtype: "HTML",
				options: job_html,
			},
			{
				label: __("Signature Pad"),
				fieldname: "signature_pad_html",
				fieldtype: "HTML",
				options: `
					<div style="border: 1px solid #ccc; border-radius: 4px; background: #fff;">
						<canvas id="ism-signature-pad" width="450" height="150" style="touch-action: none; width: 100%; display: block;"></canvas>
					</div>
					<button type="button" class="btn btn-xs btn-default mt-1" id="ism-clear-sig">${__("Clear Signature")}</button>
				`,
			},
		],
		primary_action_label: __("Confirm Authorization"),
		primary_action(values) {
			const canvas = document.getElementById("ism-signature-pad");
			const sigData = canvas ? canvas.toDataURL("image/png") : null;

			const decisions = [];
			d.$wrapper.find(".job-decision").each(function () {
				decisions.push({
					name: $(this).data("job"),
					status: $(this).val(),
				});
			});

			d.hide();
			frappe.call({
				method: "ism.api.approval.capture_in_person_signature",
				args: {
					quotation_name: frm.doc.name,
					approvals: decisions,
					signature_data: sigData,
					signer_name: values.signer_name,
				},
				callback(r) {
					if (r && r.message) {
						frappe.show_alert({ message: r.message.message, indicator: "green" });
						frm.reload_doc();
					}
				},
			});
		},
	});

	d.show();

	// Attach canvas drawing listeners
	setTimeout(() => {
		const canvas = document.getElementById("ism-signature-pad");
		if (!canvas) return;
		const ctx = canvas.getContext("2d");
		let drawing = false;

		function getPos(e) {
			const rect = canvas.getBoundingClientRect();
			const clientX = e.touches ? e.touches[0].clientX : e.clientX;
			const clientY = e.touches ? e.touches[0].clientY : e.clientY;
			return {
				x: (clientX - rect.left) * (canvas.width / rect.width),
				y: (clientY - rect.top) * (canvas.height / rect.height),
			};
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
			e.preventDefault();
		});
		canvas.addEventListener("touchmove", (e) => {
			if (!drawing) return;
			const pos = getPos(e);
			ctx.lineTo(pos.x, pos.y);
			ctx.stroke();
			e.preventDefault();
		});
		window.addEventListener("touchend", () => { drawing = false; });

		document.getElementById("ism-clear-sig")?.addEventListener("click", () => {
			ctx.clearRect(0, 0, canvas.width, canvas.height);
		});
	}, 200);
}


function open_verbal_approval_dialog(frm) {
	if (frm.doc.docstatus === 0) {
		frappe.confirm(
			__("The quotation must be submitted to freeze the estimate before recording verbal authorization. Submit now?"),
			() => {
				frm.savesubmit();
			}
		);
		return;
	}

	const jobs = frm.doc.custom_jobs || [];
	if (!jobs.length) {
		frappe.msgprint(__("Please configure at least one Repair Job before logging verbal authorization."));
		return;
	}

	let job_html = `<table class="table table-bordered table-sm">
		<thead>
			<tr>
				<th>${__("Job")}</th>
				<th>${__("Est. Total")}</th>
				<th>${__("Decision")}</th>
			</tr>
		</thead>
		<tbody>`;

	jobs.forEach((j) => {
		job_html += `<tr>
			<td><b>${frappe.utils.escape_html(j.job_title)}</b></td>
			<td>$${frappe.format(j.estimated_total, { fieldtype: "Currency" })}</td>
			<td>
				<select class="form-control form-control-sm job-decision" data-job="${j.name}">
					<option value="Approved" ${j.status === "Approved" ? "selected" : ""}>${__("Approve")}</option>
					<option value="Declined" ${j.status === "Declined" ? "selected" : ""}>${__("Decline")}</option>
				</select>
			</td>
		</tr>`;
	});
	job_html += `</tbody></table>`;

	const d = new frappe.ui.Dialog({
		title: __("Log Verbal Phone Authorization"),
		fields: [
			{
				label: __("Spoke With (Customer Name)"),
				fieldname: "contact_person",
				fieldtype: "Data",
				default: frm.doc.customer_name || frm.doc.party_name,
				reqd: 1,
			},
			{
				label: __("Phone Number Called"),
				fieldname: "phone_number",
				fieldtype: "Data",
				default: frm.doc.contact_mobile || "",
				reqd: 1,
			},
			{
				fieldname: "job_table_html",
				fieldtype: "HTML",
				options: job_html,
			},
			{
				label: __("Conversation & Authorization Notes"),
				fieldname: "notes",
				fieldtype: "Small Text",
				description: __("Detail customer consent and any discussion points"),
				reqd: 1,
			},
		],
		primary_action_label: __("Save Verbal Authorization"),
		primary_action(values) {
			const decisions = [];
			d.$wrapper.find(".job-decision").each(function () {
				decisions.push({
					name: $(this).data("job"),
					status: $(this).val(),
				});
			});

			d.hide();
			frappe.call({
				method: "ism.api.approval.log_verbal_authorization",
				args: {
					quotation_name: frm.doc.name,
					approvals: decisions,
					contact_person: values.contact_person,
					phone_number: values.phone_number,
					notes: values.notes,
				},
				callback(r) {
					if (r && r.message) {
						frappe.show_alert({ message: r.message.message, indicator: "green" });
						frm.reload_doc();
					}
				},
			});
		},
	});

	d.show();
}


frappe.ui.form.on("Quotation Job", {
	service_package(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.service_package) return;
		populate_service_package_items(frm, row);
	},
	custom_jobs_remove(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row) return;
		const items_to_keep = (frm.doc.items || []).filter(
			(it) => it.custom_job !== row.name && it.custom_job_title !== row.job_title
		);
		frm.set_value("items", items_to_keep);
		recalculate_job_totals(frm);
	},
});


frappe.ui.form.on("Quotation Item", {
	amount(frm) {
		recalculate_job_totals(frm);
	},
	rate(frm) {
		recalculate_job_totals(frm);
	},
	qty(frm) {
		recalculate_job_totals(frm);
	},
	items_remove(frm) {
		recalculate_job_totals(frm);
	},
});


function populate_service_package_items(frm, row) {
	frappe.call({
		method: "ism.api.quotation.get_service_package_details",
		args: { package_name: row.service_package },
		callback(r) {
			if (!r || !r.message) return;
			const pkg = r.message;

			frappe.model.set_value(row.doctype, row.name, "job_title", pkg.package_name);
			frappe.model.set_value(row.doctype, row.name, "job_category", pkg.category);

			// Remove any existing items previously linked to this job row
			const current_items = (frm.doc.items || []).filter(
				(it) => it.custom_job !== row.name && it.custom_job_title !== pkg.package_name
			);
			frm.set_value("items", current_items);

			// 1. Add labor/service item if labor hours or rate > 0
			const labor_hours = flt(pkg.standard_labor_hours);
			const hourly_rate = flt(pkg.hourly_rate);
			const service_rate = flt(pkg.service_rate);
			const is_flat = cint(pkg.is_flat_rate);

			const labor_qty = (!is_flat && labor_hours > 0) ? labor_hours : 1.0;
			const labor_unit_rate = (!is_flat && hourly_rate > 0) ? hourly_rate : service_rate;

			if (labor_unit_rate > 0 || service_rate > 0) {
				const item = frm.add_child("items", {
					item_code: pkg.package_name,
					item_name: pkg.package_name,
					description: pkg.description || pkg.package_name,
					qty: labor_qty,
					rate: labor_unit_rate,
					amount: labor_qty * labor_unit_rate,
					uom: "Hour",
					conversion_factor: 1.0,
					custom_job: row.name,
					custom_job_title: pkg.package_name,
				});
				frm.script_manager.trigger("item_code", item.doctype, item.name);
			}

			// 2. Add linked parts
			(pkg.items || []).forEach((part) => {
				const p_qty = flt(part.qty) || 1.0;
				const p_rate = flt(part.rate) || 0.0;
				const p_item = frm.add_child("items", {
					item_code: part.item_code,
					item_name: part.item_name || part.item_code,
					description: part.item_name || part.item_code,
					qty: p_qty,
					rate: p_rate,
					amount: p_qty * p_rate,
					uom: part.uom || "Nos",
					conversion_factor: 1.0,
					custom_job: row.name,
					custom_job_title: pkg.package_name,
				});
				frm.script_manager.trigger("item_code", p_item.doctype, p_item.name);
			});

			frm.refresh_fields(["items", "custom_jobs"]);
			recalculate_job_totals(frm);
		},
	});
}


function recalculate_job_totals(frm) {
	const job_totals = {};
	(frm.doc.items || []).forEach((it) => {
		const key = it.custom_job || it.custom_job_title;
		if (key) {
			job_totals[key] = (job_totals[key] || 0.0) + flt(it.amount);
		}
	});

	(frm.doc.custom_jobs || []).forEach((j) => {
		const total = job_totals[j.name] !== undefined ? job_totals[j.name] : (job_totals[j.job_title] || 0.0);
		frappe.model.set_value(j.doctype, j.name, "estimated_total", total);
	});
	frm.refresh_field("custom_jobs");
}


function open_service_package_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Add from Service Package"),
		fields: [
			{
				label: __("Select Service Package"),
				fieldname: "service_package",
				fieldtype: "Link",
				options: "Service Package",
				reqd: 1,
			},
		],
		primary_action_label: __("Add Package"),
		primary_action(values) {
			d.hide();
			const job_row = frm.add_child("custom_jobs", {
				service_package: values.service_package,
				status: "Draft",
			});
			frm.refresh_field("custom_jobs");
			populate_service_package_items(frm, job_row);
			frappe.show_alert({
				message: __("Service Package '{0}' added to quote.").format(values.service_package),
				indicator: "green",
			});
		},
	});
	d.show();
}
