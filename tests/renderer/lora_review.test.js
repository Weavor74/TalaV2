'use strict';

/**
 * Settings > LoRA review: the queue in front of the training corpus, and the panel that says what the adapter is doing.
 *
 * The rules: replies and memories are untrusted text, so nothing is built from an HTML string; a held-back (blocked) item can be
 * neither approved nor bulk-selected; every decision goes through `lora.decide` and the list is re-read from the server; train
 * and roll back ask first; and the page polls only while it is open.
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const ROOT = path.join(__dirname, '../..');
const SRC = fs.readFileSync(path.join(ROOT, 'renderer/lora_review.js'), 'utf8');

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.className = '';
    this.disabled = false;
    this.checked = false;
    this.hidden = false;
    this.value = '';
    this.title = '';
    this.type = '';
    this._text = '';
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set textContent(value) { this.children = []; this._text = String(value); }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  async fire(name, event) { for (const fn of this.listeners[name] || []) await fn({ target: this, ...event }); }
  async click() { return this.fire('click'); }
  async change() { return this.fire('change'); }
  walk(pred, out = []) {
    for (const child of this.children) { if (pred(child)) out.push(child); child.walk(pred, out); }
    return out;
  }
  byClass(name) { return this.walk((e) => String(e.className).split(/\s+/).includes(name)); }
  button(label) { return this.walk((e) => e.tagName === 'BUTTON' && e.textContent.startsWith(label))[0]; }
}

function makeDoc() {
  const byId = {};
  const doc = {
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => byId[id] || null,
  };
  byId['lora-review-root'] = new FakeElement('div');
  byId['settings-tab-nav'] = new FakeElement('div');
  return { doc, byId };
}

const HOSTILE = '<img src=x onerror=alert(1)> and <script>alert(2)</script>';

function item(id, over = {}) {
  return { id, kind: 'chat', title: `title ${id}`, prompt: `message ${id}`, reply: `reply ${id}`, created_at: '2026-10-01T12:00:00+00:00',
    blocked: null, decision: 'pending', ...over };
}

function status(over = {}) {
  return {
    pipeline: { running: false, last_status: null, last_stage: null, last_reason: null, last_trained_at: null },
    persona: { active: null, adapters: 0, serving: false, routing: 'off', entries: [] },
    counts: { pending: 2, approved: 0, rejected: 0, blocked: 1 },
    settings: { auto_train: false, routing: 'off' },
    ...over,
  };
}

function setup({ items = [item('a'), item('b'), item('c', { blocked: 'safety filter', decision: 'blocked' })], stat = status(), confirmAnswer = true } = {}) {
  const { doc, byId } = makeDoc();
  const calls = [];
  const confirms = [];
  const intervals = [];
  const sandbox = { window: {}, console }; // no global document: the module's own start-up wiring must not run in the test
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  const state = { items, stat };
  const rpc = async (method, params) => {
    params = JSON.parse(JSON.stringify(params)); // the module runs in a vm context: compare plain data, not its prototypes
    calls.push({ method, params });
    if (method === 'lora.candidates') {
      return { items: state.items, total: state.items.length, counts: state.stat.counts };
    }
    if (method === 'lora.status') return state.stat;
    if (method === 'lora.decide') return { success: true, updated: params.ids.length, refused: state.refused || {} };
    if (method === 'lora.train') return state.train || { started: true, reason: 'started' };
    if (method === 'lora.rollback') return { success: true, active: 'a-02' };
    if (method === 'lora.settings') return { success: true, settings: { auto_train: params.auto_train ?? false, routing: params.routing ?? 'off' } };
    throw new Error(`unexpected ${method}`);
  };
  const timers = {
    setInterval: (fn, ms) => { intervals.push({ fn, ms, cleared: false }); return intervals.length - 1; },
    clearInterval: (id) => { if (intervals[id]) intervals[id].cleared = true; },
  };
  const ctl = sandbox.window.LoraReview.create(doc, rpc, (text) => { confirms.push(text); return confirmAnswer; }, timers);
  return { ctl, doc, byId, calls, confirms, intervals, state, root: byId['lora-review-root'], sandbox };
}

const methods = (calls) => calls.map((c) => c.method);

describe('rendering', () => {
  test('shows the counts and every item, her words as text', async () => {
    const t = setup();
    await t.ctl.open();
    const text = t.root.textContent;
    assert.match(text, /pending 2/i);
    assert.match(text, /blocked 1/i);
    assert.match(text, /message a/);
    assert.match(text, /reply b/);
  });

  test('untrusted text is never parsed as HTML', async () => {
    const t = setup({ items: [item('x', { prompt: HOSTILE, reply: HOSTILE, title: HOSTILE, blocked: HOSTILE, decision: 'blocked' })] });
    await t.ctl.open();
    assert.ok(t.root.textContent.includes('<img src=x onerror=alert(1)>'), 'the markup is shown as characters');
    assert.equal(t.root.walk((e) => ['IMG', 'SCRIPT'].includes(e.tagName)).length, 0, 'no element was created from it');
    assert.ok(!/innerHTML|insertAdjacentHTML|outerHTML/.test(SRC), 'the module never builds markup from strings');
  });

  test('a blocked item shows why and can be neither approved nor selected', async () => {
    const t = setup();
    await t.ctl.open();
    const blocked = t.root.byClass('lora-item').find((e) => e.dataset.id === 'c');
    assert.match(blocked.textContent, /safety filter/);
    assert.equal(blocked.button('Approve').disabled, true);
    assert.equal(blocked.walk((e) => e.tagName === 'INPUT' && e.type === 'checkbox')[0].disabled, true);
    const clean = t.root.byClass('lora-item').find((e) => e.dataset.id === 'a');
    assert.equal(clean.button('Approve').disabled, false);
  });
});

describe('deciding', () => {
  test('Approve on one item decides it and re-reads the queue from the server', async () => {
    const t = setup();
    await t.ctl.open();
    t.calls.length = 0;
    const row = t.root.byClass('lora-item').find((e) => e.dataset.id === 'a');
    await row.button('Approve').click();
    assert.deepEqual(t.calls[0], { method: 'lora.decide', params: { ids: ['a'], decision: 'approved' } });
    assert.ok(methods(t.calls).includes('lora.candidates'), 'the list is refreshed after a decision');
  });

  test('bulk approve sends the selected ids only, and is disabled with nothing selected', async () => {
    const t = setup();
    await t.ctl.open();
    assert.equal(t.root.button('Approve selected').disabled, true);
    const boxes = t.root.byClass('lora-item').map((r) => r.walk((e) => e.tagName === 'INPUT' && e.type === 'checkbox')[0]);
    boxes[0].checked = true; await boxes[0].change();
    boxes[1].checked = true; await boxes[1].change();
    assert.equal(t.root.button('Approve selected').disabled, false);
    assert.match(t.root.button('Approve selected').textContent, /2/);
    t.calls.length = 0;
    await t.root.button('Approve selected').click();
    assert.deepEqual(t.calls[0], { method: 'lora.decide', params: { ids: ['a', 'b'], decision: 'approved' } });
  });

  test('bulk reject uses the same selection', async () => {
    const t = setup();
    await t.ctl.open();
    const box = t.root.byClass('lora-item')[0].walk((e) => e.tagName === 'INPUT' && e.type === 'checkbox')[0];
    box.checked = true; await box.change();
    t.calls.length = 0;
    await t.root.button('Reject selected').click();
    assert.deepEqual(t.calls[0], { method: 'lora.decide', params: { ids: ['a'], decision: 'rejected' } });
  });

  test('what the server refused is shown with its reason', async () => {
    const t = setup();
    await t.ctl.open();
    t.state.refused = { a: 'safety filter' };
    await t.root.byClass('lora-item')[0].button('Approve').click();
    assert.match(t.root.byClass('lora-message').map((e) => e.textContent).join(' '), /safety filter/);
  });

  test('a failing call is reported, not thrown', async () => {
    const t = setup();
    const broken = t.sandbox.window.LoraReview.create(t.doc, async () => { throw new Error('socket closed'); }, () => true,
      { setInterval: () => 0, clearInterval: () => {} });
    await broken.open();
    assert.match(t.root.textContent, /socket closed/);
  });
});

describe('filters', () => {
  test('a status tab asks the server for that status', async () => {
    const t = setup();
    await t.ctl.open();
    t.calls.length = 0;
    await t.root.button('approved').click();
    const call = t.calls.find((c) => c.method === 'lora.candidates');
    assert.equal(call.params.status, 'approved');
  });

  test('a kind tab asks for that kind', async () => {
    const t = setup();
    await t.ctl.open();
    t.calls.length = 0;
    await t.root.button('Memories').click();
    assert.equal(t.calls.find((c) => c.method === 'lora.candidates').params.kind, 'memory');
  });

  test('changing the filter clears the selection', async () => {
    const t = setup();
    await t.ctl.open();
    const box = t.root.byClass('lora-item')[0].walk((e) => e.tagName === 'INPUT' && e.type === 'checkbox')[0];
    box.checked = true; await box.change();
    await t.root.button('approved').click();
    assert.equal(t.root.button('Approve selected').disabled, true);
  });
});

describe('the status panel', () => {
  const withAdapter = () => status({
    persona: { active: 'a-03', adapters: 2, serving: true, routing: 'off',
      entries: [{ id: 'a-02', scores: { persona_nll: 2.1 } }, { id: 'a-03', scores: { persona_nll: 1.8, safety: 1, general: 0.9 } }] },
    pipeline: { running: false, last_status: 'deployed', last_stage: 'done', last_reason: 'beat the current adapter', last_trained_at: '2026-10-25T02:10:00' },
  });

  test('shows the active adapter, its eval scores and the last run', async () => {
    const t = setup({ stat: withAdapter() });
    await t.ctl.open();
    const text = t.root.textContent;
    assert.match(text, /a-03/);
    assert.match(text, /persona_nll[^0-9]*1\.8/);
    assert.match(text, /safety[^0-9]*1/);
    assert.match(text, /deployed/);
    assert.match(text, /beat the current adapter/);
  });

  test('says plainly when there is no adapter and no run yet', async () => {
    const t = setup();
    await t.ctl.open();
    assert.match(t.root.textContent, /no adapter/i);
    assert.match(t.root.textContent, /no run yet/i);
  });

  test('says a run is in progress', async () => {
    const t = setup({ stat: status({ pipeline: { running: true } }) });
    await t.ctl.open();
    assert.match(t.root.textContent, /in progress/i);
  });

  test('the routing select and the auto-train box write the two settings', async () => {
    const t = setup();
    await t.ctl.open();
    const select = t.root.walk((e) => e.tagName === 'SELECT')[0];
    select.value = 'in_character';
    t.calls.length = 0;
    await select.change();
    assert.deepEqual(t.calls[0], { method: 'lora.settings', params: { routing: 'in_character' } });
    const box = t.root.byClass('lora-autotrain')[0];
    box.checked = true;
    t.calls.length = 0;
    await box.change();
    assert.deepEqual(t.calls[0], { method: 'lora.settings', params: { auto_train: true } });
  });

  test('Train now asks first and does nothing if declined', async () => {
    const t = setup({ confirmAnswer: false });
    await t.ctl.open();
    t.calls.length = 0;
    await t.root.button('Train now').click();
    assert.equal(t.confirms.length, 1);
    assert.match(t.confirms[0], /GPU|main model/i);
    assert.equal(methods(t.calls).includes('lora.train'), false);
  });

  test('Train now, confirmed, starts a run and reports the reason when it did not start', async () => {
    const t = setup();
    await t.ctl.open();
    await t.root.button('Train now').click();
    assert.ok(methods(t.calls).includes('lora.train'));
    t.state.train = { started: false, reason: 'a training run is already in progress' };
    await t.root.button('Train now').click();
    assert.match(t.root.textContent, /already in progress/);
  });

  test('Roll back asks first, then restores, and is off when there is no adapter', async () => {
    const none = setup();
    await none.ctl.open();
    assert.equal(none.root.button('Roll back').disabled, true);

    const declined = setup({ stat: withAdapter(), confirmAnswer: false });
    await declined.ctl.open();
    declined.calls.length = 0;
    await declined.root.button('Roll back').click();
    assert.equal(methods(declined.calls).includes('lora.rollback'), false);

    const t = setup({ stat: withAdapter() });
    await t.ctl.open();
    await t.root.button('Roll back').click();
    assert.equal(t.confirms.length, 1);
    assert.ok(methods(t.calls).includes('lora.rollback'));
    assert.match(t.root.textContent, /a-02/);
  });
});

describe('polling', () => {
  test('nothing polls until the page is open, and closing stops it', async () => {
    const t = setup();
    assert.equal(t.intervals.length, 0);
    await t.ctl.open();
    assert.equal(t.intervals.length, 1);
    t.ctl.close();
    assert.equal(t.intervals[0].cleared, true);
  });

  test('opening twice does not start a second timer', async () => {
    const t = setup();
    await t.ctl.open();
    await t.ctl.open();
    assert.equal(t.intervals.filter((i) => !i.cleared).length, 1);
  });

  test('a poll tick re-reads the status, not the list the person is working through', async () => {
    const t = setup();
    await t.ctl.open();
    t.calls.length = 0;
    await t.intervals[0].fn();
    assert.deepEqual(methods(t.calls), ['lora.status']);
  });
});

describe('wiring', () => {
  const html = fs.readFileSync(path.join(ROOT, 'renderer/index.html'), 'utf8');

  test('index.html loads the module before renderer.js and has the tab and the panel', () => {
    const mod = html.indexOf('lora_review.js');
    assert.ok(mod > -1, 'lora_review.js is not loaded by index.html');
    assert.ok(mod < html.indexOf('renderer.js"'));
    assert.match(html, /data-panel="lora"/);
    assert.match(html, /id="settings-panel-lora"/);
    assert.match(html, /id="lora-review-root"/);
  });

  test('opening the LoRA tab opens the page, and any other tab closes it', async () => {
    const t = setup();
    t.ctl.init();
    const nav = t.byId['settings-tab-nav'];
    await nav.fire('click', { target: { dataset: { panel: 'lora' } } });
    assert.equal(t.intervals.filter((i) => !i.cleared).length, 1);
    await nav.fire('click', { target: { dataset: { panel: 'inference' } } });
    assert.equal(t.intervals.filter((i) => !i.cleared).length, 0);
  });
});
