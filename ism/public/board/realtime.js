/**
 * Native Frappe Socket.IO realtime subscription & delta catch-up sync for /board.
 * Degrades gracefully when Socket.IO client/server is unavailable.
 */

const SOCKETIO_LOAD_TIMEOUT_MS = 3500;
const FALLBACK_POLL_MS = 15000;

function loadScriptOnce(src, timeoutMs = SOCKETIO_LOAD_TIMEOUT_MS) {
	return new Promise((resolve) => {
		if (typeof window.io === "function") {
			resolve(window.io);
			return;
		}
		const existing = document.querySelector(`script[data-ism-sio="${src}"]`);
		if (existing) {
			resolve(typeof window.io === "function" ? window.io : null);
			return;
		}

		let settled = false;
		const finish = (val) => {
			if (!settled) {
				settled = true;
				resolve(val);
			}
		};

		const timer = setTimeout(() => finish(null), timeoutMs);
		const script = document.createElement("script");
		script.src = src;
		script.async = true;
		script.dataset.ismSio = src;
		script.onload = () => {
			clearTimeout(timer);
			finish(typeof window.io === "function" ? window.io : null);
		};
		script.onerror = () => {
			clearTimeout(timer);
			finish(null);
		};
		document.head.appendChild(script);
	});
}

export class BoardRealtimeClient {
	constructor({
		sitename = "",
		socketioPort = 9000,
		onBoardEvent = null,
		onProjection = null,
		onAiStatus = null,
		onThreadSummary = null,
		onCatchUpSync = null,
		onConnectionChange = null,
	} = {}) {
		this.sitename = sitename || window.location.hostname;
		this.socketioPort = socketioPort || 9000;
		this.onBoardEvent = onBoardEvent;
		this.onProjection = onProjection;
		this.onAiStatus = onAiStatus;
		this.onThreadSummary = onThreadSummary;
		this.onCatchUpSync = onCatchUpSync;
		this.onConnectionChange = onConnectionChange;

		this.socket = null;
		this.connected = false;
		this.subscribedThreadKey = null;
		this._syncDebounceTimer = null;
		this._fallbackTimer = null;
		this._boundVisibility = () => this._handleVisibilityChange();
		this._boundOnline = () => this._scheduleCatchUpSync(150);
	}

	async connect() {
		document.addEventListener("visibilitychange", this._boundVisibility);
		window.addEventListener("online", this._boundOnline);

		const ioFn = await this._resolveSocketIo();
		if (!ioFn) {
			this._startFallbackPolling();
			return;
		}

		const { baseUrl, namespace } = this._buildSocketTarget();
		try {
			this.socket = ioFn(`${baseUrl}/${namespace}`, {
				withCredentials: true,
				transports: ["websocket", "polling"],
				reconnection: true,
				reconnectionDelay: 1500,
				reconnectionDelayMax: 10000,
			});
		} catch (_err) {
			this._startFallbackPolling();
			return;
		}

		this.socket.on("connect", () => {
			this.connected = true;
			this._stopFallbackPolling();
			if (this.subscribedThreadKey) {
				this._emitSubscribe(this.subscribedThreadKey);
			}
			if (this.onConnectionChange) this.onConnectionChange(true);
			this._scheduleCatchUpSync(200);
		});

		this.socket.on("disconnect", () => {
			this.connected = false;
			if (this.onConnectionChange) this.onConnectionChange(false);
			this._startFallbackPolling();
		});

		this.socket.on("connect_error", () => {
			this.connected = false;
			this._startFallbackPolling();
		});

		this.socket.on("ism_board_event", (payload) => {
			if (this.onBoardEvent && payload) this.onBoardEvent(payload);
		});

		this.socket.on("ism_board_projection", (payload) => {
			if (this.onProjection && payload) this.onProjection(payload);
		});

		this.socket.on("ism_board_ai_status", (payload) => {
			if (this.onAiStatus && payload) this.onAiStatus(payload);
		});

		this.socket.on("ism_board_thread_summary", (payload) => {
			if (this.onThreadSummary && payload) this.onThreadSummary(payload);
		});
	}

	subscribeThread(threadKey) {
		if (this.subscribedThreadKey === threadKey) return;
		if (this.subscribedThreadKey && this.socket && this.connected) {
			this.socket.emit("doc_unsubscribe", "Shop Board Thread", this.subscribedThreadKey);
		}
		this.subscribedThreadKey = threadKey || null;
		if (this.subscribedThreadKey && this.socket && this.connected) {
			this._emitSubscribe(this.subscribedThreadKey);
		}
	}

	destroy() {
		document.removeEventListener("visibilitychange", this._boundVisibility);
		window.removeEventListener("online", this._boundOnline);
		this._stopFallbackPolling();
		if (this._syncDebounceTimer) {
			clearTimeout(this._syncDebounceTimer);
			this._syncDebounceTimer = null;
		}
		if (this.socket) {
			try {
				if (this.subscribedThreadKey) {
					this.socket.emit("doc_unsubscribe", "Shop Board Thread", this.subscribedThreadKey);
				}
				this.socket.disconnect();
			} catch (_err) {
				// ignore
			}
			this.socket = null;
		}
	}

	_emitSubscribe(threadKey) {
		try {
			this.socket.emit("doc_subscribe", "Shop Board Thread", threadKey);
		} catch (_err) {
			// ignore
		}
	}

	_handleVisibilityChange() {
		if (document.visibilityState === "visible") {
			this._scheduleCatchUpSync(150);
		}
	}

	_scheduleCatchUpSync(delayMs = 250) {
		if (!this.onCatchUpSync) return;
		if (this._syncDebounceTimer) clearTimeout(this._syncDebounceTimer);
		this._syncDebounceTimer = setTimeout(() => {
			this._syncDebounceTimer = null;
			this.onCatchUpSync();
		}, delayMs);
	}

	_startFallbackPolling() {
		if (this._fallbackTimer) return;
		this._fallbackTimer = setInterval(() => {
			if (document.visibilityState === "visible" && this.onCatchUpSync) {
				this.onCatchUpSync();
			}
		}, FALLBACK_POLL_MS);
	}

	_stopFallbackPolling() {
		if (this._fallbackTimer) {
			clearInterval(this._fallbackTimer);
			this._fallbackTimer = null;
		}
	}

	_buildSocketTarget() {
		const proto = window.location.protocol;
		const host = window.location.hostname;
		const port = window.location.port;
		const isLocalPort = Boolean(port && port !== "80" && port !== "443");
		const baseUrl = isLocalPort
			? `${proto}//${host}:${this.socketioPort}`
			: `${proto}//${window.location.host}`;
		return { baseUrl, namespace: this.sitename };
	}

	async _resolveSocketIo() {
		if (typeof window.io === "function") return window.io;
		const local = await loadScriptOnce("/socket.io/socket.io.js");
		if (local) return local;
		const { baseUrl } = this._buildSocketTarget();
		if (baseUrl !== window.location.origin) {
			return loadScriptOnce(`${baseUrl}/socket.io/socket.io.js`);
		}
		return null;
	}
}
