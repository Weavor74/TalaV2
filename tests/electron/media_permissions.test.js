'use strict';

/**
 * electron/media_permissions.js — the renderer may use the microphone, and only the microphone.
 *
 * main.js used to deny every permission request, which is the right default and also what made a local voice assistant
 * impossible: getUserMedia needs 'media'. The exception is as narrow as the rule it relaxes: audio only (never video or
 * screen), only the app's own window, only a file:// page. Everything else stays denied.
 */

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const { decide, installMediaPermissions } = require('../../electron/media_permissions');

const ROOT = path.join(__dirname, '../..');

function windowWith(webContents) {
  return { webContents, isDestroyed: () => false };
}

const wc = { id: 1 };
const other = { id: 2 };
const getWindow = () => windowWith(wc);

describe('decide', () => {
  test('allows audio capture for the app window from a file page', () => {
    assert.equal(decide({ webContents: wc, permission: 'media', mediaTypes: ['audio'], url: 'file:///app/index.html', getWindow }), true);
  });

  test('denies video, and audio together with video', () => {
    assert.equal(decide({ webContents: wc, permission: 'media', mediaTypes: ['video'], url: 'file:///a', getWindow }), false);
    assert.equal(decide({ webContents: wc, permission: 'media', mediaTypes: ['audio', 'video'], url: 'file:///a', getWindow }), false);
  });

  test('denies a media request that does not say what it wants', () => {
    assert.equal(decide({ webContents: wc, permission: 'media', mediaTypes: [], url: 'file:///a', getWindow }), false);
    assert.equal(decide({ webContents: wc, permission: 'media', mediaTypes: undefined, url: 'file:///a', getWindow }), false);
  });

  test.each(['geolocation', 'notifications', 'display-capture', 'clipboard-read', 'openExternal', 'midi', 'camera'])(
    'denies %s', (permission) => {
      assert.equal(decide({ webContents: wc, permission, mediaTypes: ['audio'], url: 'file:///a', getWindow }), false);
    });

  test('denies any window that is not the main window', () => {
    assert.equal(decide({ webContents: other, permission: 'media', mediaTypes: ['audio'], url: 'file:///a', getWindow }), false);
  });

  test('denies a remote page, even inside the main window', () => {
    assert.equal(decide({ webContents: wc, permission: 'media', mediaTypes: ['audio'], url: 'https://evil.example/', getWindow }), false);
    assert.equal(decide({ webContents: wc, permission: 'media', mediaTypes: ['audio'], url: 'http://localhost:3000/', getWindow }), false);
  });

  test('denies when there is no main window or it is gone', () => {
    assert.equal(decide({ webContents: wc, permission: 'media', mediaTypes: ['audio'], url: 'file:///a', getWindow: () => null }), false);
    assert.equal(decide({ webContents: wc, permission: 'media', mediaTypes: ['audio'], url: 'file:///a',
      getWindow: () => ({ webContents: wc, isDestroyed: () => true }) }), false);
  });
});

describe('installMediaPermissions', () => {
  function fakeSession() {
    const s = {};
    s.setPermissionRequestHandler = (fn) => { s.request = fn; };
    s.setPermissionCheckHandler = (fn) => { s.check = fn; };
    return s;
  }

  test('installs both the request handler and the check handler', () => {
    const s = fakeSession();
    installMediaPermissions(s, getWindow);
    assert.equal(typeof s.request, 'function');
    assert.equal(typeof s.check, 'function');
  });

  test('the request handler answers through its callback', () => {
    const s = fakeSession();
    installMediaPermissions(s, getWindow);
    let answer;
    s.request(wc, 'media', (v) => { answer = v; }, { mediaTypes: ['audio'], requestingUrl: 'file:///app/index.html' });
    assert.equal(answer, true);
    s.request(wc, 'media', (v) => { answer = v; }, { mediaTypes: ['video'], requestingUrl: 'file:///app/index.html' });
    assert.equal(answer, false);
    s.request(wc, 'geolocation', (v) => { answer = v; }, { requestingUrl: 'file:///app/index.html' });
    assert.equal(answer, false);
  });

  test('the check handler allows audio only', () => {
    const s = fakeSession();
    installMediaPermissions(s, getWindow);
    assert.equal(s.check(wc, 'media', 'file://', { mediaType: 'audio', requestingUrl: 'file:///app/index.html' }), true);
    assert.equal(s.check(wc, 'media', 'file://', { mediaType: 'video', requestingUrl: 'file:///app/index.html' }), false);
    assert.equal(s.check(wc, 'media', 'file://', { mediaType: 'unknown', requestingUrl: 'file:///app/index.html' }), false);
    assert.equal(s.check(wc, 'notifications', 'file://', { requestingUrl: 'file:///app/index.html' }), false);
  });

  test('a handler that throws denies rather than allows', () => {
    const s = fakeSession();
    installMediaPermissions(s, () => { throw new Error('boom'); });
    let answer;
    s.request(wc, 'media', (v) => { answer = v; }, { mediaTypes: ['audio'], requestingUrl: 'file:///a' });
    assert.equal(answer, false);
  });
});

describe('main.js', () => {
  const main = fs.readFileSync(path.join(ROOT, 'electron/main.js'), 'utf8');

  test('installs the narrow policy instead of denying everything', () => {
    assert.match(main, /installMediaPermissions\(session\.defaultSession/);
    assert.doesNotMatch(main, /setPermissionRequestHandler\(\(_webContents, _permission, callback\) => \{\s*\/\/ Deny all/);
  });
});
