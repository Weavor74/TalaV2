'use strict';

// Runtime command resolution shared by desktop bootstrap and terminal launch.
const path = require('node:path');
const { spawnSync } = require('node:child_process');

/**
 * Resolves the absolute path to the Python executable within the local virtual environment.
 * Handles cross-platform differences (Windows 'Scripts/python.exe' vs Unix 'bin/python').
 */
function venvPythonPath(basePath) {
  const venvBinDir = process.platform === 'win32' ? 'Scripts' : 'bin';
  const pythonName = process.platform === 'win32' ? 'python.exe' : 'python';
  return path.join(basePath, '.venv', venvBinDir, pythonName);
}

/**
 * Resolves a working system Python interpreter, preferring `python3`.
 *
 * A bare `python` does not exist on modern macOS or most Linux distros, and on
 * Windows it can hit the Microsoft Store stub (which exits non-zero), so each
 * candidate is probed with `--version` before being trusted. Returns null when
 * no interpreter is found so callers can surface a real error instead of a
 * confusing ENOENT.
 */
let cachedSystemPython;
function systemPythonCommand() {
  if (cachedSystemPython !== undefined) return cachedSystemPython;
  cachedSystemPython = null;
  for (const candidate of ['python3', 'python']) {
    try {
      const probe = spawnSync(candidate, ['--version'], { stdio: 'ignore' });
      if (!probe.error && probe.status === 0) {
        cachedSystemPython = candidate;
        break;
      }
    } catch { /* keep looking */ }
  }
  return cachedSystemPython;
}

/**
 * Resolves the POSIX shell to spawn, via PATH rather than a hardcoded
 * /bin/bash (which does not exist on NixOS). Falls back to `sh`.
 */
let cachedPosixShell;
function posixShellCommand() {
  if (cachedPosixShell !== undefined) return cachedPosixShell;
  const probe = spawnSync('bash', ['-c', 'true'], { stdio: 'ignore' });
  cachedPosixShell = (!probe.error && probe.status === 0) ? 'bash' : 'sh';
  return cachedPosixShell;
}

module.exports = { venvPythonPath, systemPythonCommand, posixShellCommand };
