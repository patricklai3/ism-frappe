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
	"vehicle_generation",
	"recommended_manual_url",
];

function clear_decoded_vin_fields(frm) {
	frm.set_value("is_valid_vin", 0);
	frm.dashboard?.clear_headline();
	VIN_TEXT_FIELDS.forEach((field) => {
		if (frm.doc[field]) {
			frm.set_value(field, "");
		}
	});
}

function update_generation_alert(frm, decoded) {
	if (!frm.dashboard) return;
	frm.dashboard.clear_headline();
	if (decoded?.generation_ambiguous && !frm.doc.manufactured_month) {
		const candidates = (decoded.candidate_generations || [])
			.map((entry) => entry.generation_name)
			.filter(Boolean);
		const candidateText = candidates.length
			? candidates.join(" vs ")
			: __("multiple Tesla generations");
		frm.dashboard.set_headline_alert(
			__(
				"Split transition model year detected ({0}). Enter Manufactured Month (from B-pillar label) to confirm the exact generation manual.",
				[candidateText]
			),
			"orange"
		);
	}
}

function decode_vehicle_form(frm) {
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
		args: {
			vin: clean_vin,
			manufactured_month: frm.doc.manufactured_month || "",
		},
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
			update_generation_alert(frm, decoded);
		},
	});
}

frappe.ui.form.on("Repair Vehicle", {
	refresh(frm) {
		if (frm.doc.recommended_manual_url) {
			frm.add_custom_button(__("Tesla Service Manual"), () => {
				window.open(frm.doc.recommended_manual_url, "_blank", "noopener");
			});
		}
	},

	vin(frm) {
		decode_vehicle_form(frm);
	},

	manufactured_month(frm) {
		if ((frm.doc.vin || "").trim().length === 17) {
			decode_vehicle_form(frm);
		}
	},
});
