import re
import frappe


def execute():
	packages = frappe.get_all("Service Package", fields=["name", "package_name", "correction_code"])
	for pkg in packages:
		code = str(pkg.correction_code or "").strip()
		pkg_name = str(pkg.package_name or pkg.name or "").strip()
		if code:
			clean_title = re.sub(rf"^{re.escape(code)}\s*[-–—:]?\s*", "", pkg_name).strip()
		else:
			clean_title = re.sub(r"^\d{8}\s*[-–—:]?\s*", "", pkg_name).strip()

		if clean_title and clean_title != pkg_name:
			frappe.db.set_value("Service Package", pkg.name, "package_name", clean_title, update_modified=False)

		target_name = clean_title if (clean_title and clean_title != pkg_name) else pkg.package_name
		if target_name and pkg.name != target_name and not frappe.db.exists("Service Package", target_name):
			try:
				frappe.rename_doc("Service Package", pkg.name, target_name, force=True, ignore_permissions=True)
			except Exception:
				frappe.log_error(
					message=frappe.get_traceback(),
					title=f"Service Package rename failed: {pkg.name} -> {target_name}"[:140],
				)

	services = frappe.get_all("Repair Order Service", fields=["name", "service_title", "correction_code"])
	for srv in services:
		code = str(srv.correction_code or "").strip()
		title = str(srv.service_title or "").strip()
		if code:
			clean_title = re.sub(rf"^{re.escape(code)}\s*[-–—:]?\s*", "", title).strip()
			if clean_title and clean_title != title:
				frappe.db.set_value("Repair Order Service", srv.name, "service_title", clean_title, update_modified=False)
