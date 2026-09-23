/**
 * GPU-aware inference routing hints (advanced operator UX).
 * Classic script — attaches helpers used by InferenceManager in renderer.js.
 *
 * Policy: CPU embedded Qwen is the right default on CPU-only boxes.
 * When a GPU is present, do not steer chat toward CPU Qwen 0.5B.
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

function inferenceApplyGpuCopy(gpuAvailable) {
  const note = document.getElementById('inference-gpu-note');
  const copy = document.getElementById('inference-router-copy');
  const embDesc = document.getElementById('ptc-desc-embedded');
  if (gpuAvailable) {
    if (note) {
      note.hidden = false;
      note.textContent = 'GPU detected — prefer Local/Remote GPU providers for chat. The embedded Qwen 0.5B CPU engine remains last-resort fallback only; it is not the default route on GPU machines.';
    }
    if (copy) {
      copy.textContent = 'Choose the model for this chat session. GPU is available: Auto prefers Local/Remote providers; embedded CPU Qwen is fallback only.';
    }
    if (embDesc) {
      embDesc.textContent = 'Built-in Qwen 0.5B · CPU fallback only on GPU machines · Always available offline';
    }
  } else {
    if (note) {
      note.hidden = false;
      note.textContent = 'No GPU detected — embedded CPU Qwen 0.5B is the correct local default on this machine.';
    }
    if (copy) {
      copy.textContent = 'Choose the model for this chat session. No GPU detected: Auto may use the embedded CPU engine; Local/Remote still win when configured and online.';
    }
    if (embDesc) {
      embDesc.textContent = 'Built-in 0.5B–1B model · Always available · No internet required · CPU inference';
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
    IPCLog.append('info', 'GPU present: cleared session pin to embedded CPU Qwen (use Local/Remote or Auto).');
  }
}

async function inferenceProbeGpuAndApply(mgr) {
  if (!mgr) return;
  mgr._gpuAvailable = await inferenceProbeGpuAvailable();
  inferenceApplyGpuCopy(mgr._gpuAvailable);
  inferenceMaybeClearCpuEmbeddedSessionPin(mgr);
}

function inferenceEmbeddedMeta(mgr, provider) {
  const gpu = mgr && mgr._gpuAvailable === true;
  const deviceBit = gpu ? 'CPU fallback (GPU present)' : 'CPU inference';
  if (provider.status === 'downloading') {
    const pct = provider.progress !== undefined ? `${provider.progress}%` : '0%';
    return `Qwen/Qwen2.5-0.5B-Instruct · ${deviceBit} · Downloading: ${pct}`;
  }
  return `Qwen/Qwen2.5-0.5B-Instruct · ${deviceBit} · ~0.5B–1B params · ${gpu ? 'Last-resort fallback' : 'Always available'}`;
}

window.inferenceProbeGpuAndApply = inferenceProbeGpuAndApply;
window.inferenceEmbeddedMeta = inferenceEmbeddedMeta;
