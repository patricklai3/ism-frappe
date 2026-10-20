// Copyright (c) 2026, patricklai3 and contributors
// For license information, please see license.txt

frappe.listview_settings["Shop Action Item"] = {
	has_indicator_for_draft: false,
	add_fields: ["status", "priority", "category", "reference_doctype", "reference_name"],
	get_indicator: function (doc) {
		const status = doc.status || "Open";
		if (status === "Open") {
			return [__(status), "red", `status,=,${status}`];
		} else if (status === "In Progress") {
			return [__(status), "blue", `status,=,${status}`];
		} else if (status === "Resolved") {
			return [__(status), "green", `status,=,${status}`];
		} else if (status === "Dismissed") {
			return [__(status), "gray", `status,=,${status}`];
		}
		return [__(status), "orange", `status,=,${status}`];
	},
};
