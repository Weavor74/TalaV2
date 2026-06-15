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
  Store = require('electron-store');
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

function venvPythonPath(basePath) {
  const venvBinDir = process.platform === 'win32' ? 'Scripts' : 'bin';
  const pythonName = process.platform === 'win32' ? 'python.exe' : 'python';
  return path.join(basePath, '.venv', venvBinDir, pythonName);
}

function firstRunEnv(appPath) {
  const userDataPath = app.getPath('userData');
  const venvDir = path.join(app.isPackaged ? userDataPath : appPath, '.venv');
  const envPath = path.join(app.isPackaged ? userDataPath : appPath, '.env');
  return {
    ...process.env,
    TALA_INSTALL_ROOT: appPath,
    TALA_USER_DATA: userDataPath,
    TALA_USER_DATA_ROOT: app.isPackaged ? userDataPath : appPath,
    TALA_VENV_DIR: venvDir,
    TALA_ENV_PATH: envPath,
    TALA_WS_PORT: String(HARNESS_PORT),
    PYTHONUNBUFFERED: '1',
  };
}

function ensureFirstRun(appPath) {
  const runtimeBase = app.isPackaged ? app.getPath('userData') : appPath;
  const pythonPath = venvPythonPath(runtimeBase);
  const envPath = path.join(runtimeBase, '.env');
  if (fs.existsSync(pythonPath) && fs.existsSync(envPath)) {
    return pythonPath;
  }

  const firstRunScript = path.join(appPath, 'scripts', 'first_run.py');
  if (!fs.existsSync(firstRunScript)) {
    console.warn(`[main] first-run script not found: ${firstRunScript}`);
    return fs.existsSync(pythonPath) ? pythonPath : 'python';
  }

  const runner = fs.existsSync(pythonPath) ? pythonPath : 'python';
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
let blockchainProcess = null;

/** @type {import('child_process').ChildProcess|null} */
let shellProcess = null;

/** @type {WebSocket|null} */
let wsClient = null;

/** Pending JSON-RPC calls waiting for a response from Python: id → {resolve, timer} */
const pendingRpcCalls = new Map();

/** Number of times the Python harness has been (re-)started this session. */
let pythonRestartCount = 0;

/** Number of times the Blockchain node has been (re-)started this session. */
let blockchainRestartCount = 0;

/** Whether the Python WS connection is currently established. */
let wsConnected = false;

/** Tracks whether we are in "network isolated" mode (used by vault:read guard). */
let networkIsolated = false;

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
        "frame-src 'self' file: https://duckduckgo.com https://*.duckduckgo.com http: https:; " +
        "connect-src 'self' http://127.0.0.1:8332;"
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
 * Spawn the Python harness server.
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

  const candidate = new WebSocket(HARNESS_URL, { handshakeTimeout: 3_000 });

  candidate.once('open', () => {
    // Hand off to the normal persistent connection machinery
    wsClient = candidate;
    wsConnected = true;
    networkIsolated = false;
    console.log('[main] WebSocket connected to Python harness.');

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
        sendLogToRenderer('python:notification', msg);
      }
    });

    candidate.on('error', err => {
      console.error(`[main] WebSocket error: ${err.message}`);
      wsConnected = false;
    });

    candidate.on('close', (code, reason) => {
      console.warn(`[main] WebSocket closed (code=${code}, reason=${reason}).`);
      wsConnected = false;
      wsClient = null;
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

/**
 * Spawn the Node.js Blockchain Node.
 * @param {number} attempt  — Current attempt number (0-indexed), used for back-off.
 */
function spawnBlockchain(attempt = 0) {
  if (attempt >= MAX_RESTARTS) {
    console.error(`[main] Blockchain node failed after ${MAX_RESTARTS} attempts. Giving up.`);
    return;
  }

  const delay = attempt === 0 ? 0 : RESTART_BASE_DELAY_MS * Math.pow(2, attempt - 1);
  if (delay > 0) {
    console.log(`[main] Restarting Blockchain node in ${delay}ms (attempt ${attempt + 1}/${MAX_RESTARTS})…`);
  }

  setTimeout(() => {
    if (!app.isReady()) return;   // Guard if app is quitting

    const scriptPath = path.join(app.getAppPath(), 'blockchain', 'src', 'app.js');
    console.log(`[main] Spawning Blockchain node: node "${scriptPath}"`);

    blockchainProcess = spawn('node', [scriptPath], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...process.env,
      },
    });

    blockchainRestartCount = attempt + 1;

    blockchainProcess.stdout.on('data', buf => {
      const text = buf.toString();
      const lines = text.trim().split('\n');
      lines.forEach(line => console.log(`[chain] ${line}`));
      sendLogToRenderer('blockchain:output', text);
    });

    blockchainProcess.stderr.on('data', buf => {
      const text = buf.toString();
      const lines = text.trim().split('\n');
      lines.forEach(line => console.error(`[chain:err] ${line}`));
      sendLogToRenderer('blockchain:output', text);
    });

    blockchainProcess.on('error', err => {
      console.error(`[main] Failed to start Blockchain process: ${err.message}`);
    });

    blockchainProcess.on('exit', (code, signal) => {
      console.warn(`[main] Blockchain node exited (code=${code}, signal=${signal})`);
      blockchainProcess = null;

      // Attempt restart unless app is quitting
      if (!app.isQuiting) {
        spawnBlockchain(attempt + 1);
      }
    });

  }, delay);
}

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
    const { method, params = {} } = payload ?? {};

    if (!method || typeof method !== 'string') {
      throw new Error('python:rpc requires a "method" string field.');
    }

    // If WS is not connected, return a degraded stub for known health checks
    if (!wsConnected || !wsClient || wsClient.readyState !== (WebSocket?.OPEN ?? 1)) {
      if (method === 'harness.health') {
        return {
          status:          'connecting',
          active_agent:    null,
          tokens_used:     0,
          last_latency_ms: null,
        };
      }
      if (method === 'app.version') {
        return { version: require('../package.json').version };
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
      const timeoutMs = (method === 'inference.generate' || 
                         method === 'workflow.execute' ||
                         method === 'task.submit' ||
                         method === 'task.list' ||
                         method === 'task.get_telemetry') ? 300_000
                       : method === 'research.crawl_and_summarize' ? 300_000
                       : 60_000;
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
 * Pre-injects the Python virtual environment (.venv) PATH context.
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
  const shell = process.platform === 'win32' ? 'powershell.exe' : '/bin/bash';
  // Win32: -NoExit launches powershell persistently. Unix: -i launches an interactive bash.
  const shellArgs = process.platform === 'win32' ? ['-NoProfile', '-NoExit'] : ['-i'];

  const venvBinDir = process.platform === 'win32' ? 'Scripts' : 'bin';
  const venvPath = path.join(appPath, '.venv');
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
    } else {
      execSync(`lsof -t -i:${port} | xargs kill -9`);
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
  killProcessOnPort(8332);

  // Initialise persistent store
  initStore();

  // Apply session-level permissions (deny all except needed)
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    // Deny all permission requests from the renderer (camera, mic, notifications, etc.)
    callback(false);
  });

  // Register sandboxed file + vault IPC handlers
  registerHandlers({
    isNetworkIsolated: () => networkIsolated,
  });

  // Register Python RPC relay
  registerPythonRpcHandler();

  // Register Terminal IPC handlers
  registerTerminalHandlers();

  // Create main window
  createWindow();

  // Start Blockchain Node
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
  if (blockchainProcess) {
    try { blockchainProcess.kill('SIGTERM'); } catch { /* ignore */ }
    blockchainProcess = null;
  }

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
