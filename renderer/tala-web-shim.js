/**
 * tala-web-shim.js — window.tala, for a browser instead of Electron.
 *
 * WHY THIS EXISTS
 * ---------------
 * The renderer is already a web app: plain HTML, CSS and JS, no Node, no
 * Electron API beyond one object. Everything it needs from the outside world
 * goes through `window.tala`, which preload.js exposes over Electron IPC.
 *
 * So the whole of "make a web UI" is: provide the same object, backed by the
 * WebSocket the harness has always spoken. This file is that object. The
 * renderer is not modified and does not know the difference.
 *
 * WHAT MAPS TO WHAT
 * -----------------
 *   invoke('python:rpc', {method, params})  -> JSON-RPC over ws://host:8765
 *   invoke('<anything else>', params)       -> same, treating the channel as
 *                                              the method name, exactly as
 *                                              preload.js does in its fallback
 *   invoke('file:read' | 'file:write' | ... ) -> POST /api/bridge, because
 *                                              those are filesystem operations
 *                                              that Electron's main process
 *                                              used to perform
 *   on('python:notification', cb)           -> every JSON-RPC notification
 *   on('terminal:output' | ...)             -> notifications of that name
 *
 * WHAT IS DIFFERENT, AND SAID OUT LOUD
 * ------------------------------------
 * `terminal:command` spawns a process. In Electron that was a child of the
 * desktop app; here it is the harness's own shell RPC, which is the same
 * capability reached a different way. Where a channel has no equivalent, the
 * promise REJECTS with a clear message naming the channel. It does not
 * resolve with an empty result -- a feature that silently returns nothing is
 * the failure mode this codebase has spent a month removing.
 */
'use strict';

(function () {
  if (window.tala) {
    console.warn('[tala-web] window.tala already exists; shim not installed.');
    return;
  }

  const CONFIG = window.__TALA_WEB_CONFIG__ || {};
  const BRIDGE = CONFIG.bridgeUrl || '/api/bridge';
  const WS_TICKET_URL = CONFIG.wsTicketUrl || '/api/ws-ticket';
  const WS_BASE = CONFIG.wsUrl || `ws://${location.hostname}:8765`;

  // UI-audit C3: HTTP UI token comes from the URL once (or sessionStorage),
  // never from /tala-web-config.js. Prefer X-Tala-Token header thereafter;
  // strip long-lived ?token= from history as soon as we have it in memory.
  function readHttpToken() {
    try {
      const stored = sessionStorage.getItem('talaHttpToken');
      if (stored) return stored;
    } catch (_) { /* private mode */ }
    const fromQuery = new URLSearchParams(location.search).get('token') || '';
    if (fromQuery) {
      try { sessionStorage.setItem('talaHttpToken', fromQuery); } catch (_) {}
      try {
        const url = new URL(location.href);
        url.searchParams.delete('token');
        history.replaceState(null, '', url.pathname + url.search + url.hash);
      } catch (_) {}
    }
    return fromQuery;
  }
  const TOKEN = readHttpToken();

  let lastWsUrl = WS_BASE;

  /** Channels that are filesystem/process work, not JSON-RPC. */
  const BRIDGE_CHANNELS = new Set([
    'file:read', 'file:write', 'file:list', 'file:delete',
    'vault:read', 'git:status', 'system:apply_update',
  ]);

  /** Events the renderer is allowed to subscribe to, matching preload.js. */
  const ALLOWED_EVENTS = new Set([
    'terminal:output', 'terminal:exit', 'blockchain:output',
    'harness:output', 'python:notification',
  ]);

  // ── connection ────────────────────────────────────────────────────
  let socket = null;
  let connected = false;
  let nextId = 1;
  let reconnectDelay = 500;
  const pending = new Map();      // id -> {resolve, reject, timer}
  const queue = [];               // requests made before the socket opened
  const listeners = new Map();    // event name -> Set<callback>

  function emit(event, payload) {
    const set = listeners.get(event);
    if (!set) return;
    for (const cb of set) {
      try { cb(payload); } catch (err) {
        console.error(`[tala-web] listener for ${event} threw:`, err);
      }
    }
  }

  function setStatus(state, detail) {
    // The renderer has no concept of a dropped backend, because Electron
    // restarted it. Surface it rather than letting the UI look merely idle.
    document.documentElement.setAttribute('data-tala-connection', state);
    emit('harness:output', `[web] backend ${state}${detail ? ': ' + detail : ''}`);
  }

  async function fetchWsTicket() {
    if (!TOKEN) throw new Error('HTTP UI token missing; open with ?token= once or set X-Tala-Token.');
    const res = await fetch(WS_TICKET_URL, {
      method: 'POST',
      headers: { 'X-Tala-Token': TOKEN, 'Content-Type': 'application/json' },
      body: '{}',
      cache: 'no-store',
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.ticket) {
      throw new Error(body.error || `ws-ticket failed (HTTP ${res.status})`);
    }
    return body.ticket;
  }

  async function connect() {
    let wsUrl = WS_BASE;
    try {
      const ticket = await fetchWsTicket();
      // new WebSocket() cannot set headers; the single-use ticket rides in the
      // query string and is burned by 8765 on first use (UI-audit C3).
      wsUrl = `${WS_BASE}${WS_BASE.includes('?') ? '&' : '?'}token=${encodeURIComponent(ticket)}`;
      lastWsUrl = wsUrl;
    } catch (err) {
      setStatus('error', err.message || String(err));
      scheduleReconnect();
      return;
    }

    try {
      socket = new WebSocket(wsUrl);
    } catch (err) {
      setStatus('error', err.message);
      scheduleReconnect();
      return;
    }

    socket.addEventListener('open', () => {
      connected = true;
      reconnectDelay = 500;
      setStatus('connected');
      while (queue.length) send(queue.shift());
    });

    socket.addEventListener('message', (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }

      if (msg.id !== undefined && msg.id !== null && pending.has(msg.id)) {
        const { resolve, reject, timer } = pending.get(msg.id);
        pending.delete(msg.id);
        clearTimeout(timer);
        if (msg.error) reject(new Error(msg.error.message || String(msg.error)));
        else resolve(msg.result);
        return;
      }

      // No id: a notification. Electron relayed the whole raw message, so the
      // renderer's handlers expect the object with .method and .params.
      if (msg.method) {
        emit('python:notification', msg);
        if (ALLOWED_EVENTS.has(msg.method)) emit(msg.method, msg.params);
      }
    });

    socket.addEventListener('close', () => {
      connected = false;
      setStatus('disconnected');
      // Fail everything in flight rather than leaving callers hanging.
      for (const [, p] of pending) {
        clearTimeout(p.timer);
        p.reject(new Error('Backend connection closed.'));
      }
      pending.clear();
      scheduleReconnect();
    });

    socket.addEventListener('error', () => { /* close follows */ });
  }

  function scheduleReconnect() {
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
  }

  function send(entry) {
    try {
      socket.send(JSON.stringify(entry.payload));
    } catch (err) {
      entry.reject(new Error(`Could not reach the backend: ${err.message}`));
    }
  }

  function rpc(method, params, timeoutMs = 300000) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      const payload = { jsonrpc: '2.0', id, method, params: params || {} };
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      pending.set(id, { resolve, reject, timer });

      const entry = { payload, reject };
      if (connected) send(entry); else queue.push(entry);
    });
  }

  async function bridge(channel, payload) {
    const res = await fetch(BRIDGE, {
      method: 'POST',
      headers: Object.assign(
        { 'Content-Type': 'application/json' },
        TOKEN ? { 'X-Tala-Token': TOKEN } : {},
      ),
      body: JSON.stringify({ channel, payload: payload || {} }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.__rpcError) {
      throw new Error(body.error || `${channel} failed (HTTP ${res.status})`);
    }
    return body;
  }

  // ── the surface ───────────────────────────────────────────────────
  window.tala = {
    invoke(channel, data) {
      if (BRIDGE_CHANNELS.has(channel)) return bridge(channel, data);

      if (channel === 'terminal:command') {
        // Same capability, reached through the harness rather than through a
        // child of the desktop process.
        return rpc('shell.run', data || {});
      }
      if (channel === 'terminal:kill') {
        return Promise.reject(new Error(
          'terminal:kill is not available in the web UI: the harness owns the ' +
          'shell process, and there is no per-session handle to kill yet.'
        ));
      }
      if (channel === 'python:rpc') {
        return rpc((data && data.method) || '', (data && data.params) || {});
      }
      // preload.js treats an unknown channel as a python method name.
      return rpc(channel, data || {});
    },

    request(channel, data) { return this.invoke(channel, data); },

    on(channel, callback) {
      if (!ALLOWED_EVENTS.has(channel)) {
        throw new Error(`[tala-web] Event "${channel}" is not allowed.`);
      }
      if (!listeners.has(channel)) listeners.set(channel, new Set());
      listeners.get(channel).add(callback);
      return () => listeners.get(channel).delete(callback);
    },

    get allowedChannels() {
      return [...BRIDGE_CHANNELS, 'python:rpc', 'terminal:command', 'terminal:kill'];
    },

    /** Present only in the web build, for diagnostics. */
    __web: {
      wsUrl: lastWsUrl,
      get connected() { return connected; },
      get pending() { return pending.size; },
    },
  };

  connect();
})();
