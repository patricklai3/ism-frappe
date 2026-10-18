import frappe

def main():
	frappe.init("development.localhost")
	frappe.connect()
	print("--- Item Supplier ---")
	for f in frappe.get_meta("Item Supplier").fields:
		print(f"{f.fieldname}: {f.fieldtype} ({f.options})")

	print("\n--- Purchase Order required fields ---")
	for f in frappe.get_meta("Purchase Order").fields:
		if f.reqd:
			print(f"{f.fieldname}: {f.fieldtype} ({f.options})")

	print("\n--- Purchase Order Item required fields ---")
	for f in frappe.get_meta("Purchase Order Item").fields:
		if f.reqd:
			print(f"{f.fieldname}: {f.fieldtype} ({f.options})")

	print("\n--- Request for Quotation required fields ---")
	for f in frappe.get_meta("Request for Quotation").fields:
		if f.reqd:
			print(f"{f.fieldname}: {f.fieldtype} ({f.options})")

	print("\n--- RFQ Item & Supplier required fields ---")
	for f in frappe.get_meta("Request for Quotation Item").fields:
		if f.reqd:
			print(f"Item: {f.fieldname}: {f.fieldtype}")
	for f in frappe.get_meta("Request for Quotation Supplier").fields:
		if f.reqd:
			print(f"Supplier: {f.fieldname}: {f.fieldtype}")

if __name__ == "__main__":
	main()
