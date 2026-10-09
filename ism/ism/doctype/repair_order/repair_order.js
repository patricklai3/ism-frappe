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

function get_status_target_tab(doc_or_frm) {
	const doc = doc_or_frm && doc_or_frm.doc ? doc_or_frm.doc : doc_or_frm;
	if (!doc || doc.__islocal) {
		return "tab_intake";
	}

	const status = doc.status;
	const is_released = !!doc.vehicle_released || status === "Closed / Released";
	const is_billed = status === "Billed / Paid";
	const is_in_bay = doc.vehicle_location === "In Bay / Being Serviced";
	const is_work_completed =
		status === "Work Completed" && doc.vehicle_location === "Completed / Parked in Lot";
	const is_in_progress =
		["Authorized - In Progress", "Waiting on Parts"].includes(status) || is_in_bay;
	const is_approved =
		doc.approval_status === "Approved" || doc.approval_status === "Partially Approved";

	if (is_released || is_billed) {
		return "tab_billing";
	} else if (is_in_bay) {
		// As long as vehicle is in the bay, stay on workshop tab so technician can complete tasks and park vehicle
		return "tab_workshop";
	} else if (is_work_completed) {
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
	const is_in_bay = frm.doc.vehicle_location === "In Bay / Being Serviced";
	const is_in_progress = ["Authorized - In Progress", "Waiting on Parts"].includes(status) || is_in_bay;
	const is_work_completed =
		["Work Completed", "Billed / Paid", "Closed / Released"].includes(status) ||
		frm.doc.vehicle_location === "Completed / Parked in Lot" ||
		!!frm.doc.vehicle_released;
	const is_cancelled = status === "Cancelled";

	const show_workshop = is_approved || is_in_progress || is_in_bay || is_work_completed || is_cancelled;
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

frappe.provide("frappe.ui.form");

if (!frappe.ui.form.RepairVehicleQuickEntryForm) {
	frappe.ui.form.RepairVehicleQuickEntryForm = class RepairVehicleQuickEntryForm extends frappe.ui.form.QuickEntryForm {
		render_dialog() {
			if (!this.doc.customer) {
				const ro_customer =
					(frappe._from_link?.doc?.doctype === "Repair Order" && frappe._from_link.doc.customer) ||
					(cur_frm?.doctype === "Repair Order" && cur_frm.doc?.customer);
				if (ro_customer) {
					this.doc.customer = ro_customer;
				}
			}
			super.render_dialog();
			if (this.doc.customer && this.fields_dict?.customer && !this.fields_dict.customer.get_value()) {
				this.fields_dict.customer.set_input(this.doc.customer);
			}
		}
	};
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

		frm.trigger("set_route_options_for_new_doc");
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

		frappe.realtime.off("repair_order_updated");
		frappe.realtime.on("repair_order_updated", (data) => {
			if (data && frm.doc && data.docname === frm.doc.name) {
				frappe.show_alert({
					message: __("Tesla Procedure Imported: {0} ({1} hrs)", [data.service_title, data.frt]),
					indicator: "green",
				}, 5);
				frm.reload_doc();
			}
		});
	},

	on_tab_change(frm) {
		// Native Frappe tab change hook
	},

	refresh(frm) {
		frm.trigger("render_progress_indicator");
		frm.trigger("render_workshop_tasks");
		frm._original_odometer_in = frm.doc.odometer_in;
		update_tab_visibility(frm);

		if (!frm.doc.approval_status || frm.doc.approval_status === "Draft") {
			frm.set_value("approval_status", "Estimating");
		}

		if (frm.fields_dict.services && frm.fields_dict.services.grid) {
			const grid = frm.fields_dict.services.grid;
			const sp_col = grid.get_field("service_package");
			if (sp_col && !sp_col._ism_formatter_set) {
				sp_col._ism_formatter_set = true;
				sp_col.formatter = function (value, df, options, doc) {
					if (!value && doc && doc.service_title) {
						return `<span style="font-weight: 500; color: #1e293b;">${frappe.utils.escape_html(doc.service_title)}</span>`;
					}
					return frappe.form.formatters.Link ? frappe.form.formatters.Link(value, df, options, doc) : (value || "");
				};
			}

			if (!grid._opposite_side_bound) {
				grid._opposite_side_bound = true;
				grid.wrapper.on("click", ".btn-grid-add-opposite-side", function (e) {
					e.preventDefault();
					e.stopPropagation();
					const row_name = $(this).attr("data-row-name");
					if (!row_name) return;

					const perform_duplicate = () => {
						frm.call({
							doc: frm.doc,
							method: "duplicate_service_for_opposite_side",
							args: { service_row_id: row_name },
							freeze: true,
							freeze_message: __("Adding opposite side service..."),
							callback(r) {
								if (r.message && r.message.status === "success") {
									frappe.show_alert({
										message: __("Added opposite side: {0}", [r.message.service_title]),
										indicator: "green",
									});
									frm.reload_doc();
								}
							},
						});
					};

					if (frm.is_dirty() || String(row_name).startsWith("new-")) {
						frm.save().then(() => perform_duplicate());
					} else {
						perform_duplicate();
					}
				});
			}

			const pos_col = grid.get_field("service_position");
			if (pos_col && !pos_col._ism_formatter_set) {
				pos_col._ism_formatter_set = true;
				pos_col.formatter = function (value, df, options, doc) {
					if (!value || value === "None") return `<span class="text-muted">-</span>`;
					const opp = value === "LH" ? "RH" : (value === "RH" ? "LH" : null);
					if (opp && doc && doc.name) {
						return `
							<span class="badge" style="background: #e0f2fe; color: #0369a1; font-weight: 600;">${value}</span>
							<button type="button" class="btn btn-default btn-xs btn-grid-add-opposite-side" data-row-name="${doc.name}" title="${__("Add Opposite Side ({0})", [opp])}" style="padding: 1px 4px; margin-left: 4px; font-size: 10px; border-radius: 3px; line-height: 1;">
								+${opp}
							</button>
						`;
					}
					return `<span class="badge" style="background: #f1f5f9; color: #475569;">${value}</span>`;
				};
			}
		}

		frm.trigger("set_route_options_for_new_doc");

		if (frm._user_clicked_tab) {
			select_tab(frm, frm._user_clicked_tab);
		} else {
			auto_select_active_tab(frm);
		}

		if (!frm.is_new() && frm.doc.status !== "Cancelled" && frm.doc.docstatus !== 2) {
			try {
				localStorage.setItem("ism_active_ro", JSON.stringify({
					name: frm.doc.name,
					customer: frm.doc.customer,
					vehicle: frm.doc.vehicle,
					status: frm.doc.status,
					timestamp: Date.now()
				}));
			} catch (e) {}

			const is_approved = frm.doc.approval_status === "Approved" || frm.doc.approval_status === "Partially Approved";
			const is_work_completed = frm.doc.status === "Work Completed" || frm.doc.vehicle_location === "Completed / Parked in Lot";
			const is_released = !!frm.doc.vehicle_released || frm.doc.status === "Closed / Released";

			// 1. Service Packages & Supplements (available before vehicle is released)
			if (!is_released) {
				frm.add_custom_button(__("Tesla Manual"), () => {
					frm.trigger("open_tesla_manual");
				});

				frm.add_custom_button(__("Add Service Package"), () => {
					frm.trigger("open_service_package_dialog");
				}, __("Jobs & Packages"));

				frm.add_custom_button(__("Tesla Manual"), () => {
					frm.trigger("open_tesla_manual");
				}, __("Jobs & Packages"));

				frm.add_custom_button(__("Import Tesla URL"), () => {
					frm.trigger("open_import_tesla_url_dialog");
				}, __("Jobs & Packages"));

				frm.add_custom_button(__("Add Supplement"), () => {
					frm.trigger("open_add_supplement_dialog");
				}, __("Jobs & Packages"));

				frm.add_custom_button(__("Deferred Services"), () => {
					frm.trigger("show_deferred_services_dialog_manual");
				}, __("Jobs & Packages"));

				if (frm.fields_dict.services && frm.fields_dict.services.grid) {
					frm.fields_dict.services.grid.add_custom_button(__("Tesla Manual"), () => {
						frm.trigger("open_tesla_manual");
					});
					frm.fields_dict.services.grid.add_custom_button(__("Import Tesla URL"), () => {
						frm.trigger("open_import_tesla_url_dialog");
					});
					frm.fields_dict.services.grid.add_custom_button(__("Deferred Services"), () => {
						frm.trigger("show_deferred_services_dialog_manual");
					});
				}
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
					frm.add_custom_button(__("Park Vehicle in Lot"), () => {
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

			// 6. Cancellation (available before vehicle is released and when not already cancelled)
			if (!is_released && frm.doc.status !== "Cancelled") {
				frm.add_custom_button(__("Cancel Order"), () => {
					frappe.confirm(__("Are you sure you want to cancel this Repair Order?"), () => {
						frm.call({
							doc: frm.doc,
							method: "cancel_repair_order",
							freeze: true,
							freeze_message: __("Cancelling Repair Order..."),
							callback(r) {
								frm.reload_doc();
							},
						});
					});
				});
			}

			if (frm.doc.status === "Cancelled") {
				frm.disable_save();
			}
		}
	},

	customer(frm) {
		frm.trigger("set_route_options_for_new_doc");
	},

	set_route_options_for_new_doc(frm) {
		const get_options = (control) => {
			const route_options = {};
			if (frm.doc && frm.doc.customer) {
				route_options.customer = frm.doc.customer;
			}
			const entered_value = control?.get_label_value?.() || frm.fields_dict?.vehicle?.get_label_value?.();
			if (entered_value && entered_value !== "create_new__link_option") {
				route_options.vin = entered_value;
			}
			return route_options;
		};

		const vehicle_docfield = frm.get_docfield("vehicle");
		if (vehicle_docfield) {
			vehicle_docfield.get_route_options_for_new_doc = get_options;
		}
		if (frm.fields_dict && frm.fields_dict.vehicle && frm.fields_dict.vehicle.df) {
			frm.fields_dict.vehicle.df.get_route_options_for_new_doc = get_options;
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
			frm.trigger("show_deferred_services_for_vehicle");
		} else {
			frm.set_df_property("customer", "description", "");
		}
	},

	show_deferred_services_for_vehicle(frm) {
		fetch_and_show_deferred_services(frm, false);
	},

	show_deferred_services_dialog_manual(frm) {
		fetch_and_show_deferred_services(frm, true);
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

	render_workshop_tasks(frm) {
		if (!frm.fields_dict.workshop_tasks_html) return;
		const $wrapper = frm.fields_dict.workshop_tasks_html.$wrapper;
		if (!$wrapper) return;

		if (frm.is_new()) {
			$wrapper.html(
				`<div class="text-muted" style="padding: 18px; text-align: center; border: 1px dashed #cbd5e1; border-radius: 8px; font-size: 13px; background: #f8fafc;">
					${__("Save the Repair Order to enable workshop task execution and service checklist.")}
				</div>`
			);
			return;
		}

		const services = frm.doc.services || [];
		if (services.length === 0) {
			$wrapper.html(
				`<div class="text-muted" style="padding: 22px; text-align: center; border: 1px dashed #cbd5e1; border-radius: 8px; font-size: 13px; background: #f8fafc;">
					${__("No services added to this order. Add services under the Estimate & Authorization tab.")}
				</div>`
			);
			return;
		}

		const approved = services.filter((s) => s.status === "Approved");
		const declined = services.filter((s) => s.status === "Declined");
		const pending = services.filter((s) => ["Draft", "Estimating", "Pending Approval"].includes(s.status));
		const completed = approved.filter((s) => !!s.is_completed);

		const approved_count = approved.length;
		const completed_count = completed.length;
		const progress_pct = approved_count > 0 ? Math.round((completed_count / approved_count) * 100) : 0;
		const is_all_completed = approved_count > 0 && completed_count === approved_count && pending.length === 0;
		const in_bay = frm.doc.vehicle_location === "In Bay / Being Serviced";
		const is_released = !!frm.doc.vehicle_released || frm.doc.status === "Closed / Released";

		let html = `<div class="workshop-execution-board" style="padding: 4px 0 12px 0;">`;

		// 1. Progress & Summary Bar
		html += `
			<div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 18px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
				<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
					<div>
						<span style="font-size: 14px; font-weight: 700; color: #1e293b;">
							${__("Shop Floor Execution:")} ${completed_count} ${__("of")} ${approved_count} ${__("Services Completed")}
						</span>
						<span class="text-muted small" style="margin-left: 8px;">
							(${approved_count - completed_count} ${__("remaining")})
						</span>
					</div>
					<div style="font-size: 13px; font-weight: 700; color: ${progress_pct === 100 ? "#16a34a" : "#2563eb"};">
						${progress_pct}%
					</div>
				</div>
				<div class="progress" style="height: 10px; margin: 0; background: #e2e8f0; border-radius: 5px; overflow: hidden;">
					<div class="progress-bar ${progress_pct === 100 ? "bg-success" : "bg-primary"}" role="progressbar" style="width: ${progress_pct}%; transition: width 0.3s ease;"></div>
				</div>
			</div>
		`;

		// 2. Action Handoff Banners
		if (is_all_completed && !is_released && frm.doc.vehicle_location !== "Completed / Parked in Lot") {
			html += `
				<div style="background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px; padding: 12px 18px; margin-bottom: 16px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
					<div>
						<div style="font-weight: 700; color: #166534; font-size: 14px;">
							✓ ${__("All Approved Services Completed!")}
						</div>
						<div style="color: #15803d; font-size: 12px; margin-top: 2px;">
							${__("All tasks have been signed off. Park vehicle in lot to complete workshop execution and advance to billing.")}
						</div>
					</div>
					<button type="button" class="btn btn-sm btn-success btn-board-park-vehicle" style="font-weight: 600; padding: 7px 16px; border-radius: 6px; box-shadow: 0 1px 2px rgba(0,0,0,0.05);">
						🅿️ ${__("Park Vehicle in Lot")}
					</button>
				</div>
			`;
		} else if (frm.doc.vehicle_location === "Completed / Parked in Lot" && !is_released) {
			html += `
				<div style="background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px; padding: 12px 18px; margin-bottom: 16px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
					<div>
						<div style="font-weight: 700; color: #166534; font-size: 14px;">
							🅿️ ${__("Vehicle Parked in Customer Lot (Completed)")}
						</div>
						<div style="color: #15803d; font-size: 12px; margin-top: 2px;">
							${__("All authorized services have been completed and vehicle is parked in the lot. Ready for invoicing and release.")}
						</div>
					</div>
					<button type="button" class="btn btn-sm btn-primary btn-board-go-to-billing" style="font-weight: 600; padding: 7px 16px; border-radius: 6px;">
						💳 ${__("Go to Billing & Release")}
					</button>
				</div>
			`;
		} else if (in_bay && !is_released) {
			html += `
				<div style="background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 12px 18px; margin-bottom: 16px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
					<div>
						<div style="font-weight: 700; color: #1e40af; font-size: 14px;">
							🔧 ${__("Vehicle is In Bay / Being Serviced")}
						</div>
						<div style="color: #1d4ed8; font-size: 12px; margin-top: 2px;">
							${__("Work is in progress. Return vehicle to the lot if service is paused or non-consecutive.")}
						</div>
					</div>
					<button type="button" class="btn btn-sm btn-outline-secondary btn-board-park-vehicle" style="font-weight: 600; padding: 7px 16px; border-radius: 6px; background: #fff;">
						🅿️ ${__("Park Vehicle in Lot")}
					</button>
				</div>
			`;
		} else if (frm.doc.vehicle_location === "In Customer Lot" && approved_count > 0 && !is_released) {
			html += `
				<div style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 12px 18px; margin-bottom: 16px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
					<div>
						<div style="font-weight: 700; color: #92400e; font-size: 14px;">
							🚗 ${__("Vehicle is in Customer Lot")}
						</div>
						<div style="color: #b45309; font-size: 12px; margin-top: 2px;">
							${__("Jobs are authorized. Move vehicle into the bay to record start time and commence work.")}
						</div>
					</div>
					<button type="button" class="btn btn-sm btn-primary btn-board-pickup-vehicle" style="font-weight: 600; padding: 7px 16px; border-radius: 6px;">
						${__("Pick Up Vehicle (Into Bay)")}
					</button>
				</div>
			`;
		}

		// 3. Approved Services Cards List
		if (approved.length === 0) {
			html += `
				<div style="background: #fff7ed; border: 1px solid #ffedd5; border-radius: 8px; padding: 16px; text-align: center; color: #9a3412; font-size: 13px; margin-bottom: 16px;">
					⚠️ ${__("No approved services found on this repair order. Obtain customer authorization before performing bay work.")}
				</div>
			`;
		} else {
			approved.forEach((s) => {
				const is_done = !!s.is_completed;
				let parts = [];
				try {
					parts = typeof s.parts === "string" ? JSON.parse(s.parts) : (s.parts || []);
				} catch (e) {
					parts = [];
				}

				html += `
					<div class="workshop-task-card" data-service-id="${frappe.utils.escape_html(s.name)}" style="background: ${is_done ? "#f8fdf9" : "#ffffff"}; border: 1.5px solid ${is_done ? "#86efac" : "#e2e8f0"}; border-radius: 8px; padding: 14px 18px; margin-bottom: 12px; box-shadow: 0 1px 2px rgba(0,0,0,0.03); transition: all 0.15s ease;">
						<div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap;">
							<!-- Left Column: Service Information -->
							<div style="flex: 1; min-width: 260px;">
								<div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px;">
									<span style="font-size: 15px; font-weight: 700; color: #1e293b; ${is_done ? "text-decoration: line-through; color: #64748b;" : ""}">
										${frappe.utils.escape_html(s.service_title)}
									</span>
									${s.category ? `
									<span class="badge" style="background: #f1f5f9; color: #475569; font-size: 11px; padding: 3px 8px; border-radius: 4px; border: 1px solid #e2e8f0;">
										${frappe.utils.escape_html(s.category)}
									</span>
									` : ""}
									${s.correction_code ? `
										<span class="badge" style="background: #eef2ff; color: #4338ca; font-size: 11px; padding: 3px 8px; border-radius: 4px; border: 1px solid #c7d2fe;">
											${frappe.utils.escape_html(s.correction_code)}
										</span>
									` : ""}
									${s.is_supplement ? `
										<span class="badge" style="background: #fef3c7; color: #92400e; font-size: 11px; padding: 3px 8px; border-radius: 4px;">
											${__("Supplement")}
										</span>
									` : ""}
									${s.is_flat_rate ? `
										<span class="badge" style="background: #f0fdf4; color: #166534; font-size: 11px; padding: 3px 8px; border-radius: 4px;">
											${__("Flat Rate")}
										</span>
									` : ""}
									${(s.labor_hours || s.frt) ? `
										<span style="font-size: 11px; color: #64748b; font-weight: 600;">
											FRT: ${s.labor_hours || s.frt} hrs
										</span>
									` : ""}
								</div>

								<!-- Parts Checklist Details -->
								${parts.length > 0 ? `
									<div style="margin-top: 8px; font-size: 12px; color: #475569;">
										<span style="font-weight: 600; color: #334155; margin-right: 4px;">${__("Required Parts:")}</span>
										${parts.map((p) => `
											<span class="badge badge-light" style="background: #f8fafc; border: 1px solid #cbd5e1; color: #334155; font-size: 11px; margin-right: 5px; margin-top: 3px; padding: 3px 7px;">
												📦 ${p.qty || 1}x ${frappe.utils.escape_html(p.item_name || p.item_code)}
											</span>
										`).join("")}
									</div>
								` : ""}

								<!-- Advisor Instructions / Notes -->
								${s.advisor_notes ? `
									<div style="margin-top: 8px; font-size: 12px; color: #334155; background: #f8fafc; border-left: 3px solid #94a3b8; padding: 5px 10px; border-radius: 0 4px 4px 0;">
										<strong>${__("Advisor Note:")}</strong> ${frappe.utils.escape_html(s.advisor_notes)}
									</div>
								` : ""}

								<!-- Execution Metadata -->
								${is_done ? `
									<div style="margin-top: 8px; font-size: 12px; color: #166534; font-weight: 600; display: flex; align-items: center; gap: 6px;">
										<span>✓ ${__("Completed by")} <b>${frappe.utils.escape_html(s.completed_by || frappe.session.user)}</b></span>
										${s.completed_at ? `<span>${__("at")} ${frappe.datetime.str_to_user(s.completed_at)}</span>` : ""}
										${s.actual_duration_hours ? `<span>(${s.actual_duration_hours} ${__("bay hrs")})</span>` : ""}
									</div>
								` : (s.assigned_technician ? `
									<div style="margin-top: 6px; font-size: 12px; color: #64748b;">
										${__("Assigned Technician:")} <b style="color: #334155;">${frappe.utils.escape_html(s.assigned_technician)}</b>
									</div>
								` : "")}
							</div>

							<!-- Right Column: Tactile Toggle and Manual Link -->
							<div style="display: flex; flex-direction: column; align-items: flex-end; gap: 6px;">
								<button type="button" class="btn btn-sm ${is_done ? "btn-success" : "btn-outline-success"} btn-board-toggle-completion" data-service-id="${frappe.utils.escape_html(s.name)}" data-action="${is_done ? "reopen" : "complete"}" style="min-width: 145px; height: 38px; font-weight: 600; font-size: 13px; border-radius: 6px; display: flex; align-items: center; justify-content: center; gap: 6px; border-width: 1.5px; box-shadow: 0 1px 2px rgba(0,0,0,0.04);">
									${is_done ? `
										<span style="font-size: 14px;">✓</span> ${__("Completed")}
									` : `
										<span style="font-size: 14px;">○</span> ${__("Mark Complete")}
									`}
								</button>
								${s.tesla_manual_url ? `
									<a href="${frappe.utils.escape_html(s.tesla_manual_url)}" target="_blank" rel="noopener noreferrer" class="btn btn-xs btn-default" style="width: 145px; font-size: 11px; padding: 4px 6px; text-align: center; color: #475569; border-radius: 4px; border: 1px solid #cbd5e1; text-decoration: none;">
										📘 ${__("Tesla Manual")}
									</a>
								` : ""}
							</div>
						</div>
					</div>
				`;
			});
		}

		// 4. Always-Unfolded Unapproved, Estimating & Declined Services Panel (if any)
		const other_services = services.filter((s) => s.status !== "Approved");
		if (other_services.length > 0) {
			html += `
				<div style="margin-top: 18px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 18px;">
					<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; border-bottom: 1px solid #e2e8f0; padding-bottom: 8px;">
						<span style="font-size: 13px; font-weight: 700; color: #475569;">
							⚠️ ${__("Unapproved, Estimating & Declined Services")} (${other_services.length})
						</span>
						<span class="text-muted small" style="font-style: italic;">
							${__("Not authorized for shop floor execution")}
						</span>
					</div>
					<div>
						${other_services.map((s) => `
							<div style="display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #edf2f7; font-size: 12px;">
								<div>
									<span style="font-weight: 600; color: #334155;">${frappe.utils.escape_html(s.service_title)}</span>
									${s.is_supplement ? `
										<span class="badge" style="background: #fef3c7; color: #92400e; font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-left: 4px;">
											${__("Supplement")}
										</span>
									` : ""}
									<span class="badge ${s.status === "Declined" ? "badge-danger" : s.status === "Estimating" ? "badge-info" : s.status === "Draft" ? "badge-secondary" : "badge-warning"}" style="margin-left: 6px; font-size: 10px;">
										${frappe.utils.escape_html(s.status)}
									</span>
									${s.total_amount ? `
										<span style="margin-left: 8px; color: #64748b; font-weight: 500;">
											$${frappe.format(s.total_amount, { fieldtype: "Currency" })}
										</span>
									` : ""}
								</div>
								<span class="text-muted" style="font-size: 11px;">
									${s.status === "Declined" ? __("Logged to Vehicle Deferred Services") : s.status === "Estimating" ? __("Estimating (Internal)") : s.status === "Draft" ? __("Internal Draft (Unsent)") : __("Sent to Customer (Pending Approval)")}
								</span>
							</div>
						`).join("")}
					</div>
				</div>
			`;
		}

		html += `</div>`;
		$wrapper.html(html);

		// Event Bindings
		$wrapper.find(".btn-board-toggle-completion").on("click", function (e) {
			e.preventDefault();
			const $btn = $(this);
			const service_id = $btn.attr("data-service-id");
			const action = $btn.attr("data-action");
			const is_complete = action === "complete" ? 1 : 0;

			frm._user_clicked_tab = "tab_workshop";
			frm.call({
				doc: frm.doc,
				method: "toggle_service_completion",
				args: {
					service_row_id: service_id,
					is_completed: is_complete,
				},
				freeze: true,
				freeze_message: is_complete ? __("Marking service complete...") : __("Re-opening service..."),
				callback(r) {
					if (r.message && (r.message.status === "success" || r.message.progress_percent !== undefined)) {
						frm._user_clicked_tab = "tab_workshop";
						frm.reload_doc().then(() => {
							select_tab(frm, "tab_workshop");
						});
					}
				},
			});
		});

		$wrapper.find(".btn-board-park-vehicle").on("click", function (e) {
			e.preventDefault();
			frm._user_clicked_tab = "tab_workshop";
			frm.call({
				doc: frm.doc,
				method: "park_vehicle",
				freeze: true,
				freeze_message: __("Parking vehicle in lot..."),
				callback() {
					frm._user_clicked_tab = null;
					frm.reload_doc();
				},
			});
		});

		$wrapper.find(".btn-board-go-to-billing").on("click", function (e) {
			e.preventDefault();
			frm._user_clicked_tab = "tab_billing";
			select_tab(frm, "tab_billing");
		});

		$wrapper.find(".btn-board-pickup-vehicle").on("click", function (e) {
			e.preventDefault();
			frm._user_clicked_tab = "tab_workshop";
			frm.call({
				doc: frm.doc,
				method: "pick_up_vehicle",
				freeze: true,
				freeze_message: __("Picking up vehicle into bay..."),
				callback() {
					frm._user_clicked_tab = "tab_workshop";
					frm.reload_doc();
				},
			});
		});
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
					onchange() {
						const pkg_name = dialog.get_value("service_package");
						if (pkg_name) {
							frappe.db.get_value("Service Package", pkg_name, ["is_symmetrical", "package_name"], (r) => {
								if (r && cint(r.is_symmetrical)) {
									dialog.set_df_property("side_section", "hidden", false);
									dialog.set_df_property("side", "hidden", false);
								} else {
									dialog.set_df_property("side_section", "hidden", true);
									dialog.set_df_property("side", "hidden", true);
								}
							});
						} else {
							dialog.set_df_property("side_section", "hidden", true);
							dialog.set_df_property("side", "hidden", true);
						}
					},
				},
				{
					fieldtype: "Section Break",
					fieldname: "side_section",
					label: __("Symmetrical Operation Side"),
					hidden: true,
				},
				{
					fieldname: "side",
					fieldtype: "Select",
					label: __("Side Selection"),
					options: "Both Sides\nLH (Left Hand)\nRH (Right Hand)",
					default: "Both Sides",
					hidden: true,
					description: __("This procedure is symmetrical. Choose whether to add both sides or a specific side."),
				},
			],
			primary_action_label: __("Add to Order"),
			primary_action(values) {
				let side_arg = "both";
				if (values.side === "LH (Left Hand)") side_arg = "lh";
				else if (values.side === "RH (Right Hand)") side_arg = "rh";
				else if (values.side === "Both Sides") side_arg = "both";

				frm.call("add_service_package", {
					service_package: values.service_package,
					side: side_arg,
				}).then(() => {
					dialog.hide();
					frm.reload_doc();
				});
			},
		});
		dialog.show();
	},

	open_tesla_manual(frm) {
		frappe.call({
			method: "ism.api.tesla.get_vehicle_manual_url",
			args: {
				vehicle: frm.doc.vehicle,
				repair_order: frm.doc.name
			},
			callback(r) {
				let url = (r && r.message) ? r.message : "https://service.tesla.com/docs/";
				if (frm.doc && frm.doc.name) {
					const hashParam = `ism_ro=${encodeURIComponent(frm.doc.name)}`;
					url = url.includes("#") ? `${url}&${hashParam}` : `${url}#${hashParam}`;
				}
				try {
					localStorage.setItem("ism_active_ro", JSON.stringify({
						name: frm.doc.name,
						customer: frm.doc.customer,
						vehicle: frm.doc.vehicle,
						status: frm.doc.status,
						manual_url: url,
						timestamp: Date.now()
					}));
				} catch (e) {}
				window.open(url, "_blank");
			}
		});
	},

	open_import_tesla_url_dialog(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Import Tesla Manual Procedure"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "help_text",
					options: `<div class="text-muted small mb-2">${__(
						"Paste the URL of any procedure from the Tesla Service Manual (e.g. https://service.tesla.com/docs/Model3/ServiceManual/.../GUID-XXXX.html). The Title, Correction Code, and Flat Rate Time (FRT) will be extracted and added to this Repair Order."
					)}</div>`
				},
				{
					fieldname: "tesla_url",
					fieldtype: "Data",
					label: __("Tesla Procedure URL"),
					reqd: 1,
					placeholder: "https://service.tesla.com/docs/..."
				},
				{
					fieldname: "side",
					fieldtype: "Select",
					label: __("Side (for symmetrical procedures)"),
					options: "Both Sides\nLH (Left Hand)\nRH (Right Hand)",
					default: "Both Sides",
					description: __("If the procedure is symmetrical (e.g. Door, Suspension, Mirror), select which side(s) to add."),
				}
			],
			primary_action_label: __("Fetch & Import"),
			primary_action(values) {
				let side_arg = "both";
				if (values.side === "LH (Left Hand)") side_arg = "lh";
				else if (values.side === "RH (Right Hand)") side_arg = "rh";
				else if (values.side === "Both Sides") side_arg = "both";

				const do_import = (allow_mismatch = 0, duplicate_action = null) => {
					frappe.call({
						method: "ism.api.tesla.import_tesla_procedure",
						args: {
							repair_order: frm.doc.name,
							url: values.tesla_url.trim(),
							side: side_arg,
							allow_generation_mismatch: allow_mismatch,
							duplicate_action: duplicate_action,
						},
						freeze: true,
						freeze_message: __("Fetching and parsing Tesla manual procedure..."),
						callback(r) {
							if (r && r.message) {
								if (r.message.status === "warning" && r.message.requires_confirmation) {
									frappe.confirm(
										`<b>${__("Generation / Model Mismatch")}</b><br><br>${r.message.warning}<br><br>${__("Do you still want to import this procedure?")}`,
										() => {
											do_import(1, duplicate_action);
										}
									);
									return;
								}
								if (r.message.status === "duplicate_warning" && r.message.requires_duplicate_choice) {
									const prompt_msg = r.message.message || __("Correction code {0} is already on this Repair Order. Update existing row or Add as additional job?", [r.message.correction_code]);
									const d = new frappe.ui.Dialog({
										title: __("Duplicate Procedure"),
										fields: [
											{
												fieldtype: "HTML",
												fieldname: "msg",
												options: `<div style="font-size: 13px; line-height: 1.5; padding: 10px 0;">${prompt_msg}</div>`
											}
										],
										primary_action_label: __("Update Existing Row"),
										primary_action() {
											d.hide();
											do_import(allow_mismatch, "update");
										},
										secondary_action_label: __("Add as Additional Job"),
										secondary_action() {
											d.hide();
											do_import(allow_mismatch, "add");
										}
									});
									d.show();
									return;
								}
								if (r.message.status === "success") {
									dialog.hide();
									const action_text = r.message.action === "updated" ? __("Updated") : __("Imported");
									frappe.show_alert({
										message: `${action_text}: ${r.message.service_package} (${r.message.frt} hrs)`,
										indicator: "green"
									}, 5);
									frm.reload_doc();
								}
							}
						}
					});
				};
				do_import(0);
			}
		});
		dialog.show();
	},

	open_send_approval_dialog(frm) {
		frm.call("generate_approval_token").then((r) => {
			if (r && r.message) {
				frm.reload_doc();
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
					if (pkg.correction_code) frappe.model.set_value(cdt, cdn, "correction_code", pkg.correction_code);
					if (pkg.service_position) frappe.model.set_value(cdt, cdn, "service_position", pkg.service_position);
					if (pkg.is_mobile_capable !== undefined) frappe.model.set_value(cdt, cdn, "is_mobile_capable", pkg.is_mobile_capable);
					if (pkg.tesla_manual_url) frappe.model.set_value(cdt, cdn, "tesla_manual_url", pkg.tesla_manual_url);
					frappe.model.set_value(cdt, cdn, "labor_hours", flt(pkg.standard_labor_hours || pkg.labor_hours || pkg.frt));
					frappe.model.set_value(cdt, cdn, "labor_rate", flt(pkg.hourly_rate));
					frappe.model.set_value(cdt, cdn, "labor_amount", labor_amt);
					frappe.model.set_value(cdt, cdn, "parts", JSON.stringify(pkg.parts || []));
					frappe.model.set_value(cdt, cdn, "parts_total", parts_tot);
					frappe.model.set_value(cdt, cdn, "total_amount", labor_amt + parts_tot);
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
		const current_tab = frm._user_clicked_tab || "tab_estimate";
		frm._user_clicked_tab = current_tab;
		frm.call({
			doc: frm.doc,
			method: "toggle_service_completion",
			args: { service_row_id: row.name, is_completed: row.is_completed },
		}).then(() => {
			frm.reload_doc().then(() => {
				select_tab(frm, current_tab);
			});
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

	btn_add_opposite_side(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.service_position || (row.service_position !== "LH" && row.service_position !== "RH")) {
			frappe.msgprint(__("This service does not have an LH or RH position set."));
			return;
		}
		const perform_duplicate = () => {
			frm.call({
				doc: frm.doc,
				method: "duplicate_service_for_opposite_side",
				args: {
					service_row_id: row.name,
				},
				freeze: true,
				freeze_message: __("Adding opposite side service..."),
				callback(r) {
					if (r.message && r.message.status === "success") {
						frappe.show_alert({
							message: __("Added opposite side: {0}", [r.message.service_title]),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		};

		if (frm.is_dirty() || !row.name || String(row.name).startsWith("new-")) {
			frm.save().then(() => {
				perform_duplicate();
			});
		} else {
			perform_duplicate();
		}
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

function fetch_and_show_deferred_services(frm, is_manual = false) {
	if (!frm.doc.vehicle) {
		if (is_manual) {
			frappe.msgprint(__("Please select a vehicle first."));
		}
		return;
	}

	frappe.call({
		method: "ism.ism.doctype.repair_order.repair_order.get_deferred_services_for_vehicle",
		args: { vehicle: frm.doc.vehicle },
		callback(r) {
			const deferred_list = (r && r.message) || [];
			if (deferred_list.length === 0) {
				if (is_manual) {
					frappe.msgprint({
						title: __("Deferred Services History"),
						message: __("No open deferred services found for vehicle <b>{0}</b>.", [frm.doc.vehicle]),
						indicator: "blue",
					});
				}
				return;
			}

			open_deferred_services_modal(frm, deferred_list);
		},
	});
}

function open_deferred_services_modal(frm, deferred_list) {
	const currency = frm.doc.currency || (frappe.boot && frappe.boot.sysdefaults && frappe.boot.sysdefaults.currency) || "CAD";
	const existing_titles = (frm.doc.services || []).map((s) => (s.service_title || "").trim().toLowerCase());

	let all_already_added = true;
	deferred_list.forEach((d) => {
		const title_clean = (d.job_title || d.service_title || "").trim().toLowerCase();
		d._already_added = existing_titles.includes(title_clean);
		if (!d._already_added) {
			all_already_added = false;
		}
	});

	const items_html = deferred_list.map((d, idx) => {
		const title = frappe.utils.escape_html(d.job_title || d.service_title || __("Deferred Service"));
		const amount_val = flt(d.estimated_amount != null ? d.estimated_amount : d.estimated_cost);
		const formatted_cost = format_currency(amount_val, currency);

		const meta = [];
		if (d.decline_date) {
			meta.push(`<span><i class="fa fa-calendar text-muted"></i> ${frappe.datetime.str_to_user(d.decline_date)}</span>`);
		}
		if (d.repair_order) {
			meta.push(`<span><i class="fa fa-file-text-o text-muted"></i> ${frappe.utils.escape_html(d.repair_order)}</span>`);
		}
		if (d.safety_hazard) {
			meta.push(`<span class="indicator-pill red" style="font-size: 11px; padding: 2px 6px;">${__("Safety Hazard")}</span>`);
		}

		let reason_html = "";
		if (d.decline_reason && d.decline_reason !== "Declined during customer authorization") {
			reason_html = `<div style="font-size: 12px; color: var(--text-muted); margin-top: 4px; font-style: italic;">${frappe.utils.escape_html(d.decline_reason)}</div>`;
		}

		const chk_id = `deferred_item_${idx}`;
		const is_checked = !d._already_added;
		const disabled_attr = d._already_added ? "disabled" : "";
		const card_opacity = d._already_added ? "opacity: 0.75;" : "";

		return `
			<div class="deferred-service-card" style="padding: 12px 14px; margin-bottom: 8px; border: 1px solid var(--border-color); border-radius: var(--border-radius-md, 6px); background: var(--bg-light-gray, #f8f9fa); ${card_opacity}">
				<div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px;">
					<div style="display: flex; align-items: flex-start; gap: 10px; flex: 1;">
						<input type="checkbox" id="${chk_id}" data-idx="${idx}" class="deferred-service-chk" style="margin-top: 4px; cursor: pointer; transform: scale(1.15);" ${is_checked ? "checked" : ""} ${disabled_attr}>
						<label for="${chk_id}" style="margin: 0; cursor: pointer; flex: 1;">
							<span style="font-weight: 600; color: var(--text-color); font-size: 14px;">${title}</span>
							${d._already_added ? `<span class="indicator-pill green" style="margin-left: 8px; font-size: 11px; padding: 2px 6px;">${__("In Current Order")}</span>` : ""}
						</label>
					</div>
					<div style="display: flex; align-items: center; gap: 8px;">
						<span style="font-weight: 700; color: var(--text-color); font-size: 14px; white-space: nowrap;">${formatted_cost}</span>
						${!d._already_added ? `<button type="button" class="btn btn-xs btn-default btn-add-single-deferred" data-idx="${idx}" style="font-weight: 600;">${__("+ Add")}</button>` : ""}
					</div>
				</div>
				<div style="margin-left: 26px;">
					${meta.length > 0 ? `<div style="display: flex; gap: 12px; align-items: center; font-size: 12px; color: var(--text-muted); margin-top: 4px; flex-wrap: wrap;">${meta.join("")}</div>` : ""}
					${reason_html}
				</div>
			</div>
		`;
	}).join("");

	const d = new frappe.ui.Dialog({
		title: __("Deferred Services History"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "deferred_services_html",
				options: `
					<div style="margin-bottom: 12px;">
						<p class="text-muted" style="margin-bottom: 8px;">
							${__("This vehicle has open recommendations from previous visits. Select services to add them directly to this Repair Order:")}
						</p>
						${!all_already_added ? `
							<div style="display: flex; justify-content: flex-end; margin-bottom: 6px;">
								<a href="javascript:void(0)" class="btn-toggle-all-deferred" style="font-size: 12px; font-weight: 500;">
									${__("Toggle All")}
								</a>
							</div>
						` : ""}
						<div class="deferred-services-container" style="max-height: 400px; overflow-y: auto;">
							${items_html}
						</div>
					</div>
				`,
			},
		],
		primary_action_label: all_already_added ? __("Close") : __("Add Selected Services"),
		primary_action() {
			if (all_already_added) {
				d.hide();
				return;
			}

			const selected_indices = [];
			d.$wrapper.find(".deferred-service-chk:checked").each(function () {
				const idx = cint($(this).attr("data-idx"));
				if (!deferred_list[idx]._already_added) {
					selected_indices.push(idx);
				}
			});

			if (selected_indices.length === 0) {
				frappe.msgprint(__("Please select at least one deferred service to add."));
				return;
			}

			const to_add = selected_indices.map((idx) => deferred_list[idx]);
			execute_add_deferred_services(frm, to_add, d);
		},
		secondary_action_label: all_already_added ? null : __("Dismiss"),
		secondary_action() {
			d.hide();
		},
	});

	d.show();

	// Bind Toggle All
	d.$wrapper.find(".btn-toggle-all-deferred").on("click", function () {
		const checkboxes = d.$wrapper.find(".deferred-service-chk:not(:disabled)");
		const any_unchecked = checkboxes.toArray().some((chk) => !chk.checked);
		checkboxes.prop("checked", any_unchecked);
	});

	// Bind single item add
	d.$wrapper.find(".btn-add-single-deferred").on("click", function () {
		const idx = cint($(this).attr("data-idx"));
		const item = deferred_list[idx];
		if (!item || item._already_added) return;

		$(this).prop("disabled", true).text(__("Added ✓"));
		d.$wrapper.find(`.deferred-service-chk[data-idx="${idx}"]`).prop("disabled", true).prop("checked", true);
		item._already_added = true;

		execute_add_deferred_services(frm, [item], null);
	});
}

function execute_add_deferred_services(frm, items_to_add, d) {
	if (!items_to_add || items_to_add.length === 0) return;

	if (frm.is_new()) {
		let added_count = 0;
		const has_prior_approval = frm.doc.approval_status === "Approved"
			|| frm.doc.approval_status === "Partially Approved"
			|| (frm.doc.services || []).some((s) => s.status === "Approved");

		items_to_add.forEach((item) => {
			const title = (item.job_title || item.service_title || "").trim();
			if (!title) return;

			// Prevent duplicate additions
			const exists = (frm.doc.services || []).some(
				(s) => (s.service_title || "").trim().toLowerCase() === title.toLowerCase()
			);
			if (exists) return;

			const row = frm.add_child("services");
			row.service_title = title;
			row.is_flat_rate = 1;
			row.labor_hours = 0.0;
			row.labor_rate = 0.0;
			row.labor_amount = flt(item.estimated_amount != null ? item.estimated_amount : item.estimated_cost);
			row.parts_total = 0.0;
			row.total_amount = row.labor_amount;
			row.is_supplement = has_prior_approval ? 1 : 0;
			row.status = "Estimating";
			row.advisor_notes = "";

			added_count++;

			// If a matching Service Package exists, link it to trigger package field population
			frappe.db.get_value(
				"Service Package",
				{ package_name: title },
				"name",
				(res) => {
					const pkg_name = res && res.name;
					if (pkg_name) {
						frappe.model.set_value(row.doctype, row.name, "service_package", pkg_name);
					} else {
						frappe.db.get_value("Service Package", title, "name", (r2) => {
							if (r2 && r2.name) {
								frappe.model.set_value(row.doctype, row.name, "service_package", r2.name);
							}
						});
					}
				}
			);
		});

		if (!frm.doc.approval_status || frm.doc.approval_status === "Draft") {
			frm.set_value("approval_status", "Estimating");
		}
		frm.refresh_field("services");
		recalculate_totals(frm);
		if (d) d.hide();
		frappe.show_alert({
			message: __("Added {0} deferred service(s) to Repair Order", [added_count]),
			indicator: "green",
		});
	} else {
		frm.call({
			doc: frm.doc,
			method: "add_deferred_services",
			args: { deferred_items: items_to_add },
			freeze: true,
			freeze_message: __("Adding deferred services to Repair Order..."),
			callback(r) {
				if (r.message && r.message.status === "success") {
					frappe.show_alert({
						message: __("Added {0} deferred service(s) to Repair Order", [r.message.added_count]),
						indicator: "green",
					});
					if (d) d.hide();
					frm.reload_doc();
				} else if (r.message && r.message.status === "noop") {
					frappe.msgprint(r.message.message);
					if (d) d.hide();
				}
			},
		});
	}
}
