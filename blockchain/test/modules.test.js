/**
 * The node is split into modules (fs_util, request_limits, identity,
 * chain_model, authority_set, peer_directory). AuthoritySet and PeerDirectory
 * used to read app.js globals; they now get their data file and the ledger
 * injected. These drive the real server over HTTP, because the failure
 * modes here are silent ones: PeerDirectory anchors inside a try/catch that
 * only logs, so a broken ledger link would simply stop anchoring.
 *
 *     node --test blockchain/test/modules.test.js
 */

const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { test, before, after } = require('node:test');

const APP = path.join(__dirname, '..', 'src', 'app.js');
const PORT = 8393;
const BASE = `http://127.0.0.1:${PORT}`;

let dataDir;
let server;

function start() {
  return new Promise((resolve, reject) => {
    server = spawn(process.execPath, [APP], {
      env: { ...process.env, TALA_CHAIN_DATA_DIR: dataDir, TALA_CHAIN_BIND_PORT: String(PORT) },
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

async function get(pathname) {
  const response = await fetch(BASE + pathname);
  return { status: response.status, body: await response.json() };
}

before(async () => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tala-chain-modules-'));
  await start();
});

after(async () => {
  await stop();
  fs.rmSync(dataDir, { recursive: true, force: true });
});

const PEER = {
  username: '@modular',
  wallet_address: 'wallet-modular-1',
  endpoint_url: 'ws://203.0.113.9:8765',
};

test('registering a peer anchors it on the ledger through the injected ledger', async () => {
  const response = await fetch(`${BASE}/chain/peers/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(PEER),
  });
  assert.equal(response.status, 201);

  const { body: chain } = await get('/chain/blocks');
  const anchors = chain.flatMap(b => b.transactions)
    .filter(t => t.id === `peer-directory:${PEER.wallet_address}`);
  assert.equal(anchors.length, 1, 'no anchor transaction: the directory lost its link to the ledger');
  assert.equal(anchors[0].type, 'peer-directory-register');
  assert.equal(anchors[0].creator, PEER.username);
  assert.equal(anchors[0].metadata.directory_entry.wallet_address, PEER.wallet_address);
});

test('an id with a colon can be read back by id, and a recent-blocks limit is honoured', async () => {
  const { status, body } = await get(`/chain/tx/${encodeURIComponent(`peer-directory:${PEER.wallet_address}`)}`);
  assert.equal(status, 200, 'ids containing : used to match no route and 404');
  assert.equal(body.transaction.type, 'peer-directory-register');

  const { body: whole } = await get('/chain/blocks');
  const { body: tail } = await get('/chain/blocks?limit=1');
  assert.ok(whole.length >= 2);
  assert.equal(tail.length, 1);
  assert.equal(tail[0].hash, whole[whole.length - 1].hash, 'limit must return the NEWEST blocks');
  assert.equal((await get('/chain/blocks?limit=0')).body.length, whole.length, 'no usable limit means the whole chain');
});

test('registering the same peer again is recorded as an update', async () => {
  await fetch(`${BASE}/chain/peers/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...PEER, endpoint_url: 'ws://203.0.113.9:9999' }),
  });
  const { body: chain } = await get('/chain/blocks');
  const types = chain.flatMap(b => b.transactions)
    .filter(t => t.id === `peer-directory:${PEER.wallet_address}`)
    .map(t => t.type);
  assert.deepEqual(types, ['peer-directory-register', 'peer-directory-update']);
});

test('the directory and the authority set live in the configured data directory', () => {
  assert.ok(fs.existsSync(path.join(dataDir, 'peers.json')));
  assert.ok(fs.existsSync(path.join(dataDir, 'authorities.json')));
  const peers = JSON.parse(fs.readFileSync(path.join(dataDir, 'peers.json'), 'utf8'));
  assert.equal(peers.peers[0].username, PEER.username);
});

test('both survive a restart', async () => {
  await stop();
  await start();

  const { status, body } = await get(`/chain/peers/${encodeURIComponent(PEER.wallet_address)}`);
  assert.equal(status, 200);
  assert.equal(body.peer.endpoint_url, 'ws://203.0.113.9:9999');

  const secret = fs.readFileSync(path.join(dataDir, 'admin_secret.txt'), 'utf8').trim();
  const response = await fetch(`${BASE}/chain/authorities`, { headers: { 'x-tala-chain-secret': secret } });
  assert.equal(response.status, 200);
  const authorities = await response.json();
  const list = authorities.authorities || authorities;
  assert.equal(list.length, 1, 'the node must still trust exactly its own key');
  assert.equal(list[0].is_self, true);
});
