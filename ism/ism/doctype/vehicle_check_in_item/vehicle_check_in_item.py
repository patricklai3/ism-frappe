# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json

from frappe.model.document import Document


class VehicleCheckinItem(Document):
	"""Child row representing one check-in inspection point on a Repair Order."""

	def validate(self):
		if self.inspection_description:
			self.inspection_description = self.inspection_description.strip()
		if not self.status:
			self.status = "Pending"

		photos = self._parse_photos()
		if self.image and self.image not in photos:
			photos.insert(0, self.image)
		if photos and not self.image:
			self.image = photos[0]
		self.photos = json.dumps(photos) if photos else None

	def _parse_photos(self) -> list[str]:
		raw = getattr(self, "photos", None)
		if isinstance(raw, str):
			try:
				raw = json.loads(raw) if raw.strip() else []
			except Exception:
				raw = []
		if not isinstance(raw, list):
			return []
		deduped: list[str] = []
		for entry in raw:
			if isinstance(entry, str) and entry.strip() and entry.strip() not in deduped:
				deduped.append(entry.strip())
		return deduped
