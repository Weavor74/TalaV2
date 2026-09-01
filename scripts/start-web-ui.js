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

console.log(`[tala] starting harness with the web UI at http://${host}:${port}`);
console.log('[tala] press Ctrl+C to stop.');

const child = spawn(executable, ['-m', 'python.harness.server'], {
  cwd: root,
  stdio: 'inherit',
  env,
  shell: false,
});

child.on('error', (err) => {
  console.error(`[tala] could not start the harness: ${err.message}`);
  process.exit(1);
});

// Pass signals through so Ctrl+C stops the harness rather than orphaning it.
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    if (!child.killed) child.kill(signal);
  });
}

child.on('exit', (code) => process.exit(code ?? 0));
