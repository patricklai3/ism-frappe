"""External Google Drive media storage, private-file fallback, validation, and authenticated proxy."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import frappe
import requests
from frappe import _
from frappe.utils import cint

ALLOWED_IMAGE_MIMES: frozenset[str] = frozenset(
	{
		"image/jpeg",
		"image/png",
		"image/webp",
	}
)

ALLOWED_AUDIO_MIMES: frozenset[str] = frozenset(
	{
		"audio/webm",
		"audio/ogg",
		"audio/mp4",
		"audio/mpeg",
		"audio/wav",
		"audio/x-m4a",
	}
)

ALLOWED_MEDIA_MIMES: frozenset[str] = ALLOWED_IMAGE_MIMES | ALLOWED_AUDIO_MIMES

MIME_TO_EXTENSION: dict[str, str] = {
	"image/jpeg": ".jpg",
	"image/png": ".png",
	"image/webp": ".webp",
	"audio/webm": ".webm",
	"audio/ogg": ".ogg",
	"audio/mp4": ".mp4",
	"audio/mpeg": ".mp3",
	"audio/wav": ".wav",
	"audio/x-m4a": ".m4a",
}

DEFAULT_MEDIA_BACKEND = "Frappe Private File"
GOOGLE_DRIVE_BACKEND = "Google Drive"
AUTH_MODE_OAUTH2 = "OAuth2 Refresh Token"
AUTH_MODE_SERVICE_ACCOUNT = "Service Account JSON"

DEFAULT_MAX_AUDIO_MB = 15
DEFAULT_MAX_IMAGE_MB = 10

GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GDRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GDRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
GDRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
GDRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

GDRIVE_ACCESS_TOKEN_CACHE_KEY = "ism:gdrive:access_token"
GDRIVE_ACCESS_TOKEN_TTL_SECONDS = 3300
GDRIVE_FOLDER_CACHE_PREFIX = "ism:gdrive:folder:"
GDRIVE_FOLDER_CACHE_TTL_SECONDS = 3600

ALLOWED_MEDIA_READ_ROLES: tuple[str, ...] = (
	"System Manager",
	"ISM Manager",
	"ISM Service Advisor",
	"ISM Technician",
)

ALLOWED_MEDIA_ADMIN_ROLES: tuple[str, ...] = (
	"System Manager",
	"ISM Manager",
)

_FILE_REF_RE = re.compile(r"^m_[A-Za-z0-9]{8,64}$")
_RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_SAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_FOLDER_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_STREAM_CHUNK_SIZE = 65536
_HTTP_TIMEOUT_SECONDS = 20


@dataclass
class MediaStreamResult:
	"""Streaming media payload with HTTP status and range headers."""

	status_code: int
	content_type: str
	filename: str
	chunks: Iterator[bytes]
	content_length: int | None = None
	content_range: str | None = None
	accept_ranges: str = "bytes"
	close_callback: Callable[[], None] | None = None

	def close(self) -> None:
		if self.close_callback is not None:
			self.close_callback()
			self.close_callback = None

	def read_all(self, max_bytes: int | None = None) -> bytes:
		"""Consume the stream into memory, enforcing an optional byte cap."""
		buf = bytearray()
		try:
			for chunk in self.chunks:
				if not chunk:
					continue
				if max_bytes is not None and len(buf) + len(chunk) > max_bytes:
					frappe.throw(
						_("Media stream exceeded maximum allowed size of {0} bytes.").format(max_bytes),
						frappe.ValidationError,
					)
				buf.extend(chunk)
		finally:
			self.close()
		return bytes(buf)


def _load_settings(settings: Any = None) -> Any:
	"""Load ISM Settings defensively without assuming new fields are migrated yet."""
	if settings is not None:
		return settings
	if frappe.db.exists("DocType", "ISM Settings"):
		try:
			return frappe.get_cached_doc("ISM Settings")
		except Exception:
			return None
	return None


def _get_setting(settings: Any, field: str, default: Any = None) -> Any:
	"""Read a field from ISM Settings defensively via getattr."""
	if settings is None:
		return default
	val = getattr(settings, field, default)
	if val is None or val == "":
		return default
	return val


def _get_secret(settings: Any, field: str, default: str = "") -> str:
	"""Read a Password field from ISM Settings defensively, supporting both Document and plain objects."""
	if settings is None:
		return default
	raw_val = getattr(settings, field, None)
	if not raw_val:
		return default
	get_password = getattr(settings, "get_password", None)
	if callable(get_password):
		try:
			decrypted = get_password(field, raise_exception=False)
			if decrypted:
				return str(decrypted)
		except Exception:
			pass
	return str(raw_val)


def normalize_content_type(content_type: str | None) -> str:
	"""Strip MIME parameters (e.g. '; codecs=opus') and normalize to lowercase."""
	if not content_type or not isinstance(content_type, str):
		return ""
	return content_type.split(";", 1)[0].strip().lower()


def _matches_magic_bytes(content_type: str, data_bytes: bytes) -> bool:
	"""Verify binary magic-byte header matches the declared allowlisted MIME type."""
	if not data_bytes:
		return False

	if content_type == "image/jpeg":
		return len(data_bytes) >= 3 and data_bytes[:3] == b"\xff\xd8\xff"

	if content_type == "image/png":
		return len(data_bytes) >= 8 and data_bytes[:8] == b"\x89PNG\r\n\x1a\n"

	if content_type == "image/webp":
		return len(data_bytes) >= 12 and data_bytes[:4] == b"RIFF" and data_bytes[8:12] == b"WEBP"

	if content_type == "audio/webm":
		return len(data_bytes) >= 4 and data_bytes[:4] == b"\x1a\x45\xdf\xa3"

	if content_type == "audio/ogg":
		return len(data_bytes) >= 4 and data_bytes[:4] == b"OggS"

	if content_type in ("audio/mp4", "audio/x-m4a"):
		return len(data_bytes) >= 12 and data_bytes[4:8] == b"ftyp"

	if content_type == "audio/mpeg":
		if len(data_bytes) >= 3 and data_bytes[:3] == b"ID3":
			return True
		return (
			len(data_bytes) >= 2
			and data_bytes[0] == 0xFF
			and (data_bytes[1] & 0xE0) == 0xE0
			and (data_bytes[1] & 0x06) != 0x00
		)

	if content_type == "audio/wav":
		return len(data_bytes) >= 12 and data_bytes[:4] == b"RIFF" and data_bytes[8:12] == b"WAVE"

	return False


def sanitize_filename(filename: str | None, content_type: str) -> str:
	"""Sanitize an uploaded filename, stripping traversal and enforcing a safe extension."""
	canonical_ext = MIME_TO_EXTENSION.get(content_type, "")
	raw = (filename or "").replace("\\", "/")
	base = os.path.basename(raw).strip()
	base = _SAFE_FILENAME_CHARS_RE.sub("_", base)
	base = re.sub(r"_+", "_", base).strip("._-")

	if not base:
		return f"media{canonical_ext}"

	stem, ext = os.path.splitext(base)
	stem = stem.strip("._-") or "media"
	ext = ext.lower()

	valid_exts_for_mime = {canonical_ext}
	if content_type == "image/jpeg":
		valid_exts_for_mime.add(".jpeg")
	elif content_type == "audio/mp4":
		valid_exts_for_mime.add(".m4a")

	if ext not in valid_exts_for_mime:
		ext = canonical_ext

	max_stem_len = max(1, 120 - len(ext))
	stem = stem[:max_stem_len]
	return f"{stem}{ext}"


def derive_folder_name(thread_key: str | None = None, repair_order: str | None = None) -> str:
	"""Derive a deterministic, safe Google Drive subfolder name for a board thread."""
	if repair_order and isinstance(repair_order, str) and repair_order.strip():
		candidate = repair_order.strip()
	elif thread_key and isinstance(thread_key, str) and thread_key.strip():
		stripped = thread_key.strip()
		if stripped.startswith("RO:"):
			candidate = stripped[3:].strip()
		else:
			candidate = stripped.replace(":", "_")
	else:
		candidate = "UNASSIGNED"

	safe = _SAFE_FOLDER_CHARS_RE.sub("_", candidate)
	safe = re.sub(r"_+", "_", safe).strip("._-")
	return safe[:100] or "UNASSIGNED"


def validate_media_payload(
	filename: str | None,
	content_type: str | None,
	data_bytes: bytes,
	settings: Any = None,
) -> dict[str, Any]:
	"""Validate media MIME type, magic bytes, and size limits; return sanitized metadata and opaque file_ref."""
	if not isinstance(data_bytes, bytes | bytearray) or len(data_bytes) == 0:
		frappe.throw(_("Media payload cannot be empty."), frappe.ValidationError)

	raw_bytes = bytes(data_bytes)
	normalized_mime = normalize_content_type(content_type)
	if normalized_mime not in ALLOWED_MEDIA_MIMES:
		frappe.throw(
			_("Unsupported media type '{0}'.").format(content_type or ""),
			frappe.ValidationError,
		)

	if not _matches_magic_bytes(normalized_mime, raw_bytes):
		frappe.throw(
			_("File content does not match declared media type '{0}'.").format(normalized_mime),
			frappe.ValidationError,
		)

	resolved_settings = _load_settings(settings)
	media_kind = "image" if normalized_mime in ALLOWED_IMAGE_MIMES else "audio"
	if media_kind == "image":
		max_mb = cint(_get_setting(resolved_settings, "media_max_image_mb", DEFAULT_MAX_IMAGE_MB))
		if max_mb <= 0:
			max_mb = DEFAULT_MAX_IMAGE_MB
	else:
		max_mb = cint(_get_setting(resolved_settings, "media_max_audio_mb", DEFAULT_MAX_AUDIO_MB))
		if max_mb <= 0:
			max_mb = DEFAULT_MAX_AUDIO_MB

	max_bytes = max_mb * 1024 * 1024
	if len(raw_bytes) > max_bytes:
		frappe.throw(
			_("Media payload exceeds maximum allowed size of {0} MB for {1}.").format(max_mb, media_kind),
			frappe.ValidationError,
		)

	safe_filename = sanitize_filename(filename, normalized_mime)
	sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
	file_ref = f"m_{frappe.generate_hash(length=16)}"

	return {
		"file_ref": file_ref,
		"filename": safe_filename,
		"content_type": normalized_mime,
		"media_kind": media_kind,
		"size_bytes": len(raw_bytes),
		"sha256": sha256_hex,
	}


def _b64url_encode(raw: bytes) -> str:
	return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign_rs256_jwt(header: dict[str, Any], claims: dict[str, Any], private_key_pem: str) -> str:
	"""Sign an RS256 JWT using Frappe-bundled cryptography (zero external packages)."""
	from cryptography.hazmat.primitives import hashes, serialization
	from cryptography.hazmat.primitives.asymmetric import padding

	header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
	claims_b64 = _b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
	signing_input = f"{header_b64}.{claims_b64}".encode("ascii")

	private_key = serialization.load_pem_private_key(
		private_key_pem.encode("utf-8"),
		password=None,
	)
	signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
	return f"{header_b64}.{claims_b64}.{_b64url_encode(signature)}"


def _parse_range_header(range_header: str | None, total_size: int) -> tuple[int, int] | None:
	"""Parse a single HTTP byte range header against total_size. Returns (start, end) inclusive or None."""
	if not range_header:
		return None
	match = _RANGE_HEADER_RE.match(range_header.strip())
	if not match:
		return None
	start_str, end_str = match.groups()
	if not start_str and not end_str:
		return None
	if total_size <= 0:
		frappe.throw(_("Requested range not satisfiable."), frappe.ValidationError)

	if not start_str:
		suffix_len = int(end_str)
		if suffix_len <= 0:
			frappe.throw(_("Requested range not satisfiable."), frappe.ValidationError)
		start = max(0, total_size - suffix_len)
		end = total_size - 1
	else:
		start = int(start_str)
		end = int(end_str) if end_str else total_size - 1
		if start >= total_size or end < start:
			frappe.throw(_("Requested range not satisfiable."), frappe.ValidationError)
		end = min(end, total_size - 1)

	return start, end


class GoogleDriveStorageBackend:
	"""Pure HTTPS Google Drive storage backend using requests + bundled cryptography."""

	backend_name = GOOGLE_DRIVE_BACKEND

	def __init__(self, settings: Any = None):
		self.settings = _load_settings(settings)

	@property
	def auth_mode(self) -> str:
		return str(_get_setting(self.settings, "gdrive_auth_mode", AUTH_MODE_OAUTH2))

	@property
	def root_folder_id(self) -> str:
		return str(_get_setting(self.settings, "gdrive_root_folder_id", "")).strip()

	def get_access_token(self, *, force_refresh: bool = False) -> str:
		"""Obtain a Google OAuth2 bearer token, cached in Redis for 3300s."""
		if not force_refresh:
			cached = frappe.cache.get_value(GDRIVE_ACCESS_TOKEN_CACHE_KEY)
			if cached:
				return str(cached)

		if self.auth_mode == AUTH_MODE_SERVICE_ACCOUNT:
			token, ttl = self._exchange_service_account_token()
		else:
			token, ttl = self._exchange_oauth2_refresh_token()

		frappe.cache.set_value(
			GDRIVE_ACCESS_TOKEN_CACHE_KEY,
			token,
			expires_in_sec=ttl,
		)
		return token

	def _exchange_oauth2_refresh_token(self) -> tuple[str, int]:
		client_id = str(_get_setting(self.settings, "gdrive_client_id", "")).strip()
		client_secret = _get_secret(self.settings, "gdrive_client_secret", "").strip()
		refresh_token = _get_secret(self.settings, "gdrive_refresh_token", "").strip()

		if not client_id or not client_secret or not refresh_token:
			frappe.throw(
				_(
					"Google Drive OAuth2 credentials (client ID, client secret, refresh token) "
					"must be configured in ISM Settings."
				),
				frappe.ValidationError,
			)

		response = requests.post(
			GDRIVE_TOKEN_URL,
			data={
				"client_id": client_id,
				"client_secret": client_secret,
				"refresh_token": refresh_token,
				"grant_type": "refresh_token",
			},
			timeout=_HTTP_TIMEOUT_SECONDS,
		)
		return self._parse_token_response(response)

	def _exchange_service_account_token(self) -> tuple[str, int]:
		raw_json = _get_secret(self.settings, "gdrive_service_account_json", "").strip()
		if not raw_json:
			frappe.throw(
				_("Google Drive Service Account JSON must be configured in ISM Settings."),
				frappe.ValidationError,
			)
		try:
			sa_info = json.loads(raw_json)
		except Exception:
			frappe.throw(
				_("Invalid Google Drive Service Account JSON in ISM Settings."),
				frappe.ValidationError,
			)

		client_email = (sa_info.get("client_email") or "").strip()
		private_key = sa_info.get("private_key") or ""
		token_uri = (sa_info.get("token_uri") or GDRIVE_TOKEN_URL).strip()
		if not client_email or not private_key:
			frappe.throw(
				_("Service Account JSON must include client_email and private_key."),
				frappe.ValidationError,
			)

		now = int(time.time())
		header: dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
		if sa_info.get("private_key_id"):
			header["kid"] = sa_info["private_key_id"]
		claims = {
			"iss": client_email,
			"scope": GDRIVE_SCOPE,
			"aud": token_uri,
			"iat": now,
			"exp": now + 3600,
		}
		assertion = _sign_rs256_jwt(header, claims, private_key)

		response = requests.post(
			token_uri,
			data={
				"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
				"assertion": assertion,
			},
			timeout=_HTTP_TIMEOUT_SECONDS,
		)
		return self._parse_token_response(response)

	def _parse_token_response(self, response: requests.Response) -> tuple[str, int]:
		if response.status_code != 200:
			frappe.throw(
				_("Google Drive token exchange failed with HTTP {0}.").format(response.status_code),
				frappe.ValidationError,
			)
		payload = response.json()
		access_token = payload.get("access_token")
		if not access_token:
			frappe.throw(
				_("Google Drive token response did not include an access_token."),
				frappe.ValidationError,
			)
		expires_in = cint(payload.get("expires_in"))
		if expires_in > 0:
			ttl = min(GDRIVE_ACCESS_TOKEN_TTL_SECONDS, max(60, expires_in - 300))
		else:
			ttl = GDRIVE_ACCESS_TOKEN_TTL_SECONDS
		return str(access_token), ttl

	def ensure_thread_folder(self, folder_name: str) -> str:
		"""Find or create a per-thread subfolder under gdrive_root_folder_id, cached in Redis."""
		safe_name = derive_folder_name(thread_key=folder_name)
		cache_key = f"{GDRIVE_FOLDER_CACHE_PREFIX}{safe_name}"
		cached_id = frappe.cache.get_value(cache_key)
		if cached_id:
			return str(cached_id)

		root_id = self.root_folder_id
		if not root_id:
			frappe.throw(
				_("Google Drive root folder ID (gdrive_root_folder_id) must be configured in ISM Settings."),
				frappe.ValidationError,
			)

		token = self.get_access_token()
		headers = {"Authorization": f"Bearer {token}"}
		escaped_name = safe_name.replace("\\", "\\\\").replace("'", "\\'")
		escaped_root = root_id.replace("\\", "\\\\").replace("'", "\\'")
		query = (
			f"mimeType='{GDRIVE_FOLDER_MIME}' and name='{escaped_name}' "
			f"and '{escaped_root}' in parents and trashed=false"
		)

		search_resp = requests.get(
			GDRIVE_FILES_URL,
			headers=headers,
			params={
				"q": query,
				"fields": "files(id,name)",
				"supportsAllDrives": "true",
				"includeItemsFromAllDrives": "true",
				"pageSize": 1,
			},
			timeout=_HTTP_TIMEOUT_SECONDS,
		)
		if search_resp.status_code != 200:
			frappe.throw(
				_("Google Drive folder lookup failed with HTTP {0}.").format(search_resp.status_code),
				frappe.ValidationError,
			)

		files = search_resp.json().get("files") or []
		if files and files[0].get("id"):
			folder_id = str(files[0]["id"])
		else:
			create_resp = requests.post(
				GDRIVE_FILES_URL,
				headers={
					"Authorization": f"Bearer {token}",
					"Content-Type": "application/json",
				},
				params={
					"supportsAllDrives": "true",
					"fields": "id,name",
				},
				json={
					"name": safe_name,
					"mimeType": GDRIVE_FOLDER_MIME,
					"parents": [root_id],
				},
				timeout=_HTTP_TIMEOUT_SECONDS,
			)
			if create_resp.status_code not in (200, 201):
				frappe.throw(
					_("Google Drive folder creation failed with HTTP {0}.").format(create_resp.status_code),
					frappe.ValidationError,
				)
			folder_id = str(create_resp.json()["id"])

		frappe.cache.set_value(
			cache_key,
			folder_id,
			expires_in_sec=GDRIVE_FOLDER_CACHE_TTL_SECONDS,
		)
		return folder_id

	def upload(
		self,
		validated: dict[str, Any],
		data_bytes: bytes,
		*,
		thread_key: str,
		repair_order: str | None = None,
		thread_name: str | None = None,
	) -> dict[str, Any]:
		"""Multipart upload directly from memory bytes with zero local disk writes."""
		folder_name = derive_folder_name(thread_key=thread_key, repair_order=repair_order)
		folder_id = self.ensure_thread_folder(folder_name)
		token = self.get_access_token()

		boundary = f"ism_board_{frappe.generate_hash(length=16)}"
		metadata = {
			"name": f"{validated['file_ref']}_{validated['filename']}",
			"parents": [folder_id],
			"appProperties": {
				"file_ref": validated["file_ref"],
				"sha256": validated["sha256"],
				"thread_key": thread_key or "",
			},
		}
		body = (
			f"--{boundary}\r\n"
			f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
			f"{json.dumps(metadata, separators=(',', ':'))}\r\n"
			f"--{boundary}\r\n"
			f"Content-Type: {validated['content_type']}\r\n\r\n"
		).encode("utf-8") + data_bytes + f"\r\n--{boundary}--\r\n".encode("ascii")

		response = requests.post(
			GDRIVE_UPLOAD_URL,
			headers={
				"Authorization": f"Bearer {token}",
				"Content-Type": f"multipart/related; boundary={boundary}",
			},
			params={
				"uploadType": "multipart",
				"supportsAllDrives": "true",
				"fields": "id,name,size,mimeType",
			},
			data=body,
			timeout=60,
		)
		if response.status_code not in (200, 201):
			frappe.throw(
				_("Google Drive media upload failed with HTTP {0}.").format(response.status_code),
				frappe.ValidationError,
			)

		uploaded = response.json()
		storage_id = uploaded.get("id")
		if not storage_id:
			frappe.throw(
				_("Google Drive upload response missing file ID."),
				frappe.ValidationError,
			)

		return {
			"file_ref": validated["file_ref"],
			"backend": self.backend_name,
			"storage_id": str(storage_id),
			"file_url": None,
			"filename": validated["filename"],
			"content_type": validated["content_type"],
			"media_kind": validated["media_kind"],
			"size_bytes": validated["size_bytes"],
			"sha256": validated["sha256"],
			"folder_name": folder_name,
		}

	def open_stream(
		self,
		media_ref: dict[str, Any],
		*,
		range_header: str | None = None,
	) -> MediaStreamResult:
		"""Open a streaming download from Google Drive with optional HTTP Range passthrough."""
		storage_id = (media_ref.get("storage_id") or "").strip()
		if not storage_id:
			frappe.throw(_("Missing Google Drive storage_id for media reference."), frappe.ValidationError)

		token = self.get_access_token()
		headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
		if range_header and _RANGE_HEADER_RE.match(range_header.strip()):
			headers["Range"] = range_header.strip()

		response = requests.get(
			f"{GDRIVE_FILES_URL}/{storage_id}",
			headers=headers,
			params={
				"alt": "media",
				"supportsAllDrives": "true",
			},
			stream=True,
			timeout=60,
		)
		if response.status_code not in (200, 206):
			response.close()
			frappe.throw(
				_("Google Drive media download failed with HTTP {0}.").format(response.status_code),
				frappe.ValidationError,
			)

		content_length_raw = response.headers.get("Content-Length")
		content_length = int(content_length_raw) if content_length_raw and content_length_raw.isdigit() else None
		content_range = response.headers.get("Content-Range")
		content_type = media_ref.get("content_type") or response.headers.get("Content-Type") or "application/octet-stream"
		filename = media_ref.get("filename") or "media"

		return MediaStreamResult(
			status_code=response.status_code,
			content_type=content_type,
			filename=filename,
			chunks=response.iter_content(chunk_size=_STREAM_CHUNK_SIZE),
			content_length=content_length,
			content_range=content_range,
			accept_ranges=response.headers.get("Accept-Ranges") or "bytes",
			close_callback=response.close,
		)

	def download_bytes(self, media_ref: dict[str, Any], *, max_bytes: int | None = None) -> bytes:
		stream = self.open_stream(media_ref)
		return stream.read_all(max_bytes=max_bytes)


class FrappePrivateFileBackend:
	"""Dev/test/offline fallback storing media as Frappe private File documents."""

	backend_name = DEFAULT_MEDIA_BACKEND

	def __init__(self, settings: Any = None):
		self.settings = _load_settings(settings)

	def upload(
		self,
		validated: dict[str, Any],
		data_bytes: bytes,
		*,
		thread_key: str,
		repair_order: str | None = None,
		thread_name: str | None = None,
	) -> dict[str, Any]:
		"""Persist media as a private Frappe File attached to Repair Order or Shop Board Thread."""
		from frappe.utils.file_manager import save_file

		attached_dt: str | None = None
		attached_dn: str | None = None

		if repair_order and frappe.db.exists("Repair Order", repair_order):
			attached_dt = "Repair Order"
			attached_dn = repair_order
		elif (
			thread_name
			and frappe.db.exists("DocType", "Shop Board Thread")
			and frappe.db.exists("Shop Board Thread", thread_name)
		):
			attached_dt = "Shop Board Thread"
			attached_dn = thread_name

		stored_fname = f"{validated['file_ref']}_{validated['filename']}"
		file_doc = save_file(
			fname=stored_fname,
			content=data_bytes,
			dt=attached_dt,
			dn=attached_dn,
			decode=False,
			is_private=1,
		)

		return {
			"file_ref": validated["file_ref"],
			"backend": self.backend_name,
			"storage_id": file_doc.name,
			"file_url": file_doc.file_url,
			"filename": validated["filename"],
			"content_type": validated["content_type"],
			"media_kind": validated["media_kind"],
			"size_bytes": validated["size_bytes"],
			"sha256": validated["sha256"],
			"folder_name": derive_folder_name(thread_key=thread_key, repair_order=repair_order),
		}

	def _load_file_bytes(self, media_ref: dict[str, Any]) -> bytes:
		storage_id = (media_ref.get("storage_id") or "").strip()
		file_url = (media_ref.get("file_url") or "").strip()

		file_doc = None
		if storage_id and frappe.db.exists("File", storage_id):
			file_doc = frappe.get_doc("File", storage_id)
		elif file_url:
			file_name = frappe.db.get_value("File", {"file_url": file_url, "is_private": 1}, "name")
			if file_name:
				file_doc = frappe.get_doc("File", file_name)

		if file_doc is None:
			frappe.throw(_("Private media file not found."), frappe.DoesNotExistError)

		content = file_doc.get_content()
		if isinstance(content, str):
			return content.encode("utf-8")
		return bytes(content)

	def open_stream(
		self,
		media_ref: dict[str, Any],
		*,
		range_header: str | None = None,
	) -> MediaStreamResult:
		"""Stream private Frappe File bytes with full HTTP Range support for audio scrubbing."""
		raw_bytes = self._load_file_bytes(media_ref)
		total_size = len(raw_bytes)
		byte_range = _parse_range_header(range_header, total_size)

		if byte_range is not None:
			start, end = byte_range
			sliced = raw_bytes[start : end + 1]
			status_code = 206
			content_range = f"bytes {start}-{end}/{total_size}"
		else:
			sliced = raw_bytes
			status_code = 200
			content_range = None

		def _iter_chunks() -> Iterator[bytes]:
			for offset in range(0, len(sliced), _STREAM_CHUNK_SIZE):
				yield sliced[offset : offset + _STREAM_CHUNK_SIZE]

		return MediaStreamResult(
			status_code=status_code,
			content_type=media_ref.get("content_type") or "application/octet-stream",
			filename=media_ref.get("filename") or "media",
			chunks=_iter_chunks(),
			content_length=len(sliced),
			content_range=content_range,
			accept_ranges="bytes",
		)

	def download_bytes(self, media_ref: dict[str, Any], *, max_bytes: int | None = None) -> bytes:
		raw_bytes = self._load_file_bytes(media_ref)
		if max_bytes is not None and len(raw_bytes) > max_bytes:
			frappe.throw(
				_("Media payload exceeded maximum allowed size of {0} bytes.").format(max_bytes),
				frappe.ValidationError,
			)
		return raw_bytes


def get_media_storage_backend(
	settings: Any = None,
	*,
	backend_override: str | None = None,
) -> GoogleDriveStorageBackend | FrappePrivateFileBackend:
	"""Return the configured media storage backend (Google Drive or Frappe Private File)."""
	resolved_settings = _load_settings(settings)
	backend_name = backend_override or str(
		_get_setting(resolved_settings, "media_storage_backend", DEFAULT_MEDIA_BACKEND)
	)
	if backend_name == GOOGLE_DRIVE_BACKEND:
		return GoogleDriveStorageBackend(resolved_settings)
	return FrappePrivateFileBackend(resolved_settings)


def store_media_payload(
	filename: str | None,
	content_type: str | None,
	data_bytes: bytes,
	*,
	thread_key: str,
	repair_order: str | None = None,
	thread_name: str | None = None,
	settings: Any = None,
) -> dict[str, Any]:
	"""Validate and persist a media payload through the active storage backend."""
	resolved_settings = _load_settings(settings)
	validated = validate_media_payload(filename, content_type, data_bytes, resolved_settings)
	backend = get_media_storage_backend(resolved_settings)
	return backend.upload(
		validated,
		bytes(data_bytes),
		thread_key=thread_key,
		repair_order=repair_order,
		thread_name=thread_name,
	)


def _resolve_user_roles(user: str | None = None, roles: Iterable[str] | None = None) -> tuple[str, set[str]]:
	active_user = user or getattr(frappe.session, "user", None) or "Guest"
	if roles is not None:
		return active_user, set(roles)
	if active_user == "Guest":
		return active_user, {"Guest"}
	return active_user, set(frappe.get_roles(active_user))


def assert_media_access(user: str | None = None, *, roles: Iterable[str] | None = None) -> str:
	"""Verify the caller is an authenticated ISM staff member permitted to read board media."""
	active_user, user_roles = _resolve_user_roles(user, roles)
	if active_user == "Guest" or not user_roles.intersection(ALLOWED_MEDIA_READ_ROLES):
		frappe.throw(_("You are not permitted to access board media."), frappe.PermissionError)
	return active_user


def _assert_media_admin(user: str | None = None, *, roles: Iterable[str] | None = None) -> str:
	"""Verify the caller is an ISM Manager or System Manager."""
	active_user, user_roles = _resolve_user_roles(user, roles)
	if active_user == "Guest" or not user_roles.intersection(ALLOWED_MEDIA_ADMIN_ROLES):
		frappe.throw(
			_("Only ISM Manager or System Manager may run Google Drive diagnostics."),
			frappe.PermissionError,
		)
	return active_user


def _extract_media_refs(event_doc_or_name: Any) -> list[dict[str, Any]]:
	"""Extract parsed media_refs list from a Shop Board Event name, Document, or dict."""
	if isinstance(event_doc_or_name, str):
		if not frappe.db.exists("DocType", "Shop Board Event"):
			frappe.throw(_("Board event not found."), frappe.PermissionError)
		raw_refs = frappe.db.get_value("Shop Board Event", event_doc_or_name, "media_refs_json")
		if raw_refs is None:
			frappe.throw(_("Board event not found."), frappe.PermissionError)
	elif isinstance(event_doc_or_name, dict):
		raw_refs = event_doc_or_name.get("media_refs_json") or event_doc_or_name.get("media_refs")
	else:
		raw_refs = getattr(event_doc_or_name, "media_refs_json", None)

	if not raw_refs:
		return []
	if isinstance(raw_refs, list):
		return [item for item in raw_refs if isinstance(item, dict)]
	if isinstance(raw_refs, str):
		try:
			parsed = json.loads(raw_refs)
		except Exception:
			return []
		return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
	return []


def resolve_event_media_ref(
	event_doc_or_name: Any,
	file_ref: str,
	*,
	user: str | None = None,
	roles: Iterable[str] | None = None,
) -> dict[str, Any]:
	"""Validate caller RBAC and strict file_ref membership on the target event (IDOR protection)."""
	assert_media_access(user=user, roles=roles)
	if not isinstance(file_ref, str) or not _FILE_REF_RE.match(file_ref.strip()):
		frappe.throw(_("Invalid media file reference."), frappe.PermissionError)

	target_ref = file_ref.strip()
	for entry in _extract_media_refs(event_doc_or_name):
		if entry.get("file_ref") == target_ref:
			return entry

	frappe.throw(_("Media reference does not belong to the requested event."), frappe.PermissionError)


def stream_event_media(
	event_doc_or_name: Any,
	file_ref: str,
	*,
	range_header: str | None = None,
	user: str | None = None,
	roles: Iterable[str] | None = None,
	settings: Any = None,
) -> MediaStreamResult:
	"""Authorize and open a media stream for a verified event file_ref with Range passthrough."""
	media_ref = resolve_event_media_ref(event_doc_or_name, file_ref, user=user, roles=roles)
	backend = get_media_storage_backend(settings, backend_override=media_ref.get("backend"))
	return backend.open_stream(media_ref, range_header=range_header)


def fetch_event_media_bytes(
	event_doc_or_name: Any,
	file_ref: str,
	*,
	user: str | None = None,
	roles: Iterable[str] | None = None,
	max_bytes: int | None = None,
	settings: Any = None,
) -> tuple[dict[str, Any], bytes]:
	"""Authorize and download media bytes for server-side consumers (e.g., AI orchestrator)."""
	media_ref = resolve_event_media_ref(event_doc_or_name, file_ref, user=user, roles=roles)
	backend = get_media_storage_backend(settings, backend_override=media_ref.get("backend"))
	data = backend.download_bytes(media_ref, max_bytes=max_bytes)
	return media_ref, data


def build_media_http_response(stream_result: MediaStreamResult):
	"""Wrap a MediaStreamResult in a Werkzeug streaming Response suitable for Frappe endpoints."""
	from werkzeug.wrappers import Response

	def _generate() -> Iterator[bytes]:
		try:
			yield from stream_result.chunks
		finally:
			stream_result.close()

	safe_disp_name = sanitize_filename(stream_result.filename, stream_result.content_type)
	headers: dict[str, str] = {
		"Accept-Ranges": stream_result.accept_ranges or "bytes",
		"Cache-Control": "private, max-age=300",
		"X-Content-Type-Options": "nosniff",
		"Content-Disposition": f'inline; filename="{safe_disp_name}"',
	}
	if stream_result.content_length is not None:
		headers["Content-Length"] = str(stream_result.content_length)
	if stream_result.content_range:
		headers["Content-Range"] = stream_result.content_range

	return Response(
		_generate(),
		status=stream_result.status_code,
		mimetype=stream_result.content_type,
		headers=headers,
	)


def test_google_drive_connection(
	settings: Any = None,
	*,
	user: str | None = None,
	roles: Iterable[str] | None = None,
) -> dict[str, Any]:
	"""Manager-only diagnostic verifying Google Drive token exchange and root folder accessibility."""
	_assert_media_admin(user=user, roles=roles)
	resolved_settings = _load_settings(settings)
	backend = GoogleDriveStorageBackend(resolved_settings)

	root_id = backend.root_folder_id
	if not root_id:
		return {
			"ok": False,
			"auth_mode": backend.auth_mode,
			"root_folder_id": "",
			"error": _("Google Drive root folder ID (gdrive_root_folder_id) is not configured."),
		}

	try:
		token = backend.get_access_token(force_refresh=True)
		resp = requests.get(
			f"{GDRIVE_FILES_URL}/{root_id}",
			headers={"Authorization": f"Bearer {token}"},
			params={
				"fields": "id,name,mimeType,trashed",
				"supportsAllDrives": "true",
			},
			timeout=_HTTP_TIMEOUT_SECONDS,
		)
	except Exception as exc:
		return {
			"ok": False,
			"auth_mode": backend.auth_mode,
			"root_folder_id": root_id,
			"error": str(exc),
		}

	if resp.status_code != 200:
		return {
			"ok": False,
			"auth_mode": backend.auth_mode,
			"root_folder_id": root_id,
			"error": _("Google Drive root folder check failed with HTTP {0}.").format(resp.status_code),
		}

	folder_info = resp.json()
	if folder_info.get("trashed"):
		return {
			"ok": False,
			"auth_mode": backend.auth_mode,
			"root_folder_id": root_id,
			"error": _("Configured Google Drive root folder is in the trash."),
		}
	if folder_info.get("mimeType") != GDRIVE_FOLDER_MIME:
		return {
			"ok": False,
			"auth_mode": backend.auth_mode,
			"root_folder_id": root_id,
			"error": _("Configured gdrive_root_folder_id is not a Google Drive folder."),
		}

	return {
		"ok": True,
		"auth_mode": backend.auth_mode,
		"root_folder_id": str(folder_info.get("id") or root_id),
		"root_folder_name": str(folder_info.get("name") or ""),
		"token_cached": bool(frappe.cache.get_value(GDRIVE_ACCESS_TOKEN_CACHE_KEY)),
	}
