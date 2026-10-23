/**
 * Where the local chain node listens, and how the main process proves it may
 * write to it.
 *
 * The node (blockchain/src/app.js) takes its bind address from
 * TALA_CHAIN_BIND_HOST / TALA_CHAIN_BIND_PORT (or TALA_CHAIN_PORT) and refuses
 * anonymous POST /chain/tx with a 401; callers on this machine present the
 * admin secret the node mints into its data directory. The Neural Node
 * anchors in ipc-handlers.js had port 8332 written into them and sent no
 * secret, so on any moved port they went nowhere, and on the default port
 * every one was rejected and the rejection was swallowed.
 */
'use strict';

const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

function chainHost() {
  const host = process.env.TALA_CHAIN_BIND_HOST || '127.0.0.1';
  // A wildcard is a bind address, not somewhere to connect to.
  return host === '0.0.0.0' || host === '::' ? '127.0.0.1' : host;
}

function chainPort() {
  const port = Number(process.env.TALA_CHAIN_BIND_PORT || process.env.TALA_CHAIN_PORT || 8332);
  return Number.isInteger(port) && port > 0 && port < 65536 ? port : 8332;
}

/** The directory the node keeps admin_secret.txt in (see Electron's spawn in blockchain-process.js). */
function chainDataDir(dataRoot) {
  return process.env.TALA_CHAIN_DATA_DIR
    ? path.resolve(process.env.TALA_CHAIN_DATA_DIR)
    : path.join(path.resolve(dataRoot), 'blockchain', 'data');
}

/** The header the node trusts from this machine, or {} if no secret exists yet. Never sent off-machine. */
function chainAdminHeaders(dataRoot) {
  let secret = process.env.TALA_CHAIN_ADMIN_SECRET || '';
  if (!secret) {
    try {
      secret = fs.readFileSync(path.join(chainDataDir(dataRoot), 'admin_secret.txt'), 'utf8').trim();
    } catch {
      /* the node mints it on first run; until then the node will say 401 */
    }
  }
  return secret ? { 'x-tala-chain-secret': secret } : {};
}

const DOCUMENT_MARK = Buffer.from('"ciphertext"');

/** Does the local chain already carry an encrypted document? Streams chain.json; stops at the first. */
function chainHoldsDocuments(dataRoot) {
  let fd;
  try {
    fd = fs.openSync(path.join(chainDataDir(dataRoot), 'chain.json'), 'r');
    const chunk = Buffer.alloc(1 << 20);
    let tail = Buffer.alloc(0);
    for (;;) {
      const n = fs.readSync(fd, chunk, 0, chunk.length, null);
      if (n === 0) return false;
      if (Buffer.concat([tail, chunk.subarray(0, n)]).includes(DOCUMENT_MARK)) return true;
      tail = Buffer.from(chunk.subarray(Math.max(0, n - DOCUMENT_MARK.length), n));
    }
  } catch {
    return false;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

/**
 * Called before minting a document key. A missing key beside a chain that
 * already holds sealed documents means the original is lost or this process
 * is pointed at the wrong data directory; a replacement would seal every new
 * document under a key nothing else has. TALA_ALLOW_NEW_CHAIN_KEY=1 is the
 * deliberate way to start over.
 */
function assertMayMintDocumentKey(dataRoot, keyPath) {
  if (process.env.TALA_ALLOW_NEW_CHAIN_KEY === '1' || !chainHoldsDocuments(dataRoot)) return;
  const err = new Error(
    `No document key at ${keyPath}, but the chain at ${chainDataDir(dataRoot)} already holds encrypted ` +
    'documents. Restore the original key, or set TALA_ALLOW_NEW_CHAIN_KEY=1 to deliberately start a new ' +
    'one (older documents will stay unreadable).'
  );
  err.code = 'CHAIN_KEY_MISSING';
  throw err;
}

/**
 * POST one transaction (a JSON string) to the local node. Never rejects: an
 * anchor must not break the file write it describes, but a refusal is logged
 * instead of vanishing.
 */
function postTransaction(dataRoot, body) {
  return new Promise(resolve => {
    const req = http.request({
      hostname: chainHost(),
      port: chainPort(),
      path: '/chain/tx',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        ...chainAdminHeaders(dataRoot),
      },
    }, res => {
      res.resume();
      res.on('end', () => {
        const ok = res.statusCode >= 200 && res.statusCode < 300;
        if (!ok) console.warn(`[chain] anchor refused by the node: HTTP ${res.statusCode}`);
        resolve({ ok, status: res.statusCode });
      });
    });
    req.on('error', err => resolve({ ok: false, status: 0, error: err.message })); // node offline
    req.write(body);
    req.end();
  });
}

module.exports = {
  chainHost, chainPort, chainDataDir, chainAdminHeaders,
  chainHoldsDocuments, assertMayMintDocumentKey, postTransaction,
};
