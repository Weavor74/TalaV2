'use strict';

/**
 * Distinct token files for 8765 (WS) vs 8767 (HTTP UI).
 *
 * UI-audit C3: never collapse TALA_WEB_TOKEN into the long-lived WS secret.
 * Electron mints both before spawning Python so the harness and desktop agree.
 */

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

/**
 * @param {{ envValue?: string, filePath: string, label: string }} opts
 * @returns {string}
 */
function ensureTokenFile({ envValue, filePath, label }) {
  const pinned = (envValue || '').trim();
  if (pinned) return pinned;

  try {
    if (fs.existsSync(filePath)) {
      const existing = fs.readFileSync(filePath, 'utf8').trim();
      if (existing) return existing;
    }
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    const token = crypto.randomBytes(32).toString('base64url');
    fs.writeFileSync(filePath, token, { mode: 0o600 });
    console.log(`[main] created a new ${label} at ${filePath}`);
    return token;
  } catch (err) {
    console.error(`[main] could not establish a ${label}: ${err.message}`);
    return '';
  }
}

/**
 * Long-lived WS credential for port 8765 (ws_token.txt / TALA_WS_TOKEN only).
 * @param {string} dataRoot
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string}
 */
function ensureWsToken(dataRoot, env = process.env) {
  return ensureTokenFile({
    envValue: env.TALA_WS_TOKEN,
    filePath: path.join(dataRoot, 'vault', '.keys', 'ws_token.txt'),
    label: 'harness WebSocket token',
  });
}

/**
 * HTTP UI credential for port 8767 (web_ui_token.txt / TALA_WEB_TOKEN only).
 * Never reads or copies the WS token.
 * @param {string} dataRoot
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string}
 */
function ensureWebUiToken(dataRoot, env = process.env) {
  return ensureTokenFile({
    envValue: env.TALA_WEB_TOKEN,
    filePath: path.join(dataRoot, 'vault', '.keys', 'web_ui_token.txt'),
    label: 'HTTP UI token',
  });
}

module.exports = {
  ensureTokenFile,
  ensureWsToken,
  ensureWebUiToken,
};
