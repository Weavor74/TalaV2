'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
// Jest supplies the global `test`; node:assert works under jest. These files
// were written for node:test, which registers nothing inside jest and made
// the whole suite fail with 'must contain at least one test'.
const assert = require('node:assert/strict');

function loadHealthOps(document) {
  const src = fs.readFileSync(path.join(__dirname, '../../renderer/health_ops.js'), 'utf8');
  const sandbox = {
    window: {},
    document,
    console,
    alert: () => {},
    confirm: () => true,
    invoke: async () => ({}),
    UI: { switchView() {} },
  };
  sandbox.window = sandbox;
  vm.runInNewContext(src, sandbox, { filename: 'health_ops.js' });
  return sandbox;
}

function fakeDoc() {
  const strip = {
    id: 'health-actions-strip',
    className: '',
    hidden: true,
    innerHTML: '',
    querySelectorAll() { return []; },
  };
  const panel = {
    querySelector: (sel) => (sel === '.system-health-grid' ? { tag: 'grid' } : null),
    insertBefore() {},
    appendChild() {},
  };
  return {
    querySelector: (sel) => (sel === '.system-health-panel' ? panel : null),
    getElementById: (id) => (id === 'health-actions-strip' ? strip : null),
    createElement: () => strip,
    _strip: strip,
  };
}

test('healthRenderActions shows maintenance rows with click labels', () => {
  const document = fakeDoc();
  const sand = loadHealthOps(document);
  sand.healthRenderActions({
    overall: 'MAINTENANCE',
    mode: 'MAINTENANCE_REQUIRED',
    actions: [
      {
        service: 'MCP_TOOL_AVAILABILITY',
        status: 'MAINTENANCE',
        detail: 'no MCP servers configured',
        label: 'Open Settings → Plugins (add/enable MCP)',
        kind: 'settings_panel',
        target: 'plugins',
      },
    ],
  });
  assert.equal(document._strip.hidden, false);
  assert.match(document._strip.innerHTML, /MCP_TOOL_AVAILABILITY/);
  assert.match(document._strip.innerHTML, /Open Settings/);
});

test('healthRenderActions hides strip when all healthy', () => {
  const document = fakeDoc();
  const sand = loadHealthOps(document);
  sand.healthRenderActions({ overall: 'HEALTHY', mode: 'FULL_AUTONOMY', actions: [] });
  assert.equal(document._strip.hidden, true);
});
