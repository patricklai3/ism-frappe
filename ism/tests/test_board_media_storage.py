"""Tests for Wave 1B Google Drive media storage, validation, Range proxy, IDOR, and Private File fallback."""

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ism.board import media_storage
from ism.tests.base import ISMTestCase

SAMPLE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
SAMPLE_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
SAMPLE_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00"
SAMPLE_WEBM = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01"
SAMPLE_OGG = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04"
SAMPLE_MP4 = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42isom"
SAMPLE_MP3_ID3 = b"ID3\x03\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x00"
SAMPLE_MP3_FRAME = b"\xff\xfb\x90\x64\x00\x00\x00\x00\x00\x00\x00\x00"
SAMPLE_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"


def _make_mock_response(
	status_code: int = 200,
	*,
	json_data: dict | None = None,
	content: bytes = b"",
	headers: dict[str, str] | None = None,
) -> MagicMock:
	resp = MagicMock()
	resp.status_code = status_code
	resp.headers = headers or {}
	resp.json.return_value = json_data if json_data is not None else {}
	resp.content = content
	resp.iter_content.side_effect = lambda chunk_size=65536: iter(
		[content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]
	)
	resp.close = MagicMock()
	return resp


class TestBoardMediaStorage(ISMTestCase):
	"""Comprehensive unit and integration tests for ism.board.media_storage."""

	def setUp(self):
		super().setUp()
		self._created_files: list[str] = []
		self._clear_gdrive_cache()

	def tearDown(self):
		self._clear_gdrive_cache()
		for file_name in reversed(self._created_files):
			if frappe.db.exists("File", file_name):
				frappe.delete_doc("File", file_name, force=True, ignore_permissions=True)
		super().tearDown()

	def _clear_gdrive_cache(self):
		frappe.cache.delete_value(media_storage.GDRIVE_ACCESS_TOKEN_CACHE_KEY)
		for folder in ("RO-2026-0001", "RO-TEST-001", "SHOP_INTAKE", "SHOP_AP_RECEIVING"):
			frappe.cache.delete_value(f"{media_storage.GDRIVE_FOLDER_CACHE_PREFIX}{folder}")

	# -------------------------------------------------------------------------
	# 1. Validation: MIME allowlist, magic bytes, size caps, sanitization, SHA-256
	# -------------------------------------------------------------------------

	def test_validate_accepts_all_allowlisted_image_and_audio_types(self):
		cases = [
			("photo.jpg", "image/jpeg", SAMPLE_JPEG, "image", ".jpg"),
			("shot.png", "image/png", SAMPLE_PNG, "image", ".png"),
			("pic.webp", "image/webp", SAMPLE_WEBP, "image", ".webp"),
			("note.webm", "audio/webm; codecs=opus", SAMPLE_WEBM, "audio", ".webm"),
			("voice.ogg", "audio/ogg", SAMPLE_OGG, "audio", ".ogg"),
			("memo.mp4", "audio/mp4", SAMPLE_MP4, "audio", ".mp4"),
			("memo.m4a", "audio/x-m4a", SAMPLE_MP4, "audio", ".m4a"),
			("clip1.mp3", "audio/mpeg", SAMPLE_MP3_ID3, "audio", ".mp3"),
			("clip2.mp3", "audio/mpeg", SAMPLE_MP3_FRAME, "audio", ".mp3"),
			("clip.wav", "audio/wav", SAMPLE_WAV, "audio", ".wav"),
		]
		settings = SimpleNamespace()
		seen_refs: set[str] = set()

		for fname, ctype, payload, expected_kind, expected_ext in cases:
			result = media_storage.validate_media_payload(fname, ctype, payload, settings)
			self.assertEqual(result["media_kind"], expected_kind)
			self.assertTrue(result["filename"].endswith(expected_ext))
			self.assertEqual(result["size_bytes"], len(payload))
			self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
			self.assertRegex(result["file_ref"], r"^m_[A-Za-z0-9]{16}$")
			self.assertNotIn(result["file_ref"], seen_refs)
			seen_refs.add(result["file_ref"])

	def test_validate_rejects_disallowed_mime_and_magic_spoofing(self):
		settings = SimpleNamespace()

		# Disallowed MIME types
		for bad_mime, payload in [
			("image/svg+xml", b"<svg onload=alert(1)></svg>"),
			("text/html", b"<html><body>hi</body></html>"),
			("application/pdf", b"%PDF-1.7\n"),
			("application/octet-stream", b"\x00\x01\x02\x03"),
		]:
			with self.assertRaises(frappe.ValidationError):
				media_storage.validate_media_payload("bad.file", bad_mime, payload, settings)

		# Magic-byte mismatches (spoofed Content-Type)
		spoof_cases = [
			("fake.jpg", "image/jpeg", SAMPLE_PNG),
			("fake.png", "image/png", b"<html>not a png</html>"),
			("fake.webp", "image/webp", SAMPLE_WAV),
			("fake.wav", "audio/wav", SAMPLE_WEBP),
			("fake.ogg", "audio/ogg", b"not ogg data at all"),
			("fake.mp4", "audio/mp4", b"0123456789abcdef"),
			("fake.webm", "audio/webm", b"plain text pretending to be webm"),
		]
		for fname, ctype, payload in spoof_cases:
			with self.assertRaises(frappe.ValidationError):
				media_storage.validate_media_payload(fname, ctype, payload, settings)

		# Empty payload
		with self.assertRaises(frappe.ValidationError):
			media_storage.validate_media_payload("empty.jpg", "image/jpeg", b"", settings)

	def test_validate_enforces_size_caps_and_sanitizes_filename(self):
		settings = SimpleNamespace(media_max_image_mb=1, media_max_audio_mb=1)

		oversized_jpeg = SAMPLE_JPEG + (b"\x00" * (1024 * 1024))
		with self.assertRaises(frappe.ValidationError):
			media_storage.validate_media_payload("big.jpg", "image/jpeg", oversized_jpeg, settings)

		oversized_ogg = SAMPLE_OGG + (b"\x00" * (1024 * 1024))
		with self.assertRaises(frappe.ValidationError):
			media_storage.validate_media_payload("big.ogg", "audio/ogg", oversized_ogg, settings)

		# Path traversal & unsafe chars stripped; extension normalized
		validated = media_storage.validate_media_payload(
			"../../Windows\\System32\\my photo (front LH)!.exe",
			"image/png",
			SAMPLE_PNG,
			settings,
		)
		self.assertNotIn("/", validated["filename"])
		self.assertNotIn("\\", validated["filename"])
		self.assertNotIn("..", validated["filename"])
		self.assertTrue(validated["filename"].endswith(".png"))

	# -------------------------------------------------------------------------
	# 2. Google Drive Auth: OAuth2 Refresh Token & Service Account RS256 JWT + Redis Cache
	# -------------------------------------------------------------------------

	@patch("ism.board.media_storage.requests.post")
	def test_gdrive_oauth2_refresh_token_and_redis_cache(self, mock_post):
		mock_post.return_value = _make_mock_response(
			200,
			json_data={"access_token": "ya29.oauth-token-1", "expires_in": 3600},
		)
		settings = SimpleNamespace(
			media_storage_backend="Google Drive",
			gdrive_auth_mode="OAuth2 Refresh Token",
			gdrive_client_id="client-id-123",
			gdrive_client_secret="client-secret-456",
			gdrive_refresh_token="refresh-token-789",
			gdrive_root_folder_id="root-folder-abc",
		)
		backend = media_storage.GoogleDriveStorageBackend(settings)

		token1 = backend.get_access_token()
		token2 = backend.get_access_token()

		self.assertEqual(token1, "ya29.oauth-token-1")
		self.assertEqual(token2, "ya29.oauth-token-1")
		self.assertEqual(mock_post.call_count, 1)
		self.assertEqual(
			frappe.cache.get_value(media_storage.GDRIVE_ACCESS_TOKEN_CACHE_KEY),
			"ya29.oauth-token-1",
		)

		called_url = mock_post.call_args.args[0]
		called_data = mock_post.call_args.kwargs["data"]
		self.assertEqual(called_url, media_storage.GDRIVE_TOKEN_URL)
		self.assertEqual(called_data["grant_type"], "refresh_token")
		self.assertEqual(called_data["client_id"], "client-id-123")
		self.assertEqual(called_data["client_secret"], "client-secret-456")
		self.assertEqual(called_data["refresh_token"], "refresh-token-789")

	@patch("ism.board.media_storage.requests.post")
	def test_gdrive_service_account_rs256_jwt_flow(self, mock_post):
		private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
		private_pem = private_key.private_bytes(
			encoding=serialization.Encoding.PEM,
			format=serialization.PrivateFormat.PKCS8,
			encryption_algorithm=serialization.NoEncryption(),
		).decode("utf-8")

		sa_json = json.dumps(
			{
				"client_email": "ism-board@test-project.iam.gserviceaccount.com",
				"private_key": private_pem,
				"private_key_id": "key-id-99",
				"token_uri": "https://oauth2.googleapis.com/token",
			}
		)
		mock_post.return_value = _make_mock_response(
			200,
			json_data={"access_token": "ya29.sa-token-1", "expires_in": 3600},
		)

		settings = SimpleNamespace(
			media_storage_backend="Google Drive",
			gdrive_auth_mode="Service Account JSON",
			gdrive_service_account_json=sa_json,
			gdrive_root_folder_id="shared-drive-root",
		)
		backend = media_storage.GoogleDriveStorageBackend(settings)
		token = backend.get_access_token()

		self.assertEqual(token, "ya29.sa-token-1")
		self.assertEqual(mock_post.call_count, 1)

		sent_data = mock_post.call_args.kwargs["data"]
		self.assertEqual(sent_data["grant_type"], "urn:ietf:params:oauth:grant-type:jwt-bearer")
		jwt_assertion = sent_data["assertion"]
		parts = jwt_assertion.split(".")
		self.assertEqual(len(parts), 3)

		def _b64url_decode(segment: str) -> bytes:
			pad = "=" * (-len(segment) % 4)
			return base64.urlsafe_b64decode(segment + pad)

		header = json.loads(_b64url_decode(parts[0]))
		claims = json.loads(_b64url_decode(parts[1]))
		sig = _b64url_decode(parts[2])

		self.assertEqual(header["alg"], "RS256")
		self.assertEqual(header["kid"], "key-id-99")
		self.assertEqual(claims["iss"], "ism-board@test-project.iam.gserviceaccount.com")
		self.assertEqual(claims["scope"], media_storage.GDRIVE_SCOPE)
		self.assertEqual(claims["aud"], "https://oauth2.googleapis.com/token")
		self.assertEqual(claims["exp"] - claims["iat"], 3600)

		# Cryptographically verify RS256 signature with matching public key
		private_key.public_key().verify(
			sig,
			f"{parts[0]}.{parts[1]}".encode("ascii"),
			padding.PKCS1v15(),
			hashes.SHA256(),
		)

	# -------------------------------------------------------------------------
	# 3. Folder Cache, Multipart Upload (Zero Local Disk Writes), Range Proxy Stream
	# -------------------------------------------------------------------------

	@patch("frappe.utils.file_manager.save_file")
	@patch("ism.board.media_storage.requests.post")
	@patch("ism.board.media_storage.requests.get")
	def test_gdrive_folder_create_cache_and_multipart_upload_zero_disk_writes(
		self, mock_get, mock_post, mock_save_file
	):
		frappe.cache.set_value(media_storage.GDRIVE_ACCESS_TOKEN_CACHE_KEY, "ya29.cached-token", expires_in_sec=300)

		# Folder lookup returns empty -> creates folder -> then uploads file
		mock_get.return_value = _make_mock_response(200, json_data={"files": []})
		mock_post.side_effect = [
			_make_mock_response(200, json_data={"id": "folder-ro-0001", "name": "RO-2026-0001"}),
			_make_mock_response(200, json_data={"id": "gdrive-file-1", "name": "f1.jpg"}),
			_make_mock_response(200, json_data={"id": "gdrive-file-2", "name": "f2.jpg"}),
		]

		settings = SimpleNamespace(
			media_storage_backend="Google Drive",
			gdrive_root_folder_id="root-folder-1",
		)

		ref1 = media_storage.store_media_payload(
			"bushing.jpg",
			"image/jpeg",
			SAMPLE_JPEG,
			thread_key="RO:RO-2026-0001",
			repair_order="RO-2026-0001",
			settings=settings,
		)
		# Second upload in same thread must reuse cached folder ID without calling GET again
		ref2 = media_storage.store_media_payload(
			"bushing2.jpg",
			"image/jpeg",
			SAMPLE_JPEG,
			thread_key="RO:RO-2026-0001",
			repair_order="RO-2026-0001",
			settings=settings,
		)

		# Zero local disk writes via Frappe file manager
		mock_save_file.assert_not_called()

		self.assertEqual(ref1["backend"], "Google Drive")
		self.assertEqual(ref1["storage_id"], "gdrive-file-1")
		self.assertIsNone(ref1["file_url"])
		self.assertEqual(ref1["folder_name"], "RO-2026-0001")
		self.assertEqual(ref2["storage_id"], "gdrive-file-2")

		# Folder lookup happened exactly once with Shared Drives flags
		self.assertEqual(mock_get.call_count, 1)
		get_params = mock_get.call_args.kwargs["params"]
		self.assertEqual(get_params["supportsAllDrives"], "true")
		self.assertEqual(get_params["includeItemsFromAllDrives"], "true")
		self.assertEqual(
			frappe.cache.get_value(f"{media_storage.GDRIVE_FOLDER_CACHE_PREFIX}RO-2026-0001"),
			"folder-ro-0001",
		)

		# Verify multipart upload payload contains metadata + raw JPEG bytes
		upload_call = mock_post.call_args_list[1]
		self.assertEqual(upload_call.args[0], media_storage.GDRIVE_UPLOAD_URL)
		self.assertEqual(upload_call.kwargs["params"]["uploadType"], "multipart")
		self.assertEqual(upload_call.kwargs["params"]["supportsAllDrives"], "true")
		self.assertIn("multipart/related; boundary=", upload_call.kwargs["headers"]["Content-Type"])
		upload_body = upload_call.kwargs["data"]
		self.assertIn(b"folder-ro-0001", upload_body)
		self.assertIn(SAMPLE_JPEG, upload_body)

	@patch("ism.board.media_storage.requests.get")
	def test_gdrive_range_header_proxy_streaming(self, mock_get):
		frappe.cache.set_value(media_storage.GDRIVE_ACCESS_TOKEN_CACHE_KEY, "ya29.stream-token", expires_in_sec=300)
		partial_bytes = SAMPLE_OGG[4:12]
		mock_get.return_value = _make_mock_response(
			206,
			content=partial_bytes,
			headers={
				"Content-Type": "audio/ogg",
				"Content-Length": str(len(partial_bytes)),
				"Content-Range": f"bytes 4-11/{len(SAMPLE_OGG)}",
				"Accept-Ranges": "bytes",
			},
		)

		event_stub = {
			"name": "SBE-TEST-001",
			"media_refs_json": json.dumps(
				[
					{
						"file_ref": "m_0123456789abcdef",
						"backend": "Google Drive",
						"storage_id": "gdrive-audio-99",
						"filename": "voice.ogg",
						"content_type": "audio/ogg",
						"media_kind": "audio",
					}
				]
			),
		}

		stream = media_storage.stream_event_media(
			event_stub,
			"m_0123456789abcdef",
			range_header="bytes=4-11",
			user="tech@example.com",
			roles=["ISM Technician"],
			settings=SimpleNamespace(media_storage_backend="Google Drive"),
		)
		response = media_storage.build_media_http_response(stream)

		self.assertEqual(response.status_code, 206)
		self.assertEqual(response.headers.get("Content-Range"), f"bytes 4-11/{len(SAMPLE_OGG)}")
		self.assertEqual(response.headers.get("Accept-Ranges"), "bytes")
		self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
		self.assertEqual(response.get_data(), partial_bytes)

		called_headers = mock_get.call_args.kwargs["headers"]
		called_params = mock_get.call_args.kwargs["params"]
		self.assertEqual(called_headers["Range"], "bytes=4-11")
		self.assertEqual(called_params["alt"], "media")
		self.assertEqual(called_params["supportsAllDrives"], "true")

	# -------------------------------------------------------------------------
	# 4. IDOR & RBAC Enforcement
	# -------------------------------------------------------------------------

	def test_idor_and_unauthorized_user_rejection(self):
		event_stub = {
			"name": "SBE-TEST-IDOR",
			"media_refs_json": json.dumps(
				[
					{
						"file_ref": "m_aaaabbbbccccdddd",
						"backend": "Frappe Private File",
						"storage_id": "FILE-0001",
						"filename": "photo.jpg",
						"content_type": "image/jpeg",
					}
				]
			),
		}

		# Guest rejected
		with self.assertRaises(frappe.PermissionError):
			media_storage.resolve_event_media_ref(event_stub, "m_aaaabbbbccccdddd", user="Guest")

		# Unauthorized role (e.g. Customer) rejected
		with self.assertRaises(frappe.PermissionError):
			media_storage.resolve_event_media_ref(
				event_stub,
				"m_aaaabbbbccccdddd",
				user="customer@example.com",
				roles=["Customer"],
			)

		# Mismatched file_ref (IDOR attempt against another event's file) rejected
		with self.assertRaises(frappe.PermissionError):
			media_storage.resolve_event_media_ref(
				event_stub,
				"m_9999888877776666",
				user="tech@example.com",
				roles=["ISM Technician"],
			)

		# Malformed / traversal file_ref rejected
		with self.assertRaises(frappe.PermissionError):
			media_storage.resolve_event_media_ref(
				event_stub,
				"../private/files/secret.jpg",
				user="manager@example.com",
				roles=["ISM Manager"],
			)

		# Authorized staff with matching file_ref succeeds
		resolved = media_storage.resolve_event_media_ref(
			event_stub,
			"m_aaaabbbbccccdddd",
			user="advisor@example.com",
			roles=["ISM Service Advisor"],
		)
		self.assertEqual(resolved["storage_id"], "FILE-0001")

	# -------------------------------------------------------------------------
	# 5. FrappePrivateFileBackend Round-Trip (Attached to RO & Null RO fallback)
	# -------------------------------------------------------------------------

	def test_frappe_private_file_backend_roundtrip_and_range_slicing(self):
		customer = self.create_test_customer()
		vehicle = self.create_test_vehicle(customer=customer.name)
		ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)

		settings = SimpleNamespace(media_storage_backend="Frappe Private File")

		# Upload attached to Repair Order
		media_ref = media_storage.store_media_payload(
			"inspection_photo.png",
			"image/png",
			SAMPLE_PNG,
			thread_key=f"RO:{ro.name}",
			repair_order=ro.name,
			settings=settings,
		)
		self._created_files.append(media_ref["storage_id"])

		self.assertEqual(media_ref["backend"], "Frappe Private File")
		self.assertTrue(media_ref["file_url"].startswith("/private/files/"))
		file_doc = frappe.get_doc("File", media_ref["storage_id"])
		self.assertEqual(file_doc.is_private, 1)
		self.assertEqual(file_doc.attached_to_doctype, "Repair Order")
		self.assertEqual(file_doc.attached_to_name, ro.name)

		# Full download via authenticated event helper
		event_stub = {"name": "SBE-LOCAL-1", "media_refs_json": json.dumps([media_ref])}
		resolved_ref, downloaded = media_storage.fetch_event_media_bytes(
			event_stub,
			media_ref["file_ref"],
			user="tech@example.com",
			roles=["ISM Technician"],
			settings=settings,
		)
		self.assertEqual(resolved_ref["file_ref"], media_ref["file_ref"])
		self.assertEqual(downloaded, SAMPLE_PNG)

		# Range streaming on FrappePrivateFileBackend (206 Partial Content)
		stream = media_storage.stream_event_media(
			event_stub,
			media_ref["file_ref"],
			range_header="bytes=0-7",
			user="tech@example.com",
			roles=["ISM Technician"],
			settings=settings,
		)
		resp = media_storage.build_media_http_response(stream)
		self.assertEqual(resp.status_code, 206)
		self.assertEqual(resp.headers.get("Content-Range"), f"bytes 0-7/{len(SAMPLE_PNG)}")
		self.assertEqual(resp.get_data(), SAMPLE_PNG[:8])

		# Upload with repair_order=None (gracefully handles Shop Board Thread absent in Wave 1B)
		orphan_ref = media_storage.store_media_payload(
			"intake_note.ogg",
			"audio/ogg",
			SAMPLE_OGG,
			thread_key="SHOP:INTAKE",
			repair_order=None,
			thread_name="SHOP:INTAKE",
			settings=settings,
		)
		self._created_files.append(orphan_ref["storage_id"])
		self.assertEqual(orphan_ref["folder_name"], "SHOP_INTAKE")
		orphan_doc = frappe.get_doc("File", orphan_ref["storage_id"])
		self.assertEqual(orphan_doc.is_private, 1)

	# -------------------------------------------------------------------------
	# 6. test_google_drive_connection() Diagnostic & RBAC
	# -------------------------------------------------------------------------

	@patch("ism.board.media_storage.requests.get")
	@patch("ism.board.media_storage.requests.post")
	def test_google_drive_connection_diagnostic_and_rbac(self, mock_post, mock_get):
		settings = SimpleNamespace(
			media_storage_backend="Google Drive",
			gdrive_auth_mode="OAuth2 Refresh Token",
			gdrive_client_id="cid",
			gdrive_client_secret="csec",
			gdrive_refresh_token="rtok",
			gdrive_root_folder_id="root-123",
		)

		# Technician and Guest blocked
		with self.assertRaises(frappe.PermissionError):
			media_storage.test_google_drive_connection(
				settings, user="tech@example.com", roles=["ISM Technician"]
			)
		with self.assertRaises(frappe.PermissionError):
			media_storage.test_google_drive_connection(settings, user="Guest")

		# Manager succeeds
		mock_post.return_value = _make_mock_response(
			200, json_data={"access_token": "ya29.diag-token", "expires_in": 3600}
		)
		mock_get.return_value = _make_mock_response(
			200,
			json_data={
				"id": "root-123",
				"name": "ISM Shop Media",
				"mimeType": media_storage.GDRIVE_FOLDER_MIME,
				"trashed": False,
			},
		)
		res = media_storage.test_google_drive_connection(
			settings, user="mgr@example.com", roles=["ISM Manager"]
		)
		self.assertTrue(res["ok"])
		self.assertEqual(res["root_folder_id"], "root-123")
		self.assertEqual(res["root_folder_name"], "ISM Shop Media")
		self.assertTrue(res["token_cached"])

		# Trashed root folder returns ok=False cleanly
		mock_get.return_value = _make_mock_response(
			200,
			json_data={
				"id": "root-123",
				"name": "ISM Shop Media",
				"mimeType": media_storage.GDRIVE_FOLDER_MIME,
				"trashed": True,
			},
		)
		res_trashed = media_storage.test_google_drive_connection(
			settings, user="mgr@example.com", roles=["System Manager"]
		)
		self.assertFalse(res_trashed["ok"])
		self.assertIn("trash", res_trashed["error"].lower())
