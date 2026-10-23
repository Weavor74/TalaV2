'use strict';

/**
 * Thumbs up / thumbs down / "that's wrong" under a reply, and the learning toggle + reset in settings.
 *
 * The rules: feedback names exactly the nodes the reply used (and no more than the server accepts); a reply that used no
 * nodes gets no controls; the user is told what happened, including when learning is off; the reset needs a confirmation;
 * and nothing is built with innerHTML.
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const ROOT = path.join(__dirname, '../..');
const SRC = fs.readFileSync(path.join(ROOT, 'renderer/learning_feedback.js'), 'utf8');

function load() {
  const sandbox = { window: {}, console };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return sandbox.window.LearningFeedback;
}

class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.textContent = '';
    this.disabled = false;
    this.checked = false;
  }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  async click() { return this.listeners.click && this.listeners.click(); }
  async change() { return this.listeners.change && this.listeners.change(); }
  find(pred) { return this.children.find(pred); }
}

const fakeDoc = (byId = {}) => ({
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => byId[id] || null,
});

describe('learningFeedbackBar', () => {
  test('a reply that used no nodes gets no controls', () => {
    const { bar } = load();
    assert.equal(bar(fakeDoc(), [], async () => ({})), null);
    assert.equal(bar(fakeDoc(), null, async () => ({})), null);
    assert.equal(bar(fakeDoc(), undefined, async () => ({})), null);
  });

  test('has an up, a down and a wrong button', () => {
    const el = load().bar(fakeDoc(), ['apples'], async () => ({}));
    const kinds = el.children.filter((c) => c.tagName === 'button').map((c) => c.dataset.kind);
    assert.equal(kinds.join(','), 'up,down,wrong');
  });

  test('sends the nodes the reply used and the kind, then says what changed', async () => {
    const calls = [];
    const rpc = async (method, params) => {
      calls.push([method, params]);
      return { changes: [{ before: 0.8, after: 0.7 }] };
    };
    const el = load().bar(fakeDoc(), ['apples', 'pears'], rpc);
    await el.find((c) => c.dataset.kind === 'down').click();
    assert.equal(JSON.stringify(calls), JSON.stringify([['learning.feedback', { node_ids: ['apples', 'pears'], kind: 'down' }]]));
    const status = el.children.find((c) => c.tagName === 'span');
    assert.match(status.textContent, /0\.80 → 0\.70/);
  });

  test('sends at most the 8 nodes the server accepts', async () => {
    const calls = [];
    const ids = Array.from({ length: 12 }, (_, i) => `n${i}`);
    const el = load().bar(fakeDoc(), ids, async (m, p) => { calls.push(p); return { changes: [] }; });
    await el.find((c) => c.dataset.kind === 'up').click();
    assert.equal(calls[0].node_ids.length, 8);
  });

  test('only one piece of feedback per reply: the buttons lock after a click', async () => {
    const el = load().bar(fakeDoc(), ['apples'], async () => ({ changes: [] }));
    await el.find((c) => c.dataset.kind === 'wrong').click();
    assert.ok(el.children.filter((c) => c.tagName === 'button').every((b) => b.disabled));
  });

  test('says so when learning is off or the call fails, rather than claiming success', async () => {
    const { bar } = load();
    const off = bar(fakeDoc(), ['apples'], async () => ({ changes: [], skipped: 'learning is disabled' }));
    await off.find((c) => c.dataset.kind === 'up').click();
    assert.match(off.children.find((c) => c.tagName === 'span').textContent, /not recorded.*disabled/i);

    const boom = bar(fakeDoc(), ['apples'], async () => { throw new Error('socket closed'); });
    await boom.find((c) => c.dataset.kind === 'up').click();
    assert.match(boom.children.find((c) => c.tagName === 'span').textContent, /not recorded.*socket closed/i);

    const refused = bar(fakeDoc(), ['apples'], async () => ({ error: { message: 'halted' } }));
    await refused.find((c) => c.dataset.kind === 'up').click();
    assert.match(refused.children.find((c) => c.tagName === 'span').textContent, /not recorded.*halted/i);
  });
});

describe('learningInitSettings', () => {
  function setup(rpc, confirmFn = () => true) {
    const toggle = new FakeElement('input');
    const reset = new FakeElement('button');
    const note = new FakeElement('span');
    const doc = fakeDoc({
      'settings-learning-toggle': toggle, 'btn-settings-learning-reset': reset, 'settings-learning-note': note,
    });
    return { toggle, reset, note, run: () => load().initSettings(doc, rpc, confirmFn) };
  }

  test('shows the saved setting', async () => {
    const t = setup(async () => ({ enabled: false }));
    await t.run();
    assert.equal(t.toggle.checked, false);
  });

  test('persists a change through learning.set_enabled', async () => {
    const calls = [];
    const t = setup(async (m, p) => { calls.push([m, p]); return m === 'learning.status' ? { enabled: true } : { enabled: p.enabled }; });
    await t.run();
    t.toggle.checked = false;
    await t.toggle.change();
    assert.equal(JSON.stringify(calls.pop()), JSON.stringify(['learning.set_enabled', { enabled: false }]));
    assert.match(t.note.textContent, /off/i);
  });

  test('reset asks first and does nothing when declined', async () => {
    const calls = [];
    const t = setup(async (m, p) => { calls.push(m); return { enabled: true }; }, () => false);
    await t.run();
    calls.length = 0;
    await t.reset.click();
    assert.deepEqual(calls, []);
  });

  test('reset, once confirmed, sends confirm=true and reports the count', async () => {
    const calls = [];
    const t = setup(async (m, p) => { calls.push([m, p]); return m === 'learning.reset_weights' ? { nodes_reset: 3 } : { enabled: true }; });
    await t.run();
    await t.reset.click();
    assert.equal(JSON.stringify(calls.pop()), JSON.stringify(['learning.reset_weights', { confirm: true }]));
    assert.match(t.note.textContent, /3 node/);
  });

  test('a refused reset is reported, not hidden', async () => {
    const t = setup(async (m) => (m === 'learning.reset_weights' ? { error: { message: 'no lease' } } : { enabled: true }));
    await t.run();
    await t.reset.click();
    assert.match(t.note.textContent, /refused.*no lease/i);
  });

  test('loads without either control present', async () => {
    await load().initSettings(fakeDoc(), async () => ({}), () => true);
  });
});

describe('wiring', () => {
  test('the page loads the script and the chat passes the reply’s node ids to it', () => {
    const html = fs.readFileSync(path.join(ROOT, 'renderer/index.html'), 'utf8');
    const js = fs.readFileSync(path.join(ROOT, 'renderer/renderer.js'), 'utf8');
    assert.match(html, /<script src="learning_feedback\.js"><\/script>/);
    assert.match(html, /id="settings-learning-toggle"/);
    assert.match(html, /id="btn-settings-learning-reset"/);
    assert.match(js, /response\.injected_node_ids/);
    assert.match(js, /LearningFeedback\.bar\(/);
  });

  test('builds with textContent, never innerHTML', () => {
    assert.doesNotMatch(SRC, /innerHTML/);
  });
});
