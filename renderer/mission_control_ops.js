/**
 * Mission Control owner-gated ops extracted from renderer.js (size ratchet).
 * Classic script (not ESM): loaded before renderer.js so functions are globals.
 * Resume ≠ release_halt: only mcReleaseEmergencyHalt calls system.release_halt.
 */
'use strict';

/** Wire HALT / RELEASE HALT buttons. Resume remains a separate task.control path. */
function mcBindOwnerControls(mc) {
  mc._btnHalt = qs('#btn-mc-halt');
  mc._btnReleaseHalt = qs('#btn-mc-release-halt');
  if (mc._btnHalt) mc._btnHalt.addEventListener('click', () => mc.haltCurrentLoop());
  if (mc._btnReleaseHalt) {
    mc._btnReleaseHalt.addEventListener('click', () => mc.releaseEmergencyHalt());
  }
}

async function mcClearTaskQueue(mc, taskCache) {
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
    IPCLog.append('error', `Could not clear the queue: ${err.message || err}`);
  }
}

/** Abort the active task (triggers emergency halt). Does not release it. */
async function mcHaltCurrentLoop(mc) {
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
