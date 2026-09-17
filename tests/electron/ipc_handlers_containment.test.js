/**
 * Containment tests for electron/ipc-handlers.js (P2 review item 20).
 *
 * The path/containment logic in this module is the sandbox boundary between
 * the renderer and the filesystem — the exact place the P1 review items
 * lived. These tests run the REAL handlers against a temp sandbox, with only
 * the electron module mocked: a fake ipcMain that records the registered
 * handlers so each can be invoked directly.
 */

'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// Sandbox root, created before the module under test reads TALA_USER_DATA.
const mockSandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'tala-ipc-sandbox-'));
const mockOutside = fs.mkdtempSync(path.join(os.tmpdir(), 'tala-ipc-outside-'));
process.env.TALA_USER_DATA = mockSandbox;

const mockRegisteredHandlers = new Map();

jest.mock('electron', () => ({
  app: { getAppPath: () => mockSandbox },
  ipcMain: {
    handle: (channel, fn) => mockRegisteredHandlers.set(channel, fn),
  },
}));

const {
  registerHandlers,
  safePath,
  isNodesHtmlPath,
} = require('../../electron/ipc-handlers.js');

let networkIsolated = false;
registerHandlers({ isNetworkIsolated: () => networkIsolated });

const invoke = (channel, payload) => mockRegisteredHandlers.get(channel)(null, payload);

beforeAll(() => {
  fs.writeFileSync(path.join(mockSandbox, 'inside.txt'), 'inside contents');
  fs.writeFileSync(path.join(mockOutside, 'secret.txt'), 'must never be readable');
  fs.mkdirSync(path.join(mockSandbox, 'vault'), { recursive: true });
  fs.writeFileSync(path.join(mockSandbox, 'vault', 'note.txt'), 'vault note');
});

afterAll(() => {
  fs.rmSync(mockSandbox, { recursive: true, force: true });
  fs.rmSync(mockOutside, { recursive: true, force: true });
});

/* ── safePath: the guard itself ─────────────────────────────────────── */

describe('safePath', () => {
  test('accepts a relative path inside the sandbox', async () => {
    await expect(safePath(mockSandbox, 'inside.txt')).resolves.toBe(
      fs.realpathSync(path.join(mockSandbox, 'inside.txt'))
    );
  });

  test('rejects ../ traversal out of the sandbox', async () => {
    const escape = path.join('..', path.basename(mockOutside), 'secret.txt');
    await expect(safePath(mockSandbox, escape)).rejects.toThrow(/outside|Access denied/);
  });

  test('rejects an absolute path outside the sandbox', async () => {
    await expect(
      safePath(mockSandbox, path.join(mockOutside, 'secret.txt'))
    ).rejects.toThrow(/outside|Access denied/);
  });

  test('rejects a symlink that points outside the sandbox', async () => {
    const link = path.join(mockSandbox, 'sneaky-link');
    fs.symlinkSync(path.join(mockOutside, 'secret.txt'), link);
    await expect(safePath(mockSandbox, 'sneaky-link')).rejects.toThrow(/outside|Access denied/);
  });

  test('rejects a not-yet-existing path whose parent is outside (write case)', async () => {
    const escape = path.join('..', path.basename(mockOutside), 'new-file.txt');
    await expect(safePath(mockSandbox, escape)).rejects.toThrow(/outside|Access denied/);
  });

  test('accepts a not-yet-existing path whose parent is inside (write case)', async () => {
    await expect(safePath(mockSandbox, 'new-file.txt')).resolves.toBeDefined();
  });

  test('rejects empty and non-string paths', async () => {
    await expect(safePath(mockSandbox, '')).rejects.toThrow(/non-empty string/);
    await expect(safePath(mockSandbox, null)).rejects.toThrow(/non-empty string/);
    await expect(safePath(mockSandbox, 42)).rejects.toThrow(/non-empty string/);
  });

  test('a path that merely PREFIXES the sandbox name is still outside', async () => {
    // /tmp/tala-ipc-sandbox-XXXX-evil is not inside /tmp/tala-ipc-sandbox-XXXX,
    // which a naive startsWith(baseDir) would get wrong.
    const sibling = mockSandbox + '-evil';
    fs.mkdirSync(sibling, { recursive: true });
    fs.writeFileSync(path.join(sibling, 'x.txt'), 'x');
    try {
      await expect(safePath(mockSandbox, path.join(sibling, 'x.txt'))).rejects.toThrow(
        /outside|Access denied/
      );
    } finally {
      fs.rmSync(sibling, { recursive: true, force: true });
    }
  });
});

/* ── handler-level behavior ─────────────────────────────────────────── */

describe('file:read', () => {
  test('reads a sandboxed file', async () => {
    const res = await invoke('file:read', { filePath: 'inside.txt' });
    expect(res.ok).toBe(true);
    expect(res.data).toBe('inside contents');
  });

  test('refuses traversal', async () => {
    const escape = path.join('..', path.basename(mockOutside), 'secret.txt');
    await expect(invoke('file:read', { filePath: escape })).rejects.toThrow(
      /outside|Access denied/
    );
  });
});

describe('file:write', () => {
  test('writes inside the sandbox, creating parents', async () => {
    const res = await invoke('file:write', { filePath: 'sub/dir/out.txt', data: 'written' });
    expect(res.ok).toBe(true);
    expect(fs.readFileSync(path.join(mockSandbox, 'sub', 'dir', 'out.txt'), 'utf8')).toBe('written');
  });

  test('refuses to write outside the sandbox', async () => {
    const escape = path.join('..', path.basename(mockOutside), 'planted.txt');
    await expect(invoke('file:write', { filePath: escape, data: 'x' })).rejects.toThrow(
      /outside|Access denied/
    );
    expect(fs.existsSync(path.join(mockOutside, 'planted.txt'))).toBe(false);
  });

  test('requires a data field', async () => {
    await expect(invoke('file:write', { filePath: 'y.txt' })).rejects.toThrow(/"data"/);
  });
});

describe('file:delete', () => {
  test('refuses traversal', async () => {
    const escape = path.join('..', path.basename(mockOutside), 'secret.txt');
    await expect(invoke('file:delete', { filePath: escape })).rejects.toThrow(
      /outside|Access denied/
    );
    expect(fs.existsSync(path.join(mockOutside, 'secret.txt'))).toBe(true);
  });

  test('refuses to delete a directory', async () => {
    fs.mkdirSync(path.join(mockSandbox, 'a-dir'), { recursive: true });
    await expect(invoke('file:delete', { filePath: 'a-dir' })).rejects.toThrow(
      /not a regular file/
    );
  });

  test('deletes a sandboxed regular file', async () => {
    fs.writeFileSync(path.join(mockSandbox, 'doomed.txt'), 'x');
    const res = await invoke('file:delete', { filePath: 'doomed.txt' });
    expect(res.ok).toBe(true);
    expect(fs.existsSync(path.join(mockSandbox, 'doomed.txt'))).toBe(false);
  });
});

describe('file:list', () => {
  test('lists sandbox entries and skips ignored directories', async () => {
    fs.mkdirSync(path.join(mockSandbox, 'node_modules', 'junk'), { recursive: true });
    fs.writeFileSync(path.join(mockSandbox, 'node_modules', 'junk', 'x.js'), 'x');
    const res = await invoke('file:list', { dir: '.', recursive: true });
    expect(res.ok).toBe(true);
    const names = res.entries.map((e) => e.name);
    expect(names).toContain('inside.txt');
    expect(names).not.toContain('node_modules');
    expect(names).not.toContain('x.js');
  });

  test('returns empty for a missing directory instead of throwing', async () => {
    const res = await invoke('file:list', { dir: 'does-not-exist' });
    expect(res).toEqual({ ok: true, entries: [] });
  });

  test('refuses to list outside the sandbox', async () => {
    await expect(invoke('file:list', { dir: mockOutside })).rejects.toThrow(
      /outside|Access denied/
    );
  });
});

describe('vault:read', () => {
  test('is blocked while the network is active', async () => {
    networkIsolated = false;
    await expect(invoke('vault:read', { filePath: 'note.txt' })).rejects.toThrow(
      /network isolation/
    );
  });

  test('reads vault files only under isolation, confined to vault/', async () => {
    networkIsolated = true;
    const res = await invoke('vault:read', { filePath: 'note.txt' });
    expect(res).toMatchObject({ ok: true, data: 'vault note', isolated: true });

    // inside the sandbox but mockOutside vault/ must still be refused
    await expect(
      invoke('vault:read', { filePath: path.join('..', 'inside.txt') })
    ).rejects.toThrow(/outside|Access denied/);
  });
});

/* ── isNodesHtmlPath: the schema-validation gate ───────────────────── */

describe('isNodesHtmlPath', () => {
  test.each([
    ['nodes/thing.html', true],
    ['nodes\\thing.html', true], // Windows renderer separators
    ['deep/nodes/thing.html', true],
    ['nodes/thing.txt', false],
    ['renderer/nodes.html', false],
    [null, false],
    [42, false],
  ])('%s → %s', (input, expected) => {
    expect(isNodesHtmlPath(input)).toBe(expected);
  });
});

describe('file:* vault refusal (UI-audit C4)', () => {
  test('file:read must not bypass vault:read', async () => {
    await expect(invoke('file:read', { filePath: 'vault/note.txt' })).rejects.toThrow(
      /secrets|Access denied/
    );
  });

  test('file:write refuses vault paths', async () => {
    await expect(
      invoke('file:write', { filePath: 'vault/planted.txt', data: 'x' })
    ).rejects.toThrow(/secrets|Access denied/);
  });
});
