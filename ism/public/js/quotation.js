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
