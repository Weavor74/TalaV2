/**
 * Electron's Neural Node anchors (file:write / file:delete in ipc-handlers.js).
 *
 * They wrote port 8332 into themselves and sent no admin secret, so on any
 * moved port they went nowhere and on the default port the node refused every
 * one with a 401 that the empty error handler swallowed.
 */
'use strict';

const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const {
  chainHost,
  chainPort,
  chainDataDir,
  chainAdminHeaders,
  chainHoldsDocuments,
  assertMayMintDocumentKey,
  postTransaction,
} = require('../../electron/chain_endpoint.js');

const VARS = [
  'TALA_CHAIN_BIND_HOST', 'TALA_CHAIN_BIND_PORT', 'TALA_CHAIN_PORT',
  'TALA_CHAIN_DATA_DIR', 'TALA_CHAIN_ADMIN_SECRET', 'TALA_ALLOW_NEW_CHAIN_KEY',
];

describe('chain endpoint', () => {
  const saved = {};
  let root;

  beforeEach(() => {
    for (const v of VARS) { saved[v] = process.env[v]; delete process.env[v]; }
    root = fs.mkdtempSync(path.join(os.tmpdir(), 'tala-chain-ep-'));
  });
  afterEach(() => {
    for (const v of VARS) {
      if (saved[v] === undefined) delete process.env[v]; else process.env[v] = saved[v];
    }
    fs.rmSync(root, { recursive: true, force: true });
  });

  test('defaults match the node', () => {
    expect(chainHost()).toBe('127.0.0.1');
    expect(chainPort()).toBe(8332);
  });

  test('follows the variables the node reads, BIND_PORT first', () => {
    process.env.TALA_CHAIN_PORT = '9001';
    expect(chainPort()).toBe(9001);
    process.env.TALA_CHAIN_BIND_PORT = '9002';
    expect(chainPort()).toBe(9002);
  });

  test('a nonsense port falls back, and a wildcard host is not somewhere to connect', () => {
    process.env.TALA_CHAIN_BIND_PORT = 'banana';
    expect(chainPort()).toBe(8332);
    process.env.TALA_CHAIN_BIND_PORT = '99999';
    expect(chainPort()).toBe(8332);
    process.env.TALA_CHAIN_BIND_HOST = '0.0.0.0';
    expect(chainHost()).toBe('127.0.0.1');
  });

  test('the admin secret is read from where Electron spawns the node', () => {
    const dir = path.join(root, 'blockchain', 'data');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'admin_secret.txt'), 's3cret\n');
    expect(chainDataDir(root)).toBe(dir);
    expect(chainAdminHeaders(root)).toEqual({ 'x-tala-chain-secret': 's3cret' });
  });

  test('TALA_CHAIN_DATA_DIR wins, matching the node and the spawn', () => {
    const dir = path.join(root, 'elsewhere');
    fs.mkdirSync(dir);
    fs.writeFileSync(path.join(dir, 'admin_secret.txt'), 'other');
    process.env.TALA_CHAIN_DATA_DIR = dir;
    expect(chainAdminHeaders(root)).toEqual({ 'x-tala-chain-secret': 'other' });
  });

  test('no secret yet means no header, not a crash', () => {
    expect(chainAdminHeaders(root)).toEqual({});
  });

  describe('document key fail-closed', () => {
    function chainWith(documents) {
      const dir = path.join(root, 'blockchain', 'data');
      fs.mkdirSync(dir, { recursive: true });
      const txs = documents
        ? [{ id: 'node-x', document: { encrypted: true, ciphertext: 'AAAA' } }]
        : [{ id: 'plain' }];
      fs.writeFileSync(path.join(dir, 'chain.json'), JSON.stringify([{ index: 1, transactions: txs }]));
    }

    test('sees documents on the chain, and their absence', () => {
      expect(chainHoldsDocuments(root)).toBe(false); // no chain at all
      chainWith(false);
      expect(chainHoldsDocuments(root)).toBe(false);
      chainWith(true);
      expect(chainHoldsDocuments(root)).toBe(true);
    });

    test('finds a document that straddles a read boundary', () => {
      const dir = path.join(root, 'blockchain', 'data');
      fs.mkdirSync(dir, { recursive: true });
      // push the marker across the 1 MiB chunk edge
      const pad = ' '.repeat((1 << 20) - 5);
      fs.writeFileSync(path.join(dir, 'chain.json'), `${pad}{"ciphertext":"AAAA"}`);
      expect(chainHoldsDocuments(root)).toBe(true);
    });

    test('a replacement key is refused beside existing documents', () => {
      chainWith(true);
      expect(() => assertMayMintDocumentKey(root, '/some/key.bin')).toThrow(/already holds encrypted documents/);
      try { assertMayMintDocumentKey(root, '/some/key.bin'); } catch (err) { expect(err.code).toBe('CHAIN_KEY_MISSING'); }
    });

    test('the first key is allowed on an empty chain, and deliberate rotation on a full one', () => {
      chainWith(false);
      expect(() => assertMayMintDocumentKey(root, '/some/key.bin')).not.toThrow();
      chainWith(true);
      process.env.TALA_ALLOW_NEW_CHAIN_KEY = '1';
      expect(() => assertMayMintDocumentKey(root, '/some/key.bin')).not.toThrow();
    });

    test('follows TALA_CHAIN_DATA_DIR, the directory the node reads', () => {
      chainWith(true);
      process.env.TALA_CHAIN_DATA_DIR = path.join(root, 'elsewhere');
      expect(chainHoldsDocuments(root)).toBe(false);
    });
  });

  describe('postTransaction', () => {
    let server;
    let received;

    function listen(status) {
      return new Promise(resolve => {
        server = http.createServer((req, res) => {
          let body = '';
          req.on('data', c => { body += c; });
          req.on('end', () => {
            received = { url: req.url, method: req.method, headers: req.headers, body };
            res.writeHead(status, { 'Content-Type': 'application/json' });
            res.end('{}');
          });
        }).listen(0, '127.0.0.1', () => {
          process.env.TALA_CHAIN_BIND_PORT = String(server.address().port);
          resolve();
        });
      });
    }
    afterEach(() => new Promise(resolve => (server ? server.close(resolve) : resolve())));

    test('sends the body to the configured port with the admin secret', async () => {
      const dir = path.join(root, 'blockchain', 'data');
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, 'admin_secret.txt'), 'local-secret');
      await listen(201);

      const result = await postTransaction(root, JSON.stringify({ id: 'node-x' }));

      expect(result).toEqual({ ok: true, status: 201 });
      expect(received.method).toBe('POST');
      expect(received.url).toBe('/chain/tx');
      expect(received.headers['x-tala-chain-secret']).toBe('local-secret');
      expect(JSON.parse(received.body).id).toBe('node-x');
    });

    test('a refusal is reported, not swallowed', async () => {
      const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
      await listen(401);
      const result = await postTransaction(root, '{}');
      expect(result).toEqual({ ok: false, status: 401 });
      expect(warn).toHaveBeenCalledWith(expect.stringContaining('401'));
      warn.mockRestore();
    });

    test('an offline node resolves instead of throwing', async () => {
      process.env.TALA_CHAIN_BIND_PORT = '1';
      const result = await postTransaction(root, '{}');
      expect(result.ok).toBe(false);
      expect(result.status).toBe(0);
    });
  });
});
