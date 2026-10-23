'use strict';

/**
 * The engine panel must say plainly that LoRA is not implemented.
 *
 * The backend already reports `lora.implemented = false` on every engine, and
 * the panel ignored it, so the UI read as though adapters were something the
 * engine could do.
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

  test('says nothing when LoRA is implemented', () => {
    assert.equal(load()({ lora: { implemented: true, note: 'ready' } }), '');
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
