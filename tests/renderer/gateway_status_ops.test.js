'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

function load() {
  const src = fs.readFileSync(path.join(__dirname, '../../renderer/gateway_status_ops.js'), 'utf8');
  const sandbox = {
    window: {},
    console,
    ChatManager: { appendMessage() {} },
    AgentChat: { appendMessage() {} },
    IPCLog: { append() {} },
  };
  sandbox.window = sandbox;
  vm.runInNewContext(src, sandbox, { filename: 'gateway_status_ops.js' });
  return sandbox;
}

test('busy snapshot is not System Degraded', () => {
  const s = load();
  const out = s.gatewayInterpretStatus({ status: 'online', busy: true, busy_detail: 'Tala is busy (inference.generate)' }, null);
  assert.equal(out.kind, 'busy');
  assert.match(out.message, /busy/i);
});

test('rpc timeout is busy, not a dead sidecar', () => {
  const s = load();
  const out = s.gatewayInterpretStatus(null, new Error('python:rpc timeout for method "gateway.status"'));
  assert.equal(out.kind, 'busy');
  assert.equal(out.message, 'Tala is busy');
});

test('real disconnect stays degraded', () => {
  const s = load();
  const out = s.gatewayInterpretStatus(null, new Error('Python harness WebSocket is not connected.'));
  assert.equal(out.kind, 'degraded');
  assert.match(out.message, /Connection failed/);
});

test('online and idle hides the bar', () => {
  const s = load();
  const out = s.gatewayInterpretStatus({ status: 'online', busy: false }, null);
  assert.equal(out.kind, 'ok');
});

test('fallback notice reaches agent chat', () => {
  const s = load();
  const seen = [];
  s.AgentChat.appendMessage = (sender, name, text) => seen.push({ sender, name, text });
  s.showInferenceFallbackNotice({
    from: 'Gemini',
    to: 'Tala Embedded Engine',
    reason: 'HTTP 400 thought_signature',
  });
  assert.equal(seen.length, 1);
  assert.match(seen[0].text, /Gemini/);
  assert.match(seen[0].text, /Tala Embedded Engine/);
});
