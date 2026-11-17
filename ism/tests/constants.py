# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

TEST_PREFIX = "_TEST_ISM_"

CANONICAL_COMPANY = "Lightspeed EV"
CANONICAL_COMPANY_ABBR = "LEV"
CANONICAL_CURRENCY = "CAD"
CANONICAL_WAREHOUSE = "Main Shop Stores - LEV"

TEST_VINS = ["5YJ3E1EBXNF123456", "7SAYGDEE5PF123450"]
TEST_VIN_PREFIX = "5YJ3E1EB"

STANDARD_SCHEDULE_ENTRY_TYPES = frozenset({
	"Diagnostic",
	"Repair",
	"Maintenance",
	"Meeting",
	"Internal Service",
})

CANONICAL_EQUIPMENT_TAGS = frozenset({
	"Lift",
	"Alignment Rack",
	"EV High Voltage",
	"HV Battery Station",
	"ADAS Calibration Target",
	"Tire Mount & Road Force Balancer",
})

CANONICAL_SERVICE_BAYS = frozenset({
	"Bay 1 - Main Lift",
	"Bay 2 - Alignment & Suspension",
	"Bay 3 - HV & Diagnostics",
	"Bay 4 - Tire & Quick Service",
	"Bay 5 - Offline",
})

TEST_CUSTOM_DOCPERM_ROLES = frozenset({"MCP Desk Tester"})
