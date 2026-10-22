/**
 * GPU-aware inference routing hints (advanced operator UX).
 * Classic script — attaches helpers used by InferenceManager in renderer.js.
 *
 * Policy: the CPU embedded engine is the right default on CPU-only boxes.
 * When a GPU is present, do not steer chat toward the CPU embedded engine.
 */
'use strict';

const EMBEDDED_PROVIDER_ID = 'embedded-builtin';
const CHAT_PROVIDER_KEY = 'tala.chat.provider_id';

async function inferenceProbeGpuAvailable() {
  try {
    const info = await invoke('python:rpc', { method: 'system.info', params: {} });
    const gpu = (info && info.gpu) || (info && info.result && info.result.gpu) || {};
    return Boolean(gpu.available) || (Array.isArray(gpu.devices) && gpu.devices.length > 0);
  } catch (err) {
    return false;
  }
}

function inferenceApplyGpuCopy(gpuAvailable, mgr) {
  const note = document.getElementById('inference-gpu-note');
  const copy = document.getElementById('inference-router-copy');
  const embDesc = document.getElementById('ptc-desc-embedded');
  // Read the real model off the provider row rather than hardcoding a name
  // here -- this string went stale silently once already (0.5B -> 1.5B)
  // because nothing but a full source edit could ever update it.
  const emb = mgr && Array.isArray(mgr._providers)
    ? mgr._providers.find((p) => p.id === EMBEDDED_PROVIDER_ID) : null;
  const modelLabel = (emb && emb.config && emb.config.model) || 'the embedded model';
  if (gpuAvailable) {
    if (note) {
      note.hidden = false;
      note.textContent = `GPU detected — prefer Local/Remote GPU providers for chat. ${modelLabel} (CPU) remains last-resort fallback only; it is not the default route on GPU machines.`;
    }
    if (copy) {
      copy.textContent = `Choose the model for this chat session. GPU is available: Auto prefers Local/Remote providers; ${modelLabel} is fallback only.`;
    }
    if (embDesc) {
      embDesc.textContent = `${modelLabel} · CPU fallback only on GPU machines · Always available offline`;
    }
  } else {
    if (note) {
      note.hidden = false;
      note.textContent = `No GPU detected — ${modelLabel} (CPU) is the correct local default on this machine.`;
    }
    if (copy) {
      copy.textContent = 'Choose the model for this chat session. No GPU detected: Auto may use the embedded CPU engine; Local/Remote still win when configured and online.';
    }
    if (embDesc) {
      embDesc.textContent = `${modelLabel} · Always available · No internet required · CPU inference`;
    }
  }
}

function inferenceMaybeClearCpuEmbeddedSessionPin(mgr) {
  if (!mgr || !mgr._gpuAvailable) return;
  const pinned = localStorage.getItem(CHAT_PROVIDER_KEY) || '';
  if (pinned !== EMBEDDED_PROVIDER_ID) return;
  const forced = typeof mgr.activeProvider === 'function' ? mgr.activeProvider() : null;
  if (forced && forced.id === EMBEDDED_PROVIDER_ID) return;
  localStorage.removeItem(CHAT_PROVIDER_KEY);
  if (typeof AgentChat !== 'undefined') AgentChat._providerId = '';
  if (mgr._modelPicker) mgr._modelPicker.value = '';
  if (typeof IPCLog !== 'undefined' && IPCLog.append) {
    IPCLog.append('info', 'GPU present: cleared session pin to embedded engine (use Local/Remote or Auto).');
  }
}

async function inferenceProbeGpuAndApply(mgr) {
  if (!mgr) return;
  mgr._gpuAvailable = await inferenceProbeGpuAvailable();
  inferenceApplyGpuCopy(mgr._gpuAvailable, mgr);
  inferenceMaybeClearCpuEmbeddedSessionPin(mgr);
}

function inferenceEmbeddedMeta(mgr, provider) {
  const gpu = mgr && mgr._gpuAvailable === true;
  const deviceBit = gpu ? 'CPU fallback (GPU present)' : 'CPU inference';
  const modelLabel = (provider.config && provider.config.model) || 'embedded model';
  if (provider.status === 'downloading') {
    const pct = provider.progress !== undefined ? `${provider.progress}%` : '0%';
    return `${modelLabel} · ${deviceBit} · Downloading: ${pct}`;
  }
  return `${modelLabel} · ${deviceBit} · ${gpu ? 'Last-resort fallback' : 'Always available'}`;
}

window.inferenceProbeGpuAndApply = inferenceProbeGpuAndApply;
window.inferenceEmbeddedMeta = inferenceEmbeddedMeta;
