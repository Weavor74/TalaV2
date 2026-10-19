/**
 * Highs H3: renderer must not call window.tala.invoke/request with bare RPC method names.
 * Smoke (2026-09-17): gateway.status / plugins.* / mcp.* used window.tala.request and
 * hit preload fail-closed after H3 — they must go through helpers.invoke remap.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

function collectBareDottedCalls(src, method) {
  const bad = [];
  const re = new RegExp(`window\\.tala\\.${method}\\(\\s*['"]([^'"]+)['"]`, 'g');
  let m;
  while ((m = re.exec(src))) {
    const ch = m[1];
    if (ch === 'python:rpc') continue;
    if (ch.includes(':')) continue; // file:/vault:/etc allowlisted form
    if (ch.includes('.')) bad.push(ch);
  }
  return bad;
}

describe('renderer RPC invoke migration (H3)', () => {
  const rendererDir = path.join(__dirname, '../../renderer');
  const rendererSrc = fs.readFileSync(path.join(rendererDir, 'renderer.js'), 'utf8');
  const rendererBundle = [
    'renderer.js',
    'gateway_status_ops.js',
    'plugins_ops.js',
    'helpers.js',
  ].map((name) => fs.readFileSync(path.join(rendererDir, name), 'utf8')).join('\n');

  test('no window.tala.invoke("swarm.*"|"task.*"|dotted RPC) remain', () => {
    expect(collectBareDottedCalls(rendererSrc, 'invoke')).toEqual([]);
  });

  test('no window.tala.request(dotted RPC) remain (gateway/plugins/mcp)', () => {
    expect(collectBareDottedCalls(rendererSrc, 'request')).toEqual([]);
  });

  test('gateway.status / plugins.* / mcp.* go through helpers.invoke', () => {
    for (const method of [
      'gateway.status',
      'plugins.list',
      'plugins.reload',
      'plugins.validate',
      'mcp.discover',
      'mcp.connect_server',
      'mcp.list_servers',
      'mcp.remove_server',
      'mcp.list_tools',
    ]) {
      expect(rendererBundle).toMatch(new RegExp(`invoke\\(\\s*['"]${method.replace('.', '\\.')}['"]`));
      expect(rendererBundle).not.toMatch(
        new RegExp(`window\\.tala\\.(invoke|request)\\(\\s*['"]${method.replace('.', '\\.')}['"]`),
      );
    }
  });

  test('helpers.invoke remaps dotted method names to python:rpc', () => {
    const src = fs.readFileSync(
      path.join(__dirname, '../../renderer/helpers.js'),
      'utf8',
    );
    expect(src).toMatch(/python:rpc/);
    expect(src).toMatch(/_TALA_IPC_CHANNELS/);
    expect(src).toMatch(/method:\s*channel/);
  });

  test('StatusBar WS badge is driven by main harness:ws-status / harness.ws_status', () => {
    expect(rendererSrc).toMatch(/harness:ws-status/);
    expect(rendererSrc).toMatch(/harness\.ws_status/);
    expect(rendererSrc).toMatch(/window\.StatusBar\s*=\s*this/);
    expect(rendererSrc).toMatch(/applyMainWsConnected/);
    // TelemetryPoller must not own the WS badge anymore.
    const tpStart = rendererSrc.indexOf('const TelemetryPoller');
    const tpEnd = rendererSrc.indexOf('const BlockchainViewer');
    const tp = rendererSrc.slice(tpStart, tpEnd);
    expect(tp).not.toMatch(/StatusBar\.setWsStatus/);
  });
});
