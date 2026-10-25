/**
 * LoRA status note for the engine panel.
 * Classic script — attaches a helper used by InferenceManager in renderer.js.
 *
 * Every engine reports `lora: { implemented, note }` (python/inference/engine_manager.py, from
 * python/inference/lora_trainer.py engine_status). While nothing could train or load an adapter the panel said so; now that a
 * persona adapter can be trained and served, it shows the backend's own one-line status. The text is the backend's, so what
 * the panel says is whatever is true: which adapter is active, whether scheduled training is on. An engine that reports
 * nothing gets no claim either way.
 */
'use strict';

const LORA_FALLBACK_NOTE = 'LoRA is not implemented: nothing trains an adapter and no engine loads one.';

function loraEscape(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function loraStatusNote(engine) {
  const lora = engine && engine.lora;
  if (!lora || typeof lora.implemented !== 'boolean') return '';
  const style = 'font-size: 12px; color: var(--text-2);';
  if (lora.implemented) {
    return `<div class="lora-status-note" style="${style}">LoRA: ${loraEscape(lora.note || 'available')}</div>`;
  }
  const note = loraEscape(lora.note || LORA_FALLBACK_NOTE);
  return `<div class="lora-status-note" style="${style}">LoRA: not implemented. ${note}</div>`;
}

window.loraStatusNote = loraStatusNote;
