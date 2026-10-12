# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import flt
from ism.tests.base import ISMTestCase
from ism.utils.tesla_manual import (
	get_category_for_correction_code,
	parse_tesla_manual_url,
	parse_vehicle_year_month,
	is_in_generation_range,
	get_tesla_manual_url_for_vehicle,
	validate_vehicle_compatibility,
	parse_tesla_procedure_html,
	_MANIFEST_CACHE,
)
from ism.api.tesla import import_tesla_procedure, get_active_repair_orders, get_vehicle_manual_url
from ism.ism.doctype.repair_order.repair_order import _get_default_labor_rate


class TestTeslaIntegration(ISMTestCase):
	def setUp(self):
		super().setUp()
		_MANIFEST_CACHE.clear()

	def tearDown(self):
		_MANIFEST_CACHE.clear()
		super().tearDown()

	def test_category_taxonomy_mapping(self):
		# 00 -> INSPECTION AND SERVICE
		cat, subcat = get_category_for_correction_code("00030700")
		self.assertEqual(cat, "INSPECTION AND SERVICE")

		# 3301 -> BRAKES / Brake Discs and Calipers
		cat, subcat = get_category_for_correction_code("33010100")
		self.assertEqual(cat, "BRAKES")
		self.assertEqual(subcat, "Brake Discs and Calipers")

		# 1001 -> BODY / Bumper/Fascia
		cat, subcat = get_category_for_correction_code("10010000")
		self.assertEqual(cat, "BODY")
		self.assertEqual(subcat, "Bumper/Fascia")

		# 1750 -> ELECTRICAL / Wipers and Washers
		cat, subcat = get_category_for_correction_code("17500100")
		self.assertEqual(cat, "ELECTRICAL")
		self.assertEqual(subcat, "Wipers and Washers")

	def test_generation_url_parsing(self):
		# Model 3 Classic
		m3_url = "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-C24C6004-94BF-4099-8681-8D6598EE6E50.html"
		p1 = parse_tesla_manual_url(m3_url)
		self.assertEqual(p1["applicable_model"], "Model 3")
		self.assertEqual(p1["matched_generation"], "Model 3 Jun 2017 - Dec 2023")
		self.assertEqual(p1["doc_guid"], "GUID-C24C6004-94BF-4099-8681-8D6598EE6E50")

		# Model 3 2024 Highland
		m3_24_url = "https://service.tesla.com/docs/Model3/ServiceManual/2024/en-us/GUID-AAAA-BBBB-CCCC-DDDD.html"
		p2 = parse_tesla_manual_url(m3_24_url)
		self.assertEqual(p2["applicable_model"], "Model 3")
		self.assertEqual(p2["matched_generation"], "Model 3 Jan 2024")

		# Model S Palladium
		ms_pal_url = "https://service.tesla.com/docs/ModelS/ServiceManual/Palladium/en-us/GUID-1111.html"
		p3 = parse_tesla_manual_url(ms_pal_url)
		self.assertEqual(p3["applicable_model"], "Model S")
		self.assertEqual(p3["matched_generation"], "Model S Feb 2021 - May 2025")

		# Model Y 2025 Juniper
		my_25_url = "https://service.tesla.com/docs/ModelY/ServiceManual/2025/en-us/GUID-2222.html"
		p4 = parse_tesla_manual_url(my_25_url)
		self.assertEqual(p4["applicable_model"], "Model Y")
		self.assertEqual(p4["matched_generation"], "Model Y Feb 2025")

		# Cybertruck
		ct_url = "https://service.tesla.com/docs/Cybertruck/ServiceManual/en-us/GUID-3333.html"
		p5 = parse_tesla_manual_url(ct_url)
		self.assertEqual(p5["applicable_model"], "Cybertruck")

	def test_vehicle_date_and_url_resolution(self):
		# Test date parsing
		self.assertEqual(parse_vehicle_year_month("05/22"), (2022, 5))
		self.assertEqual(parse_vehicle_year_month("May 2022"), (2022, 5))
		self.assertEqual(parse_vehicle_year_month(None, 2023), (2023, 6))

		# Create mock vehicle objects
		v_m3_22 = frappe._dict({
			"model": "Model 3",
			"model_year": 2022,
			"manufactured_month": "05/22",
		})
		url1 = get_tesla_manual_url_for_vehicle(v_m3_22)
		self.assertEqual(url1, "https://service.tesla.com/docs/Model3/ServiceManual/en-us/")

		v_m3_24 = frappe._dict({
			"model": "Model 3",
			"model_year": 2024,
			"manufactured_month": "03/24",
		})
		url2 = get_tesla_manual_url_for_vehicle(v_m3_24)
		self.assertEqual(url2, "https://service.tesla.com/docs/Model3/ServiceManual/2024/en-us/")

	def test_generation_compatibility_validation(self):
		v_m3_20 = frappe._dict({
			"model": "Model 3",
			"model_year": 2020,
			"manufactured_month": "06/20",
		})

		# Compatible URL (2017-2023 Model 3)
		compat_good = validate_vehicle_compatibility(
			v_m3_20,
			"https://service.tesla.com/docs/Model3/ServiceManual/en-us/"
		)
		self.assertTrue(compat_good["is_compatible"])

		# Mismatched URL (2024+ Highland on 2020 Model 3)
		compat_bad = validate_vehicle_compatibility(
			v_m3_20,
			"https://service.tesla.com/docs/Model3/ServiceManual/2024/en-us/"
		)
		self.assertFalse(compat_bad["is_compatible"])
		self.assertIn("Generation mismatch", compat_bad["warning"])

		# Model Mismatch (Model Y procedure on Model 3 vehicle)
		compat_model_mismatch = validate_vehicle_compatibility(
			v_m3_20,
			"https://service.tesla.com/docs/ModelY/ServiceManual/en-us/GUID-XXXX.html"
		)
		self.assertFalse(compat_model_mismatch["is_compatible"])
		self.assertIn("Model mismatch", compat_model_mismatch["warning"])

	def test_html_procedure_parsing(self):
		sample_html = """
		<html>
		<head><title>Basic Vehicle Inspection (Inspection) | Tesla</title></head>
		<body>
			<article>
				<h1 class="title topictitle1">
					<span class="ph procedure-title">Basic Vehicle Inspection (Inspection)</span>
				</h1>
				<p class="shortdesc">
					<span class="ph correction-code-title">Correction code</span>
					<span class="ph correction-code">00030700</span>
					<span class="ph frt-title">FRT</span>
					<span class="ph frt">0.90</span>
					Scope of vehicle check.
				</p>
			</article>
		</body>
		</html>
		"""
		url = "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-C24C6004-94BF-4099-8681-8D6598EE6E50.html"
		parsed = parse_tesla_procedure_html(sample_html, url)

		self.assertEqual(parsed["title"], "Basic Vehicle Inspection (Inspection)")
		self.assertEqual(parsed["correction_code"], "00030700")
		self.assertEqual(parsed["frt"], 0.90)
		self.assertEqual(parsed["category"], "INSPECTION AND SERVICE")
		self.assertEqual(parsed["doc_guid"], "GUID-C24C6004-94BF-4099-8681-8D6598EE6E50")
		self.assertEqual(parsed["is_mobile_capable"], 0)

		# Test mobile-capable procedure
		mobile_url = "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-000FD8BF-C857-490D-85B1-2DD266887CB3.html"
		_MANIFEST_CACHE["https://service.tesla.com/docs/Model3/ServiceManual/en-us/dependencies.json"] = [
			{
				"guid": "GUID-000FD8BF-C857-490D-85B1-2DD266887CB3",
				"url": "GUID-000FD8BF-C857-490D-85B1-2DD266887CB3.html",
				"correction-code": "00030700",
				"mobile-capable": "Level 1",
			}
		]
		parsed_mobile = parse_tesla_procedure_html(sample_html, mobile_url)
		self.assertEqual(parsed_mobile["is_mobile_capable"], 1)
		self.assertIn("Level 1", parsed_mobile["mobile_service_level"])

		# Test Model Y manifest match with GUID and Level 1
		model_y_url = "https://service.tesla.com/docs/ModelY/ServiceManual/en-us/GUID-2210D20F-E132-4806-8C20-E44630117D89.html"
		_MANIFEST_CACHE["https://service.tesla.com/docs/ModelY/ServiceManual/en-us/dependencies.json"] = [
			{
				"guid": "GUID-2210D20F-E132-4806-8C20-E44630117D89",
				"url": "GUID-2210D20F-E132-4806-8C20-E44630117D89.html",
				"correction-code": 12201402,
				"mobile-capable": "Level 1",
			}
		]
		parsed_my = parse_tesla_procedure_html(sample_html, model_y_url)
		self.assertEqual(parsed_my["is_mobile_capable"], 1)
		self.assertEqual(parsed_my["mobile_service_level"], "Level 1")

		# Test DOM-based mobile capability indicators
		sample_dom_mobile = """
		<html>
		<body>
			<article>
				<h1 class="title topictitle1"><span class="ph procedure-title">Sample Mobile Procedure</span></h1>
				<div class="mobile-capable-indicator show-mobile-capable-indicator">
					<span class="tds-chip--green-pastel">Mobile Capable</span>
				</div>
				<p class="shortdesc">
					<span class="ph correction-code">12345678</span>
					<span class="ph frt">0.20</span>
				</p>
			</article>
		</body>
		</html>
		"""
		parsed_dom = parse_tesla_procedure_html(sample_dom_mobile, "https://service.tesla.com/docs/ModelY/ServiceManual/en-us/GUID-NONEXISTENT.html")
		self.assertEqual(parsed_dom["is_mobile_capable"], 1)

		# Test Service Package validate auto-populates is_mobile_capable
		pkg = frappe.new_doc("Service Package")
		pkg.package_name = "Test Mobile Service"
		pkg.correction_code = "12201402"
		pkg.tesla_manual_url = model_y_url
		pkg.doc_guid = "GUID-2210D20F-E132-4806-8C20-E44630117D89"
		pkg.validate()
		self.assertEqual(pkg.is_mobile_capable, 1)
		_MANIFEST_CACHE.clear()

	def test_import_tesla_procedure_end_to_end(self):
		cust = self.create_test_customer()
		veh = self.create_test_vehicle(
			vin="5YJ3E1EBXNF999999",
			customer=cust.name,
			model="Model 3",
			model_year="2022",
			manufactured_month="05/22",
		)
		ro = self.create_test_repair_order(customer=cust.name, vehicle=veh.name)
		self.track(ro)

		proc_payload = {
			"title": "Basic Vehicle Inspection (Inspection)",
			"correction_code": "00030700",
			"frt": 0.90,
			"applicable_model": "Model 3",
			"applicable_generation": "Model 3 Jun 2017 - Dec 2023",
			"doc_guid": "GUID-C24C6004-94BF-4099-8681-8D6598EE6E50",
			"tesla_manual_url": "https://service.tesla.com/docs/Model3/ServiceManual/en-us/",
			"description": "Basic vehicle multi-point inspection.",
			"is_mobile_capable": 0,
			"mobile_service_level": "No",
		}

		res = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload
		)
		self.assertEqual(res["status"], "success")
		self.assertEqual(res["correction_code"], "00030700")
		self.assertEqual(res["frt"], 0.90)
		self.assertEqual(res["is_mobile_capable"], 0)

		# Verify Service Package was created and properly populated
		pkg_name = res["service_package"]
		pkg = frappe.get_doc("Service Package", pkg_name)
		self.track(pkg)
		self.assertEqual(pkg.correction_code, "00030700")
		self.assertEqual(flt(pkg.frt), 0.90)
		self.assertEqual(flt(pkg.standard_labor_hours), 0.90)
		self.assertEqual(pkg.is_mobile_capable, 0)
		self.assertEqual(pkg.package_name, "Basic Vehicle Inspection (Inspection)")
		self.assertFalse(pkg.package_name.startswith("00030700"))

		# Verify Repair Order Service row and RO scheduling flags
		ro.reload()
		self.assertEqual(len(ro.services), 1)
		srv = ro.services[0]
		self.assertEqual(srv.service_package, pkg.name)
		self.assertEqual(srv.service_title, "Basic Vehicle Inspection (Inspection)")
		self.assertFalse(srv.service_title.startswith("00030700"))
		self.assertEqual(srv.correction_code, "00030700")
		self.assertEqual(srv.category, "INSPECTION AND SERVICE")
		self.assertEqual(flt(srv.frt), 0.90)
		self.assertEqual(flt(srv.labor_hours), 0.90)
		self.assertEqual(srv.is_mobile_capable, 0)
		self.assertEqual(srv.is_supplement, 0)
		self.assertEqual(ro.is_mobile_capable, 0)

		default_rate = _get_default_labor_rate()
		expected_labor_amt = flt(0.90 * default_rate, 2)
		self.assertEqual(flt(srv.labor_amount), expected_labor_amt)

		# Re-importing same procedure without duplicate_action should trigger duplicate_warning
		res2 = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload
		)
		self.assertEqual(res2["status"], "duplicate_warning")
		self.assertTrue(res2["requires_duplicate_choice"])
		self.assertEqual(
			res2["message"],
			"Correction code 00030700 is already on this Repair Order. Update existing row or Add as additional job?"
		)

		# Re-importing with duplicate_action="update" should update existing row
		res_update = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload,
			duplicate_action="update"
		)
		self.assertEqual(res_update["status"], "success")
		self.assertEqual(res_update["action"], "updated")
		self.assertEqual(res_update["service_package"], pkg_name)
		ro.reload()
		self.assertEqual(len(ro.services), 1)

		# Re-importing with duplicate_action="add" should append an additional row
		res_add = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload,
			duplicate_action="add"
		)
		self.assertEqual(res_add["status"], "success")
		self.assertEqual(res_add["action"], "added")
		ro.reload()
		self.assertEqual(len(ro.services), 2)

		# Verify imported service into an order does NOT default to supplement
		ro.services[0].status = "Approved"
		ro.approval_status = "Approved"
		ro.save(ignore_permissions=True)

		proc_payload2 = {
			"title": "Garnish - Trunk (Remove and Replace)",
			"correction_code": "15111102",
			"frt": 0.06,
			"applicable_model": "Model 3",
			"tesla_manual_url": "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-E9564680-EA4B-4815-A239-171679188CA3.html"
		}
		res3 = import_tesla_procedure(repair_order=ro.name, data=proc_payload2)
		self.assertEqual(res3["status"], "success")
		ro.reload()
		self.assertEqual(len(ro.services), 3)
		# Newly imported procedure on an authorized order must be flagged as a supplement in Estimating (unsent)
		self.assertEqual(ro.services[2].is_supplement, 1)
		self.assertEqual(ro.services[2].status, "Estimating")
		self.assertEqual(ro.approval_status, "Partially Approved")

		# Generating approval token communicates the supplement and promotes Estimating -> Pending Approval
		ro.generate_approval_token()
		ro.reload()
		self.assertEqual(ro.services[2].status, "Pending Approval")
		self.assertEqual(ro.approval_status, "Partially Approved")

		# Explicit supplement import should set is_supplement = 1 and status Estimating
		proc_payload3 = {
			"title": "Trunk Hinge Replacement",
			"correction_code": "15111199",
			"frt": 0.50,
			"is_supplement": 1
		}
		res4 = import_tesla_procedure(repair_order=ro.name, data=proc_payload3)
		self.assertEqual(res4["status"], "success")
		ro.reload()
		self.assertEqual(len(ro.services), 4)
		self.assertEqual(ro.services[3].is_supplement, 1)
		self.assertEqual(ro.services[3].status, "Estimating")
		self.assertEqual(ro.approval_status, "Partially Approved")

	def test_symmetrical_procedure_import(self):
		from ism.utils.tesla_manual import swap_side_in_title, detect_service_side

		# 1. Helper tests
		self.assertEqual(swap_side_in_title("Door - Front - LH (Remove and Replace)", "RH"), "Door - Front - RH (Remove and Replace)")
		self.assertEqual(swap_side_in_title("Upper Control Arm - Front - RH", "LH"), "Upper Control Arm - Front - LH")
		self.assertEqual(detect_service_side("Mirror - Side - LH"), "LH")
		self.assertEqual(detect_service_side("Mirror - Side - RH"), "RH")

		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)
		ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)

		proc_payload = {
			"title": "Door - Front - LH (Remove and Replace)",
			"correction_code": "10106002",
			"rh_correction_code": "10106102",
			"frt": 1.25,
			"is_symmetrical": 1,
			"applicable_model": "Model Y",
			"tesla_manual_url": "https://service.tesla.com/docs/ModelY/ServiceManual/en-us/GUID-DOOR-LH.html",
		}

		# Import with side="both"
		res = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload,
			side="both",
			allow_generation_mismatch=1,
		)
		self.assertEqual(res["status"], "success")
		ro.reload()
		self.assertEqual(len(ro.services), 2)

		lh_service = ro.services[0]
		rh_service = ro.services[1]

		self.assertEqual(lh_service.service_position, "LH")
		self.assertEqual(lh_service.correction_code, "10106002")
		self.assertIn("LH", lh_service.service_title)

		self.assertEqual(rh_service.service_position, "RH")
		self.assertEqual(rh_service.correction_code, "10106102")
		self.assertIn("RH", rh_service.service_title)

		# Verify duplicate detection handles sides independently
		# Importing LH again should trigger duplicate warning
		dup_res = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload,
			side="lh",
			allow_generation_mismatch=1,
		)
		self.assertEqual(dup_res["status"], "duplicate_warning")

		# Opposite side duplicate action: duplicate_service_for_opposite_side
		ro_single = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
		res_single = import_tesla_procedure(
			repair_order=ro_single.name,
			data=proc_payload,
			side="lh",
			allow_generation_mismatch=1,
		)
		self.assertEqual(res_single["status"], "success")
		ro_single.reload()
		self.assertEqual(len(ro_single.services), 1)

		# Call duplicate_service_for_opposite_side
		ro_single.duplicate_service_for_opposite_side(ro_single.services[0].name)
		ro_single.reload()
		self.assertEqual(len(ro_single.services), 2)
		self.assertEqual(ro_single.services[1].service_position, "RH")
		self.assertEqual(ro_single.services[1].correction_code, "10106102")
		self.assertIn("RH", ro_single.services[1].service_title)

		# 2. Test importing from an RH baseline procedure with side="both"
		proc_rh_payload = {
			"title": "Upper Control Arm - Front - RH",
			"correction_code": "10106102",
			"rh_correction_code": "10106102",
			"frt": 1.5,
			"is_symmetrical": 1,
			"applicable_model": "Model Y",
			"tesla_manual_url": "https://service.tesla.com/docs/ModelY/ServiceManual/en-us/GUID-UCA-RH.html",
		}
		ro_rh = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
		res_rh = import_tesla_procedure(
			repair_order=ro_rh.name,
			data=proc_rh_payload,
			side="both",
			allow_generation_mismatch=1,
		)
		self.assertEqual(res_rh["status"], "success")
		ro_rh.reload()
		self.assertEqual(len(ro_rh.services), 2)
		self.assertEqual(ro_rh.services[0].service_position, "LH")
		self.assertIn("LH", ro_rh.services[0].service_title)
		self.assertEqual(ro_rh.services[1].service_position, "RH")
		self.assertIn("RH", ro_rh.services[1].service_title)

		# 3. Verify non-symmetrical service duplication is blocked
		ro_non_symm = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
		ro_non_symm.append("services", {
			"service_title": "Standard Oil Change",
			"service_position": "None",
			"status": "Estimating",
			"labor_hours": 1.0,
			"labor_rate": 120.0,
			"labor_amount": 120.0,
			"total_amount": 120.0,
		})
		ro_non_symm.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			ro_non_symm.duplicate_service_for_opposite_side(ro_non_symm.services[0].name)

		# 4. Verify SSRF protection on fetch_and_parse_tesla_procedure
		from ism.api.tesla import fetch_and_parse_tesla_procedure
		with self.assertRaises(frappe.ValidationError):
			fetch_and_parse_tesla_procedure("http://169.254.169.254/latest/meta-data/")
		with self.assertRaises(frappe.ValidationError):
			fetch_and_parse_tesla_procedure("https://malicious-domain.com/hack.html")


