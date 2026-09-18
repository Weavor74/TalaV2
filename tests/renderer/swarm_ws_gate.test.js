/**
 * Swarm dashboard / Agent Builder start+destructive controls must reuse
 * Mission Control's mcRequireWs / window.StatusBar WS gate (smoke nit).
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../..');

function loadOps(windowObj, qsMap = {}) {
  const src = fs.readFileSync(
    path.join(root, 'renderer/mission_control_ops.js'),
    'utf8',
  );
  const sandbox = {
    window: windowObj,
    StatusBar: undefined,
    console,
    alert: jest.fn(),
    confirm: jest.fn(),
    qs: (sel) => qsMap[sel] || null,
    IPCLog: { append: jest.fn() },
    invoke: jest.fn(),
  };
  vm.runInNewContext(
    src
      + '\n;this.mcWsConnected = mcWsConnected;'
      + '\n;this.mcRequireWs = mcRequireWs;'
      + '\n;this.swarmSyncWsControls = swarmSyncWsControls;',
    sandbox,
    { filename: 'mission_control_ops.js' },
  );
  return sandbox;
}

describe('Swarm WS gate (mcRequireWs pattern)', () => {
  test('renderer gates fleet pause/resume/terminate and topology start/save/delete', () => {
    const rendererSrc = fs.readFileSync(path.join(root, 'renderer/renderer.js'), 'utf8');
    for (const needle of [
      "mcRequireWs('pause this swarm task')",
      "mcRequireWs('resume this swarm task')",
      "mcRequireWs('terminate this swarm task')",
      "mcRequireWs('start this swarm')",
      "mcRequireWs('save this swarm')",
      "mcRequireWs('delete this swarm')",
      'swarmSyncWsControls()',
    ]) {
      expect(rendererSrc).toContain(needle);
    }
  });

  test('StatusBar.setWsStatus syncs Swarm controls', () => {
    const rendererSrc = fs.readFileSync(path.join(root, 'renderer/renderer.js'), 'utf8');
    const start = rendererSrc.indexOf('setWsStatus(status)');
    const chunk = rendererSrc.slice(start, start + 1600);
    expect(chunk).toMatch(/swarmSyncWsControls/);
  });

  test('HTML banners exist for Swarm dashboard and Agent Builder topologies', () => {
    const html = fs.readFileSync(path.join(root, 'renderer/index.html'), 'utf8');
    expect(html).toMatch(/id="swarm-ws-disconnected"/);
    expect(html).toMatch(/id="agent-builder-swarm-ws-disconnected"/);
  });

  test('swarmSyncWsControls disables start/delete/save when WS down', () => {
    const startBtn = { disabled: false, title: 'Run Swarm', getAttribute: jest.fn(() => 'Run Swarm'), setAttribute: jest.fn() };
    const deleteBtn = { disabled: false, title: 'Delete', getAttribute: jest.fn(() => 'Delete'), setAttribute: jest.fn() };
    const saveBtn = { disabled: false, title: 'Save', getAttribute: jest.fn(() => 'Save'), setAttribute: jest.fn() };
    const refreshBtn = { disabled: false, title: 'Refresh', getAttribute: jest.fn(() => 'Refresh'), setAttribute: jest.fn() };
    const bannerA = { hidden: true };
    const bannerB = { hidden: true };
    const windowObj = {
      StatusBar: { isWsConnected: () => false },
      SwarmDashboardManager: { _wsConnected: true },
    };
    const sand = loadOps(windowObj, {
      '#btn-topology-start': startBtn,
      '#btn-topology-delete': deleteBtn,
      '#btn-topo-save': saveBtn,
      '#btn-refresh-swarm': refreshBtn,
      '#swarm-ws-disconnected': bannerA,
      '#agent-builder-swarm-ws-disconnected': bannerB,
    });
    sand.swarmSyncWsControls();
    expect(startBtn.disabled).toBe(true);
    expect(deleteBtn.disabled).toBe(true);
    expect(saveBtn.disabled).toBe(true);
    expect(refreshBtn.disabled).toBe(true);
    expect(bannerA.hidden).toBe(false);
    expect(bannerB.hidden).toBe(false);
    expect(windowObj.SwarmDashboardManager._wsConnected).toBe(false);

    windowObj.StatusBar.isWsConnected = () => true;
    sand.swarmSyncWsControls();
    expect(startBtn.disabled).toBe(false);
    expect(bannerA.hidden).toBe(true);
    expect(windowObj.SwarmDashboardManager._wsConnected).toBe(true);
  });

  test('mcRequireWs blocks when disconnected', () => {
    const sand = loadOps({ StatusBar: { isWsConnected: () => false } });
    expect(sand.mcRequireWs('start this swarm')).toBe(false);
    expect(sand.alert).toHaveBeenCalled();
  });
});
