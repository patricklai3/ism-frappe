# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch
import urllib.request

import frappe
from frappe.utils import flt
import ism.api.tesla as tesla_api
from ism.tests.base import ISMTestCase, TEST_PREFIX
from ism.utils.tesla_manual import (
	get_category_for_correction_code,
	parse_tesla_manual_url,
	parse_vehicle_year_month,
	get_tesla_manual_url_for_vehicle,
	validate_vehicle_compatibility,
	parse_tesla_procedure_html,
	normalize_tesla_procedure,
	detect_service_side,
	_MANIFEST_CACHE,
)
from ism.api.tesla import (
	decode_vehicle_vin,
	fetch_and_parse_tesla_procedure,
	get_vehicle_intelligence,
	import_tesla_procedure,
	link_vehicle_customer,
	search_vehicles,
)
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
		self.assertEqual(parsed["suggested_equipment_tags"], ["Lift"])

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
		self.assertEqual(parsed_mobile["suggested_equipment_tags"], [])

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
		self.assertEqual(parsed_my["suggested_equipment_tags"], [])

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
		self.assertEqual(parsed_dom["suggested_equipment_tags"], [])

		# Ensure "Lift" Equipment Tag exists for manifest auto-tagging check
		if not frappe.db.exists("Equipment Tag", "Lift"):
			lift_tag = frappe.get_doc({"doctype": "Equipment Tag", "tag_name": "Lift"}).insert(ignore_permissions=True)
			self.track(lift_tag)

		# Mobile-capable manifest leaves Service Package equipment_tags empty
		pkg_mobile = frappe.new_doc("Service Package")
		pkg_mobile.package_name = f"{TEST_PREFIX}Test Mobile Service"
		pkg_mobile.correction_code = "12201402"
		pkg_mobile.tesla_manual_url = model_y_url
		pkg_mobile.doc_guid = "GUID-2210D20F-E132-4806-8C20-E44630117D89"
		pkg_mobile.validate()
		self.assertEqual(pkg_mobile.get_equipment_tags(), [])

		# Non-mobile manifest auto-adds "Lift" on new Service Package, and preserves manual removal on subsequent save
		non_mobile_url = "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB.html"
		_MANIFEST_CACHE["https://service.tesla.com/docs/Model3/ServiceManual/en-us/dependencies.json"] = [
			{
				"guid": "GUID-AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB",
				"url": "GUID-AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB.html",
				"correction-code": "31019999",
				"mobile-capable": "No",
			}
		]
		pkg_lift = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": f"{TEST_PREFIX}Test Lift Auto Tag",
			"correction_code": "31019999",
			"tesla_manual_url": non_mobile_url,
			"doc_guid": "GUID-AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB",
		}).insert(ignore_permissions=True)
		self.track(pkg_lift)
		self.assertEqual(pkg_lift.get_equipment_tags(), ["Lift"])

		# Manager removes "Lift" and re-saves: must stay empty
		pkg_lift.equipment_tags = []
		pkg_lift.save(ignore_permissions=True)
		pkg_lift.reload()
		self.assertEqual(pkg_lift.get_equipment_tags(), [])
		_MANIFEST_CACHE.clear()

	def test_import_tesla_procedure_end_to_end(self):
		if not frappe.db.exists("Equipment Tag", "Lift"):
			lift_tag = frappe.get_doc({"doctype": "Equipment Tag", "tag_name": "Lift"}).insert(ignore_permissions=True)
			self.track(lift_tag)
		cust = self.create_test_customer()
		veh = self.create_test_vehicle(
			vin="5YJ3E1EBXNF999999",
			customer=cust.name,
			model="Model 3",
			model_year="2022",
			manufactured_month="05/22",
		)
		ro = self.create_test_repair_order(customer=cust.name, vehicle=veh.name)


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
		self.assertEqual(res["required_equipment_tags"], ["Lift"])

		# Verify Service Package was created and properly populated
		pkg_name = res["service_package"]
		pkg = frappe.get_doc("Service Package", pkg_name)
		self.track(pkg)
		self.assertEqual(pkg.correction_code, "00030700")
		self.assertEqual(flt(pkg.frt), 0.90)
		self.assertEqual(flt(pkg.standard_labor_hours), 0.90)
		self.assertEqual(pkg.get_equipment_tags(), ["Lift"])
		self.assertEqual(pkg.package_name, "Basic Vehicle Inspection (Inspection)")
		self.assertFalse(pkg.package_name.startswith("00030700"))

		# Verify Repair Order Service row and RO required_equipment_tags rollup
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
		self.assertEqual(srv.required_equipment_tags, "Lift")
		self.assertEqual(srv.is_supplement, 0)
		self.assertEqual(ro.required_equipment_tags, "Lift")

		default_rate = _get_default_labor_rate()
		expected_labor_amt = flt(0.90 * default_rate, 2)
		self.assertEqual(flt(srv.labor_amount), expected_labor_amt)

		# Default on_conflict="skip" skips existing matching row cleanly without pausing
		res2 = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload
		)
		self.assertEqual(res2["status"], "skipped")
		self.assertEqual(res2["action"], "skipped")
		self.assertNotIn("requires_duplicate_choice", res2)
		ro.reload()
		self.assertEqual(len(ro.services), 1)

		# on_conflict="error" throws DuplicateEntryError
		with self.assertRaises(frappe.DuplicateEntryError):
			import_tesla_procedure(
				repair_order=ro.name,
				data=proc_payload,
				on_conflict="error",
			)

		# Invalid on_conflict value is rejected
		with self.assertRaises(frappe.ValidationError):
			import_tesla_procedure(
				repair_order=ro.name,
				data=proc_payload,
				on_conflict="prompt",
			)

		# Re-importing with on_conflict="overwrite" (or legacy duplicate_action="update") updates existing row
		res_update = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload,
			on_conflict="overwrite",
		)
		self.assertEqual(res_update["status"], "success")
		self.assertEqual(res_update["action"], "updated")
		self.assertEqual(res_update["service_package"], pkg_name)
		ro.reload()
		self.assertEqual(len(ro.services), 1)

		# Re-importing with on_conflict="add" appends an additional row
		res_add = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload,
			on_conflict="add",
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
		from ism.utils.tesla_manual import swap_side_in_title

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

		# Mismatched vehicle model with force=False throws ValidationError (no requires_confirmation pause)
		with self.assertRaises(frappe.ValidationError):
			import_tesla_procedure(
				repair_order=ro.name,
				data=proc_payload,
				side="both",
			)

		# Import with side="both" and force=True succeeds and emits compatibility warning
		res = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload,
			side="both",
			force=True,
		)
		self.assertEqual(res["status"], "success")
		self.assertTrue(res.get("warnings"))
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

		# Verify conflict detection handles sides independently (default skip keeps existing row untouched)
		dup_res = import_tesla_procedure(
			repair_order=ro.name,
			data=proc_payload,
			side="lh",
			force=True,
		)
		self.assertEqual(dup_res["status"], "skipped")
		self.assertEqual(dup_res["action"], "skipped")

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
		with self.assertRaises(frappe.ValidationError):
			fetch_and_parse_tesla_procedure("http://169.254.169.254/latest/meta-data/")
		with self.assertRaises(frappe.ValidationError):
			fetch_and_parse_tesla_procedure("https://malicious-domain.com/hack.html")

	def test_extension_endpoints_removed_and_no_cors_wildcard(self):
		"""Verify deprecated Chrome extension CORS-wildcard endpoints are removed."""
		for removed_fn in ("get_active_repair_orders", "get_session_info", "get_vehicle_manual_url"):
			self.assertFalse(hasattr(tesla_api, removed_fn), f"{removed_fn} should be removed")

		frappe.local.allow_cors = None
		sample_html = (
			'<h1 class="topictitle1"><span class="ph procedure-title">Cabin Filter Replacement</span></h1>'
			'<span class="ph correction-code">18100102</span>'
			'<span class="ph frt">0.40</span>'
			'<table><tr><td>Cabin Air Filter (Qty: 2)</td><td>1107681-00-A</td></tr></table>'
		)
		mock_resp = MagicMock()
		mock_resp.__enter__.return_value = mock_resp
		mock_resp.read.return_value = sample_html.encode("utf-8")

		cust = self.create_test_customer(f"{TEST_PREFIX}CorsCheck")
		veh = self.create_test_vehicle(
			vin="5YJ3E1EBXNF909090",
			customer=cust.name,
			model="Model 3",
			model_year="2022",
			manufactured_month="05/22",
		)
		with patch("ism.api.tesla.urllib.request.urlopen", return_value=mock_resp):
			parsed = fetch_and_parse_tesla_procedure(
				"https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-CABIN-FILT.html",
				vehicle=veh.name,
			)
		self.assertIsNone(getattr(frappe.local, "allow_cors", None))
		self.assertEqual(parsed["title"], "Cabin Filter Replacement")
		self.assertEqual(parsed["correction_code"], "18100102")
		self.assertTrue(parsed["compatibility"]["is_compatible"])
		self.assertEqual(len(parsed["parts"]), 1)
		self.assertEqual(parsed["parts"][0]["part_number"], "1107681-00-A")
		self.assertEqual(parsed["parts"][0]["qty"], 2.0)

	def test_unified_parts_persistence_and_side_filtering(self):
		"""Verify procedure normalization persists parts on Service Package and splits LH/RH parts on RO."""
		normalized = normalize_tesla_procedure({
			"procedure_name": "31011402 - Link - Lower - Front - LH",
			"correction_code": "31011402",
			"rh_correction_code": "31011502",
			"labor_hours": 0.85,
			"model": "Model 3",
			"parts": [
				{"part_number": "1044341-00-D", "part_name": "Front Lower Lateral Link - LH", "qty": 1, "rate": 185.0},
				{"part_number": "1044342-00-D", "part_name": "Front Lower Lateral Link - RH", "qty": 1, "rate": 185.0, "side": "RH"},
				{"part_number": "1044399-00-A", "part_name": "Lateral Link Mounting Bolt", "qty": 2, "rate": 10.0, "side": "Both"},
			],
		}, url="https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-LINK-TEST.html")
		self.assertEqual(normalized["title"], "Link - Lower - Front - LH")
		self.assertEqual(normalized["service_side"], "LH")
		self.assertEqual(detect_service_side(normalized["title"]), "LH")
		self.assertEqual(len(normalized["parts"]), 3)

		cust = self.create_test_customer(f"{TEST_PREFIX}PartsPersist")
		veh = self.create_test_vehicle(
			vin="5YJ3E1EBXNF818181",
			customer=cust.name,
			model="Model 3",
			model_year="2022",
			manufactured_month="05/22",
		)
		ro = self.create_test_repair_order(customer=cust.name, vehicle=veh.name)

		res = import_tesla_procedure(repair_order=ro.name, data=normalized, side="both")
		self.assertEqual(res["status"], "success")
		self.assertEqual(res["count_added"], 2)

		pkg = frappe.get_doc("Service Package", res["service_package"])
		self.track(pkg)
		self.assertEqual(len(pkg.items), 3)

		ro.reload()
		lh_parts = frappe.parse_json(ro.services[0].parts)
		rh_parts = frappe.parse_json(ro.services[1].parts)
		lh_codes = {p["item_code"] for p in lh_parts}
		rh_codes = {p["item_code"] for p in rh_parts}
		self.assertIn("OEM-1044341-00-D-NEW", lh_codes)
		self.assertIn("OEM-1044399-00-A-NEW", lh_codes)
		self.assertNotIn("OEM-1044342-00-D-NEW", lh_codes)
		self.assertIn("OEM-1044342-00-D-NEW", rh_codes)
		self.assertIn("OEM-1044399-00-A-NEW", rh_codes)
		self.assertNotIn("OEM-1044341-00-D-NEW", rh_codes)
		self.assertEqual(flt(ro.services[0].parts_total), 205.0)
		self.assertEqual(flt(ro.services[1].parts_total), 205.0)

	def test_vehicle_intelligence_api_endpoints(self):
		"""Verify offline VIN decoding, multi-customer search, linking, and intelligence summary endpoints."""
		decoded = decode_vehicle_vin("5YJ3E1EBXNF123456")
		self.assertTrue(decoded["is_tesla_wmi"])
		self.assertEqual(decoded["is_valid_vin"], 1)
		self.assertEqual(decoded["model"], "Model 3")

		cust1 = self.create_test_customer(f"{TEST_PREFIX}IntelOwner1")
		cust2 = self.create_test_customer(f"{TEST_PREFIX}IntelOwner2")
		veh = self.create_test_vehicle(
			vin="5YJ3E1EBXNF727272",
			customer=cust1.name,
			license_plate="TESLA-99",
			model="Model 3",
			model_year="2022",
			manufactured_month="05/22",
		)
		veh.append("deferred_services", {
			"job_title": "Brake Fluid Flush",
			"estimated_amount": 180.0,
			"status": "Open Recommendation",
			"safety_hazard": 1,
		})
		veh.save(ignore_permissions=True)

		linked = link_vehicle_customer(vehicle="TESLA-99", customer=cust2.name, relationship="Spouse")
		self.assertEqual(len(linked["owners"]), 2)

		matches = search_vehicles(query="TESLA-99", customer=cust2.name)
		self.assertTrue(any(m["name"] == veh.name for m in matches))

		intel = get_vehicle_intelligence(veh.vin)
		self.assertEqual(intel["vehicle"]["vin"], veh.vin)
		self.assertEqual(len(intel["owners"]), 2)
		self.assertEqual(intel["deferred_services"]["open_count"], 1)
		self.assertEqual(intel["deferred_services"]["safety_hazard_count"], 1)
		self.assertEqual(flt(intel["deferred_services"]["open_estimated_total"]), 180.0)
		self.assertIn("Model3", intel["recommended_manual_url"])

		# Type-confusion guards reject non-string filter-injection payloads
		for fn, kwargs in (
			(fetch_and_parse_tesla_procedure, {"url": {"$ne": ""}}),
			(import_tesla_procedure, {"repair_order": {"!=": ""}, "url": "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-123.html"}),
			(get_vehicle_intelligence, {"vehicle": {"$ne": ""}}),
		):
			with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
				fn(**kwargs)

	def test_tesla_safe_redirect_handler_and_bounded_html(self):
		"""Verify _TeslaSafeRedirectHandler blocks unsafe redirects and _read_bounded_tesla_response caps payload size."""
		handler = tesla_api._TeslaSafeRedirectHandler()
		req = urllib.request.Request("https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-TEST.html")

		for unsafe_target in (
			"https://evil.example.com/payload",
			"http://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-TEST.html",
			"//evil.example.com/redirect",
		):
			with self.assertRaises(frappe.ValidationError):
				handler.redirect_request(req, None, 302, "Found", {}, unsafe_target)

		allowed_req = handler.redirect_request(
			req,
			None,
			302,
			"Found",
			{},
			"https://service.teslamotors.com/docs/Model3/ServiceManual/en-us/GUID-NEXT.html",
		)
		self.assertEqual(
			allowed_req.full_url,
			"https://service.teslamotors.com/docs/Model3/ServiceManual/en-us/GUID-NEXT.html",
		)

		ok_resp = MagicMock()
		ok_resp.geturl.return_value = "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-TEST.html"
		ok_resp.read.return_value = b"<html><body>Procedure OK</body></html>"
		self.assertEqual(tesla_api._read_bounded_tesla_response(ok_resp), "<html><body>Procedure OK</body></html>")

		oversized_resp = MagicMock()
		oversized_resp.geturl.return_value = "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-TEST.html"
		oversized_resp.read.return_value = b"x" * (tesla_api._MAX_TESLA_HTML_BYTES + 1)
		with self.assertRaises(frappe.ValidationError):
			tesla_api._read_bounded_tesla_response(oversized_resp)

	def test_import_tesla_procedure_offline_raw_html_and_markdown(self):
		"""Verify raw_html and raw_markdown import procedures offline without network fetches."""
		cust = self.create_test_customer()
		veh = self.create_test_vehicle(
			vin="5YJ3E1EBXNF998877",
			customer=cust.name,
			model="Model 3",
			model_year="2022",
			manufactured_month="05/22",
		)
		ro = self.create_test_repair_order(customer=cust.name, vehicle=veh.name)

		raw_html = """
		<html>
			<head><title>Upper Control Arm - Front (Remove and Replace)</title></head>
			<body>
				<h1>Upper Control Arm - Front - LH (Remove and Replace)</h1>
				<div>Correction Code: 31011402</div>
				<div>FRT: 1.25</div>
				<p>Replace bolt 1044321-00-G if removed.</p>
			</body>
		</html>
		"""
		with patch("ism.api.tesla._fetch_tesla_html", side_effect=AssertionError("Network fetch must not run")):
			res_html = import_tesla_procedure(
				repair_order=ro.name,
				raw_html=raw_html,
			)
		self.assertEqual(res_html["status"], "success")
		self.assertEqual(res_html["correction_code"], "31011402")
		self.assertEqual(flt(res_html["frt"]), 1.25)
		pkg_html = frappe.get_doc("Service Package", res_html["service_package"])
		self.track(pkg_html)

		# Markdown import with optional source URL metadata (still no network fetch)
		raw_md = """# Cabin Air Filter (Remove and Replace)
Correction Code: 18100102
FRT: 0.45
Parts: 1107681-00-A
"""
		optional_url = "https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-CABIN-FILTER.html"
		with patch("ism.api.tesla._fetch_tesla_html", side_effect=AssertionError("Network fetch must not run")):
			res_md = import_tesla_procedure(
				repair_order=ro.name,
				url=optional_url,
				raw_markdown=raw_md,
			)
		self.assertEqual(res_md["status"], "success")
		self.assertEqual(res_md["correction_code"], "18100102")
		self.assertEqual(flt(res_md["frt"]), 0.45)
		pkg_md = frappe.get_doc("Service Package", res_md["service_package"])
		self.track(pkg_md)
		self.assertEqual(pkg_md.tesla_manual_url, optional_url)

		ro.reload()
		self.assertEqual(len(ro.services), 2)

		# Validation guards: non-string, oversized payload, and invalid optional URL
		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			import_tesla_procedure(repair_order=ro.name, raw_html=["not-a-str"])
		with self.assertRaises(frappe.ValidationError):
			import_tesla_procedure(repair_order=ro.name, raw_markdown="x" * (tesla_api._MAX_TESLA_HTML_BYTES + 1))
		with self.assertRaises(frappe.ValidationError):
			import_tesla_procedure(
				repair_order=ro.name,
				url="https://evil.example.com/manual.html",
				raw_markdown=raw_md,
			)

	def test_generation_mapping_disambiguation_and_scoped_package_creation(self):
		"""Verify fixed Model X Palladium URL, full variant bounds, generation ambiguity, and non-overwriting imports."""
		from ism.utils.tesla.catalog import (
			get_generation_catalog,
			resolve_vehicle_generation,
		)

		# 1. Model X Palladium fixed URL & multi-gen range spans
		mx_pal_url = "https://service.tesla.com/docs/ModelX/ServiceManual/Palladium/en-us/GUID-MX-PAL.html"
		parsed_mx = parse_tesla_manual_url(mx_pal_url)
		self.assertEqual(parsed_mx["applicable_model"], "Model X")
		self.assertEqual(parsed_mx["matched_generation"], "Model X Mar 2021 - May 2025")
		self.assertEqual(parsed_mx["applicable_generation"], "Model X Mar 2021 - May 2025, Model X June 2025")
		self.assertEqual(parsed_mx["gen_start"], "2021-03")
		self.assertIsNone(parsed_mx["gen_end"])

		ms_classic = parse_tesla_manual_url("https://service.tesla.com/docs/ModelS/ServiceManual/en-us/GUID-MS-CL.html")
		self.assertEqual(ms_classic["gen_start"], "2012-02")
		self.assertEqual(ms_classic["gen_end"], "2021-01")
		self.assertEqual(len(ms_classic["generation_entries"]), 2)

		mx_pal_details = resolve_vehicle_generation({"model": "Model X", "model_year": 2022, "manufactured_month": "04/22"})
		self.assertEqual(mx_pal_details["vehicle_generation"], "Model X Mar 2021 - May 2025")
		self.assertEqual(
			mx_pal_details["recommended_manual_url"],
			"https://service.tesla.com/docs/ModelX/ServiceManual/Palladium/en-us/",
		)
		self.assertFalse(mx_pal_details["generation_ambiguous"])

		# 2. Split transition year without manufactured_month flags ambiguity
		ms_2021_ambig = resolve_vehicle_generation({"model": "Model S", "model_year": 2021})
		self.assertTrue(ms_2021_ambig["generation_ambiguous"])
		self.assertEqual(
			[e["generation_name"] for e in ms_2021_ambig["candidate_generations"]],
			["Model S Apr 2016 - Jan 2021", "Model S Feb 2021 - May 2025"],
		)
		ms_2021_jan = resolve_vehicle_generation({"model": "Model S", "model_year": 2021, "manufactured_month": "01/21"})
		self.assertFalse(ms_2021_jan["generation_ambiguous"])
		self.assertEqual(ms_2021_jan["vehicle_generation"], "Model S Apr 2016 - Jan 2021")

		cat = get_generation_catalog("Model 3")
		self.assertEqual([c["generation_name"] for c in cat], ["Model 3 Jun 2017 - Dec 2023", "Model 3 Jan 2024"])

		# 3. Importing same procedure/correction code across two distinct generations creates separate Service Packages
		cust = self.create_test_customer()
		v_old = self.create_test_vehicle(
			vin="5YJ3E1EA1KF100001",
			customer=cust.name,
			model="Model 3",
			model_year="2019",
			manufactured_month="06/19",
		)
		v_new = self.create_test_vehicle(
			vin="5YJ3E1EA1RF200002",
			customer=cust.name,
			model="Model 3",
			model_year="2024",
			manufactured_month="03/24",
		)
		ro_old = self.create_test_repair_order(customer=cust.name, vehicle=v_old.name)
		ro_new = self.create_test_repair_order(customer=cust.name, vehicle=v_new.name)

		with patch("ism.api.tesla._fetch_tesla_html", side_effect=AssertionError("Network fetch must not run")):
			r1 = import_tesla_procedure(
				repair_order=ro_old.name,
				url="https://service.tesla.com/docs/Model3/ServiceManual/en-us/GUID-GEN-OLD.html",
				raw_markdown="# Shared Cabin Filter\nCorrection Code: 18109901\nFRT: 0.4\n",
			)
			r2 = import_tesla_procedure(
				repair_order=ro_new.name,
				url="https://service.tesla.com/docs/Model3/ServiceManual/2024/en-us/GUID-GEN-NEW.html",
				raw_markdown="# Shared Cabin Filter\nCorrection Code: 18109901\nFRT: 0.6\n",
			)

		self.assertNotEqual(r1["service_package"], r2["service_package"])
		self.track("Service Package", r1["service_package"])
		self.track("Service Package", r2["service_package"])

		pkg1 = frappe.get_doc("Service Package", r1["service_package"])
		pkg2 = frappe.get_doc("Service Package", r2["service_package"])
		self.assertEqual(pkg1.applicable_generation, "Model 3 Jun 2017 - Dec 2023")
		self.assertEqual(pkg2.applicable_generation, "Model 3 Jan 2024")
		self.assertEqual([row.generation_name for row in pkg1.generation_compatibility], ["Model 3 Jun 2017 - Dec 2023"])
		self.assertEqual([row.generation_name for row in pkg2.generation_compatibility], ["Model 3 Jan 2024"])

		# 4. Link query filters compatible generation cleanly
		rows_for_new = tesla_api.query_service_packages_for_vehicle(
			doctype="Service Package",
			txt="Shared Cabin Filter",
			searchfield="name",
			start=0,
			page_len=20,
			filters={"vehicle": v_new.name},
		)
		names_for_new = [r[0] for r in rows_for_new]
		self.assertIn(pkg2.name, names_for_new)
		self.assertNotIn(pkg1.name, names_for_new)



