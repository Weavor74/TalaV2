/**
 * UI-audit C3: Electron must mint distinct WS (8765) and HTTP UI (8767) tokens.
 */
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  ensureWsToken,
  ensureWebUiToken,
} = require('../../electron/token_files');

let dataRoot;

beforeEach(() => {
  dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'tala-token-files-'));
});

afterEach(() => {
  fs.rmSync(dataRoot, { recursive: true, force: true });
});

describe('ensureWsToken / ensureWebUiToken (C3)', () => {
  test('mints distinct secrets into distinct files', () => {
    const env = {};
    const ws = ensureWsToken(dataRoot, env);
    const web = ensureWebUiToken(dataRoot, env);
    expect(ws).toHaveLength(43); // 32 bytes base64url
    expect(web).toHaveLength(43);
    expect(ws).not.toEqual(web);

    const wsPath = path.join(dataRoot, 'vault', '.keys', 'ws_token.txt');
    const webPath = path.join(dataRoot, 'vault', '.keys', 'web_ui_token.txt');
    expect(fs.readFileSync(wsPath, 'utf8').trim()).toBe(ws);
    expect(fs.readFileSync(webPath, 'utf8').trim()).toBe(web);
  });

  test('TALA_WEB_TOKEN does not pin the WS secret', () => {
    const env = { TALA_WEB_TOKEN: 'http-ui-only' };
    const ws = ensureWsToken(dataRoot, env);
    expect(ws).not.toBe('http-ui-only');
    expect(ensureWebUiToken(dataRoot, env)).toBe('http-ui-only');
  });

  test('TALA_WS_TOKEN pins WS only; web UI stays separate', () => {
    const env = { TALA_WS_TOKEN: 'ws-only-token' };
    expect(ensureWsToken(dataRoot, env)).toBe('ws-only-token');
    const web = ensureWebUiToken(dataRoot, env);
    expect(web).not.toBe('ws-only-token');
    expect(web.length).toBeGreaterThanOrEqual(32);
  });

  test('reuses existing files and never copies WS into web UI', () => {
    const keys = path.join(dataRoot, 'vault', '.keys');
    fs.mkdirSync(keys, { recursive: true });
    fs.writeFileSync(path.join(keys, 'ws_token.txt'), 'persisted-ws', { mode: 0o600 });
    const env = {};
    expect(ensureWsToken(dataRoot, env)).toBe('persisted-ws');
    const web = ensureWebUiToken(dataRoot, env);
    expect(web).not.toBe('persisted-ws');
    expect(fs.existsSync(path.join(keys, 'web_ui_token.txt'))).toBe(true);
  });
});
