# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import time
from unittest.mock import patch

import frappe
from ism.ism.doctype.repair_order.repair_order import RepairOrder
from ism.tests.base import ISMTestCase


class TestScheduleEntry(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.manifest = self.get_manifest()
		self.customer = frappe.get_doc("Customer", self.manifest.customers["retail_1"])
		self.vehicle = frappe.get_doc("Repair Vehicle", self.manifest.vehicles["veh_model3_liam"])
		self.ro = self.create_test_repair_order(customer=self.customer.name, vehicle=self.vehicle.name)

	def _insert_entry(self, doc_dict):
		"""Insert Schedule Entry with 3-attempt restart loop on error 1020 without inner rollback."""
		for attempt in range(3):
			try:
				doc = frappe.get_doc(doc_dict.copy()).insert(ignore_permissions=True)
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					time.sleep(0.05 * (attempt + 1))
					continue
				raise

	def test_schedule_entry_naming_series(self):
		"""Verify that Schedule Entry generates SE-.##### series names."""
		se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"status": "Draft",
		})

		self.assertTrue(se.name.startswith("SE-"))
		self.assertEqual(se.status, "Draft")
		self.assertEqual(se.estimated_duration, 60)

	def test_multiple_schedule_entries_per_repair_order(self):
		"""Crucial Invariant: Multiple Schedule Entries on the same Repair Order must succeed (1:N cardinality)."""
		se1 = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 120,
			"status": "Scheduled",
		})

		# Supplemental work session on the same Repair Order
		se2 = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": frappe.utils.add_days(frappe.utils.today(), 1),
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"status": "Scheduled",
		})

		self.assertNotEqual(se1.name, se2.name)
		self.assertEqual(se1.repair_order, self.ro.name)
		self.assertEqual(se2.repair_order, self.ro.name)

		# Both entries linked to same RO
		linked_entries = frappe.get_all(
			"Schedule Entry",
			filters={"repair_order": self.ro.name},
			pluck="name",
		)
		self.assertIn(se1.name, linked_entries)
		self.assertIn(se2.name, linked_entries)
		self.assertEqual(len(linked_entries), 2)

	def test_auto_population_from_repair_order(self):
		"""Verify customer and vehicle auto-population when linked to Repair Order."""
		se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
		})

		self.assertEqual(se.customer, self.customer.name)
		self.assertEqual(se.repair_vehicle, self.vehicle.name)

	def test_provisional_contact_and_vehicle_display(self):
		"""Verify display fallbacks for provisional customer and vehicle intake."""
		se_empty = frappe.new_doc("Schedule Entry")
		self.assertEqual(se_empty.get_display_customer(), "Guest")
		self.assertEqual(se_empty.get_display_vehicle(), "")

		se_prov = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "11:00:00",
			"provisional_customer_name": "Alice Walker",
			"provisional_phone": "555-123-4567",
			"provisional_email": "alice@example.com",
			"provisional_vehicle_info": "2024 Model Y Red",
		})

		self.assertEqual(se_prov.get_display_customer(), "Alice Walker")
		self.assertEqual(se_prov.get_display_vehicle(), "2024 Model Y Red")
		self.assertEqual(se_prov.title, "Alice Walker | 2024 Model Y Red")

	def test_requires_repair_order_validation(self):
		"""Verify that entry types with requires_repair_order=1 mandate a Repair Order."""
		# Repair requires Repair Order
		se_repair = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "13:00:00",
		})
		self.assertRaises(frappe.MandatoryError, se_repair.insert)

		# Diagnostic does not require Repair Order
		se_diag = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "13:00:00",
		})
		self.assertTrue(se_diag.name)

	def test_estimated_duration_must_be_positive(self):
		"""Verify that estimated_duration <= 0 raises ValidationError."""
		# Zero duration
		se_zero = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
			"estimated_duration": 0,
		})
		self.assertRaises(frappe.ValidationError, se_zero.insert)

		# Negative duration
		se_neg = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
			"estimated_duration": -30,
		})
		self.assertRaises(frappe.ValidationError, se_neg.insert)

	def test_populate_from_repair_order_on_update(self):
		"""Verify customer/vehicle auto-population when linking Repair Order to existing Schedule Entry."""
		se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"provisional_customer_name": "Provisional Client",
		})

		self.assertEqual(se.get_display_customer(), "Provisional Client")
		self.assertIsNone(se.customer)
		self.assertIsNone(se.repair_vehicle)

		# Advisor links existing repair order on update
		se.repair_order = self.ro.name
		se.save(ignore_permissions=True)

		self.assertEqual(se.customer, self.customer.name)
		self.assertEqual(se.repair_vehicle, self.vehicle.name)
		self.assertIn(self.customer.name, se.title)

	def test_sync_does_not_revalidate_parent_ro(self):
		"""Verify Schedule Entry save syncs child rows directly without invoking RepairOrder.validate."""
		with patch.object(RepairOrder, "validate") as mock_ro_validate:
			se = self._insert_entry({
				"doctype": "Schedule Entry",
				"entry_type": "Repair",
				"repair_order": self.ro.name,
				"scheduled_date": frappe.utils.today(),
				"scheduled_time": "15:00:00",
				"estimated_duration": 60,
			})
			se.status = "Confirmed"
			se.save(ignore_permissions=True)

		mock_ro_validate.assert_not_called()
		self.ro.reload()
		self.assertEqual(len(self.ro.schedule_entries), 1)
		self.assertEqual(self.ro.schedule_entries[0].schedule_entry, se.name)
		self.assertEqual(self.ro.schedule_entries[0].status, "Confirmed")

	def test_warnings_and_no_show_release_capacity(self):
		"""Verify assignment warnings use doc flags without msgprint, and No Show releases bay capacity."""
		from ism.api.scheduling import check_bay_availability

		bay_name = self.manifest.bays["main_lift"]

		se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"service_bay": bay_name,
		})
		self.assertFalse(check_bay_availability(bay_name, frappe.utils.today(), "09:00:00", 60))

		with patch("frappe.msgprint") as mock_msgprint, patch(
			"ism.api.technician_availability.check_technician_availability",
			return_value={"is_available": False, "on_leave": True, "conflicts": []},
		):
			se.flags.ignore_links = True
			se.assigned_technician = "TECH-MOCK"
			se.save(ignore_permissions=True)
		mock_msgprint.assert_not_called()
		self.assertEqual(len(se.flags.scheduling_warnings), 1)

		se.status = "No Show"
		se.save(ignore_permissions=True)
		self.assertTrue(check_bay_availability(bay_name, frappe.utils.today(), "09:00:00", 60))

	def test_get_display_vehicle_linked_and_provisional(self):
		"""get_display_vehicle() includes year, color, model, and license plate for linked and provisional entries."""
		from ism.ism.doctype.repair_vehicle.vin_decoder import compute_vin_check_digit

		template = "7SAYGDEE0PF223344"  # 2023 Model Y
		vin = f"{template[:8]}{compute_vin_check_digit(template)}{template[9:]}"
		veh = self.create_test_vehicle(
			vin=vin,
			customer=self.customer.name,
			color="Pearl White",
			license_plate="se 123",
		)
		se_linked = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"customer": self.customer.name,
			"repair_vehicle": veh.name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
			"estimated_duration": 60,
		})
		self.assertEqual(se_linked.get_display_vehicle(), "2023 Pearl White Model Y (SE 123)")

		se_prov = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"provisional_customer_name": "Provisional Driver",
			"provisional_vehicle_year": 2024,
			"provisional_vehicle_color": "  Stealth Grey ",
			"provisional_vehicle_model": "Model 3",
			"provisional_license_plate": "  prv 888 ",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "11:00:00",
			"estimated_duration": 60,
		})
		self.assertEqual(se_prov.provisional_vehicle_color, "Stealth Grey")
		self.assertEqual(se_prov.provisional_license_plate, "PRV 888")
		self.assertEqual(se_prov.get_display_vehicle(), "2024 Stealth Grey Model 3 (PRV 888)")

	def test_create_ro_from_schedule_entry_color_plate_enforcement_and_persistence(self):
		"""create_repair_order_from_schedule_entry enforces color & plate and persists them onto Repair Vehicle."""
		from ism.api.scheduling import create_repair_order_from_schedule_entry

		veh = self.create_test_vehicle(
			customer=self.customer.name,
			color="Deep Blue",
			license_plate="CHK 100",
		)
		se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"customer": self.customer.name,
			"repair_vehicle": veh.name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "12:00:00",
			"estimated_duration": 60,
		})

		# Non-string color or plate rejected at API boundary
		with self.assertRaises((frappe.ValidationError, frappe.FrappeTypeError, TypeError)):
			create_repair_order_from_schedule_entry(
				se.name, odometer_in=20000, vehicle_color=["!=", ""], license_plate="CHK 202"
			)
		with self.assertRaises((frappe.ValidationError, frappe.FrappeTypeError, TypeError)):
			create_repair_order_from_schedule_entry(
				se.name, odometer_in=20000, vehicle_color="Quicksilver", license_plate=True
			)

		# Clear color & plate on vehicle to simulate legacy incomplete record
		frappe.db.set_value(
			"Repair Vehicle",
			veh.name,
			{"color": "", "license_plate": ""},
			update_modified=False,
		)
		with self.assertRaises(frappe.MandatoryError):
			create_repair_order_from_schedule_entry(se.name, odometer_in=20000)

		# Missing plate still blocked
		with self.assertRaises(frappe.MandatoryError):
			create_repair_order_from_schedule_entry(
				se.name, odometer_in=20000, vehicle_color="Quicksilver"
			)

		# Providing both color and plate persists them onto Repair Vehicle and creates RO
		res = create_repair_order_from_schedule_entry(
			se.name,
			odometer_in=20000,
			vehicle_color="Quicksilver",
			license_plate="chk 202",
		)
		self.track("Repair Order", res["repair_order"])
		veh.reload()
		self.assertEqual(veh.color, "Quicksilver")
		self.assertEqual(veh.license_plate, "CHK 202")
		self.assertIn("Quicksilver", res["vehicle_display"])
		self.assertIn("(CHK 202)", res["vehicle_display"])
		self.assertEqual(res["vehicle_info"]["color"], "Quicksilver")
		self.assertEqual(res["vehicle_info"]["license_plate"], "CHK 202")

	def test_prevent_overlapping_same_vehicle_across_bays_and_unassigned(self):
		"""Same linked Repair Vehicle cannot overlap across different bays, unassigned bay, lunch, or overnight."""
		bay1 = self.manifest.bays["main_lift"]
		bay2 = self.manifest.bays["alignment"]
		day1 = frappe.utils.add_days(frappe.utils.today(), 7)
		day2 = frappe.utils.add_days(day1, 1)

		se1 = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": day1,
			"scheduled_time": "08:00:00",
			"estimated_duration": 60,
			"service_bay": bay1,
			"status": "Scheduled",
		})

		# Overlapping on different bay -> blocked
		with self.assertRaises(frappe.ValidationError) as ctx:
			frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"repair_vehicle": self.vehicle.name,
				"scheduled_date": day1,
				"scheduled_time": "08:30:00",
				"estimated_duration": 45,
				"service_bay": bay2,
				"status": "Scheduled",
			}).insert(ignore_permissions=True)
		self.assertEqual(getattr(ctx.exception, "reason_code", None), "vehicle_conflict")

		# Overlapping with unassigned bay (service_bay=None) -> also blocked
		with self.assertRaises(frappe.ValidationError) as ctx_unassigned:
			frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"repair_order": self.ro.name,
				"scheduled_date": day1,
				"scheduled_time": "08:15:00",
				"estimated_duration": 30,
				"status": "Scheduled",
			}).insert(ignore_permissions=True)
		self.assertEqual(getattr(ctx_unassigned.exception, "reason_code", None), "vehicle_conflict")

		# Linking an existing entry to the same RO during an overlapping window on update -> blocked
		other_se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": day1,
			"scheduled_time": "08:30:00",
			"estimated_duration": 30,
			"service_bay": bay2,
			"status": "Scheduled",
		})
		other_se.repair_order = self.ro.name
		with self.assertRaises(frappe.ValidationError):
			other_se.save(ignore_permissions=True)
		other_se.delete(ignore_permissions=True)

		# Adjacent back-to-back on different bay (09:00-10:00) -> allowed
		se_adj = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": day1,
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"service_bay": bay2,
			"status": "Scheduled",
		})
		self.assertTrue(se_adj.name)

		# Self-update (extending duration without hitting se_adj, or changing notes) -> allowed
		se1.notes = "Updated notes"
		se1.save(ignore_permissions=True)

		# Overnight span (15:00 Day 1 for 240 min -> finishes Day 2 10:00) blocks Day 2 08:30
		self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": day1,
			"scheduled_time": "15:00:00",
			"estimated_duration": 240,
			"service_bay": bay1,
			"status": "Scheduled",
		})
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"repair_vehicle": self.vehicle.name,
				"scheduled_date": day2,
				"scheduled_time": "08:30:00",
				"estimated_duration": 60,
				"service_bay": bay2,
				"status": "Scheduled",
			}).insert(ignore_permissions=True)

		# Marking se1 Cancelled / Completed / No Show releases vehicle capacity for [08:00, 09:00)
		se1.status = "Cancelled"
		se1.save(ignore_permissions=True)
		se_rebooked = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"repair_vehicle": self.vehicle.name,
			"scheduled_date": day1,
			"scheduled_time": "08:30:00",
			"estimated_duration": 30,
			"service_bay": bay2,
			"status": "Scheduled",
		})
		self.assertTrue(se_rebooked.name)

	def test_cross_tier_vin_plate_phone_and_false_positive_guards(self):
		"""Cross-matches VIN/plate, enforces one-phone-at-a-time (Slot 5 case), and allows distinct cars/customers."""
		bay1 = self.manifest.bays["main_lift"]
		bay2 = self.manifest.bays["alignment"]
		bay4 = self.manifest.bays["tire_quick"]
		target_date = frappe.utils.add_days(frappe.utils.today(), 8)

		veh = self.create_test_vehicle(
			customer=self.customer.name,
			color="Pearl White",
			license_plate="EV 7788",
		)
		self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"customer": self.customer.name,
			"repair_vehicle": veh.name,
			"scheduled_date": target_date,
			"scheduled_time": "08:00:00",
			"estimated_duration": 60,
			"service_bay": bay1,
			"status": "Scheduled",
		})

		# Linked <-> Provisional cross-match via license plate with different spacing/case/hyphen + different phone
		with self.assertRaises(frappe.ValidationError) as ctx_plate:
			frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"provisional_customer_name": "Spouse Driver",
				"provisional_phone": "+16045559991",
				"provisional_license_plate": "ev-7788",
				"scheduled_date": target_date,
				"scheduled_time": "08:30:00",
				"estimated_duration": 45,
				"service_bay": bay2,
				"status": "Scheduled",
			}).insert(ignore_permissions=True)
		self.assertEqual(getattr(ctx_plate.exception, "reason_code", None), "vehicle_conflict")

		# Linked <-> Provisional cross-match via 17-char VIN
		with self.assertRaises(frappe.ValidationError) as ctx_vin:
			frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"provisional_customer_name": "Another Driver",
				"provisional_phone": "+16045559992",
				"provisional_vin_or_plate": veh.vin.lower(),
				"scheduled_date": target_date,
				"scheduled_time": "08:15:00",
				"estimated_duration": 30,
				"service_bay": bay4,
				"status": "Scheduled",
			}).insert(ignore_permissions=True)
		self.assertEqual(getattr(ctx_vin.exception, "reason_code", None), "vehicle_conflict")

		# Slot 5 exact reproduction + one-phone-at-a-time rule (even with blank vehicle model/plate/year)
		slot5_date = frappe.utils.add_days(frappe.utils.today(), 9)
		self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"booking_source": "Website Self-Serve",
			"provisional_customer_name": "test",
			"provisional_phone": "+15552311564",
			"provisional_vehicle_year": 2023,
			"provisional_vehicle_model": "Model 3",
			"provisional_vehicle_color": "Pearl White",
			"provisional_vehicle_info": "2023 Pearl White Model 3",
			"scheduled_date": slot5_date,
			"scheduled_time": "08:00:00",
			"estimated_duration": 60,
			"service_bay": bay1,
			"status": "Scheduled",
		})

		# Same phone, overlapping 08:30-09:15 on Bay 4 (exact Slot 5 SE-00002 shape) -> STILL blocked as same car even with default phone limit=2
		with self.assertRaises(frappe.ValidationError) as ctx_slot5:
			frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"booking_source": "Website Self-Serve",
				"provisional_customer_name": "Test User",
				"provisional_phone": "555-231-1564",
				"provisional_vehicle_year": 0,
				"provisional_vehicle_model": "Model 3",
				"provisional_vehicle_color": "Pearl White",
				"provisional_vehicle_info": "Pearl White Model 3",
				"scheduled_date": slot5_date,
				"scheduled_time": "08:30:00",
				"estimated_duration": 45,
				"service_bay": bay4,
				"status": "Scheduled",
			}).insert(ignore_permissions=True)
		self.assertEqual(getattr(ctx_slot5.exception, "reason_code", None), "vehicle_conflict")

		# ISM Settings max_concurrent_bookings_per_phone defaults to 2 and validates bounds (1-10)
		settings = frappe.get_single("ISM Settings")
		orig_phone_limit = settings.get("max_concurrent_bookings_per_phone")
		self.assertEqual(int(orig_phone_limit or 2), 2)
		try:
			for bad_val in (0, 11):
				settings.max_concurrent_bookings_per_phone = bad_val
				with self.assertRaises(frappe.ValidationError):
					settings.save(ignore_permissions=True)
				settings.reload()

			# Default limit=2: same phone CAN book a 2nd DIFFERENT car (Model Y) at overlapping time
			second_car_se = self._insert_entry({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"provisional_phone": "+15552311564",
				"provisional_vehicle_model": "Model Y",
				"provisional_vehicle_color": "Deep Blue",
				"scheduled_date": slot5_date,
				"scheduled_time": "08:30:00",
				"estimated_duration": 30,
				"service_bay": bay2,
				"status": "Scheduled",
			})
			self.assertTrue(second_car_se.name)

			# Default limit=2: a 3rd overlapping appointment for the same phone is BLOCKED
			with self.assertRaises(frappe.ValidationError) as ctx_third:
				frappe.get_doc({
					"doctype": "Schedule Entry",
					"entry_type": "Diagnostic",
					"provisional_phone": "+15552311564",
					"provisional_vehicle_model": "Model S",
					"provisional_vehicle_color": "Ultra Red",
					"scheduled_date": slot5_date,
					"scheduled_time": "08:30:00",
					"estimated_duration": 30,
					"service_bay": bay4,
					"status": "Scheduled",
				}).insert(ignore_permissions=True)
			self.assertEqual(getattr(ctx_third.exception, "reason_code", None), "vehicle_conflict")

			# Lowering setting to 1 blocks even a 2nd different car / blank-vehicle entry on the same phone
			settings.max_concurrent_bookings_per_phone = 1
			settings.save(ignore_permissions=True)
			frappe.clear_cache(doctype="ISM Settings")
			second_car_se.delete(ignore_permissions=True)

			with self.assertRaises(frappe.ValidationError) as ctx_limit1:
				frappe.get_doc({
					"doctype": "Schedule Entry",
					"entry_type": "Diagnostic",
					"provisional_phone": "+15552311564",
					"scheduled_date": slot5_date,
					"scheduled_time": "08:30:00",
					"estimated_duration": 30,
					"service_bay": bay2,
					"status": "Scheduled",
				}).insert(ignore_permissions=True)
			self.assertEqual(getattr(ctx_limit1.exception, "reason_code", None), "vehicle_conflict")
		finally:
			settings.reload()
			settings.max_concurrent_bookings_per_phone = orig_phone_limit if orig_phone_limit is not None else 2
			settings.save(ignore_permissions=True)
			frappe.clear_cache(doctype="ISM Settings")

		# False-positive guard 1: Different customer/phone with identical Pearl White Model 3 -> allowed
		other_cust_se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"provisional_customer_name": "Other Customer",
			"provisional_phone": "+15559998888",
			"provisional_vehicle_year": 2023,
			"provisional_vehicle_model": "Model 3",
			"provisional_vehicle_color": "Pearl White",
			"provisional_vehicle_info": "2023 Pearl White Model 3",
			"scheduled_date": slot5_date,
			"scheduled_time": "08:30:00",
			"estimated_duration": 45,
			"service_bay": bay4,
			"status": "Scheduled",
		})
		self.assertTrue(other_cust_se.name)

		# False-positive guard 2: Fleet customer with TWO distinct linked Repair Vehicles -> allowed
		fleet_date = frappe.utils.add_days(frappe.utils.today(), 10)
		fleet_cust = self.manifest.customers["fleet_1"]
		fv1 = self.manifest.vehicles["veh_fleet_1"]
		fv2 = self.manifest.vehicles["veh_fleet_2"]
		fse1 = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"customer": fleet_cust,
			"repair_vehicle": fv1,
			"scheduled_date": fleet_date,
			"scheduled_time": "10:00:00",
			"estimated_duration": 60,
			"service_bay": bay1,
			"status": "Scheduled",
		})
		fse2 = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"customer": fleet_cust,
			"repair_vehicle": fv2,
			"scheduled_date": fleet_date,
			"scheduled_time": "10:00:00",
			"estimated_duration": 60,
			"service_bay": bay2,
			"status": "Scheduled",
		})
		self.assertNotEqual(fse1.name, fse2.name)

		# False-positive guard 3: Vehicle-less shop entries (Meeting / Internal Service) overlapping -> allowed
		m1 = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Meeting",
			"scheduled_date": fleet_date,
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"status": "Scheduled",
		})
		m2 = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Internal Service",
			"scheduled_date": fleet_date,
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"status": "Scheduled",
		})
		self.assertNotEqual(m1.name, m2.name)
