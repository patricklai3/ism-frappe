# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import frappe
from frappe.core.doctype.user.user import generate_keys
from frappe.mcp.test_utils import McpTestCase
from frappe.utils import today
from ism.tests.test_fixtures import (
	setup_all,
	teardown_transactional,
	CUSTOMER,
	VEHICLE_VIN,
)


class TestMcpProtocol(McpTestCase):
	"""Integration tests validating Frappe v16 native MCP (/api/v2/mcp) support for ISM Agent Facade."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		setup_all()

		# Grant permissions to default test user role (MCP Desk Tester) for Repair Order operations
		for dt, read, write, create in [
			("Repair Order", 1, 1, 1),
			("Schedule Entry", 1, 1, 1),
			("Customer", 1, 1, 1),
			("Repair Vehicle", 1, 1, 1),
			("Item", 1, 0, 0),
			("Service Package", 1, 0, 0),
			("ISM Settings", 1, 0, 0),
		]:
			if not frappe.db.exists("Custom DocPerm", {"parent": dt, "role": "MCP Desk Tester"}):
				frappe.get_doc({
					"doctype": "Custom DocPerm",
					"parent": dt,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": "MCP Desk Tester",
					"read": read,
					"write": write,
					"create": create,
				}).insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

	def delete_user(self, user):
		"""Resilient delete_user override with retry to prevent MariaDB tabDocShare deadlocks."""
		frappe.set_user("Administrator")
		for attempt in range(3):
			try:
				frappe.db.rollback()
				frappe.delete_doc_if_exists("User", user, force=True, ignore_permissions=True)
				frappe.db.commit()  # nosemgrep
				break
			except Exception:
				frappe.db.rollback()
				if attempt == 2:
					pass

	def tearDown(self):
		try:
			frappe.set_user("Administrator")
			teardown_transactional()
		finally:
			super().tearDown()

	def invoke(self, *, tool="call_method", auth_header=None, **arguments):
		"""Helper to dispatch JSON-RPC 2.0 tools/call to /api/v2/mcp."""
		headers = {}
		if auth_header:
			headers["Authorization"] = auth_header
		return self.request(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "tools/call",
				"params": {"name": tool, "arguments": arguments},
			},
			headers=headers,
		)

	def result(self, **arguments):
		"""Helper expecting a successful tool execution returning structuredContent."""
		response = self.invoke(**arguments)
		self.assertEqual(response.status_code, 200, f"HTTP {response.status_code}: {response.data}")
		res = response.json.get("result", {})
		self.assertFalse(res.get("isError", False), f"Tool execution failed: {res}")
		return res.get("structuredContent", {})

	def test_mcp_tools_list(self):
		"""Verify standard Frappe v16 MCP tools are discoverable via tools/list."""
		response = self.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
		self.assertEqual(response.status_code, 200)
		tools = response.json.get("result", {}).get("tools", [])
		tool_names = [t.get("name") for t in tools]

		self.assertIn("call_method", tool_names)
		self.assertIn("call_document_method", tool_names)
		self.assertIn("search_methods", tool_names)
		self.assertIn("get_method_schema", tool_names)
		self.assertIn("list_documents", tool_names)

	def test_mcp_search_agent_methods(self):
		"""Verify that ism.api.agent methods can be searched via search_methods."""
		if not frappe.cache.get_value("frappe:api_discovery:python_methods", expires=True):
			from frappe.api import discovery
			discovery.build_cache()

		response = self.invoke(tool="search_methods", query="ism.api.agent")
		self.assertEqual(response.status_code, 200)
		res = response.json.get("result", {})
		self.assertFalse(res.get("isError", False))

		results = res.get("structuredContent", {}).get("result", [])
		method_names = [r.get("name") or r.get("path") for r in results]
		# At least some agent methods should match
		self.assertTrue(any("ism.api.agent" in str(m) for m in method_names))

	def test_mcp_call_create_quote(self):
		"""Verify ism.api.agent.create_quote via MCP call_method."""
		res = self.result(
			method="ism.api.agent.create_quote",
			args={
				"customer": CUSTOMER,
				"vehicle": VEHICLE_VIN,
				"concern": "Loud clicking from front suspension over bumps",
			},
		)
		data = res.get("result", {})
		self.assertTrue(data.get("success"), f"create_quote returned: {data}")
		ro_name = data.get("repair_order") or data.get("quote_id")
		self.assertIsNotNone(ro_name)
		self.assertTrue(ro_name.startswith("RO-"))

		# Verify Markdown envelope and hyperlinks
		summary = data.get("summary_markdown", "")
		self.assertIn("Repair Order Quote", summary)
		self.assertIn("Est. Shop Time", summary)

		urls = data.get("urls", {})
		self.assertIn("repair_order", urls)
		self.assertIn(ro_name, urls["repair_order"])
		self.assertIn("printview", urls.get("print_view", ""))

	def test_mcp_call_amend_quote(self):
		"""Verify ism.api.agent.amend_quote via MCP call_method."""
		# Create quote
		res1 = self.result(
			method="ism.api.agent.create_quote",
			args={"customer": CUSTOMER, "vehicle": VEHICLE_VIN, "concern": "Diagnostic needed"},
		)
		ro_name = res1["result"]["repair_order"]

		# Amend quote
		res2 = self.result(
			method="ism.api.agent.amend_quote",
			args={
				"repair_order": ro_name,
				"add_services": [
					{
						"service_title": "Front Brake Replacement",
						"labor_hours": 1.5,
						"labor_rate": 140.0,
					}
				],
			},
		)
		data = res2.get("result", {})
		self.assertTrue(data.get("success"))
		services_count = data.get("total_services") or len(data.get("services", [])) or len(data.get("data", {}).get("services", []))
		self.assertGreaterEqual(services_count, 2)

	def test_mcp_call_find_available_slots(self):
		"""Verify ism.api.agent.find_available_slots via MCP call_method."""
		res = self.result(
			method="ism.api.agent.find_available_slots",
			args={"target_date": today(), "duration_minutes": 60},
		)
		data = res.get("result", {})
		self.assertTrue(data.get("success"))
		self.assertIsInstance(data.get("slots"), list)

	def test_mcp_call_get_shop_floor_status(self):
		"""Verify ism.api.agent.get_shop_floor_status via MCP call_method."""
		res = self.result(
			method="ism.api.agent.get_shop_floor_status",
			args={},
		)
		data = res.get("result", {})
		self.assertTrue(data.get("success"))
		summary = data.get("summary", {})
		self.assertIn("active_bays", summary)
		self.assertIn("active_technicians", summary)
		self.assertIn("active_repair_orders", summary)

	def test_mcp_call_send_quote_for_approval(self):
		"""Verify ism.api.agent.send_quote_for_approval via MCP call_method."""
		res1 = self.result(
			method="ism.api.agent.create_quote",
			args={"customer": CUSTOMER, "vehicle": VEHICLE_VIN, "concern": "Approval test"},
		)
		ro_name = res1["result"]["repair_order"]

		res2 = self.result(
			method="ism.api.agent.send_quote_for_approval",
			args={"repair_order": ro_name, "communication_medium": "sms"},
		)
		data = res2.get("result", {})
		self.assertTrue(data.get("success"))
		self.assertIsNotNone(data.get("token"))
		urls = data.get("urls", {})
		self.assertIn("approval", urls)
		self.assertIn(f"/quote-approval?token={data['token']}", urls["approval"])

	def test_mcp_call_update_job_status_guard(self):
		"""Verify update_job_status blocks work when authorization is not granted."""
		res1 = self.result(
			method="ism.api.agent.create_quote",
			args={"customer": CUSTOMER, "vehicle": VEHICLE_VIN, "concern": "Guard test"},
		)
		ro_name = res1["result"]["repair_order"]

		# Starting work on unapproved quote should fail
		response = self.invoke(
			tool="call_method",
			method="ism.api.agent.update_job_status",
			args={"repair_order": ro_name, "action": "start"},
		)
		res = response.json.get("result", {})
		# It should either be an MCP error or return success=False with clear error
		if res.get("isError"):
			self.assertTrue(res.get("isError"))
		else:
			structured = res.get("structuredContent", {}).get("result", {})
			self.assertFalse(structured.get("success", False))

	def test_mcp_rbac_dedicated_service_advisor_user(self):
		"""Verify dedicated Service Advisor user has execution permissions for Agent Facade."""
		# 1. Ensure Service Advisor role exists with Desk Access
		if not frappe.db.exists("Role", "Service Advisor"):
			frappe.get_doc(
				doctype="Role",
				role_name="Service Advisor",
				desk_access=1,
			).insert(ignore_permissions=True)

		# 2. Grant permissions on required DocTypes to Service Advisor
		for dt, read, write, create in [
			("Repair Order", 1, 1, 1),
			("Schedule Entry", 1, 1, 1),
			("Customer", 1, 1, 1),
			("Repair Vehicle", 1, 1, 1),
			("Item", 1, 0, 0),
			("Service Package", 1, 0, 0),
			("ISM Settings", 1, 0, 0),
		]:
			if not frappe.db.exists("Custom DocPerm", {"parent": dt, "role": "Service Advisor"}):
				frappe.get_doc({
					"doctype": "Custom DocPerm",
					"parent": dt,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": "Service Advisor",
					"read": read,
					"write": write,
					"create": create,
				}).insert(ignore_permissions=True)

		# 3. Create dedicated agent user
		user_email = f"shop_agent_{frappe.generate_hash(length=6).lower()}@example.com"
		agent_user = frappe.get_doc(
			doctype="User",
			email=user_email,
			first_name="Shop",
			last_name="Agent",
			send_welcome_email=0,
			roles=[{"role": "Service Advisor"}],
		).insert(ignore_permissions=True)
		self.addCleanup(self.delete_user, user_email)

		keys = generate_keys(user_email)
		auth_header = f"token {keys['api_key']}:{keys['api_secret']}"
		frappe.db.commit()

		# 4. Service Advisor can call create_quote via MCP
		response = self.invoke(
			tool="call_method",
			auth_header=auth_header,
			method="ism.api.agent.create_quote",
			args={
				"customer": CUSTOMER,
				"vehicle": VEHICLE_VIN,
				"concern": "Created by dedicated shop agent user",
			},
		)
		self.assertEqual(response.status_code, 200)
		res = response.json.get("result", {})
		self.assertFalse(res.get("isError", False), f"Agent user call failed: {res}")
		data = res.get("structuredContent", {}).get("result", {})
		self.assertTrue(data.get("success"))

		# 5. Service Advisor is strictly blocked from System Manager operations (e.g. System Settings modification)
		forbidden_response = self.invoke(
			tool="create_document",
			auth_header=auth_header,
			doctype="System Settings",
			data={},
		)
		self.assertEqual(forbidden_response.status_code, 200)
		f_res = forbidden_response.json.get("result", {})
		# Must result in an error
		self.assertTrue(f_res.get("isError", False), "Service Advisor should not be allowed to modify System Settings")

	def test_mcp_unauthenticated_request_rejected(self):
		"""Verify unauthenticated MCP requests are strictly rejected with 401."""
		response = self.request(
			{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
			auth=False,
		)
		self.assertIn(response.status_code, [401, 403])
