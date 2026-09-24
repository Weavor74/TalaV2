/**
 * Contract tests for the signed updater path in electron/main.js (P2 item 20).
 *
 * main.js cannot be required in a test process (it boots an Electron app at
 * module scope), so these pin the source of the ONE update path the app has
 * left after the P2 item 21 consolidation — the same style the Python side
 * uses in tests/test_ota_update_push.py, which owns the "the unsigned path
 * stays gone" assertions.
 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const MAIN = fs.readFileSync(path.join(__dirname, '..', '..', 'electron', 'main.js'), 'utf8');
const HARNESS_ADMIN = fs.readFileSync(
  path.join(__dirname, '..', '..', 'electron', 'harness_admin.js'), 'utf8');
// The generated updater scripts themselves (extracted from main.js under the
// size ratchet); the vm harness below wires the REAL builders into the
// handler, so generated-script assertions exercise actual output.
const SCRIPTS = fs.readFileSync(
  path.join(__dirname, '..', '..', 'electron', 'update_scripts.js'), 'utf8');
const {
  buildWindowsUpdateScript,
  buildUnixUpdateScript,
} = require('../../electron/update_scripts');

function appUpdateBlock() {
  // The app.update handler, from its method check to the next method check.
  const start = MAIN.indexOf("if (method === 'app.update')");
  const end = MAIN.indexOf("if (method === 'app.version')");
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  return MAIN.slice(start, end);
}

const { isNewerVersion } = require('../../electron/harness_admin');

describe('app.update — the only update path', () => {
  function updateHarness(hash, { published = '99.0.0', installed = '0.1.83' } = {}) {
    let handler;
    const writeFileSync = jest.fn();
    const spawn = jest.fn(() => ({ unref() {} }));
    const context = {
      ipcMain: { handle: (_channel, fn) => { handler = fn; } },
      fetchExpectedUpdateHash: async () => hash,
      fetchPublishedVersion: async () => published,
      getInstalledVersion: () => installed,
      isNewerVersion,
      buildWindowsUpdateScript,
      buildUnixUpdateScript,
      URL, __dirname: 'C:\\Tala\\electron',
      process: { platform: 'win32', env: {} },
      setTimeout: jest.fn(),
      require: (name) => {
        if (name === 'fs') return { writeFileSync };
        if (name === 'child_process') return { spawn };
        if (name === 'path') return path.win32;
        if (name === 'os') return { tmpdir: () => 'C:\\Temp' };
        return require(name);
      },
    };
    const start = MAIN.indexOf('function registerPythonRpcHandler()');
    const end = MAIN.indexOf('function sendLogToRenderer(', start);
    vm.runInNewContext(MAIN.slice(start, end) + '\nregisterPythonRpcHandler();', context);
    return { update: (url) => handler(null, { method: 'app.update', params: { url } }), writeFileSync, spawn };
  }

  test('no hash means no script, no download and no updater process', async () => {
    const harness = updateHarness(null);
    expect(await harness.update('https://talacore.app/tala-windows.zip')).toMatchObject({ __rpcError: true });
    expect(harness.writeFileSync).not.toHaveBeenCalled();
    expect(harness.spawn).not.toHaveBeenCalled();
  });

  test('a published version that is not strictly newer is refused', async () => {
    for (const published of ['0.1.83', '0.1.82', '0.0.99']) {
      const harness = updateHarness('a'.repeat(64), { published, installed: '0.1.83' });
      expect(await harness.update('https://talacore.app/tala-windows.zip')).toMatchObject({ __rpcError: true });
      expect(harness.writeFileSync).not.toHaveBeenCalled();
      expect(harness.spawn).not.toHaveBeenCalled();
    }
  });

  test('an unparseable published version is "cannot verify", never "newer"', async () => {
    for (const published of ['0.1.84-rc1', 'latest', null, '0.1']) {
      const harness = updateHarness('a'.repeat(64), { published, installed: '0.1.83' });
      expect(await harness.update('https://talacore.app/tala-windows.zip')).toMatchObject({ __rpcError: true });
      expect(harness.spawn).not.toHaveBeenCalled();
    }
  });

  test('isNewerVersion is strict and NaN-safe', () => {
    expect(isNewerVersion('0.1.84', '0.1.83')).toBe(true);
    expect(isNewerVersion('0.2.0', '0.1.99')).toBe(true);
    expect(isNewerVersion('1.0.0', '0.99.99')).toBe(true);
    expect(isNewerVersion('0.1.83', '0.1.83')).toBe(false);
    expect(isNewerVersion('0.1.82', '0.1.83')).toBe(false);
    expect(isNewerVersion('0.1.84-rc1', '0.1.83')).toBe(false);
    expect(isNewerVersion('0.1.84.1', '0.1.83')).toBe(false);
    expect(isNewerVersion(undefined, '0.1.83')).toBe(false);
  });

  test('the Windows updater writes a post-mortem log', () => {
    expect(SCRIPTS).toContain('Start-Transcript');
    expect(SCRIPTS).toContain('tala-update.log');
  });

  test('PowerShell receives URL metacharacters as environment data', async () => {
    const harness = updateHarness('a'.repeat(64));
    const url = 'https://talacore.app/$(Write-Output%20injected).zip';
    expect(await harness.update(url)).toMatchObject({ success: true });
    const script = harness.writeFileSync.mock.calls[0][1];
    expect(script).toContain('$updateUrl = $env:TALA_UPDATE_URL');
    expect(script).toContain('Invoke-WebRequest -Uri $updateUrl');
    expect(script).not.toContain('injected');
    expect(harness.spawn.mock.calls[0][2].env.TALA_UPDATE_URL).toBe(new URL(url).href);
  });

  test('downloads are pinned to the release host over https', () => {
    const block = appUpdateBlock();
    expect(block).toContain("const UPDATE_HOST = 'talacore.app'");
    expect(block).toContain("parsedUrl.protocol !== 'https:'");
    expect(block).toContain('parsedUrl.hostname !== UPDATE_HOST');
  });

  test('URL validation happens before any download starts', () => {
    const block = appUpdateBlock();
    const validation = block.indexOf('Refusing update: URL host must be');
    const download = block.indexOf('return new Promise');
    expect(validation).toBeGreaterThan(-1);
    expect(download).toBeGreaterThan(validation);
  });

  test('the expected hash is fetched independently of the renderer', () => {
    const block = appUpdateBlock();
    expect(block).toContain('fetchExpectedUpdateHash(platformKey)');
    // and the fetch helper itself goes to the pinned host, not a caller-supplied URL
    expect(HARNESS_ADMIN).toContain("https.get('https://talacore.app/version.json'");
  });

  test('a missing published hash is "cannot verify", never "verified"', () => {
    expect(HARNESS_ADMIN).toMatch(/cannot verify/);
    const block = appUpdateBlock();
    expect(block).toContain("return { __rpcError: true, error: 'Refusing update: no valid published SHA-256 hash is available.' }");
  });

  test('both platform scripts verify the hash before extracting', () => {
    // Windows: PowerShell Get-FileHash guard ahead of extraction
    const winCheck = SCRIPTS.indexOf('Get-FileHash');
    expect(winCheck).toBeGreaterThan(-1);
    expect(SCRIPTS.indexOf('Extracting update', winCheck)).toBeGreaterThan(winCheck);
    // Unix: the hash is verified in the main process before the script runs
    expect(appUpdateBlock()).toMatch(/sha256|actualHash/i);
  });

  test('the archive never extracts directly over the install root', async () => {
    const harness = updateHarness('a'.repeat(64));
    await harness.update('https://talacore.app/tala-windows.zip');
    const script = harness.writeFileSync.mock.calls[0][1];
    // Extraction goes to staging; the install root is only ever the robocopy
    // destination of an already-validated staging tree.
    expect(script).toMatch(/Expand-Archive -Path "[^"]*tala-update\.zip" -DestinationPath \$staging/);
    expect(script).not.toMatch(/Expand-Archive[^\n]*DestinationPath "C:\\\\Tala"/);
    expect(script).toContain('tala-update-staged');
  });

  test('a failed apply restores the backup, on both platforms', () => {
    // Windows: robocopy failure path restores from the backup dir.
    expect(SCRIPTS).toMatch(/if \(\$LASTEXITCODE -ge 8\) \{[\s\S]*?robocopy \$backup/);
    // Unix: cp failure path restores from the backup dir.
    expect(SCRIPTS).toMatch(/if ! cp -Rp "\$STAGING\/\."[\s\S]*?cp -Rp "\$BACKUP\/\."/);
    // Both extract to staging before touching the install.
    expect(SCRIPTS).toContain('the install was not touched');
    // And every overwritten file is backed up first.
    expect(SCRIPTS).toContain('Backing up the files this update overwrites');
  });

  test('the renderer cannot supply update bytes over IPC', () => {
    // The allowlist in preload.js is the renderer's entire IPC surface.
    const preload = fs.readFileSync(
      path.join(__dirname, '..', '..', 'electron', 'preload.js'), 'utf8');
    expect(preload).not.toContain('apply_update');
    expect(MAIN).not.toContain("ipcMain.handle('system:apply_update'");
  });
});
