/**
 * Status-bar readout of how fast the last reply was: tokens per second and time to first token.
 * Classic script - attaches `window.LatencyStatus`, used by AgentChat in renderer.js.
 *
 * The harness attaches a `timing` object to each reply (python/inference/latency.py), taken from what the provider itself
 * reported. A reply without one clears the readout rather than leaving the previous reply's numbers standing as if they
 * were this one's. Built with textContent only: model names are data.
 */
'use strict';

function latencyDuration(ms) {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function latencyFormat(timing) {
  if (!timing || typeof timing.tokens_per_second !== 'number' || !(timing.tokens_per_second > 0)) return '';
  const parts = [`${timing.tokens_per_second.toFixed(1)} tok/s`];
  if (typeof timing.ttft_ms === 'number') parts.push(`first token ${latencyDuration(timing.ttft_ms)}`);
  return parts.join(' · ');
}

function latencyDetail(timing) {
  if (!timing) return '';
  const lines = [];
  if (timing.model) lines.push(`Model: ${timing.model}`);
  if (typeof timing.prompt_tokens === 'number') {
    const speed = typeof timing.prompt_per_second === 'number' ? ` at ${timing.prompt_per_second.toFixed(0)} tok/s` : '';
    lines.push(`Prompt: ${timing.prompt_tokens} tokens${speed}`);
  }
  if (typeof timing.generated_tokens === 'number') lines.push(`Reply: ${timing.generated_tokens} tokens`);
  if (timing.source) lines.push(`Reported by ${timing.source}`);
  return lines.join('\n') || 'Speed of the last reply';
}

function latencyShow(doc, timing) {
  const item = doc.getElementById('status-latency');
  const text = doc.getElementById('status-latency-text');
  if (!item || !text) return;
  const line = latencyFormat(timing);
  text.textContent = line;
  item.hidden = !line;
  item.title = line ? latencyDetail(timing) : '';
}

window.LatencyStatus = { format: latencyFormat, detail: latencyDetail, show: latencyShow };
