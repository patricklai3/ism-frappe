/**
 * ISM Shared Vehicle Display Component
 * Classic script (no bundler) exposing window.ISMVehicleDisplay for Desk & SPAs.
 *
 * Progressive hiding hierarchy (paired with vehicle_display.css):
 *   1st hidden: VIN (.ism-veh__vin)
 *   2nd hidden: Year (.ism-veh__year) & License Plate (.ism-veh__plate)
 *   Kept visible longest: Color (.ism-veh__color) & Model (.ism-veh__model)
 */
(function (global) {
	"use strict";

	function cleanStr(val) {
		if (val === null || val === undefined) return "";
		return String(val).trim();
	}

	function escapeHtml(value) {
		return cleanStr(value)
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;")
			.replace(/"/g, "&quot;")
			.replace(/'/g, "&#39;");
	}

	function normalize(input) {
		if (!input) {
			return {
				year: "",
				model: "",
				color: "",
				plate: "",
				vin: "",
				manufacturer: "",
				display: "",
				compactDisplay: "",
			};
		}

		if (typeof input === "string") {
			const trimmed = cleanStr(input);
			return {
				year: "",
				model: "",
				color: "",
				plate: "",
				vin: "",
				manufacturer: "",
				display: trimmed,
				compactDisplay: trimmed,
			};
		}

		const nested =
			(input.vehicle_info && typeof input.vehicle_info === "object"
				? input.vehicle_info
				: null) ||
			(input.vehicle && typeof input.vehicle === "object" ? input.vehicle : null);

		const pick = function () {
			for (let i = 0; i < arguments.length; i += 1) {
				const val = cleanStr(arguments[i]);
				if (val) return val;
			}
			return "";
		};

		const year = pick(
			nested && nested.model_year,
			nested && nested.year,
			nested && nested.vehicle_year,
			input.model_year,
			input.vehicle_year,
			input.year
		);
		const manufacturer = pick(
			nested && nested.manufacturer,
			input.manufacturer
		);
		const model = pick(
			nested && nested.model,
			nested && nested.vehicle_model,
			input.vehicle_model,
			input.model
		);
		const color = pick(
			nested && nested.color,
			nested && nested.vehicle_color,
			input.vehicle_color,
			input.color
		);
		const plate = pick(
			nested && nested.license_plate,
			nested && nested.plate,
			input.license_plate,
			input.plate
		);
		const vin = pick(
			nested && nested.vin,
			input.vin
		);

		const rawDisplay = pick(
			nested && nested.display,
			nested && nested.label,
			input.vehicle_display,
			input.vehicle_label,
			input.display,
			input.label
		);
		const rawCompact = pick(
			nested && nested.compact_display,
			nested && nested.compact_label,
			input.vehicle_compact_display,
			input.vehicle_compact_label,
			input.compact_display,
			input.compact_label
		);

		const modelPart = model || manufacturer;
		const fullCore = [year, color, modelPart].filter(Boolean).join(" ");
		const computedDisplay = fullCore
			? plate
				? fullCore + " (" + plate + ")"
				: fullCore
			: plate || rawDisplay || vin;
		const computedCompact =
			[color, modelPart].filter(Boolean).join(" ") ||
			rawCompact ||
			computedDisplay;

		return {
			year: year,
			model: model,
			color: color,
			plate: plate,
			vin: vin,
			manufacturer: manufacturer,
			display: computedDisplay || rawDisplay,
			compactDisplay: computedCompact,
		};
	}

	function hasHumanFields(n) {
		return Boolean(n.year || n.color || n.model || n.manufacturer || n.plate);
	}

	function formatText(input, opts) {
		const options = opts || {};
		const includeVin = Boolean(options.includeVin);
		const compact = Boolean(options.compact);
		const fallback =
			options.fallback !== undefined ? String(options.fallback) : "Vehicle";

		const n = normalize(input);
		const modelPart = n.model || n.manufacturer;

		if (compact) {
			const compactCore = [n.color, modelPart].filter(Boolean).join(" ");
			if (compactCore) return compactCore;
			if (n.compactDisplay) return n.compactDisplay;
		}

		let text = "";
		if (hasHumanFields(n)) {
			const core = [n.year, n.color, modelPart].filter(Boolean).join(" ");
			if (core && n.plate) {
				text = core + " (" + n.plate + ")";
			} else {
				text = core || n.plate;
			}
			if (includeVin && n.vin) {
				text = text ? text + " · VIN: " + n.vin : "VIN: " + n.vin;
			}
			return text;
		}

		if (n.display) {
			if (includeVin && n.vin && n.display.indexOf(n.vin) === -1) {
				return n.display + " · VIN: " + n.vin;
			}
			return n.display;
		}
		if (n.vin) {
			return n.vin;
		}
		return fallback;
	}

	function formatTooltip(input) {
		const n = normalize(input);
		const modelPart = n.model || n.manufacturer;
		const core = [n.year, n.color, modelPart].filter(Boolean).join(" ");
		const parts = [];

		if (core) {
			parts.push(core);
		} else if (n.display && n.display !== n.plate && n.display !== n.vin) {
			parts.push(n.display);
		}
		if (n.plate) {
			parts.push("Plate: " + n.plate);
		}
		if (n.vin) {
			parts.push("VIN: " + n.vin);
		}
		return parts.join(" · ");
	}

	function renderHtml(input, opts) {
		const options = opts || {};
		const showVin = Boolean(options.showVin);
		const compact = Boolean(options.compact);
		const extraClass = cleanStr(options.extraClass);
		const fallback =
			options.fallback !== undefined ? String(options.fallback) : "Vehicle";

		const n = normalize(input);
		const tooltip = formatTooltip(n);

		const classes = ["ism-veh"];
		if (compact) classes.push("ism-veh--compact");
		if (extraClass) classes.push(extraClass);
		const classAttr = escapeHtml(classes.join(" "));

		if (!hasHumanFields(n)) {
			const fb = n.display || n.vin || fallback;
			if (!fb) return "";
			return (
				'<span class="' +
				classAttr +
				'" title="' +
				escapeHtml(tooltip || fb) +
				'">' +
				'<span class="ism-veh__fallback">' +
				escapeHtml(fb) +
				"</span></span>"
			);
		}

		const modelPart = n.model || n.manufacturer;
		const chunks = [];
		if (n.year) {
			chunks.push(
				'<span class="ism-veh__year">' + escapeHtml(n.year) + "</span>"
			);
		}
		if (n.color) {
			chunks.push(
				'<span class="ism-veh__color">' + escapeHtml(n.color) + "</span>"
			);
		}
		if (modelPart) {
			chunks.push(
				'<span class="ism-veh__model">' + escapeHtml(modelPart) + "</span>"
			);
		}
		if (n.plate) {
			chunks.push(
				'<span class="ism-veh__plate">' + escapeHtml(n.plate) + "</span>"
			);
		}
		if (showVin && n.vin) {
			chunks.push(
				'<span class="ism-veh__vin">VIN: ' + escapeHtml(n.vin) + "</span>"
			);
		}

		return (
			'<span class="' +
			classAttr +
			'" title="' +
			escapeHtml(tooltip) +
			'">' +
			chunks.join("") +
			"</span>"
		);
	}

	function matchesSearch(input, query) {
		const q = cleanStr(query).toLowerCase();
		if (!q) return true;
		const n = normalize(input);
		const hay = [
			n.year,
			n.color,
			n.manufacturer,
			n.model,
			n.plate,
			n.vin,
			n.display,
			n.compactDisplay,
		]
			.filter(Boolean)
			.join(" ")
			.toLowerCase();
		if (!hay) return false;
		if (hay.indexOf(q) !== -1) return true;
		const tokens = q.split(/\s+/).filter(Boolean);
		if (tokens.length > 1) {
			return tokens.every(function (tok) {
				return hay.indexOf(tok) !== -1;
			});
		}
		return false;
	}

	global.ISMVehicleDisplay = {
		normalize: normalize,
		formatText: formatText,
		formatTooltip: formatTooltip,
		renderHtml: renderHtml,
		matchesSearch: matchesSearch,
	};
})(typeof window !== "undefined" ? window : globalThis);
