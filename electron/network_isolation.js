/**
 * network_isolation.js — Highs H5
 *
 * networkIsolated is true only under lockdown / emergency halt / enabled
 * network guard — never merely because the local harness WebSocket is up.
 */
'use strict';

let networkIsolated = false;
let lockdownActive = false;
let haltActive = false;
let networkGuardActive = false;

/** @type {null | ((channel: string, message: any) => void)} */
let notify = null;

/** @type {null | (() => void)} */
let refreshHook = null;

function setNotify(fn) { notify = fn; }
function setRefreshHook(fn) { refreshHook = fn; }

function isNetworkIsolated() { return networkIsolated; }

function recomputeNetworkIsolated() {
  const next = Boolean(lockdownActive || haltActive || networkGuardActive);
  if (next === networkIsolated) return;
  networkIsolated = next;
  console.log(
    `[main] networkIsolated=${networkIsolated} `
    + `(lockdown=${lockdownActive}, halt=${haltActive}, netGuard=${networkGuardActive})`
  );
  if (typeof notify === 'function') {
    notify('python:notification', {
      method: 'system.isolation_changed',
      params: {
        isolated: networkIsolated,
        lockdown: lockdownActive,
        halt: haltActive,
        network_guard: networkGuardActive,
      },
    });
  }
}

function applyIsolationHints(method, result) {
  if (!result || typeof result !== 'object' || result.__rpcError) return;
  let changed = false;
  if (method === 'environment.get' || method === 'environment.set') {
    if (Object.prototype.hasOwnProperty.call(result, 'lockdown')) {
      lockdownActive = Boolean(result.lockdown);
      changed = true;
    }
  }
  if (method === 'system.release_halt') {
    haltActive = Boolean(result.halted);
    changed = true;
  }
  // Highs H5: abort/pause via task.control returns halted; learn it here.
  if (method === 'task.control' && Object.prototype.hasOwnProperty.call(result, 'halted')) {
    haltActive = Boolean(result.halted);
    changed = true;
  }
  if (method === 'harness.health' && Object.prototype.hasOwnProperty.call(result, 'halted')) {
    haltActive = Boolean(result.halted);
    changed = true;
  }
  if (method === 'guardrails.get_network' || method === 'guardrails.set_network') {
    if (Object.prototype.hasOwnProperty.call(result, 'enabled')) {
      networkGuardActive = Boolean(result.enabled);
      changed = true;
    }
  }
  if (changed) recomputeNetworkIsolated();
}

function applyIsolationNotification(msg) {
  if (!msg || typeof msg !== 'object') return;
  const method = msg.method || msg.type;
  const params = msg.params || msg;
  if (method === 'environment.changed' || method === 'environment_changed') {
    if (Object.prototype.hasOwnProperty.call(params, 'lockdown')) {
      lockdownActive = Boolean(params.lockdown);
      recomputeNetworkIsolated();
    }
  } else if (method === 'system.halt_changed' || method === 'halt_changed') {
    if (Object.prototype.hasOwnProperty.call(params, 'halted')) {
      haltActive = Boolean(params.halted);
      recomputeNetworkIsolated();
    }
  }
  // Note: task_status_update+action=abort was never broadcast; Abort sets
  // halt via task.control hints and system.halt_changed (see task_rpc).
}

function onHarnessConnected() {
  if (typeof refreshHook === 'function') refreshHook();
}

module.exports = {
  isNetworkIsolated,
  setNotify,
  setRefreshHook,
  applyIsolationHints,
  applyIsolationNotification,
  onHarnessConnected,
  recomputeNetworkIsolated,
};
