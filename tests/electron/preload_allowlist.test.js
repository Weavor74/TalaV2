/**
 * Highs H3: unknown IPC channels must fail closed — never fall through to python:rpc.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadPreload() {
  const preloadPath = path.join(__dirname, '../../electron/preload.js');
  const src = fs.readFileSync(preloadPath, 'utf8');

  const allowedInvokes = [];
  const exposed = {};
  const ipcRenderer = {
    invoke: jest.fn(async (channel, data) => {
      allowedInvokes.push({ channel, data });
      return { ok: true, channel, data };
    }),
    on: jest.fn(),
    removeListener: jest.fn(),
  };
  const contextBridge = {
    exposeInMainWorld: jest.fn((name, api) => {
      exposed[name] = api;
    }),
  };

  const sandbox = {
    require: (mod) => {
      if (mod === 'electron') return { contextBridge, ipcRenderer };
      throw new Error(`unexpected require: ${mod}`);
    },
    module: { exports: {} },
    exports: {},
    console,
    Promise,
    Set,
    Error,
    Array,
  };
  vm.runInNewContext(src, sandbox, { filename: 'preload.js' });
  return { exposed, ipcRenderer, allowedInvokes };
}

describe('preload allowlist fail-closed (H3)', () => {
  test('unknown channel rejects and does not invoke python:rpc', async () => {
    const { exposed, ipcRenderer } = loadPreload();
    expect(exposed.tala).toBeTruthy();
    await expect(exposed.tala.invoke('shell.run', { cmd: 'whoami' })).rejects.toThrow(
      /not allowlisted/i,
    );
    expect(ipcRenderer.invoke).not.toHaveBeenCalled();
  });

  test('allowlisted channel reaches ipcRenderer.invoke', async () => {
    const { exposed, ipcRenderer } = loadPreload();
    const res = await exposed.tala.invoke('python:rpc', { method: 'harness.health', params: {} });
    expect(res.ok).toBe(true);
    expect(ipcRenderer.invoke).toHaveBeenCalledWith(
      'python:rpc',
      { method: 'harness.health', params: {} },
    );
  });

  test('source no longer forwards unknown channels to python:rpc', () => {
    const src = fs.readFileSync(
      path.join(__dirname, '../../electron/preload.js'),
      'utf8',
    );
    const guard = src.match(
      /if\s*\(!ALLOWED_CHANNELS\.has\(channel\)\)\s*\{([\s\S]*?)\n\s{2}\}/,
    );
    expect(guard).toBeTruthy();
    expect(guard[1]).not.toMatch(/ipcRenderer\.invoke\(['\"]python:rpc/);
    expect(guard[1]).toMatch(/reject|throw|Error/i);
  });
});

describe('preload allowlist stays closed for smoke-found RPC names', () => {
  test.each([
    'gateway.status',
    'plugins.list',
    'mcp.discover',
    'shell.run',
  ])('%s is rejected (must use helpers.invoke → python:rpc)', async (channel) => {
    const { exposed, ipcRenderer } = loadPreload();
    await expect(exposed.tala.request(channel, {})).rejects.toThrow(/not allowlisted/i);
    expect(ipcRenderer.invoke).not.toHaveBeenCalled();
  });
});

describe('preload allows harness:ws-status push event', () => {
  test('tala.on("harness:ws-status") registers ipcRenderer listener', () => {
    const { exposed, ipcRenderer } = loadPreload();
    const cb = jest.fn();
    const cleanup = exposed.tala.on('harness:ws-status', cb);
    expect(ipcRenderer.on).toHaveBeenCalledWith('harness:ws-status', expect.any(Function));
    expect(typeof cleanup).toBe('function');
  });
});
