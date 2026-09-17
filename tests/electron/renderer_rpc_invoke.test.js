/**
 * Highs H3: renderer must not call window.tala.invoke with bare RPC method names.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

describe('renderer RPC invoke migration (H3)', () => {
  test('no window.tala.invoke("swarm.*"|"task.*"|dotted RPC) remain', () => {
    const src = fs.readFileSync(
      path.join(__dirname, '../../renderer/renderer.js'),
      'utf8',
    );
    const bad = [];
    const re = /window\.tala\.invoke\(\s*['"]([^'"]+)['"]/g;
    let m;
    while ((m = re.exec(src))) {
      const ch = m[1];
      if (ch === 'python:rpc') continue;
      if (ch.includes(':')) continue; // file:/vault:/etc allowlisted form
      if (ch.includes('.')) bad.push(ch);
    }
    expect(bad).toEqual([]);
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
});
