	def test_ism_manager_permissions(self):
		"""Verify that a user with only ISM Manager role has read and write permissions on ISM Settings."""
		user_email = f"test_ism_mgr_{frappe.generate_hash(length=6)}@example.com"
		if frappe.db.exists("User", user_email):
			frappe.delete_doc("User", user_email, force=True)

		user = frappe.get_doc({
			"doctype": "User",
			"email": user_email,
			"first_name": "Test ISM Manager",
			"roles": [{"role": "ISM Manager"}],
		}).insert(ignore_permissions=True)
		self.track(user)

		self.assertTrue(
			frappe.has_permission("ISM Settings", "read", user=user_email),
			"ISM Manager role should have read permission on ISM Settings",
		)
		self.assertTrue(
			frappe.has_permission("ISM Settings", "write", user=user_email),
			"ISM Manager role should have write permission on ISM Settings",
		)
