/**
 * renderer/helpers.js
 * ===================
 * The shared DOM/IPC helpers every renderer module leans on. Deliberately a
 * plain (non-module) script — loaded via a classic <script> tag in index.html
 * *before* renderer.js (which is `type="module"`), so `qs`, `fmtTime` and
 * `invoke` are real globals that renderer.js, thinking_blocks.js-style
 * companion scripts, and plain-Node Jest tests can all reach without an ESM
 * pipeline. Same precedent as thinking_blocks.js, and the first step of the
 * renderer.js cut-down: helpers had to move out first, because a classic
 * companion script cannot see bindings scoped inside the renderer module.
 */

/**
 * Safely query a single DOM element.
 * @param {string} selector
 * @param {Element} [root=document]
 * @returns {Element|null}
 */
function qs(selector, root = document) {
  return root.querySelector(selector);
}

/**
 * Format a timestamp as HH:MM:SS.
 * @param {Date} [d]
 * @returns {string}
 */
function fmtTime(d = new Date()) {
  return d.toTimeString().slice(0, 8);
}

/**
 * Invoke a whitelisted IPC channel with a payload.
 * Falls back gracefully if the contextBridge is not available (e.g., during tests).
 *
 * // Context Bridge Relay: Provides a type-safe boundary between the sandboxed UI (renderer)
 * // and the Node.js/Python backend. All OS-level operations (file I/O, database queries)
 * // must pass through this funnel to prevent malicious Neural Nodes from escaping the browser sandbox.
 *
 * @param {string} channel
 * @param {*} [data]
 * @returns {Promise<*>}
 */
async function invoke(channel, data) {
  if (window.tala && typeof window.tala.invoke === 'function') {
    return window.tala.invoke(channel, data);
  }
  console.warn('[renderer] window.tala not available — running in degraded mode');
  return null;
}
