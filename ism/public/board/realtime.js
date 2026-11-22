/**
 * Native Frappe Socket.IO realtime subscription & delta catch-up sync for /board.
 * Reuses window.frappe.realtime or window.io if already loaded, and degrades
 * cleanly to visibilitychange + periodic visible-tab delta sync without injecting
 * failing <script> tags.
 */

const FALLBACK_POLL_MS = 15000;

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
		this._usingFrappeRealtime = false;
		this._frappeRealtimeBound = [];
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

		// 1. Reuse Frappe's realtime wrapper if already loaded on window
		if (
			window.frappe &&
			window.frappe.realtime &&
			typeof window.frappe.realtime.on === "function"
		) {
			this._usingFrappeRealtime = true;
			this.connected = true;
			const handlers = [
				["ism_board_event", (payload) => this.onBoardEvent && payload && this.onBoardEvent(payload)],
				["ism_board_projection", (payload) => this.onProjection && payload && this.onProjection(payload)],
				["ism_board_ai_status", (payload) => this.onAiStatus && payload && this.onAiStatus(payload)],
				["ism_board_thread_summary", (payload) => this.onThreadSummary && payload && this.onThreadSummary(payload)],
			];
			for (const [evt, fn] of handlers) {
				window.frappe.realtime.on(evt, fn);
				this._frappeRealtimeBound.push([evt, fn]);
			}
			if (this.subscribedThreadKey) {
				this._emitSubscribe(this.subscribedThreadKey);
			}
			if (this.onConnectionChange) this.onConnectionChange(true);
			return;
		}

		// 2. Direct Socket.IO connection only if window.io is already available
		if (typeof window.io === "function") {
			const { baseUrl, namespace } = this._buildSocketTarget();
			try {
				this.socket = window.io(`${baseUrl}/${namespace}`, {
					path: "/socket.io/",
					withCredentials: true,
					transports: ["websocket", "polling"],
					reconnection: true,
					reconnectionAttempts: 5,
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
			return;
		}

		// 3. Graceful fallback: visibilitychange + periodic visible-tab delta sync
		this._startFallbackPolling();
	}

	subscribeThread(threadKey) {
		if (this.subscribedThreadKey === threadKey) return;
		if (this.subscribedThreadKey && this.connected) {
			this._emitUnsubscribe(this.subscribedThreadKey);
		}
		this.subscribedThreadKey = threadKey || null;
		if (this.subscribedThreadKey && this.connected) {
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
		if (this._usingFrappeRealtime) {
			if (this.subscribedThreadKey) {
				this._emitUnsubscribe(this.subscribedThreadKey);
			}
			if (
				window.frappe &&
				window.frappe.realtime &&
				typeof window.frappe.realtime.off === "function"
			) {
				for (const [evt, fn] of this._frappeRealtimeBound) {
					try {
						window.frappe.realtime.off(evt, fn);
					} catch (_err) {
						// ignore
					}
				}
			}
			this._frappeRealtimeBound = [];
			this._usingFrappeRealtime = false;
			this.connected = false;
		}
		if (this.socket) {
			try {
				if (this.subscribedThreadKey) {
					this._emitUnsubscribe(this.subscribedThreadKey);
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
			if (this._usingFrappeRealtime && window.frappe && window.frappe.realtime) {
				if (typeof window.frappe.realtime.doc_subscribe === "function") {
					window.frappe.realtime.doc_subscribe("Shop Board Thread", threadKey);
				} else if (window.frappe.realtime.socket) {
					window.frappe.realtime.socket.emit("doc_subscribe", "Shop Board Thread", threadKey);
				}
			} else if (this.socket) {
				this.socket.emit("doc_subscribe", "Shop Board Thread", threadKey);
			}
		} catch (_err) {
			// ignore
		}
	}

	_emitUnsubscribe(threadKey) {
		try {
			if (this._usingFrappeRealtime && window.frappe && window.frappe.realtime) {
				if (typeof window.frappe.realtime.doc_unsubscribe === "function") {
					window.frappe.realtime.doc_unsubscribe("Shop Board Thread", threadKey);
				} else if (window.frappe.realtime.socket) {
					window.frappe.realtime.socket.emit("doc_unsubscribe", "Shop Board Thread", threadKey);
				}
			} else if (this.socket) {
				this.socket.emit("doc_unsubscribe", "Shop Board Thread", threadKey);
			}
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
}
