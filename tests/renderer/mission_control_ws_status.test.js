/**
 * Mission Control must read WS connectedness from window.StatusBar
 * (ES-module StatusBar is invisible to classic mission_control_ops.js).
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadOps(windowObj) {
  const src = fs.readFileSync(
    path.join(__dirname, '../../renderer/mission_control_ops.js'),
    'utf8',
  );
  const sandbox = {
    window: windowObj,
    StatusBar: undefined, // classic script must NOT rely on bare StatusBar
    console,
    alert: jest.fn(),
    confirm: jest.fn(),
    qs: () => null,
    IPCLog: { append: jest.fn() },
    invoke: jest.fn(),
  };
  vm.runInNewContext(src + '\n;this.mcWsConnected = mcWsConnected; this.mcSyncDestructiveControls = mcSyncDestructiveControls;', sandbox, {
    filename: 'mission_control_ops.js',
  });
  return sandbox;
}

describe('mission_control_ops WS source of truth', () => {
  test('mcWsConnected reads window.StatusBar.isWsConnected', () => {
    const windowObj = {
      StatusBar: { isWsConnected: () => true },
    };
    const sand = loadOps(windowObj);
    expect(sand.mcWsConnected()).toBe(true);
    windowObj.StatusBar.isWsConnected = () => false;
    expect(sand.mcWsConnected()).toBe(false);
  });

  test('mcWsConnected is false when window.StatusBar is missing', () => {
    const sand = loadOps({});
    expect(sand.mcWsConnected()).toBe(false);
  });

  test('main.js exposes harness.ws_status and pushes harness:ws-status', () => {
    const main = fs.readFileSync(
      path.join(__dirname, '../../electron/main.js'),
      'utf8',
    );
    expect(main).toMatch(/harness\.ws_status/);
    expect(main).toMatch(/harness:ws-status/);
    expect(main).toMatch(/function notifyWsStatus/);
  });
});
