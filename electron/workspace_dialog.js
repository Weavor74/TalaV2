/**
 * electron/workspace_dialog.js — the folder picker for Settings > Workspace.
 *
 * One IPC channel, `dialog:pick-folder`: opens the native "choose a folder"
 * dialog (openDirectory + createDirectory, so the user can make a new folder
 * from inside it) and returns `{ canceled, path }`. It only RETURNS a path; it
 * never saves anything. The renderer passes the path to the harness RPC
 * `workstation.set_workspace`, which is where validation and persistence live
 * (so the browser UI, which has no native dialog, goes through the same check).
 */

'use strict';

/**
 * @param {import('electron').IpcMain} ipcMain
 * @param {{dialog?: object, BrowserWindow?: object}} [deps] injected for tests
 */
function registerWorkspaceDialog(ipcMain, deps = {}) {
  ipcMain.handle('dialog:pick-folder', async (event, payload) => {
    const electron = (deps.dialog && deps.BrowserWindow) ? deps : require('electron');
    const { dialog, BrowserWindow } = electron;
    const opts = {
      title: (payload && typeof payload.title === 'string' && payload.title.slice(0, 120)) || "Choose Tala's workspace",
      buttonLabel: 'Use this folder',
      properties: ['openDirectory', 'createDirectory', 'promptToCreate', 'dontAddToRecent'],
    };
    if (payload && typeof payload.defaultPath === 'string' && payload.defaultPath.trim()) {
      opts.defaultPath = payload.defaultPath.trim();
    }
    const win = (event && event.sender && BrowserWindow.fromWebContents)
      ? BrowserWindow.fromWebContents(event.sender)
      : null;
    const res = win ? await dialog.showOpenDialog(win, opts) : await dialog.showOpenDialog(opts);
    if (!res || res.canceled || !Array.isArray(res.filePaths) || !res.filePaths.length) {
      return { canceled: true, path: null };
    }
    return { canceled: false, path: res.filePaths[0] };
  });
}

module.exports = { registerWorkspaceDialog };
