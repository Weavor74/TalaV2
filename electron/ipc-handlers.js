/**
 * ipc-handlers.js — Tala Cognitive Engine Room
 *
 * Extracted IPC handler module imported by main.js.
 * Implements:
 *   - file:read   — sandboxed file read (confined to the data root)
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
 *
 * Data root: Tala is portable by default -- installed on a USB stick, it
 * must behave identically on any machine/OS it's plugged into, so the
 * sandbox is confined to the app's install/checkout directory
 * (app.getAppPath()), not Electron's OS-profile userData directory.
 * TALA_USER_DATA is the explicit escape hatch for anyone deliberately
 * running a locked-down/traditional install who wants OS-profile storage
 * instead. Mirrors main.js's resolveDataRoot() and
 * sdk/agent/documentation_nodes.py::resolve_user_data_dir() on the Python
 * side -- all three must agree, or the app, the harness, and this sandbox
 * end up looking at three different directories.
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

/**
 * Resolves the absolute path to the Python executable within the local
 * virtual environment. Mirrors main.js's venvPythonPath() -- kept as a
 * separate copy here (rather than importing main.js, which would create a
 * circular require) since it's a small, pure, cross-platform helper.
 * Handles Windows 'Scripts/python.exe' vs Unix 'bin/python'.
 */
function venvPythonPath(basePath) {
  const venvBinDir = process.platform === 'win32' ? 'Scripts' : 'bin';
  const pythonName = process.platform === 'win32' ? 'python.exe' : 'python';
  return path.join(basePath, '.venv', venvBinDir, pythonName);
}

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
// Windows and (by default) macOS volumes are case-insensitive, so containment
// must compare case-folded paths there: "C:\Tala" and "c:\tala" are the same
// directory, and comparing them case-sensitively yields spurious denials (or,
// worse, containment confusion). Linux stays case-sensitive.
function foldCase(p) {
  return (process.platform === 'win32' || process.platform === 'darwin')
    ? p.toLowerCase()
    : p;
}

async function safePath(baseDir, userPath) {
  if (!userPath || typeof userPath !== 'string') {
    throw new Error('Path must be a non-empty string.');
  }

  // Normalize separators and resolve relative to base
  const joined   = path.resolve(baseDir, userPath);
  const baseCmp  = foldCase(baseDir);

  // Attempt to resolve real path (follows symlinks) so traversal via
  // symlinks pointing outside the sandbox is caught.
  let realResolved;
  try {
    realResolved = await fs.realpath(joined);
  } catch {
    // If the path doesn't exist yet (write case), use the non-symlink path
    // but ensure the parent directory is safe.
    const parentReal = await fs.realpath(path.dirname(joined)).catch(() => path.dirname(joined));
    const parentCmp = foldCase(parentReal);
    if (!parentCmp.startsWith(baseCmp + path.sep) && parentCmp !== baseCmp) {
      throw new Error(`Access denied: parent of "${userPath}" is outside the sandbox.`);
    }
    realResolved = joined;
  }

  // Ensure the real resolved path begins with baseDir
  const resolvedCmp = foldCase(realResolved);
  const safe = resolvedCmp === baseCmp ||
               resolvedCmp.startsWith(baseCmp + path.sep);

  if (!safe) {
    throw new Error(`Access denied: "${userPath}" resolves outside the allowed directory.`);
  }

  return realResolved;
}

/**
 * UI-audit C4: file:* must not reach vault secrets while vault:read is gated.
 * Path confinement alone puts vault/ inside the sandbox root; this separates
 * the two the same way python/harness/webui.py._refuse_secret_path does.
 */
function refuseSecretPath(baseDir, resolved) {
  const baseCmp = foldCase(path.resolve(baseDir));
  const resolvedCmp = foldCase(path.resolve(resolved));
  let relative;
  if (resolvedCmp === baseCmp) return;
  if (!resolvedCmp.startsWith(baseCmp + path.sep)) return;
  relative = resolvedCmp.slice(baseCmp.length + 1).split(path.sep);
  if (!relative.length || (relative.length === 1 && relative[0] === '')) return;
  const top = relative[0];
  if (top === 'vault' || top === '.env') {
    throw new Error(`Access denied: "${relative.join('/')}" holds secrets and is not served by file:*.`);
  }
  if (top === 'blockchain' && relative[1] === 'data') {
    throw new Error(`Access denied: "${relative.join('/')}" holds the chain's keys and is not served by file:*.`);
  }
  const name = relative[relative.length - 1];
  if (name.endsWith('.pem') || name === 'wallet_seed_phrase.txt' || name === 'poa_keys.json' || name === 'admin_secret.txt') {
    throw new Error(`Access denied: "${relative.join('/')}" is key material and is not served by file:*.`);
  }
}

/**
 * Is this a Neural Node HTML file? Separator-normalized: a Windows renderer
 * sends `nodes\x.html`, which a bare includes('nodes/') misses, letting the
 * write skip schema validation and blockchain anchoring while still landing
 * on disk.
 */
function isNodesHtmlPath(filePath) {
  if (typeof filePath !== 'string') return false;
  const normalized = filePath.replace(/\\/g, '/');
  return normalized.includes('nodes/') && normalized.endsWith('.html');
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
  // Where the actual application source (sdk/, python/, scripts/) lives --
  // always the app bundle/checkout, never the data root, packaged or not.
  const appPath   = app.getAppPath();
  // The sandbox root for file:read/write/list/vault:read, and where the
  // .venv is expected. TALA_USER_DATA (if set) overrides; otherwise this
  // is appPath itself -- see the module doc comment above. Kept in the
  // variable name `userData` since it's still what every handler below
  // calls its confinement root; it just isn't Electron's userData API
  // anymore. Must match main.js's resolveDataRoot() exactly, or the app,
  // the harness, and this sandbox end up looking at different directories.
  const userData  = process.env.TALA_USER_DATA
    ? path.resolve(process.env.TALA_USER_DATA)
    : appPath;
  const vaultDir  = path.join(userData, 'vault');
  const pythonRuntimeBase = userData;

  /* ── file:read ──────────────────────────────────────────────────── */
  ipcMain.handle('file:read', async (_event, payload) => {
    const { filePath, encoding = 'utf8' } = payload ?? {};

    const resolved = await safePath(userData, filePath);
    refuseSecretPath(userData, resolved);

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

    if (isNodesHtmlPath(filePath)) {
      const fsSync = require('node:fs');
      const crypto = require('node:crypto');
      const os = require('node:os');
      const tmpPath = path.join(os.tmpdir(), `tala_val_${crypto.randomBytes(4).toString('hex')}.html`);
      fsSync.writeFileSync(tmpPath, data, 'utf8');
      
      const { execFile } = require('node:child_process');
      const util = require('node:util');
      const execFilePromise = util.promisify(execFile);
      
      try {
        // Was: path.join(userData, '..', '.venv', 'Scripts', 'python.exe')
        // -- assumed .venv sits as a *sibling* of userData (e.g.
        // AppData\Roaming\.venv on Windows), which was never true in dev
        // (.venv lives at the repo root) or packaged (.venv lives *inside*
        // userData, per main.js's ensureFirstRun()) -- always ENOENT.
        // cwd had the same bug: userData's parent has no sdk/agent/parser.py
        // to import regardless of platform or packaging state.
        const pythonPath = process.env.TALA_PYTHON_EXEC || venvPythonPath(pythonRuntimeBase);
        await execFilePromise(pythonPath, [
          '-c',
          'import sys; from sdk.agent.parser import parse_file; parse_file(sys.argv[1])',
          tmpPath
        ], { cwd: appPath });
      } catch (e) {
        fsSync.unlinkSync(tmpPath);
        throw new Error('Schema validation failed via backend parser: ' + (e.stderr || e.message));
      }
      fsSync.unlinkSync(tmpPath);
    }

    const resolved = await safePath(userData, filePath);
    refuseSecretPath(userData, resolved);

    // Ensure parent directory exists
    await fs.mkdir(path.dirname(resolved), { recursive: true });
    await fs.writeFile(resolved, data, { encoding });

    // Anchor Neural Node creation on blockchain (graceful fallback if offline)
    if (isNodesHtmlPath(filePath)) {
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
    refuseSecretPath(userData, resolved);

    // Verify it exists and is a regular file
    const stat = await fs.stat(resolved);
    if (!stat.isFile()) {
      throw new Error(`"${filePath}" is not a regular file and cannot be deleted.`);
    }

    await fs.unlink(resolved);

    // Anchor Neural Node deletion on blockchain (graceful fallback if offline)
    if (isNodesHtmlPath(filePath)) {
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
    const { dir = '.', recursive = false, withSizes = false } = payload ?? {};

    const resolved = await safePath(userData, dir);
    refuseSecretPath(userData, resolved);

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

    const entries = await listDir(resolved, resolved, recursive, 0, MAX_LIST_ENTRIES, withSizes);
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
      const path = require('node:path');
      const workspaceRoot = path.join(__dirname, '..');
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
// Directories the Explorer must never walk. Without this, listDir recursed into
// .venv (37,718 files), .git (4,418) and node_modules (11,504) — and because
// readdir returns entries in name order, those dot-directories are reached
// before renderer/, python/, sdk/ or nodes/. The 10,000-entry cap was spent
// long before any file the user cares about, so the tree came back empty of
// anything useful (UI-115).
const LIST_IGNORED_DIRS = new Set([
  'node_modules', '.git', '.venv', 'venv', '.tala_snapshots', 'dist',
  '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
  '.idea', '.vscode', 'build', '.next', '.cache', 'chroma',
]);

async function listDir(dir, base, recursive, depth, limit, withSizes = false) {
  if (depth > 20 || limit <= 0) return [];

  const dirents = await fs.readdir(dir, { withFileTypes: true });
  const entries = [];

  for (const dirent of dirents) {
    if (entries.length >= limit) break;
    if (dirent.isDirectory() && LIST_IGNORED_DIRS.has(dirent.name)) continue;

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

    // A stat per file cost ~10,000 syscalls on a full walk and nothing renders
    // the size, so it is skipped unless the caller asks (UI-115).
    if (dirent.isFile() && withSizes) {
      try {
        const st  = await fs.stat(abs);
        entry.size = st.size;
      } catch { /* ignore stat errors */ }
    }

    entries.push(entry);

    if (recursive && dirent.isDirectory()) {
      const children = await listDir(
        abs, base, true, depth + 1, limit - entries.length, withSizes
      );
      entries.push(...children);
    }
  }

  return entries;
}

// The pure helpers are exported for the jest suite (tests/electron/): the
// containment guard is exactly where sandbox-escape bugs live, so it gets
// direct unit coverage in addition to the handler-level tests.
module.exports = { registerHandlers, safePath, refuseSecretPath, foldCase, isNodesHtmlPath, listDir };
