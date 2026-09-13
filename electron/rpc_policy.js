/**
 * electron/rpc_policy.js
 * ======================
 * How long a python:rpc call may run before main.js gives up on it. Moved
 * out of main.js, which the JS size ratchet (eslint.config.mjs) pins at its
 * current size — and because the method → budget table is policy, not
 * plumbing: the update that added desktop.run to the table pushed main.js
 * one line over its cap, which is exactly the growth-by-drift the ratchet
 * exists to catch. The next long-running method is one line here, not one
 * line in the god file.
 */

//: Methods allowed the 30-minute budget; everything else gets 5 minutes.
const LONG_RUNNING_METHODS = new Set([
  'inference.generate',
  'desktop.run',
  'workflow.execute',
  'task.submit',
  'task.list',
  'task.get_telemetry',
  'research.crawl_and_summarize',
  'self_improvement.execute_goal',
  'self_improvement.execute_plan',
  'code.generate',
  'code.repair'
]);

/**
 * The timeout budget for one python:rpc call.
 * @param {string} method
 * @param {*} customTimeout caller override (timeout_ms / timeoutMs / _timeoutMs)
 * @returns {number} milliseconds
 */
function rpcTimeoutMs(method, customTimeout) {
  if (customTimeout) return Number(customTimeout);
  return LONG_RUNNING_METHODS.has(method) ? 1_800_000 : 300_000;
}

module.exports = { LONG_RUNNING_METHODS, rpcTimeoutMs };
