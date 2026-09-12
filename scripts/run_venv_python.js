/**
 * run_venv_python.js - Auto-generated documentation base.
 */

const { spawnSync } = require('node:child_process');
const { join } = require('node:path');
const { resolvePython } = require('./resolve_python');

const root = join(__dirname, '..');

let executable;
try {
  executable = resolvePython(root);
} catch (err) {
  console.error(err.message);
  process.exit(1);
}

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
