/**
 * blockchain-process.js — lifecycle of the Node.js blockchain sidecar.
 *
 * Moved out of main.js wholesale when shipping blockchain/src in packaged
 * builds pushed that file past its size ratchet (eslint max-lines).
 * Spawn/restart/log-piping behavior is unchanged.
 */

'use strict';

const path = require('node:path');
const fs   = require('node:fs');
const { spawn } = require('node:child_process');
const { app }   = require('electron');

const MAX_RESTARTS = 3;
const RESTART_BASE_DELAY_MS = 1_500; // base for exponential back-off

/** @type {import('child_process').ChildProcess|null} */
let blockchainProcess = null;

/** Wired once from main.js before the first spawn. */
let deps = {
  resolveDataRoot: appPath => appPath,
  sendLogToRenderer: () => {},
};

function initBlockchain(wiring) {
  deps = wiring;
}

/**
 * Spawns the Node.js Blockchain Node for P2P networking and state distribution.
 *
 * This process connects to the Tala mesh network to sync system prompts, configurations,
 * and discover other peers. Logs are captured and piped to the UI renderer.
 *
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
    if (!fs.existsSync(scriptPath)) {
      console.warn(`[main:warn] Blockchain script not found at ${scriptPath}. Skipping blockchain spawn.`);
      return;
    }
    console.log(`[main] Spawning Blockchain node: "${scriptPath}"`);

    // Run app.js under Electron's own binary rather than a system `node`,
    // which a packaged install cannot assume exists. The ledger lands under
    // the resolved data root: blockchain/data/ inside a packaged app is
    // excluded from the bundle (it would carry PoA keys and the admin
    // secret) and may not be writable anyway.
    blockchainProcess = spawn(process.execPath, [scriptPath], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...process.env,
        ELECTRON_RUN_AS_NODE: '1',
        TALA_CHAIN_DATA_DIR: process.env.TALA_CHAIN_DATA_DIR
          || path.join(deps.resolveDataRoot(app.getAppPath()), 'blockchain', 'data'),
      },
    });

    blockchainProcess.stdout.on('data', buf => {
      const text = buf.toString();
      const lines = text.trim().split('\n');
      lines.forEach(line => console.log(`[chain] ${line}`));
      deps.sendLogToRenderer('blockchain:output', text);
    });

    blockchainProcess.stderr.on('data', buf => {
      const text = buf.toString();
      const lines = text.trim().split('\n');
      lines.forEach(line => console.error(`[chain:err] ${line}`));
      deps.sendLogToRenderer('blockchain:output', text);
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

function stopBlockchain() {
  if (blockchainProcess) {
    try { blockchainProcess.kill('SIGTERM'); } catch { /* ignore */ }
    blockchainProcess = null;
  }
}

module.exports = { initBlockchain, spawnBlockchain, stopBlockchain };
