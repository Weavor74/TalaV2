/**
 * Owner-gated harness admin helpers (extracted from main.js for the size ratchet).
 *
 * Electron injects the on-disk harness admin secret into a small set of RPC
 * methods so Mission Control can call them without shipping the secret through
 * the renderer. Resume must never silently release the emergency halt.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

/** Methods that receive harness_secret from the main process only. */
const OWNER_SECRET_METHODS = new Set([
  'system.trigger_update',
  'system.release_halt',
  'task.clear',
]);

function getHarnessAdminSecret() {
  const envSecret = process.env.TALA_HARNESS_ADMIN_SECRET;
  if (envSecret) return envSecret;

  const configuredPath = process.env.TALA_HARNESS_ADMIN_SECRET_FILE;
  const userData = process.env.TALA_USER_DATA;
  const secretPath = configuredPath
    ? path.resolve(configuredPath)
    : userData
      ? path.join(path.resolve(userData), 'vault', '.keys', 'harness_admin_secret.txt')
      : path.join(path.resolve(__dirname, '..'), 'vault', '.keys', 'harness_admin_secret.txt');

  if (fs.existsSync(secretPath)) {
    return fs.readFileSync(secretPath, 'utf8').trim();
  }

  const secret = crypto.randomBytes(32).toString('hex');
  fs.mkdirSync(path.dirname(secretPath), { recursive: true });
  fs.writeFileSync(secretPath, secret, 'utf8');
  return secret;
}

/**
 * Fetches the published sha256 for a platform zip from talacore.app/version.json.
 * Returns null if unavailable — callers must treat null as "cannot verify."
 */
function fetchExpectedUpdateHash(platformKey) {
  return new Promise((resolve) => {
    const https = require('https');
    const req = https.get('https://talacore.app/version.json', { timeout: 5000 }, (response) => {
      if (response.statusCode !== 200) {
        response.resume();
        return resolve(null);
      }
      let body = '';
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => {
        try {
          const data = JSON.parse(body);
          const hash = data && data.sha256 && data.sha256[platformKey];
          resolve(typeof hash === 'string' && hash ? hash : null);
        } catch {
          resolve(null);
        }
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

/**
 * Inject harness_secret for owner-gated RPCs. Mutates and returns params.
 * Does not inject for task.control resume — Resume ≠ release_halt.
 */
function injectOwnerSecret(method, params) {
  const out = params && typeof params === 'object' ? params : {};
  if (OWNER_SECRET_METHODS.has(method)) {
    out.harness_secret = getHarnessAdminSecret();
  }
  return out;
}

module.exports = {
  OWNER_SECRET_METHODS,
  getHarnessAdminSecret,
  fetchExpectedUpdateHash,
  injectOwnerSecret,
};
