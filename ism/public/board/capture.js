/**
 * Multimodal Capture Controller for /board:
 * - Press-and-hold + tap-toggle Push-to-Talk (MediaRecorder)
 * - Camera input with offscreen canvas compression (max 1600px, 0.82 quality)
 * - Multi-photo staging tray for atomic photo + voice/text submission
 * - Context-aware quick-intent pills & silent metadata injection
 */

import { generateIdempotencyKey, postBoardCapture } from "./api.js";

const ACTIVE_RO_INTENT_PILLS = [
	{ label: "Blocker", value: "BLOCKER" },
	{ label: "Parts", value: "PARTS" },
	{ label: "Supplement", value: "SUPPLEMENT" },
	{ label: "Question", value: "QUESTION" },
	{ label: "FYI", value: "FYI" },
];

const AUDIO_MIME_CANDIDATES = [
	{ mime: "audio/webm;codecs=opus", ext: "webm" },
	{ mime: "audio/webm", ext: "webm" },
	{ mime: "audio/mp4", ext: "m4a" },
	{ mime: "audio/ogg;codecs=opus", ext: "ogg" },
	{ mime: "audio/ogg", ext: "ogg" },
];

const MAX_IMAGE_DIM = 1600;
const IMAGE_QUALITY = 0.82;
const HOLD_THRESHOLD_MS = 350;

function pickAudioMime() {
	if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") {
		return { mime: "", ext: "webm" };
	}
	for (const candidate of AUDIO_MIME_CANDIDATES) {
		if (MediaRecorder.isTypeSupported(candidate.mime)) {
			return candidate;
		}
	}
	return { mime: "", ext: "webm" };
}

function formatTimer(seconds) {
	const mins = Math.floor(seconds / 60);
	const secs = seconds % 60;
	return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

async function compressImageFile(file) {
	const objectUrl = URL.createObjectURL(file);
	try {
		const img = await new Promise((resolve, reject) => {
			const el = new Image();
			el.onload = () => resolve(el);
			el.onerror = reject;
			el.src = objectUrl;
		});

		let { width, height } = img;
		if (width > MAX_IMAGE_DIM || height > MAX_IMAGE_DIM) {
			if (width >= height) {
				height = Math.round((height * MAX_IMAGE_DIM) / width);
				width = MAX_IMAGE_DIM;
			} else {
				width = Math.round((width * MAX_IMAGE_DIM) / height);
				height = MAX_IMAGE_DIM;
			}
		}

		const canvas = document.createElement("canvas");
		canvas.width = Math.max(1, width);
		canvas.height = Math.max(1, height);
		const ctx = canvas.getContext("2d");
		ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

		const tryEncode = (mimeType) =>
			new Promise((resolve) => canvas.toBlob((blob) => resolve(blob), mimeType, IMAGE_QUALITY));

		let blob = await tryEncode("image/jpeg");
		let ext = "jpg";
		if (!blob) {
			blob = await tryEncode("image/webp");
			ext = "webp";
		}
		if (!blob) {
			return { blob: file, filename: file.name || "photo.jpg", previewUrl: objectUrl, keepUrl: true };
		}

		const base = (file.name || "photo").replace(/\.[^.]+$/, "");
		const filename = `${base}.${ext}`;
		const previewUrl = URL.createObjectURL(blob);
		return { blob, filename, previewUrl, keepUrl: false };
	} finally {
		// Revoked below only if not kept as previewUrl
		URL.revokeObjectURL(objectUrl);
	}
}

export class BoardCaptureController {
	constructor({
		getContext,
		onCaptureSent,
		onToast,
		elements,
	}) {
		this.getContext = getContext;
		this.onCaptureSent = onCaptureSent;
		this.onToast = onToast || (() => {});
		this.el = elements;

		this.selectedIntent = null;
		this.stagedPhotos = [];
		this.isSending = false;
		this.isReadOnly = false;
		this.lastFailedCapture = null;

		// PTT recording state
		this.isRecording = false;
		this.mediaRecorder = null;
		this.mediaStream = null;
		this.audioChunks = [];
		this.audioMime = { mime: "", ext: "webm" };
		this.recordStartTime = 0;
		this.recordTimerId = null;
		this.pointerDownAt = 0;
		this.cancelRequested = false;

		this._bindEvents();
	}

	setThreadType(_threadType) {
		const pills = ACTIVE_RO_INTENT_PILLS;
		this.selectedIntent = null;
		this.el.intentPills.innerHTML = "";

		for (const pill of pills) {
			const btn = document.createElement("button");
			btn.type = "button";
			btn.className = "intent-pill";
			btn.textContent = pill.label;
			btn.dataset.intent = pill.value;
			btn.disabled = this.isReadOnly;
			btn.addEventListener("click", () => {
				if (this.isReadOnly) return;
				if (this.selectedIntent === pill.value) {
					this.selectedIntent = null;
					btn.classList.remove("is-active");
				} else {
					this.selectedIntent = pill.value;
					for (const child of this.el.intentPills.querySelectorAll(".intent-pill")) {
						child.classList.toggle("is-active", child.dataset.intent === pill.value);
					}
				}
			});
			this.el.intentPills.appendChild(btn);
		}
	}

	setReadOnly(locked, reason = "") {
		this.isReadOnly = Boolean(locked);
		if (this.isReadOnly && this.isRecording) {
			this._stopRecording({ send: false });
		}
		if (this.el.pttBtn) this.el.pttBtn.disabled = this.isReadOnly;
		if (this.el.cameraInput) {
			this.el.cameraInput.disabled = this.isReadOnly;
			const camLabel = this.el.cameraInput.closest(".capture-btn--cam");
			if (camLabel) camLabel.classList.toggle("is-disabled", this.isReadOnly);
		}
		if (this.el.textInput) this.el.textInput.disabled = this.isReadOnly;
		if (this.el.sendBtn) this.el.sendBtn.disabled = this.isReadOnly || this.isSending;
		if (this.el.intentPills) {
			for (const btn of this.el.intentPills.querySelectorAll(".intent-pill")) {
				btn.disabled = this.isReadOnly;
			}
		}
		if (this.el.readonlyBanner) {
			if (this.isReadOnly) {
				this.el.readonlyBanner.textContent =
					reason || "This Repair Order is closed or cancelled. Captures are disabled.";
				this.el.readonlyBanner.classList.remove("is-hidden");
			} else {
				this.el.readonlyBanner.textContent = "";
				this.el.readonlyBanner.classList.add("is-hidden");
			}
		}
	}

	async retryLastFailed() {
		if (!this.lastFailedCapture || this.isSending) return false;
		const payload = this.lastFailedCapture;
		this.lastFailedCapture = null;
		await this._submitCapture(payload);
		return true;
	}

	_bindEvents() {
		this.el.sendBtn.addEventListener("click", () => this.sendCurrent());

		this.el.textInput.addEventListener("keydown", (evt) => {
			if (evt.key === "Enter" && !evt.shiftKey) {
				evt.preventDefault();
				this.sendCurrent();
			}
		});

		this.el.cameraInput.addEventListener("change", async (evt) => {
			const files = Array.from(evt.target.files || []);
			evt.target.value = "";
			if (!files.length) return;
			await this._stagePhotos(files);
		});

		// Press-and-hold + tap-toggle PTT
		this.el.pttBtn.addEventListener("pointerdown", (evt) => {
			if (evt.button !== 0 && evt.pointerType === "mouse") return;
			evt.preventDefault();
			this.pointerDownAt = Date.now();
			if (!this.isRecording) {
				this._startRecording();
			}
		});

		this.el.pttBtn.addEventListener("pointerup", (evt) => {
			evt.preventDefault();
			if (!this.isRecording) return;
			const heldMs = Date.now() - this.pointerDownAt;
			if (heldMs >= HOLD_THRESHOLD_MS) {
				this._stopRecording({ send: true });
			}
		});

		this.el.pttBtn.addEventListener("click", (evt) => {
			evt.preventDefault();
			const heldMs = Date.now() - this.pointerDownAt;
			// Second short tap stops & sends
			if (this.isRecording && heldMs < HOLD_THRESHOLD_MS && Date.now() - this.recordStartTime > 400) {
				this._stopRecording({ send: true });
			}
		});

		this.el.pttCancelBtn.addEventListener("click", () => {
			this._stopRecording({ send: false });
		});

		this.el.pttStopSendBtn.addEventListener("click", () => {
			this._stopRecording({ send: true });
		});
	}

	_showNotice(message) {
		if (!message) {
			this.el.captureNotice.classList.add("is-hidden");
			this.el.captureNotice.textContent = "";
			return;
		}
		this.el.captureNotice.textContent = message;
		this.el.captureNotice.classList.remove("is-hidden");
	}

	async _stagePhotos(files) {
		this._showNotice("");
		for (const file of files) {
			if (!file.type || !file.type.startsWith("image/")) {
				this.onToast(`Skipped unsupported file: ${file.name}`, "warn");
				continue;
			}
			try {
				const compressed = await compressImageFile(file);
				const previewUrl = compressed.keepUrl
					? URL.createObjectURL(compressed.blob)
					: compressed.previewUrl;
				this.stagedPhotos.push({
					id: generateIdempotencyKey(),
					blob: compressed.blob,
					filename: compressed.filename,
					previewUrl,
				});
			} catch (_err) {
				this.onToast(`Could not process image ${file.name}`, "error");
			}
		}
		this._renderPhotoTray();
	}

	_renderPhotoTray() {
		const tray = this.el.photoTray;
		tray.innerHTML = "";
		if (!this.stagedPhotos.length) {
			tray.classList.add("is-hidden");
			return;
		}
		tray.classList.remove("is-hidden");

		for (const item of this.stagedPhotos) {
			const wrap = document.createElement("div");
			wrap.className = "photo-tray__item";

			const img = document.createElement("img");
			img.src = item.previewUrl;
			img.alt = item.filename;

			const removeBtn = document.createElement("button");
			removeBtn.type = "button";
			removeBtn.className = "photo-tray__remove";
			removeBtn.setAttribute("aria-label", `Remove ${item.filename}`);
			removeBtn.textContent = "×";
			removeBtn.addEventListener("click", () => {
				URL.revokeObjectURL(item.previewUrl);
				this.stagedPhotos = this.stagedPhotos.filter((p) => p.id !== item.id);
				this._renderPhotoTray();
			});

			wrap.appendChild(img);
			wrap.appendChild(removeBtn);
			tray.appendChild(wrap);
		}
	}

	_clearStagedPhotos() {
		for (const item of this.stagedPhotos) {
			URL.revokeObjectURL(item.previewUrl);
		}
		this.stagedPhotos = [];
		this._renderPhotoTray();
	}

	async _startRecording() {
		if (this.isReadOnly) return;
		this._showNotice("");
		if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === "undefined") {
			this._showNotice("Voice recording is not supported in this browser. Type your update or attach a photo.");
			return;
		}

		try {
			this.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
		} catch (err) {
			const denied = err && (err.name === "NotAllowedError" || err.name === "PermissionDeniedError");
			this._showNotice(
				denied
					? "Microphone permission denied. Allow mic access in browser settings or type your update."
					: "Unable to access microphone. Check audio device or type your update."
			);
			return;
		}

		this.audioMime = pickAudioMime();
		this.audioChunks = [];
		this.cancelRequested = false;

		const options = this.audioMime.mime ? { mimeType: this.audioMime.mime } : {};
		try {
			this.mediaRecorder = new MediaRecorder(this.mediaStream, options);
		} catch (_err) {
			this.mediaRecorder = new MediaRecorder(this.mediaStream);
		}

		this.mediaRecorder.ondataavailable = (evt) => {
			if (evt.data && evt.data.size > 0) {
				this.audioChunks.push(evt.data);
			}
		};

		this.mediaRecorder.onstop = () => {
			const chunks = this.audioChunks;
			const cancelled = this.cancelRequested;
			this._cleanupRecordingUi();

			if (cancelled || !chunks.length) {
				return;
			}
			const blobType = this.audioMime.mime || chunks[0].type || "audio/webm";
			const audioBlob = new Blob(chunks, { type: blobType });
			if (audioBlob.size < 64) {
				this._showNotice("Voice clip was too short. Hold or tap PTT and speak clearly.");
				return;
			}
			const filename = `voice-${Date.now()}.${this.audioMime.ext || "webm"}`;
			this.sendCurrent({ extraFile: { blob: audioBlob, filename } });
		};

		this.isRecording = true;
		this.recordStartTime = Date.now();
		this.el.pttBtn.classList.add("is-recording");
		this.el.pttRecordingBar.classList.remove("is-hidden");
		this.el.pttTimer.textContent = "00:00";

		this.recordTimerId = setInterval(() => {
			const elapsed = Math.floor((Date.now() - this.recordStartTime) / 1000);
			this.el.pttTimer.textContent = formatTimer(elapsed);
		}, 250);

		this.mediaRecorder.start(250);
	}

	_stopRecording({ send = true } = {}) {
		if (!this.isRecording) return;
		this.cancelRequested = !send;
		try {
			if (this.mediaRecorder && this.mediaRecorder.state !== "inactive") {
				this.mediaRecorder.stop();
			} else {
				this._cleanupRecordingUi();
			}
		} catch (_err) {
			this._cleanupRecordingUi();
		}
	}

	_cleanupRecordingUi() {
		this.isRecording = false;
		if (this.recordTimerId) {
			clearInterval(this.recordTimerId);
			this.recordTimerId = null;
		}
		if (this.mediaStream) {
			for (const track of this.mediaStream.getTracks()) {
				track.stop();
			}
			this.mediaStream = null;
		}
		this.mediaRecorder = null;
		this.audioChunks = [];
		this.el.pttBtn.classList.remove("is-recording");
		this.el.pttRecordingBar.classList.add("is-hidden");
	}

	async sendCurrent({ extraFile = null } = {}) {
		if (this.isSending || this.isReadOnly) return;
		const ctx = this.getContext();
		if (!ctx || !ctx.thread_key) {
			this.onToast("Select a thread first.", "warn");
			return;
		}

		const text = (this.el.textInput.value || "").trim();
		const files = this.stagedPhotos.map((p) => ({ blob: p.blob, filename: p.filename }));
		if (extraFile) files.push(extraFile);

		if (!text && !files.length) {
			return;
		}

		const idempotency_key = generateIdempotencyKey();
		const metadata = {
			thread_key: ctx.thread_key,
			ro_id: ctx.ro_id || null,
			vin: ctx.vin || null,
			bay_id: ctx.bay_id || null,
			tech_id: ctx.tech_id || null,
			client_timestamp: new Date().toISOString(),
			idempotency_key,
		};

		await this._submitCapture({
			thread_key: ctx.thread_key,
			repair_order: ctx.ro_id || null,
			text,
			intent_tag: this.selectedIntent,
			metadata,
			idempotency_key,
			files,
		});
	}

	async _submitCapture(payload) {
		this.isSending = true;
		this.el.sendBtn.disabled = true;
		this._showNotice("");

		const hasFiles = payload.files && payload.files.length > 0;
		if (hasFiles) {
			this.el.uploadProgress.classList.remove("is-hidden");
			this.el.uploadProgressFill.style.width = "5%";
			this.el.uploadProgressLabel.textContent = "Uploading 5%";
		}

		try {
			const res = await postBoardCapture({
				...payload,
				onProgress: (pct) => {
					this.el.uploadProgressFill.style.width = `${pct}%`;
					this.el.uploadProgressLabel.textContent = `Uploading ${pct}%`;
				},
			});

			this.el.textInput.value = "";
			this.selectedIntent = null;
			for (const child of this.el.intentPills.querySelectorAll(".intent-pill")) {
				child.classList.remove("is-active");
			}
			this._clearStagedPhotos();
			this.lastFailedCapture = null;

			if (this.onCaptureSent) {
				this.onCaptureSent(res);
			}
		} catch (err) {
			this.lastFailedCapture = payload;
			this.onToast(err.message || "Failed to post update.", "error");
		} finally {
			this.isSending = false;
			this.el.sendBtn.disabled = this.isReadOnly;
			this.el.uploadProgress.classList.add("is-hidden");
		}
	}
}
