/**
 * Mission Control owner-gated ops extracted from renderer.js (size ratchet).
 * Classic script (not ESM): loaded before renderer.js so functions are globals.
 * Resume ≠ release_halt: only mcReleaseEmergencyHalt calls system.release_halt.
 *
 * Destructive controls must stay disabled when the harness WS is down (H4):
 * Clear Queue / Abort / Resume / RELEASE HALT / task delete need a live relay.
 */
'use strict';

/** True when StatusBar last reported WS as connected. */
function mcWsDisabledAttr() {
  return mcWsConnected() ? '' : ' disabled title="WebSocket disconnected"';
}

function mcWsConnected() {
  // StatusBar lives in the renderer ES module — classic scripts must use
  // window.StatusBar (exposed from StatusBar.init), not a bare StatusBar bind.
  const bar = (typeof window !== 'undefined' && window.StatusBar)
    || (typeof StatusBar !== 'undefined' ? StatusBar : null);
  if (bar && typeof bar.isWsConnected === 'function') {
    return bar.isWsConnected();
  }
  return false;
}

function mcRequireWs(actionLabel) {
  if (mcWsConnected()) return true;
  const msg = `Cannot ${actionLabel}: WebSocket is disconnected. Reconnect to the harness first.`;
  try { alert(msg); } catch (_) { /* headless */ }
  if (typeof IPCLog !== 'undefined' && IPCLog.append) {
    IPCLog.append('error', msg);
  }
  return false;
}

/** Enable/disable MC destructive controls from live WS state. */
function mcSyncDestructiveControls(mc) {
  const connected = mcWsConnected();
  const targets = [
    mc && mc._btnHalt,
    mc && mc._btnReleaseHalt,
    mc && mc._btnTaskClear,
  ];
  for (const el of targets) {
    if (!el) continue;
    el.disabled = !connected;
    el.title = connected
      ? (el.getAttribute('data-title-connected') || el.title || '')
      : 'WebSocket disconnected — reconnect before using this control';
  }
  // Per-row Abort / Resume / Delete are re-rendered in loadTasks; stamp a
  // data attribute the renderer reads when building row HTML.
  if (mc) mc._wsConnected = connected;
  const banner = (typeof qs === 'function') ? qs('#mc-ws-disconnected') : null;
  if (banner) {
    banner.hidden = connected;
  }
}

/** Wire HALT / RELEASE HALT buttons. Resume remains a separate task.control path. */
function mcBindOwnerControls(mc) {
  mc._btnHalt = qs('#btn-mc-halt');
  mc._btnReleaseHalt = qs('#btn-mc-release-halt');
  if (mc._btnHalt) {
    mc._btnHalt.setAttribute('data-title-connected', mc._btnHalt.title || 'Halt current loop');
    mc._btnHalt.addEventListener('click', () => mc.haltCurrentLoop());
  }
  if (mc._btnReleaseHalt) {
    mc._btnReleaseHalt.setAttribute(
      'data-title-connected',
      mc._btnReleaseHalt.title || 'Release emergency halt (owner secret; not Resume)',
    );
    mc._btnReleaseHalt.addEventListener('click', () => mc.releaseEmergencyHalt());
  }
  mcSyncDestructiveControls(mc);
}

async function mcClearTaskQueue(mc, taskCache) {
  if (!mcRequireWs('clear the task queue')) return;
  const count = (taskCache || []).length;
  if (!confirm(`Clear the entire task queue? This deletes all ${count} task(s) and their telemetry, and cannot be undone.`)) {
    return;
  }
  try {
    await invoke('python:rpc', {
      method: 'task.clear',
      params: { confirm: 'CLEAR_QUEUE' },
    });
    IPCLog.append('info', 'Task queue cleared.');
    await mc.loadTasks();
  } catch (err) {
    const msg = `Could not clear the queue: ${err.message || err}`;
    IPCLog.append('error', msg);
    alert(msg);
  }
}

/** Abort the active task (triggers emergency halt). Does not release it. */
async function mcHaltCurrentLoop(mc) {
  if (!mcRequireWs('halt the task loop')) return;
  try {
    const result = await invoke('python:rpc', { method: 'task.list', params: {} });
    if (result && result.tasks && result.tasks.length > 0) {
      const active = result.tasks[0];
      const haltRes = await invoke('python:rpc', {
        method: 'task.control',
        params: { task_id: active.id, action: 'abort' },
      });
      if (haltRes && haltRes.success) {
        alert('Task queue loop halted and emergency lockdown enabled.');
        await mc.refreshAll();
      } else {
        alert('Failed to halt task.');
      }
    } else {
      alert('No active task queue running.');
    }
  } catch (err) {
    alert(`Halt loop error: ${err.message}`);
  }
}

/**
 * Explicit owner unlock. Electron injects harness_secret for release_halt;
 * Resume must never call this.
 */
async function mcReleaseEmergencyHalt(mc) {
  if (!mcRequireWs('release the emergency halt')) return;
  if (!confirm(
    'Release the emergency halt? Tool execution and destructive RPCs will be allowed again. '
    + 'This is not the same as Resume — only system.release_halt unlocks the kill switch.',
  )) {
    return;
  }
  try {
    const res = await invoke('python:rpc', {
      method: 'system.release_halt',
      params: {},
    });
    if (res && (res.success || res.status === 'released' || res.halted === false)) {
      alert('Emergency halt released.');
      await mc.refreshAll();
    } else if (res && res.__rpcError) {
      alert(`Release halt failed: ${res.error || 'unknown error'}`);
    } else {
      alert('Release halt returned an unexpected result.');
      await mc.refreshAll();
    }
  } catch (err) {
    alert(`Release halt error: ${err.message || err}`);
  }
}
