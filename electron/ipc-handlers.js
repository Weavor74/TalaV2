/**
 * ipc-handlers.js — Tala Cognitive Engine Room
 *
 * Extracted IPC handler module imported by main.js.
 * Implements:
 *   - file:read   — sandboxed file read (confined to userData)
 *   - file:write  — sandboxed file write
 *   - file:list   — sandboxed directory listing
 *   - vault:read  — read from vault directory with extra network-isolation guard
 *
 * Security principles:
 *   - All paths are resolved to absolute paths and verified to be within
 *     the allowed base directory before any I/O takes place.
 *   - Symlinks are resolved before the containment check (TOCTOU mitigation).
 *   - vault:read additionally checks that the caller environment has not
 *     opened any outbound connections (heuristic flag set by main.js).
 */

'use strict';

const path = require('node:path');
const fs   = require('node:fs/promises');
const { app, ipcMain } = require('electron');

/* ──────────────────────────────────────────────────────────────────
   Constants / config
   ────────────────────────────────────────────────────────────────── */

/** Maximum size (bytes) for a single file:read to avoid OOM on huge files. */
const MAX_READ_BYTES = 50 * 1024 * 1024; // 50 MB

/** Maximum number of entries returned by file:list. */
const MAX_LIST_ENTRIES = 10_000;

const CHAIN_DOCUMENT_KEY_FILE = 'blockchain_document_key.bin';

async function loadOrCreateChainDocumentKey(userData) {
  const crypto = require('node:crypto');
  if (process.env.TALA_CHAIN_DOCUMENT_KEY) {
    const key = Buffer.from(process.env.TALA_CHAIN_DOCUMENT_KEY, 'base64');
    if (key.length !== 32) {
      throw new Error('TALA_CHAIN_DOCUMENT_KEY must decode to 32 bytes.');
    }
    return key;
  }

  const configured = process.env.TALA_CHAIN_DOCUMENT_KEY_FILE;
  const keyPath = configured
    ? path.resolve(configured)
    : path.join(userData, 'vault', '.keys', CHAIN_DOCUMENT_KEY_FILE);

  try {
    const key = await fs.readFile(keyPath);
    if (key.length !== 32) {
      throw new Error(`Invalid blockchain document key length at ${keyPath}.`);
    }
    return key;
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
  }

  const key = crypto.randomBytes(32);
  await fs.mkdir(path.dirname(keyPath), { recursive: true });
  await fs.writeFile(keyPath, key, { mode: 0o600 });
  return key;
}

async function encryptChainDocument(userData, document, associatedData) {
  const crypto = require('node:crypto');
  const key = await loadOrCreateChainDocumentKey(userData);
  const nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, nonce);
  cipher.setAAD(Buffer.from(associatedData, 'utf8'));
  const plaintext = Buffer.from(JSON.stringify(document), 'utf8');
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const tag = cipher.getAuthTag();
  return {
    encrypted: true,
    algorithm: 'AES-256-GCM',
    key_id: 'local-vault:blockchain-document-key:v1',
    nonce: nonce.toString('base64'),
    ciphertext: ciphertext.toString('base64'),
    tag: tag.toString('base64'),
    aad: associatedData
  };
}

/* ──────────────────────────────────────────────────────────────────
   Path containment guard
   ────────────────────────────────────────────────────────────────── */

/**
 * Resolve `userPath` relative to `baseDir` and verify the resolved path
 * is strictly inside `baseDir`.  Throws a descriptive Error if not.
 *
 * @param {string} baseDir   — Absolute, already-resolved base directory.
 * @param {string} userPath  — User-supplied relative (or absolute) path.
 * @returns {Promise<string>} Resolved absolute path that is safe to use.
 */
async function safePath(baseDir, userPath) {
  if (!userPath || typeof userPath !== 'string') {
    throw new Error('Path must be a non-empty string.');
  }

  // Normalize separators and resolve relative to base
  const joined   = path.resolve(baseDir, userPath);

  // Attempt to resolve real path (follows symlinks) so traversal via
  // symlinks pointing outside the sandbox is caught.
  let realResolved;
  try {
    realResolved = await fs.realpath(joined);
  } catch {
    // If the path doesn't exist yet (write case), use the non-symlink path
    // but ensure the parent directory is safe.
    const parentReal = await fs.realpath(path.dirname(joined)).catch(() => path.dirname(joined));
    if (!parentReal.startsWith(baseDir + path.sep) && parentReal !== baseDir) {
      throw new Error(`Access denied: parent of "${userPath}" is outside the sandbox.`);
    }
    realResolved = joined;
  }

  // Ensure the real resolved path begins with baseDir
  const safe = realResolved === baseDir ||
               realResolved.startsWith(baseDir + path.sep);

  if (!safe) {
    throw new Error(`Access denied: "${userPath}" resolves outside the allowed directory.`);
  }

  return realResolved;
}

/* ──────────────────────────────────────────────────────────────────
   Registration
   ────────────────────────────────────────────────────────────────── */

/**
 * Register all IPC handlers.
 *
 * @param {{
 *   isNetworkIsolated: () => boolean
 * }} opts
 */
function registerHandlers({ isNetworkIsolated } = {}) {
  const userData  = app.getPath('userData');
  const vaultDir  = path.join(userData, 'vault');

  /* ── file:read ──────────────────────────────────────────────────── */
  ipcMain.handle('file:read', async (_event, payload) => {
    const { filePath, encoding = 'utf8' } = payload ?? {};

    const resolved = await safePath(userData, filePath);

    // Stat first to avoid reading huge files accidentally
    const stat = await fs.stat(resolved);
    if (!stat.isFile()) throw new Error(`"${filePath}" is not a regular file.`);
    if (stat.size > MAX_READ_BYTES) {
      throw new Error(`File exceeds maximum read size (${MAX_READ_BYTES} bytes).`);
    }

    const data = await fs.readFile(resolved, { encoding });
    const formattedPath = resolved.replace(/\\/g, '/');
    const fileUrl = `file:///${formattedPath.startsWith('/') ? formattedPath.slice(1) : formattedPath}`;
    return { ok: true, data, size: stat.size, absolutePath: resolved, fileUrl };
  });

  /* ── file:write ─────────────────────────────────────────────────── */
  ipcMain.handle('file:write', async (_event, payload) => {
    const { filePath, data, encoding = 'utf8' } = payload ?? {};

    if (data === undefined || data === null) {
      throw new Error('file:write requires a "data" field.');
    }

    if (filePath.includes('nodes/') && filePath.endsWith('.html')) {
      const fsSync = require('node:fs');
      const crypto = require('node:crypto');
      const os = require('node:os');
      const tmpPath = path.join(os.tmpdir(), `tala_val_${crypto.randomBytes(4).toString('hex')}.html`);
      fsSync.writeFileSync(tmpPath, data, 'utf8');
      
      const { execFile } = require('node:child_process');
      const util = require('node:util');
      const execFilePromise = util.promisify(execFile);
      
      try {
        const pythonPath = process.env.TALA_PYTHON_EXEC || path.join(userData, '..', '.venv', 'Scripts', 'python.exe');
        await execFilePromise(pythonPath, [
          '-c', 
          'import sys; from sdk.agent.parser import parse_file; parse_file(sys.argv[1])', 
          tmpPath
        ], { cwd: path.join(userData, '..') });
      } catch (e) {
        fsSync.unlinkSync(tmpPath);
        throw new Error('Schema validation failed via backend parser: ' + (e.stderr || e.message));
      }
      fsSync.unlinkSync(tmpPath);
    }

    const resolved = await safePath(userData, filePath);

    // Ensure parent directory exists
    await fs.mkdir(path.dirname(resolved), { recursive: true });
    await fs.writeFile(resolved, data, { encoding });

    // Anchor Neural Node creation on blockchain (graceful fallback if offline)
    if (filePath.includes('nodes/') && filePath.endsWith('.html')) {
      try {
        const crypto = require('crypto');
        const hash = crypto.createHash('sha256').update(data).digest('hex');
        const slug = path.basename(filePath, '.html');
        const http = require('http');
        const document = await encryptChainDocument(userData, {
          document_type: 'html-nn',
          encoding,
          path: filePath,
          content_html: data
        }, `node-${slug}:${hash}`);

        const txData = JSON.stringify({
          id: `node-${slug}`,
          type: 'node-create',
          hash: hash,
          creator: 'tala-system',
          counterparty: 'blockchain-ledger',
          document,
          metadata: {
            storage_policy: 'local-blockchain-encrypted-document',
            content_hash_algorithm: 'sha256',
            document_type: 'html-nn',
            encryption: 'AES-256-GCM'
          }
        });

        const req = http.request({
          hostname: '127.0.0.1',
          port: 8332,
          path: '/chain/tx',
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(txData)
          }
        }, (res) => {
          res.on('data', () => {});
        });
        req.on('error', () => { /* fallback if offline */ });
        req.write(txData);
        req.end();
      } catch (err) {
        // ignore errors so file write succeeds regardless
      }
    }

    const formattedPath = resolved.replace(/\\/g, '/');
    const fileUrl = `file:///${formattedPath.startsWith('/') ? formattedPath.slice(1) : formattedPath}`;
    return { ok: true, path: resolved, fileUrl };
  });

  /* ── file:delete ────────────────────────────────────────────────── */
  ipcMain.handle('file:delete', async (_event, payload) => {
    const { filePath } = payload ?? {};

    if (!filePath) {
      throw new Error('file:delete requires a "filePath" field.');
    }

    const resolved = await safePath(userData, filePath);

    // Verify it exists and is a regular file
    const stat = await fs.stat(resolved);
    if (!stat.isFile()) {
      throw new Error(`"${filePath}" is not a regular file and cannot be deleted.`);
    }

    await fs.unlink(resolved);

    // Anchor Neural Node deletion on blockchain (graceful fallback if offline)
    if (filePath.includes('nodes/') && filePath.endsWith('.html')) {
      try {
        const slug = path.basename(filePath, '.html');
        const http = require('http');

        const txData = JSON.stringify({
          id: `node-${slug}`,
          type: 'node-delete',
          hash: '0000000000000000000000000000000000000000000000000000000000000000',
          creator: 'tala-system',
          counterparty: 'blockchain-ledger'
        });

        const req = http.request({
          hostname: '127.0.0.1',
          port: 8332,
          path: '/chain/tx',
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(txData)
          }
        }, (res) => {
          res.on('data', () => {});
        });
        req.on('error', () => { /* fallback if offline */ });
        req.write(txData);
        req.end();
      } catch (err) {
        // ignore
      }
    }

    return { ok: true, path: resolved };
  });

  /* ── file:list ──────────────────────────────────────────────────── */
  ipcMain.handle('file:list', async (_event, payload) => {
    const { dir = '.', recursive = false } = payload ?? {};

    const resolved = await safePath(userData, dir);

    // Ensure it exists and is a directory
    let stat;
    try {
      stat = await fs.stat(resolved);
    } catch {
      // Return empty list if directory doesn't exist yet
      return { ok: true, entries: [] };
    }

    if (!stat.isDirectory()) {
      throw new Error(`"${dir}" is not a directory.`);
    }

    const entries = await listDir(resolved, resolved, recursive, 0, MAX_LIST_ENTRIES);
    return { ok: true, entries };
  });

  /* ── vault:read ─────────────────────────────────────────────────── */
  ipcMain.handle('vault:read', async (_event, payload) => {
    // Extra guard: the vault must only be read when network isolation is active.
    // isNetworkIsolated is a runtime flag set by main.js based on current WS state.
    if (typeof isNetworkIsolated === 'function' && !isNetworkIsolated()) {
      throw new Error(
        'vault:read blocked: network connections are active. ' +
        'Vault access requires network isolation mode.'
      );
    }

    const { filePath, encoding = 'utf8' } = payload ?? {};

    // Ensure vault directory exists
    await fs.mkdir(vaultDir, { recursive: true });

    const resolved = await safePath(vaultDir, filePath);

    const stat = await fs.stat(resolved);
    if (!stat.isFile()) throw new Error(`"${filePath}" is not a regular file.`);
    if (stat.size > MAX_READ_BYTES) {
      throw new Error(`Vault file exceeds maximum read size.`);
    }

    const data = await fs.readFile(resolved, { encoding });
    return { ok: true, data, size: stat.size, isolated: true };
  });

  /* ── git:status ─────────────────────────────────────────────────── */
  ipcMain.handle('git:status', async () => {
    const { exec } = require('node:child_process');
    const util = require('node:util');
    const execPromise = util.promisify(exec);
    try {
      const workspaceRoot = 'D:\\src\\TalaV2';
      const { stdout, stderr } = await execPromise('git status --short', { cwd: workspaceRoot });
      return { ok: true, stdout, stderr };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });
}

/* ──────────────────────────────────────────────────────────────────
   Internal helpers
   ────────────────────────────────────────────────────────────────── */

/**
 * Recursively list a directory, returning entries relative to `base`.
 *
 * @param {string} dir      — Absolute path of the directory to list.
 * @param {string} base     — The sandbox base (for relative path calculation).
 * @param {boolean} recursive
 * @param {number} depth    — Current recursion depth (prevents infinite loops).
 * @param {number} limit    — Max entries remaining.
 * @returns {Promise<Array<{name:string, relativePath:string, type:string, size?:number}>>}
 */
async function listDir(dir, base, recursive, depth, limit) {
  if (depth > 20 || limit <= 0) return [];

  const dirents = await fs.readdir(dir, { withFileTypes: true });
  const entries = [];

  for (const dirent of dirents) {
    if (entries.length >= limit) break;

    const abs  = path.join(dir, dirent.name);
    const rel  = path.relative(base, abs);
    const type = dirent.isDirectory() ? 'directory' : dirent.isFile() ? 'file' : 'other';

    const formattedPath = abs.replace(/\\/g, '/');
    const fileUrl = `file:///${formattedPath.startsWith('/') ? formattedPath.slice(1) : formattedPath}`;
    const entry = {
      name: dirent.name,
      relativePath: rel,
      type,
      absolutePath: abs,
      fileUrl
    };

    if (dirent.isFile()) {
      try {
        const st  = await fs.stat(abs);
        entry.size = st.size;
      } catch { /* ignore stat errors */ }
    }

    entries.push(entry);

    if (recursive && dirent.isDirectory()) {
      const children = await listDir(
        abs, base, true, depth + 1, limit - entries.length
      );
      entries.push(...children);
    }
  }

  return entries;
}

module.exports = { registerHandlers };
