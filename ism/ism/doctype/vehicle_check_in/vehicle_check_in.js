// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.ui.form.on("Vehicle Check-in", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm._original_mileage = frm.doc.intake_mileage;

				frm.add_custom_button(__("Repair Order"), () => {
					frappe.model.open_mapped_doc({
						method: "ism.ism.doctype.repair_order.repair_order.make_repair_order",
						frm: frm,
					});
				}, __("Create"));

				frm.add_custom_button(__("Quotation"), () => {
					frappe.model.open_mapped_doc({
						method: "ism.ism.doctype.vehicle_check_in.vehicle_check_in.make_quotation",
						frm: frm,
					});
				}, __("Create"));

				frm.add_custom_button(__("Check Out Vehicle"), () => {
					frappe.confirm(__("Are you sure you want to check out this vehicle?"), () => {
						frappe.call({
							method: "ism.ism.doctype.vehicle_check_in.vehicle_check_in.check_out_vehicle",
							args: { check_in_name: frm.doc.name },
							callback: () => frm.reload_doc(),
						});
					});
				});
			}
		}

		if (frm.doc.vehicle) {
			frm.trigger("load_vehicle_customers");
		}
	},

	vehicle(frm) {
		if (frm.doc.vehicle) {
			frm.trigger("load_vehicle_customers");
		} else {
			frm.set_df_property("customer", "description", "");
		}
	},

	load_vehicle_customers(frm) {
		if (!frm.doc.vehicle) return;

		frappe.call({
			method: "ism.ism.doctype.vehicle_check_in.vehicle_check_in.get_vehicle_customers",
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
				} else {
					frm.set_df_property(
						"customer",
						"description",
						__("No customer linked yet. Selected customer will be linked on save.")
					);
				}
			},
		});
	},

	intake_mileage(frm) {
		if (!frm.is_new() && frm.doc.intake_mileage !== frm._original_mileage) {
			frappe.confirm(
				__("Are you sure the original odometer entry was incorrect? If not, cancel to revert."),
				() => {
					frm._original_mileage = frm.doc.intake_mileage;
				},
				() => {
					frm.set_value("intake_mileage", frm._original_mileage);
				},
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
});
