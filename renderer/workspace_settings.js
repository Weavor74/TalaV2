/**
 * renderer/workspace_settings.js — Settings > Workspace.
 *
 * Shows the folder Tala uses as her own computer and lets the user choose it:
 * the native folder picker in the desktop app (`dialog:pick-folder`), or a
 * typed path in the browser UI. Nothing is decided here: every save goes to
 * the harness RPC `workstation.set_workspace`, which validates the folder
 * (and creates it), persists it, and applies it immediately. The reason for
 * a refusal is shown exactly as the server gives it.
 *
 * A classic (non-module) script, the desktop_commands.js pattern, so Jest can
 * run it in a VM with a fake DOM. Uses the global `invoke` from helpers.js.
 */
'use strict';

const WorkspaceSettings = {
  _wired: false,
  _status: null,

  _el(id) { return document.getElementById(id); },

  canBrowse() {
    const channels = window.tala && window.tala.allowedChannels;
    return Array.isArray(channels) && channels.includes('dialog:pick-folder');
  },

  _message(text, kind) {
    const el = this._el('workspace-message');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = kind === 'error' ? 'var(--danger, #fa5252)' : (kind === 'ok' ? 'var(--success, #40c057)' : '');
  },

  render(st) {
    this._status = st || {};
    const s = this._status;
    const current = this._el('workspace-current');
    const detail = this._el('workspace-current-detail');
    const badge = this._el('workspace-state-badge');
    const input = this._el('workspace-root-input');
    const extra = this._el('workspace-extra-input');
    const suggestCopy = this._el('workspace-suggest-copy');
    const jobsNote = this._el('workspace-jobs-note');
    if (current) current.textContent = s.configured ? s.root : 'Not configured';
    if (badge) badge.textContent = s.configured ? 'ACTIVE' : 'NOT SET';
    if (detail) {
      const bits = [];
      if (!s.configured) bits.push("Tala's computer tools are off until you choose a folder.");
      if (s.problem) bits.push(`The configured folder was refused: ${s.problem}`);
      if (s.source === 'env') bits.push('Set by the TALA_WORKSTATION_ROOT environment variable (save here to override it).');
      if (s.source === 'settings') bits.push('Saved in Settings.');
      if (s.audit_path) bits.push(`Audit log: ${s.audit_path}`);
      detail.textContent = bits.join(' ');
    }
    if (input) {
      input.placeholder = s.suggested_root || '';
      if (!input.value) input.value = s.root || '';
    }
    if (extra && !extra.value) extra.value = (s.extra_roots || []).join('\n');
    if (suggestCopy) suggestCopy.textContent = s.suggested_root ? `Suggested: ${s.suggested_root}` : '';
    if (jobsNote) {
      const n = (s.running_jobs || []).length;
      jobsNote.hidden = n === 0;
      jobsNote.textContent = n ? `${n} background job(s) running; they keep running in the folder they started in.` : '';
    }
    const browse = this._el('btn-workspace-browse');
    if (browse) browse.hidden = !this.canBrowse();
  },

  async refresh() {
    try {
      this.render(await invoke('python:rpc', { method: 'workstation.get_config', params: {} }));
    } catch (err) {
      this._message(`Could not load the workspace setting: ${err.message || err}`, 'error');
    }
  },

  async browse() {
    const input = this._el('workspace-root-input');
    try {
      const res = await invoke('dialog:pick-folder', {
        title: "Choose Tala's workspace",
        defaultPath: (input && input.value) || (this._status && this._status.suggested_root) || '',
      });
      if (res && !res.canceled && res.path && input) {
        input.value = res.path;
        await this.check();
      }
    } catch (err) {
      this._message(`Folder picker unavailable: ${err.message || err}. Type the path instead.`, 'error');
    }
  },

  async check() {
    const input = this._el('workspace-root-input');
    const path = input && input.value.trim();
    if (!path) { this._message('', ''); return null; }
    try {
      const res = await invoke('python:rpc', { method: 'workstation.validate_workspace', params: { path } });
      if (res && res.ok) this._message(res.exists ? 'Folder OK.' : 'Folder OK; it will be created when you save.', 'ok');
      else this._message((res && res.reason) || 'This folder cannot be used.', 'error');
      return res;
    } catch (err) {
      this._message(`Could not check the folder: ${err.message || err}`, 'error');
      return null;
    }
  },

  async save() {
    const input = this._el('workspace-root-input');
    const extraEl = this._el('workspace-extra-input');
    const root = input ? input.value.trim() : '';
    if (!root) { this._message('Choose or type a folder first.', 'error'); return null; }
    const extra_roots = (extraEl ? extraEl.value : '').split('\n').map(x => x.trim()).filter(Boolean);
    this._message('Saving…', '');
    let res;
    try {
      res = await invoke('python:rpc', { method: 'workstation.set_workspace', params: { root, extra_roots } });
    } catch (err) {
      this._message(`Save failed: ${err.message || err}`, 'error');
      return null;
    }
    if (!res || !res.success) {
      this._message((res && res.error) || 'Save failed.', 'error');
      return res;
    }
    if (input) input.value = res.root || root;
    if (extraEl) extraEl.value = (res.extra_roots || []).join('\n');
    this.render(res);
    this._message(`Saved. Tala now works in ${res.root}.${res.created ? ' (Folder created.)' : ''} ${res.note || ''}`.trim(), 'ok');
    return res;
  },

  init() {
    if (this._wired) return;
    this._wired = true;
    const on = (id, ev, fn) => { const el = this._el(id); if (el) el.addEventListener(ev, fn); };
    on('btn-workspace-browse', 'click', () => this.browse());
    on('btn-workspace-save', 'click', () => this.save());
    on('btn-workspace-suggest', 'click', () => {
      const input = this._el('workspace-root-input');
      if (input && this._status && this._status.suggested_root) {
        input.value = this._status.suggested_root;
        this.check();
      }
    });
    on('workspace-root-input', 'change', () => this.check());
    const tab = document.querySelector('.settings-sub-tab[data-panel="workspace"]');
    if (tab) tab.addEventListener('click', () => this.refresh());
  },
};

if (typeof window !== 'undefined') window.WorkspaceSettings = WorkspaceSettings;
if (typeof document !== 'undefined' && document.addEventListener) {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => WorkspaceSettings.init());
  else WorkspaceSettings.init();
}
