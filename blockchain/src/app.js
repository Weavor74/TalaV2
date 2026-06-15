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
const DATA_DIR = path.join(__dirname, '..', 'data');
const CHAIN_FILE = path.join(DATA_DIR, 'chain.json');
const KEYS_FILE = path.join(DATA_DIR, 'poa_keys.json');
const PEERS_FILE = path.join(DATA_DIR, 'peers.json');

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

function sha256Hex(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex');
}

function readRequestBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => {
      body += chunk;
      if (body.length > 1024 * 1024) {
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
  console.log('[Tala Chain] Loaded existing PoA key pair.');
} else {
  const { privateKey, publicKey } = crypto.generateKeyPairSync('ed25519', {
    publicKeyEncoding: { type: 'spki', format: 'pem' },
    privateKeyEncoding: { type: 'pkcs8', format: 'pem' }
  });
  poaPrivateKey = privateKey;
  poaPublicKey = publicKey;
  fs.writeFileSync(KEYS_FILE, JSON.stringify({ privateKey, publicKey }, null, 2), 'utf8');
  console.log('[Tala Chain] Generated new Ed25519 PoA key pair.');
}

// ---------------------------------------------------------------------------
// Blockchain Classes
// ---------------------------------------------------------------------------

class Transaction {
  constructor(id, type, hash, creator, counterparty, timestamp = Date.now(), document = null, metadata = null) {
    this.id = id; // Contract UUID
    this.type = type; // "contract", "payment", etc.
    this.hash = hash; // SHA-256 payload hash
    this.creator = creator;
    this.counterparty = counterparty;
    this.timestamp = timestamp;
    this.document = document;
    this.metadata = metadata;
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
          this.saveChain();
        }
      } else {
        this.chain = [this.createGenesisBlock()];
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

  createGenesisBlock() {
    return new Block(0, '0', 1716681600000, [], 0); // Hardcoded timestamp
  }

  getLatestBlock() {
    return this.chain[this.chain.length - 1];
  }

  addTransaction(tx) {
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
    this.pendingTransactions = [];
    console.log(`[Chain] Block mined and signed: ${newBlock.index} | Hash: ${newBlock.hash}`);
    this.saveChain();
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

  isValid() {
    for (let i = 1; i < this.chain.length; i++) {
      const current = this.chain[i];
      const previous = this.chain[i - 1];

      if (current.hash !== current.calculateHash()) return false;
      if (current.previousHash !== previous.hash) return false;

      // Verify signature if present
      if (current.signature && current.public_key) {
        try {
          const isValidSig = crypto.verify(
            null,
            Buffer.from(current.hash),
            current.public_key,
            Buffer.from(current.signature, 'hex')
          );
          if (!isValidSig) return false;
        } catch (e) {
          return false;
        }
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
  // CORS Headers for Electron integration compatibility
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  const url = new URL(req.url, `http://${req.headers.host}`);

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
  if (url.pathname === '/chain/tx' && req.method === 'POST') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        const payload = JSON.parse(body);
        
        if (!payload.id || !payload.type || !payload.hash || !payload.creator || !payload.counterparty) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Missing required transaction fields' }));
          return;
        }

        if (payload.document && payload.document.encrypted !== true) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Document payloads must be encrypted before anchoring' }));
          return;
        }

        const tx = new Transaction(
          payload.id,
          payload.type,
          payload.hash,
          payload.creator,
          payload.counterparty,
          Date.now(),
          payload.document ?? null,
          payload.metadata ?? null
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
        res.end(JSON.stringify({ error: 'Malformed JSON payload' }));
      }
    });
    return;
  }

  // Route: GET /chain/tx/:id
  const txMatch = url.pathname.match(/^\/chain\/tx\/([a-zA-Z0-9-]+)$/);
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
