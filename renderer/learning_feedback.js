/**
 * Node-learning controls: thumbs up / thumbs down / "that's wrong" under a reply, and the settings toggle + reset.
 * Classic script - attaches `window.LearningFeedback`, used by AgentChat in renderer.js.
 *
 * A reply carries `injected_node_ids`, the nodes it was built from. Feedback goes to `learning.feedback` and moves the
 * confidence of exactly those nodes (python/harness/learning_rpc.py); nothing here edits a node. Everything is built
 * with textContent, never by parsing an HTML string: node ids and server messages are data.
 */
'use strict';

const LEARNING_KINDS = [
  { kind: 'up', label: '\u{1F44D}', title: 'Helpful: raise confidence in the memories behind this reply' },
  { kind: 'down', label: '\u{1F44E}', title: 'Not helpful: lower confidence in the memories behind this reply' },
  { kind: 'wrong', label: "That's wrong", title: 'Incorrect: lower confidence the most' },
];

function learningOutcomeText(result) {
  if (!result || result.error) {
    const why = result && result.error ? (result.error.message || result.error) : 'no response';
    return `Feedback not recorded: ${why}`;
  }
  if (result.skipped) return `Not recorded: ${result.skipped}.`;
  const changes = Array.isArray(result.changes) ? result.changes : [];
  if (!changes.length) return 'Noted. Nothing needed to change.';
  const c = changes[0];
  const more = changes.length > 1 ? ` (+${changes.length - 1} more)` : '';
  return `Noted: confidence ${Number(c.before).toFixed(2)} → ${Number(c.after).toFixed(2)}${more}`;
}

/** The feedback row for one reply, or null when the reply used no nodes (nothing to give feedback on). */
function learningFeedbackBar(doc, nodeIds, rpc) {
  if (!Array.isArray(nodeIds) || !nodeIds.length) return null;
  const bar = doc.createElement('div');
  bar.className = 'learning-feedback';
  const status = doc.createElement('span');
  status.className = 'learning-feedback-status';
  const buttons = [];
  for (const { kind, label, title } of LEARNING_KINDS) {
    const button = doc.createElement('button');
    button.className = 'btn btn-ghost learning-feedback-btn';
    button.dataset.kind = kind;
    button.title = title;
    button.textContent = label;
    button.addEventListener('click', async () => {
      buttons.forEach((b) => { b.disabled = true; });
      let result;
      try {
        result = await rpc('learning.feedback', { node_ids: nodeIds.slice(0, 8), kind });
      } catch (err) {
        result = { error: String((err && err.message) || err) };
      }
      status.textContent = learningOutcomeText(result);
    });
    buttons.push(button);
    bar.appendChild(button);
  }
  bar.appendChild(status);
  return bar;
}

/** Wire the settings toggle and the admin reset. Both controls are optional so a build without them still loads. */
async function learningInitSettings(doc, rpc, confirmFn) {
  const toggle = doc.getElementById('settings-learning-toggle');
  const reset = doc.getElementById('btn-settings-learning-reset');
  const note = doc.getElementById('settings-learning-note');
  const say = (text) => { if (note) note.textContent = text; };
  if (toggle) {
    try {
      const status = await rpc('learning.status', {});
      if (status && typeof status.enabled === 'boolean') toggle.checked = status.enabled;
    } catch (_) { /* leave the control as rendered */ }
    toggle.addEventListener('change', async () => {
      const result = await rpc('learning.set_enabled', { enabled: toggle.checked });
      if (result && typeof result.enabled === 'boolean') toggle.checked = result.enabled;
      say(result && result.error ? 'Could not change the setting.' : (toggle.checked ? 'Learning is on.' : 'Learning is off.'));
    });
  }
  if (reset) {
    reset.addEventListener('click', async () => {
      if (!confirmFn('Reset every learned node weight to its default? This discards what Tala has learned from use and feedback.')) return;
      const result = await rpc('learning.reset_weights', { confirm: true });
      say(result && result.error
        ? `Reset refused: ${result.error.message || result.error}`
        : `Reset ${result.nodes_reset || 0} node(s).`);
    });
  }
}

window.LearningFeedback = { bar: learningFeedbackBar, initSettings: learningInitSettings, outcomeText: learningOutcomeText };
