/**
 * Highs H5: haltActive must learn Abort (task.control + halt_changed + health).
 */
'use strict';

describe('network_isolation (H5)', () => {
  let iso;

  beforeEach(() => {
    jest.resetModules();
    iso = require('../../electron/network_isolation');
  });

  test('task.control abort result sets networkIsolated via halt', () => {
    expect(iso.isNetworkIsolated()).toBe(false);
    iso.applyIsolationHints('task.control', {
      success: true,
      action: 'abort',
      task_id: 't1',
      halted: true,
    });
    expect(iso.isNetworkIsolated()).toBe(true);
  });

  test('system.halt_changed notification sets halt isolation', () => {
    iso.applyIsolationNotification({
      method: 'system.halt_changed',
      params: { halted: true },
    });
    expect(iso.isNetworkIsolated()).toBe(true);
    iso.applyIsolationNotification({
      method: 'system.halt_changed',
      params: { halted: false },
    });
    expect(iso.isNetworkIsolated()).toBe(false);
  });

  test('harness.health halted field drives haltActive', () => {
    iso.applyIsolationHints('harness.health', {
      status: 'ok',
      halted: true,
    });
    expect(iso.isNetworkIsolated()).toBe(true);
  });

  test('dead task_status_update+action=abort is ignored (never broadcast)', () => {
    iso.applyIsolationNotification({
      method: 'task_status_update',
      params: { action: 'abort', task_id: 't1' },
    });
    expect(iso.isNetworkIsolated()).toBe(false);
  });
});
