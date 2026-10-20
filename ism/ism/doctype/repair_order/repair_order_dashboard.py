# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt


def get_data():
	return {
		"fieldname": "repair_order",
		"non_standard_fieldnames": {
			"Shop Action Item": "reference_name",
		},
		"dynamic_links": {
			"reference_name": ["Repair Order", "reference_doctype"],
		},
		"internal_links": {
			"Sales Invoice": "sales_invoice",
		},
		"transactions": [
			{
				"label": "Procurement & Billing",
				"items": ["Purchase Order", "Request for Quotation", "Sales Invoice"],
			},
			{
				"label": "Shop Floor Actions",
				"items": ["Shop Action Item"],
			},
		],
	}
