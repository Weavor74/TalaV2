/**
 * Who may sign a block.
 *
 *
 * Verifying a signature against the public key the block itself carries proves
 * only that the block is internally consistent -- anyone can generate a key,
 * sign with it, and produce a block that verifies. That is no bar at all on a
 * chain other people write to, so signatures are checked against an explicit
 * set of permitted signers.
 *
 * The set is seeded with this node's own key and nothing else. Additions are
 * deliberate, admin-gated, and made by the owner: on a chain whose whole point
 * is recording what happened between parties, who may write is exactly the
 * decision that should not have a default.
 */
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const { keyFingerprint } = require('./identity');

class AuthoritySet {
  // file: where the set lives. ownPublicKey: this node's own key, always a member.
  // getLedger: the ledger, which does not exist yet when this is constructed.
  constructor({ file, ownPublicKey, getLedger }) {
    this.file = file;
    this.ownPublicKey = ownPublicKey;
    this.getLedger = getLedger;
    this.authorities = [];
    this.load();
  }

  load() {
    try {
      if (fs.existsSync(this.file)) {
        const payload = JSON.parse(fs.readFileSync(this.file, 'utf8'));
        this.authorities = Array.isArray(payload.authorities) ? payload.authorities : [];
      }
    } catch (err) {
      // Refusing to start would be worse than starting closed: an unreadable
      // file leaves the set empty, and an empty set trusts nobody, which the
      // seeding below then corrects with this node's own key.
      console.error(`[Authorities] Could not read ${this.file}: ${err.message}`);
      this.authorities = [];
    }
    this.seedSelf();
  }

  // This node signs its own blocks, so it must be able to verify them. Without
  // this a fresh install would reject every block it had just written.
  seedSelf() {
    if (!this.ownPublicKey) return;
    if (this.has(this.ownPublicKey)) return;
    this.authorities.push({
      public_key: this.ownPublicKey,
      fingerprint: keyFingerprint(this.ownPublicKey),
      label: 'this node',
      added_at: new Date().toISOString(),
      added_by: 'bootstrap',
    });
    this.save();
    console.log(`[Authorities] Seeded with this node's own key (${keyFingerprint(this.ownPublicKey)}).`);
  }

  save() {
    try {
      fs.writeFileSync(this.file,
        JSON.stringify({ version: 1, authorities: this.authorities }, null, 2), 'utf8');
    } catch (err) {
      console.error(`[Authorities] Could not save: ${err.message}`);
    }
  }

  has(publicKeyPem) {
    const print = keyFingerprint(publicKeyPem);
    return this.authorities.some(entry => entry.fingerprint === print);
  }

  list() {
    // The public keys are not secret, but they are not needed for a listing
    // either; the fingerprint is what an operator works with.
    return this.authorities.map(entry => ({
      fingerprint: entry.fingerprint,
      label: entry.label || '',
      added_at: entry.added_at || '',
      added_by: entry.added_by || '',
      is_self: entry.fingerprint === keyFingerprint(this.ownPublicKey),
    }));
  }

  add(publicKeyPem, label = '') {
    const pem = String(publicKeyPem || '').trim();
    if (!pem) throw new Error('A public key is required');

    // Only a key that can actually verify a signature is any use, and a
    // malformed one accepted now becomes a block rejected later for reasons
    // nobody can trace back to here.
    try {
      crypto.createPublicKey(pem);
    } catch (err) {
      throw new Error(`Not a usable public key: ${err.message}`);
    }

    // Admitting a key that has retired itself would be admitting one whose
    // holder has said not to trust it. Refused rather than accepted-and-ignored,
    // so the list never claims a signer that nothing would accept.
    //
    // Guarded because the authority set is constructed before the ledger is,
    // and `talaLedger` is a const: reaching it too early throws rather than
    // reading as undefined. Nothing calls add() that early today, and this
    // stops that staying true by luck.
    let retired = false;
    try {
      retired = this.getLedger().revocations().has(keyFingerprint(pem));
    } catch (err) {
      retired = false;
    }
    if (retired) {
      const error = new Error('That key has retired itself and cannot be admitted');
      error.conflict = true;
      throw error;
    }
    if (this.has(pem)) {
      return { added: false, fingerprint: keyFingerprint(pem), reason: 'already an authority' };
    }

    this.authorities.push({
      public_key: pem,
      fingerprint: keyFingerprint(pem),
      label: String(label || ''),
      added_at: new Date().toISOString(),
      added_by: 'admin',
    });
    this.save();
    console.log(`[Authorities] Added ${keyFingerprint(pem)}${label ? ` (${label})` : ''}.`);
    return { added: true, fingerprint: keyFingerprint(pem) };
  }

  remove(fingerprint) {
    const print = String(fingerprint || '').trim();
    if (print === keyFingerprint(this.ownPublicKey)) {
      // Removing it would leave the node unable to verify the blocks it signs
      // itself, so the whole chain would stop validating on the next check.
      const error = new Error("This node's own key cannot be removed");
      error.conflict = true;
      throw error;
    }
    const before = this.authorities.length;
    this.authorities = this.authorities.filter(entry => entry.fingerprint !== print);
    if (this.authorities.length === before) {
      return { removed: false, reason: 'not an authority' };
    }
    this.save();
    console.log(`[Authorities] Removed ${print}.`);
    return { removed: true, fingerprint: print };
  }
}

module.exports = { AuthoritySet };
