/**
 * renderer/lora_review.js -- Settings > LoRA: the review queue in front of the training corpus, and what the adapter is doing.
 *
 * Nothing is trained on unless it was approved here. The queue lists her chat replies (with the message each answered) and her
 * HTML-NN skills and memories; anything the safety filters hold back is shown with the reason and cannot be approved or selected.
 * Decisions go to `lora.decide` and the list is re-read from the server, so the page never claims more than the server stored.
 * The panel beside it shows the active adapter, how it scored, the last run, and the two controls (scheduled training, which
 * turns try the persona), plus Train now and Roll back, both of which ask first.
 *
 * Replies and memories are untrusted text: everything here is built with createElement and textContent, never an HTML string.
 * Classic script (the learning_feedback.js pattern) so Jest can run it in a VM with a fake DOM; uses the global `invoke` from
 * helpers.js in the app. The page polls only while it is open.
 */
'use strict';

const LORA_STATUSES = ['pending', 'approved', 'rejected', 'blocked'];
const LORA_KINDS = [['', 'All'], ['chat', 'Replies'], ['memory', 'Memories']];
const LORA_ROUTING = [['off', 'off'], ['fallback', 'after the other providers'], ['in_character', 'first for in-character turns without tools'], ['final_stage', 'her voice on every finished reply']];
const LORA_POLL_MS = 5000;
const LORA_PAGE = 100;

const LORA_TRAIN_CONFIRM = 'Start a training run now? It takes the GPU from the main model while it runs (the main model is unloaded and comes back '
  + 'afterwards), and the adapter is only deployed if it beats the current one on the eval set.';
const LORA_ROLLBACK_CONFIRM = 'Roll back to the previous adapter? The persona server restarts with it on the next turn. With no earlier adapter, '
  + 'the persona is switched off.';

function loraErrorText(res) {
  if (!res) return 'no response';
  if (res.error) return String(res.error.message || res.error);
  if (res.success === false) return String(res.halted ? 'the emergency halt is engaged' : (res.error || 'refused'));
  return '';
}

function loraEl(doc, tag, props = {}, children = []) {
  const el = doc.createElement(tag);
  if (props.className) el.className = props.className;
  if (props.text !== undefined) el.textContent = props.text;
  if (props.title) el.title = props.title;
  if (props.type) el.type = props.type;
  if (props.disabled) el.disabled = true;
  if (props.checked) el.checked = true;
  if (props.value !== undefined) el.value = props.value;
  for (const [k, v] of Object.entries(props.dataset || {})) el.dataset[k] = v;
  for (const child of children) el.appendChild(child);
  return el;
}

function loraScoresText(entry) {
  const scores = entry && entry.scores && typeof entry.scores === 'object' ? entry.scores : {};
  const parts = Object.entries(scores).map(([k, v]) => `${k} ${typeof v === 'number' ? Number(v.toFixed(3)) : v}`);
  return parts.length ? parts.join(', ') : 'no eval scores yet';
}

function loraRunText(pipeline) {
  const p = pipeline || {};
  if (p.running) return 'A training run is in progress.';
  if (!p.last_status) return 'No run yet.';
  const stage = p.last_stage ? ` (${p.last_stage})` : '';
  const why = p.last_reason ? `: ${p.last_reason}` : '';
  const when = p.last_trained_at ? `, last trained ${p.last_trained_at}` : '';
  return `Last run: ${p.last_status}${stage}${why}${when}.`;
}

function loraCreate(doc, rpc, confirmFn, timers) {
  const state = { status: 'pending', kind: '', limit: LORA_PAGE, items: [], total: 0, counts: null, info: null, selected: new Set(), message: '', timer: null };
  const root = doc.getElementById('lora-review-root');
  const parts = {};

  async function call(method, params) {
    try {
      return await rpc(method, params || {});
    } catch (err) {
      return { error: String((err && err.message) || err) };
    }
  }

  function say(text) {
    state.message = text || '';
    if (parts.message) parts.message.textContent = state.message;
  }

  function button(label, onClick, props = {}) {
    const b = loraEl(doc, 'button', { className: `btn btn-ghost ${props.className || ''}`.trim(), text: label, title: props.title, disabled: props.disabled });
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }

  // --- the status panel -------------------------------------------------------------------------------------------------------

  function renderStatus() {
    const box = parts.status;
    if (!box) return;
    box.textContent = '';
    const info = state.info;
    if (!info) { box.appendChild(loraEl(doc, 'div', { text: 'Loading…' })); return; }
    const persona = info.persona || {};
    const entries = Array.isArray(persona.entries) ? persona.entries : [];
    const active = entries.find((e) => e.id === persona.active) || null;
    box.appendChild(loraEl(doc, 'div', { className: 'lora-active', text: persona.active
      ? `Active adapter: ${persona.active}${persona.serving ? ' (serving now)' : ''}`
      : 'No adapter is active: turns use the base models.' }));
    box.appendChild(loraEl(doc, 'div', { className: 'lora-scores', text: active ? `Eval: ${loraScoresText(active)}` : 'Eval: nothing to show without an active adapter.' }));
    box.appendChild(loraEl(doc, 'div', { className: 'lora-run', text: loraRunText(info.pipeline) }));

    const settings = info.settings || {};
    const auto = loraEl(doc, 'input', { className: 'lora-autotrain', type: 'checkbox', checked: !!settings.auto_train });
    auto.addEventListener('change', async () => {
      const res = await call('lora.settings', { auto_train: !!auto.checked });
      const err = loraErrorText(res);
      if (err) { auto.checked = !auto.checked; say(`Not changed: ${err}`); return; }
      say(auto.checked ? 'Scheduled training is on (nights, once the system is idle).' : 'Scheduled training is off.');
    });
    box.appendChild(loraEl(doc, 'label', { className: 'lora-setting' }, [auto, loraEl(doc, 'span', { text: ' Train automatically at night, once idle' })]));

    const select = loraEl(doc, 'select', { className: 'lora-routing' });
    for (const [value, label] of LORA_ROUTING) select.appendChild(loraEl(doc, 'option', { value, text: label }));
    select.value = settings.routing || 'off';
    select.addEventListener('change', async () => {
      const res = await call('lora.settings', { routing: select.value });
      const err = loraErrorText(res);
      say(err ? `Not changed: ${err}` : `The persona is used: ${select.value}.`);
    });
    box.appendChild(loraEl(doc, 'label', { className: 'lora-setting' }, [loraEl(doc, 'span', { text: 'Use the persona: ' }), select]));

    const row = loraEl(doc, 'div', { className: 'lora-actions' });
    row.appendChild(button('Train now', onTrain, { title: 'Run the whole train, evaluate, deploy cycle now' }));
    row.appendChild(button('Roll back', onRollback, { disabled: !persona.active, title: 'Go back to the previous adapter' }));
    box.appendChild(row);
  }

  async function onTrain() {
    if (!confirmFn(LORA_TRAIN_CONFIRM)) return;
    const res = await call('lora.train', {});
    const err = loraErrorText(res);
    say(err ? `Not started: ${err}` : (res.started ? 'Training started in the background.' : `Not started: ${res.reason}`));
    await refreshStatus();
  }

  async function onRollback() {
    if (!confirmFn(LORA_ROLLBACK_CONFIRM)) return;
    const res = await call('lora.rollback', {});
    const err = loraErrorText(res);
    say(err ? `Not rolled back: ${err}` : (res.active ? `Rolled back to ${res.active}.` : 'Rolled back: no earlier adapter, the persona is off.'));
    await refreshStatus();
  }

  // --- the queue --------------------------------------------------------------------------------------------------------------

  function renderCounts() {
    const box = parts.counts;
    if (!box) return;
    box.textContent = '';
    const counts = state.counts || {};
    for (const name of LORA_STATUSES) {
      const label = loraEl(doc, 'span', { className: 'lora-count', text: `${name} ${counts[name] ?? 0}` });
      box.appendChild(label);
    }
  }

  function renderFilters() {
    const box = parts.filters;
    box.textContent = '';
    for (const name of LORA_STATUSES) {
      box.appendChild(button(name, () => setFilter({ status: name }), { className: state.status === name ? 'active' : '' }));
    }
    for (const [value, label] of LORA_KINDS) {
      box.appendChild(button(label, () => setFilter({ kind: value }), { className: state.kind === value ? 'active' : '' }));
    }
  }

  function renderBulk() {
    const box = parts.bulk;
    box.textContent = '';
    const n = state.selected.size;
    box.appendChild(button(`Approve selected (${n})`, () => decide([...state.selected], 'approved'), { disabled: n === 0 }));
    box.appendChild(button(`Reject selected (${n})`, () => decide([...state.selected], 'rejected'), { disabled: n === 0 }));
  }

  function renderItem(item) {
    const blocked = !!item.blocked;
    const box = loraEl(doc, 'div', { className: 'lora-item', dataset: { id: item.id, decision: item.decision || 'pending' } });
    const check = loraEl(doc, 'input', { type: 'checkbox', checked: state.selected.has(item.id), disabled: blocked });
    check.addEventListener('change', () => {
      if (check.checked) state.selected.add(item.id); else state.selected.delete(item.id);
      renderBulk();
    });
    box.appendChild(check);
    const body = loraEl(doc, 'div', { className: 'lora-item-body' });
    body.appendChild(loraEl(doc, 'div', { className: 'lora-kind', text: item.kind === 'memory' ? `Memory: ${item.title || ''}` : 'Reply' }));
    body.appendChild(loraEl(doc, 'div', { className: 'lora-prompt', text: `You: ${item.prompt || ''}` }));
    body.appendChild(loraEl(doc, 'div', { className: 'lora-reply', text: `Tala: ${item.reply || ''}` }));
    if (blocked) body.appendChild(loraEl(doc, 'div', { className: 'lora-blocked', text: `Held back: ${item.blocked}` }));
    box.appendChild(body);
    const actions = loraEl(doc, 'div', { className: 'lora-item-actions' });
    actions.appendChild(button('Approve', () => decide([item.id], 'approved'), { disabled: blocked || item.decision === 'approved' }));
    actions.appendChild(button('Reject', () => decide([item.id], 'rejected'), { disabled: blocked || item.decision === 'rejected' }));
    if (item.decision === 'approved' || item.decision === 'rejected') actions.appendChild(button('Clear', () => decide([item.id], 'pending')));
    box.appendChild(actions);
    return box;
  }

  function renderList() {
    const box = parts.list;
    box.textContent = '';
    if (!state.items.length) box.appendChild(loraEl(doc, 'div', { className: 'lora-empty', text: 'Nothing here.' }));
    for (const item of state.items) box.appendChild(renderItem(item));
    if (state.total > state.items.length) {
      box.appendChild(button(`Show more (${state.items.length} of ${state.total})`, async () => { state.limit += LORA_PAGE; await refreshList(); }));
    }
  }

  async function refreshList() {
    const params = { status: state.status, limit: state.limit };
    if (state.kind) params.kind = state.kind;
    const res = await call('lora.candidates', params);
    const err = loraErrorText(res);
    if (err) { say(`Could not load the queue: ${err}`); return; }
    state.items = Array.isArray(res.items) ? res.items : [];
    state.total = Number(res.total) || state.items.length;
    state.counts = res.counts || state.counts;
    const visible = new Set(state.items.map((i) => i.id));
    for (const id of [...state.selected]) if (!visible.has(id)) state.selected.delete(id);
    renderCounts();
    renderList();
    renderBulk();
  }

  async function refreshStatus() {
    const res = await call('lora.status', {});
    const err = loraErrorText(res);
    if (err) { say(`Could not read the status: ${err}`); return; }
    state.info = res;
    if (res.counts) { state.counts = res.counts; renderCounts(); }
    renderStatus();
  }

  async function setFilter(change) {
    Object.assign(state, change, { limit: LORA_PAGE });
    state.selected.clear();
    renderFilters();
    await refreshList();
  }

  async function decide(ids, decision) {
    if (!ids.length) return;
    const res = await call('lora.decide', { ids, decision });
    const err = loraErrorText(res);
    if (err) { say(`Not saved: ${err}`); return; }
    const refused = Object.entries(res.refused || {});
    say(refused.length
      ? `${res.updated} saved; ${refused.length} refused: ${refused.map(([, why]) => why).join(', ')}`
      : `${res.updated} ${decision === 'pending' ? 'cleared' : decision}.`);
    state.selected.clear();
    await refreshList();
  }

  // --- opening and closing ----------------------------------------------------------------------------------------------------

  function build() {
    if (!root || parts.built) return;
    parts.built = true;
    parts.status = loraEl(doc, 'div', { className: 'lora-status-panel' });
    parts.message = loraEl(doc, 'div', { className: 'lora-message' });
    if (parts.message.setAttribute) parts.message.setAttribute('role', 'status');
    parts.counts = loraEl(doc, 'div', { className: 'lora-counts' });
    parts.filters = loraEl(doc, 'div', { className: 'lora-filters' });
    parts.bulk = loraEl(doc, 'div', { className: 'lora-bulk' });
    parts.list = loraEl(doc, 'div', { className: 'lora-list' });
    for (const key of ['status', 'message', 'counts', 'filters', 'bulk', 'list']) root.appendChild(parts[key]);
    renderFilters();
  }

  async function open() {
    build();
    if (state.timer === null) state.timer = timers.setInterval(() => refreshStatus(), LORA_POLL_MS);
    await Promise.all([refreshStatus(), refreshList()]);
  }

  function close() {
    if (state.timer !== null) { timers.clearInterval(state.timer); state.timer = null; }
  }

  /** Open when the LoRA tab is shown, close when any other is. */
  function init() {
    const nav = doc.getElementById('settings-tab-nav');
    if (!nav) return;
    nav.addEventListener('click', (event) => {
      const panel = event && event.target && event.target.dataset ? event.target.dataset.panel : undefined;
      if (!panel) return;
      if (panel === 'lora') open(); else close();
    });
  }

  return { open, close, init, refresh: () => Promise.all([refreshStatus(), refreshList()]), state };
}

if (typeof window !== 'undefined') {
  window.LoraReview = { create: loraCreate };
  if (typeof document !== 'undefined' && document.getElementById && document.getElementById('lora-review-root')) {
    const start = () => loraCreate(document, (method, params) => invoke('python:rpc', { method, params }), (text) => window.confirm(text),
      { setInterval: (fn, ms) => window.setInterval(fn, ms), clearInterval: (id) => window.clearInterval(id) }).init();
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
  }
}
