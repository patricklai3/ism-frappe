# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Curated Ontario EV / Tesla independent repair shop starter presets for ISM Setup Wizard."""

from __future__ import annotations

from typing import Any

CANADIAN_PAYMENT_MODES: list[dict[str, Any]] = [
	{
		"mode_of_payment": "Cash",
		"type": "Cash",
		"enabled": 1,
	},
	{
		"mode_of_payment": "Interac Debit",
		"type": "Bank",
		"enabled": 1,
	},
	{
		"mode_of_payment": "Interac e-Transfer",
		"type": "Bank",
		"enabled": 1,
	},
	{
		"mode_of_payment": "Visa / Mastercard",
		"type": "Bank",
		"enabled": 1,
	},
	{
		"mode_of_payment": "Square",
		"type": "Bank",
		"enabled": 1,
	},
	{
		"mode_of_payment": "Cheque",
		"type": "Bank",
		"enabled": 1,
	},
]

EV_EQUIPMENT_TAGS: list[dict[str, str]] = [
	{
		"tag_name": "Lift",
		"description": "Standard automotive 2-post or 4-post lift with EV jack pad adapters.",
	},
	{
		"tag_name": "EV High Voltage",
		"description": "High-voltage isolation PPE, insulated tooling, and HV battery service equipment.",
	},
	{
		"tag_name": "Alignment Rack",
		"description": "3D optical four-wheel alignment rack with EV suspension calibration support.",
	},
	{
		"tag_name": "Battery Pack Lift Table",
		"description": "Heavy-duty hydraulic scissor lift table rated for EV traction battery pack removal.",
	},
	{
		"tag_name": "Tire Changer & Balancer",
		"description": "Touchless alloy wheel tire mounting machine and road-force wheel balancer.",
	},
	{
		"tag_name": "A/C Recovery (R-1234yf)",
		"description": "EV-safe non-conductive POE oil A/C recovery, vacuum, and recharge machine for R-1234yf.",
	},
	{
		"tag_name": "ADAS Calibration",
		"description": "Camera, radar, and steering-angle sensor calibration targets and diagnostic bay.",
	},
	{
		"tag_name": "Tesla Toolbox Diagnostics",
		"description": "Tesla Toolbox 3 laptop station with Ethernet/CAN diagnostic harness.",
	},
]

STARTER_STORAGE_BINS: list[dict[str, Any]] = [
	{
		"bin_code": "RECV-01",
		"bin_type": "Receiving",
		"zone": "Receiving",
		"aisle": "R1",
		"rack": "01",
		"shelf": "A",
		"is_active": 1,
		"description": "Inbound parts receiving, QC inspection, and staging bench.",
	},
	{
		"bin_code": "A1-R1-S1",
		"bin_type": "Standard",
		"zone": "Parts Room",
		"aisle": "A1",
		"rack": "R1",
		"shelf": "S1",
		"is_active": 1,
		"description": "Front and rear suspension control arms, links, knuckles, and bushings.",
	},
	{
		"bin_code": "A1-R1-S2",
		"bin_type": "Standard",
		"zone": "Parts Room",
		"aisle": "A1",
		"rack": "R1",
		"shelf": "S2",
		"is_active": 1,
		"description": "Brake pads, coated rotors, caliper hardware, and wheel hub bearings.",
	},
	{
		"bin_code": "A2-R1-S1",
		"bin_type": "Standard",
		"zone": "Parts Room",
		"aisle": "A2",
		"rack": "R1",
		"shelf": "S1",
		"is_active": 1,
		"description": "HEPA and cabin air filters, wiper blades, clips, and maintenance consumables.",
	},
	{
		"bin_code": "A2-R2-S1",
		"bin_type": "Standard",
		"zone": "Parts Room",
		"aisle": "A2",
		"rack": "R2",
		"shelf": "S1",
		"is_active": 1,
		"description": "Thermal management valves, supermanifold seals, desiccant bags, and pumps.",
	},
	{
		"bin_code": "HV-01",
		"bin_type": "Hazmat",
		"zone": "HV Cage",
		"aisle": "H1",
		"rack": "R1",
		"shelf": "S1",
		"is_active": 1,
		"description": "High-voltage pyrofuses, contactors, 12V/16V LV batteries, and EV coolants.",
	},
	{
		"bin_code": "BULK-01",
		"bin_type": "Bulk",
		"zone": "Tire Bay",
		"aisle": "B1",
		"rack": "R1",
		"shelf": "G",
		"is_active": 1,
		"description": "EV winter/summer tires, TPMS sensors, and aero wheel assemblies.",
	},
	{
		"bin_code": "OVF-01",
		"bin_type": "Overflow",
		"zone": "Parts Room",
		"aisle": "O1",
		"rack": "R1",
		"shelf": "S1",
		"is_active": 1,
		"description": "Customer special-order parts staged by Repair Order.",
	},
]

EV_UNDERCARRIAGE_INSPECTION: dict[str, Any] = {
	"template_name": "EV & Tesla Undercarriage & Safety Inspection",
	"is_active": 1,
	"description": (
		"Comprehensive Ontario EV & Tesla undercarriage, suspension, regenerative brake, "
		"and high-voltage safety inspection."
	),
	"items": [
		{
			"inspection_description": "HV Battery Pack Skid Plate & Jack Pad Points",
			"category": "High Voltage & Chassis",
			"photo_required": 1,
			"default_notes": "Inspect for impact dents, punctured coolant rails, and jack pad integrity.",
		},
		{
			"inspection_description": "Front & Rear Aero Shields / Undertray Fasteners",
			"category": "Undercarriage",
			"photo_required": 0,
			"default_notes": "Check for winter salt damage, sagging fibre shields, or missing bolts.",
		},
		{
			"inspection_description": "Front Upper & Lower Control Arms / Ball Joints",
			"category": "Suspension & Steering",
			"photo_required": 1,
			"default_notes": "Check upper control arm boots for squeaks/play and compliance bushing tears.",
		},
		{
			"inspection_description": "Rear Knuckle, Camber & Toe Link Bushings",
			"category": "Suspension & Steering",
			"photo_required": 0,
			"default_notes": "Inspect rear knuckle bushings and eccentric alignment cams for seizure.",
		},
		{
			"inspection_description": "Brake Pad Thickness & Rotor Corrosion (Regen Check)",
			"category": "Brakes",
			"photo_required": 1,
			"default_notes": "Measure pad mm, inspect inner rotor faces for Ontario road-salt rust lip, check slider pins.",
		},
		{
			"inspection_description": "Brake Fluid Moisture & Boiling Point Test",
			"category": "Brakes",
			"photo_required": 0,
			"default_notes": "Test moisture percentage (recommend flush if >= 3% or overdue 2-year interval).",
		},
		{
			"inspection_description": "Tire Tread Depth, Inner Shoulder Wear & Pressure (FL/FR/RL/RR)",
			"category": "Tires & Wheels",
			"photo_required": 1,
			"default_notes": "Record 32nds tread depth across inner/centre/outer ribs and verify EV load rating.",
		},
		{
			"inspection_description": "Half-Shafts, CV Boots & Drive Unit Seals",
			"category": "Drivetrain",
			"photo_required": 0,
			"default_notes": "Check for grease leaks, torn boots, or drive unit oil seepage.",
		},
		{
			"inspection_description": "Front Radiator / Condenser Louvers & Coolant Level",
			"category": "Thermal Management",
			"photo_required": 0,
			"default_notes": "Inspect active aero shutters and condenser fins for road debris or coolant residue.",
		},
		{
			"inspection_description": "12V / 16V Low-Voltage Battery & Service Mode Alerts",
			"category": "Electrical",
			"photo_required": 1,
			"default_notes": "Capture Tesla Service Mode active alerts and verify low-voltage battery health.",
		},
	],
}

STARTER_SUPPLIERS: list[dict[str, Any]] = [
	{
		"supplier_name": "Tesla Parts Canada",
		"supplier_group": "All Supplier Groups",
		"supplier_type": "Company",
		"country": "Canada",
		"default_currency": "CAD",
	},
	{
		"supplier_name": "Worldpac Canada",
		"supplier_group": "All Supplier Groups",
		"supplier_type": "Company",
		"country": "Canada",
		"default_currency": "CAD",
	},
	{
		"supplier_name": "NAPA Auto Parts Ontario",
		"supplier_group": "All Supplier Groups",
		"supplier_type": "Company",
		"country": "Canada",
		"default_currency": "CAD",
	},
	{
		"supplier_name": "RockAuto Aftermarket EV",
		"supplier_group": "All Supplier Groups",
		"supplier_type": "Company",
		"country": "United States",
		"default_currency": "CAD",
	},
	{
		"supplier_name": "Ontario Tire & Wheel Wholesale",
		"supplier_group": "All Supplier Groups",
		"supplier_type": "Company",
		"country": "Canada",
		"default_currency": "CAD",
	},
	{
		"supplier_name": "Wurth Canada (Shop Supplies)",
		"supplier_group": "All Supplier Groups",
		"supplier_type": "Company",
		"country": "Canada",
		"default_currency": "CAD",
	},
]

STARTER_SERVICE_PACKAGES: list[dict[str, Any]] = [
	{
		"package_name": "Annual EV Brake Service (Clean & Lubricate All 4 Calipers)",
		"applicable_model": "All Models",
		"standard_labor_hours": 1.2,
		"hourly_rate": 150.0,
		"service_rate": 180.0,
		"is_flat_rate": 1,
		"allow_self_serve_booking": 1,
		"self_serve_sort_order": 10,
		"customer_title": "Annual EV Brake Cleaning & Lubrication",
		"customer_summary": (
			"Essential Ontario winter salt maintenance: disassemble, clean, and lubricate "
			"all four brake calipers, slider pins, and pad contact points."
		),
		"customer_price_display_mode": "Exact Price",
		"customer_estimate_min": 180.0,
		"customer_estimate_max": 180.0,
		"self_serve_duration_minutes": 75,
		"equipment_tags": [{"equipment_tag": "Lift"}],
	},
	{
		"package_name": "Seasonal Tire Swap (On Rims) & Torque Check",
		"applicable_model": "All Models",
		"standard_labor_hours": 0.5,
		"hourly_rate": 150.0,
		"service_rate": 75.0,
		"is_flat_rate": 1,
		"allow_self_serve_booking": 1,
		"self_serve_sort_order": 20,
		"customer_title": "Seasonal Tire Changeover (On Rims)",
		"customer_summary": (
			"Swap mounted seasonal wheel assemblies using EV jack pucks, set pressures, "
			"reset TPMS, and torque lugs to factory spec."
		),
		"customer_price_display_mode": "Exact Price",
		"customer_estimate_min": 75.0,
		"customer_estimate_max": 75.0,
		"self_serve_duration_minutes": 30,
		"equipment_tags": [{"equipment_tag": "Lift"}],
	},
	{
		"package_name": "Cabin Air Filter Replacement & Evaporator Clean",
		"applicable_model": "Model 3",
		"standard_labor_hours": 0.5,
		"hourly_rate": 150.0,
		"service_rate": 85.0,
		"is_flat_rate": 1,
		"allow_self_serve_booking": 1,
		"self_serve_sort_order": 30,
		"customer_title": "Cabin Air Filter Service (Model 3 / Y)",
		"customer_summary": (
			"Replace dual cabin air filters and inspect HVAC intake for moisture or odour."
		),
		"customer_price_display_mode": "Starting At",
		"customer_estimate_min": 85.0,
		"customer_estimate_max": 145.0,
		"self_serve_duration_minutes": 30,
		"equipment_tags": [],
	},
	{
		"package_name": "Four-Wheel Precision EV Alignment",
		"applicable_model": "All Models",
		"standard_labor_hours": 1.2,
		"hourly_rate": 150.0,
		"service_rate": 195.0,
		"is_flat_rate": 1,
		"allow_self_serve_booking": 1,
		"self_serve_sort_order": 40,
		"customer_title": "Four-Wheel EV Alignment",
		"customer_summary": (
			"Precision 3D four-wheel alignment with steering angle sensor verification "
			"to prevent premature inner-shoulder EV tire wear."
		),
		"customer_price_display_mode": "Starting At",
		"customer_estimate_min": 195.0,
		"customer_estimate_max": 245.0,
		"self_serve_duration_minutes": 75,
		"equipment_tags": [{"equipment_tag": "Alignment Rack"}],
	},
	{
		"package_name": "Comprehensive EV & Tesla Pre-Purchase / Safety Inspection",
		"applicable_model": "All Models",
		"standard_labor_hours": 1.0,
		"hourly_rate": 150.0,
		"service_rate": 165.0,
		"is_flat_rate": 1,
		"allow_self_serve_booking": 1,
		"self_serve_sort_order": 50,
		"customer_title": "Comprehensive EV Safety & Battery Health Inspection",
		"customer_summary": (
			"Full undercarriage lift inspection, suspension play check, brake wear measurement, "
			"and Tesla Service Mode alert & battery health review."
		),
		"customer_price_display_mode": "Exact Price",
		"customer_estimate_min": 165.0,
		"customer_estimate_max": 165.0,
		"self_serve_duration_minutes": 60,
		"equipment_tags": [
			{"equipment_tag": "Lift"},
			{"equipment_tag": "Tesla Toolbox Diagnostics"},
		],
	},
	{
		"package_name": "EV High-Voltage & Thermal System Diagnostic",
		"applicable_model": "All Models",
		"standard_labor_hours": 1.0,
		"hourly_rate": 165.0,
		"service_rate": 165.0,
		"is_flat_rate": 0,
		"allow_self_serve_booking": 1,
		"self_serve_sort_order": 60,
		"customer_title": "EV Fault / Warning Light Diagnostic",
		"customer_summary": (
			"Initial 1-hour diagnostic evaluation for HV alerts, heat pump / thermal faults, "
			"charging issues, or suspension noises."
		),
		"customer_price_display_mode": "Starting At",
		"customer_estimate_min": 165.0,
		"customer_estimate_max": 330.0,
		"self_serve_duration_minutes": 60,
		"equipment_tags": [
			{"equipment_tag": "EV High Voltage"},
			{"equipment_tag": "Tesla Toolbox Diagnostics"},
		],
	},
]

STAFF_ROLE_BUNDLES: dict[str, dict[str, Any]] = {
	"Shop Manager": {
		"label": "Shop Manager",
		"description": (
			"Full shop oversight: ISM Setup Wizard, ISM Settings, pricing, billing, "
			"inventory management, scheduling, and financial reporting."
		),
		"desk_access": 1,
		"roles": [
			"ISM Manager",
			"Accounts Manager",
			"Stock Manager",
			"Purchase Manager",
			"Sales Manager",
			"Maintenance Manager",
		],
	},
	"Service Advisor": {
		"label": "Service Advisor",
		"description": (
			"Front-desk customer intake, quoting, Repair Order lifecycle, scheduling, "
			"customer portal/SMS communications, and checkout payments."
		),
		"desk_access": 1,
		"roles": [
			"ISM Service Advisor",
			"Sales User",
			"Accounts User",
			"Stock User",
			"Maintenance User",
		],
	},
	"Parts Specialist": {
		"label": "Parts Specialist",
		"description": (
			"Parts sourcing, Purchase Orders, Purchase Receipts, Storage Bin management, "
			"stock transfers, and supplier catalog imports."
		),
		"desk_access": 1,
		"roles": [
			"ISM Service Advisor",
			"Stock Manager",
			"Stock User",
			"Purchase User",
			"Item Manager",
		],
	},
	"Floor Technician": {
		"label": "Floor Technician",
		"description": (
			"Bay execution via /tech mobile portal: assigned Repair Orders, time logs, "
			"check-in inspections, and Shop Board captures. No pricing/setup admin access."
		),
		"desk_access": 0,
		"roles": [
			"ISM Technician",
			"Maintenance User",
		],
	},
	"Agent OS Service User": {
		"label": "Agent OS Service User",
		"description": (
			"Dedicated non-human API service account for ISM Agent OS CLI & MCP automation "
			"with token authentication and shop operational + setup permissions."
		),
		"desk_access": 1,
		"roles": [
			"ISM Manager",
			"ISM Service Advisor",
			"Stock Manager",
			"Purchase User",
			"Sales User",
			"Accounts User",
			"Maintenance User",
		],
	},
}
