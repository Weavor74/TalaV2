'use strict';

/**
 * The engine panel must say plainly what LoRA is.
 *
 * The backend reports `lora: { implemented, note }` on every engine. While nothing could train or load an adapter the panel had
 * to say "not implemented"; now that one can (python/inference/lora_trainer.py engine_status), it shows the backend's own
 * one-line status instead, and still says nothing when an engine reports nothing at all.
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const ROOT = path.join(__dirname, '../..');

function load() {
  const src = fs.readFileSync(path.join(ROOT, 'renderer/lora_status.js'), 'utf8');
  const sandbox = { window: {}, console };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return sandbox.window.loraStatusNote;
}

describe('loraStatusNote', () => {
  test('says LoRA is not implemented when the backend reports so', () => {
    const html = load()({ lora: { implemented: false, note: 'LoRA is not implemented: nothing trains an adapter.' } });
    assert.match(html, /not implemented/i);
    assert.match(html, /nothing trains an adapter/);
  });

  test('shows the backend note, and no "not implemented", when LoRA is implemented', () => {
    const html = load()({ lora: { implemented: true, note: 'adapter a-03 active; scheduled training is off' } });
    assert.match(html, /LoRA: adapter a-03 active; scheduled training is off/);
    assert.ok(!/not implemented/i.test(html), html);
  });

  test('says LoRA is on without inventing a note when an implemented engine sent none', () => {
    const html = load()({ lora: { implemented: true } });
    assert.match(html, /LoRA: /);
    assert.ok(!/not implemented/i.test(html), html);
  });

  test('escapes an implemented note too', () => {
    const html = load()({ lora: { implemented: true, note: '<script>alert(1)</script>' } });
    assert.ok(!html.includes('<script'), html);
    assert.match(html, /&lt;script&gt;/);
  });

  test('makes no claim about an engine that did not report LoRA status', () => {
    const note = load();
    assert.equal(note({}), '');
    assert.equal(note(null), '');
    assert.equal(note({ lora: {} }), '');
  });

  test('escapes the backend note before it reaches innerHTML', () => {
    const html = load()({ lora: { implemented: false, note: '<img src=x onerror=alert(1)>' } });
    assert.ok(!html.includes('<img'), html);
    assert.match(html, /&lt;img/);
  });

  test('has a fallback sentence when the note is missing', () => {
    assert.match(load()({ lora: { implemented: false } }), /not implemented/i);
  });
});

describe('wiring', () => {
  test('index.html loads the module before renderer.js', () => {
    const html = fs.readFileSync(path.join(ROOT, 'renderer/index.html'), 'utf8');
    const mod = html.indexOf('lora_status.js');
    assert.ok(mod > -1, 'lora_status.js is not loaded by index.html');
    assert.ok(mod < html.indexOf('renderer.js"'), 'lora_status.js must load before renderer.js');
  });

  test('the engine panel renders the note', () => {
    const src = fs.readFileSync(path.join(ROOT, 'renderer/renderer.js'), 'utf8');
    assert.match(src, /loraStatusNote\(engine\)/);
  });
});
