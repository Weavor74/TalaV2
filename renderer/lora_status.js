/**
 * LoRA status note for the engine panel.
 * Classic script — attaches a helper used by InferenceManager in renderer.js.
 *
 * Every engine reports `lora: { implemented, note }` (python/inference/engine_manager.py,
 * from lora_trainer.IMPLEMENTED). Nothing trains an adapter and no engine loads
 * one, so the panel says so instead of leaving adapters to look like a feature.
 * An engine that reports nothing gets no claim either way.
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
  if (!lora || lora.implemented !== false) return '';
  const note = loraEscape(lora.note || LORA_FALLBACK_NOTE);
  return `<div class="lora-status-note" style="font-size: 12px; color: var(--text-2);">LoRA: not implemented. ${note}</div>`;
}

window.loraStatusNote = loraStatusNote;
