/**
 * Settings > Workspace folder picker (electron/workspace_dialog.js) and its
 * place in the preload allowlist. The dialog only returns a path; validation
 * and saving happen in the harness (workstation.set_workspace).
 */
'use strict';

const { registerWorkspaceDialog } = require('../../electron/workspace_dialog.js');

function setup(result) {
  const handlers = new Map();
  const ipcMain = { handle: (ch, fn) => handlers.set(ch, fn) };
  const win = { id: 1 };
  const dialog = { showOpenDialog: jest.fn().mockResolvedValue(result) };
  const BrowserWindow = { fromWebContents: jest.fn(() => win) };
  registerWorkspaceDialog(ipcMain, { dialog, BrowserWindow });
  return { handler: handlers.get('dialog:pick-folder'), dialog, win };
}

test('opens a directory picker that can create folders, and returns the choice', async () => {
  const { handler, dialog, win } = setup({ canceled: false, filePaths: ['/home/tala/TalaWorkspace'] });
  const res = await handler({ sender: {} }, { defaultPath: '/home/tala/TalaWorkspace' });
  expect(res).toEqual({ canceled: false, path: '/home/tala/TalaWorkspace' });
  const [parent, opts] = dialog.showOpenDialog.mock.calls[0];
  expect(parent).toBe(win);
  expect(opts.properties).toEqual(expect.arrayContaining(['openDirectory', 'createDirectory']));
  expect(opts.properties).not.toContain('openFile');
  expect(opts.defaultPath).toBe('/home/tala/TalaWorkspace');
});

test('cancel returns no path', async () => {
  const { handler } = setup({ canceled: true, filePaths: [] });
  expect(await handler({ sender: {} }, {})).toEqual({ canceled: true, path: null });
});

test('preload allowlists dialog:pick-folder', () => {
  const src = require('node:fs').readFileSync(require.resolve('../../electron/preload.js'), 'utf8');
  expect(src).toMatch(/'dialog:pick-folder'/);
});
