# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Layer 1 Canonical Master Catalog seeder and deterministic synthetic catalog generators."""

from __future__ import annotations

import random
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

import frappe

from ism.ism.doctype.repair_vehicle.vin_decoder import compute_vin_check_digit, decode_tesla_vin
from ism.setup_tasks import seed_schedule_entry_types
from ism.setup_tasks.taxes import _ensure_company_tax_template, _ensure_default_tax_rule, _get_generic_tax_rules
from ism.testing.manifest import SimManifest

CATALOG_SEED = 20260101
CANONICAL_COMPANY = "Lightspeed EV"
CANONICAL_ABBR = "LEV"
CANONICAL_CURRENCY = "CAD"
CANONICAL_COUNTRY = "Canada"
CANONICAL_TIMEZONE = "America/Toronto"
CANONICAL_MAIN_WAREHOUSE = f"Main Shop Stores - {CANONICAL_ABBR}"

_VIN_ARCHETYPES: dict[str, tuple[str, str, str, str, str]] = {
	"3": ("5YJ", "E", "1", "E", "B"),
	"Y": ("7SA", "G", "1", "E", "E"),
	"S": ("5YJ", "A", "1", "E", "5"),
	"X": ("7SA", "C", "1", "E", "5"),
}

_PLANT_FOR_MODEL: dict[str, str] = {
	"3": "F",
	"Y": "A",
	"S": "F",
	"X": "F",
}

_MODES_OF_PAYMENT: tuple[tuple[str, str, str], ...] = (
	("Debit / Interac", "Bank", "bank"),
	("Credit Card", "Bank", "bank"),
	("E-Transfer", "Bank", "bank"),
	("Cash", "Cash", "cash"),
	("Bank Draft", "Bank", "bank"),
	("Wire Transfer", "Bank", "bank"),
)

_UOMS = ("Nos", "Unit", "Hour", "Litre", "Set", "Pair", "Kit", "Bottle")

_ITEM_GROUPS = (
	"Services",
	"Products",
	"Parts",
	"Brake",
	"Suspension",
	"Tire",
	"Electrical",
	"Diagnostic",
	"HVAC",
	"Drivetrain",
	"Body",
	"Consumables",
)

_PERSONAS: tuple[dict[str, Any], ...] = (
	{
		"handle": "manager",
		"email": "manager@lightspeedev.ca",
		"first_name": "Claire",
		"last_name": "Sterling",
		"roles": (
			"ISM Manager",
			"Sales User",
			"Maintenance User",
			"Stock User",
			"Purchase User",
			"Accounts User",
			"HR User",
			"HR Manager",
		),
	},
	{
		"handle": "advisor",
		"email": "advisor@lightspeedev.ca",
		"first_name": "Jordan",
		"last_name": "Reyes",
		"roles": ("ISM Service Advisor", "Sales User", "Maintenance User", "Accounts User"),
	},
	{
		"handle": "parts",
		"email": "parts@lightspeedev.ca",
		"first_name": "Samira",
		"last_name": "Khan",
		"roles": (
			"ISM Service Advisor",
			"Stock User",
			"Stock Manager",
			"Purchase User",
			"Purchase Manager",
			"Accounts User",
		),
	},
	{
		"handle": "tech",
		"email": "tech@lightspeedev.ca",
		"first_name": "Marcus",
		"last_name": "Vance",
		# Strictly technician-level roles so permlevel=1 pricing/discount locks are enforced.
		"roles": ("ISM Technician", "Maintenance User"),
	},
)

_EMPLOYEES: tuple[dict[str, Any], ...] = (
	{
		"handle": "marcus",
		"first_name": "Marcus",
		"last_name": "Vance",
		"employee_name": "Marcus Vance",
		"status": "Active",
		"gender": "Male",
		"date_of_birth": "1989-04-12",
		"date_of_joining": "2021-03-15",
		"user_id": "tech@lightspeedev.ca",
	},
	{
		"handle": "elena",
		"first_name": "Elena",
		"last_name": "Rostova",
		"employee_name": "Elena Rostova",
		"status": "Active",
		"gender": "Female",
		"date_of_birth": "1991-08-23",
		"date_of_joining": "2022-01-10",
	},
	{
		"handle": "devon",
		"first_name": "Devon",
		"last_name": "Brooks",
		"employee_name": "Devon Brooks",
		"status": "Active",
		"gender": "Male",
		"date_of_birth": "1993-11-05",
		"date_of_joining": "2022-06-01",
	},
	{
		"handle": "noah",
		"first_name": "Noah",
		"last_name": "Patel",
		"employee_name": "Noah Patel",
		"status": "Active",
		"gender": "Male",
		"date_of_birth": "1995-02-17",
		"date_of_joining": "2023-02-20",
	},
	{
		"handle": "inactive_tech",
		"first_name": "Victor",
		"last_name": "Halvorsen",
		"employee_name": "Victor Halvorsen",
		"status": "Left",
		"gender": "Male",
		"date_of_birth": "1986-07-09",
		"date_of_joining": "2020-05-01",
		"relieving_date": "2024-11-30",
	},
)

_EQUIPMENT_TAGS: tuple[tuple[str, str, str], ...] = (
	("lift", "Lift", "Standard automotive 2-post / 4-post vehicle lift"),
	("alignment_rack", "Alignment Rack", "3D optical four-wheel alignment rack and turntable system"),
	("ev_hv", "EV High Voltage", "High-voltage PPE, CAT III multimeter, and insulation tester"),
	("hv_battery_station", "HV Battery Station", "High-voltage battery lift table and thermal coolant evacuator"),
	("adas_target", "ADAS Calibration Target", "Optical radar and Autopilot camera calibration rig"),
	("tire_balancer", "Tire Mount & Road Force Balancer", "Touchless EV tire changer and road-force balancer"),
)

_SERVICE_BAYS: tuple[dict[str, Any], ...] = (
	{
		"handle": "main_lift",
		"bay_name": "Bay 1 - Main Lift",
		"is_active": 1,
		"description": "Primary 2-post asymmetric lift bay for general mechanical and brake service",
		"tags": ("Lift",),
	},
	{
		"handle": "alignment",
		"bay_name": "Bay 2 - Alignment & Suspension",
		"is_active": 1,
		"description": "Four-wheel alignment rack with lift and ADAS calibration target suite",
		"tags": ("Lift", "Alignment Rack", "ADAS Calibration Target"),
	},
	{
		"handle": "hv_diagnostics",
		"bay_name": "Bay 3 - HV & Diagnostics",
		"is_active": 1,
		"description": "High-voltage isolation, battery pack service, and drive unit diagnostic bay",
		"tags": ("Lift", "EV High Voltage", "HV Battery Station"),
	},
	{
		"handle": "tire_quick",
		"bay_name": "Bay 4 - Tire & Quick Service",
		"is_active": 1,
		"description": "Quick-turn tire, road-force balancing, wiper, and cabin filter bay",
		"tags": ("Lift", "Tire Mount & Road Force Balancer"),
	},
	{
		"handle": "offline",
		"bay_name": "Bay 5 - Offline",
		"is_active": 0,
		"description": "Decommissioned overflow bay retained inactive for capacity filtering",
		"tags": ("Lift",),
	},
)

_WAREHOUSES: tuple[tuple[str, str], ...] = (
	("main_stores", "Main Shop Stores"),
	("special_order", "Special Order Staging"),
	("core_return", "Core Return Cage"),
)

_SUPPLIERS: tuple[tuple[str, str, str], ...] = (
	("tesla_oem", "Tesla OEM Direct", "Distributor"),
	("euro_aftermarket", "EuroEV Aftermarket Distributors", "Distributor"),
	("metro_tire", "Metro Tire & Wheel Wholesale", "Distributor"),
	("shop_consumables", "Shop Consumables Co.", "Local"),
)

# Stock distribution bands: abundant (>= 16), tight (== 2 for FIFO contention), zero (== 0 for Waiting on Parts)
_PART_SPECS: tuple[dict[str, Any], ...] = (
	# Abundant band
	{
		"handle": "dot4_brake_fluid",
		"item_code": "LEV-FLD-DOT4-1L",
		"item_name": "DOT 4 LV High-Performance Brake Fluid (1L)",
		"item_group": "Consumables",
		"uom": "Bottle",
		"source": "Aftermarket",
		"condition": "New",
		"cost": 14.00,
		"tesla_oem": 28.00,
		"stock_qty": 30.0,
		"supplier": "shop_consumables",
		"frt": 0.2,
		"warranty": "12 Months",
	},
	{
		"handle": "cabin_hepa_filter_oem",
		"item_code": "LEV-FLT-CABIN-OEM",
		"item_name": "Tesla Cabin HEPA & Carbon Filter Pair (OEM)",
		"item_group": "HVAC",
		"uom": "Set",
		"source": "OEM",
		"condition": "New",
		"cost": 36.00,
		"tesla_oem": 68.00,
		"stock_qty": 24.0,
		"supplier": "tesla_oem",
		"frt": 0.5,
		"warranty": "12 Months / 20,000 km",
	},
	{
		"handle": "cabin_hepa_filter_aft",
		"item_code": "LEV-FLT-CABIN-AFT",
		"item_name": "Cabin Activated Carbon Filter Pair (Aftermarket)",
		"item_group": "HVAC",
		"uom": "Set",
		"source": "Aftermarket",
		"condition": "New",
		"cost": 22.00,
		"tesla_oem": 68.00,
		"stock_qty": 20.0,
		"supplier": "euro_aftermarket",
		"frt": 0.5,
		"warranty": "12 Months / 20,000 km",
	},
	{
		"handle": "wiper_blade_set_oem",
		"item_code": "LEV-WPR-FRONT-SET",
		"item_name": "Aero Front Wiper Blade Pair (650mm / 475mm)",
		"item_group": "Body",
		"uom": "Pair",
		"source": "OEM",
		"condition": "New",
		"cost": 28.00,
		"tesla_oem": 56.00,
		"stock_qty": 25.0,
		"supplier": "tesla_oem",
		"frt": 0.2,
		"warranty": "6 Months",
	},
	{
		"handle": "coolant_g48",
		"item_code": "LEV-FLD-G48-4L",
		"item_name": "EV Battery Thermal Coolant G48 Premix (4L)",
		"item_group": "Consumables",
		"uom": "Bottle",
		"source": "OEM",
		"condition": "New",
		"cost": 26.00,
		"tesla_oem": 48.00,
		"stock_qty": 18.0,
		"supplier": "tesla_oem",
		"frt": 0.3,
		"warranty": "12 Months",
	},
	{
		"handle": "lug_nut_kit",
		"item_code": "LEV-HDW-LUG-M14",
		"item_name": "M14x1.5 Chrome Wheel Lug Nut (Single)",
		"item_group": "Tire",
		"uom": "Nos",
		"source": "OEM",
		"condition": "New",
		"cost": 4.50,
		"tesla_oem": 12.00,
		"stock_qty": 40.0,
		"supplier": "tesla_oem",
		"frt": 0.1,
		"warranty": "12 Months",
	},
	{
		"handle": "front_pad_set_oem",
		"item_code": "LEV-BRK-FPAD-OEM",
		"item_name": "Front Ceramic Brake Pad Set (OEM)",
		"item_group": "Brake",
		"uom": "Set",
		"source": "OEM",
		"condition": "New",
		"cost": 118.00,
		"tesla_oem": 220.00,
		"stock_qty": 16.0,
		"supplier": "tesla_oem",
		"frt": 0.8,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "front_pad_set_aft",
		"item_code": "LEV-BRK-FPAD-AFT",
		"item_name": "Brembo NAO Front Brake Pad Set (Aftermarket)",
		"item_group": "Brake",
		"uom": "Set",
		"source": "Aftermarket",
		"condition": "New",
		"cost": 74.00,
		"tesla_oem": 220.00,
		"stock_qty": 18.0,
		"supplier": "euro_aftermarket",
		"frt": 0.8,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "tpms_ble_sensor",
		"item_code": "LEV-TIR-TPMS-BLE",
		"item_name": "Bluetooth LE Tire Pressure Sensor",
		"item_group": "Tire",
		"uom": "Nos",
		"source": "OEM",
		"condition": "New",
		"cost": 52.00,
		"tesla_oem": 95.00,
		"stock_qty": 16.0,
		"supplier": "metro_tire",
		"frt": 0.3,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "ev_tire_235_45r18",
		"item_code": "LEV-TIR-235-45R18",
		"item_name": "235/45R18 Acoustic Foam EV All-Season Tire",
		"item_group": "Tire",
		"uom": "Nos",
		"source": "Aftermarket",
		"condition": "New",
		"cost": 195.00,
		"tesla_oem": 340.00,
		"stock_qty": 16.0,
		"supplier": "metro_tire",
		"frt": 0.4,
		"warranty": "Road Hazard 24 Months",
	},
	{
		"handle": "susp_bolt_kit",
		"item_code": "LEV-SUS-BOLT-KIT",
		"item_name": "Front Fore Link Prevailing Torque Bolt & Nut Kit",
		"item_group": "Suspension",
		"uom": "Kit",
		"source": "OEM",
		"condition": "New",
		"cost": 11.00,
		"tesla_oem": 18.00,
		"stock_qty": 24.0,
		"supplier": "tesla_oem",
		"frt": 0.1,
		"warranty": "12 Months",
	},
	# Tight band (qty == 2 for FIFO commitment contention)
	{
		"handle": "front_rotor_oem",
		"item_code": "LEV-BRK-FROT-OEM",
		"item_name": "355mm Vented Front Brake Rotor (OEM)",
		"item_group": "Brake",
		"uom": "Nos",
		"source": "OEM",
		"condition": "New",
		"cost": 124.00,
		"tesla_oem": 245.00,
		"stock_qty": 2.0,
		"supplier": "tesla_oem",
		"frt": 0.6,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "front_rotor_aft",
		"item_code": "LEV-BRK-FROT-AFT",
		"item_name": "Zimmermann Coated Front Brake Rotor (Aftermarket)",
		"item_group": "Brake",
		"uom": "Nos",
		"source": "Aftermarket",
		"condition": "New",
		"cost": 88.00,
		"tesla_oem": 245.00,
		"stock_qty": 2.0,
		"supplier": "euro_aftermarket",
		"frt": 0.6,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "fore_link_lh_oem",
		"item_code": "LEV-SUS-FLNK-LH-OEM",
		"item_name": "Front Lower Fore Compliance Link - LH (OEM)",
		"item_group": "Suspension",
		"uom": "Nos",
		"source": "OEM",
		"condition": "New",
		"cost": 162.00,
		"tesla_oem": 290.00,
		"stock_qty": 2.0,
		"supplier": "tesla_oem",
		"frt": 1.2,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "fore_link_rh_oem",
		"item_code": "LEV-SUS-FLNK-RH-OEM",
		"item_name": "Front Lower Fore Compliance Link - RH (OEM)",
		"item_group": "Suspension",
		"uom": "Nos",
		"source": "OEM",
		"condition": "New",
		"cost": 162.00,
		"tesla_oem": 290.00,
		"stock_qty": 2.0,
		"supplier": "tesla_oem",
		"frt": 1.2,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "fore_link_lh_aft",
		"item_code": "LEV-SUS-FLNK-LH-AFT",
		"item_name": "Lemforder Front Lower Fore Link - LH (Aftermarket)",
		"item_group": "Suspension",
		"uom": "Nos",
		"source": "Aftermarket",
		"condition": "New",
		"cost": 115.00,
		"tesla_oem": 290.00,
		"stock_qty": 2.0,
		"supplier": "euro_aftermarket",
		"frt": 1.2,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "fore_link_rh_aft",
		"item_code": "LEV-SUS-FLNK-RH-AFT",
		"item_name": "Lemforder Front Lower Fore Link - RH (Aftermarket)",
		"item_group": "Suspension",
		"uom": "Nos",
		"source": "Aftermarket",
		"condition": "New",
		"cost": 115.00,
		"tesla_oem": 290.00,
		"stock_qty": 2.0,
		"supplier": "euro_aftermarket",
		"frt": 1.2,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "ptc_cabin_heater_recon",
		"item_code": "LEV-HVC-PTC-RECON",
		"item_name": "HV Cabin Coolant Heater Assembly (Reconditioned)",
		"item_group": "HVAC",
		"uom": "Nos",
		"source": "OEM",
		"condition": "Reconditioned",
		"cost": 380.00,
		"tesla_oem": 760.00,
		"stock_qty": 2.0,
		"supplier": "tesla_oem",
		"frt": 1.8,
		"warranty": "12 Months / 20,000 km",
	},
	# Zero band (qty == 0 for Waiting on Parts + PO/RFQ flows)
	{
		"handle": "hv_pyro_fuse_oem",
		"item_code": "LEV-ELC-PYRO-FUSE",
		"item_name": "HV Battery Pyrotechnic Disconnect Fuse Assembly",
		"item_group": "Electrical",
		"uom": "Nos",
		"source": "OEM",
		"condition": "New",
		"cost": 245.00,
		"tesla_oem": 440.00,
		"stock_qty": 0.0,
		"supplier": "tesla_oem",
		"frt": 2.5,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "rear_subframe_arm_lh",
		"item_code": "LEV-SUS-RCAMB-LH",
		"item_name": "Rear Subframe Upper Camber Link - LH (OEM)",
		"item_group": "Suspension",
		"uom": "Nos",
		"source": "OEM",
		"condition": "New",
		"cost": 175.00,
		"tesla_oem": 325.00,
		"stock_qty": 0.0,
		"supplier": "tesla_oem",
		"frt": 1.8,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "rear_subframe_arm_rh",
		"item_code": "LEV-SUS-RCAMB-RH",
		"item_name": "Rear Subframe Upper Camber Link - RH (OEM)",
		"item_group": "Suspension",
		"uom": "Nos",
		"source": "OEM",
		"condition": "New",
		"cost": 175.00,
		"tesla_oem": 325.00,
		"stock_qty": 0.0,
		"supplier": "tesla_oem",
		"frt": 1.8,
		"warranty": "24 Months / 40,000 km",
	},
	{
		"handle": "adas_windshield_bracket",
		"item_code": "LEV-ELC-ADAS-BRKT",
		"item_name": "Forward Autopilot Camera Heater & Mount Bracket",
		"item_group": "Electrical",
		"uom": "Nos",
		"source": "OEM",
		"condition": "New",
		"cost": 140.00,
		"tesla_oem": None,
		"stock_qty": 0.0,
		"supplier": "tesla_oem",
		"extra_suppliers": ("euro_aftermarket",),
		"frt": 1.0,
		"warranty": "12 Months / 20,000 km",
	},
)

_PACKAGE_SPECS: tuple[dict[str, Any], ...] = (
	{
		"handle": "symmetrical_fore_link",
		"package_name": "Link - Lower - Front - Fore - LH (Remove & Replace)",
		"applicable_model": "Model 3",
		"applicable_generation": "Model 3 Jun 2017 - Dec 2023",
		"correction_code": "31011802",
		"rh_correction_code": "31011902",
		"standard_labor_hours": 1.2,
		"hourly_rate": 140.0,
		"is_flat_rate": 0,
		"equipment_tags": ("Lift",),
		"is_symmetrical": 1,
		"items": (
			{"slot_number": 1, "item_handle": "fore_link_lh_oem", "qty": 1.0, "side": "LH", "is_default": 1},
			{"slot_number": 1, "item_handle": "fore_link_lh_aft", "qty": 1.0, "side": "LH", "is_default": 0},
			{"slot_number": 1, "item_handle": "fore_link_rh_oem", "qty": 1.0, "side": "RH", "is_default": 1},
			{"slot_number": 1, "item_handle": "fore_link_rh_aft", "qty": 1.0, "side": "RH", "is_default": 0},
			{"slot_number": 2, "item_handle": "susp_bolt_kit", "qty": 1.0, "side": "Both", "is_default": 1},
		),
	},
	{
		"handle": "front_brake_overhaul",
		"package_name": "Front Brake Pads, Rotors & Fluid Overhaul",
		"applicable_model": "Model Y",
		"applicable_generation": "Model Y Jan 2020 - Present",
		"correction_code": "33010402",
		"standard_labor_hours": 2.0,
		"hourly_rate": 140.0,
		"is_flat_rate": 0,
		"equipment_tags": ("Lift",),
		"is_symmetrical": 0,
		"items": (
			{"slot_number": 1, "item_handle": "front_pad_set_oem", "qty": 1.0, "side": "Both", "is_default": 1},
			{"slot_number": 1, "item_handle": "front_pad_set_aft", "qty": 1.0, "side": "Both", "is_default": 0},
			{"slot_number": 2, "item_handle": "front_rotor_oem", "qty": 2.0, "side": "Both", "is_default": 1},
			{"slot_number": 2, "item_handle": "front_rotor_aft", "qty": 2.0, "side": "Both", "is_default": 0},
			{"slot_number": 3, "item_handle": "dot4_brake_fluid", "qty": 1.0, "side": "Both", "is_default": 1},
		),
	},
	{
		"handle": "four_wheel_alignment",
		"package_name": "Four-Wheel Thrust & Camber Alignment",
		"applicable_model": "All Models",
		"correction_code": "31002100",
		"standard_labor_hours": 1.0,
		"hourly_rate": 0.0,
		"service_rate": 169.00,
		"is_flat_rate": 1,
		"equipment_tags": ("Lift", "Alignment Rack"),
		"is_symmetrical": 0,
		"items": (),
	},
	{
		"handle": "cabin_filter_service",
		"package_name": "Cabin HEPA & Carbon Air Filter Replacement",
		"applicable_model": "All Models",
		"correction_code": "18105401",
		"standard_labor_hours": 0.5,
		"hourly_rate": 140.0,
		"is_flat_rate": 0,
		"equipment_tags": (),
		"is_symmetrical": 0,
		"items": (
			{"slot_number": 1, "item_handle": "cabin_hepa_filter_oem", "qty": 1.0, "side": "Both", "is_default": 1},
			{"slot_number": 1, "item_handle": "cabin_hepa_filter_aft", "qty": 1.0, "side": "Both", "is_default": 0},
		),
	},
	{
		"handle": "wiper_blade_service",
		"package_name": "Front Wiper Blade Set Replacement",
		"applicable_model": "All Models",
		"correction_code": "17501300",
		"standard_labor_hours": 0.3,
		"hourly_rate": 0.0,
		"service_rate": 65.00,
		"is_flat_rate": 1,
		"equipment_tags": (),
		"is_symmetrical": 0,
		"items": (
			{"slot_number": 1, "item_handle": "wiper_blade_set_oem", "qty": 1.0, "side": "Both", "is_default": 1},
		),
	},
	{
		"handle": "tire_rotation_balance",
		"package_name": "Four-Tire Rotation & Road Force Balance",
		"applicable_model": "All Models",
		"correction_code": "34015000",
		"standard_labor_hours": 0.5,
		"hourly_rate": 0.0,
		"service_rate": 89.00,
		"is_flat_rate": 1,
		"equipment_tags": ("Lift", "Tire Mount & Road Force Balancer"),
		"is_symmetrical": 0,
		"items": (),
	},
	{
		"handle": "brake_fluid_flush",
		"package_name": "DOT 4 Brake Fluid Thermal Flush & Bleed",
		"applicable_model": "All Models",
		"correction_code": "33031502",
		"standard_labor_hours": 1.0,
		"hourly_rate": 140.0,
		"is_flat_rate": 0,
		"equipment_tags": ("Lift",),
		"is_symmetrical": 0,
		"items": (
			{"slot_number": 1, "item_handle": "dot4_brake_fluid", "qty": 2.0, "side": "Both", "is_default": 1},
		),
	},
	{
		"handle": "hv_isolation_diagnostic",
		"package_name": "High-Voltage Isolation & Drive Unit Diagnostic",
		"applicable_model": "All Models",
		"correction_code": "16300100",
		"standard_labor_hours": 1.0,
		"hourly_rate": 140.0,
		"is_flat_rate": 0,
		"equipment_tags": ("EV High Voltage",),
		"is_symmetrical": 0,
		"items": (),
	},
	{
		"handle": "hv_pyro_fuse_replace",
		"package_name": "HV Battery Pyrotechnic Disconnect Fuse Replacement",
		"applicable_model": "Model S",
		"correction_code": "16013202",
		"standard_labor_hours": 2.5,
		"hourly_rate": 140.0,
		"is_flat_rate": 0,
		"equipment_tags": ("Lift", "EV High Voltage", "HV Battery Station"),
		"is_symmetrical": 0,
		"items": (
			{"slot_number": 1, "item_handle": "hv_pyro_fuse_oem", "qty": 1.0, "side": "Both", "is_default": 1},
			{"slot_number": 2, "item_handle": "coolant_g48", "qty": 1.0, "side": "Both", "is_default": 1},
		),
	},
	{
		"handle": "rear_subframe_camber_sym",
		"package_name": "Link - Rear Subframe Camber - LH (Remove & Replace)",
		"applicable_model": "Model X",
		"correction_code": "31034102",
		"rh_correction_code": "31034202",
		"standard_labor_hours": 1.8,
		"hourly_rate": 140.0,
		"is_flat_rate": 0,
		"equipment_tags": ("Lift",),
		"is_symmetrical": 1,
		"items": (
			{"slot_number": 1, "item_handle": "rear_subframe_arm_lh", "qty": 1.0, "side": "LH", "is_default": 1},
			{"slot_number": 1, "item_handle": "rear_subframe_arm_rh", "qty": 1.0, "side": "RH", "is_default": 1},
		),
	},
)

PACKAGE_EQUIPMENT_TAGS: dict[str, list[str]] = {
	spec["handle"]: list(spec["equipment_tags"])
	for spec in _PACKAGE_SPECS
	if spec.get("equipment_tags")
}

_UNDERCARRIAGE_INSPECTION_ITEMS: tuple[str, ...] = (
	"Front Brake Pad Thickness (mm) & Rotor Runout",
	"Rear Brake Pad Thickness (mm) & Caliper Slide Pins",
	"Brake Fluid Moisture & Boiling Point Check",
	"Front Lower Fore & Aft Compliance Link Bushings",
	"Rear Subframe Camber & Toe Link Play",
	"Tire Tread Depth (FL / FR / RL / RR) & Wear Pattern",
	"HV Battery Pack Skid Plate & Aero Shield Integrity",
	"HV Orange Harness Conduit & Thermal Valve Leaks",
	"12V / 16V Low-Voltage Auxiliary Battery State of Health",
	"Cabin HEPA Air Filter & Wiper Blade Condition",
)

_CUSTOMERS: tuple[dict[str, Any], ...] = (
	{
		"handle": "retail_1",
		"customer_name": "Liam Chen",
		"customer_type": "Individual",
		"customer_group": "Individual",
		"email": "liam.chen@example.ca",
		"phone": "+1-416-555-0101",
		"street": "148 Spadina Ave",
		"city": "Toronto",
		"state": "ON",
		"postal_code": "M5V 2L4",
	},
	{
		"handle": "retail_2",
		"customer_name": "Sophia Desjardins",
		"customer_type": "Individual",
		"customer_group": "Individual",
		"email": "sophia.desjardins@example.ca",
		"phone": "+1-647-555-0142",
		"street": "82 Queen St E",
		"city": "Mississauga",
		"state": "ON",
		"postal_code": "L5G 1N1",
	},
	{
		"handle": "retail_3",
		"customer_name": "Owen MacLeod",
		"customer_type": "Individual",
		"customer_group": "Individual",
		"email": "owen.macleod@example.ca",
		"phone": "+1-905-555-0188",
		"street": "410 Lakeshore Rd W",
		"city": "Oakville",
		"state": "ON",
		"postal_code": "L6K 1G1",
	},
	{
		"handle": "household_primary",
		"customer_name": "Hannah Kowalski",
		"customer_type": "Individual",
		"customer_group": "Individual",
		"email": "hannah.kowalski@example.ca",
		"phone": "+1-416-555-0215",
		"street": "29 Birchwood Dr",
		"city": "Markham",
		"state": "ON",
		"postal_code": "L3R 4B8",
	},
	{
		"handle": "household_secondary",
		"customer_name": "Daniel Kowalski",
		"customer_type": "Individual",
		"customer_group": "Individual",
		"email": "daniel.kowalski@example.ca",
		"phone": "+1-416-555-0216",
		"street": "29 Birchwood Dr",
		"city": "Markham",
		"state": "ON",
		"postal_code": "L3R 4B8",
	},
	{
		"handle": "fleet_1",
		"customer_name": "Metro Green Rideshare Fleet",
		"customer_type": "Company",
		"customer_group": "Commercial",
		"email": "fleetops@metrogreen.example.ca",
		"phone": "+1-416-555-0900",
		"street": "900 Commissioners St",
		"city": "Toronto",
		"state": "ON",
		"postal_code": "M4M 1A8",
	},
)

_VEHICLES: tuple[dict[str, Any], ...] = (
	{
		"handle": "veh_model3_liam",
		"model_code": "3",
		"year_code": "P",
		"plate": "LEV-301",
		"color": "Midnight Silver Metallic",
		"customer": "retail_1",
		"odometer": 48200,
		"deferred": (
			{
				"job_title": "Front Lower Fore Compliance Link Replacement (LH & RH)",
				"estimated_amount": 780.00,
				"safety_hazard": 1,
				"decline_reason": "Customer requested follow-up next week; audible clunk over speed bumps.",
			},
			{
				"job_title": "Four-Wheel Thrust & Camber Alignment",
				"estimated_amount": 169.00,
				"safety_hazard": 0,
				"decline_reason": "Defer until front compliance links are replaced.",
			},
		),
	},
	{
		"handle": "veh_modely_sophia",
		"model_code": "Y",
		"year_code": "R",
		"plate": "LEV-Y02",
		"color": "Pearl White Multi-Coat",
		"customer": "retail_2",
		"odometer": 34100,
		"deferred": (),
	},
	{
		"handle": "veh_models_owen",
		"model_code": "S",
		"year_code": "P",
		"plate": "LEV-S03",
		"color": "Ultra Red",
		"customer": "retail_3",
		"odometer": 27650,
		"deferred": (),
	},
	{
		"handle": "veh_modelx_household",
		"model_code": "X",
		"year_code": "P",
		"plate": "KOW-X01",
		"color": "Deep Blue Metallic",
		"customer": "household_primary",
		"co_owners": (("household_secondary", "Spouse / Co-Driver"),),
		"odometer": 52400,
		"deferred": (
			{
				"job_title": "Rear Brake Pad Replacement (3mm Remaining)",
				"estimated_amount": 495.00,
				"safety_hazard": 1,
				"decline_reason": "Driver opted to re-book before winter highway trip.",
			},
		),
	},
	{
		"handle": "veh_model3_household",
		"model_code": "3",
		"year_code": "R",
		"plate": "KOW-302",
		"color": "Stealth Grey",
		"customer": "household_primary",
		"co_owners": (("household_secondary", "Spouse / Co-Driver"),),
		"odometer": 19800,
		"deferred": (),
	},
	{
		"handle": "veh_fleet_1",
		"model_code": "3",
		"year_code": "N",
		"plate": "MGR-101",
		"color": "Solid Black",
		"customer": "fleet_1",
		"odometer": 112400,
		"deferred": (
			{
				"job_title": "DOT 4 Brake Fluid Thermal Flush & Bleed",
				"estimated_amount": 196.00,
				"safety_hazard": 0,
				"decline_reason": "Fleet manager scheduled for next PM cycle.",
			},
		),
	},
	{
		"handle": "veh_fleet_2",
		"model_code": "3",
		"year_code": "P",
		"plate": "MGR-102",
		"color": "Solid Black",
		"customer": "fleet_1",
		"odometer": 87900,
		"deferred": (),
	},
	{
		"handle": "veh_fleet_3",
		"model_code": "3",
		"year_code": "R",
		"plate": "MGR-103",
		"color": "Pearl White Multi-Coat",
		"customer": "fleet_1",
		"odometer": 64300,
		"deferred": (),
	},
	{
		"handle": "veh_fleet_4",
		"model_code": "Y",
		"year_code": "P",
		"plate": "MGR-201",
		"color": "Midnight Silver Metallic",
		"customer": "fleet_1",
		"odometer": 94150,
		"deferred": (),
	},
	{
		"handle": "veh_fleet_5",
		"model_code": "Y",
		"year_code": "R",
		"plate": "MGR-202",
		"color": "Pearl White Multi-Coat",
		"customer": "fleet_1",
		"odometer": 51200,
		"deferred": (),
	},
)


def _round_cents(val: float) -> float:
	return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_catalog_part_price(cost: float, tesla_oem_price: Optional[float] = None) -> float:
	"""Compute sell price in CAD aligned with ism-agent-os/rules/PARTS_PRICING.md."""
	cost_val = max(0.0, float(cost))
	if tesla_oem_price is not None and float(tesla_oem_price) > 0:
		oem = float(tesla_oem_price)
		tesla_based = oem if oem <= 20.0 else oem * 0.70
		tesla_based = max(5.0, tesla_based)
		if cost_val > 0:
			cost_floor = cost_val + max(cost_val * 0.25, 5.0)
			return _round_cents(max(tesla_based, cost_floor))
		return _round_cents(tesla_based)
	fallback = max(cost_val + max(cost_val * 0.40, 5.0), 5.0)
	return _round_cents(fallback)


def generate_oem_part_number(
	rng: random.Random,
	prefix_7: Optional[int] = None,
	rev: Optional[str] = None,
) -> tuple[str, str]:
	"""Generate deterministic Tesla-style OEM part number (NNNNNNN-00-R) and revision."""
	num = prefix_7 if prefix_7 is not None else rng.randint(1040000, 1989999)
	revision = rev or rng.choice(["A", "B", "C", "D", "E", "F"])
	return f"{num:07d}-00-{revision}", revision


def generate_synthetic_vin(
	rng: random.Random,
	model_code: str = "3",
	year_code: str = "P",
	plant_code: Optional[str] = None,
	seq: Optional[int] = None,
) -> str:
	"""Generate a valid 17-character NHTSA check-digit Tesla VIN decodable offline."""
	code = model_code.upper()
	wmi, body, restraint, fuel, drive = _VIN_ARCHETYPES.get(code, _VIN_ARCHETYPES["3"])
	plant = plant_code or _PLANT_FOR_MODEL.get(code, "F")
	seq_num = seq if seq is not None else rng.randint(100000, 899999)
	draft = f"{wmi}{code}{body}{restraint}{fuel}{drive}0{year_code.upper()}{plant}{seq_num:06d}"
	check = compute_vin_check_digit(draft)
	vin = f"{draft[:8]}{check}{draft[9:]}"
	decoded = decode_tesla_vin(vin)
	if not decoded.get("is_valid_vin"):
		raise ValueError(f"Generated invalid Tesla VIN '{vin}': {decoded}")
	return vin


def _ensure_doc(doctype: str, name_or_filters: Any, payload: dict[str, Any]) -> Any:
	"""Fetch an existing document or insert a new one idempotently."""
	existing_name = (
		name_or_filters
		if isinstance(name_or_filters, str) and frappe.db.exists(doctype, name_or_filters)
		else frappe.db.get_value(doctype, name_or_filters, "name") if isinstance(name_or_filters, dict) else None
	)
	if existing_name:
		return frappe.get_doc(doctype, existing_name)
	doc = frappe.get_doc({"doctype": doctype, **payload})
	doc.insert(ignore_permissions=True)
	return doc


def _ensure_bootstrap_base() -> None:
	"""Ensure CAD currency, Canada, UOMs, Party Types, and Stock Entry Types exist before company setup."""
	if frappe.db.exists("Currency", CANONICAL_CURRENCY):
		if not frappe.db.get_value("Currency", CANONICAL_CURRENCY, "enabled"):
			frappe.db.set_value("Currency", CANONICAL_CURRENCY, "enabled", 1)
	else:
		frappe.get_doc({
			"doctype": "Currency",
			"currency_name": CANONICAL_CURRENCY,
			"symbol": "$",
			"fraction": "Cent",
			"fraction_units": 100,
			"number_format": "#,###.##",
			"enabled": 1,
		}).insert(ignore_permissions=True)

	if not frappe.db.exists("Country", CANONICAL_COUNTRY):
		frappe.get_doc({
			"doctype": "Country",
			"country_name": CANONICAL_COUNTRY,
			"code": "ca",
			"time_zones": CANONICAL_TIMEZONE,
		}).insert(ignore_permissions=True)

	for uom_name in _UOMS:
		if not frappe.db.exists("UOM", uom_name):
			frappe.get_doc({"doctype": "UOM", "uom_name": uom_name, "must_be_whole_number": 0}).insert(
				ignore_permissions=True
			)

	for party_type, account_type in (("Customer", "Receivable"), ("Supplier", "Payable"), ("Employee", "Payable")):
		if not frappe.db.exists("Party Type", party_type):
			frappe.get_doc({"doctype": "Party Type", "party_type": party_type, "account_type": account_type}).insert(
				ignore_permissions=True
			)

	for se_type, purpose in (
		("Material Receipt", "Material Receipt"),
		("Material Issue", "Material Issue"),
		("Material Transfer", "Material Transfer"),
	):
		if not frappe.db.exists("Stock Entry Type", se_type):
			frappe.get_doc({"doctype": "Stock Entry Type", "name": se_type, "purpose": purpose}).insert(
				ignore_permissions=True
			)


def _ensure_ism_tree_nodes() -> None:
	"""Ensure Item/Customer/Supplier Groups and Territories exist after company setup."""
	_ensure_tree_node("Item Group", "All Item Groups", None, is_group=1)
	for ig in _ITEM_GROUPS:
		_ensure_tree_node("Item Group", ig, "All Item Groups", is_group=0)

	_ensure_tree_node("Customer Group", "All Customer Groups", None, is_group=1)
	for cg in ("Individual", "Commercial"):
		_ensure_tree_node("Customer Group", cg, "All Customer Groups", is_group=0)

	_ensure_tree_node("Supplier Group", "All Supplier Groups", None, is_group=1)
	for sg in ("Distributor", "Local", "Services"):
		_ensure_tree_node("Supplier Group", sg, "All Supplier Groups", is_group=0)

	_ensure_tree_node("Territory", "All Territories", None, is_group=1)
	_ensure_tree_node("Territory", CANONICAL_COUNTRY, "All Territories", is_group=0)


def _ensure_tree_node(doctype: str, name: str, parent: Optional[str], *, is_group: int) -> None:
	if frappe.db.exists(doctype, name):
		return
	name_field = {
		"Item Group": "item_group_name",
		"Customer Group": "customer_group_name",
		"Supplier Group": "supplier_group_name",
		"Territory": "territory_name",
	}[doctype]
	parent_field = f"parent_{doctype.lower().replace(' ', '_')}"
	payload: dict[str, Any] = {"doctype": doctype, name_field: name, "is_group": is_group}
	if parent:
		payload[parent_field] = parent
	frappe.get_doc(payload).insert(ignore_permissions=True)


def _ensure_canonical_company() -> Any:
	"""Create Lightspeed EV via Setup Wizard on clean site or direct Company insert fallback."""
	if frappe.db.exists("Company", CANONICAL_COMPANY):
		return frappe.get_doc("Company", CANONICAL_COMPANY)

	sp_name = "ism_catalog_setup_wizard"
	frappe.db.savepoint(sp_name)
	try:
		from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

		setup_complete({
			"language": "en",
			"country": CANONICAL_COUNTRY,
			"timezone": CANONICAL_TIMEZONE,
			"currency": CANONICAL_CURRENCY,
			"company_name": CANONICAL_COMPANY,
			"company_abbr": CANONICAL_ABBR,
			"fy_start_date": "2026-01-01",
			"fy_end_date": "2026-12-31",
			"full_name": "Claire Sterling",
			"email": "manager@lightspeedev.ca",
			"password": "ism_sim_admin_password_2026",
			"enable_view_tracking": 0,
		})
	except Exception:
		frappe.db.rollback(save_point=sp_name)
	else:
		if not frappe.db.exists("Company", CANONICAL_COMPANY):
			frappe.db.rollback(save_point=sp_name)

	if not frappe.db.exists("Company", CANONICAL_COMPANY):
		comp = frappe.get_doc({
			"doctype": "Company",
			"company_name": CANONICAL_COMPANY,
			"abbr": CANONICAL_ABBR,
			"default_currency": CANONICAL_CURRENCY,
			"country": CANONICAL_COUNTRY,
			"chart_of_accounts": "Standard",
		})
		comp.insert(ignore_permissions=True)

	return frappe.get_doc("Company", CANONICAL_COMPANY)


def _ensure_fiscal_years() -> None:
	for year in (2024, 2025, 2026, 2027):
		fy_name = str(year)
		if frappe.db.exists("Fiscal Year", fy_name):
			fy = frappe.get_doc("Fiscal Year", fy_name)
			if not any(r.company == CANONICAL_COMPANY for r in (fy.companies or [])):
				fy.append("companies", {"company": CANONICAL_COMPANY})
				fy.save(ignore_permissions=True)
			continue
		fy = frappe.get_doc({
			"doctype": "Fiscal Year",
			"year": fy_name,
			"year_start_date": f"{year}-01-01",
			"year_end_date": f"{year}-12-31",
			"disabled": 0,
			"companies": [{"company": CANONICAL_COMPANY}],
		})
		fy.insert(ignore_permissions=True)


def _ensure_leaf_account(account_name: str, parent_account: str, account_type: str) -> str:
	full_name = f"{account_name} - {CANONICAL_ABBR}"
	if frappe.db.exists("Account", full_name):
		return full_name
	existing = frappe.db.get_value(
		"Account",
		{"company": CANONICAL_COMPANY, "account_type": account_type, "is_group": 0},
		"name",
	)
	if existing and account_type != "Bank":
		return existing
	if not frappe.db.exists("Account", parent_account):
		parent_account = (
			frappe.db.get_value(
				"Account",
				{"company": CANONICAL_COMPANY, "is_group": 1, "account_type": account_type},
				"name",
			)
			or frappe.db.get_value(
				"Account",
				{"company": CANONICAL_COMPANY, "is_group": 1, "root_type": "Asset"},
				"name",
			)
		)
	acc = frappe.get_doc({
		"doctype": "Account",
		"account_name": account_name,
		"company": CANONICAL_COMPANY,
		"parent_account": parent_account,
		"account_type": account_type,
		"account_currency": CANONICAL_CURRENCY,
		"is_group": 0,
	})
	acc.insert(ignore_permissions=True)
	return acc.name


def _ensure_gl_and_global_defaults(comp: Any) -> tuple[str, str]:
	bank_parent = (
		frappe.db.get_value("Account", {"company": CANONICAL_COMPANY, "account_name": "Bank Accounts", "is_group": 1}, "name")
		or f"Bank Accounts - {CANONICAL_ABBR}"
	)
	cash_parent = (
		frappe.db.get_value("Account", {"company": CANONICAL_COMPANY, "account_name": "Cash In Hand", "is_group": 1}, "name")
		or f"Cash In Hand - {CANONICAL_ABBR}"
	)
	bank_acc = _ensure_leaf_account("Operating Bank", bank_parent, "Bank")
	cash_acc = (
		comp.default_cash_account
		if comp.default_cash_account and frappe.db.exists("Account", comp.default_cash_account)
		else _ensure_leaf_account("Cash", cash_parent, "Cash")
	)

	updates: dict[str, Any] = {}
	if comp.default_bank_account != bank_acc:
		updates["default_bank_account"] = bank_acc
	if comp.default_cash_account != cash_acc:
		updates["default_cash_account"] = cash_acc
	if comp.default_currency != CANONICAL_CURRENCY:
		updates["default_currency"] = CANONICAL_CURRENCY
	if updates:
		for fieldname, val in updates.items():
			comp.set(fieldname, val)
		comp.save(ignore_permissions=True)

	frappe.db.set_single_value(
		"Global Defaults",
		{"default_company": CANONICAL_COMPANY, "default_currency": CANONICAL_CURRENCY, "country": CANONICAL_COUNTRY},
	)
	frappe.defaults.set_global_default("company", CANONICAL_COMPANY)
	frappe.defaults.set_global_default("currency", CANONICAL_CURRENCY)
	frappe.db.set_single_value(
		"System Settings",
		{"country": CANONICAL_COUNTRY, "time_zone": CANONICAL_TIMEZONE, "setup_complete": 1},
	)
	frappe.db.set_single_value(
		"Selling Settings",
		{"selling_price_list": "Standard Selling", "cust_master_name": "Customer Name"},
	)
	frappe.db.set_single_value(
		"Buying Settings",
		{"buying_price_list": "Standard Buying", "supp_master_name": "Supplier Name", "po_required": "No", "pr_required": "No"},
	)
	return bank_acc, cash_acc


def _ensure_canonical_hst_taxes(comp: Any) -> tuple[str, str]:
	"""Enforce canonical 13% HST Account, Sales/Purchase Tax Templates, and generic Tax Rules."""
	c_name = comp.name
	abbr = comp.abbr or CANONICAL_ABBR
	currency = comp.default_currency or CANONICAL_CURRENCY

	duties_and_taxes = (
		frappe.db.get_value("Account", {"account_name": "Duties and Taxes", "company": c_name}, "name")
		or f"Duties and Taxes - {abbr}"
	)
	account_name = f"HST 13% - {abbr}"
	if not frappe.db.exists("Account", account_name) and frappe.db.exists("Account", duties_and_taxes):
		frappe.get_doc({
			"doctype": "Account",
			"account_name": "HST 13%",
			"company": c_name,
			"parent_account": duties_and_taxes,
			"account_type": "Tax",
			"currency": currency,
		}).insert(ignore_permissions=True)
	tax_head = account_name if frappe.db.exists("Account", account_name) else duties_and_taxes

	template_name = f"13% HST - {abbr}"
	template_specs = (
		(
			"Sales Taxes and Charges Template",
			"Sales",
			"sales_tax_template",
			{
				"charge_type": "On Net Total",
				"account_head": tax_head,
				"rate": 13.0,
				"description": "HST @ 13.0%",
			},
		),
		(
			"Purchase Taxes and Charges Template",
			"Purchase",
			"purchase_tax_template",
			{
				"category": "Total",
				"add_deduct_tax": "Add",
				"charge_type": "On Net Total",
				"account_head": tax_head,
				"rate": 13.0,
				"description": "HST @ 13.0%",
			},
		),
	)
	for tmpl_doctype, tax_type, rule_field, tax_row in template_specs:
		for other_name in frappe.get_all(
			tmpl_doctype,
			filters={"company": c_name, "name": ["!=", template_name]},
			pluck="name",
		):
			frappe.db.set_value(tmpl_doctype, other_name, {"is_default": 0, "disabled": 1})
		_ensure_company_tax_template(tmpl_doctype, c_name, template_name, tax_row)
		if frappe.db.exists(tmpl_doctype, template_name):
			frappe.db.set_value(tmpl_doctype, template_name, {"is_default": 1, "disabled": 0})

		generic_rules = _get_generic_tax_rules(tax_type, c_name)
		if generic_rules:
			for rule in generic_rules:
				if rule.get(rule_field) != template_name:
					frappe.db.set_value("Tax Rule", rule["name"], rule_field, template_name)
		else:
			_ensure_default_tax_rule(tax_type, template_name, c_name)

	if not frappe.db.exists("Item Tax Template", {"company": c_name, "disabled": 0}) and not frappe.db.exists(
		"Item Tax Template", template_name
	):
		frappe.get_doc({
			"doctype": "Item Tax Template",
			"title": "13% HST",
			"company": c_name,
			"taxes": [{"tax_type": tax_head, "tax_rate": 13.0}],
		}).insert(ignore_permissions=True)

	if frappe.db.exists("DocType", "Accounts Settings"):
		frappe.db.set_single_value("Accounts Settings", "add_taxes_from_taxes_and_charges_template", 1)

	return template_name, template_name


def seed_accounting_and_settings(manifest: SimManifest) -> SimManifest:
	"""Phase 1.1: Seed canonical company, 13% HST taxes, price lists, payment modes, and ISM Settings."""
	_ensure_bootstrap_base()
	comp = _ensure_canonical_company()
	_ensure_ism_tree_nodes()
	_ensure_fiscal_years()
	bank_acc, cash_acc = _ensure_gl_and_global_defaults(comp)

	for pl_name, is_selling, is_buying in (
		("Standard Selling", 1, 0),
		("Standard Buying", 0, 1),
	):
		pl = _ensure_doc(
			"Price List",
			pl_name,
			{
				"price_list_name": pl_name,
				"currency": CANONICAL_CURRENCY,
				"selling": is_selling,
				"buying": is_buying,
				"enabled": 1,
			},
		)
		manifest.register("price_lists", "selling" if is_selling else "buying", pl.name)

	sales_tax, purchase_tax = _ensure_canonical_hst_taxes(comp)
	if sales_tax:
		manifest.register("tax_templates", "sales_hst_13", sales_tax)
	if purchase_tax:
		manifest.register("tax_templates", "purchase_hst_13", purchase_tax)

	for mop_name, mop_type, acct_kind in _MODES_OF_PAYMENT:
		target_acct = bank_acc if acct_kind == "bank" else cash_acc
		mop = _ensure_doc(
			"Mode of Payment",
			mop_name,
			{"mode_of_payment": mop_name, "type": mop_type, "enabled": 1},
		)
		dirty = False
		if not mop.enabled:
			mop.enabled = 1
			dirty = True
		existing_row = next((r for r in (mop.accounts or []) if r.company == CANONICAL_COMPANY), None)
		if existing_row is None:
			mop.append("accounts", {"company": CANONICAL_COMPANY, "default_account": target_acct})
			dirty = True
		elif existing_row.default_account != target_acct:
			existing_row.default_account = target_acct
			dirty = True
		if dirty:
			mop.save(ignore_permissions=True)
		handle = mop_name.lower().replace(" / ", "_").replace(" ", "_").replace("-", "_")
		manifest.register("modes_of_payment", handle, mop.name)

	# Ensure ISM custom fields, roles, and core metadata are in place before catalog items/packages.
	from ism.setup import _run_setup_tasks

	_run_setup_tasks(full=True)

	settings = frappe.get_single("ISM Settings")
	canonical_settings = {
		"operating_hours_start": "08:00:00",
		"operating_hours_end": "17:00:00",
		"break_start": "12:00:00",
		"break_end": "12:30:00",
		"default_slot_interval": 30,
		"scheduling_horizon_days": 30,
		"technician_designation": "Technician",
		"enable_technician_capacity": 1,
		"estimation_percentile": 80.0,
		"estimation_sigma": 0.30,
		"buffer_padding_minutes": 0,
		"default_diagnostic_duration": 60,
		"default_labor_rate": 140.0,
		"shop_supplies_percentage": 0.0,
		"shop_supplies_cap": 0.0,
		"cost_tolerance_percentage": 10.0,
		"cost_tolerance_flat_amount": 20.0,
		"authorization_token_expiry_hours": 48,
		"sms_gateway_provider": "Disabled",
	}
	for fieldname, val in canonical_settings.items():
		settings.set(fieldname, val)
	settings.save(ignore_permissions=True)

	manifest.company = CANONICAL_COMPANY
	manifest.company_abbr = CANONICAL_ABBR
	manifest.currency = CANONICAL_CURRENCY
	return manifest


def _ensure_holiday_lists() -> tuple[str, str]:
	lists: list[str] = []
	for year, holidays in (
		(2026, (("2026-01-01", "New Year's Day"), ("2026-12-25", "Christmas Day"))),
		(2027, (("2027-01-01", "New Year's Day"), ("2027-12-25", "Christmas Day"))),
	):
		hl_name = f"Lightspeed EV Holidays {year}"
		if not frappe.db.exists("Holiday List", hl_name):
			hl = frappe.get_doc({
				"doctype": "Holiday List",
				"holiday_list_name": hl_name,
				"from_date": f"{year}-01-01",
				"to_date": f"{year}-12-31",
				"holidays": [{"holiday_date": dt, "description": desc} for dt, desc in holidays],
			})
			hl.insert(ignore_permissions=True)
		lists.append(hl_name)

	frappe.db.set_value("Company", CANONICAL_COMPANY, "default_holiday_list", lists[0])
	frappe.db.set_single_value("ISM Settings", "holiday_list", lists[0])
	return lists[0], lists[1]


def _ensure_holiday_list_assignment(hl_name: str, from_date: str) -> None:
	if not frappe.db.exists("DocType", "Holiday List Assignment"):
		return
	existing = frappe.db.get_value(
		"Holiday List Assignment",
		{
			"applicable_for": "Company",
			"assigned_to": CANONICAL_COMPANY,
			"holiday_list": hl_name,
			"docstatus": 1,
		},
		"name",
	)
	if existing:
		return
	hla = frappe.get_doc({
		"doctype": "Holiday List Assignment",
		"applicable_for": "Company",
		"assigned_to": CANONICAL_COMPANY,
		"holiday_list": hl_name,
		"from_date": from_date,
	})
	hla.insert(ignore_permissions=True)
	hla.submit()


def seed_workforce_and_leave(manifest: SimManifest) -> SimManifest:
	"""Phase 1.2: Seed RBAC users, Technician employees, Holiday Lists, and active Leave Allocations."""
	for persona in _PERSONAS:
		if frappe.db.exists("User", persona["email"]):
			user_doc = frappe.get_doc("User", persona["email"])
		else:
			user_doc = frappe.get_doc({
				"doctype": "User",
				"email": persona["email"],
				"first_name": persona["first_name"],
				"last_name": persona["last_name"],
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
			})
			user_doc.flags.ignore_password_policy = True
			user_doc.insert(ignore_permissions=True)

		desired_roles = set(persona["roles"])
		current_roles = {r.role for r in (user_doc.roles or [])}
		if current_roles != desired_roles:
			user_doc.set("roles", [{"role": r} for r in persona["roles"]])
			user_doc.flags.ignore_password_policy = True
			user_doc.save(ignore_permissions=True)
		manifest.register("users", persona["handle"], user_doc.name)

	hl_2026, hl_2027 = _ensure_holiday_lists()
	manifest.holiday_list = hl_2026
	_ensure_holiday_list_assignment(hl_2026, "2026-01-01")
	_ensure_holiday_list_assignment(hl_2027, "2027-01-01")

	if not frappe.db.exists("Designation", "Technician"):
		frappe.get_doc({"doctype": "Designation", "designation_name": "Technician"}).insert(ignore_permissions=True)

	active_emp_ids: list[str] = []
	for spec in _EMPLOYEES:
		existing = frappe.db.get_value("Employee", {"employee_name": spec["employee_name"]}, "name")
		if existing:
			emp = frappe.get_doc("Employee", existing)
		else:
			payload: dict[str, Any] = {
				"doctype": "Employee",
				"first_name": spec["first_name"],
				"last_name": spec["last_name"],
				"employee_name": spec["employee_name"],
				"status": spec["status"],
				"gender": spec["gender"],
				"date_of_birth": spec["date_of_birth"],
				"date_of_joining": spec["date_of_joining"],
				"company": CANONICAL_COMPANY,
				"designation": "Technician",
				"holiday_list": hl_2026,
			}
			if "user_id" in spec:
				payload["user_id"] = spec["user_id"]
			if "relieving_date" in spec:
				payload["relieving_date"] = spec["relieving_date"]
			emp = frappe.get_doc(payload).insert(ignore_permissions=True)
		manifest.register("techs", spec["handle"], emp.name)
		if spec["status"] == "Active":
			active_emp_ids.append(emp.name)

	for lt_handle, lt_name, max_days in (
		("privilege", "Privilege Leave", 20),
		("sick", "Sick Leave", 15),
		("casual", "Casual Leave", 15),
	):
		if frappe.db.exists("DocType", "Leave Type"):
			if not frappe.db.exists("Leave Type", lt_name):
				frappe.get_doc({
					"doctype": "Leave Type",
					"leave_type_name": lt_name,
					"max_leaves_allowed": max_days,
					"is_lwp": 0,
					"include_holiday": 0,
					"allow_negative": 0,
				}).insert(ignore_permissions=True)
			manifest.register("leave_types", lt_handle, lt_name)

	if frappe.db.exists("DocType", "Leave Allocation"):
		for emp_id in active_emp_ids:
			for year in (2026, 2027):
				from_dt, to_dt = f"{year}-01-01", f"{year}-12-31"
				for lt_name, days in (("Privilege Leave", 15), ("Sick Leave", 10), ("Casual Leave", 10)):
					exists = frappe.db.exists(
						"Leave Allocation",
						{
							"employee": emp_id,
							"leave_type": lt_name,
							"from_date": from_dt,
							"to_date": to_dt,
							"docstatus": 1,
						},
					)
					if not exists:
						alloc = frappe.get_doc({
							"doctype": "Leave Allocation",
							"employee": emp_id,
							"leave_type": lt_name,
							"from_date": from_dt,
							"to_date": to_dt,
							"new_leaves_allocated": days,
							"company": CANONICAL_COMPANY,
						})
						alloc.insert(ignore_permissions=True)
						alloc.submit()

	return manifest


def seed_shop_floor(manifest: SimManifest) -> SimManifest:
	"""Phase 1.3: Seed Equipment Tags, Service Bays, and canonical Schedule Entry Types."""
	for handle, tag_name, desc in _EQUIPMENT_TAGS:
		tag = _ensure_doc("Equipment Tag", tag_name, {"tag_name": tag_name, "description": desc})
		manifest.register("equipment_tags", handle, tag.name)

	for spec in _SERVICE_BAYS:
		if frappe.db.exists("Service Bay", spec["bay_name"]):
			bay = frappe.get_doc("Service Bay", spec["bay_name"])
		else:
			bay = frappe.get_doc({
				"doctype": "Service Bay",
				"bay_name": spec["bay_name"],
				"is_active": spec["is_active"],
				"description": spec["description"],
				"equipment": [{"equipment_tag": tag} for tag in spec["tags"]],
			}).insert(ignore_permissions=True)
		manifest.register("bays", spec["handle"], bay.name)

	seed_schedule_entry_types()
	for se_type in frappe.get_all("Schedule Entry Type", pluck="name"):
		manifest.register("schedule_entry_types", se_type.lower().replace(" ", "_"), se_type)

	return manifest


def _ensure_warehouse(wh_name: str, parent_wh: str) -> str:
	full_name = f"{wh_name} - {CANONICAL_ABBR}"
	if frappe.db.exists("Warehouse", full_name):
		return full_name
	wh = frappe.get_doc({
		"doctype": "Warehouse",
		"warehouse_name": wh_name,
		"company": CANONICAL_COMPANY,
		"parent_warehouse": parent_wh if frappe.db.exists("Warehouse", parent_wh) else None,
		"is_group": 0,
	})
	wh.insert(ignore_permissions=True)
	return wh.name


def seed_inventory_and_parts(manifest: SimManifest, rng: random.Random) -> SimManifest:
	"""Phase 1.4: Seed warehouses, suppliers, synthetic EV parts, and initial Stock Reconciliation."""
	parent_wh = (
		frappe.db.get_value("Warehouse", {"company": CANONICAL_COMPANY, "is_group": 1}, "name")
		or f"All Warehouses - {CANONICAL_ABBR}"
	)
	for handle, wh_label in _WAREHOUSES:
		wh_doc_name = _ensure_warehouse(wh_label, parent_wh)
		manifest.register("warehouses", handle, wh_doc_name)

	main_wh = manifest.warehouses["main_stores"]
	manifest.warehouse = main_wh
	if frappe.db.exists("DocType", "Stock Settings"):
		stock_settings = frappe.get_single("Stock Settings")
		if stock_settings.meta.has_field("default_warehouse"):
			stock_settings.default_warehouse = main_wh
			stock_settings.save(ignore_permissions=True)

	frappe.defaults.set_global_default("default_warehouse", main_wh)
	legacy_stores = f"Stores - {CANONICAL_ABBR}"
	frappe.db.sql("UPDATE `tabDefaultValue` SET defvalue = %s WHERE defvalue = %s", (main_wh, legacy_stores))
	if frappe.db.exists("DocType", "Item Default"):
		frappe.db.sql(
			"UPDATE `tabItem Default` SET default_warehouse = %s WHERE default_warehouse = %s",
			(main_wh, legacy_stores),
		)
	frappe.clear_cache()

	if frappe.db.exists("Warehouse", legacy_stores) and not frappe.db.exists(
		"Stock Ledger Entry", {"warehouse": legacy_stores}
	):
		try:
			frappe.delete_doc("Warehouse", legacy_stores, force=True, ignore_permissions=True)
		except Exception:
			frappe.db.set_value("Warehouse", legacy_stores, "disabled", 0)

	for handle, sup_name, sup_group in _SUPPLIERS:
		sup = _ensure_doc(
			"Supplier",
			sup_name,
			{"supplier_name": sup_name, "supplier_group": sup_group, "supplier_type": "Company"},
		)
		manifest.register("suppliers", handle, sup.name)

	recon_rows: list[dict[str, Any]] = []
	for idx, spec in enumerate(_PART_SPECS):
		oem_part_no, revision = generate_oem_part_number(rng, prefix_7=1044300 + idx * 17)
		sell_price = compute_catalog_part_price(spec["cost"], spec["tesla_oem"])
		primary_supplier = manifest.suppliers[spec["supplier"]]
		supplier_list = [primary_supplier]
		for extra_handle in spec.get("extra_suppliers", ()):
			supplier_list.append(manifest.suppliers[extra_handle])

		if frappe.db.exists("Item", spec["item_code"]):
			item = frappe.get_doc("Item", spec["item_code"])
		else:
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": spec["item_code"],
				"item_name": spec["item_name"],
				"item_group": spec["item_group"],
				"stock_uom": spec["uom"],
				"is_stock_item": 1,
				"is_vehicle_part": 1,
				"part_source": spec["source"],
				"part_condition": spec["condition"],
				"oem_part_number": oem_part_no,
				"revision": revision,
				"warranty_period": spec["warranty"],
				"custom_frt": spec["frt"],
				"valuation_rate": spec["cost"],
				"standard_rate": sell_price,
				"supplier_items": [{"supplier": s} for s in supplier_list],
				"item_defaults": [
					{
						"company": CANONICAL_COMPANY,
						"default_warehouse": main_wh,
						"default_supplier": primary_supplier,
					}
				],
			})
			item.insert(ignore_permissions=True)

		manifest.register("items", spec["handle"], item.name)

		target_qty = float(spec["stock_qty"])
		current_qty = float(
			frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": main_wh}, "actual_qty") or 0.0
		)
		if abs(current_qty - target_qty) > 1e-6:
			recon_rows.append({
				"item_code": item.name,
				"warehouse": main_wh,
				"qty": target_qty,
				"valuation_rate": float(spec["cost"]),
			})

	if recon_rows:
		diff_account = (
			frappe.db.get_value(
				"Account", {"company": CANONICAL_COMPANY, "account_type": "Temporary", "is_group": 0}, "name"
			)
			or frappe.db.get_value(
				"Account", {"company": CANONICAL_COMPANY, "account_name": "Temporary Opening", "is_group": 0}, "name"
			)
		)
		if not diff_account:
			temp_parent = (
				frappe.db.get_value(
					"Account",
					{"company": CANONICAL_COMPANY, "account_name": "Temporary Accounts", "is_group": 1},
					"name",
				)
				or frappe.db.get_value(
					"Account",
					{"company": CANONICAL_COMPANY, "root_type": "Liability", "is_group": 1},
					"name",
				)
			)
			diff_account = _ensure_leaf_account("Temporary Opening", temp_parent, "Temporary")
		cost_center = frappe.db.get_value("Company", CANONICAL_COMPANY, "cost_center") or f"Main - {CANONICAL_ABBR}"
		sr = frappe.get_doc({
			"doctype": "Stock Reconciliation",
			"purpose": "Opening Stock",
			"company": CANONICAL_COMPANY,
			"posting_date": "2025-01-02",
			"posting_time": "08:00:00",
			"set_posting_time": 1,
			"expense_account": diff_account,
			"cost_center": cost_center,
			"items": recon_rows,
		})
		sr.insert(ignore_permissions=True)
		sr.submit()

	return manifest


def _ensure_canonical_package_tags(pkg_name: str, tags: tuple[str, ...]) -> None:
	"""Backfill missing Equipment Tags on canonical Service Packages (e.g. warm DB upgrades)."""
	if not tags or not frappe.db.exists("DocType", "Service Package Equipment"):
		return
	if frappe.db.exists("Service Package Equipment", {"parent": pkg_name, "parenttype": "Service Package"}):
		return
	pkg = frappe.get_doc("Service Package", pkg_name)
	for tag in tags:
		if frappe.db.exists("Equipment Tag", tag):
			pkg.append("equipment_tags", {"equipment_tag": tag})
	if pkg.equipment_tags:
		pkg.save(ignore_permissions=True)


def seed_service_packages_and_inspections(manifest: SimManifest) -> SimManifest:
	"""Phase 1.5: Seed nested Service Packages and Inspection Templates."""
	item_rates = {
		row.name: float(row.standard_rate or 0.0)
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", list(manifest.items.values())]},
			fields=["name", "standard_rate"],
		)
	}

	for spec in _PACKAGE_SPECS:
		pkg_name = spec["package_name"]
		spec_tags = tuple(spec.get("equipment_tags") or ())
		if frappe.db.exists("Service Package", pkg_name):
			_ensure_canonical_package_tags(pkg_name, spec_tags)
			pkg = frappe.get_doc("Service Package", pkg_name)
		else:
			child_items = []
			for row in spec["items"]:
				item_code = manifest.items[row["item_handle"]]
				child_items.append({
					"slot_number": row["slot_number"],
					"item_code": item_code,
					"qty": row["qty"],
					"side": row["side"],
					"is_default": row["is_default"],
					"rate": item_rates.get(item_code, 0.0),
				})
			pkg = frappe.get_doc({
				"doctype": "Service Package",
				"package_name": pkg_name,
				"applicable_model": spec["applicable_model"],
				"applicable_generation": spec.get("applicable_generation", ""),
				"correction_code": spec["correction_code"],
				"rh_correction_code": spec.get("rh_correction_code", ""),
				"standard_labor_hours": spec["standard_labor_hours"],
				"hourly_rate": spec["hourly_rate"],
				"service_rate": spec.get("service_rate", 0.0),
				"is_flat_rate": spec["is_flat_rate"],
				"equipment_tags": [{"equipment_tag": tag} for tag in spec_tags],
				"is_symmetrical": spec["is_symmetrical"],
				"items": child_items,
			})
			pkg.insert(ignore_permissions=True)
		manifest.register("packages", spec["handle"], pkg.name)

	if frappe.db.exists("Inspection Template", "Standard Vehicle Walkaround"):
		manifest.register("inspection_templates", "walkaround", "Standard Vehicle Walkaround")

	undercarriage_name = "Comprehensive EV Undercarriage & Brake Inspection"
	tmpl = _ensure_doc(
		"Inspection Template",
		undercarriage_name,
		{
			"template_name": undercarriage_name,
			"description": "10-point EV brake, suspension, tire, and high-voltage safety inspection.",
			"items": [{"inspection_description": desc} for desc in _UNDERCARRIAGE_INSPECTION_ITEMS],
		},
	)
	manifest.register("inspection_templates", "undercarriage_brake", tmpl.name)
	return manifest


def _ensure_customer_contact_and_address(spec: dict[str, Any], cust_name: str) -> None:
	existing_contact = frappe.db.get_value(
		"Dynamic Link",
		{"parenttype": "Contact", "link_doctype": "Customer", "link_name": cust_name},
		"parent",
	)
	if not existing_contact:
		parts = spec["customer_name"].split(" ", 1)
		first = parts[0]
		last = parts[1] if len(parts) > 1 else ""
		contact = frappe.get_doc({
			"doctype": "Contact",
			"first_name": first,
			"last_name": last,
			"is_primary_contact": 1,
			"email_ids": [{"email_id": spec["email"], "is_primary": 1}],
			"phone_nos": [{"phone": spec["phone"], "is_primary_phone": 1, "is_primary_mobile_no": 1}],
			"links": [{"link_doctype": "Customer", "link_name": cust_name}],
		})
		contact.insert(ignore_permissions=True)
		frappe.db.set_value("Customer", cust_name, "customer_primary_contact", contact.name, update_modified=False)

	existing_addr = frappe.db.get_value(
		"Dynamic Link",
		{"parenttype": "Address", "link_doctype": "Customer", "link_name": cust_name},
		"parent",
	)
	if not existing_addr:
		addr = frappe.get_doc({
			"doctype": "Address",
			"address_title": cust_name,
			"address_type": "Billing",
			"address_line1": spec["street"],
			"city": spec["city"],
			"state": spec["state"],
			"pincode": spec["postal_code"],
			"country": CANONICAL_COUNTRY,
			"is_primary_address": 1,
			"links": [{"link_doctype": "Customer", "link_name": cust_name}],
		})
		addr.insert(ignore_permissions=True)
		frappe.db.set_value("Customer", cust_name, "customer_primary_address", addr.name, update_modified=False)


def seed_customers_and_vehicles(manifest: SimManifest, rng: random.Random) -> SimManifest:
	"""Phase 1.6: Seed retail, multi-driver household, fleet customers, check-digit VINs, and deferred work."""
	for spec in _CUSTOMERS:
		cust = _ensure_doc(
			"Customer",
			spec["customer_name"],
			{
				"customer_name": spec["customer_name"],
				"customer_type": spec["customer_type"],
				"customer_group": spec["customer_group"],
				"territory": CANONICAL_COUNTRY,
				"default_currency": CANONICAL_CURRENCY,
			},
		)
		_ensure_customer_contact_and_address(spec, cust.name)
		manifest.register("customers", spec["handle"], cust.name)

	for idx, vspec in enumerate(_VEHICLES):
		seq_num = 210100 + idx * 1373
		vin = generate_synthetic_vin(
			rng,
			model_code=vspec["model_code"],
			year_code=vspec["year_code"],
			seq=seq_num,
		)
		primary_cust = manifest.customers[vspec["customer"]]

		if frappe.db.exists("Repair Vehicle", vin):
			veh = frappe.get_doc("Repair Vehicle", vin)
		else:
			customer_rows = [{"customer": primary_cust, "is_primary": 1, "relationship": "Owner"}]
			for co_handle, rel in vspec.get("co_owners", ()):
				customer_rows.append({
					"customer": manifest.customers[co_handle],
					"is_primary": 0,
					"relationship": rel,
				})
			deferred_rows = [
				{
					"job_title": d["job_title"],
					"decline_date": "2026-10-15",
					"estimated_amount": d["estimated_amount"],
					"status": "Open Recommendation",
					"safety_hazard": d["safety_hazard"],
					"decline_reason": d["decline_reason"],
				}
				for d in vspec.get("deferred", ())
			]
			veh = frappe.get_doc({
				"doctype": "Repair Vehicle",
				"vin": vin,
				"license_plate": vspec["plate"],
				"color": vspec["color"],
				"customer": primary_cust,
				"last_odometer": vspec["odometer"],
				"customers": customer_rows,
				"deferred_services": deferred_rows,
			})
			veh.insert(ignore_permissions=True)

		manifest.register("vehicles", vspec["handle"], veh.name)

	return manifest


def seed_catalog_manifest(
	seed: int = CATALOG_SEED,
	anchor_date: Optional[str] = None,
	commit: bool = True,
) -> SimManifest:
	"""Idempotently seed all Layer 1 canonical master records and return a SimManifest."""
	rng = random.Random(int(seed))
	resolved_anchor = str(anchor_date or frappe.utils.today())
	manifest = SimManifest(
		profile="clean_master",
		seed=int(seed),
		anchor_date=resolved_anchor,
	)

	seed_accounting_and_settings(manifest)
	seed_workforce_and_leave(manifest)
	seed_shop_floor(manifest)
	seed_inventory_and_parts(manifest, rng)
	seed_service_packages_and_inspections(manifest)
	seed_customers_and_vehicles(manifest, rng)

	manifest.metadata["package_equipment_tags"] = {k: list(v) for k, v in PACKAGE_EQUIPMENT_TAGS.items()}
	if commit:
		frappe.db.commit()
	return manifest


def seed_all(
	seed: int = CATALOG_SEED,
	anchor_date: Optional[str] = None,
	commit: bool = True,
) -> dict[str, Any]:
	"""Bench-executable entry point returning the JSON-serializable SimManifest dict."""
	return seed_catalog_manifest(seed=seed, anchor_date=anchor_date, commit=commit).to_dict()


def get_master_manifest(
	seed: int = CATALOG_SEED,
	anchor_date: Optional[str] = None,
) -> SimManifest:
	"""Reconstruct the Layer 1 SimManifest from an already-seeded DB (or seed in-transaction if missing)."""
	if not frappe.db.exists("Company", CANONICAL_COMPANY):
		return seed_catalog_manifest(seed=seed, anchor_date=anchor_date, commit=False)

	rng = random.Random(int(seed))
	resolved_anchor = str(anchor_date or frappe.utils.today())
	manifest = SimManifest(
		profile="clean_master",
		seed=int(seed),
		anchor_date=resolved_anchor,
		company=CANONICAL_COMPANY,
		company_abbr=CANONICAL_ABBR,
		currency=CANONICAL_CURRENCY,
		warehouse=CANONICAL_MAIN_WAREHOUSE,
		holiday_list="Lightspeed EV Holidays 2026",
	)

	manifest.price_lists = {"selling": "Standard Selling", "buying": "Standard Buying"}
	sales_tax = frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": CANONICAL_COMPANY, "is_default": 1, "disabled": 0},
		"name",
	)
	purchase_tax = frappe.db.get_value(
		"Purchase Taxes and Charges Template",
		{"company": CANONICAL_COMPANY, "is_default": 1, "disabled": 0},
		"name",
	)
	if sales_tax:
		manifest.tax_templates["sales_hst_13"] = sales_tax
	if purchase_tax:
		manifest.tax_templates["purchase_hst_13"] = purchase_tax

	for mop_name, _, _ in _MODES_OF_PAYMENT:
		if frappe.db.exists("Mode of Payment", mop_name):
			handle = mop_name.lower().replace(" / ", "_").replace(" ", "_").replace("-", "_")
			manifest.modes_of_payment[handle] = mop_name

	for persona in _PERSONAS:
		if frappe.db.exists("User", persona["email"]):
			manifest.users[persona["handle"]] = persona["email"]

	emp_map = {
		row.employee_name: row.name
		for row in frappe.get_all("Employee", fields=["name", "employee_name"])
	}
	for spec in _EMPLOYEES:
		if spec["employee_name"] in emp_map:
			manifest.techs[spec["handle"]] = emp_map[spec["employee_name"]]

	for lt_handle, lt_name in (("privilege", "Privilege Leave"), ("sick", "Sick Leave"), ("casual", "Casual Leave")):
		if frappe.db.exists("Leave Type", lt_name):
			manifest.leave_types[lt_handle] = lt_name

	for handle, tag_name, _ in _EQUIPMENT_TAGS:
		if frappe.db.exists("Equipment Tag", tag_name):
			manifest.equipment_tags[handle] = tag_name

	for spec in _SERVICE_BAYS:
		if frappe.db.exists("Service Bay", spec["bay_name"]):
			manifest.bays[spec["handle"]] = spec["bay_name"]

	for se_type in frappe.get_all("Schedule Entry Type", pluck="name"):
		manifest.schedule_entry_types[se_type.lower().replace(" ", "_")] = se_type

	for handle, wh_label in _WAREHOUSES:
		full_name = f"{wh_label} - {CANONICAL_ABBR}"
		if frappe.db.exists("Warehouse", full_name):
			manifest.warehouses[handle] = full_name

	for handle, sup_name, _ in _SUPPLIERS:
		if frappe.db.exists("Supplier", sup_name):
			manifest.suppliers[handle] = sup_name

	for spec in _PART_SPECS:
		if frappe.db.exists("Item", spec["item_code"]):
			manifest.items[spec["handle"]] = spec["item_code"]

	for spec in _PACKAGE_SPECS:
		if frappe.db.exists("Service Package", spec["package_name"]):
			_ensure_canonical_package_tags(spec["package_name"], tuple(spec.get("equipment_tags") or ()))
			manifest.packages[spec["handle"]] = spec["package_name"]

	if frappe.db.exists("Inspection Template", "Standard Vehicle Walkaround"):
		manifest.inspection_templates["walkaround"] = "Standard Vehicle Walkaround"
	undercarriage_name = "Comprehensive EV Undercarriage & Brake Inspection"
	if frappe.db.exists("Inspection Template", undercarriage_name):
		manifest.inspection_templates["undercarriage_brake"] = undercarriage_name

	for spec in _CUSTOMERS:
		if frappe.db.exists("Customer", spec["customer_name"]):
			manifest.customers[spec["handle"]] = spec["customer_name"]

	for idx, vspec in enumerate(_VEHICLES):
		vin = generate_synthetic_vin(
			rng,
			model_code=vspec["model_code"],
			year_code=vspec["year_code"],
			seq=210100 + idx * 1373,
		)
		if frappe.db.exists("Repair Vehicle", vin):
			manifest.vehicles[vspec["handle"]] = vin

	manifest.metadata["package_equipment_tags"] = {k: list(v) for k, v in PACKAGE_EQUIPMENT_TAGS.items()}
	return manifest
