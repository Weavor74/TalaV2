/**
 * blockchain/src/app.js — Tala Custom Node.js Blockchain Node
 * =========================================================
 * Implements a lightweight Proof-of-Authority (PoA) blockchain server
 * using native Node.js HTTP and crypto APIs (zero dependencies).
 * Runs locally by default on http://127.0.0.1:8332, but can be bound to a
 * public interface on the permanent VPS via TALA_CHAIN_BIND_HOST.
 *
 * Endpoints:
 * - POST /chain/tx         - Broadcast new smart contract/transaction
 * - GET  /chain/tx/:id     - Query transaction by ID (contract UUID)
 * - GET  /chain/status     - Get current block height, size, status, and peer counts
 * - POST /chain/peers/*    - Register and refresh the VPS peer directory
 * - GET  /chain/peers/*    - Discover known peers from the VPS directory
 */

const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const BIND_HOST = process.env.TALA_CHAIN_BIND_HOST || '127.0.0.1';
const PORT = Number(process.env.TALA_CHAIN_BIND_PORT || process.env.TALA_CHAIN_PORT || 8332);
// Overridable so the chain can be exercised against a scratch directory
// rather than the live ledger. Defaults to the real one, so nothing changes
// unless a caller deliberately points it elsewhere.
const DATA_DIR = process.env.TALA_CHAIN_DATA_DIR
  ? path.resolve(process.env.TALA_CHAIN_DATA_DIR)
  : path.join(__dirname, '..', 'data');
const CHAIN_FILE = path.join(DATA_DIR, 'chain.json');
const KEYS_FILE = path.join(DATA_DIR, 'poa_keys.json');
const PEERS_FILE = path.join(DATA_DIR, 'peers.json');
const AUTHORITIES_FILE = path.join(DATA_DIR, 'authorities.json');
const ADMIN_SECRET_FILE = path.join(DATA_DIR, 'admin_secret.txt');

// Ensure data directory exists
if (!fs.existsSync(DATA_DIR)) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

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

// Sliding-window per-IP limiter for non-admin transaction submission. The
// mempool mines instantly, so every accepted tx costs disk and a block; an
// internet-reachable node needs a ceiling per stranger.
const txSubmissionHits = new Map();
const TX_RATE_LIMIT = 30;
const TX_RATE_WINDOW_MS = 60 * 1000;

function txRateLimited(ip) {
  const now = Date.now();
  const hits = (txSubmissionHits.get(ip) || []).filter(t => now - t < TX_RATE_WINDOW_MS);
  if (hits.length >= TX_RATE_LIMIT) {
    txSubmissionHits.set(ip, hits);
    return true;
  }
  hits.push(now);
  txSubmissionHits.set(ip, hits);
  // Keep the map from growing one entry per strange IP forever.
  if (txSubmissionHits.size > 10000) {
    for (const [key, times] of txSubmissionHits) {
      if (!times.some(t => now - t < TX_RATE_WINDOW_MS)) txSubmissionHits.delete(key);
    }
  }
  return false;
}

function sha256Hex(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex');
}

// Default cap is 1MB. The old blanket 100MB limit was a memory-DoS lever on
// an internet-reachable node: anonymous callers could hold open concurrent
// 100MB bodies. Routes that legitimately carry more (chain reconcile, which
// ships a full chain and is admin-gated BEFORE the body is read) pass their
// own cap.
function readRequestBody(req, maxBytes = 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => {
      body += chunk;
      if (body.length > maxBytes) {
        reject(new Error('Request body too large'));
        req.destroy();
      }
    });
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });
}

let poaPrivateKey = '';
let poaPublicKey = '';

if (fs.existsSync(KEYS_FILE)) {
  const keys = JSON.parse(fs.readFileSync(KEYS_FILE, 'utf8'));
  poaPrivateKey = keys.privateKey;
  poaPublicKey = keys.publicKey;
  restrictPermissions(KEYS_FILE);
  console.log('[Tala Chain] Loaded existing PoA key pair.');
} else {
  const { privateKey, publicKey } = crypto.generateKeyPairSync('ed25519', {
    publicKeyEncoding: { type: 'spki', format: 'pem' },
    privateKeyEncoding: { type: 'pkcs8', format: 'pem' }
  });
  poaPrivateKey = privateKey;
  poaPublicKey = publicKey;
  writeSecretFile(KEYS_FILE, JSON.stringify({ privateKey, publicKey }, null, 2));
  console.log('[Tala Chain] Generated new Ed25519 PoA key pair.');
}

// ---------------------------------------------------------------------------
// Who may sign a block
// ---------------------------------------------------------------------------
//
// Verifying a signature against the public key the block itself carries proves
// only that the block is internally consistent -- anyone can generate a key,
// sign with it, and produce a block that verifies. That is no bar at all on a
// chain other people write to, so signatures are checked against an explicit
// set of permitted signers.
//
// The set is seeded with this node's own key and nothing else. Additions are
// deliberate, admin-gated, and made by the owner: on a chain whose whole point
// is recording what happened between parties, who may write is exactly the
// decision that should not have a default.

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

class AuthoritySet {
  constructor() {
    this.authorities = [];
    this.load();
  }

  load() {
    try {
      if (fs.existsSync(AUTHORITIES_FILE)) {
        const payload = JSON.parse(fs.readFileSync(AUTHORITIES_FILE, 'utf8'));
        this.authorities = Array.isArray(payload.authorities) ? payload.authorities : [];
      }
    } catch (err) {
      // Refusing to start would be worse than starting closed: an unreadable
      // file leaves the set empty, and an empty set trusts nobody, which the
      // seeding below then corrects with this node's own key.
      console.error(`[Authorities] Could not read ${AUTHORITIES_FILE}: ${err.message}`);
      this.authorities = [];
    }
    this.seedSelf();
  }

  // This node signs its own blocks, so it must be able to verify them. Without
  // this a fresh install would reject every block it had just written.
  seedSelf() {
    if (!poaPublicKey) return;
    if (this.has(poaPublicKey)) return;
    this.authorities.push({
      public_key: poaPublicKey,
      fingerprint: keyFingerprint(poaPublicKey),
      label: 'this node',
      added_at: new Date().toISOString(),
      added_by: 'bootstrap',
    });
    this.save();
    console.log(`[Authorities] Seeded with this node's own key (${keyFingerprint(poaPublicKey)}).`);
  }

  save() {
    try {
      fs.writeFileSync(AUTHORITIES_FILE,
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
      is_self: entry.fingerprint === keyFingerprint(poaPublicKey),
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
      retired = talaLedger.revocations().has(keyFingerprint(pem));
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
    if (print === keyFingerprint(poaPublicKey)) {
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

const authoritySet = new AuthoritySet();

// Local admin secret — required on every state-mutating administrative route
// (chain reconcile, treasury mint). Auto-generated on first run and persisted
// alongside the PoA keys so any locally-running, trusted caller (the Python
// harness) can read the same file and authenticate without manual setup.
// This is a locally-scoped bearer secret, not a cryptographic identity: it
// proves "this caller can read files on this machine," which is the actual
// trust boundary these routes need on a VPS bound to 0.0.0.0.
let adminSecret = process.env.TALA_CHAIN_ADMIN_SECRET || '';
if (!adminSecret) {
  if (fs.existsSync(ADMIN_SECRET_FILE)) {
    adminSecret = fs.readFileSync(ADMIN_SECRET_FILE, 'utf8').trim();
    restrictPermissions(ADMIN_SECRET_FILE);
  } else {
    adminSecret = crypto.randomBytes(32).toString('hex');
    writeSecretFile(ADMIN_SECRET_FILE, adminSecret);
    console.log('[Tala Chain] Generated new local admin secret.');
  }
}

function requireAdminSecret(req, res) {
  const provided = req.headers['x-tala-chain-secret'];
  if (provided && provided === adminSecret) {
    return true;
  }
  res.writeHead(401, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'Unauthorized: missing or invalid admin secret.' }));
  return false;
}

// ---------------------------------------------------------------------------
// Blockchain Classes
// ---------------------------------------------------------------------------

class Transaction {
  constructor(id, type, hash, creator, counterparty, timestamp = Date.now(), document = null, metadata = null, signature = null, public_key = null) {
    this.id = id; // Contract UUID
    this.type = type; // "contract", "payment", etc.
    this.hash = hash; // SHA-256 payload hash
    this.creator = creator;
    this.counterparty = counterparty;
    this.timestamp = timestamp;
    this.document = document;
    this.metadata = metadata;
    this.signature = signature;
    this.public_key = public_key;
  }

  calculateHash() {
    const documentData = this.document === null || this.document === undefined
      ? ''
      : JSON.stringify(this.document);
    const metadataData = this.metadata === null || this.metadata === undefined
      ? ''
      : JSON.stringify(this.metadata);
    return crypto
      .createHash('sha256')
      .update(this.id + this.type + this.hash + this.creator + this.counterparty + this.timestamp + documentData + metadataData)
      .digest('hex');
  }
}

class Block {
  constructor(index, previousHash, timestamp, transactions, nonce = 0, signature = null, public_key = null) {
    this.index = index;
    this.previousHash = previousHash;
    this.timestamp = timestamp;
    this.transactions = transactions;
    this.nonce = nonce;
    this.hash = this.calculateHash();
    this.signature = signature;
    this.public_key = public_key;
  }

  calculateHash() {
    const txData = JSON.stringify(this.transactions);
    return crypto
      .createHash('sha256')
      .update(this.index + this.previousHash + this.timestamp + txData + this.nonce)
      .digest('hex');
  }

  mineBlock(difficulty = 1) {
    const target = Array(difficulty + 1).join('0');
    while (this.hash.substring(0, difficulty) !== target) {
      this.nonce++;
      this.hash = this.calculateHash();
    }
  }

  signBlock(privateKeyPem, publicKeyPem) {
    this.public_key = publicKeyPem;
    this.signature = crypto.sign(null, Buffer.from(this.hash), privateKeyPem).toString('hex');
  }
}

class Blockchain {
  constructor() {
    this.chain = [];
    this.pendingTransactions = [];
    this.difficulty = 1; // Light difficulty for instant edge execution
    this.walletBalances = new Map(); // Track token balances
    this.loadChain();
  }

  loadChain() {
    try {
      if (fs.existsSync(CHAIN_FILE)) {
        const data = fs.readFileSync(CHAIN_FILE, 'utf8');
        const parsed = JSON.parse(data);
        this.chain = parsed.map(b => {
          const txs = b.transactions.map(t => new Transaction(
            t.id, t.type, t.hash, t.creator, t.counterparty, t.timestamp, t.document ?? null, t.metadata ?? null
          ));
          const block = new Block(b.index, b.previousHash, b.timestamp, txs, b.nonce, b.signature, b.public_key);
          block.hash = b.hash;
          return block;
        });
        console.log(`[Chain] Loaded ${this.chain.length} blocks from file: ${CHAIN_FILE}`);
        if (!this.isValid()) {
          console.error('[Chain] Loaded chain is invalid. Starting fresh...');
          this.chain = [this.createGenesisBlock()];
          this.walletBalances.clear();
          this.saveChain();
        } else {
          this.recalculateBalances();
        }
      } else {
        this.chain = [this.createGenesisBlock()];
        this.walletBalances.clear();
        this.saveChain();
      }
    } catch (err) {
      console.error(`[Chain] Error loading chain from file: ${err.message}`);
      this.chain = [this.createGenesisBlock()];
      this.saveChain();
    }
  }

  saveChain() {
    try {
      fs.writeFileSync(CHAIN_FILE, JSON.stringify(this.chain, null, 2), 'utf8');
      console.log(`[Chain] Saved ${this.chain.length} blocks to file: ${CHAIN_FILE}`);
    } catch (err) {
      console.error(`[Chain] Error saving chain to file: ${err.message}`);
    }
  }

  recalculateBalances() {
    this.walletBalances.clear();
    for (const block of this.chain) {
      for (const tx of block.transactions) {
        this.applyTransactionToBalance(tx);
      }
    }
  }

  applyTransactionToBalance(tx) {
    if (tx.type === 'mint') {
      const amount = parseFloat(tx.metadata?.amount || 0);
      const current = this.walletBalances.get(tx.counterparty) || 0;
      this.walletBalances.set(tx.counterparty, current + amount);
    } else if (tx.type === 'payment') {
      const amount = parseFloat(tx.metadata?.amount || 0);
      const houseFeePercent = parseFloat(tx.metadata?.house_fee_percent || 0);
      
      const creatorBal = this.walletBalances.get(tx.creator) || 0;
      this.walletBalances.set(tx.creator, creatorBal - amount);
      
      let houseFee = 0;
      if (houseFeePercent > 0 && houseFeePercent <= 1) {
        houseFee = amount * houseFeePercent;
      }
      const finalAmount = amount - houseFee;

      // Fetch counterparty balance AFTER deducting creator, in case they are the same
      const cpBal = this.walletBalances.get(tx.counterparty) || 0;
      this.walletBalances.set(tx.counterparty, cpBal + finalAmount);
      
      if (houseFee > 0) {
        const networkBal = this.walletBalances.get('tala-network') || 0;
        this.walletBalances.set('tala-network', networkBal + houseFee);
      }
    }
  }

  getBalance(address) {
    return this.walletBalances.get(address) || 0;
  }

  createGenesisBlock() {
    const genesisTx = new Transaction(
      '00000000-0000-0000-0000-000000000000',
      'mint',
      'genesis',
      'system',
      'tala-network',
      1716681600000,
      null,
      { amount: 440000000000000 }
    );
    return new Block(0, '0', 1716681600000, [genesisTx], 0); // Hardcoded timestamp
  }

  getLatestBlock() {
    return this.chain[this.chain.length - 1];
  }

  addTransaction(tx) {
    if (tx.type === 'mint' && this.chain.length > 0) {
      console.log(`[Chain] Rejected mint transaction: ${tx.id}. Minting is disabled after genesis.`);
      throw new Error("Minting new tokens is disabled. Maximum supply is fixed.");
    }
    
    // Zero-Trust Cryptographic Payment Verification
    if (tx.type === 'payment') {
      if (!tx.signature || !tx.public_key) {
        throw new Error("Unauthorized: Payment transactions must be cryptographically signed.");
      }
      if (tx.creator !== tx.public_key) {
        throw new Error("Unauthorized: Creator must match the public key.");
      }
      try {
        const key = crypto.createPublicKey({
          key: Buffer.from(tx.public_key, 'hex'),
          format: 'raw',
          type: 'ed25519'
        });
        const isValid = crypto.verify(null, Buffer.from(tx.hash), key, Buffer.from(tx.signature, 'hex'));
        if (!isValid) {
          throw new Error("Cryptographic signature verification failed.");
        }
      } catch (err) {
        throw new Error("Unauthorized: " + err.message);
      }
    }

    // A revocation retires a key for every node that sees it, so an unverifiable
    // one must never reach the chain -- it would be a denial-of-service against
    // whoever it names.
    if (tx.type === 'key-revocation') {
      const print = Blockchain.verifyRevocation(tx);
      if (!print) {
        throw new Error('Unauthorized: a revocation must be signed by the key it retires.');
      }
      console.log(`[Chain] Key ${print} retired itself.`);
    }

    this.pendingTransactions.push(tx);
    console.log(`[Chain] Transaction added: ${tx.id}`);
    
    // In PoA mode, we mine instantly upon transaction receipt
    this.minePendingTransactions();
  }

  minePendingTransactions() {
    if (this.pendingTransactions.length === 0) return;

    const latest = this.getLatestBlock();
    const newBlock = new Block(
      this.chain.length,
      latest.hash,
      Date.now(),
      this.pendingTransactions
    );
    newBlock.mineBlock(this.difficulty);
    newBlock.signBlock(poaPrivateKey, poaPublicKey);

    this.chain.push(newBlock);
    
    // Apply balances for new block
    for (const tx of newBlock.transactions) {
      this.applyTransactionToBalance(tx);
    }

    this.pendingTransactions = [];
    console.log(`[Chain] Block mined and signed: ${newBlock.index} | Hash: ${newBlock.hash}`);
    this.saveChain();
  }

  // ── Merging two chains without losing anything ────────────────────────
  //
  // This replaced a longest-chain-wins whole-chain overwrite (UI-124). That
  // rule is sound between one machine and its own backup and is data loss on a
  // chain more than one party writes to: two peers who each append while the
  // other is offline both hold a chain at least as long as the other's, so both
  // pass a height check, and whichever writes second discards the first one's
  // blocks. Nothing notices afterwards, because a discarded block leaves no
  // trace -- the receiving node simply holds a different chain than it did, and
  // it verifies. The transactions that vanish are by definition the external
  // ones, which are the transactions a shared chain exists to record.
  //
  // Merging happens at the transaction level, not the block level, because a
  // block cannot be moved: its hash covers its own index and previousHash, and
  // its signature covers the hash. Re-siting somebody else's block invalidates
  // their signature. So blocks that both sides share are kept exactly as they
  // are, and only the divergent tail is rebuilt from the union of both sides'
  // transactions.

  // What makes two transactions the same transaction.
  //
  // Not the id: that is a *document* identity which repeats every time the
  // document changes -- across the 2,748 local transactions there are 185
  // distinct ids, and settings-workflows alone accounts for 174 of them.
  // Not the hash either, which covers content and so repeats whenever the same
  // content is re-anchored (683 distinct across the same 2,748). Nothing the
  // chain assigns is unique, so identity is a fingerprint of the whole
  // transaction: two that serialise identically are the same transaction by
  // any definition that matters.
  static fingerprint(tx) {
    return crypto.createHash('sha256').update(JSON.stringify({
      id: tx.id, type: tx.type, hash: tx.hash, creator: tx.creator,
      counterparty: tx.counterparty, timestamp: tx.timestamp,
      document: tx.document ?? null, metadata: tx.metadata ?? null,
      signature: tx.signature ?? null, public_key: tx.public_key ?? null,
    })).digest('hex');
  }

  /**
   * Which keys have retired themselves, and where on the chain they did it.
   *
   * Only *self*-revocation lives on the chain, and the distinction is the
   * whole design. A revocation signed by the key being revoked is a fact
   * anybody can check: nobody but the holder could have produced it, so every
   * node can act on it without trusting whoever relayed it. "I no longer trust
   * your key" is an opinion, and putting opinions on a shared chain would let
   * any signer evict any other — so local distrust stays local.
   *
   * Returns fingerprint -> the block index the revocation appeared in, because
   * a revocation is not retroactive: blocks signed before it stay valid.
   * Retiring a key must not collapse the history it wrote, or nobody could
   * ever safely retire one.
   */
  revocations() {
    const found = new Map();
    for (const block of this.chain) {
      for (const tx of block.transactions || []) {
        if (tx.type !== 'key-revocation') continue;
        const print = Blockchain.verifyRevocation(tx);
        if (!print) continue;
        // The earliest wins: a key is retired from the first moment it said so.
        if (!found.has(print)) found.set(print, block.index);
      }
    }
    return found;
  }

  /**
   * Check a revocation transaction. Returns the revoked fingerprint, or null.
   *
   * Verified from the transaction alone, with no reference to who relayed it
   * or which chain it arrived on — that is what makes revocation propagate
   * safely through nodes that do not trust each other.
   */
  static verifyRevocation(tx) {
    const meta = tx.metadata || {};
    const publicKey = String(meta.public_key || tx.public_key || '').trim();
    const claimed = String(meta.fingerprint || '').trim();
    const revokedAt = String(meta.revoked_at || '');
    const signature = String(tx.signature || '');
    if (!publicKey || !claimed || !revokedAt || !signature) return null;

    // The fingerprint is derived, never believed: a revocation naming one key
    // and carrying another would otherwise retire whichever the reader
    // happened to look at.
    const print = keyFingerprint(publicKey);
    if (print !== claimed) return null;

    try {
      const ok = crypto.verify(
        null,
        Buffer.from(revocationMessage(print, revokedAt)),
        publicKey,
        Buffer.from(signature, 'hex'),
      );
      return ok ? print : null;
    } catch (err) {
      return null;
    }
  }

  // The last position at which the two chains still agree.
  forkPoint(incoming) {
    let k = 0;
    while (k < this.chain.length && k < incoming.length
           && this.chain[k].hash === incoming[k].hash) {
      k++;
    }
    return k;
  }

  /**
   * Merge an incoming chain into this one. Returns a report of what happened.
   *
   * Three outcomes, and only the third rewrites anything:
   *
   *   behind      — the incoming chain extends ours and we have nothing it
   *                 lacks, so its blocks are appended exactly as they are,
   *                 hashes and signatures intact. The ordinary case.
   *   up-to-date  — it holds nothing we do not.
   *   merged      — both sides appended after the fork. Their transactions and
   *                 ours are unioned, ordered deterministically so that two
   *                 nodes performing this merge arrive at the same chain, and
   *                 re-mined from the fork point.
   *
   * Re-mining changes the hashes of the divergent blocks, which is unavoidable:
   * two different blocks cannot occupy one index. No transaction is dropped,
   * which is the property that matters and the one the overwrite lacked.
   */
  merge(incoming) {
    if (!incoming.length) {
      return { outcome: 'up-to-date', height: this.chain.length,
               learned: 0, kept: 0 };
    }

    // A chain with a different genesis is a different chain, not a fork of
    // this one. Merging them would fabricate a history that never existed.
    if (this.chain.length && incoming[0].hash !== this.chain[0].hash) {
      const error = new Error('The incoming chain has a different genesis block');
      error.conflict = true;
      throw error;
    }

    const k = this.forkPoint(incoming);
    const ourTail = this.chain.slice(k);
    const theirTail = incoming.slice(k);

    if (!theirTail.length) {
      return { outcome: 'up-to-date', height: this.chain.length,
               learned: 0, kept: ourTail.length };
    }

    if (!ourTail.length) {
      // Fast-forward. Their blocks already carry the right index and
      // previousHash, so they are appended untouched and every signature
      // survives -- the strongest form of lossless there is.
      for (const block of theirTail) {
        this.chain.push(block);
      }
      this.recalculateBalances();
      this.saveChain();
      const learned = theirTail.reduce((n, b) => n + b.transactions.length, 0);
      console.log(`[Reconcile] Fast-forwarded to height ${this.chain.length} ` +
                  `(+${theirTail.length} block(s), ${learned} transaction(s))`);
      return { outcome: 'behind', height: this.chain.length,
               learned, kept: 0, forkPoint: k };
    }

    // Genuine divergence: both sides wrote after the fork. Keep everything.
    const seen = new Set();
    const union = [];
    for (const block of [...ourTail, ...theirTail]) {
      for (const tx of block.transactions) {
        const print = Blockchain.fingerprint(tx);
        if (seen.has(print)) continue;
        seen.add(print);
        union.push(tx);
      }
    }

    // Deterministic order, so two nodes merging the same pair of chains
    // produce the same chain rather than diverging further. Timestamp first
    // because it is the one meaningful ordering; the fingerprint breaks ties
    // without reference to which side a transaction arrived from.
    union.sort((a, b) => {
      const ta = Number(a.timestamp) || 0;
      const tb = Number(b.timestamp) || 0;
      if (ta !== tb) return ta - tb;
      return Blockchain.fingerprint(a).localeCompare(Blockchain.fingerprint(b));
    });

    const ourCount = ourTail.reduce((n, b) => n + b.transactions.length, 0);
    const theirCount = theirTail.reduce((n, b) => n + b.transactions.length, 0);

    this.chain = this.chain.slice(0, k);
    for (const tx of union) {
      const block = new Block(this.chain.length, this.getLatestBlock().hash,
                              Number(tx.timestamp) || Date.now(), [tx]);
      block.mineBlock(this.difficulty);
      block.signBlock(poaPrivateKey, poaPublicKey);
      this.chain.push(block);
    }

    this.recalculateBalances();
    this.saveChain();
    console.log(`[Reconcile] Merged at fork ${k}: ${ourCount} local and ` +
                `${theirCount} incoming transaction(s) -> ${union.length} kept, ` +
                `height ${this.chain.length}`);
    return {
      outcome: 'merged', height: this.chain.length, forkPoint: k,
      learned: union.length - ourCount, kept: ourCount,
      transactions: union.length,
      dropped: 0,
    };
  }

  findTransactionById(txId) {
    // Search backward from newest blocks
    for (let i = this.chain.length - 1; i >= 0; i--) {
      const block = this.chain[i];
      for (const tx of block.transactions) {
        if (tx.id === txId) {
          return {
            transaction: tx,
            blockIndex: block.index,
            blockHash: block.hash,
            confirmations: this.chain.length - i
          };
        }
      }
    }
    return null;
  }

  /**
   * Whether this chain holds together.
   *
   * Three checks, and the last two both changed when key authentication went
   * in:
   *
   *   contents  — every block's hash is the hash of its own contents.
   *   linkage   — every block's previousHash is the block before it.
   *   signature — every block after genesis is signed, and signed by a key
   *               this node recognises as permitted to sign.
   *
   * The signature check used to read `if (current.signature &&
   * current.public_key)`, which skipped verification entirely for a block
   * carrying neither. Forging a block was therefore a matter of *deleting*
   * its signature. That is the more serious of the two holes here, because
   * authenticating keys is pointless while a block can decline to name one.
   *
   * And verification was against the public key the block itself carried,
   * which proves only internal consistency: anyone can generate a key, sign
   * with it, and produce a block that verifies. On a chain other people write
   * to that is no bar at all, so the signer must be in the authority set.
   *
   * `reason` is filled in on failure, so a chain that is refused can say which
   * block and why rather than merely being refused.
   */
  isValid() {
    this.invalidReason = '';

    // Scanned once up front rather than per block: a key retired at block R
    // may not sign anything after R, and that question is asked for every
    // block.
    const retired = this.revocations();

    for (let i = 1; i < this.chain.length; i++) {
      const current = this.chain[i];
      const previous = this.chain[i - 1];

      if (current.hash !== current.calculateHash()) {
        this.invalidReason = `block ${current.index}: contents do not match its hash`;
        return false;
      }
      if (current.previousHash !== previous.hash) {
        this.invalidReason = `block ${current.index}: does not link to the block before it`;
        return false;
      }

      if (!current.signature || !current.public_key) {
        this.invalidReason = `block ${current.index}: is not signed`;
        return false;
      }

      if (!authoritySet.has(current.public_key)) {
        this.invalidReason =
          `block ${current.index}: signed by ${keyFingerprint(current.public_key)}, ` +
          `which is not a permitted signer`;
        return false;
      }

      const retiredAt = retired.get(keyFingerprint(current.public_key));
      if (retiredAt !== undefined && current.index > retiredAt) {
        // Not retroactive: blocks it signed before retiring stay valid, or
        // retiring a key would collapse the history it wrote and nobody could
        // ever safely do it.
        this.invalidReason =
          `block ${current.index}: signed by ` +
          `${keyFingerprint(current.public_key)}, which retired itself at ` +
          `block ${retiredAt}`;
        return false;
      }

      try {
        const ok = crypto.verify(
          null,
          Buffer.from(current.hash),
          current.public_key,
          Buffer.from(current.signature, 'hex')
        );
        if (!ok) {
          this.invalidReason = `block ${current.index}: signature does not verify`;
          return false;
        }
      } catch (e) {
        this.invalidReason = `block ${current.index}: signature could not be checked (${e.message})`;
        return false;
      }
    }
    return true;
  }
}

class PeerDirectory {
  constructor() {
    const payload = readJsonFile(PEERS_FILE, { version: 1, peers: [] });
    this.peers = Array.isArray(payload.peers) ? payload.peers : [];
  }

  save() {
    writeJsonFile(PEERS_FILE, {
      version: 1,
      peers: this.peers,
    });
  }

  normalizePeer(peer, existing = null) {
    const now = new Date().toISOString();
    const username = String(peer.username || '').trim();
    const walletAddress = String(peer.wallet_address || peer.wallet || '').trim();
    const endpointUrl = String(peer.endpoint_url || peer.endpoint || '').trim();

    if (!username) {
      throw new Error('Peer username is required');
    }
    if (!walletAddress) {
      throw new Error('Peer wallet_address is required');
    }
    if (!endpointUrl) {
      throw new Error('Peer endpoint_url is required');
    }

    return {
      username,
      wallet_address: walletAddress,
      endpoint_url: endpointUrl,
      network: String(peer.network || peer.network_name || 'tala').trim() || 'tala',
      status: String(peer.status || existing?.status || 'online').trim() || 'online',
      discoverable: peer.discoverable === undefined ? true : Boolean(peer.discoverable),
      // The key this peer says it signs blocks with. Advertised, not trusted:
      // a directory entry is a claim by whoever wrote it, and admitting a
      // signer is a decision the receiving owner makes deliberately.
      signing_key: String(peer.signing_key || existing?.signing_key || '').trim(),
      capabilities: Array.isArray(peer.capabilities) ? peer.capabilities : [],
      metadata: peer.metadata && typeof peer.metadata === 'object' ? peer.metadata : {},
      last_seen: peer.last_seen || existing?.last_seen || now,
      created_at: existing?.created_at || now,
      updated_at: now,
    };
  }

  find(identifier) {
    const needle = String(identifier || '').trim();
    if (!needle) {
      return null;
    }
    return this.peers.find(peer => peer.username === needle || peer.wallet_address === needle) || null;
  }

  list(filters = {}) {
    let peers = [...this.peers];
    if (filters.discoverable !== undefined) {
      peers = peers.filter(peer => peer.discoverable === filters.discoverable);
    }
    if (filters.network) {
      peers = peers.filter(peer => peer.network === filters.network);
    }
    if (filters.username) {
      const username = String(filters.username).trim();
      peers = peers.filter(peer => peer.username === username);
    }
    if (filters.wallet_address) {
      const walletAddress = String(filters.wallet_address).trim();
      peers = peers.filter(peer => peer.wallet_address === walletAddress);
    }
    if (filters.onlineOnly) {
      peers = peers.filter(peer => peer.status === 'online');
    }
    return peers.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  }

  register(peer, { anchor = true } = {}) {
    const existing = this.find(peer.username) || this.find(peer.wallet_address);
    const normalized = this.normalizePeer(peer, existing);
    if (existing) {
      const index = this.peers.findIndex(item => item.username === existing.username || item.wallet_address === existing.wallet_address);
      if (index >= 0) {
        this.peers[index] = normalized;
      } else {
        this.peers.push(normalized);
      }
    } else {
      this.peers.push(normalized);
    }
    this.save();

    if (anchor) {
      try {
        const directoryHash = sha256Hex(JSON.stringify(normalized));
        const anchorTx = new Transaction(
          `peer-directory:${normalized.wallet_address}`,
          existing ? 'peer-directory-update' : 'peer-directory-register',
          directoryHash,
          normalized.username,
          'vps-discovery',
          Date.now(),
          null,
          {
            directory_entry: normalized,
          }
        );
        talaLedger.addTransaction(anchorTx);
      } catch (err) {
        console.warn(`[Tala Chain] Failed to anchor peer directory update for ${normalized.username}: ${err.message}`);
      }
    }

    return normalized;
  }

  heartbeat(identifier, patch = {}) {
    const existing = this.find(identifier);
    if (!existing) {
      return null;
    }
    const updated = this.normalizePeer({
      ...existing,
      ...patch,
      username: existing.username,
      wallet_address: existing.wallet_address,
      endpoint_url: patch.endpoint_url || existing.endpoint_url,
      status: patch.status || 'online',
      last_seen: new Date().toISOString(),
    }, existing);
    const index = this.peers.findIndex(item => item.username === existing.username || item.wallet_address === existing.wallet_address);
    if (index >= 0) {
      this.peers[index] = updated;
      this.save();
    }
    return updated;
  }
}

// Instantiate Global Ledger
const talaLedger = new Blockchain();
const peerDirectory = new PeerDirectory();

// ---------------------------------------------------------------------------
// HTTP Server / RPC Router
// ---------------------------------------------------------------------------

const server = http.createServer((req, res) => {
  // CORS: only ever open for GET (read-only chain status/blocks, used by the
  // Electron renderer's viewer). State-mutating routes are POST-only and must
  // never be reachable from an arbitrary cross-origin browser context — this
  // node has no other request-origin check, so a wildcard here on POST would
  // let any webpage the user has open silently call treasury/chain-mutating
  // endpoints. Tala's own backend talks to this node server-to-server and
  // neither sends nor needs CORS headers.
  const requestedMethod = req.method === 'OPTIONS'
    ? (req.headers['access-control-request-method'] || '').toUpperCase()
    : req.method;
  if (requestedMethod === 'GET' || requestedMethod === '') {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  }

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  const url = new URL(req.url, `http://${req.headers.host}`);

  // Route: GET / (Splash Page)
  if (url.pathname === '/' && req.method === 'GET') {
    try {
      const html = fs.readFileSync(path.join(__dirname, '../public/splash.html'), 'utf8');
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(html);
    } catch (e) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Splash page not found');
    }
    return;
  }

  // Route: GET static assets (.png, .jpg, .css, .js)
  const ext = path.extname(url.pathname).toLowerCase();
  const publicExts = { '.png': 'image/png', '.jpg': 'image/jpeg', '.css': 'text/css', '.js': 'application/javascript', '.zip': 'application/zip', '.json': 'application/json' };
  if (publicExts[ext] && req.method === 'GET') {
    try {
      const cleanPath = url.pathname.split('?')[0].replace(/^\/+/, '');
      const filePath = path.join(__dirname, '../public', cleanPath);
      const content = fs.readFileSync(filePath);
      res.writeHead(200, { 
        'Content-Type': publicExts[ext],
        'Access-Control-Allow-Origin': '*'
      });
      res.end(content);
    } catch (e) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('File not found');
    }
    return;
  }

  // Route: GET /preview (Landing Page)
  if (url.pathname === '/preview' && req.method === 'GET') {
    try {
      const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(html);
    } catch (e) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Landing page not found');
    }
    return;
  }

  // Route: GET /download (Download Page)
  if (url.pathname === '/download' && req.method === 'GET') {
    try {
      const html = fs.readFileSync(path.join(__dirname, '../public/download.html'), 'utf8');
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(html);
    } catch (e) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Download page not found');
    }
    return;
  }

  // Route: Install Scripts
  const installScripts = ['/install-windows.ps1', '/install-linux.sh', '/install-macos.sh'];
  if (installScripts.includes(url.pathname) && req.method === 'GET') {
    try {
      // The scripts are located in the root Tala folder (two levels above src)
      const filePath = path.join(__dirname, '../..', url.pathname);
      const content = fs.readFileSync(filePath, 'utf8');
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end(content);
    } catch (e) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Script not found');
    }
    return;
  }

  // Route: GET /buy (Payment Portal)
  // Disabled along with POST /checkout below: buy.html is a Stripe-branded
  // payment form with no real payment processor behind it. Serving it lets
  // someone fill it out and hit submit only to land on the /checkout 503 --
  // confusing at best. Nothing currently links to this route.
  if (url.pathname === '/buy' && req.method === 'GET') {
    res.writeHead(503, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      error: 'Payments are not yet available. Top-up via card purchase has not been implemented.',
    }));
    return;
  }

  // Route: POST /checkout
  // Disabled: this previously credited TalaCoin for a client-supplied amount
  // with no real payment verification behind it, while the UI (buy.html)
  // displayed a Stripe-branded "Payment Successful" confirmation. No payment
  // processor is actually integrated yet, so this must not appear to work
  // until one is.
  if (url.pathname === '/checkout' && req.method === 'POST') {
    res.writeHead(503, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      error: 'Payments are not yet available. Top-up via card purchase has not been implemented — no funds were charged or credited.',
    }));
    return;
  }

  // Route: GET /chain/status
  if (url.pathname === '/chain/status' && req.method === 'GET') {
    const status = {
      height: talaLedger.chain.length,
      isValid: talaLedger.isValid(),
      pendingTxCount: talaLedger.pendingTransactions.length,
      latestBlockHash: talaLedger.getLatestBlock().hash,
      peerCount: peerDirectory.list({ discoverable: true }).length,
      bindHost: BIND_HOST,
      bindPort: PORT,
    };
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(status));
    return;
  }

  // Route: GET /chain/peers
  if (url.pathname === '/chain/peers' && req.method === 'GET') {
    const discoverable = url.searchParams.get('discoverable');
    const onlineOnly = url.searchParams.get('online') === '1' || url.searchParams.get('online') === 'true';
    const network = url.searchParams.get('network') || undefined;
    const walletAddress = url.searchParams.get('wallet_address') || undefined;
    const username = url.searchParams.get('username') || undefined;
    const peers = peerDirectory.list({
      discoverable: discoverable === null ? undefined : discoverable === '1' || discoverable === 'true',
      onlineOnly,
      network,
      wallet_address: walletAddress,
      username,
    });
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ peers, count: peers.length }));
    return;
  }

  // Route: GET /chain/peers/:identifier
  const peerMatch = url.pathname.match(/^\/chain\/peers\/([^/]+)$/);
  if (peerMatch && req.method === 'GET') {
    const peer = peerDirectory.find(decodeURIComponent(peerMatch[1]));
    if (!peer) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Peer not found' }));
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ peer }));
    return;
  }

  // Route: POST /chain/peers/register
  if (url.pathname === '/chain/peers/register' && req.method === 'POST') {
    readRequestBody(req).then(body => {
      try {
        const payload = JSON.parse(body || '{}');
        const peer = peerDirectory.register(payload);
        res.writeHead(201, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          status: 'registered',
          peer,
          discovery_url: `/chain/peers/${encodeURIComponent(peer.wallet_address)}`,
        }));
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message || 'Malformed JSON payload' }));
      }
    }).catch(err => {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message || 'Unable to read request body' }));
    });
    return;
  }

  // Route: POST /chain/peers/heartbeat
  if (url.pathname === '/chain/peers/heartbeat' && req.method === 'POST') {
    readRequestBody(req).then(body => {
      try {
        const payload = JSON.parse(body || '{}');
        const identifier = payload.username || payload.wallet_address || payload.wallet;
        const peer = peerDirectory.heartbeat(identifier, payload);
        if (!peer) {
          res.writeHead(404, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Peer not found' }));
          return;
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'updated', peer }));
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message || 'Malformed JSON payload' }));
      }
    }).catch(err => {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message || 'Unable to read request body' }));
    });
    return;
  }

  // Route: GET /chain/blocks
  if (url.pathname === '/chain/blocks' && req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(talaLedger.chain));
    return;
  }

  // Route: POST /chain/tx
  //
  // Submission is not anonymous. Signature checks used to run only at block
  // validation (and only for payments), so an internet-reachable node
  // accepted junk transactions from anyone into its mempool. A submission is
  // now accepted from exactly two kinds of caller:
  //   * the local harness/SDK, proving it can read this machine's admin
  //     secret file (the same trust boundary every admin route uses), or
  //   * a remote peer whose transaction carries a valid ed25519 signature
  //     over its hash by the submitted public key.
  // The anonymous body cap is tight; the local caller keeps room for
  // encrypted document anchors.
  if (url.pathname === '/chain/tx' && req.method === 'POST') {
    const suppliedSecret = req.headers['x-tala-chain-secret'];
    const isLocalAdmin = Boolean(suppliedSecret && suppliedSecret === adminSecret);
    if (!isLocalAdmin && txRateLimited(req.socket.remoteAddress || '?')) {
      res.writeHead(429, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Too many transactions; slow down.' }));
      return;
    }
    readRequestBody(req, isLocalAdmin ? 10 * 1024 * 1024 : 64 * 1024).then(body => {
      try {
        const payload = JSON.parse(body);

        if (!payload.id || !payload.type || !payload.hash || !payload.creator || !payload.counterparty) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Missing required transaction fields' }));
          return;
        }

        if (!isLocalAdmin) {
          if (!payload.signature || !payload.public_key) {
            res.writeHead(401, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Transactions must be signed (signature and public_key) or submitted with the local admin secret.' }));
            return;
          }
          let signatureOk = false;
          try {
            const submitterKey = crypto.createPublicKey({
              key: Buffer.from(payload.public_key, 'hex'),
              format: 'raw',
              type: 'ed25519'
            });
            signatureOk = crypto.verify(
              null,
              Buffer.from(String(payload.hash)),
              submitterKey,
              Buffer.from(payload.signature, 'hex')
            );
          } catch {
            signatureOk = false;
          }
          if (!signatureOk) {
            res.writeHead(401, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Transaction signature did not verify.' }));
            return;
          }
        }

        if (payload.document && payload.document.encrypted !== true) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Document payloads must be encrypted before anchoring' }));
          return;
        }

        if (payload.type === 'payment') {
          const amount = parseFloat(payload.metadata?.amount || 0);
          const currentBalance = talaLedger.getBalance(payload.creator);
          if (currentBalance < amount) {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Insufficient TalaCoin balance' }));
            return;
          }
        }

        const tx = new Transaction(
          payload.id,
          payload.type,
          payload.hash,
          payload.creator,
          payload.counterparty,
          Date.now(),
          payload.document ?? null,
          payload.metadata ?? null,
          payload.signature ?? null,
          payload.public_key ?? null
        );

        talaLedger.addTransaction(tx);

        res.writeHead(201, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          status: 'anchored',
          tx_id: tx.calculateHash(),
          contract_id: tx.id,
          block: talaLedger.getLatestBlock()
        }));
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message || 'Malformed JSON payload' }));
      }
    }).catch(err => {
      res.writeHead(413, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message || 'Unable to read request body' }));
    });
    return;
  }

  // Route: GET /chain/identity
  //
  // This node's signing key, so a peer can obtain it and decide whether to
  // admit it. Deliberately not admin-gated: a public key is public, and a key
  // nobody can fetch cannot be distributed at all. It carries no claim about
  // *who* this node is -- that is what the fingerprint comparison over a
  // channel neither node controls is for.
  if (url.pathname === '/chain/identity' && req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      public_key: poaPublicKey,
      fingerprint: keyFingerprint(poaPublicKey),
      note: 'Compare this fingerprint out of band before admitting it as a signer.',
    }));
    return;
  }

  // Route: POST /chain/prove
  //
  // Proof of possession: the caller supplies a nonce it chose, and this node
  // signs it, demonstrating that whoever answers here actually holds the
  // private key for the advertised public key. Without it a directory entry
  // could advertise somebody else's key and have it admitted on their behalf.
  //
  // ─── the trap this avoids ───
  //
  // Signing caller-supplied bytes with the block-signing key is a forgery
  // oracle. Block signatures are made over `block.hash`, a bare 64-character
  // hex string, so an attacker who can get arbitrary input signed computes a
  // malicious block, submits its hash as the "nonce", and walks away with a
  // validly signed block from this node.
  //
  // So the signature is over a domain-separated message -- a fixed prefix the
  // signer chooses, plus the nonce -- which can never be a bare block hash.
  // The prefix is not decoration; it is the whole reason this route is safe.
  if (url.pathname === '/chain/prove' && req.method === 'POST') {
    readRequestBody(req).then(body => {
      try {
        const payload = JSON.parse(body || '{}');
        const nonce = String(payload.nonce || '').trim();

        // A nonce is a random challenge, so it has no business being long or
        // containing anything but hex. Constraining it keeps the signed
        // message shape predictable.
        if (!/^[0-9a-f]{32,128}$/i.test(nonce)) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({
            error: 'nonce must be 32-128 hexadecimal characters',
          }));
          return;
        }

        const message = `${POP_PREFIX}${nonce}`;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          nonce,
          prefix: POP_PREFIX,
          public_key: poaPublicKey,
          fingerprint: keyFingerprint(poaPublicKey),
          signature: crypto.sign(null, Buffer.from(message), poaPrivateKey).toString('hex'),
        }));
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    }).catch(err => {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message || 'Unable to read request body' }));
    });
    return;
  }

  // Route: POST /chain/revoke
  //
  // Retire this node's own signing key, permanently, and publish that fact.
  //
  // Self-revocation is the only kind that belongs on a shared chain. A
  // revocation signed by the key it retires is a fact anyone can check --
  // nobody but the holder could have produced it -- so every node can act on it
  // without trusting whoever passed it along. "I no longer trust your key" is
  // an opinion, and an opinion on a shared chain would let any signer evict any
  // other, so local distrust stays local.
  //
  // Admin-gated, and irreversible: there is no un-revoke, because a key whose
  // retirement can be withdrawn is not retired. After this the node keeps its
  // existing blocks -- revocation is not retroactive -- and can sign nothing
  // further until it is given a new key.
  if (url.pathname === '/chain/revoke' && req.method === 'POST') {
    if (!requireAdminSecret(req, res)) return;
    readRequestBody(req).then(body => {
      try {
        const payload = JSON.parse(body || '{}');
        const fingerprint = keyFingerprint(poaPublicKey);

        // Deliberately awkward. Retiring the key this node signs with is not
        // something to do by sending an empty POST at the wrong URL.
        if (String(payload.confirm || '') !== fingerprint) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({
            error: `To retire this key, send confirm: "${fingerprint}"`,
            fingerprint,
          }));
          return;
        }

        if (talaLedger.revocations().has(fingerprint)) {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({
            success: true, already: true, fingerprint,
            note: 'This key was already retired.',
          }));
          return;
        }

        const revokedAt = new Date().toISOString();
        const message = revocationMessage(fingerprint, revokedAt);
        const tx = new Transaction(
          `revocation-${fingerprint}`,
          'key-revocation',
          crypto.createHash('sha256').update(message).digest('hex'),
          fingerprint,
          'chain',
          Date.now(),
          null,
          {
            fingerprint,
            public_key: poaPublicKey,
            revoked_at: revokedAt,
            reason: String(payload.reason || ''),
          },
          crypto.sign(null, Buffer.from(message), poaPrivateKey).toString('hex'),
          poaPublicKey,
        );

        // Mined into a block signed by this same key. That is not a
        // contradiction: the revocation takes effect *after* the block it
        // appears in, so the block carrying it is the last one this key may
        // sign.
        talaLedger.addTransaction(tx);

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          success: true,
          fingerprint,
          revoked_at: revokedAt,
          block: talaLedger.getLatestBlock().index,
          note: 'Blocks signed before this stay valid. Nothing may be signed with this key after it.',
        }));
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    }).catch(err => {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message || 'Unable to read request body' }));
    });
    return;
  }

  // Route: GET /chain/revocations
  //
  // Keys that have retired themselves, and where. Not admin-gated: a
  // revocation is meant to travel, and one only its holder's friends can read
  // is not a revocation.
  if (url.pathname === '/chain/revocations' && req.method === 'GET') {
    const retired = [...talaLedger.revocations().entries()].map(
      ([fingerprint, block]) => ({ fingerprint, block }));
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ revocations: retired, count: retired.length }));
    return;
  }

  // Route: GET /chain/authorities
  //
  // Who may sign a block on this chain. Admin-gated: the list of parties a
  // node trusts is not something an anonymous caller should be able to survey.
  if (url.pathname === '/chain/authorities' && req.method === 'GET') {
    if (!requireAdminSecret(req, res)) return;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      authorities: authoritySet.list(),
      count: authoritySet.authorities.length,
      note: 'A block signed by a key not listed here is refused.',
    }));
    return;
  }

  // Route: POST /chain/authorities
  //
  // Permit another party to sign blocks on this chain. Deliberate and
  // admin-gated by design: on a chain whose purpose is recording what happened
  // between parties, who may write is exactly the decision that should not
  // have a default.
  if (url.pathname === '/chain/authorities' && req.method === 'POST') {
    if (!requireAdminSecret(req, res)) return;
    readRequestBody(req).then(body => {
      try {
        const payload = JSON.parse(body || '{}');
        const result = authoritySet.add(payload.public_key, payload.label);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ success: true, ...result }));
      } catch (err) {
        // A retired key is a conflict with the chain's own state, not a
        // malformed request; the two deserve different answers.
        res.writeHead(err.conflict ? 409 : 400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    }).catch(err => {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message || 'Unable to read request body' }));
    });
    return;
  }

  // Route: DELETE /chain/authorities/:fingerprint
  //
  // Withdrawing permission does not retroactively invalidate what that signer
  // already wrote -- those blocks are in the chain and removing them would be
  // the whole-chain rewrite this endpoint exists to avoid. It stops them
  // signing anything further, and any chain they offer from here on is
  // refused.
  const authorityMatch = url.pathname.match(/^\/chain\/authorities\/([^/]+)$/);
  if (authorityMatch && req.method === 'DELETE') {
    if (!requireAdminSecret(req, res)) return;
    try {
      const result = authoritySet.remove(decodeURIComponent(authorityMatch[1]));
      res.writeHead(result.removed ? 200 : 404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ success: result.removed, ...result }));
    } catch (err) {
      res.writeHead(err.conflict ? 409 : 400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message }));
    }
    return;
  }

  // Route: POST /chain/reconcile
  if (url.pathname === '/chain/reconcile' && req.method === 'POST') {
    if (!requireAdminSecret(req, res)) return;
    // A full chain is legitimately large; the admin gate above has already
    // run, so the wide cap is only reachable by the local harness.
    readRequestBody(req, 100 * 1024 * 1024).then(body => {
      try {
        const payload = JSON.parse(body || '{}');
        if (!payload.chain || !Array.isArray(payload.chain)) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Missing or invalid chain array' }));
          return;
        }

        // Create a temporary Blockchain instance to validate the incoming chain
        const tempLedger = new Blockchain();
        
        // Reconstruct the incoming chain blocks & transactions
        tempLedger.chain = payload.chain.map(b => {
          const txs = b.transactions.map(t => new Transaction(
            t.id, t.type, t.hash, t.creator, t.counterparty, t.timestamp, t.document ?? null, t.metadata ?? null, t.signature ?? null, t.public_key ?? null
          ));
          const block = new Block(b.index, b.previousHash, b.timestamp, txs, b.nonce, b.signature, b.public_key);
          block.hash = b.hash;
          return block;
        });

        // Validate the incoming chain: contents, linkage, and a signature from
        // a permitted signer on every block after genesis. A chain carrying a
        // block signed by a key this node does not recognise is refused whole
        // -- importing the blocks it *does* recognise would be a partial
        // import, and a chain with holes in it is not a chain.
        if (!tempLedger.isValid()) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({
            error: 'The incoming chain failed verification',
            reason: tempLedger.invalidReason || '',
          }));
          return;
        }

        // Merge rather than replace (UI-124).
        //
        // This used to accept the incoming chain whenever it was as long or
        // longer and assign it over the top: talaLedger.chain = tempLedger.chain.
        // A shorter incoming chain was refused with a 409 and a longer one
        // silently discarded every block this node held that the sender did
        // not. Height is not a measure of whose blocks matter.
        let report;
        try {
          report = talaLedger.merge(tempLedger.chain);
        } catch (mergeErr) {
          res.writeHead(mergeErr.conflict ? 409 : 500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: mergeErr.message }));
          return;
        }

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          success: true,
          outcome: report.outcome,          // behind | up-to-date | merged
          height: talaLedger.chain.length,
          forkPoint: report.forkPoint,
          learned: report.learned,          // transactions gained from the sender
          kept: report.kept,                // ours that a replacement would have destroyed
          latestBlockHash: talaLedger.getLatestBlock().hash,
        }));
      } catch (err) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    }).catch(err => {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message || 'Unable to read request body' }));
    });
    return;
  }

  // Route: POST /chain/mint
  if (url.pathname === '/chain/mint' && req.method === 'POST') {
    if (!requireAdminSecret(req, res)) return;
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        const payload = JSON.parse(body);
        if (!payload.address || !payload.amount) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Missing address or amount' }));
          return;
        }

        const amount = parseFloat(payload.amount);
        if (amount <= 0) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Amount must be greater than 0' }));
          return;
        }

        const id = crypto.randomUUID();
        const hash = sha256Hex(`payment:tala-network:${payload.address}:${amount}:${Date.now()}`);
        
        const tx = new Transaction(
          id,
          'payment',
          hash,
          'tala-network',
          payload.address,
          Date.now(),
          null,
          { amount: amount }
        );
        
        const currentBalance = talaLedger.getBalance('tala-network');
        if (currentBalance < amount) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Insufficient TalaCoin balance in treasury' }));
          return;
        }
        talaLedger.addTransaction(tx);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ 
          status: 'Treasury payment added (formerly mint)', 
          contract_id: tx.id,
          new_balance: talaLedger.getBalance(payload.address)
        }));
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Malformed JSON payload' }));
      }
    });
    return;
  }

  // Route: GET /chain/wallet/:address/balance
  const balanceMatch = url.pathname.match(/^\/chain\/wallet\/([^/]+)\/balance$/);
  if (balanceMatch && req.method === 'GET') {
    const address = balanceMatch[1];
    const balance = talaLedger.getBalance(address);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ address, balance }));
    return;
  }

  // Route: GET /chain/tx/:id
  const txMatch = url.pathname.match(/^\/chain\/tx\/([a-zA-Z0-9_-]+)$/);
  if (txMatch && req.method === 'GET') {
    const txId = txMatch[1];
    const result = talaLedger.findTransactionById(txId);
    
    if (!result) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: `Transaction ${txId} not found on-chain` }));
      return;
    }

    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(result));
    return;
  }

  // Fallback 404
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'Endpoint not found' }));
});

server.on('error', (err) => {
  console.error(`[Tala Chain] Server error: ${err.message}`);
  process.exit(1);
});

server.listen(PORT, BIND_HOST, () => {
  console.log(`[Tala Chain] Custom Node.js Blockchain running at http://${BIND_HOST}:${PORT}`);
});

// Electron keeps stdin open and uses that pipe as a lifecycle signal.
// VPS/systemd mode runs the chain as a standalone service process.
if (process.env.TALA_CHAIN_STANDALONE !== '1') {
  process.stdin.resume();
  const exitHandler = () => {
    console.log('[Tala Chain] Parent process pipe closed. Exiting blockchain node.');
    process.exit(0);
  };
  process.stdin.on('end', exitHandler);
  process.stdin.on('close', exitHandler);
}
