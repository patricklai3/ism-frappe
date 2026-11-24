// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

const SE_PIN_PURGE_STATUSES = ['Completed', 'Cancelled', 'No Show'];

frappe.ui.form.on('Schedule Entry', {
	refresh(frm) {
		frm.trigger('setup_field_visibility');
		frm.trigger('setup_buttons');
		frm.trigger('setup_pin_to_drive_controls');
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
		frm.toggle_display('provisional_vehicle_model', !frm.doc.repair_vehicle || Boolean(frm.doc.provisional_vehicle_model));
		frm.toggle_display('provisional_vehicle_year', !frm.doc.repair_vehicle || Boolean(frm.doc.provisional_vehicle_year));
		frm.toggle_display('provisional_vehicle_color', !frm.doc.repair_vehicle || Boolean(frm.doc.provisional_vehicle_color));
		frm.toggle_display('provisional_license_plate', !frm.doc.repair_vehicle || Boolean(frm.doc.provisional_license_plate));
		frm.toggle_display('provisional_vehicle_generation', !frm.doc.repair_vehicle || Boolean(frm.doc.provisional_vehicle_generation));
		frm.toggle_display('provisional_vin_or_plate', !frm.doc.repair_vehicle || Boolean(frm.doc.provisional_vin_or_plate));
	},

	setup_buttons(frm) {
		if (!frm.is_new()) {
			if (frm.doc.repair_order) {
				frm.add_custom_button(__('Open Repair Order'), function () {
					frappe.set_route('Form', 'Repair Order', frm.doc.repair_order);
				}, __('Navigate'));
			} else {
				frm.add_custom_button(__('Create Repair Order'), function () {
					const open_create_ro_prompt = (default_color, default_plate) => {
						frappe.prompt(
							[
								{
									fieldname: 'odometer_in',
									fieldtype: 'Int',
									label: __('Intake Odometer (km)'),
									reqd: 1,
								},
								{
									fieldname: 'vehicle_color',
									fieldtype: 'Data',
									label: __('Vehicle Color'),
									reqd: 1,
									default: default_color || '',
								},
								{
									fieldname: 'license_plate',
									fieldtype: 'Data',
									label: __('License Plate'),
									reqd: 1,
									default: default_plate || '',
								},
							],
							function (values) {
								const clean_color = String(values.vehicle_color || '').trim().replace(/\s+/g, ' ');
								const clean_plate = String(values.license_plate || '').trim().replace(/\s+/g, ' ').toUpperCase();
								frappe.call({
									method: 'ism.api.scheduling.create_repair_order_from_schedule_entry',
									args: {
										schedule_entry: frm.doc.name,
										odometer_in: values.odometer_in,
										vehicle_color: clean_color,
										license_plate: clean_plate,
									},
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
							},
							__('Create Repair Order')
						);
					};

					const fallback_color = frm.doc.provisional_vehicle_color || '';
					const raw_legacy = String(frm.doc.provisional_vin_or_plate || '').trim();
					const legacy_plate = raw_legacy && !(raw_legacy.length === 17 && /^[A-Za-z0-9]+$/.test(raw_legacy)) ? raw_legacy.toUpperCase() : '';
					const fallback_plate = (frm.doc.provisional_license_plate || legacy_plate || '').toUpperCase();

					if (frm.doc.repair_vehicle) {
						frappe.db.get_value(
							'Repair Vehicle',
							frm.doc.repair_vehicle,
							['color', 'license_plate'],
							(r) => {
								open_create_ro_prompt(
									(r && r.color) || fallback_color,
									(r && r.license_plate) || fallback_plate
								);
							}
						);
					} else {
						open_create_ro_prompt(fallback_color, fallback_plate);
					}
				});
			}
		}
	},

	setup_pin_to_drive_controls(frm) {
		if (frm.is_new() || frm.doc.docstatus === 2) return;
		if (frm.doc.pin_to_drive_status !== 'Provided') return;
		if (!frm.doc.repair_order && SE_PIN_PURGE_STATUSES.includes(frm.doc.status)) return;

		const allowed_roles = [
			'ISM Technician',
			'ISM Service Advisor',
			'ISM Manager',
			'System Manager',
		];
		const can_view_pin =
			frappe.session.user === 'Administrator' ||
			allowed_roles.some((role) => frappe.user.has_role(role));
		if (!can_view_pin) return;

		frm.add_custom_button(__('🔐 Reveal PIN-to-Drive'), () => {
			frappe.call({
				method: 'ism.portal.pin_vault.reveal_schedule_entry_pin_desk',
				args: { schedule_entry: frm.doc.name },
				freeze: true,
				freeze_message: __('Retrieving PIN-to-Drive...'),
				callback: (r) => {
					if (r.exc) return;
					const data = r.message || {};
					if (!data.available || !data.pin) {
						frappe.show_alert({
							message: __('No active PIN-to-Drive is available for this appointment.'),
							indicator: 'orange',
						});
						frm.reload_doc();
						return;
					}

					const mask_seconds = Math.max(1, parseInt(data.auto_mask_seconds, 10) || 15);
					let remaining = mask_seconds;
					let countdown_interval = null;

					const clear_pin_timer = () => {
						if (countdown_interval) {
							clearInterval(countdown_interval);
							countdown_interval = null;
						}
					};

					const d = new frappe.ui.Dialog({
						title: __('Vehicle PIN-to-Drive — {0}', [data.repair_order || frm.doc.name]),
						fields: [
							{
								fieldname: 'pin_html',
								fieldtype: 'HTML',
							},
						],
						primary_action_label: __('Mask & Close'),
						primary_action() {
							d.hide();
						},
						onhide() {
							clear_pin_timer();
							const $pin_el = d.fields_dict.pin_html.$wrapper.find('.ism-pin-digits');
							if ($pin_el.length) {
								$pin_el.text('••••');
							}
						},
					});

					const safe_pin = frappe.utils.escape_html(String(data.pin));
					const safe_views = frappe.utils.escape_html(String(data.view_count || 1));
					d.fields_dict.pin_html.$wrapper.html(`
						<div class="text-center py-3">
							<div class="text-muted small mb-2">${__('Ephemeral Customer PIN-to-Drive (Audited Access)')}</div>
							<div class="ism-pin-digits font-weight-bold" style="font-family: monospace; font-size: 2.5rem; letter-spacing: 0.45rem;">${safe_pin}</div>
							<div class="text-muted small mt-3">
								${__('Auto-masking in')} <strong class="ism-pin-countdown">${mask_seconds}</strong>s
								&middot; ${__('Reveal')} #${safe_views}
							</div>
						</div>
					`);

					d.show();

					countdown_interval = setInterval(() => {
						remaining -= 1;
						const $countdown = d.fields_dict.pin_html.$wrapper.find('.ism-pin-countdown');
						if ($countdown.length) {
							$countdown.text(String(Math.max(0, remaining)));
						}
						if (remaining <= 0) {
							clear_pin_timer();
							d.hide();
						}
					}, 1000);
				},
			});
		});
	},
});
