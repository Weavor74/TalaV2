/**
 * Electron owner-secret injection for release_halt / task.clear (UI-audit C2/C5).
 */
'use strict';

const {
  OWNER_SECRET_METHODS,
  injectOwnerSecret,
} = require('../../electron/harness_admin.js');

describe('injectOwnerSecret', () => {
  const prev = process.env.TALA_HARNESS_ADMIN_SECRET;
  beforeAll(() => {
    process.env.TALA_HARNESS_ADMIN_SECRET = 'unit-test-harness-secret';
  });
  afterAll(() => {
    if (prev === undefined) delete process.env.TALA_HARNESS_ADMIN_SECRET;
    else process.env.TALA_HARNESS_ADMIN_SECRET = prev;
  });

  test('injects for release_halt, trigger_update, and task.clear', () => {
    for (const method of ['system.release_halt', 'system.trigger_update', 'task.clear']) {
      expect(OWNER_SECRET_METHODS.has(method)).toBe(true);
      const out = injectOwnerSecret(method, {});
      expect(out.harness_secret).toBe('unit-test-harness-secret');
    }
  });

  test('does not inject for Resume / task.control', () => {
    const out = injectOwnerSecret('task.control', { action: 'resume', task_id: 't1' });
    expect(out.harness_secret).toBeUndefined();
    expect(out.action).toBe('resume');
  });

  test('main.js wires injectOwnerSecret into python:rpc', () => {
    const fs = require('node:fs');
    const src = fs.readFileSync(require('node:path').join(__dirname, '../../electron/main.js'), 'utf8');
    expect(src).toMatch(/params = injectOwnerSecret\(method, params\)/);
    expect(src).toMatch(/require\('\.\/harness_admin'\)/);
  });
});
