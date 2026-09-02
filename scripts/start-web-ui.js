/**
 * start-web-ui.js — run the harness with the browser UI switched on.
 *
 * The harness is normally a child of Electron's main process, so there was no
 * way to start it on its own. The web UI needs exactly that: the same backend,
 * without the desktop shell in front of it.
 *
 * Everything here is the harness's own configuration, passed through the
 * environment. This script sets TALA_WEB_UI and otherwise stays out of the
 * way, so the defaults documented in README.md are the ones that apply.
 *
 *   npm run web                        # 127.0.0.1:8767
 *   TALA_WEB_PORT=9000 npm run web     # a different port
 *   TALA_WEB_HOST=0.0.0.0 TALA_WEB_TOKEN=... npm run web   # reachable
 *
 * Binding anywhere other than loopback without a token is refused by the
 * server itself, not here: this UI reaches every RPC handler the harness has,
 * including shell execution.
 */

'use strict';

const { spawn } = require('node:child_process');
const net = require('node:net');
const { existsSync } = require('node:fs');
const { join } = require('node:path');

const root = join(__dirname, '..');
const pythonPath = process.platform === 'win32'
  ? join(root, '.venv', 'Scripts', 'python.exe')
  : join(root, '.venv', 'bin', 'python');

const executable = existsSync(pythonPath) ? pythonPath : 'python';

const env = { ...process.env, TALA_WEB_UI: '1' };
const host = env.TALA_WEB_HOST || '127.0.0.1';
const port = env.TALA_WEB_PORT || '8767';
const wsPort = Number(env.TALA_WS_PORT || 8765);

/**
 * Is a harness already listening?
 *
 * If Tala's desktop app is running it owns the WebSocket port, and starting a
 * second harness here would collide on it and be useless at best. In that case
 * only the UI is served, pointed at the harness that already exists — the
 * renderer does not care which process hands it its files.
 */
function harnessIsRunning() {
  return new Promise((resolve) => {
    const socket = net.connect({ host: '127.0.0.1', port: wsPort });
    const done = (answer) => {
      socket.destroy();
      resolve(answer);
    };
    socket.setTimeout(1000);
    socket.once('connect', () => done(true));
    socket.once('timeout', () => done(false));
    socket.once('error', () => done(false));
  });
}

async function launch() {
  const alreadyUp = await harnessIsRunning();

  const args = alreadyUp
    ? ['-m', 'python.harness.webui']
    : ['-m', 'python.harness.server'];

  if (alreadyUp) {
    console.log(`[tala] a harness is already running on ws://127.0.0.1:${wsPort}`);
    console.log('[tala] serving the UI only, against that harness.');
  } else {
    console.log('[tala] no harness detected; starting one.');
  }
  console.log(`[tala] web UI at http://${host}:${port}`);
  console.log('[tala] press Ctrl+C to stop.');

  return spawn(executable, args, {
    cwd: root,
    stdio: 'inherit',
    env,
    shell: false,
  });
}

const childPromise = launch();
let child = null;
childPromise.then((c) => {
  child = c;
  wire(c);
});

function wire(child) {

  child.on('error', (err) => {
    console.error(`[tala] could not start: ${err.message}`);
    process.exit(1);
  });
  child.on('exit', (code) => process.exit(code ?? 0));
}

// Pass signals through so Ctrl+C stops the child rather than orphaning it.
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    if (child && !child.killed) child.kill(signal);
  });
}
