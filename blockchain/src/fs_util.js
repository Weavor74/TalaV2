/**
 * Small file helpers for the chain node's data directory. Paths are always
 * passed in, so nothing here knows where the data lives.
 */
'use strict';

const fs = require('fs');

function readJsonFile(filePath, fallback) {
  try {
    if (!fs.existsSync(filePath)) {
      return fallback;
    }
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return fallback;
  }
}

function writeJsonFile(filePath, payload) {
  fs.writeFileSync(filePath, JSON.stringify(payload, null, 2), 'utf8');
}

// Secrets are written owner-only, never at the umask default (commonly 644 —
// world-readable). chmod is best-effort: Windows ACLs do not speak POSIX
// modes, and a failed tighten must not stop the chain from starting.
function writeSecretFile(filePath, contents) {
  fs.writeFileSync(filePath, contents, { encoding: 'utf8', mode: 0o600 });
  restrictPermissions(filePath);
}

// Self-heals files created by earlier versions that relied on the umask.
function restrictPermissions(filePath) {
  try {
    fs.chmodSync(filePath, 0o600);
  } catch { /* best effort; Windows ACLs differ */ }
}

module.exports = { readJsonFile, writeJsonFile, writeSecretFile, restrictPermissions };
