/**
 * main.js — Tala Cognitive Engine Room — Electron Main Process
 *
 * Responsibilities:
 *  1. Create and manage the BrowserWindow with strict security settings.
 *  2. Register IPC handlers (delegated to ipc-handlers.js).
 *  3. Spawn the Python harness (python/harness/server.py) as a child process.
 *     - Restart on crash with exponential back-off, max 3 retries.
 *     - Pipe stdout/stderr to the Electron logger.
 *  4. Maintain a WebSocket client that connects to ws://127.0.0.1:8765.
 *     - Relay python:rpc IPC calls from the renderer → WS → Python.
 *     - Return JSON-RPC responses back to the renderer via ipcMain.handle.
 *  5. Clean up all child processes on app quit.
 *
 * Security model (renderer):
 *   contextIsolation: true  | nodeIntegration: false
 *   sandbox: true           | webSecurity: true
 */

'use strict';

// A console.log/console.error write must never take the app down. stdout and
// stderr belong to whatever launched us — a terminal, npm, a wrapper script,
// a CI harness — and if that parent closes its end of the pipe (e.g. it piped
// our output through `head`, or simply exited), every later console write
// raises EPIPE. Node surfaces that as an uncaught exception in the main
// process: confirmed live 2026-08-11, the "[python] ..." relay at the harness
// stdout handler crashed the whole app with the "A JavaScript error occurred
// in the main process" dialog mid-session. Logging is best-effort; a broken
// log pipe is not an application error.
for (const stream of [process.stdout, process.stderr]) {
  if (stream && typeof stream.on === 'function') {
    stream.on('error', () => { /* swallow — nowhere left to report it */ });
  }
}

const {
  app,
  BrowserWindow,
  ipcMain,
  session,
} = require('electron');

const path    = require('node:path');
const fs      = require('node:fs');
const { spawn, spawnSync }  = require('node:child_process');
const { registerHandlers } = require('./ipc-handlers');
const { rpcTimeoutMs } = require('./rpc_policy');
const { chainPort } = require('./chain_endpoint');
const { initBlockchain, spawnBlockchain, stopBlockchain } = require('./blockchain-process');
const { ensureWsToken: ensureWsTokenFile, ensureWebUiToken: ensureWebUiTokenFile } = require('./token_files');
const {
  fetchExpectedUpdateHash,
  fetchPublishedVersion,
  getInstalledVersion,
  isNewerVersion,
  injectOwnerSecret,
} = require('./harness_admin');
const { buildWindowsUpdateScript, buildUnixUpdateScript } = require('./update_scripts');

// Lazily require 'ws' (installed via npm). We import it here so the entire
// module doesn't fail if ws isn't yet installed (first run before npm install).
let WebSocket;
try {
  WebSocket = require('ws');
} catch {
  WebSocket = null;
}

// electron-store for persisted configuration
let Store;
try {
  const storeModule = require('electron-store');
  Store = storeModule.default || storeModule;
} catch {
  Store = null;
}

/* ──────────────────────────────────────────────────────────────────
   Constants
   ────────────────────────────────────────────────────────────────── */

const IS_DEV        = process.argv.includes('--dev');
const HARNESS_HOST  = '127.0.0.1';
const HARNESS_PORT  = 8765;
const HARNESS_URL   = `ws://${HARNESS_HOST}:${HARNESS_PORT}`;
const MAX_RESTARTS  = 3;
const RESTART_BASE_DELAY_MS = 1_500; // base for exponential back-off

const { venvPythonPath, systemPythonCommand, posixShellCommand } = require('./runtime_commands');

/**
 * Resolves the directory Tala's data (.venv, .env, nodes/, workflows/,
 * vault/, chat_mode.json, ...) lives under. Tala is portable by default:
 * installed on a USB stick, it must behave identically on any machine/OS
 * it's plugged into, with data traveling on the drive rather than
 * scattering into whichever host machine's OS profile happened to be
 * active. TALA_USER_DATA is the explicit escape hatch for anyone
 * deliberately running a locked-down/traditional install (e.g. Program
 * Files, where the install directory isn't writable by a standard user)
 * who wants OS-profile storage instead. Mirrors
 * sdk/agent/documentation_nodes.py::resolve_user_data_dir() on the Python
 * side and ipc-handlers.js's registerHandlers() for the IPC file sandbox.
 */
function resolveDataRoot(appPath) {
  const override = process.env.TALA_USER_DATA;
  return override ? path.resolve(override) : appPath;
}

/**
 * Read -- or create -- the long-lived secret for harness WebSocket port 8765.
 *
 * Mirrors python/harness/ws_auth.py::load_or_create_token(). Electron does it
 * *before* spawning Python and passes the value in the environment, so the two
 * cannot disagree and there is no window in which the renderer is connecting
 * while the harness is still writing the file.
 *
 * Only TALA_WS_TOKEN may pin this value (UI-audit C3). TALA_WEB_TOKEN is a
 * distinct HTTP UI credential and must never be copied here.
 *
 * @param {string} appPath
 * @returns {string}
 */
function ensureWsToken(appPath) {
  return ensureWsTokenFile(resolveDataRoot(appPath));
}

/**
 * Read -- or create -- the HTTP UI token for port 8767.
 *
 * Mirrors python/harness/webui.py::_load_or_create_web_token(). Deliberately a
 * DIFFERENT secret from the WS token: the UI token may appear in a browser URL
 * / history, while the WS token unlocks every RPC on 8765. Never copy
 * ws_token.txt / TALA_WS_TOKEN into TALA_WEB_TOKEN (UI-audit C3).
 *
 * Browser clients on 8767 stay ticket-only for 8765; Electron's main-process
 * WS client uses the WS credential via Authorization header instead.
 *
 * @param {string} appPath
 * @returns {string}
 */
function ensureWebUiToken(appPath) {
  return ensureWebUiTokenFile(resolveDataRoot(appPath));
}

function firstRunEnv(appPath) {
  const dataRoot = resolveDataRoot(appPath);
  const venvDir = path.join(dataRoot, '.venv');
  const envPath = path.join(dataRoot, '.env');
  // Mint BOTH secrets before spawn so Python and Electron agree, and so the
  // HTTP UI token is never the same string as the long-lived WS secret.
  const wsToken = ensureWsToken(appPath);
  const webUiToken = ensureWebUiToken(appPath);
  return {
    ...process.env,
    TALA_INSTALL_ROOT: appPath,
    TALA_USER_DATA: dataRoot,
    TALA_USER_DATA_ROOT: dataRoot,
    TALA_VENV_DIR: venvDir,
    TALA_ENV_PATH: envPath,
    TALA_WS_PORT: String(HARNESS_PORT),
    // 8765 long-lived credential (SEC-W0-03 / UI-audit C3).
    TALA_WS_TOKEN: wsToken,
    // 8767 HTTP UI credential — distinct from TALA_WS_TOKEN.
    TALA_WEB_TOKEN: webUiToken,
    PYTHONUNBUFFERED: '1',
    // Serve the same renderer to a browser alongside the desktop window.
    //
    // Without this the web UI existed only when the harness was started by
    // `npm run web`, so opening http://127.0.0.1:8767 while the desktop app
    // was running gave connection-refused -- the UI appeared broken when in
    // fact nothing was serving it.
    //
    // Loopback only, and the server refuses any other interface without
    // TALA_WEB_TOKEN, so this adds a local surface rather than a remote one.
    // Set TALA_WEB_UI=0 before launching to turn it off.
    TALA_WEB_UI: process.env.TALA_WEB_UI ?? '1',
  };
}

/**
 * Ensures the first-run Python environment is fully bootstrapped before launching the engine.
 * If the .venv or .env files are missing, it spawns the first_run.py script synchronously
 * to install dependencies and configure local environment variables.
 * 
 * @param {string} appPath - The root path of the Tala application
 * @returns {string} The path to the resolved Python executable
 */
function ensureFirstRun(appPath) {
  const runtimeBase = resolveDataRoot(appPath);
  const pythonPath = venvPythonPath(runtimeBase);
  const envPath = path.join(runtimeBase, '.env');
  if (fs.existsSync(pythonPath) && fs.existsSync(envPath)) {
    return pythonPath;
  }

  const firstRunScript = path.join(appPath, 'scripts', 'first_run.py');
  if (!fs.existsSync(firstRunScript)) {
    console.warn(`[main] first-run script not found: ${firstRunScript}`);
    return fs.existsSync(pythonPath) ? pythonPath : (systemPythonCommand() ?? 'python3');
  }

  const runner = fs.existsSync(pythonPath) ? pythonPath : systemPythonCommand();
  if (!runner) {
    console.error('[main] No Python interpreter found (tried the venv, python3 and python). Cannot run first-run setup.');
    return pythonPath; // spawn will fail loudly with the venv path in the message
  }
  const args = [firstRunScript, '--skip-node'];
  console.log(`[main] Running first-run setup: "${runner}" ${args.join(' ')}`);
  const result = spawnSync(runner, args, {
    cwd: appPath,
    env: firstRunEnv(appPath),
    stdio: 'inherit',
  });
  if (result.error) {
    console.error(`[main] First-run setup failed to start: ${result.error.message}`);
  } else if (result.status !== 0) {
    console.error(`[main] First-run setup exited with status ${result.status}.`);
  }

  return fs.existsSync(pythonPath) ? pythonPath : runner;
}


/* ──────────────────────────────────────────────────────────────────
   State
   ────────────────────────────────────────────────────────────────── */

/** @type {BrowserWindow|null} */
let mainWindow = null;

/** @type {import('child_process').ChildProcess|null} */
let pythonProcess = null;

/** @type {import('child_process').ChildProcess|null} */
let shellProcess = null;

/** @type {WebSocket|null} */
let wsClient = null;

/** Pending JSON-RPC calls waiting for a response from Python: id → {resolve, timer} */
const pendingRpcCalls = new Map();

/** Number of times the Python harness has been (re-)started this session. */
let pythonRestartCount = 0;

/** Whether the Python WS connection is currently established. */
let wsConnected = false;

/**
 * Push main-process WS truth to the renderer. StatusBar / Mission Control /
 * Settings must not infer connectedness from RPC success alone.
 */
function notifyWsStatus() {
  const open = WebSocket?.OPEN ?? 1;
  const connected = Boolean(
    wsConnected && wsClient && wsClient.readyState === open
  );
  sendLogToRenderer('harness:ws-status', { connected });
}


const {
  isNetworkIsolated,
  setNotify: setIsolationNotify,
  setRefreshHook: setIsolationRefreshHook,
  applyIsolationHints,
  applyIsolationNotification,
  onHarnessConnected,
} = require('./network_isolation');


/* ──────────────────────────────────────────────────────────────────
   electron-store config
   ────────────────────────────────────────────────────────────────── */

let store = null;

function initStore() {
  if (!Store) {
    console.warn('[main] electron-store not available; config persistence disabled.');
    return;
  }
  store = new Store({
    // Always store data in userData to keep it isolated from project source
    cwd: app.getPath('userData'),
    name: 'tala-config',
    defaults: {
      windowBounds: { width: 1440, height: 900 },
      lastView: 'neural-node',
    },
  });
}

/* ──────────────────────────────────────────────────────────────────
   Window creation
   ────────────────────────────────────────────────────────────────── */

/**
 * Initializes and displays the primary Electron browser window.
 * Enforces strict security constraints (sandbox: true, nodeIntegration: false)
 * to ensure the renderer cannot execute arbitrary OS commands if compromised.
 * 
 * Intercepts network requests to bypass X-Frame-Options for external web views
 * while enforcing a strict local Content-Security-Policy (CSP).
 */
function createWindow() {
  const bounds = store?.get('windowBounds') ?? { width: 1440, height: 900 };

  mainWindow = new BrowserWindow({
    width:           bounds.width,
    height:          bounds.height,
    minWidth:        960,
    minHeight:       600,
    titleBarStyle:   'hiddenInset',  // Native title bar, hidden on macOS
    backgroundColor: '#080b14',
    show:            false,          // Don't flash before content is ready

    webPreferences: {
      // ── Strict security settings ──────────────────────────────
      contextIsolation: true,        // Renderer and preload have separate worlds
      sandbox:          true,        // Renderer is OS-sandboxed
      nodeIntegration:  false,       // Renderer has no Node.js access
      webSecurity:      true,        // Same-origin policy enforced
      webviewTag:       true,        // Allow using <webview> tags

      preload: path.join(__dirname, 'preload.js'),

      // Disable potentially dangerous features
      allowRunningInsecureContent: false,
      experimentalFeatures:        false,
      spellcheck:                  false,
    },
  });

  // Intercept headers to bypass X-Frame-Options blocking on external mock browser frames,
  // while maintaining a strict Content Security Policy for local renderer file resources.
  mainWindow.webContents.session.webRequest.onHeadersReceived((details, callback) => {
    const responseHeaders = {};
    for (const key of Object.keys(details.responseHeaders)) {
      const lowerKey = key.toLowerCase();
      // Remove headers that prevent iframe embedding for external web views
      if (lowerKey !== 'x-frame-options' && lowerKey !== 'content-security-policy') {
        responseHeaders[key] = details.responseHeaders[key];
      }
    }

    if (details.url.startsWith('file://')) {
      responseHeaders['Content-Security-Policy'] = [
        "default-src 'self'; " +
        "script-src 'self'; " +
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; " +
        "font-src 'self' https://fonts.gstatic.com; " +
        "img-src 'self' data:; " +
        // TTS hands the renderer a data:audio/... URI (voice.speak returns
        // base64, not a file). Without media-src this falls back to
        // default-src 'self', which blocks it -- synthesis succeeds, the
        // audio element is constructed, and play() rejects with a bare
        // DOMException that reads as a broken voice backend.
        "media-src 'self' data:; " +
        "frame-src 'self' file: https://duckduckgo.com https://*.duckduckgo.com http: https:; " +
        "connect-src 'self' http://127.0.0.1:8332 https://talacore.app;"
      ];
    }

    callback({ responseHeaders });
  });

  // Load the renderer HTML
  mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));

  // Forward renderer console messages to main process console
  mainWindow.webContents.on('console-message', (event, level, message, line, sourceId) => {
    const levels = { 0: 'info', 1: 'warn', 2: 'error', 3: 'log' };
    const lvl = levels[level] || 'log';
    console.log(`[renderer:${lvl}] ${message} (at ${path.basename(sourceId)}:${line})`);
  });


  // Show window once content is ready (avoids white flash)
  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  // Persist window size on close
  mainWindow.on('close', () => {
    if (mainWindow && store) {
      store.set('windowBounds', mainWindow.getBounds());
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Prevent navigation away from local files
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith('file://')) {
      event.preventDefault();
      console.warn(`[main] Blocked navigation to: ${url}`);
    }
  });

  // Prevent opening new windows
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
}

/* ──────────────────────────────────────────────────────────────────
   Python Harness Management
   ────────────────────────────────────────────────────────────────── */

/**
 * Spawns the underlying Python harness (Tala Cognitive Engine Room) as a child process.
 * 
 * This python process runs the core AI workflows, token counting, and database connections.
 * Standard output and standard error are captured and relayed to the renderer via IPC
 * so users can view backend logs in real-time.
 * 
 * Includes an exponential back-off restart mechanism if the engine crashes unexpectedly.
 * 
 * @param {number} attempt  — Current attempt number (0-indexed), used for back-off.
 */
function spawnPython(attempt = 0) {
  if (attempt >= MAX_RESTARTS) {
    console.error(`[main] Python harness failed after ${MAX_RESTARTS} attempts. Giving up.`);
    return;
  }

  const delay = attempt === 0 ? 0 : RESTART_BASE_DELAY_MS * Math.pow(2, attempt - 1);
  if (delay > 0) {
    console.log(`[main] Restarting Python harness in ${delay}ms (attempt ${attempt + 1}/${MAX_RESTARTS})…`);
  }

  setTimeout(() => {
    if (!app.isReady()) return;   // Guard if app is quitting

    const appPath = app.getAppPath();
    const scriptPath = path.join(appPath, 'python', 'harness', 'server.py');
    const pythonExe = ensureFirstRun(appPath);

    if (process.platform === 'win32') {
      try {
        const stdout = spawnSync('netstat', ['-ano'], { encoding: 'utf8' }).stdout || '';
        const lines = stdout.split('\n');
        for (const line of lines) {
          if (line.includes(`:${HARNESS_PORT}`) && line.includes('LISTENING')) {
            const parts = line.trim().split(/\s+/);
            const pid = parts[parts.length - 1];
            if (pid && pid !== '0' && pid !== String(process.pid)) {
              console.log(`[main] Port ${HARNESS_PORT} is occupied by PID ${pid}. Killing it...`);
              spawnSync('taskkill', ['/F', '/PID', pid]);
            }
          }
        }
      } catch (err) {
        console.warn(`[main] Stale port cleanup failed: ${err.message}`);
      }
    }
    
    console.log(`[main] Spawning Python harness: "${pythonExe}" "${scriptPath}"`);

    pythonProcess = spawn(pythonExe, [scriptPath], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: firstRunEnv(appPath),
    });

    pythonRestartCount = attempt + 1;

    pythonProcess.stdout.on('data', buf => {
      const text = buf.toString();
      const lines = text.trim().split('\n');
      lines.forEach(line => console.log(`[python] ${line}`));
      sendLogToRenderer('harness:output', text);
    });

    pythonProcess.stderr.on('data', buf => {
      const text = buf.toString();
      const lines = text.trim().split('\n');
      lines.forEach(line => console.error(`[python:err] ${line}`));
      sendLogToRenderer('harness:output', text);
    });

    pythonProcess.on('error', err => {
      console.error(`[main] Failed to start Python process: ${err.message}`);
    });

    pythonProcess.on('exit', (code, signal) => {
      console.warn(`[main] Python harness exited (code=${code}, signal=${signal})`);
      pythonProcess = null;
      wsClient = null;
      wsConnected = false;
      notifyWsStatus();

      // Resolve all pending RPC calls with error payload
      for (const [id, pending] of pendingRpcCalls) {
        clearTimeout(pending.timer);
        pending.resolve({
          __rpcError: true,
          error: 'Python harness exited unexpectedly.'
        });
        pendingRpcCalls.delete(id);
      }

      // Attempt restart unless app is quitting
      if (!app.isQuiting) {
        spawnPython(attempt + 1);
      }
    });

    // Poll for WebSocket connection: try every 500 ms for up to 15 s.
    // The Python server now binds its port before doing any DB work, so
    // the port should be open within the first second.
    setTimeout(() => connectWithRetry(0), 500);

  }, delay);
}

/**
 * Attempt to connect the WebSocket; if the port is not yet open, retry.
 * @param {number} attempt
 */
function connectWithRetry(attempt) {
  const MAX_CONNECT_ATTEMPTS = 30; // 30 × 500 ms = 15 s
  const RETRY_INTERVAL_MS    = 500;

  if (!pythonProcess) return; // Python died before we could connect

  if (wsClient && wsClient.readyState === (WebSocket?.OPEN ?? 1)) return; // already open

  if (!WebSocket) {
    console.warn('[main] ws package not available; skipping WS retry.');
    return;
  }

  console.log(`[main] WS connect attempt ${attempt + 1}/${MAX_CONNECT_ATTEMPTS}…`);

  const wsToken = ensureWsToken(app.getAppPath());
  const candidate = new WebSocket(HARNESS_URL, {
    handshakeTimeout: 3_000,
    // Header rather than query string: this never reaches a URL bar, a log
    // line or a Referer. The browser shim has to use the query form because
    // new WebSocket() cannot set headers, but the main process can.
    headers: wsToken ? { Authorization: `Bearer ${wsToken}` } : {},
  });

  candidate.once('open', () => {
    // Hand off to the normal persistent connection machinery
    wsClient = candidate;
    wsConnected = true;
    // Isolation is lockdown/halt/network-guard — not "WS connected".
    console.log('[main] WebSocket connected to Python harness.');
    notifyWsStatus();
    onHarnessConnected();

    candidate.on('message', raw => {
      let msg;
      try { msg = JSON.parse(raw); } catch { return; }
      if (msg.id !== undefined && pendingRpcCalls.has(msg.id)) {
        const { resolve, timer } = pendingRpcCalls.get(msg.id);
        clearTimeout(timer);
        pendingRpcCalls.delete(msg.id);
        if (msg.error) {
          resolve({ __rpcError: true, error: `Python RPC error: ${msg.error.message ?? JSON.stringify(msg.error)}` });
        } else {
          resolve(msg.result ?? null);
        }
      } else if (msg.id === undefined && msg.method) {
        applyIsolationNotification(msg);
        sendLogToRenderer('python:notification', msg);
      }
    });

    candidate.on('error', err => {
      console.error(`[main] WebSocket error: ${err.message}`);
      wsConnected = false;
      notifyWsStatus();
    });

    candidate.on('close', (code, reason) => {
      console.warn(`[main] WebSocket closed (code=${code}, reason=${reason}).`);
      wsConnected = false;
      wsClient = null;
      notifyWsStatus();
      if (pythonProcess && !app.isQuiting) {
        setTimeout(() => connectWithRetry(0), 3_000);
      }
    });
  });

  candidate.once('error', () => {
    candidate.terminate();
    if (attempt + 1 < MAX_CONNECT_ATTEMPTS && pythonProcess && !app.isQuiting) {
      setTimeout(() => connectWithRetry(attempt + 1), RETRY_INTERVAL_MS);
    } else if (attempt + 1 >= MAX_CONNECT_ATTEMPTS) {
      console.error('[main] Could not connect to Python harness after maximum attempts.');
    }
  });

  // 'error' will fire if connection refused; suppress 'close' noise on retry candidates
  candidate.once('close', () => {
    // Only relevant if 'open' was not emitted
  });
}

/* The blockchain sidecar's spawn/restart/stop lives in blockchain-process.js. */

/* ──────────────────────────────────────────────────────────────────
   WebSocket Client (renderer <-> Python JSON-RPC relay)
   ────────────────────────────────────────────────────────────────── */

/** Auto-incrementing JSON-RPC request id. */
let rpcIdCounter = 1;

/* ──────────────────────────────────────────────────────────────────
   python:rpc IPC handler
   Bridges renderer RPC calls through the WebSocket to Python.
   ────────────────────────────────────────────────────────────────── */


function registerPythonRpcHandler() {
  ipcMain.handle('python:rpc', async (_event, payload) => {
    const { method } = payload ?? {};
    let params = (payload && payload.params) || {};

    if (!method || typeof method !== 'string') {
      throw new Error('python:rpc requires a "method" string field.');
    }

    if (method === 'app.update') {
      const url = params.url;
      if (!url) return { __rpcError: true, error: 'No url provided for update' };

      // Trust boundary: only ever download from the pinned release host.
      // Without this, any code running in the renderer (e.g. via XSS) could
      // pass an arbitrary URL and get Tala to download-and-extract-over-itself
      // whatever is there.
      const UPDATE_HOST = 'talacore.app';
      let parsedUrl;
      try {
        parsedUrl = new URL(url);
      } catch {
        return { __rpcError: true, error: 'Invalid update URL.' };
      }
      if (parsedUrl.protocol !== 'https:' || parsedUrl.hostname !== UPDATE_HOST) {
        return { __rpcError: true, error: `Refusing update: URL host must be https://${UPDATE_HOST}/...` };
      }

      const platformKey = process.platform === 'win32' ? 'windows' : process.platform === 'darwin' ? 'macos' : 'linux';
      const expectedHash = await fetchExpectedUpdateHash(platformKey);
      if (!expectedHash) {
        return { __rpcError: true, error: 'Refusing update: no valid published SHA-256 hash is available.' };
      }

      // Downgrade gate. The renderer compares versions before offering the
      // banner, but app.update is callable on its own, and the hash check
      // only proves "this is the currently published zip" — not "this is an
      // upgrade". Whoever can influence what talacore.app serves must not be
      // able to roll an install back to an older, vulnerable release.
      const installedVersion = getInstalledVersion();
      const publishedVersion = await fetchPublishedVersion();
      if (!installedVersion || !publishedVersion || !isNewerVersion(publishedVersion, installedVersion)) {
        return {
          __rpcError: true,
          error: `Refusing update: published version ${publishedVersion || 'unknown'} is not newer than installed ${installedVersion || 'unknown'}.`,
        };
      }

      return new Promise((resolve, reject) => {
        const fs = require('fs');
        const os = require('os');
        const { spawn } = require('child_process');
        const path = require('path');
        const crypto = require('crypto');
        const appRoot = path.resolve(__dirname, '..');

        // Staged apply, both platforms: the archive is fully extracted to a
        // staging dir first (a bad archive leaves the install untouched),
        // every file the update will overwrite is backed up, and a failed
        // apply restores the backup. Extracting straight over the install
        // root meant an interrupted update left a half-overwritten app.
        const stagingPath = path.join(os.tmpdir(), 'tala-update-staged');
        const backupPath = path.join(os.tmpdir(), 'tala-update-backup');

        if (process.platform === 'win32') {
          const scriptPath = path.join(os.tmpdir(), 'tala-update.ps1');
          const zipPath = path.join(os.tmpdir(), 'tala-update.zip');
          const startScript = path.join(appRoot, 'scripts', 'start-tala.ps1');
          const scriptContent = buildWindowsUpdateScript({ zipPath, appRoot, startScript, stagingPath, backupPath, expectedHash });
          fs.writeFileSync(scriptPath, scriptContent);
          const child = spawn('cmd.exe', ['/c', 'start', 'powershell.exe', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Normal', '-NoExit', '-File', scriptPath], {
            detached: true,
            env: { ...process.env, TALA_UPDATE_URL: parsedUrl.href },
            stdio: 'ignore'
          });
          child.unref();
          resolve({ success: true });
          setTimeout(() => { app.quit(); }, 500);
        } else {
          // Linux/Mac logic... kept simple
          const https = require('https');
          const zipPath = path.join(os.tmpdir(), 'tala-update.zip');
          const file = fs.createWriteStream(zipPath);
          https.get(url, (response) => {
            if (response.statusCode !== 200) return reject('Download failed');
            response.pipe(file);
            file.on('finish', () => {
              file.close(() => {
                if (expectedHash) {
                  const actualHash = crypto.createHash('sha256').update(fs.readFileSync(zipPath)).digest('hex');
                  if (actualHash.toLowerCase() !== expectedHash.toLowerCase()) {
                    fs.unlinkSync(zipPath);
                    return reject('Downloaded update failed integrity check (hash mismatch).');
                  }
                }
                // The script runs after the app quits, so any failure inside
                // it is invisible. Verify an extractor exists NOW (unzip is
                // absent on many minimal installs) and refuse the update with
                // a real error instead of dying silently later; the script
                // itself also falls back to Python's zipfile module and logs
                // to a file for post-mortem.
                const shellCmd = posixShellCommand();
                const haveUnzip = spawnSync(shellCmd, ['-c', 'command -v unzip'], { stdio: 'ignore' }).status === 0;
                const updaterPython = systemPythonCommand();
                if (!haveUnzip && !updaterPython) {
                  try { fs.unlinkSync(zipPath); } catch { /* already gone */ }
                  return reject('Cannot apply the update: neither "unzip" nor a Python interpreter is available to extract it. Install one (e.g. `sudo apt install unzip`) and retry.');
                }

                const scriptPath = path.join(os.tmpdir(), 'tala-update.sh');
                const logPath = path.join(os.tmpdir(), 'tala-update.log');
                const startScript = path.join(appRoot, 'scripts', 'start-tala.sh');
                const scriptContent = buildUnixUpdateScript({ zipPath, appRoot, startScript, stagingPath, backupPath, logPath, updaterPython, shellCmd });
                fs.writeFileSync(scriptPath, scriptContent);
                fs.chmodSync(scriptPath, '755');
                const child = spawn(shellCmd, [scriptPath], { detached: true, stdio: 'ignore' });
                child.unref();
                resolve({ success: true });
                setTimeout(() => { app.quit(); }, 500);
              });
            });
          }).on('error', (err) => reject(err.message));
        }
      });
    }

    if (method === 'app.version') {
      return { version: require('../package.json').version };
    }

    // Main-local WS probe — does not require the socket to be up.
    if (method === 'harness.ws_status') {
      const open = WebSocket?.OPEN ?? 1;
      const connected = Boolean(
        wsConnected && wsClient && wsClient.readyState === open
      );
      return { connected, wsConnected };
    }

    // Owner-gated RPCs (trigger_update, release_halt, task.clear) get the
    // on-disk harness admin secret injected here — never from the renderer.
    // Resume / task.control must not receive it; Resume ≠ release_halt.
    params = injectOwnerSecret(method, params);

    // If WS is not connected, return a degraded stub for known health checks
    if (!wsConnected || !wsClient || wsClient.readyState !== (WebSocket?.OPEN ?? 1)) {
      if (method === 'harness.health') {
        // Include halted so network_isolation can keep haltActive in sync
        // even while the WS is down (same field harness.health always returns).
        return {
          status:          'connecting',
          active_agent:    null,
          tokens_used:     0,
          last_latency_ms: null,
          halted:          false,
        };
      }
      return {
        __rpcError: true,
        error: 'Python harness WebSocket is not connected.'
      };
    }

    const startTime = Date.now();
    const id = rpcIdCounter++;
    const request = JSON.stringify({ jsonrpc: '2.0', id, method, params });

    return new Promise((resolve) => {
      const timeoutMs = rpcTimeoutMs(method, params && (params.timeout_ms || params.timeoutMs || params._timeoutMs));
      const timer = setTimeout(() => {
        pendingRpcCalls.delete(id);
        resolve({
          __rpcError: true,
          error: `python:rpc timeout for method "${method}"`
        });
      }, timeoutMs);

      pendingRpcCalls.set(id, {
        resolve: (result) => {
          if (result && method === 'harness.health') {
            result.last_latency_ms = Date.now() - startTime;
          }
          applyIsolationHints(method, result);
          resolve(result);
        },
        timer,
      });

      try {
        wsClient.send(request);
      } catch (err) {
        clearTimeout(timer);
        pendingRpcCalls.delete(id);
        resolve({
          __rpcError: true,
          error: `Failed to send RPC request: ${err.message}`
        });
      }
    });
  });
}

/**
 * Safely relay stdout/stderr logs to the renderer if the window is open.
 */
function sendLogToRenderer(channel, message) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, message);
  }
}

/**
 * Spawns a persistent, interactive terminal shell process (PowerShell on Windows, bash on Unix).
 * 
 * This allows the user to execute raw system commands from the UI.
 * The terminal environment is pre-injected with the Python virtual environment (.venv) PATH context,
 * ensuring commands like 'python' or 'pip' resolve correctly to the local sandbox.
 */
function spawnTerminalShell() {
  if (shellProcess) {
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', shellProcess.pid, '/f', '/t']);
      } else {
        shellProcess.kill('SIGKILL');
      }
    } catch { /* ignore */ }
    shellProcess = null;
  }

  const appPath = app.getAppPath();
  // PATH lookup, not a hardcoded /bin/bash — that path does not exist on NixOS.
  const shell = process.platform === 'win32' ? 'powershell.exe' : posixShellCommand();
  // Win32: -NoExit launches powershell persistently. Unix: -i launches an interactive shell.
  const shellArgs = process.platform === 'win32' ? ['-NoProfile', '-NoExit'] : ['-i'];

  const venvBinDir = process.platform === 'win32' ? 'Scripts' : 'bin';
  const venvPath = path.join(resolveDataRoot(appPath), '.venv');
  const venvBinPath = path.join(venvPath, venvBinDir);

  const pathKey = process.platform === 'win32' ? 'Path' : 'PATH';
  const customEnv = { ...process.env };
  if (require('node:fs').existsSync(venvPath)) {
    customEnv[pathKey] = `${venvBinPath}${path.delimiter}${process.env[pathKey]}`;
    customEnv['VIRTUAL_ENV'] = venvPath;
  }

  console.log(`[main:terminal] Spawning persistent interactive shell: ${shell}`);

  try {
    shellProcess = spawn(shell, shellArgs, {
      cwd: appPath,
      env: customEnv,
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    shellProcess.stdout.on('data', buf => {
      sendLogToRenderer('terminal:output', buf.toString());
    });

    shellProcess.stderr.on('data', buf => {
      sendLogToRenderer('terminal:output', buf.toString());
    });

    shellProcess.on('error', err => {
      sendLogToRenderer('terminal:output', `\r\n[Shell Spawn Error: ${err.message}]\r\n`);
    });

    shellProcess.on('exit', (code, signal) => {
      // Re-spawn only if the app isn't closing
      if (app.isQuiting) return;
      
      const exitMsg = `\r\n[Shell exited with code ${code === null ? 'null' : code} (signal: ${signal}). Restarting shell...]\r\n`;
      sendLogToRenderer('terminal:output', exitMsg);
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('terminal:exit', { code, signal });
      }
      shellProcess = null;
      setTimeout(() => {
        if (!app.isQuiting) spawnTerminalShell();
      }, 1000);
    });
  } catch (err) {
    console.error('[main:terminal] Failed to spawn persistent shell:', err);
  }
}

/**
 * Register handlers for the integrated terminal interface.
 */
function registerTerminalHandlers() {
  ipcMain.handle('terminal:command', async (_event, payload) => {
    const { command, raw } = payload ?? {};
    if (command === undefined) throw new Error('Command payload is missing');

    if (!shellProcess) {
      spawnTerminalShell();
    }

    try {
      if (raw) {
        shellProcess.stdin.write(command);
      } else {
        shellProcess.stdin.write(command + '\n');
      }
      return { ok: true, status: 'written' };
    } catch (err) {
      return { ok: false, error: `Failed to write to terminal stdin: ${err.message}` };
    }
  });

  ipcMain.handle('terminal:kill', async () => {
    if (!shellProcess) {
      return { ok: false, error: 'No active shell to reset' };
    }
    console.log('[main:terminal] Resetting terminal shell session...');
    try {
      if (process.platform === 'win32') {
        const pid = shellProcess.pid;
        spawn('taskkill', ['/pid', pid, '/f', '/t']);
      } else {
        shellProcess.kill('SIGKILL');
      }
      return { ok: true };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // There is deliberately no "apply update" IPC channel any more. It was a
  // second update-delivery mechanism with a weaker trust model: the harness
  // repackaged its own live checkout and the bytes were extracted over the
  // app directory with no published hash to check them against. Every update
  // — whether the user clicked the banner or a VPS push woke the client —
  // now goes through the one signed path: app.update, pinned to the release
  // host and verified against the sha256 map in version.json.
}

function killProcessOnPort(port) {
  try {
    const { execSync } = require('node:child_process');
    if (process.platform === 'win32') {
      const output = execSync('netstat -ano').toString();
      const lines = output.split('\n');
      for (const line of lines) {
        if (line.includes(`:${port}`) && line.includes('LISTENING')) {
          const parts = line.trim().split(/\s+/);
          const pid = parts[parts.length - 1];
          if (pid && pid !== '0') {
            console.log(`[main] Port ${port} is occupied by PID ${pid}. Killing it...`);
            execSync(`taskkill /F /PID ${pid}`);
          }
        }
      }
    } else if (spawnSync('sh', ['-c', 'command -v lsof'], { stdio: 'ignore' }).status === 0) {
      execSync(`lsof -t -i:${port} | xargs -r kill -9 2>/dev/null || true`);
    } else if (spawnSync('sh', ['-c', 'command -v fuser'], { stdio: 'ignore' }).status === 0) {
      execSync(`fuser -k ${port}/tcp 2>/dev/null || true`);
    } else {
      console.warn(`[main] Neither lsof nor fuser is available; skipping stale-process cleanup on port ${port}.`);
    }
  } catch (err) {
    // Ignore error
  }
}

/* ──────────────────────────────────────────────────────────────────
   App lifecycle
   ────────────────────────────────────────────────────────────────── */

app.whenReady().then(() => {
  // Clear any stale processes holding our ports
  killProcessOnPort(8765);
  killProcessOnPort(chainPort());

  // Initialise persistent store
  initStore();

  // Apply session-level permissions (deny all except needed)
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    // Deny all permission requests from the renderer (camera, mic, notifications, etc.)
    callback(false);
  });


  setIsolationNotify(sendLogToRenderer);
  setIsolationRefreshHook(() => {
    if (!wsConnected || !wsClient || wsClient.readyState !== (WebSocket?.OPEN ?? 1)) return;
    for (const method of ['environment.get', 'guardrails.get_network', 'harness.health']) {
      const id = rpcIdCounter++;
      const request = JSON.stringify({ jsonrpc: '2.0', id, method, params: {} });
      const timer = setTimeout(() => { pendingRpcCalls.delete(id); }, 8_000);
      pendingRpcCalls.set(id, {
        resolve: (result) => applyIsolationHints(method, result),
        timer,
      });
      try { wsClient.send(request); } catch { clearTimeout(timer); pendingRpcCalls.delete(id); }
    }
  });

  registerHandlers({
    isNetworkIsolated,
  });

  // Register Python RPC relay
  registerPythonRpcHandler();

  // Register Terminal IPC handlers
  registerTerminalHandlers();

  // Create main window
  createWindow();

  // Start Blockchain Node
  initBlockchain({ resolveDataRoot, sendLogToRenderer });
  spawnBlockchain(0);

  // Start Python harness
  spawnPython(0);

  // Start persistent Terminal Shell
  spawnTerminalShell();

  // macOS: re-create window when dock icon is clicked and no windows are open
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// Mark app as quitting so child-process exit handlers don't try to restart
app.on('before-quit', () => {
  app.isQuiting = true;
});

// Quit when all windows are closed (except macOS)
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// ── Cleanup on quit ──────────────────────────────────────────────
app.on('will-quit', () => {
  console.log('[main] Cleaning up child processes…');

  // Close WebSocket
  if (wsClient) {
    try { wsClient.close(1001, 'App quitting'); } catch { /* ignore */ }
    wsClient = null;
  }

  // Terminate Python process
  if (pythonProcess) {
    try { pythonProcess.kill('SIGTERM'); } catch { /* ignore */ }
    pythonProcess = null;
  }

  // Terminate Blockchain process
  stopBlockchain();

  // Terminate active terminal process
  if (shellProcess) {
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', shellProcess.pid, '/f', '/t']);
      } else {
        shellProcess.kill('SIGKILL');
      }
    } catch { /* ignore */ }
    shellProcess = null;
  }

  // Resolve any remaining pending RPC calls with error payload
  for (const [id, pending] of pendingRpcCalls) {
    clearTimeout(pending.timer);
    pending.resolve({
      __rpcError: true,
      error: 'App is quitting.'
    });
    pendingRpcCalls.delete(id);
  }
});
