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

function compute_vehicle_display_preview(doc) {
	if (window.ISMVehicleDisplay && typeof window.ISMVehicleDisplay.format === "function") {
		return window.ISMVehicleDisplay.format(doc, { includeVin: false });
	}
	const clean = (v) => String(v || "").trim().replace(/\s+/g, " ");
	const year = clean(doc.model_year);
	const color = clean(doc.color);
	const model = clean(doc.model) || clean(doc.manufacturer);
	const plate = clean(doc.license_plate).toUpperCase();
	const base = [year, color, model].filter(Boolean).join(" ");
	if (base && plate) return `${base} (${plate})`;
	if (base) return base;
	if (plate) return plate;
	return clean(doc.vin) || "";
}

function sync_display_title_preview(frm) {
	const preview = compute_vehicle_display_preview(frm.doc).slice(0, 140);
	if (preview && frm.doc.display_title !== preview) {
		frm.set_value("display_title", preview);
	}
}

frappe.ui.form.on("Repair Vehicle", {
	license_plate(frm) {
		const raw = frm.doc.license_plate || "";
		const clean_plate = raw.trim().replace(/\s+/g, " ").toUpperCase();
		if (raw !== clean_plate) {
			frm.set_value("license_plate", clean_plate);
		}
		sync_display_title_preview(frm);
	},

	color(frm) {
		sync_display_title_preview(frm);
	},

	vin(frm) {
		const raw = frm.doc.vin || "";
		const clean_vin = raw.toUpperCase().replace(/[^A-HJ-NPR-Z0-9]/g, "");

		if (raw !== clean_vin) {
			frm.set_value("vin", clean_vin);
		}

		if (clean_vin.length !== 17) {
			clear_decoded_vin_fields(frm);
			sync_display_title_preview(frm);
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
					sync_display_title_preview(frm);
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
				sync_display_title_preview(frm);
			},
		});
	},
});
