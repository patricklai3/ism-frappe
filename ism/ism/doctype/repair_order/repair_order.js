// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

function get_status_target_tab(doc_or_frm) {
	const doc = doc_or_frm && doc_or_frm.doc ? doc_or_frm.doc : doc_or_frm;
	if (!doc || doc.__islocal) {
		return "tab_intake";
	}

	const status = doc.status;
	const is_released = !!doc.vehicle_released || status === "Closed / Released";
	const is_work_completed =
		status === "Work Completed" || doc.vehicle_location === "Completed / Parked in Lot";
	const is_in_progress =
		["Authorized - In Progress", "Waiting on Parts"].includes(status) ||
		doc.vehicle_location === "In Bay / Being Serviced";
	const is_approved =
		doc.approval_status === "Approved" || doc.approval_status === "Partially Approved";

	if (is_released || status === "Billed / Paid" || is_work_completed) {
		return "tab_billing";
	} else if (is_in_progress || is_approved) {
		return "tab_workshop";
	} else if (doc.services && doc.services.length > 0) {
		return "tab_estimate";
	} else {
		return "tab_intake";
	}
}

function update_tab_visibility(frm) {
	if (!frm || !frm.doc) return;

	const is_approved =
		frm.doc.approval_status === "Approved" || frm.doc.approval_status === "Partially Approved";
	const status = frm.doc.status;
	const is_in_progress = ["Authorized - In Progress", "Waiting on Parts"].includes(status);
	const is_work_completed =
		["Work Completed", "Billed / Paid", "Closed / Released"].includes(status) ||
		!!frm.doc.vehicle_released;
	const is_cancelled = status === "Cancelled";

	const show_workshop = is_approved || is_in_progress || is_work_completed || is_cancelled;
	const show_billing = is_work_completed || is_cancelled;

	if (frm.layout && frm.layout.tabs) {
		const workshop_tab = frm.layout.tabs.find((t) => t.df && t.df.fieldname === "tab_workshop");
		if (workshop_tab) {
			workshop_tab.toggle(show_workshop);
		}
		const billing_tab = frm.layout.tabs.find((t) => t.df && t.df.fieldname === "tab_billing");
		if (billing_tab) {
			billing_tab.toggle(show_billing);
		}
	}
}

function select_tab(frm, target_tab_name) {
	if (!frm || !target_tab_name) return;

	const do_select = () => {
		if (!frm.layout || !frm.layout.tabs || !frm.layout.tabs.length) return;

		const target_tab = frm.layout.tabs.find(
			(t) => t.df && t.df.fieldname === target_tab_name
		);

		if (target_tab) {
			if (target_tab.df.hidden_due_to_dependency) {
				target_tab.df.hidden_due_to_dependency = false;
			}
			if (target_tab.hidden) {
				target_tab.toggle(true);
			}
			target_tab.set_active();
		} else {
			frm.layout.select_tab?.(target_tab_name);
		}

		// Ensure DOM nav link and tab pane classes are active
		const $tab_btn = frm.$wrapper.find(`.nav-link[data-fieldname="${target_tab_name}"]`);
		if ($tab_btn.length) {
			frm.$wrapper.find(".nav-link").removeClass("active");
			$tab_btn.addClass("active");

			const tab_pane_id =
				$tab_btn.attr("aria-controls") ||
				`${frappe.scrub(frm.doctype, "-")}-${target_tab_name}`;
			if (tab_pane_id) {
				frm.$wrapper.find(".tab-pane").removeClass("show active");
				frm.$wrapper.find(`#${tab_pane_id}`).addClass("show active");
			}
		}

		// Keep frm.active_tab_map synced
		if (target_tab && frm.active_tab_map) {
			frm.active_tab_map[frm.docname] = target_tab;
		}

		// Sync URL hash
		if (window.location.hash !== `#${target_tab_name}`) {
			const url = new URL(window.location.href);
			url.hash = target_tab_name;
			history.replaceState(null, null, url);
		}
	};

	do_select();
	setTimeout(do_select, 50);
	frappe.after_ajax(() => {
		do_select();
	});
}

function auto_select_active_tab(frm) {
	const target = get_status_target_tab(frm);
	if (target) {
		select_tab(frm, target);
	}
}

frappe.ui.form.on("Repair Order", {
	setup(frm) {
		if (!frm._ro_nav_bound) {
			frm._ro_nav_bound = true;

			// Track intentional DOM clicks on tab buttons by user
			frm.$wrapper.on("click", ".nav-link[data-fieldname]", function () {
				const fieldname = $(this).attr("data-fieldname");
				if (fieldname) {
					frm._user_clicked_tab = fieldname;
				}
			});

			$(frm.page.wrapper).on("hide", () => {
				frm._user_clicked_tab = null;
				if (frm.active_tab_map && frm.docname) {
					delete frm.active_tab_map[frm.docname];
				}
			});

			frappe.router.on("change", () => {
				const route = frappe.get_route();
				const is_ro_form =
					route && route[0] === "Form" && route[1] === "Repair Order" && route[2];

				if (!is_ro_form) {
					frm._user_clicked_tab = null;
					if (frm.active_tab_map && frm.docname) {
						delete frm.active_tab_map[frm.docname];
					}
				} else {
					const target_docname = route[2];
					if (target_docname !== frm.docname) {
						frm._user_clicked_tab = null;
					}
					if (!frm._user_clicked_tab) {
						const doc = frappe.get_doc("Repair Order", target_docname);
						const target = get_status_target_tab(doc);
						if (target) {
							const url = new URL(window.location.href);
							if (url.hash !== `#${target}`) {
								url.hash = target;
								history.replaceState(null, null, url);
							}
							if (frm.layout?.tabs) {
								const target_tab = frm.layout.tabs.find(
									(t) => t.df && t.df.fieldname === target
								);
								if (target_tab) {
									frm.active_tab_map ??= {};
									frm.active_tab_map[target_docname] = target_tab;
								}
							}
						}
					}
				}
			});
		}

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

		frm.set_query("taxes_and_charges", () => {
			return {
				filters: {
					company: frm.doc.company || frappe.defaults.get_user_default("Company"),
				},
			};
		});
	},

	onload(frm) {
		frm._user_clicked_tab = null;
		if (frm.active_tab_map && frm.docname) {
			delete frm.active_tab_map[frm.docname];
		}
		if (frm.is_new()) {
			if (!frm.doc.tax_rate) {
				frm.set_value("tax_rate", 13.0);
			}
			if (!frm.doc.taxes_and_charges) {
				const comp = frm.doc.company || frappe.defaults.get_user_default("Company");
				frappe.db.get_value("Sales Taxes and Charges Template", { company: comp, is_default: 1 }, "name", (r) => {
					if (r && r.name && !frm.doc.taxes_and_charges) {
						frm.set_value("taxes_and_charges", r.name);
					}
				});
			}
		}
		const target = get_status_target_tab(frm);
		if (target) {
			const url = new URL(window.location.href);
			url.hash = target;
			history.replaceState(null, null, url);

			if (frm.layout?.tabs) {
				const target_tab = frm.layout.tabs.find(
					(t) => t.df && t.df.fieldname === target
				);
				if (target_tab) {
					frm.active_tab_map ??= {};
					frm.active_tab_map[frm.docname] = target_tab;
				}
			}
		}
	},

	on_tab_change(frm) {
		// Native Frappe tab change hook
	},

	refresh(frm) {
		frm.trigger("render_progress_indicator");
		frm._original_odometer_in = frm.doc.odometer_in;
		update_tab_visibility(frm);

		if (frm._user_clicked_tab) {
			select_tab(frm, frm._user_clicked_tab);
		} else {
			auto_select_active_tab(frm);
		}

		if (!frm.is_new() && frm.doc.docstatus !== 2) {
			const is_approved = frm.doc.approval_status === "Approved" || frm.doc.approval_status === "Partially Approved";
			const is_work_completed = frm.doc.status === "Work Completed" || frm.doc.vehicle_location === "Completed / Parked in Lot";
			const is_released = !!frm.doc.vehicle_released || frm.doc.status === "Closed / Released";

			// 1. Service Packages & Supplements (available before vehicle is released)
			if (!is_released) {
				frm.add_custom_button(__("Add Service Package"), () => {
					frm.trigger("open_service_package_dialog");
				}, __("Jobs & Packages"));

				frm.add_custom_button(__("Add Supplement"), () => {
					frm.trigger("open_add_supplement_dialog");
				}, __("Jobs & Packages"));
			}

			// 2. Customer Approvals (available before release so authorization can be obtained or updated)
			if (!is_released) {
				frm.add_custom_button(__("Send Approval Link"), () => {
					frm.trigger("open_send_approval_dialog");
				}, __("Customer Approval"));

				frm.add_custom_button(__("In-Person Signature"), () => {
					frm.trigger("open_in_person_signature_dialog");
				}, __("Customer Approval"));

				frm.add_custom_button(__("Log Verbal Approval"), () => {
					frm.trigger("open_verbal_approval_dialog");
				}, __("Customer Approval"));

				frm.add_custom_button(__("Quick Authorize (All)"), () => {
					frappe.confirm(
						__("Authorize all repair jobs on this order and mark in-progress?"),
						() => {
							frm.call({
								doc: frm.doc,
								method: "authorize_jobs",
								args: {
									channel: "In-Person Signature",
									signer_name: frm.doc.customer || frappe.session.user_fullname,
								},
								freeze: true,
								freeze_message: __("Recording authorization..."),
								callback(r) {
									if (r.message && (r.message.status === "success" || r.message.approval_status)) {
										frappe.show_alert({
											message: __("All jobs authorized successfully"),
											indicator: "green",
										});
										frm._user_clicked_tab = null;
										frm.reload_doc();
									}
								},
							});
						}
					);
				}, __("Customer Approval"));
			}

			// 3. Procurement & Material Request (Gradual disclosure: only once customer authorization is on file)
			if (is_approved && !is_released) {
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

			// 4. Bay Shop Floor Execution Buttons (Gradual disclosure: strictly only when customer approved)
			if (is_approved && !is_released) {
				if (frm.doc.vehicle_location !== "In Bay / Being Serviced") {
					frm.add_custom_button(__("Pick Up Vehicle (Into Bay)"), () => {
						frm.call({
							doc: frm.doc,
							method: "pick_up_vehicle",
						}).then(() => {
							frm._user_clicked_tab = null;
							frm.reload_doc();
						});
					}, __("Bay Execution"));
				}

				if (frm.doc.vehicle_location === "In Bay / Being Serviced") {
					frm.add_custom_button(__("Park Vehicle (Lot / Ready)"), () => {
						frm.call({
							doc: frm.doc,
							method: "park_vehicle",
						}).then(() => {
							frm._user_clicked_tab = null;
							frm.reload_doc();
						});
					}, __("Bay Execution"));
				}
			}

			// 5. Billing & Release Action (Gradual disclosure: strictly only when approved AND work completed)
			if (is_approved && is_work_completed && !is_released) {
				frm.add_custom_button(__("Process Payment & Release"), () => {
					frm.trigger("open_payment_modal");
				}).addClass("btn-primary");
			}
		}
	},

	vehicle(frm) {
		if (frm.doc.vehicle) {
			frappe.db.get_value("Repair Vehicle", frm.doc.vehicle, ["last_odometer", "customer"], (r) => {
				if (r && r.last_odometer && !frm.doc.odometer_in) {
					frm.set_value("odometer_in", r.last_odometer);
				}
				if (r && r.customer && !frm.doc.customer) {
					frm.set_value("customer", r.customer);
				}
			});

			frm.trigger("load_vehicle_customers");

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
		} else {
			frm.set_df_property("customer", "description", "");
		}
	},

	load_vehicle_customers(frm) {
		if (!frm.doc.vehicle) return;

		frappe.call({
			method: "ism.ism.doctype.repair_order.repair_order.get_vehicle_customers",
			args: { vehicle: frm.doc.vehicle },
			callback(r) {
				const customers = (r && r.message) || [];
				if (customers.length > 0) {
					const names = customers.map((c) => c.customer_name || c.customer);
					frm.set_df_property(
						"customer",
						"description",
						__("Known customers for this vehicle: {0}", [names.join(", ")])
					);

					if (!frm.doc.customer) {
						const primary = customers.find((c) => c.is_primary) || customers[0];
						frm.set_value("customer", primary.customer);
					}
				}
			},
		});
	},

	odometer_in(frm) {
		if (!frm.is_new() && frm._original_odometer_in && frm.doc.odometer_in !== frm._original_odometer_in) {
			frappe.confirm(
				__("Are you sure the original intake odometer was incorrect? If not, cancel to revert."),
				() => {
					frm._original_odometer_in = frm.doc.odometer_in;
				},
				() => {
					frm.set_value("odometer_in", frm._original_odometer_in);
				}
			);
		}
	},

	inspection_template(frm) {
		if (frm.doc.inspection_template) {
			frappe.db.get_doc("Inspection Template", frm.doc.inspection_template).then((doc) => {
				if (doc && doc.items) {
					frm.clear_table("inspection_items");
					doc.items.forEach((item) => {
						const child = frm.add_child("inspection_items");
						child.inspection_description = item.inspection_description;
					});
					frm.refresh_field("inspection_items");
				}
			});
		}
	},

	taxes_and_charges(frm) {
		if (frm.doc.taxes_and_charges) {
			frappe.db.get_value("Sales Taxes and Charges", { parent: frm.doc.taxes_and_charges }, "rate", (r) => {
				if (r && r.rate !== undefined && r.rate !== null) {
					frm.set_value("tax_rate", flt(r.rate));
				}
				recalculate_totals(frm);
			});
		} else {
			recalculate_totals(frm);
		}
	},

	tax_rate(frm) {
		recalculate_totals(frm);
	},

	update_tab_visibility(frm) {
		update_tab_visibility(frm);
	},

	get_status_target_tab(frm) {
		return get_status_target_tab(frm);
	},

	select_tab(frm, target_tab_name) {
		select_tab(frm, target_tab_name);
	},

	auto_select_active_tab(frm) {
		auto_select_active_tab(frm);
	},

	status(frm) {
		update_tab_visibility(frm);
		frm._user_clicked_tab = null;
		auto_select_active_tab(frm);
	},

	approval_status(frm) {
		update_tab_visibility(frm);
		frm._user_clicked_tab = null;
		if (frm.doc.approval_status === "Approved" || frm.doc.approval_status === "Partially Approved") {
			if (frm.doc.status === "Draft Estimate" || frm.doc.status === "Pending Authorization") {
				frm.set_value("status", "Authorized - In Progress");
			}
			if (frm.doc.approval_status === "Approved" && frm.doc.services) {
				frm.doc.services.forEach((s) => {
					if (s.status !== "Declined") {
						frappe.model.set_value(s.doctype, s.name, "status", "Approved");
					}
				});
				frm.refresh_field("services");
			}
			select_tab(frm, "tab_workshop");
		}
	},

	vehicle_location(frm) {
		update_tab_visibility(frm);
		frm._user_clicked_tab = null;
		if (frm.doc.vehicle_location === "Completed / Parked in Lot") {
			select_tab(frm, "tab_billing");
		} else if (frm.doc.vehicle_location === "In Bay / Being Serviced") {
			select_tab(frm, "tab_workshop");
		}
	},

	render_progress_indicator(frm) {
		if (frm.is_new()) return;

		frm.dashboard.clear_headline();

		if (frm.doc.approval_status !== "Approved" && frm.doc.approval_status !== "Partially Approved") {
			if (frm.doc.approval_status === "Declined") {
				frm.dashboard.set_headline_alert(
					__("⛔ <b>Estimate Declined:</b> Customer has declined repair jobs. Bay work is blocked."),
					"red"
				);
			} else {
				frm.dashboard.set_headline_alert(
					__("⚠️ <b>Awaiting Customer Authorization:</b> Vehicle is blocked from entering bay until repair jobs are approved."),
					"orange"
				);
			}
			return;
		}

		const progress = frm.doc.progress_percent || 0;
		const color = progress === 100 ? "green" : progress > 0 ? "blue" : "purple";
		frm.dashboard.set_headline_alert(
			__("Location: <b>{0}</b> | Work Progress: <b>{1}%</b> | Approval: <b>{2}</b> | Status: <b>{3}</b>", [
				frm.doc.vehicle_location || "In Customer Lot",
				progress,
				frm.doc.approval_status,
				frm.doc.status,
			]),
			color
		);
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
		let services_html = `<div style="margin-bottom: 15px;">`;
		(frm.doc.services || []).forEach((s, idx) => {
			let parts_summary = "";
			let parts = [];
			try {
				parts = typeof s.parts === "string" ? JSON.parse(s.parts) : (s.parts || []);
			} catch (e) {
				parts = [];
			}
			if (parts && parts.length > 0) {
				const part_names = parts.map((p) => `${p.item_name || p.item_code} (${p.qty} ${p.uom || "Nos"})`).join(", ");
				parts_summary = `<div style="font-size: 11px; color: #666; margin-top: 2px;">Parts: ${part_names}</div>`;
			}

			services_html += `
				<div style="padding: 8px 0; border-bottom: 1px solid #f0f0f0;">
					<div style="display:flex; justify-content:space-between; align-items:center;">
						<span><b>#${idx + 1}: ${s.service_title}</b> ($${flt(s.total_amount).toFixed(2)})</span>
						<select class="service-decision form-control input-sm" data-service-name="${s.name || ""}" data-service-idx="${s.idx || idx + 1}" style="width: 140px;">
							<option value="Approved" ${s.status === "Approved" ? "selected" : ""}>${__("Approve")}</option>
							<option value="Declined" ${s.status === "Declined" ? "selected" : ""}>${__("Decline")}</option>
						</select>
					</div>
					${parts_summary}
				</div>
			`;
		});
		services_html += `</div>`;

		const d = new frappe.ui.Dialog({
			title: __("In-Person Customer Signature"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "services_table",
					options: services_html,
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
				d.$wrapper.find(".service-decision").each(function () {
					const val = $(this).val();
					const sname = $(this).attr("data-service-name");
					const sidx = $(this).attr("data-service-idx");
					if (sname) decisions[sname] = val;
					if (sidx) decisions[sidx] = val;
				});

				frm.call({
					doc: frm.doc,
					method: "authorize_jobs",
					args: {
						approvals: decisions,
						channel: "In-Person Signature",
						signer_name: values.signer_name,
						signature_image: sig_image,
					},
					freeze: true,
					freeze_message: __("Recording authorization..."),
					callback(r) {
						d.hide();
						if (r.message && (r.message.status === "success" || r.message.approval_status)) {
							frappe.show_alert({
								message: __("Customer authorization recorded ({0})", [r.message.approval_status || "Approved"]),
								indicator: "green",
							});
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
		let services_html = `<div style="margin-bottom: 15px;">`;
		(frm.doc.services || []).forEach((s, idx) => {
			let parts_summary = "";
			let parts = [];
			try {
				parts = typeof s.parts === "string" ? JSON.parse(s.parts) : (s.parts || []);
			} catch (e) {
				parts = [];
			}
			if (parts && parts.length > 0) {
				const part_names = parts.map((p) => `${p.item_name || p.item_code} (${p.qty} ${p.uom || "Nos"})`).join(", ");
				parts_summary = `<div style="font-size: 11px; color: #666; margin-top: 2px;">Parts: ${part_names}</div>`;
			}

			services_html += `
				<div style="padding: 8px 0; border-bottom: 1px solid #f0f0f0;">
					<div style="display:flex; justify-content:space-between; align-items:center;">
						<span><b>#${idx + 1}: ${s.service_title}</b> ($${flt(s.total_amount).toFixed(2)})</span>
						<select class="service-decision form-control input-sm" data-service-name="${s.name || ""}" data-service-idx="${s.idx || idx + 1}" style="width: 140px;">
							<option value="Approved" ${s.status === "Approved" ? "selected" : ""}>${__("Approve")}</option>
							<option value="Declined" ${s.status === "Declined" ? "selected" : ""}>${__("Decline")}</option>
						</select>
					</div>
					${parts_summary}
				</div>
			`;
		});
		services_html += `</div>`;

		const d = new frappe.ui.Dialog({
			title: __("Log Verbal Phone Authorization"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "services_table",
					options: services_html,
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
				d.$wrapper.find(".service-decision").each(function () {
					const val = $(this).val();
					const sname = $(this).attr("data-service-name");
					const sidx = $(this).attr("data-service-idx");
					if (sname) decisions[sname] = val;
					if (sidx) decisions[sidx] = val;
				});

				frm.call({
					doc: frm.doc,
					method: "authorize_jobs",
					args: {
						approvals: decisions,
						channel: "Verbal Phone Authorization",
						signer_name: values.authorized_by,
						notes: values.verbal_notes,
					},
					freeze: true,
					freeze_message: __("Recording authorization..."),
					callback(r) {
						d.hide();
						if (r.message && (r.message.status === "success" || r.message.approval_status)) {
							frappe.show_alert({
								message: __("Verbal authorization logged ({0})", [r.message.approval_status || "Approved"]),
								indicator: "green",
							});
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
					doc: frm.doc,
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
							frm._user_clicked_tab = null;
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
			title: __("Add Supplemental Repair Service"),
			fields: [
				{
					fieldtype: "Data",
					fieldname: "service_title",
					label: __("Service Title / Operation"),
					reqd: 1,
				},
				{
					fieldtype: "Select",
					fieldname: "category",
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
					fieldname: "labor_hours",
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
					doc: frm.doc,
					method: "add_supplement",
					args: {
						service_title: values.service_title,
						category: values.category,
						labor_hours: values.labor_hours,
						labor_rate: values.labor_rate,
						notes: values.advisor_notes,
					},
					callback(r) {
						if (r.message && r.message.status === "success") {
							frappe.show_alert({
								message: __("Supplement '{0}' added to Repair Order", [r.message.service_title]),
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

frappe.ui.form.on("Repair Order Service", {
	service_package(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.service_package) return;

		frappe.call({
			method: "ism.ism.doctype.repair_order.repair_order.get_service_package_details",
			args: { service_package: row.service_package },
			callback(r) {
				if (r.message) {
					const pkg = r.message;
					const is_flat = cint(pkg.is_flat_rate);
					const labor_amt = is_flat ? flt(pkg.service_rate) : (flt(pkg.service_rate) || (flt(pkg.standard_labor_hours) * flt(pkg.hourly_rate)));
					const parts_tot = flt(pkg.parts_total) || 0.0;

					frappe.model.set_value(cdt, cdn, "is_flat_rate", is_flat);
					frappe.model.set_value(cdt, cdn, "service_title", pkg.package_name || row.service_package);
					frappe.model.set_value(cdt, cdn, "category", pkg.category || "Maintenance");
					frappe.model.set_value(cdt, cdn, "labor_hours", flt(pkg.standard_labor_hours));
					frappe.model.set_value(cdt, cdn, "labor_rate", flt(pkg.hourly_rate));
					frappe.model.set_value(cdt, cdn, "labor_amount", labor_amt);
					frappe.model.set_value(cdt, cdn, "parts", JSON.stringify(pkg.parts || []));
					frappe.model.set_value(cdt, cdn, "parts_total", parts_tot);
					frappe.model.set_value(cdt, cdn, "total_amount", labor_amt + parts_tot);
					if (pkg.description && !row.advisor_notes) {
						frappe.model.set_value(cdt, cdn, "advisor_notes", pkg.description);
					}
					frappe.model.set_value(cdt, cdn, "parts_html", render_parts_html(pkg.parts || []));
					frm.refresh_field("services");
					recalculate_totals(frm);
				}
			},
		});
	},

	is_flat_rate(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.is_flat_rate) {
			const labor_amt = flt(row.labor_hours) * flt(row.labor_rate);
			frappe.model.set_value(cdt, cdn, "labor_amount", labor_amt);
			frappe.model.set_value(cdt, cdn, "total_amount", labor_amt + flt(row.parts_total));
		}
		recalculate_totals(frm);
	},

	labor_hours(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.is_flat_rate) return;
		const labor_amt = flt(row.labor_hours) * flt(row.labor_rate);
		frappe.model.set_value(cdt, cdn, "labor_amount", labor_amt);
		frappe.model.set_value(cdt, cdn, "total_amount", labor_amt + flt(row.parts_total));
		recalculate_totals(frm);
	},

	labor_rate(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.is_flat_rate) return;
		const labor_amt = flt(row.labor_hours) * flt(row.labor_rate);
		frappe.model.set_value(cdt, cdn, "labor_amount", labor_amt);
		frappe.model.set_value(cdt, cdn, "total_amount", labor_amt + flt(row.parts_total));
		recalculate_totals(frm);
	},

	labor_amount(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, "total_amount", flt(row.labor_amount) + flt(row.parts_total));
		recalculate_totals(frm);
	},

	is_completed(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.is_completed && row.status !== "Approved") {
			frappe.model.set_value(cdt, cdn, "is_completed", 0);
			frappe.msgprint({
				title: __("Authorization Block"),
				message: __("Service <b>{0}</b> cannot be marked complete because its status is '<b>{1}</b>'.<br><br>Only <b>Approved</b> services can be completed by technicians.", [row.service_title, row.status]),
				indicator: "red",
			});
			return;
		}
		frm.call({
			doc: frm.doc,
			method: "toggle_service_completion",
			args: { service_row_id: row.name, is_completed: row.is_completed },
		}).then(() => {
			frm.reload_doc();
		});
	},

	btn_manage_parts(frm, cdt, cdn) {
		open_parts_editor(frm, cdt, cdn);
	},

	btn_save_as_package(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.service_title) {
			frappe.msgprint(__("Please enter a Service Title first."));
			return;
		}

		frappe.prompt(
			[
				{
					fieldname: "package_name",
					fieldtype: "Data",
					label: __("Service Package Name"),
					default: row.service_title,
					reqd: 1,
				},
			],
			(values) => {
				const perform_save = () => {
					frm.call({
						doc: frm.doc,
						method: "save_service_as_package",
						args: {
							service_row_id: row.name,
							package_name: values.package_name,
						},
						callback(r) {
							if (r.message && r.message.status === "success") {
								frappe.show_alert({
									message: __("Saved as Service Package '{0}'!", [r.message.package_name]),
									indicator: "green",
								});
								frm.reload_doc();
							}
						},
					});
				};

				if (frm.is_dirty() || !row.name || String(row.name).startsWith("new-")) {
					frm.save().then(() => {
						perform_save();
					});
				} else {
					perform_save();
				}
			},
			__("Save as Reusable Service Package"),
			__("Create Package")
		);
	},
});

function render_parts_html(parts) {
	if (!parts || parts.length === 0) {
		return '<div class="text-muted" style="padding: 6px 0; font-size: 12px;"><i>No parts required for this service.</i></div>';
	}
	let rows = "";
	parts.forEach((p) => {
		const code = p.item_code || "";
		const name = p.item_name || code;
		const qty = flt(p.qty) || 1.0;
		const uom = p.uom || "Nos";
		const rate = flt(p.rate) || 0.0;
		const amt = flt(p.amount || qty * rate).toFixed(2);
		rows += `
			<tr>
				<td><b>${code}</b></td>
				<td>${name}</td>
				<td style="text-align: right;">${qty} ${uom}</td>
				<td style="text-align: right;">$${flt(rate).toFixed(2)}</td>
				<td style="text-align: right;"><b>$${amt}</b></td>
			</tr>
		`;
	});
	return `
		<table class="table table-bordered table-condensed" style="margin-top: 5px; font-size: 12px; background: #fafbfc;">
			<thead>
				<tr style="background: #eef2f7;">
					<th>Part Code</th>
					<th>Description</th>
					<th style="text-align: right;">Qty</th>
					<th style="text-align: right;">Rate</th>
					<th style="text-align: right;">Amount</th>
				</tr>
			</thead>
			<tbody>
				${rows}
			</tbody>
		</table>
	`;
}

function recalculate_totals(frm) {
	let net = 0.0;
	const has_approved = (frm.doc.services || []).some((s) => s.status === "Approved");

	(frm.doc.services || []).forEach((s) => {
		if (has_approved) {
			if (s.status === "Approved") net += flt(s.total_amount);
		} else {
			if (s.status !== "Declined") net += flt(s.total_amount);
		}
	});

	const tax_rate = flt(frm.doc.tax_rate) || 13.0;
	const tax = net * (tax_rate / 100.0);
	const grand = net + tax;

	frm.set_value("net_total", net);
	frm.set_value("total_taxes_and_charges", tax);
	frm.set_value("grand_total", grand);
	frm.set_value("outstanding_amount", grand - flt(frm.doc.paid_amount));
}

function open_parts_editor(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	let parts = [];
	try {
		parts = typeof row.parts === "string" ? JSON.parse(row.parts) : (row.parts || []);
	} catch (e) {
		parts = [];
	}

	let local_parts = JSON.parse(JSON.stringify(parts));

	function render_modal_parts_table(d) {
		let total = 0.0;
		let rows_html = "";
		local_parts.forEach((p, idx) => {
			const qty = flt(p.qty) || 1.0;
			const rate = flt(p.rate) || 0.0;
			const amt = flt(p.amount || qty * rate);
			total += amt;
			rows_html += `
				<tr data-part-idx="${idx}">
					<td style="vertical-align: middle;"><b>${p.item_code}</b></td>
					<td style="vertical-align: middle;">${p.item_name || p.item_code}</td>
					<td style="width: 100px;">
						<input type="number" step="any" class="form-control input-sm part-qty" value="${qty}" style="text-align: right;" />
					</td>
					<td style="width: 110px;">
						<input type="number" step="0.01" class="form-control input-sm part-rate" value="${rate.toFixed(2)}" style="text-align: right;" />
					</td>
					<td style="vertical-align: middle; text-align: right;" class="part-amount"><b>$${amt.toFixed(2)}</b></td>
					<td style="text-align: center; vertical-align: middle; width: 50px;">
						<button class="btn btn-default btn-xs text-danger btn-remove-part" title="${__("Remove")}">&times;</button>
					</td>
				</tr>
			`;
		});

		if (local_parts.length === 0) {
			rows_html = `<tr><td colspan="6" class="text-center text-muted" style="padding: 15px;"><i>${__("No parts added yet. Use the fields above to add parts.")}</i></td></tr>`;
		}

		const html = `
			<table class="table table-bordered table-hover" style="margin-top: 10px; font-size: 13px;">
				<thead>
					<tr style="background: #f7fafc;">
						<th>${__("Part Code")}</th>
						<th>${__("Description")}</th>
						<th style="text-align: right;">${__("Qty")}</th>
						<th style="text-align: right;">${__("Rate ($)")}</th>
						<th style="text-align: right;">${__("Amount")}</th>
						<th></th>
					</tr>
				</thead>
				<tbody id="parts-table-body">
					${rows_html}
				</tbody>
				<tfoot>
					<tr style="background: #f7fafc; font-weight: bold;">
						<td colspan="4" style="text-align: right;">${__("Total Parts:")}</td>
						<td style="text-align: right;" id="parts-modal-total">$${total.toFixed(2)}</td>
						<td></td>
					</tr>
				</tfoot>
			</table>
		`;

		d.fields_dict.parts_html_display.$wrapper.html(html);

		// Event handlers for Qty / Rate inline changes
		d.fields_dict.parts_html_display.$wrapper.find("input.part-qty, input.part-rate").on("input change", function () {
			const tr = $(this).closest("tr");
			const pidx = parseInt(tr.attr("data-part-idx"), 10);
			const new_qty = flt(tr.find(".part-qty").val());
			const new_rate = flt(tr.find(".part-rate").val());
			const new_amt = new_qty * new_rate;
			local_parts[pidx].qty = new_qty;
			local_parts[pidx].rate = new_rate;
			local_parts[pidx].amount = new_amt;
			tr.find(".part-amount").html(`<b>$${new_amt.toFixed(2)}</b>`);

			let new_total = 0;
			local_parts.forEach((p) => { new_total += flt(p.amount); });
			d.fields_dict.parts_html_display.$wrapper.find("#parts-modal-total").text(`$${new_total.toFixed(2)}`);
		});

		// Event handler for delete
		d.fields_dict.parts_html_display.$wrapper.find(".btn-remove-part").on("click", function (e) {
			e.preventDefault();
			const tr = $(this).closest("tr");
			const pidx = parseInt(tr.attr("data-part-idx"), 10);
			local_parts.splice(pidx, 1);
			render_modal_parts_table(d);
		});
	}

	const d = new frappe.ui.Dialog({
		title: __("Manage Parts: {0}", [row.service_title || "Service"]),
		fields: [
			{
				fieldtype: "Section Break",
				label: __("Add Part to Service"),
			},
			{
				fieldname: "item_code",
				fieldtype: "Link",
				label: __("Select Item / Part"),
				options: "Item",
				get_query: () => {
					return {
						filters: { is_stock_item: 1 }
					};
				},
				onchange() {
					const ic = d.get_value("item_code");
					if (ic) {
						frappe.db.get_value("Item", ic, ["item_name", "stock_uom", "standard_rate"], (r) => {
							if (r) {
								d.set_value("item_name", r.item_name || ic);
								d.set_value("uom", r.stock_uom || "Nos");
								d.set_value("rate", r.standard_rate || 0.0);
							}
						});
					}
				},
			},
			{
				fieldname: "item_name",
				fieldtype: "Data",
				label: __("Part Description"),
			},
			{
				fieldtype: "Column Break",
			},
			{
				fieldname: "qty",
				fieldtype: "Float",
				label: __("Quantity"),
				default: 1.0,
			},
			{
				fieldname: "uom",
				fieldtype: "Data",
				label: __("UOM"),
				default: "Nos",
			},
			{
				fieldname: "rate",
				fieldtype: "Currency",
				label: __("Unit Rate ($)"),
				default: 0.0,
			},
			{
				fieldtype: "Section Break",
			},
			{
				fieldname: "btn_add_part",
				fieldtype: "Button",
				label: __("+ Add Part to List"),
				click() {
					const ic = d.get_value("item_code");
					const iname = d.get_value("item_name") || ic;
					const qty = flt(d.get_value("qty")) || 1.0;
					const uom = d.get_value("uom") || "Nos";
					const rate = flt(d.get_value("rate")) || 0.0;

					if (!ic && !iname) {
						frappe.msgprint(__("Please select an Item Code or enter a Part Description."));
						return;
					}

					local_parts.push({
						item_code: ic || iname,
						item_name: iname,
						qty: qty,
						uom: uom,
						rate: rate,
						amount: flt(qty * rate),
						warehouse: "Stores - ISM",
					});

					d.set_value("item_code", "");
					d.set_value("item_name", "");
					d.set_value("qty", 1.0);
					d.set_value("rate", 0.0);

					render_modal_parts_table(d);
				},
			},
			{
				fieldtype: "Section Break",
				label: __("Parts on this Service"),
			},
			{
				fieldname: "parts_html_display",
				fieldtype: "HTML",
			},
		],
		primary_action_label: __("Save Parts to Service"),
		primary_action() {
			let total_parts_amt = 0;
			local_parts.forEach((p) => {
				const q = flt(p.qty) || 1.0;
				const r = flt(p.rate) || 0.0;
				p.amount = flt(q * r);
				total_parts_amt += p.amount;
			});

			frappe.model.set_value(cdt, cdn, "parts", JSON.stringify(local_parts));
			frappe.model.set_value(cdt, cdn, "parts_total", total_parts_amt);
			const labor_amt = row.is_flat_rate
				? flt(row.labor_amount)
				: (flt(row.labor_amount) || (flt(row.labor_hours) * flt(row.labor_rate)));
			frappe.model.set_value(cdt, cdn, "total_amount", labor_amt + total_parts_amt);
			frappe.model.set_value(cdt, cdn, "parts_html", render_parts_html(local_parts));

			frm.refresh_field("services");
			recalculate_totals(frm);
			d.hide();
		},
	});

	d.show();
	render_modal_parts_table(d);
}
