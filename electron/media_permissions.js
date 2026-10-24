/**
 * electron/media_permissions.js
 * =============================
 * The session's permission policy: everything is denied except the microphone, for the app's own window.
 *
 * main.js denied every permission request from the renderer (camera, microphone, notifications ...). That is the right
 * default, and it is also what makes a local voice assistant impossible: `getUserMedia` needs the 'media' permission.
 * The browser's echo cancellation is what lets the user talk over her on speakers without her own voice triggering the
 * interruption, which is why the microphone is captured in the renderer rather than in Python.
 *
 * The exception is as narrow as the rule it relaxes. Allowed: a 'media' request for **audio only** (never video, never
 * audio+video, never an unspecified request), from the **main window's own webContents**, on a **file://** page. Anything
 * else, including every other permission, stays denied, and a handler that throws denies.
 *
 * Lives in its own file because main.js is held to a line cap (eslint.config.mjs) and because the policy deserves a test.
 */

'use strict';

/**
 * @param {object} req
 * @param {object} req.webContents  who is asking
 * @param {string} req.permission   Electron permission name
 * @param {string[]|undefined} req.mediaTypes  for 'media': what the page wants
 * @param {string} req.url          the requesting page
 * @param {function} req.getWindow  returns the main BrowserWindow (or null)
 * @returns {boolean}
 */
function decide({ webContents, permission, mediaTypes, url, getWindow }) {
  if (permission !== 'media') return false;
  if (!Array.isArray(mediaTypes) || mediaTypes.length !== 1 || mediaTypes[0] !== 'audio') return false;
  if (typeof url !== 'string' || !url.startsWith('file://')) return false;
  const win = getWindow();
  if (!win || (typeof win.isDestroyed === 'function' && win.isDestroyed())) return false;
  return webContents === win.webContents;
}

/**
 * Replace the deny-everything handlers on a session with the narrow policy.
 * @param {object} sess  an Electron Session
 * @param {function} getWindow  returns the main BrowserWindow (or null)
 */
function installMediaPermissions(sess, getWindow) {
  sess.setPermissionRequestHandler((webContents, permission, callback, details) => {
    let allowed = false;
    try {
      allowed = decide({
        webContents, permission, mediaTypes: details && details.mediaTypes,
        url: details && details.requestingUrl, getWindow,
      });
    } catch (_) {
      allowed = false;
    }
    callback(allowed);
  });

  // getUserMedia consults the check handler too; without it a request the first handler would allow can still be refused.
  sess.setPermissionCheckHandler((webContents, permission, _origin, details) => {
    try {
      const type = details && details.mediaType;
      return decide({
        webContents, permission, mediaTypes: type ? [type] : undefined,
        url: details && details.requestingUrl, getWindow,
      });
    } catch (_) {
      return false;
    }
  });
}

module.exports = { decide, installMediaPermissions };
