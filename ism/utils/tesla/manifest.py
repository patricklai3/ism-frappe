# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import logging
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

try:
	import frappe
except ImportError:  # Allow standalone Python ingestion/CLI usage without Frappe
	frappe = None

_LOGGER = logging.getLogger(__name__)
_DEFAULT_URLOPEN = urllib.request.urlopen

_ALLOWED_TESLA_HOST = "service.tesla.com"
_MANIFEST_TIMEOUT_SECONDS = 10
_MAX_MANIFEST_BYTES = 5 * 1024 * 1024
_MAX_MANIFEST_CACHE_SIZE = 64
_MAX_NEGATIVE_CACHE_SIZE = 128
_NEGATIVE_CACHE_TTL_SECONDS = 300
_MAX_REDIRECTS = 3

# Shared mutable cache inspected and pre-populated by integration tests
_MANIFEST_CACHE = {}
_MANIFEST_NEGATIVE_CACHE = {}


def _log_manifest_error(title, exc):
	"""Log manifest fetch/parse errors via Frappe when available or standard logging."""
	tb = traceback.format_exc()
	_LOGGER.warning("%s: %s", title, exc)
	if frappe is not None:
		try:
			frappe.log_error(message=tb, title=title)
		except Exception as log_exc:
			_LOGGER.warning("Failed to record Frappe error log (%s): %s", title, log_exc)


def _validate_tesla_docs_url(raw_url):
	"""Validate URL strictly as https://service.tesla.com/docs/... with no path traversal."""
	if not isinstance(raw_url, str) or not raw_url.strip():
		return None

	candidate = raw_url.strip()
	if "\\" in candidate:
		return None

	parsed = urllib.parse.urlparse(candidate)
	if parsed.scheme.lower() != "https":
		return None
	if parsed.username or parsed.password or parsed.port is not None:
		return None
	if (parsed.netloc or "").lower() != _ALLOWED_TESLA_HOST:
		return None
	if (parsed.hostname or "").lower() != _ALLOWED_TESLA_HOST:
		return None

	raw_path = parsed.path or ""
	unquoted_path = urllib.parse.unquote(raw_path)
	if "\\" in unquoted_path:
		return None
	if ".." in raw_path.split("/") or ".." in unquoted_path.split("/"):
		return None
	if "/docs/" not in raw_path:
		return None

	return parsed


class _SafeTeslaRedirectHandler(urllib.request.HTTPRedirectHandler):
	"""Reject redirects off https://service.tesla.com/docs/ or containing '..' segments."""

	def redirect_request(self, req, fp, code, msg, headers, newurl):
		resolved = urllib.parse.urljoin(req.full_url, newurl)
		if not _validate_tesla_docs_url(resolved):
			raise urllib.error.HTTPError(
				resolved,
				code,
				f"Blocked unsafe redirect target: {resolved}",
				headers,
				fp,
			)
		redirect_count = getattr(req, "_ism_redirect_count", 0) + 1
		if redirect_count > _MAX_REDIRECTS:
			raise urllib.error.HTTPError(
				resolved,
				code,
				"Too many redirects fetching Tesla manifest",
				headers,
				fp,
			)
		redirected = super().redirect_request(req, fp, code, msg, headers, resolved)
		if redirected is not None:
			redirected._ism_redirect_count = redirect_count
		return redirected


def _store_manifest_cache(manifest_url, topics):
	"""Persist manifest topics in bounded in-process cache and Redis when available."""
	if manifest_url not in _MANIFEST_CACHE and len(_MANIFEST_CACHE) >= _MAX_MANIFEST_CACHE_SIZE:
		_MANIFEST_CACHE.pop(next(iter(_MANIFEST_CACHE)), None)
	_MANIFEST_CACHE[manifest_url] = topics
	_MANIFEST_NEGATIVE_CACHE.pop(manifest_url, None)

	if frappe is not None:
		try:
			frappe.cache().set_value(
				f"tesla_manifest:{manifest_url}",
				json.dumps(topics),
				expires_in_sec=86400,
			)
		except Exception as exc:
			_LOGGER.warning("Failed to write manifest Redis cache: %s", exc)


def _record_negative_cache(manifest_url):
	"""Cache a short-lived negative result after repeated manifest fetch failures."""
	if manifest_url not in _MANIFEST_NEGATIVE_CACHE and len(_MANIFEST_NEGATIVE_CACHE) >= _MAX_NEGATIVE_CACHE_SIZE:
		_MANIFEST_NEGATIVE_CACHE.pop(next(iter(_MANIFEST_NEGATIVE_CACHE)), None)
	_MANIFEST_NEGATIVE_CACHE[manifest_url] = time.monotonic() + _NEGATIVE_CACHE_TTL_SECONDS


def _is_negatively_cached(manifest_url):
	"""Return True if manifest_url recently failed and negative TTL has not expired."""
	expires_at = _MANIFEST_NEGATIVE_CACHE.get(manifest_url)
	if expires_at is None:
		return False
	if time.monotonic() >= expires_at:
		_MANIFEST_NEGATIVE_CACHE.pop(manifest_url, None)
		return False
	return True


def _open_manifest_request(req):
	"""Open a manifest Request via safe redirect-validating opener or patched urlopen."""
	if urllib.request.urlopen is not _DEFAULT_URLOPEN:
		return urllib.request.urlopen(req, timeout=_MANIFEST_TIMEOUT_SECONDS)
	opener = urllib.request.build_opener(_SafeTeslaRedirectHandler())
	return opener.open(req, timeout=_MANIFEST_TIMEOUT_SECONDS)


def _fetch_manifest_topics(manifest_url):
	"""Fetch and parse dependencies.json with browser headers and a single minimal-UA retry."""
	request_header_variants = (
		{
			"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
			"Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
			"Accept-Language": "en-US,en;q=0.9",
			"Referer": "https://service.tesla.com/",
		},
		{"User-Agent": "Mozilla/5.0"},
	)

	last_exc = None
	for headers in request_header_variants:
		req = urllib.request.Request(manifest_url, headers=headers)
		try:
			with _open_manifest_request(req) as res:
				final_url = res.geturl() if hasattr(res, "geturl") else None
				if isinstance(final_url, str) and final_url and not _validate_tesla_docs_url(final_url):
					raise ValueError(f"Unsafe final manifest URL after redirect: {final_url}")
				raw_bytes = res.read(_MAX_MANIFEST_BYTES + 1)
				if len(raw_bytes) > _MAX_MANIFEST_BYTES:
					raise ValueError("Tesla manifest exceeds maximum allowed size")
				manifest_data = json.loads(raw_bytes.decode("utf-8"))
				return manifest_data.get("topics", {}).get("topic", [])
		except Exception as exc:
			last_exc = exc

	if last_exc is not None:
		_log_manifest_error("Tesla Manifest Fetch Error", last_exc)
	return None


def _read_cached_topics(manifest_url):
	"""Read warm topics from _MANIFEST_CACHE first, then Frappe Redis cache."""
	topics = _MANIFEST_CACHE.get(manifest_url)
	if topics is not None:
		return topics

	if frappe is not None:
		try:
			cached_json = frappe.cache().get_value(f"tesla_manifest:{manifest_url}")
			if cached_json:
				topics = json.loads(cached_json) if isinstance(cached_json, str) else cached_json
				if topics is not None:
					return topics
		except Exception as exc:
			_LOGGER.warning("Failed to read manifest Redis cache: %s", exc)
	return None


def _match_topic(topics, filename, doc_guid=None, correction_code=None):
	"""Locate the manifest topic entry matching filename, doc_guid, or correction_code."""
	target_guid = str(doc_guid).strip().upper() if doc_guid else ""
	if not target_guid and filename and filename.startswith("GUID-"):
		target_guid = filename.split(".")[0].upper()
	target_cc = str(correction_code).strip() if correction_code else ""

	for topic in topics or []:
		if not isinstance(topic, dict):
			continue
		t_url = str(topic.get("url") or "").strip()
		t_guid = str(topic.get("guid") or "").strip().upper()
		t_cc = str(topic.get("correction-code") or "").strip()
		t_rh_cc = str(topic.get("rh-correction-code") or "").strip()

		if filename and t_url == filename:
			return topic
		if target_guid and t_guid == target_guid:
			return topic
		if target_cc and (t_cc == target_cc or t_rh_cc == target_cc):
			return topic
	return None


def _parse_topic_capability(target):
	"""Convert a manifest topic dict into ISM mobile capability and symmetry metadata."""
	cap = target.get("mobile-capable")
	if isinstance(cap, list):
		is_mobile = (
			1
			if any(str(c).strip().lower() not in ("no", "none", "unknown", "0", "false") for c in cap)
			else 0
		)
		mobile_level = ", ".join(str(c) for c in cap)
	elif cap and str(cap).strip().lower() not in ("no", "none", "unknown", "0", "false"):
		is_mobile = 1
		mobile_level = str(cap)
	else:
		is_mobile = 0
		mobile_level = "No"

	rh_cc = str(target.get("rh-correction-code") or "").strip() or None
	return {
		"is_mobile_capable": is_mobile,
		"mobile_service_level": mobile_level,
		"rh_correction_code": rh_cc,
		"is_symmetrical": 1 if rh_cc else 0,
	}


def get_mobile_capability_from_manifest(url, doc_guid=None, correction_code=None, allow_network=True):
	"""Fetch or retrieve cached dependencies.json to determine mobile capability."""
	parsed_url = _validate_tesla_docs_url(url)
	if not parsed_url:
		return None

	clean_path = (parsed_url.path or "").rstrip("/")
	if "/" not in clean_path:
		return None

	parent_path, filename = clean_path.rsplit("/", 1)
	if not parent_path:
		return None

	base_manifest_url = f"https://{_ALLOWED_TESLA_HOST}{parent_path}/dependencies.json"
	if not _validate_tesla_docs_url(base_manifest_url):
		return None

	try:
		topics = _read_cached_topics(base_manifest_url)
		if topics is None:
			in_unmocked_test = (
				frappe is not None
				and bool(getattr(getattr(frappe, "flags", None), "in_test", False))
				and urllib.request.urlopen is _DEFAULT_URLOPEN
			)
			if not allow_network or in_unmocked_test or _is_negatively_cached(base_manifest_url):
				return None
			topics = _fetch_manifest_topics(base_manifest_url)
			if topics is None:
				_record_negative_cache(base_manifest_url)
				return None
			_store_manifest_cache(base_manifest_url, topics)

		target = _match_topic(topics, filename, doc_guid=doc_guid, correction_code=correction_code)
		if target:
			return _parse_topic_capability(target)
	except Exception as exc:
		_log_manifest_error("Tesla Manifest Capability Lookup Error", exc)

	return None
