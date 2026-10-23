/**
 * Hashing and key-identity helpers shared by the node's routes and its
 * authority set. Pure functions; no node state.
 */
'use strict';

const crypto = require('crypto');

function sha256Hex(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex');
}

// Domain separation for proof-of-possession signatures. Anything signed with
// this prefix cannot be mistaken for a block signature, which is made over a
// bare 64-character hex hash. See POST /chain/prove.
const POP_PREFIX = 'tala-proof-of-possession:v1:';

// Domain separation for revocations, for the same reason and with the same
// force: a revocation signature must never be mistakable for a block
// signature, which is made over a bare 64-character hex hash.
const REVOCATION_PREFIX = 'tala-key-revocation:v1:';

// The message a key signs to retire itself. Both sides must build it
// identically or a valid revocation reads as a forged one.
function revocationMessage(fingerprint, revokedAt) {
  return `${REVOCATION_PREFIX}${fingerprint}:${revokedAt}`;
}

function keyFingerprint(publicKeyPem) {
  // Whitespace-insensitive, so a PEM that has been through JSON, a text field
  // or a different line ending still resolves to the same signer.
  const normalised = String(publicKeyPem || '').replace(/\s+/g, '');
  return crypto.createHash('sha256').update(normalised).digest('hex').slice(0, 16);
}

module.exports = { POP_PREFIX, REVOCATION_PREFIX, revocationMessage, keyFingerprint, sha256Hex };
