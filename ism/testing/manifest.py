# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

MANIFEST_SCHEMA_VERSION = "1.0"

_DOUBLE_BRACE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
_PREFIX_BRACE_PATTERN = re.compile(r"\{manifest:([a-zA-Z0-9_.-]+)\}")


@dataclass
class SimManifest:
	"""JSON-serializable handle-to-DB-name registry for ISM simulation worlds."""

	schema_version: str = MANIFEST_SCHEMA_VERSION
	profile: str = "clean_master"
	seed: int = 20260101
	anchor_date: str = ""
	company: str = "Lightspeed EV"
	company_abbr: str = "LEV"
	currency: str = "CAD"
	warehouse: str = "Main Shop Stores - LEV"
	warehouses: dict[str, str] = field(default_factory=dict)
	tax_templates: dict[str, str] = field(default_factory=dict)
	price_lists: dict[str, str] = field(default_factory=dict)
	modes_of_payment: dict[str, str] = field(default_factory=dict)
	users: dict[str, str] = field(default_factory=dict)
	bays: dict[str, str] = field(default_factory=dict)
	equipment_tags: dict[str, str] = field(default_factory=dict)
	schedule_entry_types: dict[str, str] = field(default_factory=dict)
	techs: dict[str, str] = field(default_factory=dict)
	holiday_list: str = ""
	leave_types: dict[str, str] = field(default_factory=dict)
	suppliers: dict[str, str] = field(default_factory=dict)
	items: dict[str, str] = field(default_factory=dict)
	packages: dict[str, str] = field(default_factory=dict)
	inspection_templates: dict[str, str] = field(default_factory=dict)
	customers: dict[str, str] = field(default_factory=dict)
	vehicles: dict[str, str] = field(default_factory=dict)
	ros: dict[str, str] = field(default_factory=dict)
	pos: dict[str, str] = field(default_factory=dict)
	rfqs: dict[str, str] = field(default_factory=dict)
	pis: dict[str, str] = field(default_factory=dict)
	sis: dict[str, str] = field(default_factory=dict)
	pes: dict[str, str] = field(default_factory=dict)
	schedule_entries: dict[str, str] = field(default_factory=dict)
	action_items: dict[str, str] = field(default_factory=dict)
	leave_applications: dict[str, str] = field(default_factory=dict)
	metadata: dict[str, Any] = field(default_factory=dict)

	def register(self, bucket: str, handle: str, db_name: str) -> str:
		"""Register a DB document name under a logical bucket handle and return db_name."""
		if not hasattr(self, bucket):
			raise KeyError(f"Unknown SimManifest bucket '{bucket}'")
		target = getattr(self, bucket)
		if not isinstance(target, dict):
			raise TypeError(f"SimManifest field '{bucket}' is not a mapping bucket")
		target[handle] = db_name
		return db_name

	def next_handle(self, bucket: str, prefix: str) -> str:
		"""Return the next unused sequential handle '<prefix>_N' in a bucket."""
		if not hasattr(self, bucket):
			raise KeyError(f"Unknown SimManifest bucket '{bucket}'")
		target = getattr(self, bucket)
		if not isinstance(target, dict):
			raise TypeError(f"SimManifest field '{bucket}' is not a mapping bucket")
		idx = 1
		while f"{prefix}_{idx}" in target:
			idx += 1
		return f"{prefix}_{idx}"

	def resolve(self, handle_path: str) -> Any:
		"""Resolve a top-level field ('company') or dotted path ('bays.main_lift') to its DB value."""
		if not handle_path or not isinstance(handle_path, str):
			raise KeyError("SimManifest handle_path must be a non-empty string")
		parts = handle_path.split(".")
		cur: Any = self
		for idx, part in enumerate(parts):
			if isinstance(cur, SimManifest):
				if not hasattr(cur, part):
					raise KeyError(f"Unknown SimManifest field '{part}' in '{handle_path}'")
				cur = getattr(cur, part)
			elif isinstance(cur, dict):
				if part not in cur:
					prefix = ".".join(parts[:idx])
					raise KeyError(f"Unknown handle '{part}' in SimManifest.{prefix} ('{handle_path}')")
				cur = cur[part]
			else:
				prefix = ".".join(parts[:idx])
				raise KeyError(f"Cannot traverse non-mapping SimManifest.{prefix} for '{handle_path}'")
		return cur

	def interpolate(self, template: str) -> str:
		"""Replace '{{handle.path}}' and '{manifest:handle.path}' placeholders using resolve()."""
		if not isinstance(template, str):
			raise TypeError("template must be a str")

		def _replace(match: re.Match[str]) -> str:
			return str(self.resolve(match.group(1)))

		rendered = _DOUBLE_BRACE_PATTERN.sub(_replace, template)
		return _PREFIX_BRACE_PATTERN.sub(_replace, rendered)

	def to_dict(self) -> dict[str, Any]:
		"""Convert manifest to a plain JSON-serializable dictionary."""
		return asdict(self)

	def to_json(self, indent: int = 2) -> str:
		"""Serialize manifest to a formatted JSON string."""
		return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> "SimManifest":
		"""Reconstruct SimManifest from a dictionary payload."""
		if not isinstance(data, dict):
			raise TypeError("SimManifest.from_dict expects a dict")
		valid_fields = cls.__dataclass_fields__.keys()
		kwargs = {}
		for key in valid_fields:
			if key in data:
				val = data[key]
				if isinstance(val, dict):
					kwargs[key] = dict(val)
				else:
					kwargs[key] = val
		return cls(**kwargs)

	@classmethod
	def from_json(cls, raw: str) -> "SimManifest":
		"""Reconstruct SimManifest from a JSON string."""
		parsed = json.loads(raw)
		return cls.from_dict(parsed)
