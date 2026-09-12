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

const MAIN = fs.readFileSync(path.join(__dirname, '..', '..', 'electron', 'main.js'), 'utf8');

function appUpdateBlock() {
  // The app.update handler, from its method check to the next method check.
  const start = MAIN.indexOf("if (method === 'app.update')");
  const end = MAIN.indexOf("if (method === 'app.version')");
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  return MAIN.slice(start, end);
}

describe('app.update — the only update path', () => {
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
    expect(MAIN).toContain("https.get('https://talacore.app/version.json'");
  });

  test('a missing published hash is "cannot verify", never "verified"', () => {
    expect(MAIN).toContain('callers must treat null as "cannot verify," not "verified."');
    const block = appUpdateBlock();
    expect(block).toContain('proceeding without integrity verification');
  });

  test('both platform scripts verify the hash before extracting', () => {
    const block = appUpdateBlock();
    // Windows: PowerShell Get-FileHash guard ahead of extraction
    const winCheck = block.indexOf('Get-FileHash');
    expect(winCheck).toBeGreaterThan(-1);
    expect(block.indexOf('Extracting update', winCheck)).toBeGreaterThan(winCheck);
    // Unix: sha256 comparison wired into the shell script
    expect(block).toMatch(/sha256|actualHash/i);
  });

  test('the renderer cannot supply update bytes over IPC', () => {
    // The allowlist in preload.js is the renderer's entire IPC surface.
    const preload = fs.readFileSync(
      path.join(__dirname, '..', '..', 'electron', 'preload.js'), 'utf8');
    expect(preload).not.toContain('apply_update');
    expect(MAIN).not.toContain("ipcMain.handle('system:apply_update'");
  });
});
