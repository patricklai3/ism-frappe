// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

function get_sms_config_gap(doc) {
	const provider = (doc.sms_gateway_provider || "Disabled").trim();
	if (!provider || provider === "Disabled") {
		return __("SMS Gateway Provider is currently Disabled.");
	}
	if (provider === "Telnyx") {
		if (!(doc.telnyx_api_key || "").trim()) {
			return __("Telnyx API Key is required.");
		}
		if (!(doc.sms_sender_id || "").trim() && !(doc.telnyx_messaging_profile_id || "").trim()) {
			return __("Telnyx requires an E.164 Sender ID or Messaging Profile ID.");
		}
	}
	if (provider === "Custom HTTP Webhook") {
		if (!(doc.sms_gateway_endpoint || "").trim()) {
			return __("Custom HTTP Webhook requires an HTTPS SMS Gateway Endpoint URL.");
		}
	}
	return null;
}

function update_self_serve_booking_sms_notice(frm) {
	frm.set_intro(null);
	if (!cint(frm.doc.enable_self_serve_booking)) {
		return;
	}
	const gap = get_sms_config_gap(frm.doc);
	if (gap) {
		frm.set_intro(
			__(
				"Self-Serve Booking is enabled, but /book will remain unavailable until SMS is configured ({0}). Also mark at least one Service Package with 'Allow Self-Serve Booking'.",
				[gap]
			),
			"orange"
		);
	}
}

frappe.ui.form.on("ISM Settings", {
	refresh(frm) {
		update_self_serve_booking_sms_notice(frm);
	},
	enable_self_serve_booking(frm) {
		update_self_serve_booking_sms_notice(frm);
	},
	sms_gateway_provider(frm) {
		update_self_serve_booking_sms_notice(frm);
	},
	telnyx_api_key(frm) {
		update_self_serve_booking_sms_notice(frm);
	},
	sms_sender_id(frm) {
		update_self_serve_booking_sms_notice(frm);
	},
	telnyx_messaging_profile_id(frm) {
		update_self_serve_booking_sms_notice(frm);
	},
	sms_gateway_endpoint(frm) {
		update_self_serve_booking_sms_notice(frm);
	},
});
