# File: `run_venv_python.js`

## Overview

A Node.js utility script used during Electron builds to execute Python scripts in the isolated virtual environment.

## File Contents

```js
const { spawnSync } = require('node:child_process');
const { existsSync } = require('node:fs');
const { join } = require('node:path');

const root = join(__dirname, '..');
const pythonPath = process.platform === 'win32'
  ? join(root, '.venv', 'Scripts', 'python.exe')
  : join(root, '.venv', 'bin', 'python');

const executable = existsSync(pythonPath) ? pythonPath : 'python';
const result = spawnSync(executable, process.argv.slice(2), {
  cwd: root,
  stdio: 'inherit',
  shell: false,
});

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}

process.exit(result.status ?? 0);

```
