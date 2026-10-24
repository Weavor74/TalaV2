/**
 * Local voice: microphone capture, the always-visible mic indicator, sentence-by-sentence playback, and barge-in.
 * Classic script - attaches `window.VoiceUI`, used by AgentChat in renderer.js.
 *
 * The microphone is captured here, in the renderer, because the browser's own echo cancellation is what lets her be talked
 * over on speakers without her voice triggering the interruption. Audio goes to the local harness over the existing IPC and
 * websocket (`voice.audio`, 16 kHz PCM16, ~100 ms at a time) and nowhere else; nothing is recorded or stored in the page.
 * Speech comes back as `voice.audio_chunk` notifications (one sentence each) and is played gaplessly with WebAudio.
 *
 * Listening is on only while the user has switched it on, and the indicator is visible for exactly as long. Any RPC error
 * (the emergency halt refuses voice.audio) stops the microphone.
 *
 * Everything is built with textContent, never by parsing an HTML string: transcripts and server messages are data.
 */
'use strict';

const VOICE_SAMPLE_RATE = 16000;
const VOICE_BATCH_SAMPLES = 1600; // 100 ms
const VOICE_MAX_PENDING_SAMPLES = VOICE_SAMPLE_RATE * 8;

function voiceInt16FromFloat(float32) {
  const out = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i += 1) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function voiceBase64FromInt16(int16) {
  const bytes = new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

function voiceBytesFromBase64(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Collects the worklet's 128-sample blocks into ~100 ms batches of Int16. */
class VoiceBatcher {
  constructor(onBatch, size = VOICE_BATCH_SAMPLES) {
    this.onBatch = onBatch;
    this.size = size;
    this.buffer = [];
    this.count = 0;
  }

  push(float32) {
    this.buffer.push(float32);
    this.count += float32.length;
    if (this.count >= this.size) this.flush();
  }

  flush() {
    if (!this.count) return;
    const merged = new Float32Array(this.count);
    let at = 0;
    for (const block of this.buffer) { merged.set(block, at); at += block.length; }
    this.buffer = [];
    this.count = 0;
    this.onBatch(voiceInt16FromFloat(merged));
  }
}

/**
 * Sends audio to the harness one request at a time, in order. While a request is in flight, newer audio waits and goes out as
 * one larger chunk; if the harness falls more than ~8 s behind, the oldest audio is dropped rather than growing without bound.
 */
class VoiceSendLoop {
  constructor(rpc, onError) {
    this.rpc = rpc;
    this.onError = onError;
    this.pending = [];
    this.pendingSamples = 0;
    this.inFlight = false;
    this.stopped = false;
  }

  push(int16) {
    if (this.stopped) return;
    this.pending.push(int16);
    this.pendingSamples += int16.length;
    while (this.pendingSamples > VOICE_MAX_PENDING_SAMPLES && this.pending.length > 1) {
      this.pendingSamples -= this.pending.shift().length;
    }
    this._drain();
  }

  async _drain() {
    if (this.inFlight || this.stopped || !this.pending.length) return;
    this.inFlight = true;
    const merged = new Int16Array(this.pendingSamples);
    let at = 0;
    for (const part of this.pending) { merged.set(part, at); at += part.length; }
    this.pending = [];
    this.pendingSamples = 0;
    try {
      const state = await this.rpc('voice.audio', { pcm_base64: voiceBase64FromInt16(merged) });
      if (state && state.error) throw new Error(state.error.message || state.error);
    } catch (err) {
      this.stopped = true;
      this.onError(err);
    } finally {
      this.inFlight = false;
      this._drain();
    }
  }

  stop() { this.stopped = true; this.pending = []; this.pendingSamples = 0; }
}

/** Plays a turn's sentences back to back, reports when playback starts and ends, and can be cut off instantly. */
class VoicePlayback {
  constructor(getContext, rpc) {
    this.getContext = getContext;
    this.rpc = rpc;
    this.turnId = null;
    this._reset();
  }

  _reset() {
    this.sources = [];
    this.nextTime = 0;
    this.final = false;
    this.started = false;
    this.ended = false;
    this.decoding = Promise.resolve();
    this.decodes = 0;
    this.cancelled = false;
  }

  setTurn(turnId) {
    this.stop();
    this.turnId = turnId;
    this._reset();
  }

  enqueue(chunk) {
    if (!chunk || chunk.turn_id !== this.turnId || this.cancelled) return Promise.resolve();
    if (chunk.final) {
      this.final = true;
      this._maybeEnd();
      return Promise.resolve();
    }
    // decode in arrival order: decodeAudioData calls can finish out of order, which would swap two sentences
    this.decodes += 1;
    this.decoding = this.decoding.then(async () => {
      try {
        if (this.cancelled || chunk.turn_id !== this.turnId) return;
        const ctx = this.getContext();
        const bytes = voiceBytesFromBase64(chunk.audio_base64);
        const buffer = await ctx.decodeAudioData(bytes.buffer);
        if (this.cancelled || chunk.turn_id !== this.turnId) return;
        this._schedule(ctx, buffer);
      } catch (err) {
        console.warn('[VoiceUI] could not play a sentence:', err);
      } finally {
        this.decodes -= 1;
        this._maybeEnd();
      }
    });
    return this.decoding;
  }

  _schedule(ctx, buffer) {
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    const startAt = Math.max(ctx.currentTime + 0.02, this.nextTime);
    this.nextTime = startAt + buffer.duration;
    source.onended = () => {
      this.sources = this.sources.filter((s) => s !== source);
      this._maybeEnd();
    };
    this.sources.push(source);
    source.start(startAt);
    if (!this.started) {
      this.started = true;
      this.rpc('voice.playback', { state: 'started', turn_id: this.turnId }).catch(() => {});
    }
  }

  _maybeEnd() {
    if (this.final && this.started && !this.ended && this.decodes === 0 && this.sources.length === 0) {
      this.ended = true;
      this.rpc('voice.playback', { state: 'ended', turn_id: this.turnId }).catch(() => {});
    }
  }

  /** Cut her off now (barge-in). The harness already knows; nothing is reported back. */
  stop() {
    this.cancelled = true;
    for (const source of this.sources) {
      try { source.onended = null; source.stop(); } catch (_) { /* already stopped */ }
    }
    this.sources = [];
  }
}

const VOICE_STATE_TEXT = {
  waiting: (s) => `MIC LIVE — say “${s.listening_for || s.wake_phrase || 'Hey Tala'}”`,
  capturing: () => 'MIC LIVE — listening…',
  transcribing: () => 'MIC LIVE — transcribing…',
  thinking: () => 'MIC LIVE — thinking…',
  speaking: () => 'MIC LIVE — speaking (talk to interrupt)',
};

function voiceIndicatorText(state) {
  if (!state || !state.listening) return '';
  const make = VOICE_STATE_TEXT[state.state] || VOICE_STATE_TEXT.waiting;
  const text = make(state);
  return state.mode === 'dictate' ? text.replace('say “', 'dictating (').replace(/”$/, ')') : text;
}

/** Wire everything to the page. `env` carries the browser APIs so the logic stays testable. */
function voiceCreateController(env) {
  const { doc, rpc, agentChat, confirmFn } = env;
  const els = {
    listen: doc.getElementById('btn-chat-listen'),
    indicator: doc.getElementById('voice-indicator'),
    indicatorText: doc.getElementById('voice-indicator-text'),
    textarea: doc.getElementById('chat-textarea'),
  };
  let mic = null;
  let sendLoop = null;
  let batcher = null;
  let playbackCtx = null;
  let listening = false;
  let draft = '';
  const playback = new VoicePlayback(() => {
    if (!playbackCtx) playbackCtx = new env.AudioContext();
    return playbackCtx;
  }, rpc);

  function render(state) {
    const text = voiceIndicatorText(state);
    if (els.indicator) els.indicator.hidden = !text;
    if (els.indicatorText) els.indicatorText.textContent = text;
    if (els.listen) els.listen.classList.toggle('active', Boolean(state && state.listening && state.mode === 'continuous'));
  }

  async function openMic() {
    const stream = await env.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const ctx = new env.AudioContext({ sampleRate: VOICE_SAMPLE_RATE });
    await ctx.audioWorklet.addModule('voice_worklet.js');
    const source = ctx.createMediaStreamSource(stream);
    const node = new env.AudioWorkletNode(ctx, 'tala-pcm-capture');
    sendLoop = new VoiceSendLoop(rpc, (err) => fail(err));
    batcher = new VoiceBatcher((int16) => sendLoop.push(int16));
    node.port.onmessage = (e) => batcher.push(e.data);
    source.connect(node); // deliberately not connected to the speakers: no monitoring of the microphone
    if (playbackCtx && playbackCtx.state === 'suspended') playbackCtx.resume();
    mic = {
      close() {
        node.port.onmessage = null;
        node.disconnect();
        source.disconnect();
        stream.getTracks().forEach((t) => t.stop());
        ctx.close();
      },
    };
  }

  function closeMic() {
    if (sendLoop) sendLoop.stop();
    if (mic) mic.close();
    mic = null; sendLoop = null; batcher = null;
    listening = false;
  }

  function fail(err) {
    closeMic();
    playback.stop();
    render(null);
    if (agentChat && agentChat.appendMessage) agentChat.appendMessage('agent', 'Tala', `Voice stopped: ${(err && err.message) || err}`, true);
  }

  async function start(mode) {
    if (listening) return;
    const started = await rpc('voice.listen_start', { mode });
    if (started && started.error) throw new Error(started.error.message || started.error);
    listening = true;
    if (!playbackCtx) playbackCtx = new env.AudioContext();
    try {
      await openMic();
    } catch (err) {
      await rpc('voice.listen_stop', {}).catch(() => {});
      closeMic();
      throw err;
    }
    render(started);
  }

  async function stop() {
    closeMic();
    playback.stop();
    try { render(await rpc('voice.listen_stop', {})); } catch (_) { render(null); }
    render(null);
  }

  async function toggle() {
    try {
      if (listening) await stop(); else await start('continuous');
    } catch (err) { fail(err); }
  }

  async function dictate() {
    try {
      if (listening) { await stop(); return; }
      await start('dictate');
    } catch (err) { fail(err); }
  }

  function handleNotification(method, params) {
    const p = params || {};
    if (method === 'voice.state') {
      render(p);
      if (listening && !p.listening) closeMic(); // dictation finished, or the harness stopped listening
    } else if (method === 'voice.partial') {
      if (els.textarea && p.text) { draft = p.text; els.textarea.value = p.text; }
    } else if (method === 'voice.transcript') {
      if (p.dictate && els.textarea && p.text) {
        els.textarea.value = (els.textarea.value && els.textarea.value !== draft ? `${els.textarea.value} ` : '') + p.text;
        draft = '';
        els.textarea.focus();
      }
    } else if (method === 'voice.turn') {
      if (els.textarea) { els.textarea.value = ''; draft = ''; }
      playback.setTurn(p.turn_id);
      if (agentChat && agentChat.sendVoiceTurn) agentChat.sendVoiceTurn(p.text, p.turn_id);
    } else if (method === 'voice.audio_chunk') {
      playback.enqueue(p);
    } else if (method === 'voice.interrupt') {
      playback.stop();
    }
  }

  if (els.listen) els.listen.addEventListener('click', toggle);
  doc.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && listening) rpc('voice.interrupt', {}).catch(() => {});
  });

  return {
    toggle, dictate, stop, handleNotification, playback,
    isListening: () => listening,
    turnDone: (turnId, text) => rpc('voice.turn_done', { turn_id: turnId, text }).catch(() => {}),
    _test: { render, get sendLoop() { return sendLoop; } },
    confirmFn,
  };
}

/** Settings: wake word on/off and phrase, the voice, the recogniser size, and the one switch that is cloud. */
async function voiceInitSettings(doc, rpc, confirmFn) {
  const get = (id) => doc.getElementById(id);
  const wake = get('voice-wake-enabled');
  const phrase = get('voice-wake-phrase');
  const voiceName = get('voice-piper-voice');
  const whisper = get('voice-whisper-model');
  const cloud = get('voice-cloud-tts');
  const note = get('voice-settings-note');
  const say = (text) => { if (note) note.textContent = text; };
  if (!wake && !phrase && !voiceName && !whisper && !cloud) return;

  const save = async (changes) => {
    const result = await rpc('voice.set_settings', { settings: changes });
    if (result && result.error) { say(`Not saved: ${result.error.message || result.error}`); return null; }
    say(result && result.warnings && result.warnings.length ? result.warnings[0] : 'Saved.');
    return result;
  };

  try {
    const loaded = await rpc('voice.get_settings', {});
    const s = (loaded && loaded.settings) || {};
    if (wake) wake.checked = Boolean(s.wake_enabled);
    if (phrase && s.wake_phrase) phrase.value = s.wake_phrase;
    if (voiceName && s.piper_voice) voiceName.value = s.piper_voice;
    if (whisper && s.whisper_model) whisper.value = s.whisper_model;
    if (cloud) cloud.checked = Boolean(s.allow_cloud_tts);
  } catch (_) { /* leave the controls as rendered */ }

  if (wake) wake.addEventListener('change', () => save({ wake_enabled: wake.checked }));
  if (phrase) phrase.addEventListener('change', () => save({ wake_phrase: phrase.value }));
  if (voiceName) voiceName.addEventListener('change', () => save({ piper_voice: voiceName.value }));
  if (whisper) whisper.addEventListener('change', () => save({ whisper_model: whisper.value }));
  if (cloud) {
    cloud.addEventListener('change', async () => {
      if (cloud.checked && !confirmFn("Cloud speech sends everything she says to Microsoft's servers. Turn it on anyway?")) {
        cloud.checked = false;
        return;
      }
      const result = await save({ allow_cloud_tts: cloud.checked });
      if (!result) cloud.checked = !cloud.checked;
    });
  }
}

window.VoiceUI = {
  createController: voiceCreateController,
  initSettings: voiceInitSettings,
  indicatorText: voiceIndicatorText,
  Batcher: VoiceBatcher,
  SendLoop: VoiceSendLoop,
  Playback: VoicePlayback,
  int16FromFloat: voiceInt16FromFloat,
  base64FromInt16: voiceBase64FromInt16,
  bytesFromBase64: voiceBytesFromBase64,
};
