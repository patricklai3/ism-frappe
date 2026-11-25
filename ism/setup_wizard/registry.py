# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Auto-discovery, validation, and deterministic DAG topological ordering for Setup Wizard steps."""

from __future__ import annotations

import heapq
import importlib
import inspect
import pkgutil
from collections.abc import Iterable
from typing import Any

import frappe

from ism.setup_wizard.base import BaseSetupStep

_CACHED_REGISTRY: SetupRegistry | None = None


def _resolve_dotted_attr(dotted_path: str) -> Any:
	try:
		return frappe.get_attr(dotted_path)
	except Exception:
		module_path, _, attr_name = dotted_path.rpartition(".")
		if not module_path or not attr_name:
			raise ImportError(f"Invalid dotted path: '{dotted_path}'")
		mod = importlib.import_module(module_path)
		return getattr(mod, attr_name)


def _instantiate_step_candidate(candidate: Any) -> list[BaseSetupStep]:
	if isinstance(candidate, BaseSetupStep):
		return [candidate]
	if isinstance(candidate, type) and issubclass(candidate, BaseSetupStep):
		if inspect.isabstract(candidate):
			return []
		return [candidate()]
	if callable(candidate):
		result = candidate()
		if isinstance(result, BaseSetupStep):
			return [result]
		if isinstance(result, Iterable):
			return [item for item in result if isinstance(item, BaseSetupStep)]
	raise TypeError(
		f"Setup step hook entry must resolve to a BaseSetupStep subclass/instance, got {type(candidate).__name__}."
	)


class SetupRegistry:
	"""Registry of validated, DAG-ordered BaseSetupStep instances."""

	def __init__(self, steps: Iterable[BaseSetupStep] | None = None) -> None:
		self._steps_by_id: dict[str, BaseSetupStep] = {}
		self._ordered_steps: list[BaseSetupStep] = []
		self._claimed_sections: dict[str, str] = {}
		if steps is not None:
			self._load_steps(list(steps))

	@classmethod
	def discover(cls) -> SetupRegistry:
		"""Discover all concrete BaseSetupStep subclasses from ism.setup_wizard.steps.* and hooks."""
		discovered: list[BaseSetupStep] = []
		seen_classes: set[type[BaseSetupStep]] = set()

		import ism.setup_wizard.steps as steps_pkg

		for mod_info in pkgutil.iter_modules(steps_pkg.__path__):
			if mod_info.name.startswith("_"):
				continue
			full_mod_name = f"{steps_pkg.__name__}.{mod_info.name}"
			mod = importlib.import_module(full_mod_name)
			for _attr_name, obj in inspect.getmembers(mod, inspect.isclass):
				if obj.__module__ != mod.__name__:
					continue
				if not issubclass(obj, BaseSetupStep) or obj is BaseSetupStep:
					continue
				if inspect.isabstract(obj):
					continue
				if getattr(obj, "meta", None) is None:
					continue
				if obj not in seen_classes:
					seen_classes.add(obj)
					discovered.append(obj())

		hook_paths: list[str] = []
		try:
			hook_paths = list(frappe.get_hooks("ism_setup_wizard_steps") or [])
		except Exception:
			hook_paths = []

		for path in hook_paths:
			if not path or not isinstance(path, str):
				continue
			target = _resolve_dotted_attr(path)
			for step_instance in _instantiate_step_candidate(target):
				step_cls = type(step_instance)
				if step_cls in seen_classes:
					continue
				seen_classes.add(step_cls)
				discovered.append(step_instance)

		return cls(steps=discovered)

	def _load_steps(self, steps: list[BaseSetupStep]) -> None:
		by_id: dict[str, BaseSetupStep] = {}
		claimed_sections: dict[str, str] = {}

		for step in steps:
			if not isinstance(step, BaseSetupStep):
				raise TypeError(f"Expected BaseSetupStep instance, got {type(step).__name__}.")
			meta = getattr(step, "meta", None)
			if meta is None or not getattr(meta, "id", None) or not str(meta.id).strip():
				raise ValueError(f"Setup step {type(step).__name__} is missing a valid meta.id.")
			step_id = str(meta.id).strip()
			if step_id in by_id:
				raise ValueError(
					f"Duplicate setup step id '{step_id}' defined by {type(by_id[step_id]).__name__} "
					f"and {type(step).__name__}."
				)
			by_id[step_id] = step

			for section in meta.managed_settings_sections:
				if section in claimed_sections:
					raise ValueError(
						f"Duplicate ISM Settings section ownership for '{section}': "
						f"claimed by both '{claimed_sections[section]}' and '{step_id}'."
					)
				claimed_sections[section] = step_id

		for step_id, step in by_id.items():
			for dep_id in step.meta.depends_on:
				if dep_id == step_id:
					raise ValueError(f"Setup step '{step_id}' cannot depend on itself.")
				if dep_id not in by_id:
					raise ValueError(
						f"Setup step '{step_id}' depends on unknown step '{dep_id}'."
					)

		ordered = self._topological_sort(by_id)
		self._steps_by_id = by_id
		self._ordered_steps = ordered
		self._claimed_sections = claimed_sections

	@staticmethod
	def _topological_sort(by_id: dict[str, BaseSetupStep]) -> list[BaseSetupStep]:
		"""Kahn's topological sort with deterministic tie-breaking by (meta.order, meta.id)."""
		in_degree: dict[str, int] = {sid: 0 for sid in by_id}
		dependents: dict[str, list[str]] = {sid: [] for sid in by_id}

		for sid, step in by_id.items():
			for dep_id in step.meta.depends_on:
				dependents[dep_id].append(sid)
				in_degree[sid] += 1

		ready_heap: list[tuple[int, str]] = [
			(by_id[sid].meta.order, sid) for sid, deg in in_degree.items() if deg == 0
		]
		heapq.heapify(ready_heap)

		ordered: list[BaseSetupStep] = []
		while ready_heap:
			_order, current_id = heapq.heappop(ready_heap)
			ordered.append(by_id[current_id])
			for nxt_id in dependents[current_id]:
				in_degree[nxt_id] -= 1
				if in_degree[nxt_id] == 0:
					heapq.heappush(ready_heap, (by_id[nxt_id].meta.order, nxt_id))

		if len(ordered) != len(by_id):
			cyclic = sorted(sid for sid, deg in in_degree.items() if deg > 0)
			raise ValueError(
				f"Cycle detected in setup wizard step dependencies involving: {', '.join(cyclic)}."
			)

		return ordered

	def get_ordered_steps(self) -> list[BaseSetupStep]:
		return list(self._ordered_steps)

	def get_step(self, step_id: str) -> BaseSetupStep:
		if step_id not in self._steps_by_id:
			raise KeyError(f"Unknown setup wizard step: '{step_id}'")
		return self._steps_by_id[step_id]

	def get_claimed_ism_settings_sections(self) -> dict[str, str]:
		return dict(self._claimed_sections)

	def __len__(self) -> int:
		return len(self._ordered_steps)

	def __contains__(self, step_id: str) -> bool:
		return step_id in self._steps_by_id


def get_registry() -> SetupRegistry:
	"""Return the cached SetupRegistry singleton, discovering steps on first call."""
	global _CACHED_REGISTRY
	if _CACHED_REGISTRY is None:
		_CACHED_REGISTRY = SetupRegistry.discover()
	return _CACHED_REGISTRY


def clear_registry_cache() -> None:
	"""Clear the cached SetupRegistry singleton (useful in tests and hot-reloads)."""
	global _CACHED_REGISTRY
	_CACHED_REGISTRY = None


def get_ordered_steps() -> list[BaseSetupStep]:
	"""Return all registered setup steps in deterministic DAG execution order."""
	return get_registry().get_ordered_steps()


def get_step(step_id: str) -> BaseSetupStep:
	"""Look up a single registered setup step by ID."""
	return get_registry().get_step(step_id)


def get_claimed_ism_settings_sections() -> dict[str, str]:
	"""Return a mapping of ISM Settings section_fieldname -> owning step_id."""
	return get_registry().get_claimed_ism_settings_sections()
