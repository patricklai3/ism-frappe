# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.api import discovery
from frappe.cache_manager import clear_user_cache
from frappe.core.doctype.user.user import generate_keys
from frappe.mcp.test_utils import McpTestCase
from frappe.permissions import setup_custom_perms
from frappe.utils import today

from ism.tests.base import ISMTestCase


_MCP_REQUIRED_PERMS = (
	("Repair Order", 1, 1, 1),
	("Schedule Entry", 1, 1, 1),
	("Service Bay", 1, 0, 0),
	("Inspection Template", 1, 0, 0),
	("Customer", 1, 1, 1),
	("Repair Vehicle", 1, 1, 1),
	("Item", 1, 0, 0),
	("Service Package", 1, 0, 0),
	("ISM Settings", 1, 0, 0),
	("Shop Action Item", 1, 1, 1),
)


class TestMcpProtocol(McpTestCase, ISMTestCase):
	"""Integration tests validating Frappe v16 native MCP (/api/v2/mcp) support for ISM Agent Facade."""

	@classmethod
	def _grant_role_permissions_into(
		cls, role: str, restore_actions: list, tracked_doctypes: set
	) -> None:
		"""Grant role permissions in Custom DocPerm without shadowing standard DocPerms."""
		for dt, read, write, create in _MCP_REQUIRED_PERMS:
			if (
				frappe.db.exists("Custom DocPerm", {"parent": dt})
				and frappe.db.exists("DocPerm", {"parent": dt, "role": "System Manager"})
				and not frappe.db.exists("Custom DocPerm", {"parent": dt, "role": "System Manager"})
			):
				frappe.db.delete("Custom DocPerm", {"parent": dt})
				frappe.clear_cache(doctype=dt)

			had_custom = bool(frappe.db.exists("Custom DocPerm", {"parent": dt}))
			std_perm = frappe.db.get_value(
				"DocPerm",
				{"parent": dt, "role": role, "permlevel": 0},
				["read", "write", "create"],
				as_dict=True,
			)
			if (
				not had_custom
				and std_perm
				and (
					int(std_perm.read or 0) >= read
					and int(std_perm.write or 0) >= write
					and int(std_perm.create or 0) >= create
				)
			):
				continue

			if not had_custom and dt not in tracked_doctypes:
				tracked_doctypes.add(dt)
				restore_actions.append(("delete_all", dt, None, None))
				setup_custom_perms(dt)
			else:
				prev = frappe.db.get_value(
					"Custom DocPerm",
					{"parent": dt, "role": role, "permlevel": 0, "if_owner": 0},
					["read", "write", "create"],
					as_dict=True,
				)
				if prev:
					restore_actions.append(
						(
							"restore",
							dt,
							role,
							(int(prev.read or 0), int(prev.write or 0), int(prev.create or 0)),
						)
					)
				else:
					restore_actions.append(("delete_role", dt, role, None))

			existing = frappe.db.get_value(
				"Custom DocPerm",
				{"parent": dt, "role": role, "permlevel": 0, "if_owner": 0},
				"name",
			)
			if existing:
				frappe.db.set_value(
					"Custom DocPerm",
					existing,
					{"read": read, "write": write, "create": create},
					update_modified=False,
				)
			else:
				frappe.get_doc(
					{
						"doctype": "Custom DocPerm",
						"parent": dt,
						"parenttype": "DocType",
						"parentfield": "permissions",
						"role": role,
						"permlevel": 0,
						"if_owner": 0,
						"read": read,
						"write": write,
						"create": create,
					}
				).insert(ignore_permissions=True)
			frappe.clear_cache(doctype=dt)

		clear_user_cache()

	@classmethod
	def _restore_role_permissions_from(cls, restore_actions: list, tracked_doctypes: set) -> None:
		"""Clear permission caches for any DocTypes touched during a test."""
		if not restore_actions:
			return
		touched_doctypes = {dt for _, dt, _, _ in restore_actions}
		for dt in touched_doctypes:
			frappe.clear_cache(doctype=dt)
		clear_user_cache()
		restore_actions.clear()
		tracked_doctypes.clear()

	def _grant_role_permissions(self, role: str) -> None:
		"""Grant role permissions via Frappe permission API without shadowing standard DocPerms."""
		if not hasattr(self, "_perm_restore_actions"):
			self._perm_restore_actions = []
			self._tracked_custom_perm_doctypes = set()
		self._grant_role_permissions_into(
			role,
			self._perm_restore_actions,
			self._tracked_custom_perm_doctypes,
		)

	def _restore_role_permissions(self) -> None:
		"""Clear DocType caches after per-test rollback."""
		actions = getattr(self, "_perm_restore_actions", None) or []
		if actions:
			self._restore_role_permissions_from(
				actions,
				getattr(self, "_tracked_custom_perm_doctypes", set()),
			)

	def create_user(self, first_name):
		"""Provision an in-transaction MCP user with canonical ISM Manager permissions."""
		mcp_user = f"mcp.{frappe.generate_hash(length=10).lower()}@example.com"
		user_doc = frappe.get_doc(
			doctype="User",
			email=mcp_user,
			first_name=first_name,
			last_name="Tester",
			enabled=1,
			user_type="System User",
			send_welcome_email=0,
			roles=[
				{"role": "ISM Manager"},
				{"role": "Sales User"},
				{"role": "Maintenance User"},
				{"role": "Stock User"},
				{"role": "Accounts User"},
			],
		)
		user_doc.flags.ignore_password_policy = True
		user_doc.insert(ignore_permissions=True)
		keys = generate_keys(mcp_user)
		auth_header = f"token {keys['api_key']}:{keys['api_secret']}"
		return mcp_user, auth_header

	def delete_user(self, user):
		"""No-op delete when running inside per-test transaction rollback."""
		return None

	def setUp(self):
		frappe.db.rollback()
		self._tracked_docs = []
		self._perm_restore_actions = []
		self._tracked_custom_perm_doctypes = set()
		super().setUp()
		frappe.set_user("Administrator")
		self.manifest = self.get_manifest()
		self.customer = self.manifest.resolve("customers.retail_1")
		self.vehicle_vin = self.manifest.resolve("vehicles.veh_model3_liam")

	def tearDown(self):
		try:
			frappe.set_user("Administrator")
			super().tearDown()
		finally:
			try:
				self._restore_role_permissions()
			finally:
				self.cleanup_tracked_docs()
				frappe.db.rollback()

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
		if not frappe.cache.get_value(discovery.PYTHON_SEARCH_CACHE_KEY):
			with (
				patch.object(discovery.frappe, "get_installed_apps", return_value=["ism"]),
				patch.object(discovery, "_discover_doctype_methods", return_value=[]),
			):
				discovery.build_cache()
			self.addCleanup(discovery.clear_cache)

		response = self.invoke(tool="search_methods", query="ism.api.agent")
		self.assertEqual(response.status_code, 200)
		res = response.json.get("result", {})
		self.assertFalse(res.get("isError", False))

		results = res.get("structuredContent", {}).get("result", [])
		method_names = [r.get("name") or r.get("path") for r in results]
		# At least some agent methods should match
		self.assertTrue(any("ism.api.agent" in str(m) for m in method_names))

	def test_mcp_call_create_quote(self):
		"""Verify ism.api.agent.create_quote via MCP call_method returns compact envelope by default."""
		res = self.result(
			method="ism.api.agent.create_quote",
			args={
				"customer": self.customer,
				"vehicle": self.vehicle_vin,
				"odometer_in": 45000,
				"concern": "Loud clicking from front suspension over bumps",
			},
		)
		envelope = res.get("result", {})
		self.assertTrue(envelope.get("success"), f"create_quote returned: {envelope}")
		self.assertNotIn("summary_card", envelope)
		self.assertNotIn("summary_markdown", envelope)

		ro_data = envelope.get("data", {})
		ro_name = ro_data.get("name")
		self.assertIsNotNone(ro_name)
		self.assertTrue(ro_name.startswith("RO-"))
		self.assertNotIn("parts_html", ro_data["services"][0])

		urls = envelope.get("urls", {})
		self.assertIn("desk", urls)
		self.assertIn(ro_name, urls["desk"])
		self.assertIn("printview", urls.get("print", ""))

		# Verify opt-in summary_card works over MCP
		res_summary = self.result(
			method="ism.api.agent.create_quote",
			args={
				"customer": self.customer,
				"vehicle": self.vehicle_vin,
				"odometer_in": 45000,
				"concern": "Summary opt-in check",
				"include_summary": True,
			},
		)
		self.assertIn("Repair Order Quote", res_summary["result"].get("summary_card", ""))

	def test_mcp_call_amend_quote(self):
		"""Verify ism.api.agent.amend_quote via MCP call_method."""
		res1 = self.result(
			method="ism.api.agent.create_quote",
			args={"customer": self.customer, "vehicle": self.vehicle_vin, "odometer_in": 45000, "concern": "Diagnostic needed"},
		)
		ro_name = res1["result"]["data"]["name"]

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
		envelope = res2.get("result", {})
		self.assertTrue(envelope.get("success"))
		services_count = envelope.get("data", {}).get("total_services", 0)
		self.assertGreaterEqual(services_count, 2)

	def test_mcp_call_find_available_slots(self):
		"""Verify ism.api.agent.find_available_slots via MCP call_method."""
		res = self.result(
			method="ism.api.agent.find_available_slots",
			args={"target_date": today(), "duration_minutes": 60},
		)
		envelope = res.get("result", {})
		self.assertTrue(envelope.get("success"))
		self.assertIsInstance(envelope.get("data", {}).get("slots"), list)

	def test_mcp_call_get_shop_floor_status(self):
		"""Verify ism.api.agent.get_shop_floor_status via MCP call_method."""
		res = self.result(
			method="ism.api.agent.get_shop_floor_status",
			args={},
		)
		envelope = res.get("result", {})
		self.assertTrue(envelope.get("success"))
		summary = envelope.get("data", {}).get("summary", {})
		self.assertIn("active_bays", summary)
		self.assertIn("active_technicians", summary)
		self.assertIn("active_repair_orders", summary)

	def test_mcp_call_send_quote_for_approval(self):
		"""Verify ism.api.agent.send_quote_for_approval via MCP call_method."""
		res1 = self.result(
			method="ism.api.agent.create_quote",
			args={"customer": self.customer, "vehicle": self.vehicle_vin, "odometer_in": 45000, "concern": "Approval test"},
		)
		ro_name = res1["result"]["data"]["name"]

		res2 = self.result(
			method="ism.api.agent.send_quote_for_approval",
			args={"repair_order": ro_name, "communication_medium": "link"},
		)
		envelope = res2.get("result", {})
		self.assertTrue(envelope.get("success"))
		token = envelope.get("data", {}).get("token")
		self.assertIsNotNone(token)
		urls = envelope.get("urls", {})
		self.assertIn("approval", urls)
		self.assertIn(f"/quote-approval?token={token}", urls["approval"])

	def test_mcp_call_update_job_status_guard(self):
		"""Verify update_job_status blocks work when authorization is not granted."""
		res1 = self.result(
			method="ism.api.agent.create_quote",
			args={"customer": self.customer, "vehicle": self.vehicle_vin, "odometer_in": 45000, "concern": "Guard test"},
		)
		ro_name = res1["result"]["data"]["name"]

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
		if not frappe.db.exists("Role", "Service Advisor"):
			frappe.get_doc(
				doctype="Role",
				role_name="Service Advisor",
				desk_access=1,
			).insert(ignore_permissions=True)

		self._grant_role_permissions("Service Advisor")

		# Standard DocPerms for System Manager and ISM Manager must remain intact on ISM Settings
		if frappe.db.exists("Custom DocPerm", {"parent": "ISM Settings"}):
			self.assertTrue(frappe.db.exists("Custom DocPerm", {"parent": "ISM Settings", "role": "System Manager"}))
			self.assertTrue(frappe.db.exists("Custom DocPerm", {"parent": "ISM Settings", "role": "ISM Manager"}))

		user_email = f"shop_agent_{frappe.generate_hash(length=6).lower()}@example.com"
		user_doc = frappe.get_doc(
			doctype="User",
			email=user_email,
			first_name="Shop",
			last_name="Agent",
			send_welcome_email=0,
			roles=[{"role": "Service Advisor"}],
		)
		user_doc.flags.ignore_password_policy = True
		user_doc.insert(ignore_permissions=True)

		keys = generate_keys(user_email)
		auth_header = f"token {keys['api_key']}:{keys['api_secret']}"

		# Service Advisor can call create_quote via MCP
		response = self.invoke(
			tool="call_method",
			auth_header=auth_header,
			method="ism.api.agent.create_quote",
			args={
				"customer": self.customer,
				"vehicle": self.vehicle_vin,
				"odometer_in": 45000,
				"concern": "Created by dedicated shop agent user",
			},
		)
		self.assertEqual(response.status_code, 200)
		res = response.json.get("result", {})
		self.assertFalse(res.get("isError", False), f"Agent user call failed: {res}")
		data = res.get("structuredContent", {}).get("result", {})
		self.assertTrue(data.get("success"))

		# Service Advisor is strictly blocked from System Manager operations (e.g. System Settings modification)
		forbidden_response = self.invoke(
			tool="create_document",
			auth_header=auth_header,
			doctype="System Settings",
			data={},
		)
		self.assertEqual(forbidden_response.status_code, 200)
		f_res = forbidden_response.json.get("result", {})
		self.assertTrue(f_res.get("isError", False), "Service Advisor should not be allowed to modify System Settings")

	def test_mcp_chunk2_tools_end_to_end(self):
		"""Verify Chunk 2 tools (deferred services, opposite side, toggle completion, supplement, park, action items) via MCP."""
		veh_doc = frappe.get_doc("Repair Vehicle", self.vehicle_vin)
		veh_doc.append(
			"deferred_services",
			{
				"job_title": "MCP Brake Fluid Flush",
				"estimated_amount": 135.0,
				"status": "Open Recommendation",
				"decline_reason": "Customer deferred at last visit",
			},
		)
		veh_doc.save(ignore_permissions=True)

		# Create quote and verify deferred services are surfaced automatically
		res_q = self.result(
			method="ism.api.agent.create_quote",
			args={
				"customer_name": self.customer,
				"vehicle_identifier": self.vehicle_vin,
				"odometer_in": 45000,
				"services": [{"service_title": "Front Left Strut Replacement", "labor_hours": 2.0, "labor_rate": 140.0}],
			},
		)["result"]
		self.assertTrue(res_q["success"])
		ro_name = res_q["data"]["name"]
		self.assertGreaterEqual(res_q["data"].get("deferred_count", 0), 1)
		first_row_id = res_q["data"]["services"][0]["name"]

		# Explicit deferred discovery + add deferred item
		res_def = self.result(
			method="ism.api.agent.get_deferred_services",
			args={"repair_order": ro_name},
		)["result"]
		self.assertGreaterEqual(res_def["data"]["deferred_count"], 1)

		res_add_def = self.result(
			method="ism.api.agent.add_deferred_services",
			args={"repair_order": ro_name, "deferred_items": ["MCP Brake Fluid Flush"]},
		)["result"]
		self.assertTrue(res_add_def["success"])
		self.assertGreaterEqual(len(res_add_def["data"]["services"]), 2)

		# Opposite-side duplicate
		res_dup = self.result(
			method="ism.api.agent.duplicate_service_for_opposite_side",
			args={"repair_order": ro_name, "service_row_id": first_row_id},
		)["result"]
		self.assertTrue(res_dup["success"])
		self.assertTrue(any("Right" in s["service_title"] for s in res_dup["data"]["services"]))

		# Authorize RO so floor actions are allowed
		ro_doc = frappe.get_doc("Repair Order", ro_name)
		ro_doc.approval_status = "Approved"
		ro_doc.status = "Authorized - In Progress"
		ro_doc.save(ignore_permissions=True)

		# Toggle completion, add supplement, park bay
		res_tog = self.result(
			method="ism.api.agent.toggle_service_completion",
			args={"repair_order": ro_name, "service_row_id": first_row_id, "is_completed": True},
		)["result"]
		self.assertEqual(res_tog["data"]["services"][0]["is_completed"], 1)

		res_sup = self.result(
			method="ism.api.agent.add_supplement",
			args={"repair_order": ro_name, "service_title": "MCP Pinch Bolt Supplement", "labor_hours": 0.4},
		)["result"]
		self.assertTrue(any(s.get("is_supplement") for s in res_sup["data"]["services"]))

		res_park = self.result(
			method="ism.api.agent.update_job_status",
			args={"repair_order": ro_name, "action": "park", "notes": "Waiting on specialty tool"},
		)["result"]
		self.assertEqual(res_park["data"]["vehicle_location"], "In Customer Lot")

		# Shop Action Item create / list / resolve
		res_sai = self.result(
			method="ism.api.agent.create_shop_action_item",
			args={
				"title": "MCP Subframe Stud Seized",
				"category": "Shop Floor Discrepancy",
				"priority": "High",
				"reference_doctype": "Repair Order",
				"reference_name": ro_name,
			},
		)["result"]
		self.assertTrue(res_sai["success"])
		item_name = res_sai["data"]["name"]

		res_list = self.result(method="ism.api.agent.list_shop_action_items", args={"status": "Open"})["result"]
		self.assertTrue(any(it["name"] == item_name for it in res_list["data"]["items"]))

		res_res = self.result(
			method="ism.api.agent.resolve_shop_action_item",
			args={"action_item": item_name, "resolution_notes": "Replaced stud"},
		)["result"]
		self.assertEqual(res_res["data"]["status"], "Resolved")

	def test_mcp_unauthenticated_request_rejected(self):
		"""Verify unauthenticated MCP requests are strictly rejected with 401."""
		response = self.request(
			{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
			auth=False,
		)
		self.assertIn(response.status_code, [401, 403])
