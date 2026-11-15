# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re
from typing import Any, Optional

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
		"F": {
			"S": "Li-ion — Standard Capacity",
			"H": "Li-ion — High Capacity",
			"V": "Li-ion — Ultra High Capacity",
			"E": "Electric",
		},
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

# Lookup tables for hardware generation resolution
_CONSTANT_HW_MODELS = {"C": "HW4", "A": "HW4", "T": "HW4"}
_MODEL_Y_PLANT_SEQ_HW = {
	"F": ((800000, "Likely HW4"), (790000, "Possibly HW4")),
	"A": ((131200, "Likely HW4"), (127000, "Possibly HW4")),
}
_MODEL_X_SEQ_HW = ((385000, "Likely HW4"), (370000, "Possibly HW4"))
_MODEL_S_PAL_SEQ_HW = ((502000, "Likely HW4"), (501000, "Possibly HW4"))

_HW_BY_MODEL_YEAR = {
	"X": {
		2022: "Shipped with HW3",
		2021: "Shipped with HW3",
		2020: "Shipped with HW3",
		2019: "HW2.5 or HW3",
		2018: "Shipped with HW2.5",
		2017: "HW2.0 or HW2.5",
		2016: "HW1 or HW2.0",
		2015: "Shipped with HW1",
	},
	"S_LEGACY": {
		2020: "Shipped with HW3",
		2019: "HW2.5 or HW3",
		2018: "Shipped with HW2.5",
		2017: "HW2.0 or HW2.5",
		2016: "HW1 or HW2.0",
		2015: "Shipped with HW1",
		2014: "No AP or HW1",
	},
	"3": {
		2023: "Possibly HW4",
		2022: "Shipped with HW3",
		2021: "Shipped with HW3",
		2020: "Shipped with HW3",
		2019: "HW2.5 or HW3",
		2018: "Shipped with HW2.5",
		2017: "Shipped with HW2.5",
	},
}

# Lookup tables for drivetrain & trim resolution
_AWD_DRIVE_CODES = {
	"S": frozenset(("5", "6")),
	"3": frozenset(("B", "C", "K", "L", "T")),
	"X": frozenset(("5", "6")),
	"Y": frozenset(("E", "F", "K", "L")),
	"C": frozenset(("D", "E")),
}
_LEGACY_S_BATTERY_TRIMS = {
	"C": "40 kWh",
	"G": "60 kWh",
	"N": "85 kWh",
	"P": "P85 Performance",
}
_LEGACY_S_TRIM_WINDOWS = (
	(2014, {"2": "Dual Motor"}, "Standard"),
	(2018, {"1": "Standard", "2": "Dual Motor", "3": "Performance", "4": "Dual Motor Performance"}, None),
	(9999, {"1": "Standard Range", "2": "Long Range", "3": "Performance", "4": "Long Range Performance"}, None),
)
_STANDARD_TRIMS = {
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


def _match_seq_threshold(seq: int, thresholds: tuple[tuple[int, str], ...]) -> Optional[str]:
	"""Return the first label whose sequence threshold is met by seq."""
	for min_seq, label in thresholds:
		if seq >= min_seq:
			return label
	return None


def get_hardware_generation(model_code: str, seq: int, plant_code: str, year_code: str) -> Optional[dict]:
	"""Resolve autopilot hardware generation from Tesla VIN model, sequence, plant, and year."""
	year = int(YEAR.get(year_code, 0))
	if model_code in _CONSTANT_HW_MODELS:
		return {"t": _CONSTANT_HW_MODELS[model_code]}

	if model_code == "Y":
		if year >= 2024:
			return {"t": "HW4"}
		matched = _match_seq_threshold(seq, _MODEL_Y_PLANT_SEQ_HW.get(plant_code, ()))
		if matched:
			return {"t": matched}
		return {"t": "Possibly HW4" if year == 2023 else "Shipped with HW3"}

	if model_code == "X":
		matched = _match_seq_threshold(seq, _MODEL_X_SEQ_HW)
		if matched:
			return {"t": matched}
		label = _HW_BY_MODEL_YEAR["X"].get(year)
		return {"t": label} if label else None

	if model_code == "S":
		if year_code not in S_LEGACY_YEARS:
			matched = _match_seq_threshold(seq, _MODEL_S_PAL_SEQ_HW)
			return {"t": matched or "Shipped with HW3"}
		label = _HW_BY_MODEL_YEAR["S_LEGACY"].get(year)
		if label:
			return {"t": label}
		return {"t": "No autopilot hardware"} if year <= 2013 else None

	if model_code == "3":
		if year >= 2024:
			return {"t": "HW4"}
		label = _HW_BY_MODEL_YEAR["3"].get(year)
		return {"t": label} if label else None

	return None


def get_derived_specs(model_code: str, drive_code: str, year_code: str, is_legacy_s: bool) -> dict:
	"""Derive normalized drivetrain and trim labels from Tesla VIN components."""
	year = int(YEAR.get(year_code, 0))
	if model_code == "R":
		drivetrain = "RWD"
	elif model_code in ("T", "A"):
		drivetrain = None
	elif is_legacy_s:
		drivetrain = "AWD" if (year > 2013 and drive_code in ("2", "4")) else "RWD"
	elif drive_code:
		drivetrain = "AWD" if drive_code in _AWD_DRIVE_CODES.get(model_code, ()) else "RWD"
	else:
		drivetrain = None

	if is_legacy_s:
		if drive_code in _LEGACY_S_BATTERY_TRIMS:
			trim = _LEGACY_S_BATTERY_TRIMS[drive_code]
		else:
			trim = None
			for max_year, trim_map, default_trim in _LEGACY_S_TRIM_WINDOWS:
				if year <= max_year:
					trim = trim_map.get(drive_code, default_trim)
					break
	else:
		trim = _STANDARD_TRIMS.get(model_code, {}).get(drive_code)

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
