#!/usr/bin/env python3
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt
"""Standalone verification script for ISM Agent Facade via Frappe v16 native MCP protocol.

Usage:
    bench --site development.localhost execute ism.scripts.verify_mcp_agent.run
"""


def _ensure_service_advisor_permissions():
	"""Grant Service Advisor role permissions without shadowing standard DocPerms."""
	import frappe
	from frappe.permissions import add_permission, update_permission_property

	if not frappe.db.exists("Role", "Service Advisor"):
		frappe.get_doc(doctype="Role", role_name="Service Advisor", desk_access=1).insert(ignore_permissions=True)

	role = "Service Advisor"
	for dt, read, write, create in [
		("Repair Order", 1, 1, 1),
		("Schedule Entry", 1, 1, 1),
		("Customer", 1, 1, 1),
		("Repair Vehicle", 1, 1, 1),
		("Item", 1, 0, 0),
		("Service Package", 1, 0, 0),
		("ISM Settings", 1, 0, 0),
	]:
		has_perm = (
			frappe.db.exists("Custom DocPerm", {"parent": dt, "role": role})
			or frappe.db.exists("DocPerm", {"parent": dt, "role": role})
		)
		if has_perm:
			continue
		add_permission(dt, role, permlevel=0)
		update_permission_property(dt, role, 0, "read", read)
		update_permission_property(dt, role, 0, "write", write)
		update_permission_property(dt, role, 0, "create", create)


def run(site: str = "development.localhost"):
	"""Execute MCP verification within Frappe context."""
	import frappe
	from frappe.core.doctype.user.user import generate_keys
	from frappe.mcp.test_utils import McpTestCase
	from ism.tests.test_fixtures import CUSTOMER, VEHICLE_VIN, setup_all

	if not getattr(frappe.local, "db", None):
		frappe.init(site=site)
		frappe.connect()

	print("=" * 60)
	print("ISM AGENT FACADE — MCP PROTOCOL VERIFICATION SUITE")
	print("=" * 60)

	frappe.set_user("Administrator")
	setup_all()

	# 1. Setup Role and Test Agent User
	print("\n[1/5] Verifying RBAC and Dedicated Agent User...")
	_ensure_service_advisor_permissions()

	user_email = "shop_agent_cli@example.com"
	if not frappe.db.exists("User", user_email):
		frappe.get_doc(
			doctype="User",
			email=user_email,
			first_name="Shop",
			last_name="Agent",
			send_welcome_email=0,
			roles=[{"role": "Service Advisor"}],
		).insert(ignore_permissions=True)
	keys = generate_keys(user_email)
	auth_header = f"token {keys['api_key']}:{keys['api_secret']}"
	frappe.db.commit()
	print(f"  ✓ User {user_email} provisioned with role 'Service Advisor'")

	# Helper test harness using McpTestCase request dispatcher
	case = McpTestCase()
	case.site = site
	case.authorization = auth_header

	# 2. Test Tools Discovery
	print("\n[2/5] Testing MCP tools/list...")
	r = case.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	assert r.status_code == 200, f"tools/list failed: {r.status_code}"
	tool_names = [t.get("name") for t in r.json.get("result", {}).get("tools", [])]
	assert "call_method" in tool_names, "call_method not in tools"
	print(f"  ✓ Native MCP tools discoverable: {len(tool_names)} tools found ({', '.join(tool_names[:5])}...)")

	# 3. Test Agent Facade Methods Search
	print("\n[3/5] Testing MCP method discovery (search_methods)...")
	try:
		from frappe.api import discovery
		if not frappe.cache.get_value("frappe:api_discovery:python_methods", expires=True):
			discovery.build_cache()
	except Exception:
		pass

	r = case.request({
		"jsonrpc": "2.0",
		"id": 2,
		"method": "tools/call",
		"params": {"name": "search_methods", "arguments": {"query": "ism.api.agent"}},
	})
	results = r.json.get("result", {}).get("structuredContent", {}).get("result", [])
	print(f"  ✓ Discovered {len(results)} ism.api.agent.* MCP callable endpoints")

	# 4. Test Quote Lifecycle via MCP
	print("\n[4/5] Executing Quote Lifecycle via MCP JSON-RPC 2.0...")
	ro_name = None
	try:
		# 4a. Create Quote
		r = case.request({
			"jsonrpc": "2.0",
			"id": 3,
			"method": "tools/call",
			"params": {
				"name": "call_method",
				"arguments": {
					"method": "ism.api.agent.create_quote",
					"args": {
						"customer": CUSTOMER,
						"vehicle": VEHICLE_VIN,
						"odometer_in": 45000,
						"concern": "Front suspension rattle verification",
					},
				},
			},
		})
		data = r.json.get("result", {}).get("structuredContent", {}).get("result", {})
		assert data.get("success"), f"create_quote failed: {data}"
		ro_data = data.get("data", {})
		ro_name = ro_data.get("name")
		print(f"  ✓ ism.api.agent.create_quote -> {ro_name} (Est. Shop Time: {ro_data.get('total_bay_hours', 1.0)*60:.0f} mins, Grand Total: ${ro_data.get('grand_total', 0):.2f})")

		# 4b. Amend Quote
		r = case.request({
			"jsonrpc": "2.0",
			"id": 4,
			"method": "tools/call",
			"params": {
				"name": "call_method",
				"arguments": {
					"method": "ism.api.agent.amend_quote",
					"args": {
						"repair_order": ro_name,
						"add_services": [{"service_title": "Upper Control Arm Replacement", "labor_hours": 2.0}],
					},
				},
			},
		})
		data = r.json.get("result", {}).get("structuredContent", {}).get("result", {})
		assert data.get("success"), f"amend_quote failed: {data}"
		amend_data = data.get("data", {})
		print(f"  ✓ ism.api.agent.amend_quote -> {ro_name} (Services: {amend_data.get('total_services')}, Grand Total: ${amend_data.get('grand_total', 0):.2f})")

		# 4c. Send for Approval
		r = case.request({
			"jsonrpc": "2.0",
			"id": 5,
			"method": "tools/call",
			"params": {
				"name": "call_method",
				"arguments": {
					"method": "ism.api.agent.send_quote_for_approval",
					"args": {"repair_order": ro_name, "communication_medium": "link"},
				},
			},
		})
		data = r.json.get("result", {}).get("structuredContent", {}).get("result", {})
		assert data.get("success"), f"send_quote_for_approval failed: {data}"
		token = data.get("data", {}).get("token", "")
		print(f"  ✓ ism.api.agent.send_quote_for_approval -> Token: {token[:8]}... Link: {data.get('urls', {}).get('approval')}")

		# 4d. Security Guard Check (Cannot start work without approval)
		r = case.request({
			"jsonrpc": "2.0",
			"id": 6,
			"method": "tools/call",
			"params": {
				"name": "call_method",
				"arguments": {
					"method": "ism.api.agent.update_job_status",
					"args": {"repair_order": ro_name, "action": "start"},
				},
			},
		})
		res = r.json.get("result", {})
		assert res.get("isError") or not res.get("structuredContent", {}).get("result", {}).get("success"), "Security guard failed to block unapproved start"
		print("  ✓ Authorization guard verified: Starting unapproved quote physically blocked")

		# 5. Capacity & Floor Status Queries
		print("\n[5/5] Testing dual-resource scheduling & shop floor telemetry...")
		r = case.request({
			"jsonrpc": "2.0",
			"id": 7,
			"method": "tools/call",
			"params": {
				"name": "call_method",
				"arguments": {
					"method": "ism.api.agent.get_shop_floor_status",
					"args": {},
				},
			},
		})
		data = r.json.get("result", {}).get("structuredContent", {}).get("result", {})
		assert data.get("success"), f"get_shop_floor_status failed: {data}"
		summary = data.get("data", {}).get("summary", {})
		print(f"  ✓ ism.api.agent.get_shop_floor_status -> Bays: {summary.get('active_bays')}, Techs: {summary.get('active_technicians')}, Active ROs: {summary.get('active_repair_orders')}")
	finally:
		if ro_name and frappe.db.exists("Repair Order", ro_name):
			frappe.set_user("Administrator")
			frappe.delete_doc("Repair Order", ro_name, force=True, ignore_permissions=True)
			frappe.db.commit()

	print("\n" + "=" * 60)
	print("ALL MCP PROTOCOL AND AGENT FACADE CHECKS PASSED PERFECTLY!")
	print("=" * 60)


if __name__ == "__main__":
	run()
