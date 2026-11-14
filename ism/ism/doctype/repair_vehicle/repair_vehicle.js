// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

const VIN_TEXT_FIELDS = [
	"manufacturer",
	"model",
	"model_year",
	"body_type",
	"trim",
	"drivetrain",
	"battery_type",
	"drive_unit",
	"assembly_plant",
	"production_sequence",
	"autopilot_hardware",
];

function clear_decoded_vin_fields(frm) {
	frm.set_value("is_valid_vin", 0);
	VIN_TEXT_FIELDS.forEach((field) => {
		if (frm.doc[field]) {
			frm.set_value(field, "");
		}
	});
}

frappe.ui.form.on("Repair Vehicle", {
	vin(frm) {
		const raw = frm.doc.vin || "";
		const clean_vin = raw.toUpperCase().replace(/[^A-HJ-NPR-Z0-9]/g, "");

		if (raw !== clean_vin) {
			frm.set_value("vin", clean_vin);
		}

		if (clean_vin.length !== 17) {
			clear_decoded_vin_fields(frm);
			return;
		}

		// Delegate Tesla VIN decoding to the server so lookup tables stay in repair_vehicle.py.
		frappe.call({
			method: "ism.api.tesla.decode_vehicle_vin",
			args: { vin: clean_vin },
			callback: (r) => {
				if (frm.doc.vin !== clean_vin || !r?.message) return;

				const decoded = r.message;
				if (!decoded.is_tesla_wmi) {
					clear_decoded_vin_fields(frm);
					if (decoded.error) {
						frappe.msgprint(__(decoded.error));
					}
					return;
				}

				frm.set_value("is_valid_vin", decoded.is_valid_vin ? 1 : 0);
				VIN_TEXT_FIELDS.forEach((field) => {
					if (decoded[field] !== undefined) {
						frm.set_value(field, decoded[field] || "");
					}
				});
			},
		});
	},
});
