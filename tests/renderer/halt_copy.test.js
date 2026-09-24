'use strict';

const fs = require('node:fs');
const path = require('node:path');
// Jest supplies the global `test`; node:assert works under jest. These files
// were written for node:test, which registers nothing inside jest and made
// the whole suite fail with 'must contain at least one test'.
const assert = require('node:assert/strict');

test('Mission Control halt controls distinguish abort, resume, and release', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../renderer/index.html'), 'utf8');
  assert.match(html, /ABORT \/ HALT/);
  assert.match(html, /RELEASE HALT/);
  assert.match(html, /mc-halt-legend/);
  assert.match(html, /does not unlock/i);
  assert.match(html, /Not the same as Resume|does NOT release/i);
  const ops = fs.readFileSync(path.join(__dirname, '../../renderer/mission_control_ops.js'), 'utf8');
  assert.match(ops, /system\.release_halt/);
  assert.doesNotMatch(ops, /release_emergency_halt/);
  assert.match(ops, /ABORT\/HALT engaged|kill switch/i);
});
