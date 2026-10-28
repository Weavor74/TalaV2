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

/**
 * Voice turns: the three numbers python/harness/voice_latency.py measures, in the same bar. They are not the model's speed (that is
 * the readout above) but how long a spoken turn took you to hear: wake word to the first sound of her answer, the end of your
 * speech to the final transcript, and the end of your speech to the first piece of her answer being ready.
 */
const VOICE_LATENCY_PARTS = [
  ['wake_to_first_word', 'wake to first word'],
  ['stt_latency', 'speech to text'],
  ['time_to_first_audio', 'first audio'],
];

function latencyVoiceFormat(ms) {
  if (!ms || typeof ms !== 'object') return '';
  const parts = [];
  for (const [key, label] of VOICE_LATENCY_PARTS) {
    const value = ms[key];
    if (typeof value === 'number' && value >= 0) parts.push(`${label} ${latencyDuration(value)}`);
  }
  return parts.length ? `voice: ${parts.join(' · ')}` : '';
}

function latencyVoiceShow(doc, ms) {
  const item = doc.getElementById('status-voice-latency');
  const text = doc.getElementById('status-voice-latency-text');
  if (!item || !text) return;
  const line = latencyVoiceFormat(ms);
  text.textContent = line;
  item.hidden = !line;
  item.title = line
    ? 'Wake to first word: the wake word is heard until her answer starts playing.\nSpeech to text: you stop speaking until the transcript is final.\nFirst audio: you stop speaking until the first piece of her answer is ready.\nLogged, as numbers only, to the voice latency log on D:.'
    : '';
}

window.LatencyStatus = {
  format: latencyFormat, detail: latencyDetail, show: latencyShow, formatVoice: latencyVoiceFormat, showVoice: latencyVoiceShow,
};
