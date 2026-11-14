# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re
from typing import Any, Optional
import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt
from ism.utils.tesla_manual import get_tesla_manual_url_for_vehicle

_F = "Tesla Fremont, CA"
_SH = "Giga Shanghai, China"
_LFP = "LFP — Lithium Iron Phosphate Battery"
_ELI = "Electric — Lithium-Ion"

_MB = "Manual belts + Front Airbags"
_OD = ", Occupant Detection"
_SI = ", Side Inflatable Restraints"
_KA = ", Knee Airbags"
_AH = " + Active Hood"

_R5 = _MB + _OD + _SI + _KA + " — 5-seat"
_R5s = _MB + _SI + " — 5-seat"
_R7 = _MB + _OD + _SI + _KA + " — 7-seat (2+3+2)"
_R6 = _MB + _OD + _SI + _KA + " — 6-seat (2+2+2)"
_R5c = _MB + _OD + _SI + " — 5-seat (2+3)"
_R5d = _MB + _OD + _SI + _KA + " — 5-seat (2+3)"
_RAH = _MB + _SI + _AH + " — 5-seat"

_SB12 = {
	"A": "Hatchback 5-Door LHD — RWD",
	"B": "Hatchback 5-Door LHD — AWD",
	"C": "Hatchback 5-Door RHD — RWD",
	"D": "Hatchback 5-Door RHD — AWD",
}
_SF12 = {
	"A": "10 kW Charger",
	"B": "20 kW Charger",
	"C": "10 kW Charger + DC Fast Charge",
	"D": "20 kW Charger + DC Fast Charge",
}
_SD12 = {
	"C": "Base AC Motor — Tier 2 Battery (31–40 kWh)",
	"G": "Base AC Motor — Tier 4 Battery (51–60 kWh)",
	"N": "Base AC Motor — Tier 7 Battery (81–90 kWh)",
	"P": "Performance AC Motor — Tier 7 Battery (81–90 kWh)",
}
_SD1518 = {
	"1": "Single Motor — Large Base",
	"2": "Dual Motor — Small Base + Small Base",
	"3": "Single Motor — Large Performance",
	"4": "Dual Motor — Small Base + Large Performance",
}
_SD1920 = {
	"1": "Single Motor — Standard",
	"2": "Dual Motor — Standard",
	"3": "Single Motor — Performance",
	"4": "Dual Motor — Performance",
}
_SR12 = {
	"1": "USA manual belts + front airbags, front/rear side airbags, knee airbags — 5-seat",
	"2": "EU manual belts + front airbags, front/rear side airbags, knee airbags — 5-seat",
}
_SR1517 = {"1": _R5, "3": _R5s, "7": _RAH}
_SR1820 = {"1": _R5, "3": _R5s, "7": _R5s}
_SLE = {"E": "Electric"}

MODEL = {
	"S": "Model S",
	"3": "Model 3",
	"X": "Model X",
	"Y": "Model Y",
	"C": "Cybertruck",
	"R": "Roadster",
	"A": "Cybercab",
	"T": "Semi",
}

ALL_WMI = {"5YJ", "7SA", "LRW", "XP7", "SFZ", "5XJ", "3MW", "7G2"}

WMI = {
	"3": {
		"5YJ": _F + " / Austin, TX",
		"5XJ": _F + " (Secondary)",
		"LRW": _SH,
		"3MW": "Monterrey, Mexico (Future)",
	},
	"S": {"5YJ": _F, "5XJ": _F + " (Secondary)"},
	"X": {"7SA": _F + " / Austin, TX"},
	"Y": {
		"5YJ": "Tesla Fremont — through Model Year 2021",
		"7SA": "Tesla Fremont / Giga Texas — from Model Year 2022",
		"LRW": _SH,
		"XP7": "Giga Berlin, Germany",
		"3MW": "Monterrey, Mexico (Future)",
	},
	"C": {"5YJ": _F, "7SA": "Tesla Austin, TX", "7G2": "Tesla Austin, TX / Giga Nevada"},
	"R": {"SFZ": "Lotus Factory — Norfolk, UK"},
	"A": {"5YJ": _F},
	"T": {"7G2": "Tesla Austin, TX / Giga Nevada"},
}

BODY = {
	"3": {"E": "Sedan 4-Door — Left-Hand Drive", "F": "Sedan 4-Door — Right-Hand Drive"},
	"S": {"A": "Hatchback 5-Door — Left-Hand Drive", "B": "Hatchback 5-Door — Right-Hand Drive"},
	"X": {"C": "Class E MPV 5-Door — Left-Hand Drive", "D": "Class E MPV 5-Door — Right-Hand Drive"},
	"Y": {"G": "Class D MPV 5-Door — Left-Hand Drive", "H": "Class D MPV 5-Door — Right-Hand Drive"},
	"C": {"E": "Truck / Pickup — Left-Hand Drive", "J": "Crew Cab — Left-Hand Drive"},
	"A": {"J": "Robotaxi Body"},
	"T": {
		"K": "Day Cab — No Sleeper",
		"L": "Sleeper Cab — With Sleeper Berth",
		"1": "Day Cab — Standard",
	},
}

FUEL = {
	"3": {"E": "Electric — Li-ion NMC/NCA Battery", "F": _LFP, "H": _LFP},
	"S": {"E": "Electric"},
	"X": {"E": "Electric"},
	"Y": {"E": "Electric — Li-ion NMC/NCA Battery", "F": _LFP, "H": _LFP},
	"C": {
		"S": "Standard Range (~250 mi)",
		"R": "Long Range (~340 mi)",
		"E": _ELI,
		"0": "Battery — Base",
	},
	"R": {"B": "Battery — Lithium-Ion"},
	"A": {"E": _ELI},
	"T": {
		"S": "Standard Range (~300 mi)",
		"R": "Long Range (~500 mi)",
		"E": _ELI,
		"0": "Battery — Base",
	},
}

DRIVE = {
	"3": {
		"A": "Single Motor — Standard (3DU 800A)",
		"B": "Dual Motor — Standard",
		"C": "Dual Motor — Performance",
		"J": "Single Motor — Hairpin Winding",
		"K": "Dual Motor — Hairpin Winding",
		"L": "Dual Motor — Hairpin Winding Performance",
		"R": "Single Motor — Standard (3DU 600A)",
		"S": "Single Motor — Standard (DUB 600A)",
		"T": "Dual Motor — Performance (Highland)",
	},
	"S": {"5": "Dual Motor", "6": "Triple Motor — Plaid"},
	"X": {"5": "Dual Motor", "6": "Triple Motor — Plaid"},
	"Y": {
		"D": "Single Motor — Standard",
		"E": "Dual Motor — Standard",
		"F": "Dual Motor — Performance (3DU 800A)",
		"J": "Single Motor — Hairpin Winding",
		"K": "Dual Motor — Hairpin Winding",
		"L": "Dual Motor — Hairpin Winding Performance",
		"R": "Single Motor — Standard (3DU 600A)",
		"S": "Single Motor — Standard (DUB 600A)",
	},
	"C": {
		"D": "Dual Motor — AWD Standard",
		"E": "Triple Motor — Cyberbeast Performance",
	},
	"R": {"4": "Single Motor — AC Induction"},
	"A": {"U": "Robotaxi Drive Unit"},
	"T": {"B": "Dual Drive Rear Axle — Air Brakes", "0": "Drive Unit — Standard"},
}

YEAR = {
	"8": "2008", "9": "2009", "A": "2010", "B": "2011", "C": "2012",
	"D": "2013", "E": "2014", "F": "2015", "G": "2016", "H": "2017",
	"J": "2018", "K": "2019", "L": "2020", "M": "2021", "N": "2022",
	"P": "2023", "R": "2024", "S": "2025", "T": "2026", "V": "2027",
}

PLANT = {
	"F": "Tesla Factory — Fremont, CA",
	"A": "Giga Texas — Austin, TX",
	"C": "Giga Shanghai — China",
	"B": "Giga Berlin — Germany",
	"G": "Giga Berlin — Brandenburg, Germany",
	"N": "Giga Nevada — Reno, NV",
	"1": "Lotus Factory — Norfolk, UK",
	"3": "Palo Alto — R&D / Prototypes",
	"P": "Palo Alto — R&D / Prototypes",
}

S_LEGACY_YEARS = {"C", "D", "E", "F", "G", "H", "J", "K", "L"}
S_LEGACY = {
	"body": {"C": _SB12, "D": _SB12},
	"fuel": {
		"C": _SF12, "D": _SF12,
		"E": {"H": "Li-ion — High Capacity", "S": "Li-ion — Standard Capacity"},
		"F": {"S": "Li-ion — Standard Capacity", "H": "Li-ion — High Capacity", "V": "Li-ion — Ultra High Capacity", "E": "Electric"},
		"G": _SLE, "H": _SLE, "J": _SLE, "K": _SLE, "L": _SLE,
	},
	"drive": {
		"C": _SD12, "D": _SD12,
		"E": {"1": "Single Motor — 3-Phase AC Induction", "2": "Dual Motor — 3-Phase AC Induction"},
		"F": _SD1518, "G": _SD1518, "H": _SD1518, "J": _SD1518,
		"K": _SD1920, "L": _SD1920,
	},
	"typedSeq": {"C", "D", "E"},
}


def compute_vin_check_digit(vin_string: str) -> str:
	"""Calculate 9th check digit using standard ISO 3779 / NHTSA algorithm."""
	trans = {
		"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
		"J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9, "S": 2,
		"T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
	}
	weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
	total = 0
	for i in range(17):
		char = vin_string[i]
		if not char.isdigit() and char not in trans:
			return "?"
		val = int(char) if char.isdigit() else trans[char]
		total += val * weights[i]
	remainder = total % 11
	return "X" if remainder == 10 else str(remainder)


def get_hardware_generation(model_code: str, seq: int, plant_code: str, year_code: str) -> dict | None:
	year = int(YEAR.get(year_code, 0))
	if model_code in ("C", "A", "T"):
		return {"t": "HW4"}
	if model_code == "Y":
		if year >= 2024:
			return {"t": "HW4"}
		if plant_code == "F":
			if seq >= 800000:
				return {"t": "Likely HW4"}
			if seq >= 790000:
				return {"t": "Possibly HW4"}
		elif plant_code == "A":
			if seq >= 131200:
				return {"t": "Likely HW4"}
			if seq >= 127000:
				return {"t": "Possibly HW4"}
		if year == 2023:
			return {"t": "Possibly HW4"}
		return {"t": "Shipped with HW3"}

	if model_code == "X":
		if seq >= 385000:
			return {"t": "Likely HW4"}
		if seq >= 370000:
			return {"t": "Possibly HW4"}
		if 2020 <= year <= 2022:
			return {"t": "Shipped with HW3"}
		if year == 2019:
			return {"t": "HW2.5 or HW3"}
		if year == 2018:
			return {"t": "Shipped with HW2.5"}
		if year == 2017:
			return {"t": "HW2.0 or HW2.5"}
		if year == 2016:
			return {"t": "HW1 or HW2.0"}
		if year == 2015:
			return {"t": "Shipped with HW1"}
		return None

	if model_code == "S":
		if year_code not in S_LEGACY_YEARS:
			if seq >= 502000:
				return {"t": "Likely HW4"}
			if seq >= 501000:
				return {"t": "Possibly HW4"}
			return {"t": "Shipped with HW3"}
		if year == 2020:
			return {"t": "Shipped with HW3"}
		if year == 2019:
			return {"t": "HW2.5 or HW3"}
		if year == 2018:
			return {"t": "Shipped with HW2.5"}
		if year == 2017:
			return {"t": "HW2.0 or HW2.5"}
		if year == 2016:
			return {"t": "HW1 or HW2.0"}
		if year == 2015:
			return {"t": "Shipped with HW1"}
		if year == 2014:
			return {"t": "No AP or HW1"}
		if year <= 2013:
			return {"t": "No autopilot hardware"}
		return None

	if model_code == "3":
		if year >= 2024:
			return {"t": "HW4"}
		if year == 2023:
			return {"t": "Possibly HW4"}
		if 2020 <= year <= 2022:
			return {"t": "Shipped with HW3"}
		if year == 2019:
			return {"t": "HW2.5 or HW3"}
		if year in (2017, 2018):
			return {"t": "Shipped with HW2.5"}
		return None

	return None


def get_derived_specs(model_code: str, drive_code: str, year_code: str, is_legacy_s: bool) -> dict:
	year = int(YEAR.get(year_code, 0))
	drivetrain = None
	if model_code == "R":
		drivetrain = "RWD"
	elif model_code in ("T", "A"):
		drivetrain = None
	elif is_legacy_s:
		drivetrain = "RWD" if year <= 2013 else ("AWD" if drive_code in ("2", "4") else "RWD")
	else:
		awd_map = {
			"S": ["5", "6"],
			"3": ["B", "C", "K", "L", "T"],
			"X": ["5", "6"],
			"Y": ["E", "F", "K", "L"],
			"C": ["D", "E"],
		}
		if drive_code:
			drivetrain = "AWD" if drive_code in awd_map.get(model_code, []) else "RWD"

	trim = None
	if is_legacy_s:
		if drive_code in ("C", "G", "N", "P"):
			trim = {"C": "40 kWh", "G": "60 kWh", "N": "85 kWh", "P": "P85 Performance"}.get(drive_code)
		elif year <= 2014:
			trim = "Dual Motor" if drive_code == "2" else "Standard"
		elif year <= 2018:
			trim = {"1": "Standard", "2": "Dual Motor", "3": "Performance", "4": "Dual Motor Performance"}.get(drive_code)
		else:
			trim = {"1": "Standard Range", "2": "Long Range", "3": "Performance", "4": "Long Range Performance"}.get(drive_code)
	else:
		trims = {
			"S": {"5": "Long Range", "6": "Plaid"},
			"3": {
				"A": "Standard Range", "B": "Long Range", "C": "Performance",
				"J": "Standard Range", "K": "Long Range", "L": "Performance",
				"R": "Standard Range", "S": "Standard Range", "T": "Performance",
			},
			"X": {"5": "Long Range", "6": "Plaid"},
			"Y": {
				"D": "Standard Range", "E": "Long Range", "F": "Performance",
				"J": "Standard Range", "K": "Long Range", "L": "Performance",
				"R": "Standard Range", "S": "Standard Range",
			},
			"C": {"D": "Dual Motor", "E": "Cyberbeast"},
			"R": {"4": "Sport"},
			"T": {"B": "Semi"},
		}.get(model_code, {})
		trim = trims.get(drive_code)

	return {"drivetrain": drivetrain, "trim": trim}


def normalize_vin_string(vin: Optional[str]) -> str:
	"""Strip non-VIN characters and uppercase according to ISO 3779."""
	if not vin:
		return ""
	return re.sub(r"[^A-HJ-NPR-Z0-9]", "", str(vin).upper())


def decode_tesla_vin(vin: Optional[str]) -> dict[str, Any]:
	"""Decode a Tesla VIN strictly offline using bundled WMI/spec/check-digit tables."""
	normalized = normalize_vin_string(vin)
	base = {
		"vin": normalized,
		"is_valid_length": len(normalized) == 17,
		"is_tesla_wmi": False,
		"is_valid_vin": 0,
		"wmi": normalized[:3] if len(normalized) >= 3 else "",
		"manufacturer": None,
		"model": None,
		"model_year": None,
		"body_type": None,
		"trim": None,
		"drivetrain": None,
		"battery_type": None,
		"drive_unit": None,
		"assembly_plant": None,
		"production_sequence": None,
		"autopilot_hardware": None,
		"recommended_manual_url": "https://service.tesla.com/docs/",
		"error": None,
	}
	if not normalized:
		base["error"] = "VIN is required."
		return base
	if len(normalized) != 17:
		base["error"] = "VIN must be 17 characters."
		return base

	wmi = normalized[0:3]
	if wmi not in ALL_WMI:
		base["error"] = f'"{wmi}" is not a recognized Tesla manufacturer code. Only Tesla VINs are supported.'
		return base

	expected_check = compute_vin_check_digit(normalized)
	is_valid_vin = 1 if normalized[8] == expected_check else 0

	model_code = normalized[3]
	year_code = normalized[9]
	is_legacy_s = model_code == "S" and year_code in S_LEGACY_YEARS
	seq = normalized[12:] if (is_legacy_s and year_code in S_LEGACY["typedSeq"]) else normalized[11:]
	try:
		seq_num = int(seq)
	except ValueError:
		seq_num = 0

	body_map = (S_LEGACY["body"].get(year_code) or BODY.get("S")) if is_legacy_s else BODY.get(model_code, {})
	fuel_map = S_LEGACY["fuel"].get(year_code, {}) if is_legacy_s else FUEL.get(model_code, {})
	drive_map = S_LEGACY["drive"].get(year_code, {}) if is_legacy_s else DRIVE.get(model_code, {})

	hw = get_hardware_generation(model_code, seq_num, normalized[10], year_code)
	derived = get_derived_specs(model_code, normalized[7], year_code, is_legacy_s)

	decoded_model = MODEL.get(model_code, "Unknown")
	decoded_year = YEAR.get(year_code, year_code)
	manual_url = get_tesla_manual_url_for_vehicle({"model": decoded_model, "model_year": decoded_year})

	return {
		"vin": normalized,
		"is_valid_length": True,
		"is_tesla_wmi": True,
		"is_valid_vin": is_valid_vin,
		"wmi": wmi,
		"manufacturer": WMI.get(model_code, {}).get(wmi, "Tesla"),
		"model": decoded_model,
		"model_year": decoded_year,
		"body_type": body_map.get(normalized[4], "Unknown") if body_map else "Unknown",
		"trim": derived.get("trim") or "Unknown",
		"drivetrain": derived.get("drivetrain") or "Unknown",
		"battery_type": fuel_map.get(normalized[6], "Unknown") if fuel_map else "Unknown",
		"drive_unit": drive_map.get(normalized[7], "Unknown") if drive_map else "Unknown",
		"assembly_plant": PLANT.get(normalized[10], normalized[10]),
		"production_sequence": seq,
		"autopilot_hardware": hw.get("t") if hw else "Unknown",
		"recommended_manual_url": manual_url,
		"error": None,
	}


class RepairVehicle(Document):
	def before_insert(self):
		self.normalize_vin()

	def before_validate(self):
		self.normalize_vin()

	def validate(self):
		self.normalize_vin()
		self.decode_vin()
		self.sync_customers()

	def normalize_vin(self):
		if self.vin:
			normalized = normalize_vin_string(self.vin)
			if self.vin != normalized:
				self.vin = normalized

	def decode_vin(self):
		if not self.vin or len(self.vin) != 17:
			self.clear_vin_fields()
			return

		decoded = decode_tesla_vin(self.vin)
		if not decoded["is_tesla_wmi"]:
			self.clear_vin_fields()
			self.is_valid_vin = 0
			frappe.throw(
				frappe._('"{0}" is not a recognized Tesla manufacturer code. Only Tesla VINs are supported.').format(
					decoded["wmi"]
				)
			)

		for field in (
			"is_valid_vin", "manufacturer", "model", "model_year", "body_type",
			"trim", "drivetrain", "battery_type", "drive_unit",
			"assembly_plant", "production_sequence", "autopilot_hardware",
		):
			setattr(self, field, decoded.get(field))

	def sync_customers(self):
		"""Sync top-level primary customer with child customers table."""
		# Prune any customer rows whose Customer record no longer exists in database
		if self.customers:
			self.customers = [
				row for row in self.customers if row.customer and frappe.db.exists("Customer", row.customer)
			]

		if self.customer and not frappe.db.exists("Customer", self.customer):
			self.customer = None

		# If customer field did not change but child primary changed, adopt child primary
		if not self.is_new() and not self.has_value_changed("customer"):
			primary_child = next((row.customer for row in (self.customers or []) if row.is_primary), None)
			if primary_child and primary_child != self.customer:
				self.customer = primary_child

		if self.customer:
			existing = {row.customer for row in (self.customers or [])}
			if self.customer not in existing:
				self.append("customers", {
					"customer": self.customer,
					"is_primary": 1 if not self.customers else 0,
				})

		if self.customers:
			if not self.customer:
				primary = next((row.customer for row in self.customers if row.is_primary), None)
				self.customer = primary or self.customers[0].customer
			for row in self.customers:
				row.is_primary = 1 if row.customer == self.customer else 0

	def add_customer(self, customer: str, is_primary: bool = False, relationship: Optional[str] = None):
		"""Link or update a customer on this vehicle."""
		if not customer or not frappe.db.exists("Customer", customer):
			frappe.throw(frappe._("Customer '{0}' does not exist.").format(customer))

		existing = {row.customer: row for row in (self.customers or [])}
		changed = False
		if customer not in existing:
			self.append("customers", {
				"customer": customer,
				"is_primary": 1 if (is_primary or not self.customers) else 0,
				"relationship": relationship,
			})
			if is_primary or not self.customer:
				self.customer = customer
			changed = True
		else:
			row = existing[customer]
			if relationship is not None and row.relationship != relationship:
				row.relationship = relationship
				changed = True
			if is_primary and self.customer != customer:
				self.customer = customer
				changed = True

		if changed:
			self.check_permission("write")
			self.save()

	def clear_vin_fields(self):
		fields = [
			"is_valid_vin", "manufacturer", "model", "model_year", "body_type",
			"trim", "drivetrain", "battery_type", "drive_unit",
			"assembly_plant", "production_sequence", "autopilot_hardware",
		]
		for f in fields:
			setattr(self, f, None)


def resolve_vehicle_identifier(vehicle_identifier: str) -> str:
	"""Resolve a Repair Vehicle document name from exact name, normalized VIN, or license plate."""
	if not isinstance(vehicle_identifier, str):
		frappe.throw(
			frappe._("Vehicle identifier (name, VIN, or license plate) must be a string."),
			frappe.ValidationError,
		)
	ident = vehicle_identifier.strip()
	if not ident:
		frappe.throw(frappe._("Vehicle identifier (name, VIN, or license plate) is required."))

	if frappe.db.exists("Repair Vehicle", ident):
		return ident

	norm_vin = normalize_vin_string(ident)
	if norm_vin:
		by_vin = frappe.db.get_value("Repair Vehicle", {"vin": norm_vin}, "name")
		if by_vin:
			return by_vin

	by_plate = frappe.db.sql(
		"""
		SELECT name FROM `tabRepair Vehicle`
		WHERE LOWER(REPLACE(IFNULL(license_plate, ''), ' ', '')) = LOWER(REPLACE(%s, ' ', ''))
		LIMIT 1
		""",
		(ident,),
		as_dict=True,
	)
	if by_plate:
		return by_plate[0].name

	frappe.throw(
		frappe._("Repair Vehicle '{0}' not found by name, VIN, or license plate.").format(ident),
		frappe.DoesNotExistError,
	)


def build_vehicle_search_results(
	query: Optional[str] = None,
	customer: Optional[str] = None,
	model: Optional[str] = None,
	limit: int = 20,
) -> list[dict[str, Any]]:
	"""Search vehicles across VIN, license plate, model, primary customer, and co-owners."""
	safe_limit = max(1, min(cint(limit) or 20, 100))
	conditions = []
	params: list[Any] = []

	if customer:
		cust_clean = str(customer).strip()
		conditions.append(
			"(rv.customer = %s OR EXISTS ("
			"SELECT 1 FROM `tabRepair Vehicle Customer` rvc "
			"WHERE rvc.parent = rv.name AND rvc.parenttype = 'Repair Vehicle' AND rvc.customer = %s"
			"))"
		)
		params.extend([cust_clean, cust_clean])

	if model:
		conditions.append("LOWER(IFNULL(rv.model, '')) LIKE LOWER(%s)")
		params.append(f"%{str(model).strip()}%")

	if query:
		q = str(query).strip()
		like_q = f"%{q}%"
		norm_q = normalize_vin_string(q) or q
		like_norm = f"%{norm_q}%"
		conditions.append(
			"("
			"rv.name LIKE %s OR rv.vin LIKE %s OR rv.vin LIKE %s "
			"OR IFNULL(rv.license_plate, '') LIKE %s "
			"OR IFNULL(rv.model, '') LIKE %s "
			"OR IFNULL(rv.customer, '') LIKE %s "
			"OR IFNULL(c.customer_name, '') LIKE %s "
			"OR EXISTS ("
			"SELECT 1 FROM `tabRepair Vehicle Customer` rvc "
			"LEFT JOIN `tabCustomer` c2 ON c2.name = rvc.customer "
			"WHERE rvc.parent = rv.name AND rvc.parenttype = 'Repair Vehicle' "
			"AND (rvc.customer LIKE %s OR IFNULL(c2.customer_name, '') LIKE %s)"
			")"
			")"
		)
		params.extend([like_q, like_q, like_norm, like_q, like_q, like_q, like_q, like_q, like_q])

	where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
	params.append(safe_limit)

	rows = frappe.db.sql(
		f"""
		SELECT
			rv.name, rv.vin, rv.license_plate, rv.color, rv.customer, c.customer_name,
			rv.is_valid_vin, rv.manufacturer, rv.model, rv.model_year, rv.body_type,
			rv.trim, rv.drivetrain, rv.battery_type, rv.drive_unit, rv.autopilot_hardware,
			rv.manufactured_month, rv.last_odometer, rv.last_service_date,
			rv.next_service_odometer, rv.next_service_date
		FROM `tabRepair Vehicle` rv
		LEFT JOIN `tabCustomer` c ON c.name = rv.customer
		{where_sql}
		ORDER BY rv.modified DESC
		LIMIT %s
		""",
		tuple(params),
		as_dict=True,
	)

	results = []
	for row in rows:
		item = dict(row)
		item["recommended_manual_url"] = get_tesla_manual_url_for_vehicle(item)
		results.append(item)
	return results


def build_vehicle_intelligence(vehicle_identifier: str, history_limit: int = 20) -> dict[str, Any]:
	"""Return comprehensive vehicle profile, VIN decode, multi-customer owners, RO history, and deferred items."""
	vehicle_name = resolve_vehicle_identifier(vehicle_identifier)
	vdoc = frappe.get_doc("Repair Vehicle", vehicle_name)
	vdoc.check_permission("read")
	safe_limit = max(1, min(cint(history_limit) or 20, 100))

	owners = _collect_vehicle_owners(vdoc)
	service_history = _collect_vehicle_service_history(vdoc.name, safe_limit)
	deferred_summary = _summarize_deferred_services(vdoc)
	manual_url = get_tesla_manual_url_for_vehicle(vdoc)

	return {
		"vehicle": {
			"name": vdoc.name,
			"vin": vdoc.vin,
			"is_valid_vin": cint(vdoc.is_valid_vin),
			"license_plate": vdoc.license_plate or "",
			"color": vdoc.color or "",
			"customer": vdoc.customer or "",
			"manufacturer": vdoc.manufacturer or "",
			"model": vdoc.model or "",
			"model_year": vdoc.model_year or "",
			"body_type": vdoc.body_type or "",
			"trim": vdoc.trim or "",
			"drivetrain": vdoc.drivetrain or "",
			"battery_type": vdoc.battery_type or "",
			"drive_unit": vdoc.drive_unit or "",
			"assembly_plant": vdoc.assembly_plant or "",
			"production_sequence": vdoc.production_sequence or "",
			"autopilot_hardware": vdoc.autopilot_hardware or "",
			"manufactured_month": vdoc.manufactured_month or "",
			"last_odometer": cint(vdoc.last_odometer),
			"last_service_date": str(vdoc.last_service_date) if vdoc.last_service_date else None,
			"next_service_odometer": cint(vdoc.next_service_odometer),
			"next_service_date": str(vdoc.next_service_date) if vdoc.next_service_date else None,
		},
		"vin_decode": decode_tesla_vin(vdoc.vin),
		"recommended_manual_url": manual_url,
		"owners": owners,
		"service_history": service_history,
		"deferred_services": deferred_summary,
	}


def _collect_vehicle_owners(vdoc) -> list[dict[str, Any]]:
	"""Collect primary and co-owner Customer records linked to a Repair Vehicle."""
	customer_ids = []
	for row in (vdoc.customers or []):
		if row.customer and row.customer not in customer_ids:
			customer_ids.append(row.customer)
	if vdoc.customer and vdoc.customer not in customer_ids:
		customer_ids.insert(0, vdoc.customer)

	customer_meta = {}
	if customer_ids:
		for c in frappe.get_all(
			"Customer",
			filters={"name": ["in", customer_ids]},
			fields=["name", "customer_name", "mobile_no", "email_id"],
		):
			customer_meta[c.name] = c

	owners = []
	seen = set()
	for row in (vdoc.customers or []):
		if not row.customer or row.customer in seen:
			continue
		seen.add(row.customer)
		meta = customer_meta.get(row.customer) or {}
		owners.append({
			"customer": row.customer,
			"customer_name": row.customer_name or meta.get("customer_name") or row.customer,
			"is_primary": cint(row.is_primary or (row.customer == vdoc.customer)),
			"relationship": row.relationship or "",
			"mobile_no": meta.get("mobile_no") or "",
			"email_id": meta.get("email_id") or "",
		})

	if vdoc.customer and vdoc.customer not in seen:
		meta = customer_meta.get(vdoc.customer) or {}
		owners.insert(0, {
			"customer": vdoc.customer,
			"customer_name": meta.get("customer_name") or vdoc.customer,
			"is_primary": 1,
			"relationship": "Primary Owner",
			"mobile_no": meta.get("mobile_no") or "",
			"email_id": meta.get("email_id") or "",
		})
	return owners


def _collect_vehicle_service_history(vehicle_name: str, limit: int) -> list[dict[str, Any]]:
	"""Fetch recent Repair Orders and summarized services for a vehicle without N+1 queries."""
	if not frappe.has_permission("Repair Order", "read"):
		return []

	orders = frappe.get_list(
		"Repair Order",
		filters={"vehicle": vehicle_name, "docstatus": ["<", 2]},
		fields=[
			"name", "customer", "status", "approval_status", "posting_date",
			"odometer_in", "odometer_out", "grand_total",
			"is_mobile_capable", "modified",
		],
		order_by="posting_date desc, modified desc",
		limit_page_length=limit,
	)
	if not orders:
		return []

	order_names = [o.name for o in orders]
	services_by_ro: dict[str, list[dict[str, Any]]] = {}
	labor_totals_by_ro: dict[str, float] = {}
	parts_totals_by_ro: dict[str, float] = {}
	for svc in frappe.get_all(
		"Repair Order Service",
		filters={"parent": ["in", order_names], "parenttype": "Repair Order"},
		fields=[
			"parent", "service_title", "correction_code", "service_position",
			"status", "labor_amount", "parts_total", "total_amount",
		],
		order_by="idx asc",
	):
		labor_amt = flt(svc.labor_amount, 2)
		parts_amt = flt(svc.parts_total, 2)
		labor_totals_by_ro[svc.parent] = flt(labor_totals_by_ro.get(svc.parent, 0.0) + labor_amt, 2)
		parts_totals_by_ro[svc.parent] = flt(parts_totals_by_ro.get(svc.parent, 0.0) + parts_amt, 2)
		services_by_ro.setdefault(svc.parent, []).append({
			"service_title": svc.service_title,
			"correction_code": svc.correction_code or "",
			"service_position": svc.service_position or "None",
			"status": svc.status,
			"labor_amount": labor_amt,
			"parts_total": parts_amt,
			"total_amount": flt(svc.total_amount, 2),
		})

	history = []
	for ro in orders:
		history.append({
			"name": ro.name,
			"customer": ro.customer,
			"status": ro.status,
			"approval_status": ro.approval_status or "",
			"posting_date": str(ro.posting_date) if ro.posting_date else None,
			"odometer_in": cint(ro.odometer_in),
			"odometer_out": cint(ro.odometer_out),
			"labor_total": flt(labor_totals_by_ro.get(ro.name, 0.0), 2),
			"parts_total": flt(parts_totals_by_ro.get(ro.name, 0.0), 2),
			"grand_total": flt(ro.grand_total, 2),
			"is_mobile_capable": cint(ro.is_mobile_capable),
			"services": services_by_ro.get(ro.name, []),
		})
	return history


def _summarize_deferred_services(vdoc) -> dict[str, Any]:
	"""Aggregate deferred service items and highlight open safety recommendations."""
	all_items = []
	open_items = []
	safety_hazard_count = 0
	open_estimated_total = 0.0

	for row in (vdoc.deferred_services or []):
		entry = {
			"name": row.name,
			"job_title": row.job_title,
			"decline_date": str(row.decline_date) if row.decline_date else None,
			"repair_order": row.repair_order or "",
			"estimated_amount": flt(row.estimated_amount, 2),
			"status": row.status or "Open Recommendation",
			"safety_hazard": cint(row.safety_hazard),
			"decline_reason": row.decline_reason or "",
		}
		all_items.append(entry)
		if entry["status"] == "Open Recommendation":
			open_items.append(entry)
			open_estimated_total += entry["estimated_amount"]
			if entry["safety_hazard"]:
				safety_hazard_count += 1

	return {
		"open_count": len(open_items),
		"total_count": len(all_items),
		"safety_hazard_count": safety_hazard_count,
		"open_estimated_total": flt(open_estimated_total, 2),
		"open": open_items,
		"all": all_items,
	}


def on_customer_trash(doc, method=None):
	"""Clean up any Repair Vehicle Customer child links when a Customer is deleted."""
	frappe.db.delete("Repair Vehicle Customer", {"customer": doc.name})

