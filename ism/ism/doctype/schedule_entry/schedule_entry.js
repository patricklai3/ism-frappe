// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.ui.form.on('Schedule Entry', {
	refresh(frm) {
		frm.trigger('setup_field_visibility');
		frm.trigger('setup_buttons');
	},

	entry_type(frm) {
		frm.trigger('setup_field_visibility');
	},

	customer(frm) {
		frm.trigger('setup_field_visibility');
	},

	repair_vehicle(frm) {
		frm.trigger('setup_field_visibility');
	},

	repair_order(frm) {
		if (frm.doc.repair_order) {
			frappe.db.get_value('Repair Order', frm.doc.repair_order, ['customer', 'vehicle'], (r) => {
				if (r) {
					if (!frm.doc.customer && r.customer) {
						frm.set_value('customer', r.customer);
					}
					if (!frm.doc.repair_vehicle && r.vehicle) {
						frm.set_value('repair_vehicle', r.vehicle);
					}
				}
			});
		}
	},

	setup_field_visibility(frm) {
		if (frm.doc.entry_type) {
			frappe.db.get_value('Schedule Entry Type', frm.doc.entry_type, 'requires_repair_order', (r) => {
				const reqd_ro = r && r.requires_repair_order;
				frm.set_df_property('repair_order', 'reqd', reqd_ro ? 1 : 0);
			});
		} else {
			frm.set_df_property('repair_order', 'reqd', 0);
		}

		frm.toggle_display('provisional_customer_name', !frm.doc.customer || Boolean(frm.doc.provisional_customer_name));
		frm.toggle_display('provisional_phone', !frm.doc.customer || Boolean(frm.doc.provisional_phone));
		frm.toggle_display('provisional_email', !frm.doc.customer || Boolean(frm.doc.provisional_email));
		frm.toggle_display('provisional_vehicle_info', !frm.doc.repair_vehicle || Boolean(frm.doc.provisional_vehicle_info));
	},

	setup_buttons(frm) {
		if (!frm.is_new()) {
			if (frm.doc.repair_order) {
				frm.add_custom_button(__('Open Repair Order'), function () {
					frappe.set_route('Form', 'Repair Order', frm.doc.repair_order);
				}, __('Navigate'));
			} else {
				frm.add_custom_button(__('Create Repair Order'), function () {
					frappe.confirm(
						__('Create a Repair Order from this Schedule Entry? Pre-entered notes will carry over into Customer Concern.'),
						function () {
							frappe.call({
								method: 'ism.api.scheduling.create_repair_order_from_schedule_entry',
								args: { schedule_entry: frm.doc.name },
								freeze: true,
								freeze_message: __('Creating Repair Order...'),
								callback: (r) => {
									if (r.message && r.message.repair_order) {
										frappe.show_alert({
											message: __('Repair Order {0} created.', [r.message.repair_order]),
											indicator: 'green',
										});
										frappe.set_route('Form', 'Repair Order', r.message.repair_order);
									}
								},
							});
						}
					);
				});
			}
		}
	}
});
