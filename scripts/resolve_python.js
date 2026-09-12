/**
 * resolve_python.js — pick a Python interpreter that actually exists.
 *
 * Prefers the project venv; otherwise probes `python3` then `python` with
 * `--version`. A bare `python` does not exist on modern macOS or most Linux
 * distros, and on Windows it can be the Microsoft Store stub (which exits
 * non-zero), so nothing is trusted without a successful probe.
 */

'use strict';

const { spawnSync } = require('node:child_process');
const { existsSync } = require('node:fs');
const { join } = require('node:path');

function venvPython(root) {
  return process.platform === 'win32'
    ? join(root, '.venv', 'Scripts', 'python.exe')
    : join(root, '.venv', 'bin', 'python');
}

function systemPython() {
  for (const candidate of ['python3', 'python']) {
    const probe = spawnSync(candidate, ['--version'], { stdio: 'ignore' });
    if (!probe.error && probe.status === 0) return candidate;
  }
  return null;
}

/**
 * @param {string} root project root (holds .venv)
 * @returns {string} executable path/name
 * @throws when no interpreter can be found
 */
function resolvePython(root) {
  const venv = venvPython(root);
  if (existsSync(venv)) return venv;
  const system = systemPython();
  if (system) return system;
  throw new Error(
    'No Python interpreter found: no project .venv, and neither "python3" nor "python" works. ' +
    'Run the installer (or `uv sync`) first.'
  );
}

module.exports = { resolvePython, venvPython, systemPython };
