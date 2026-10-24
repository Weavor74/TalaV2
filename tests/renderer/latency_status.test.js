'use strict';

/**
 * renderer/latency_status.js — the status-bar readout of how fast the last reply was.
 *
 * It shows only what the provider reported (a reply with no timing clears the readout instead of leaving the last reply's
 * numbers on screen), says "first token" in milliseconds or seconds as fits, and exposes the detail in the tooltip.
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const ROOT = path.join(__dirname, '../..');
const SRC = fs.readFileSync(path.join(ROOT, 'renderer/latency_status.js'), 'utf8');

function load() {
  const sandbox = { window: {}, console, Math, Number };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return sandbox.window.LatencyStatus;
}

function doc() {
  const els = { 'status-latency': { hidden: true, title: '' }, 'status-latency-text': { textContent: '' } };
  return { els, getElementById: (id) => els[id] || null };
}

describe('format', () => {
  test('shows tokens per second and time to first token', () => {
    assert.equal(load().format({ tokens_per_second: 19.6, ttft_ms: 2100 }), '19.6 tok/s · first token 2.1 s');
  });

  test('short first-token times are in milliseconds', () => {
    assert.match(load().format({ tokens_per_second: 40, ttft_ms: 380 }), /first token 380 ms/);
  });

  test('a rate without a first-token time still shows the rate', () => {
    assert.equal(load().format({ tokens_per_second: 5.04, ttft_ms: null }), '5.0 tok/s');
  });

  test.each([null, undefined, {}, { tokens_per_second: 0 }, { tokens_per_second: -1 }, { tokens_per_second: '19' }, { tokens_per_second: NaN }])(
    'says nothing for %p', (t) => {
      assert.equal(load().format(t), '');
    });
});

describe('detail', () => {
  test('lists the model, the prompt, the reply and who reported it', () => {
    const text = load().detail({ model: 'gpt-oss-20b', prompt_tokens: 8000, prompt_per_second: 615.4, generated_tokens: 64, source: 'llama-server' });
    assert.match(text, /Model: gpt-oss-20b/);
    assert.match(text, /Prompt: 8000 tokens at 615 tok\/s/);
    assert.match(text, /Reply: 64 tokens/);
    assert.match(text, /Reported by llama-server/);
  });

  test('copes with missing fields', () => {
    assert.equal(load().detail({}), 'Speed of the last reply');
    assert.equal(load().detail(null), '');
  });
});

describe('show', () => {
  test('reveals the readout with the numbers', () => {
    const d = doc();
    load().show(d, { tokens_per_second: 19.6, ttft_ms: 2100, model: 'm' });
    assert.equal(d.els['status-latency'].hidden, false);
    assert.equal(d.els['status-latency-text'].textContent, '19.6 tok/s · first token 2.1 s');
    assert.match(d.els['status-latency'].title, /Model: m/);
  });

  test('a reply with no timing clears the previous numbers', () => {
    const d = doc();
    const ui = load();
    ui.show(d, { tokens_per_second: 19.6, ttft_ms: 2100 });
    ui.show(d, null);
    assert.equal(d.els['status-latency'].hidden, true);
    assert.equal(d.els['status-latency-text'].textContent, '');
    assert.equal(d.els['status-latency'].title, '');
  });

  test('does nothing when the page has no status item', () => {
    load().show({ getElementById: () => null }, { tokens_per_second: 1 });
  });
});

describe('wiring', () => {
  const html = fs.readFileSync(path.join(ROOT, 'renderer/index.html'), 'utf8');
  const js = fs.readFileSync(path.join(ROOT, 'renderer/renderer.js'), 'utf8');

  test('the status bar has the item and the page loads the script', () => {
    const footer = html.slice(html.indexOf('<footer id="status-bar"'), html.indexOf('</footer>', html.indexOf('<footer id="status-bar"')));
    assert.match(footer, /id="status-latency"/);
    assert.match(footer, /id="status-latency-text"/);
    assert.match(html, /<script src="latency_status\.js"><\/script>/);
  });

  test('the chat shows the timing of each reply, and clears it for a reply without one', () => {
    assert.match(js, /LatencyStatus\.show\(document, response && response\.timing\)/);
  });

  test('builds with textContent, never by parsing an HTML string', () => {
    assert.doesNotMatch(SRC, /innerHTML/);
  });
});
