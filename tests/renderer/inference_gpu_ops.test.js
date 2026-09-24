'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
// Jest supplies the global `test`; node:assert works under jest. These files
// were written for node:test, which registers nothing inside jest and made
// the whole suite fail with 'must contain at least one test'.
const assert = require('node:assert/strict');

function load(gpuAvailable) {
  const src = fs.readFileSync(path.join(__dirname, '../../renderer/inference_gpu_ops.js'), 'utf8');
  const els = {
    'inference-gpu-note': { hidden: true, textContent: '' },
    'inference-router-copy': { textContent: '' },
    'ptc-desc-embedded': { textContent: '' },
  };
  const sandbox = {
    window: {},
    document: { getElementById: (id) => els[id] || null },
    localStorage: {
      _data: { 'tala.chat.provider_id': 'embedded-builtin' },
      getItem(k) { return Object.prototype.hasOwnProperty.call(this._data, k) ? this._data[k] : null; },
      setItem(k, v) { this._data[k] = String(v); },
      removeItem(k) { delete this._data[k]; },
    },
    invoke: async () => ({ gpu: { available: gpuAvailable, devices: gpuAvailable ? [{ name: 'GPU' }] : [] } }),
    console,
    AgentChat: { _providerId: 'embedded-builtin' },
    IPCLog: { append() {} },
  };
  sandbox.window = sandbox;
  vm.runInNewContext(src, sandbox, { filename: 'inference_gpu_ops.js' });
  return { sandbox, els };
}

test('CPU-only box keeps embedded wording and session pin', async () => {
  const { sandbox, els } = load(false);
  const mgr = { _modelPicker: { value: 'embedded-builtin' } };
  await sandbox.inferenceProbeGpuAndApply(mgr);
  assert.equal(mgr._gpuAvailable, false);
  assert.match(els['inference-gpu-note'].textContent, /No GPU/);
  assert.equal(sandbox.localStorage.getItem('tala.chat.provider_id'), 'embedded-builtin');
});

test('GPU box steers away from CPU Qwen session pin', async () => {
  const { sandbox, els } = load(true);
  const mgr = { _modelPicker: { value: 'embedded-builtin' }, activeProvider: () => null };
  await sandbox.inferenceProbeGpuAndApply(mgr);
  assert.equal(mgr._gpuAvailable, true);
  assert.match(els['inference-gpu-note'].textContent, /GPU/);
  assert.equal(sandbox.localStorage.getItem('tala.chat.provider_id'), null);
  const meta = sandbox.inferenceEmbeddedMeta(mgr, { status: 'online' });
  assert.match(meta, /fallback/i);
});
