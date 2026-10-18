/**
 * renderer/workspace_settings.js — Settings > Workspace, run in a VM with a
 * tiny fake DOM. Every decision is the server's: the panel shows its reason.
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const code = fs.readFileSync(path.join(__dirname, '../../renderer/workspace_settings.js'), 'utf8');

function fakeEl() {
  const listeners = {};
  return {
    value: '', textContent: '', hidden: false, placeholder: '', style: {},
    addEventListener: (ev, fn) => { listeners[ev] = fn; },
    fire: (ev) => listeners[ev] && listeners[ev](),
  };
}

function load({ replies, channels }) {
  const ids = ['workspace-current', 'workspace-current-detail', 'workspace-state-badge', 'workspace-root-input',
    'workspace-extra-input', 'workspace-suggest-copy', 'workspace-jobs-note', 'workspace-message',
    'btn-workspace-browse', 'btn-workspace-save', 'btn-workspace-suggest'];
  const els = Object.fromEntries(ids.map(id => [id, fakeEl()]));
  const tab = fakeEl();
  const document = {
    readyState: 'complete',
    getElementById: id => els[id] || null,
    querySelector: () => tab,
    addEventListener: () => {},
  };
  const invoke = jest.fn(async (channel, data) => {
    const key = channel === 'python:rpc' ? data.method : channel;
    const reply = replies[key];
    if (reply instanceof Error) throw reply;
    return typeof reply === 'function' ? reply(data) : reply;
  });
  const window = { tala: { allowedChannels: channels } };
  const ctx = { document, window, invoke, console };
  vm.runInNewContext(code, ctx);
  return { ws: window.WorkspaceSettings, els, invoke, tab };
}

const UNSET = { configured: false, root: null, suggested_root: 'D:\\TalaWorkspace', extra_roots: [], running_jobs: [] };

test('unconfigured: says so and offers the suggestion only as a placeholder', async () => {
  const { ws, els } = load({ replies: { 'workstation.get_config': UNSET }, channels: ['python:rpc'] });
  await ws.refresh();
  expect(els['workspace-current'].textContent).toBe('Not configured');
  expect(els['workspace-state-badge'].textContent).toBe('NOT SET');
  expect(els['workspace-root-input'].value).toBe('');
  expect(els['workspace-root-input'].placeholder).toBe('D:\\TalaWorkspace');
  expect(els['workspace-current-detail'].textContent).toMatch(/off until you choose/);
});

test('browser mode hides Browse; desktop shows it', async () => {
  const web = load({ replies: { 'workstation.get_config': UNSET }, channels: ['python:rpc'] });
  await web.ws.refresh();
  expect(web.els['btn-workspace-browse'].hidden).toBe(true);
  const desk = load({ replies: { 'workstation.get_config': UNSET }, channels: ['python:rpc', 'dialog:pick-folder'] });
  await desk.ws.refresh();
  expect(desk.els['btn-workspace-browse'].hidden).toBe(false);
});

test('picker fills the field and the server check is shown', async () => {
  const { ws, els, invoke } = load({
    replies: {
      'workstation.get_config': UNSET,
      'dialog:pick-folder': { canceled: false, path: 'E:\\Work\\Tala' },
      'workstation.validate_workspace': { ok: true, exists: false },
    },
    channels: ['python:rpc', 'dialog:pick-folder'],
  });
  await ws.refresh();
  await ws.browse();
  expect(els['workspace-root-input'].value).toBe('E:\\Work\\Tala');
  expect(invoke).toHaveBeenCalledWith('dialog:pick-folder', expect.objectContaining({ defaultPath: 'D:\\TalaWorkspace' }));
  expect(els['workspace-message'].textContent).toMatch(/will be created/);
});

test('save shows the server refusal reason and does not pretend it saved', async () => {
  const { ws, els } = load({
    replies: {
      'workstation.get_config': UNSET,
      'workstation.set_workspace': { success: false, error: 'C:\\ is a whole drive (a drive root, mount point or the filesystem root).' },
    },
    channels: ['python:rpc'],
  });
  await ws.refresh();
  els['workspace-root-input'].value = 'C:\\';
  await ws.save();
  expect(els['workspace-message'].textContent).toMatch(/whole drive/);
  expect(els['workspace-current'].textContent).toBe('Not configured');
});

test('successful save sends root + extra folders and shows the new workspace', async () => {
  const saved = { success: true, configured: true, root: 'D:\\TalaWorkspace', source: 'settings', created: true,
    extra_roots: ['D:\\Shared'], running_jobs: [], suggested_root: 'D:\\TalaWorkspace', note: '' };
  const { ws, els, invoke } = load({
    replies: { 'workstation.get_config': UNSET, 'workstation.set_workspace': saved },
    channels: ['python:rpc'],
  });
  await ws.refresh();
  els['workspace-root-input'].value = 'D:\\TalaWorkspace';
  els['workspace-extra-input'].value = 'D:\\Shared\n\n';
  await ws.save();
  expect(invoke).toHaveBeenCalledWith('python:rpc', { method: 'workstation.set_workspace',
    params: { root: 'D:\\TalaWorkspace', extra_roots: ['D:\\Shared'] } });
  expect(els['workspace-current'].textContent).toBe('D:\\TalaWorkspace');
  expect(els['workspace-message'].textContent).toMatch(/Saved\. Tala now works in D:\\TalaWorkspace/);
});

test('opening the tab refreshes', async () => {
  const { tab, invoke } = load({ replies: { 'workstation.get_config': UNSET }, channels: [] });
  tab.fire('click');
  await new Promise(r => setImmediate(r));
  expect(invoke).toHaveBeenCalledWith('python:rpc', { method: 'workstation.get_config', params: {} });
});
