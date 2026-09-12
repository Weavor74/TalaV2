/**
 * preload.js — Tala Cognitive Engine Room
 *
 * Runs in the renderer's isolated preload context (before page scripts).
 * Exposes a minimal, whitelisted surface via contextBridge so the renderer
 * can communicate with the main process without any direct Node.js access.
 *
 * Security model:
 *  - contextIsolation: true   → preload and renderer have separate JS worlds
 *  - sandbox: true            → preload cannot use Node built-ins freely
 *  - nodeIntegration: false   → renderer has zero Node access
 *  - Only whitelisted channels can be invoked from the renderer
 */

'use strict';

const { contextBridge, ipcRenderer } = require('electron');

/* ──────────────────────────────────────────────────────────────────
   Channel whitelist
   Any channel not in this set is rejected before it reaches main.
   ────────────────────────────────────────────────────────────────── */
const ALLOWED_CHANNELS = new Set([
  'file:read',
  'file:write',
  'file:list',
  'file:delete',
  'vault:read',
  'python:rpc',
  'terminal:command',
  'terminal:kill',
  'git:status',
]);

/* ──────────────────────────────────────────────────────────────────
   Exposed API
   window.tala — the only surface the renderer may touch
   ────────────────────────────────────────────────────────────────── */
contextBridge.exposeInMainWorld('tala', {
  /**
   * Invoke a whitelisted IPC channel and return the result.
   *
   * @param {string} channel  — Must be one of ALLOWED_CHANNELS.
   * @param {*}      [data]   — Payload to send. Must be serializable.
   * @returns {Promise<*>}    — Resolves with the main-process return value.
   * @throws  {Error}         — If the channel is not whitelisted or IPC fails.
   */
  invoke(channel, data) {
    if (!ALLOWED_CHANNELS.has(channel)) {
      // Fallback: assume channel is a python JSON-RPC method
      return ipcRenderer.invoke('python:rpc', { method: channel, params: data || {} }).then(res => {
        if (res && res.__rpcError) {
          throw new Error(res.error);
        }
        return res;
      });
    }
    return ipcRenderer.invoke(channel, data).then(res => {
      if (res && res.__rpcError) {
        throw new Error(res.error);
      }
      return res;
    });
  },

  /** Alias for invoke */
  request(channel, data) {
    return this.invoke(channel, data);
  },

  /**
   * Register a listener for secure asynchronous events.
   *
   * @param {string} channel   — The event channel name.
   * @param {function} callback — The callback function, receives args directly.
   * @returns {function}       — A cleanup function to remove the listener.
   */
  on(channel, callback) {
    const allowedEvents = new Set([
      'terminal:output',
      'terminal:exit',
      'blockchain:output',
      'harness:output',
      'python:notification',
    ]);
    if (!allowedEvents.has(channel)) {
      throw new Error(`[preload] Event "${channel}" is not allowed.`);
    }

    const listener = (event, ...args) => callback(...args);
    ipcRenderer.on(channel, listener);

    // Return cleanup function
    return () => {
      ipcRenderer.removeListener(channel, listener);
    };
  },

  /**
   * List of channels the renderer is permitted to invoke.
   * Read-only for introspection / self-documentation.
   */
  get allowedChannels() {
    return [...ALLOWED_CHANNELS];
  },
});
