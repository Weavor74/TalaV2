/**
 * Merging two chains without losing anything (UI-124).
 *
 * POST /chain/reconcile used to accept the incoming chain whenever it was as
 * long or longer and assign it over the top. Between one machine and its own
 * backup that is a sound rule. On a chain more than one party writes to it is
 * data loss, and there was a live caller pointing it at the local Ghost chain:
 * pre_flight_reconciliation() fetched the remote chain on every goal run and
 * pushed it into the local node, which would have replaced the complete local
 * history with a remote one whenever the remote was taller.
 *
 * These tests drive the real server over HTTP against a scratch data
 * directory, so the route, the admin gate, verification and the merge are all
 * exercised as they actually run.
 *
 *     node --test blockchain/test/
 */

const assert = require('node:assert');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { test, before, after } = require('node:test');

const APP = path.join(__dirname, '..', 'src', 'app.js');
const PORT = 8391;
const BASE = `http://127.0.0.1:${PORT}`;

let dataDir;
let server;
let adminSecret;

// ── a chain we can build on ──────────────────────────────────────────────────

const KEY = crypto.generateKeyPairSync('ed25519');

function transaction(id, overrides = {}) {
  return {
    id,
    type: 'peer-skill',
    hash: crypto.createHash('sha256').update(id).digest('hex'),
    creator: '@peer',
    counterparty: '@other',
    timestamp: 1756000000000,
    document: null,
    metadata: null,
    signature: null,
    public_key: null,
    ...overrides,
  };
}

function blockHash(block) {
  return crypto.createHash('sha256').update(
    block.index + block.previousHash + block.timestamp +
    JSON.stringify(block.transactions) + block.nonce
  ).digest('hex');
}

/** Build a signed chain whose genesis matches the server's own. */
function chainFrom(genesis, tails) {
  const chain = [genesis];
  let previous = genesis.hash;
  for (const [offset, transactions] of tails.entries()) {
    const block = {
      index: chain.length,
      previousHash: previous,
      timestamp: 1756000001000 + offset * 1000,
      transactions,
      nonce: 0,
    };
    block.hash = blockHash(block);
    block.signature = crypto.sign(null, Buffer.from(block.hash),
                                  KEY.privateKey).toString('hex');
    block.public_key = KEY.publicKey.export({ type: 'spki', format: 'pem' });
    previous = block.hash;
    chain.push(block);
  }
  return chain;
}

// ── the server ───────────────────────────────────────────────────────────────

async function get(pathname) {
  const response = await fetch(BASE + pathname);
  return { status: response.status, body: await response.json() };
}

async function reconcile(chain, headers = {}) {
  const response = await fetch(`${BASE}/chain/reconcile`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json',
               'x-tala-chain-secret': adminSecret, ...headers },
    body: JSON.stringify({ chain }),
  });
  let body = {};
  try { body = await response.json(); } catch { /* empty */ }
  return { status: response.status, body };
}

async function reset(chain) {
  // Put a known chain on disk and restart, rather than mutating a live one.
  fs.writeFileSync(path.join(dataDir, 'chain.json'),
                   JSON.stringify(chain, null, 2), 'utf8');
  await stop();
  await start();
}

function start() {
  return new Promise((resolve, reject) => {
    server = spawn(process.execPath, [APP], {
      env: { ...process.env, TALA_CHAIN_DATA_DIR: dataDir,
             TALA_CHAIN_BIND_PORT: String(PORT) },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const timer = setTimeout(() => reject(new Error('the chain node did not start')), 15000);
    server.stdout.on('data', chunk => {
      if (String(chunk).includes('running at')) {
        clearTimeout(timer);
        setTimeout(resolve, 100);
      }
    });
    server.stderr.on('data', chunk => process.stderr.write(chunk));
  });
}

function stop() {
  return new Promise(resolve => {
    if (!server || server.exitCode !== null) return resolve();
    server.once('exit', () => resolve());
    server.kill();
  });
}

before(async () => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tala-chain-'));
  await start();
  adminSecret = fs.readFileSync(path.join(dataDir, 'admin_secret.txt'), 'utf8').trim();

  // The chains these tests build are signed by KEY, so KEY has to be a
  // permitted signer -- exactly as a real peer would have to be. Without this
  // every merge test fails on the signer check, which is the check working.
  await fetch(`${BASE}/chain/authorities`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json',
               'x-tala-chain-secret': adminSecret },
    body: JSON.stringify({
      public_key: KEY.publicKey.export({ type: 'spki', format: 'pem' }),
      label: 'the test peer',
    }),
  });
});

after(async () => {
  await stop();
  fs.rmSync(dataDir, { recursive: true, force: true });
});

// ── the tests ────────────────────────────────────────────────────────────────

test('a chain the sender has and we do not is appended untouched', async () => {
  const { body: ours } = await get('/chain/blocks');
  const genesis = ours[0];
  const incoming = chainFrom(genesis, [[transaction('theirs-1')],
                                       [transaction('theirs-2')]]);

  const { status, body } = await reconcile(incoming);

  assert.equal(status, 200);
  assert.equal(body.outcome, 'behind');
  assert.equal(body.height, 3);
  assert.equal(body.learned, 2);

  // Fast-forward keeps their blocks exactly as they were, signatures included.
  const { body: after } = await get('/chain/blocks');
  assert.equal(after[1].hash, incoming[1].hash);
  assert.equal(after[1].signature, incoming[1].signature);
});

test('a chain holding nothing new changes nothing', async () => {
  const { body: ours } = await get('/chain/blocks');

  const { body } = await reconcile(ours);

  assert.equal(body.outcome, 'up-to-date');
  assert.equal(body.learned, 0);
  assert.equal(body.height, ours.length);
});

test('a shorter chain no longer loses its blocks', async () => {
  // The old rule refused this outright with a 409 -- and accepted the mirror
  // image of it by destroying whatever the sender lacked.
  const { body: ours } = await get('/chain/blocks');
  const shorter = ours.slice(0, 2);

  const { status, body } = await reconcile(shorter);

  assert.equal(status, 200);
  assert.equal(body.outcome, 'up-to-date');
  const { body: after } = await get('/chain/blocks');
  assert.equal(after.length, ours.length, 'our blocks survived a shorter chain');
});

test('when both sides appended, nothing is discarded', async () => {
  // The case the overwrite destroyed: two peers each appended while the other
  // was offline, so both chains are the same height and neither is a prefix of
  // the other.
  const { body: base } = await get('/chain/blocks');
  const genesis = base[0];

  await reset([genesis]);
  const ourChain = chainFrom(genesis, [[transaction('mine-1')],
                                       [transaction('mine-2')]]);
  await reconcile(ourChain);

  const theirChain = chainFrom(genesis, [[transaction('theirs-1')],
                                         [transaction('theirs-2')]]);
  const { body } = await reconcile(theirChain);

  assert.equal(body.outcome, 'merged');
  assert.equal(body.kept, 2, 'ours were kept');
  assert.equal(body.learned, 2, 'theirs were learned');

  const { body: after } = await get('/chain/blocks');
  const ids = after.flatMap(block => block.transactions.map(tx => tx.id));
  for (const id of ['mine-1', 'mine-2', 'theirs-1', 'theirs-2']) {
    assert.ok(ids.includes(id), `${id} survived the merge`);
  }
});

test('the merged chain still verifies', async () => {
  const { body } = await get('/chain/status');
  assert.equal(body.isValid, true);
});

test('merging is idempotent', async () => {
  const { body: before } = await get('/chain/blocks');

  await reconcile(before);
  const { body: after } = await get('/chain/blocks');

  assert.equal(after.length, before.length);
  assert.equal(after[after.length - 1].hash, before[before.length - 1].hash);
});

test('two nodes merging the same pair agree', async () => {
  // Without a deterministic order the peers diverge further with every
  // reconcile instead of converging.
  const { body: base } = await get('/chain/blocks');
  const genesis = base[0];

  const a = chainFrom(genesis, [[transaction('a-1', { timestamp: 1756000005000 })]]);
  const b = chainFrom(genesis, [[transaction('b-1', { timestamp: 1756000004000 })]]);

  await reset([genesis]);
  await reconcile(a);
  await reconcile(b);
  const { body: first } = await get('/chain/blocks');

  await reset([genesis]);
  await reconcile(b);
  await reconcile(a);
  const { body: second } = await get('/chain/blocks');

  assert.deepEqual(first.map(block => block.hash),
                   second.map(block => block.hash),
                   'the merge order did not matter');
});

test('the same transaction arriving twice is stored once', async () => {
  const { body: base } = await get('/chain/blocks');
  const genesis = base[0];
  const shared = transaction('shared-1');

  await reset([genesis]);
  await reconcile(chainFrom(genesis, [[shared], [transaction('mine-1')]]));
  await reconcile(chainFrom(genesis, [[shared], [transaction('theirs-1')]]));

  const { body: after } = await get('/chain/blocks');
  const ids = after.flatMap(block => block.transactions.map(tx => tx.id));
  assert.equal(ids.filter(id => id === 'shared-1').length, 1);
  assert.ok(ids.includes('mine-1') && ids.includes('theirs-1'));
});

test('transactions sharing an id are both kept', async () => {
  // The id is a document identity and repeats: across the 2,748 local
  // transactions there are 185 distinct ids. Deduping on it would erase a
  // document's history.
  const { body: base } = await get('/chain/blocks');
  const genesis = base[0];

  await reset([genesis]);
  await reconcile(chainFrom(genesis, [
    [transaction('settings-workflows', { timestamp: 1756000001000 })],
    [transaction('settings-workflows', { timestamp: 1756000002000 })],
  ]));

  const { body: after } = await get('/chain/blocks');
  const ids = after.flatMap(block => block.transactions.map(tx => tx.id));
  assert.equal(ids.filter(id => id === 'settings-workflows').length, 2);
});

test('a chain with a different genesis is refused', async () => {
  // Not a fork of this chain but a different chain; merging them would
  // fabricate a history that never happened.
  const foreign = {
    index: 0, previousHash: '0', timestamp: 1, transactions: [], nonce: 0,
  };
  foreign.hash = blockHash(foreign);

  const { status, body } = await reconcile([foreign]);

  assert.equal(status, 409);
  assert.match(body.error, /genesis/i);
});

test('a tampered chain is refused whole', async () => {
  const { body: ours } = await get('/chain/blocks');
  const genesis = ours[0];
  const incoming = chainFrom(genesis, [[transaction('good')],
                                       [transaction('tampered')]]);
  incoming[2].transactions[0].counterparty = '@attacker';

  const before = ours.length;
  const { status } = await reconcile(incoming);

  assert.equal(status, 400);
  const { body: after } = await get('/chain/blocks');
  assert.equal(after.length, before, 'nothing from an unverified chain landed');
});

test('reconcile still requires the admin secret', async () => {
  const response = await fetch(`${BASE}/chain/reconcile`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chain: [] }),
  });
  assert.ok(response.status === 401 || response.status === 403,
            `expected an auth refusal, got ${response.status}`);
});

// ── who may sign ─────────────────────────────────────────────────────────────
//
// Verifying a signature against the public key the block itself carries proves
// only internal consistency: anyone can generate a key, sign with it, and
// produce a block that verifies. And a block carrying no signature at all used
// to skip the check entirely, so forging one was a matter of *deleting* its
// signature.

/** A one-block chain on top of `genesis`, signed by an arbitrary key. */
function signedBy(keyPair, genesis, txId) {
  const block = {
    index: 1, previousHash: genesis.hash, timestamp: 1756000011000,
    transactions: [transaction(txId)], nonce: 0,
  };
  block.hash = blockHash(block);
  block.signature = crypto.sign(null, Buffer.from(block.hash),
                                keyPair.privateKey).toString('hex');
  block.public_key = keyPair.publicKey.export({ type: 'spki', format: 'pem' });
  return [genesis, block];
}

async function authorities(method = 'GET', body, suffix = '') {
  const response = await fetch(`${BASE}/chain/authorities${suffix}`, {
    method,
    headers: { 'Content-Type': 'application/json',
               'x-tala-chain-secret': adminSecret },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  let parsed = {};
  try { parsed = await response.json(); } catch { /* empty */ }
  return { status: response.status, body: parsed };
}

test('the node seeds itself as a signer and nothing else', async () => {
  // A fresh node must trust its own key or it would reject the blocks it signs
  // itself, and it must trust nothing further, since who else may write is the
  // owner's decision.
  const seeded = JSON.parse(
    fs.readFileSync(path.join(dataDir, 'authorities.json'), 'utf8'));
  const bootstrapped = seeded.authorities.filter(
    entry => entry.added_by === 'bootstrap');

  assert.equal(bootstrapped.length, 1, 'exactly one key was seeded');

  const { body } = await authorities();
  const self = body.authorities.find(entry => entry.is_self);
  assert.ok(self, 'the node knows its own key');
  assert.equal(self.added_by, 'bootstrap');

  // And its own chain verifies as a result.
  const { body: status } = await get('/chain/status');
  assert.equal(status.isValid, true);
});

test('a chain signed by a stranger is refused', async () => {
  const stranger = crypto.generateKeyPairSync('ed25519');
  const { body: ours } = await get('/chain/blocks');
  const genesis = ours[0];

  const block = {
    index: 1, previousHash: genesis.hash, timestamp: 1756000009000,
    transactions: [transaction('theirs')], nonce: 0,
  };
  block.hash = blockHash(block);
  block.signature = crypto.sign(null, Buffer.from(block.hash),
                                stranger.privateKey).toString('hex');
  block.public_key = stranger.publicKey.export({ type: 'spki', format: 'pem' });

  const { status, body } = await reconcile([genesis, block]);

  assert.equal(status, 400);
  assert.match(body.reason, /not a permitted signer/);
});

test('deleting a signature no longer forges a block', async () => {
  // The hole underneath the other one: an unsigned block skipped verification
  // altogether, so any forged block passed by declining to name a signer.
  const { body: ours } = await get('/chain/blocks');
  const genesis = ours[0];

  const block = {
    index: 1, previousHash: genesis.hash, timestamp: 1756000009000,
    transactions: [transaction('forged')], nonce: 0,
  };
  block.hash = blockHash(block);
  block.signature = null;
  block.public_key = null;

  const { status, body } = await reconcile([genesis, block]);

  assert.equal(status, 400);
  assert.match(body.reason, /not signed/);
});

test('a permitted signer is accepted', async () => {
  // A fresh key, so this exercises admitting a signer rather than finding one
  // already admitted.
  const newcomer = crypto.generateKeyPairSync('ed25519');
  const pem = newcomer.publicKey.export({ type: 'spki', format: 'pem' });
  const { body: base } = await get('/chain/blocks');
  const genesis = base[0];

  const refused = await reconcile(signedBy(newcomer, genesis, 'before-admission'));
  assert.equal(refused.status, 400, 'refused before admission');

  const added = await authorities('POST', { public_key: pem, label: 'a peer' });
  assert.equal(added.status, 200);
  assert.equal(added.body.added, true);

  await reset([genesis]);
  const { status, body } = await reconcile(
    signedBy(newcomer, genesis, 'after-admission'));

  assert.equal(status, 200, 'accepted once admitted');
  assert.equal(body.outcome, 'behind');
});

test('admitting the same key twice is not an error', async () => {
  const pem = KEY.publicKey.export({ type: 'spki', format: 'pem' });

  const { status, body } = await authorities('POST', { public_key: pem });

  assert.equal(status, 200);
  assert.equal(body.added, false);
  assert.match(body.reason, /already/);
});

test('a key that cannot verify anything is refused', async () => {
  const { status, body } = await authorities('POST', { public_key: 'not a key' });

  assert.equal(status, 400);
  assert.match(body.error, /not a usable public key/i);
});

test('withdrawing permission stops future writes', async () => {
  const pem = KEY.publicKey.export({ type: 'spki', format: 'pem' });
  const print = crypto.createHash('sha256')
    .update(pem.replace(/\s+/g, '')).digest('hex').slice(0, 16);

  const removed = await authorities('DELETE', undefined, `/${print}`);
  assert.equal(removed.status, 200);

  const { body: base } = await get('/chain/blocks');
  await reset([base[0]]);
  const { status } = await reconcile(
    chainFrom(base[0], [[transaction('after-withdrawal')]]));

  assert.equal(status, 400);
});

test('this node cannot remove its own key', async () => {
  // It signs its own blocks; without its key it would reject its own chain.
  const { body: listed } = await authorities();
  const self = listed.authorities.find(entry => entry.is_self);

  const { status, body } = await authorities('DELETE', undefined, `/${self.fingerprint}`);

  assert.equal(status, 409);
  assert.match(body.error, /own key/i);
});

test('the authority list is admin-gated', async () => {
  // Which parties a node trusts is not for an anonymous caller to survey.
  const response = await fetch(`${BASE}/chain/authorities`);
  assert.ok(response.status === 401 || response.status === 403);
});
