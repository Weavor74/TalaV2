'use strict';

/**
 * renderer/voice_ui.js — microphone capture, the mic indicator, gapless sentence playback, barge-in, voice settings.
 *
 * Browser audio is faked, so what is under test is the logic: audio is sent in order and never piles up without bound; a
 * sentence that decodes late cannot play before the one ahead of it; barge-in cuts playback off at once and later sentences
 * of the cancelled turn are ignored; the indicator is visible exactly while the microphone is on; any error from the harness
 * (the emergency halt) turns the microphone off; and cloud speech needs an explicit confirmation.
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const ROOT = path.join(__dirname, '../..');
const SRC = fs.readFileSync(path.join(ROOT, 'renderer/voice_ui.js'), 'utf8');

function load() {
  const sandbox = { window: {}, console, atob, btoa, Math, Int16Array, Float32Array, Uint8Array, Promise, String };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return sandbox.window.VoiceUI;
}

const json = (v) => JSON.stringify(v);
const tick = () => new Promise((r) => setImmediate(r));

class El {
  constructor() {
    this.listeners = {};
    this.hidden = false;
    this.textContent = '';
    this.value = '';
    this.checked = false;
    this.cls = new Set();
    this.classList = {
      toggle: (name, on) => { if (on) this.cls.add(name); else this.cls.delete(name); },
      contains: (name) => this.cls.has(name),
    };
    this.focused = false;
  }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  fire(name, ev) { return Promise.all((this.listeners[name] || []).map((fn) => fn(ev))); }
  focus() { this.focused = true; }
}

function makeDoc(ids = []) {
  const els = {};
  ids.forEach((id) => { els[id] = new El(); });
  const doc = new El();
  doc.getElementById = (id) => els[id] || null;
  return { doc, els };
}

class FakeSource {
  constructor(buffer) { this.buffer = buffer; this.started = null; this.stopped = false; this.onended = null; }
  connect() {}
  start(at) { this.started = at; }
  stop() { this.stopped = true; }
}

function makeEnv(rpcImpl) {
  const calls = [];
  const rpc = async (method, params) => {
    calls.push([method, params]);
    return rpcImpl ? rpcImpl(method, params) : {};
  };
  const closed = { ctx: 0, tracks: 0 };
  const created = { sources: [], nodes: [], decodeOrder: [], contexts: [] };
  class Ctx {
    constructor(opts) {
      this.sampleRate = opts && opts.sampleRate;
      this.currentTime = 0;
      this.destination = {};
      this.state = 'running';
      this.audioWorklet = { addModule: async (url) => { created.module = url; } };
      created.contexts.push(this);
    }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createBufferSource() { const s = new FakeSource(); created.sources.push(s); return s; }
    async decodeAudioData(ab) {
      const id = new Uint8Array(ab)[0];
      created.decodeOrder.push(id);
      // later sentences decode FASTER, to prove ordering does not depend on decode speed
      await new Promise((r) => setTimeout(r, id === 1 ? 15 : 1));
      return { duration: 1.0, id };
    }
    close() { closed.ctx += 1; }
    resume() { this.state = 'running'; }
  }
  class Node {
    constructor() { this.port = { onmessage: null }; created.nodes.push(this); }
    disconnect() {}
  }
  const stream = { getTracks: () => [{ stop() { closed.tracks += 1; } }] };
  const gum = [];
  return {
    rpc, calls, closed, created, gum,
    AudioContext: Ctx, AudioWorkletNode: Node,
    getUserMedia: async (c) => { gum.push(c); return stream; },
  };
}

function pcmChunk(turn, seq, byte, extra = {}) {
  return { turn_id: turn, seq, final: false, text: `s${seq}`, mime_type: 'audio/wav',
    audio_base64: Buffer.from([byte, 0, 0, 0]).toString('base64'), ...extra };
}

describe('audio helpers', () => {
  test('float samples become clamped 16-bit', () => {
    const { int16FromFloat } = load();
    assert.equal(json(Array.from(int16FromFloat(new Float32Array([0, 1, -1, 2, -2])))), json([0, 32767, -32768, 32767, -32768]));
  });

  test('base64 round-trips the sample bytes', () => {
    const { base64FromInt16, bytesFromBase64 } = load();
    const bytes = bytesFromBase64(base64FromInt16(new Int16Array([1, -2, 300])));
    assert.equal(json(Array.from(bytes)), json([1, 0, 254, 255, 44, 1]));
  });
});

describe('Batcher', () => {
  test('emits roughly 100 ms at a time and flushes the rest', () => {
    const { Batcher } = load();
    const out = [];
    const b = new Batcher((x) => out.push(x.length), 1600);
    for (let i = 0; i < 25; i += 1) b.push(new Float32Array(128));
    assert.equal(json(out), json([1664]), '13 blocks reach 100 ms; the other 12 are still waiting');
    b.flush();
    assert.equal(json(out), json([1664, 1536]));
    b.flush();
    assert.equal(out.length, 2);
  });
});

describe('SendLoop', () => {
  test('sends one request at a time and coalesces what arrived meanwhile', async () => {
    const { SendLoop } = load();
    const sent = [];
    let release;
    const rpc = (method, params) => { sent.push(params.pcm_base64.length); return new Promise((r) => { release = r; }); };
    const loop = new SendLoop(rpc, () => {});
    loop.push(new Int16Array(1600));
    loop.push(new Int16Array(1600));
    loop.push(new Int16Array(1600));
    assert.equal(sent.length, 1, 'only one request in flight');
    release({});
    await tick();
    assert.equal(sent.length, 2, 'the two that waited go out together');
    assert.ok(sent[1] > sent[0]);
  });

  test('audio never piles up without bound while the harness is slow', () => {
    const { SendLoop } = load();
    const loop = new SendLoop(() => new Promise(() => {}), () => {});
    for (let i = 0; i < 400; i += 1) loop.push(new Int16Array(1600)); // 40 s
    assert.ok(loop.pendingSamples <= 16000 * 8);
  });

  test('an error stops it and is reported once', async () => {
    const { SendLoop } = load();
    const errors = [];
    const loop = new SendLoop(async () => { throw new Error('Emergency halt is engaged'); }, (e) => errors.push(e.message));
    loop.push(new Int16Array(1600));
    await tick();
    loop.push(new Int16Array(1600));
    await tick();
    assert.equal(json(errors), json(['Emergency halt is engaged']));
  });

  test('an error object in the reply counts as an error', async () => {
    const { SendLoop } = load();
    const errors = [];
    const loop = new SendLoop(async () => ({ error: { message: 'refused' } }), (e) => errors.push(e.message));
    loop.push(new Int16Array(1600));
    await tick();
    assert.equal(json(errors), json(['refused']));
  });

  test('after stop nothing is sent', async () => {
    const { SendLoop } = load();
    let n = 0;
    const loop = new SendLoop(async () => { n += 1; return {}; }, () => {});
    loop.stop();
    loop.push(new Int16Array(1600));
    await tick();
    assert.equal(n, 0);
  });
});

describe('Playback', () => {
  function rig() {
    const env = makeEnv();
    const ctx = new env.AudioContext();
    const { Playback } = load();
    const pb = new Playback(() => ctx, env.rpc);
    return { env, ctx, pb };
  }

  test('plays sentences in arrival order even when a later one decodes first', async () => {
    const { env, pb } = rig();
    pb.setTurn('t1');
    await Promise.all([pb.enqueue(pcmChunk('t1', 0, 1)), pb.enqueue(pcmChunk('t1', 1, 2))]);
    assert.equal(json(env.created.sources.map((s) => s.buffer.id)), json([1, 2]));
  });

  test('sentences are scheduled back to back with no gap', async () => {
    const { env, pb } = rig();
    pb.setTurn('t1');
    await pb.enqueue(pcmChunk('t1', 0, 2));
    await pb.enqueue(pcmChunk('t1', 1, 2));
    const [a, b] = env.created.sources;
    assert.ok(Math.abs(b.started - (a.started + 1.0)) < 1e-6);
  });

  test('reports playback started once, and ended only after the last sentence has finished', async () => {
    const { env, pb } = rig();
    pb.setTurn('t1');
    await pb.enqueue(pcmChunk('t1', 0, 2));
    await pb.enqueue(pcmChunk('t1', 1, 2));
    await pb.enqueue({ turn_id: 't1', seq: 2, final: true });
    const playbackCalls = () => env.calls.filter(([m]) => m === 'voice.playback').map(([, p]) => p.state);
    assert.equal(json(playbackCalls()), json(['started']));
    env.created.sources[0].onended();
    assert.equal(json(playbackCalls()), json(['started']), 'one sentence is still playing');
    env.created.sources[1].onended();
    assert.equal(json(playbackCalls()), json(['started', 'ended']));
    env.created.sources[1].onended();
    assert.equal(playbackCalls().length, 2, 'ended is reported once');
  });

  test('a turn that spoke nothing reports nothing', async () => {
    const { env, pb } = rig();
    pb.setTurn('t1');
    await pb.enqueue({ turn_id: 't1', seq: 0, final: true });
    assert.equal(env.calls.length, 0);
  });

  test('sentences from another turn are ignored', async () => {
    const { env, pb } = rig();
    pb.setTurn('t2');
    await pb.enqueue(pcmChunk('t1', 0, 2));
    assert.equal(env.created.sources.length, 0);
  });

  test('stop cuts every playing sentence off and ignores what arrives afterwards', async () => {
    const { env, pb } = rig();
    pb.setTurn('t1');
    await pb.enqueue(pcmChunk('t1', 0, 2));
    await pb.enqueue(pcmChunk('t1', 1, 2));
    pb.stop();
    assert.ok(env.created.sources.every((s) => s.stopped));
    await pb.enqueue(pcmChunk('t1', 2, 2));
    assert.equal(env.created.sources.length, 2);
  });

  test('a sentence still decoding when she is interrupted never plays', async () => {
    const { env, pb } = rig();
    pb.setTurn('t1');
    const pending = pb.enqueue(pcmChunk('t1', 0, 1));
    pb.stop();
    await pending;
    assert.equal(env.created.sources.length, 0);
  });

  test('a new turn starts clean after an interruption', async () => {
    const { env, pb } = rig();
    pb.setTurn('t1');
    await pb.enqueue(pcmChunk('t1', 0, 2));
    pb.stop();
    pb.setTurn('t2');
    await pb.enqueue(pcmChunk('t2', 0, 2));
    assert.equal(env.created.sources.length, 2);
    assert.equal(env.created.sources[1].stopped, false);
  });

  test('an undecodable sentence is skipped and the next one still plays', async () => {
    const env = makeEnv();
    const ctx = new env.AudioContext();
    let n = 0;
    ctx.decodeAudioData = async () => { n += 1; if (n === 1) throw new Error('bad wav'); return { duration: 1 }; };
    const { Playback } = load();
    const pb = new Playback(() => ctx, env.rpc);
    pb.setTurn('t1');
    await pb.enqueue(pcmChunk('t1', 0, 2));
    await pb.enqueue(pcmChunk('t1', 1, 2));
    assert.equal(env.created.sources.length, 1);
  });
});

describe('Playback ordering', () => {
  test('a final marker that arrives while the last sentence is still decoding still ends playback afterwards', async () => {
    const env = makeEnv();
    const ctx = new env.AudioContext();
    const { Playback } = load();
    const pb = new Playback(() => ctx, env.rpc);
    pb.setTurn('t1');
    // a reply that is one sentence: the sentence and the final marker are sent back to back
    const sentence = pb.enqueue(pcmChunk('t1', 0, 2));
    await pb.enqueue({ turn_id: 't1', seq: 1, final: true });
    const states = () => env.calls.filter(([m]) => m === 'voice.playback').map(([, p]) => p.state);
    assert.equal(json(states()), json([]), 'nothing has started and nothing can have ended yet');
    await sentence;
    assert.equal(json(states()), json(['started']));
    env.created.sources[0].onended();
    assert.equal(json(states()), json(['started', 'ended']), 'it ends once the sentence has finished playing');
  });
});

describe('indicator text', () => {
  test('is empty unless the microphone is on', () => {
    const { indicatorText } = load();
    assert.equal(indicatorText(null), '');
    assert.equal(indicatorText({ listening: false, state: 'off' }), '');
  });

  test('says the mic is live and what it is waiting for', () => {
    const { indicatorText } = load();
    const text = indicatorText({ listening: true, state: 'waiting', mode: 'continuous', listening_for: 'Hey Tala' });
    assert.match(text, /MIC LIVE/);
    assert.match(text, /Hey Tala/);
  });

  test('in conversation style it says to just talk, not to say a phrase', () => {
    const { indicatorText } = load();
    const text = indicatorText({ listening: true, state: 'waiting', mode: 'continuous', wake_enabled: false, listening_for: 'Hey Tala' });
    assert.match(text, /MIC LIVE/);
    assert.match(text, /just talk/i);
    assert.doesNotMatch(text, /Hey Tala/);
  });

  test('with the wake phrase required it still names the phrase', () => {
    const { indicatorText } = load();
    assert.match(indicatorText({ listening: true, state: 'waiting', mode: 'continuous', wake_enabled: true, listening_for: 'Hey Tala' }), /Hey Tala/);
  });

  test('dictating is not described as waiting for a phrase', () => {
    const { indicatorText } = load();
    const text = indicatorText({ listening: true, state: 'waiting', mode: 'dictate', wake_enabled: false });
    assert.match(text, /dictating/i);
    assert.doesNotMatch(text, /just talk/i);
  });

  test('every state keeps saying the mic is live', () => {
    const { indicatorText } = load();
    for (const state of ['waiting', 'capturing', 'transcribing', 'thinking', 'speaking']) {
      assert.match(indicatorText({ listening: true, state, mode: 'continuous' }), /MIC LIVE/);
    }
  });

  test('says when she can be interrupted', () => {
    const { indicatorText } = load();
    assert.match(indicatorText({ listening: true, state: 'speaking', mode: 'continuous' }), /interrupt/i);
  });
});

describe('controller', () => {
  const IDS = ['btn-chat-listen', 'voice-indicator', 'voice-indicator-text', 'chat-textarea'];

  async function rig(rpcImpl) {
    const env = makeEnv(rpcImpl || ((m) => (m === 'voice.listen_start'
      ? { listening: true, state: 'waiting', mode: 'continuous', listening_for: 'Hey Tala' } : {})));
    const { doc, els } = makeDoc(IDS);
    els['voice-indicator'].hidden = true;
    const chat = { messages: [], turns: [], appendMessage(...a) { this.messages.push(a); }, sendVoiceTurn(t, id) { this.turns.push([t, id]); } };
    const ui = load().createController({ doc, rpc: env.rpc, agentChat: chat, confirmFn: () => true,
      getUserMedia: env.getUserMedia, AudioContext: env.AudioContext, AudioWorkletNode: env.AudioWorkletNode });
    return { env, els, chat, ui, doc };
  }

  test('listening starts the harness, opens the mic with echo cancellation, and shows the indicator', async () => {
    const { env, els, ui } = await rig();
    await ui.toggle();
    assert.equal(json(env.calls[0]), json(['voice.listen_start', { mode: 'continuous' }]));
    assert.equal(env.gum[0].audio.echoCancellation, true);
    assert.equal(env.gum[0].audio.noiseSuppression, true);
    assert.equal(env.gum[0].audio.channelCount, 1);
    assert.ok(env.created.contexts.some((c) => c.sampleRate === 16000), 'the capture context is 16 kHz');
    assert.equal(env.created.module, 'voice_worklet.js');
    assert.equal(els['voice-indicator'].hidden, false);
    assert.match(els['voice-indicator-text'].textContent, /MIC LIVE/);
    assert.equal(ui.isListening(), true);
  });

  test('microphone audio is sent as voice.audio', async () => {
    const { env, ui } = await rig();
    await ui.toggle();
    const node = env.created.nodes[0];
    for (let i = 0; i < 13; i += 1) node.port.onmessage({ data: new Float32Array(128) });
    await tick();
    const audio = env.calls.filter(([m]) => m === 'voice.audio');
    assert.equal(audio.length >= 1, true);
    assert.equal(typeof audio[0][1].pcm_base64, 'string');
  });

  test('stopping turns the mic off, releases the tracks, and hides the indicator', async () => {
    const { env, els, ui } = await rig();
    await ui.toggle();
    await ui.toggle();
    assert.equal(env.closed.tracks, 1);
    assert.equal(env.calls.some(([m]) => m === 'voice.listen_stop'), true);
    assert.equal(els['voice-indicator'].hidden, true);
    assert.equal(ui.isListening(), false);
  });

  test('a refusal to start (the emergency halt) never opens the microphone', async () => {
    const { env, chat, els, ui } = await rig(async (m) => (m === 'voice.listen_start'
      ? { error: { message: 'Emergency halt is engaged' } } : {}));
    await ui.toggle();
    assert.equal(env.gum.length, 0);
    assert.equal(els['voice-indicator'].hidden, true);
    assert.match(chat.messages[0][2], /Emergency halt/);
  });

  test('a microphone that cannot be opened stops listening on the harness too', async () => {
    const { env, ui } = await rig();
    env.getUserMedia = null;
    const { doc } = makeDoc(IDS);
    const ui2 = load().createController({ doc, rpc: env.rpc, agentChat: { appendMessage() {} }, confirmFn: () => true,
      getUserMedia: async () => { throw new Error('Permission denied'); }, AudioContext: env.AudioContext, AudioWorkletNode: env.AudioWorkletNode });
    await ui2.toggle();
    assert.equal(env.calls.some(([m]) => m === 'voice.listen_stop'), true);
    assert.equal(ui2.isListening(), false);
    assert.ok(ui);
  });

  test('an error from voice.audio (the halt) turns the microphone off and says why', async () => {
    let halted = false;
    const { env, els, chat, ui } = await rig(async (m) => {
      if (m === 'voice.listen_start') return { listening: true, state: 'waiting', mode: 'continuous' };
      if (m === 'voice.audio' && halted) throw new Error('Emergency halt is engaged');
      return {};
    });
    await ui.toggle();
    halted = true;
    const node = env.created.nodes[0];
    for (let i = 0; i < 13; i += 1) node.port.onmessage({ data: new Float32Array(128) });
    await tick();
    await tick();
    assert.equal(env.closed.tracks, 1);
    assert.equal(els['voice-indicator'].hidden, true);
    assert.match(chat.messages.at(-1)[2], /Voice stopped: Emergency halt/);
  });

  test('a voice.turn sends the heard text through the chat and prepares playback for that turn', async () => {
    const { chat, ui, els } = await rig();
    els['chat-textarea'].value = 'half a sentence';
    ui.handleNotification('voice.turn', { turn_id: 't9', text: 'what time is it' });
    assert.equal(json(chat.turns), json([['what time is it', 't9']]));
    assert.equal(ui.playback.turnId, 't9');
    assert.equal(els['chat-textarea'].value, '');
  });

  test('voice.audio_chunk goes to playback and voice.interrupt stops it', async () => {
    const { env, ui } = await rig();
    ui.handleNotification('voice.turn', { turn_id: 't1', text: 'hi' });
    ui.handleNotification('voice.audio_chunk', pcmChunk('t1', 0, 2));
    await ui.playback.decoding;
    assert.equal(env.created.sources.length, 1);
    ui.handleNotification('voice.interrupt', { turn_id: 't1' });
    assert.equal(env.created.sources[0].stopped, true);
  });

  test('partial transcripts appear in the input box as she hears them', async () => {
    const { ui, els } = await rig();
    ui.handleNotification('voice.partial', { text: 'what is the wea' });
    assert.equal(els['chat-textarea'].value, 'what is the wea');
  });

  test('a dictated transcript lands in the input box, not sent as a turn', async () => {
    const { ui, els, chat } = await rig();
    ui.handleNotification('voice.partial', { text: 'remind me to' });
    ui.handleNotification('voice.transcript', { text: 'remind me to call mum', final: true, dictate: true });
    assert.equal(els['chat-textarea'].value, 'remind me to call mum');
    assert.equal(chat.turns.length, 0);
  });

  test('when the harness reports it is no longer listening, the microphone closes', async () => {
    const { env, els, ui } = await rig();
    await ui.dictate();
    ui.handleNotification('voice.state', { listening: false, state: 'off', mode: 'dictate' });
    assert.equal(env.closed.tracks, 1);
    assert.equal(els['voice-indicator'].hidden, true);
  });

  test('dictation starts in dictate mode', async () => {
    const { env, ui } = await rig();
    await ui.dictate();
    assert.equal(json(env.calls[0]), json(['voice.listen_start', { mode: 'dictate' }]));
  });

  test('turnDone tells the harness the model finished, with the reply for the non-streaming case', async () => {
    const { env, ui } = await rig();
    await ui.turnDone('t1', 'Hello there.');
    assert.equal(json(env.calls.at(-1)), json(['voice.turn_done', { turn_id: 't1', text: 'Hello there.' }]));
  });

  test('Escape while listening asks her to stop talking', async () => {
    const { env, ui, doc } = await rig();
    await ui.toggle();
    await doc.fire('keydown', { key: 'Escape' });
    assert.equal(env.calls.some(([m]) => m === 'voice.interrupt'), true);
  });
});

describe('settings', () => {
  const IDS = ['voice-wake-enabled', 'voice-wake-phrase', 'voice-tts-voice', 'voice-whisper-model', 'voice-cloud-tts', 'voice-settings-note'];

  function rig(confirmAnswer = true) {
    const env = makeEnv(async (m, p) => {
      if (m === 'voice.get_settings') {
        return { settings: { wake_enabled: false, wake_phrase: 'Hey Tala', tts_voice: 'am_adam', whisper_model: 'base.en', allow_cloud_tts: false } };
      }
      if (m === 'voice.set_settings') {
        return p.settings.whisper_model === 'huge' ? { error: { message: 'whisper_model must be one of' } }
          : { ...p.settings, warnings: p.settings.allow_cloud_tts ? ["edge-tts sends the text to Microsoft's servers."] : [] };
      }
      return {};
    });
    const { doc, els } = makeDoc(IDS);
    const asked = [];
    return { env, els, asked, run: () => load().initSettings(doc, env.rpc, (t) => { asked.push(t); return confirmAnswer; }) };
  }

  test('shows the saved settings', async () => {
    const r = rig();
    await r.run();
    assert.equal(r.els['voice-wake-enabled'].checked, false);
    assert.equal(r.els['voice-tts-voice'].value, 'am_adam');
    assert.equal(r.els['voice-whisper-model'].value, 'base.en');
    assert.equal(r.els['voice-cloud-tts'].checked, false);
  });

  test('a change is saved through voice.set_settings', async () => {
    const r = rig();
    await r.run();
    r.els['voice-wake-phrase'].value = 'Hey Sam';
    await r.els['voice-wake-phrase'].fire('change');
    assert.equal(json(r.env.calls.at(-1)), json(['voice.set_settings', { settings: { wake_phrase: 'Hey Sam' } }]));
    assert.equal(r.els['voice-settings-note'].textContent, 'Saved.');
  });

  test('a refused setting is reported', async () => {
    const r = rig();
    await r.run();
    r.els['voice-whisper-model'].value = 'huge';
    await r.els['voice-whisper-model'].fire('change');
    assert.match(r.els['voice-settings-note'].textContent, /Not saved/);
  });

  test('cloud speech asks first, and declining leaves it off and saves nothing', async () => {
    const r = rig(false);
    await r.run();
    r.els['voice-cloud-tts'].checked = true;
    await r.els['voice-cloud-tts'].fire('change');
    assert.match(r.asked[0], /Microsoft/);
    assert.equal(r.els['voice-cloud-tts'].checked, false);
    assert.equal(r.env.calls.filter(([m]) => m === 'voice.set_settings').length, 0);
  });

  test('cloud speech, once confirmed, is saved and the warning is shown', async () => {
    const r = rig(true);
    await r.run();
    r.els['voice-cloud-tts'].checked = true;
    await r.els['voice-cloud-tts'].fire('change');
    assert.equal(json(r.env.calls.at(-1)), json(['voice.set_settings', { settings: { allow_cloud_tts: true } }]));
    assert.match(r.els['voice-settings-note'].textContent, /Microsoft/);
  });

  test('turning cloud speech off needs no confirmation', async () => {
    const r = rig(false);
    await r.run();
    r.els['voice-cloud-tts'].checked = false;
    await r.els['voice-cloud-tts'].fire('change');
    assert.equal(r.asked.length, 0);
  });

  test('loads without any of the controls present', async () => {
    const { doc } = makeDoc([]);
    await load().initSettings(doc, makeEnv().rpc, () => true);
  });
});

describe('wiring', () => {
  const html = fs.readFileSync(path.join(ROOT, 'renderer/index.html'), 'utf8');
  const js = fs.readFileSync(path.join(ROOT, 'renderer/renderer.js'), 'utf8');

  test('the page loads the script and has the controls', () => {
    assert.match(html, /<script src="voice_ui\.js"><\/script>/);
    for (const id of ['btn-chat-listen', 'voice-indicator', 'voice-indicator-text', 'voice-wake-enabled', 'voice-wake-phrase',
      'voice-tts-voice', 'voice-whisper-model', 'voice-cloud-tts']) {
      assert.match(html, new RegExp(`id="${id}"`));
    }
  });

  test('the mic indicator lives in the status bar, which is always on screen', () => {
    const footer = html.slice(html.indexOf('<footer id="status-bar"'), html.indexOf('</footer>', html.indexOf('<footer id="status-bar"')));
    assert.match(footer, /id="voice-indicator"/);
  });

  test('cloud speech is off in the markup and labelled as sending text to Microsoft', () => {
    const tag = html.match(/<input[^>]*id="voice-cloud-tts"[^>]*>/)[0];
    assert.doesNotMatch(tag, /checked/);
    assert.match(html, /sends text to Microsoft/);
  });

  test('renderer.js creates the controller, routes voice notifications, and streams voice turns', () => {
    assert.match(js, /VoiceUI\.createController\(/);
    assert.match(js, /method\.startsWith\('voice\.'\) && this\._voice/);
    assert.match(js, /sendVoiceTurn\(text, turnId\)/);
    assert.match(js, /stream: true, voice_turn_id: opts\.voiceTurnId/);
    assert.match(js, /this\._voice\.turnDone\(opts\.voiceTurnId, replyText\)/);
  });

  test('a voice turn cancelled by barge-in is not shown as an error', () => {
    assert.match(js, /opts\.voiceTurnId && String\(err\.message \?\? err\)\.includes\('Turn cancelled'\)/);
  });

  test('a voice turn is not also read out by the old non-streaming path', () => {
    assert.match(js, /this\.ttsEnabled\(\) && !opts\.voiceTurnId/);
  });

  test('builds with textContent, never by parsing an HTML string', () => {
    assert.doesNotMatch(SRC, /innerHTML/);
  });

  test('the worklet only forwards samples to the page', () => {
    const worklet = fs.readFileSync(path.join(ROOT, 'renderer/voice_worklet.js'), 'utf8');
    assert.match(worklet, /registerProcessor\('tala-pcm-capture'/);
    assert.doesNotMatch(worklet, /fetch|XMLHttpRequest|WebSocket|localStorage|indexedDB/);
  });
});


describe('voice latency notification', () => {
  function controller(extra = {}) {
    const env = makeEnv();
    const { doc } = makeDoc(['btn-chat-listen', 'voice-indicator', 'voice-indicator-text', 'chat-textarea']);
    return load().createController({ doc, rpc: env.rpc, agentChat: { appendMessage() {} }, confirmFn: () => true,
      getUserMedia: env.getUserMedia, AudioContext: env.AudioContext, AudioWorkletNode: env.AudioWorkletNode, ...extra });
  }

  test('the numbers the harness measured go to the status bar', () => {
    const shown = [];
    const ui = controller({ showVoiceLatency: (ms) => shown.push(ms) });

    ui.handleNotification('voice.latency', { turn_id: 't1', latency_ms: { wake_to_first_word: 6300, stt_latency: 850 } });

    assert.deepEqual(shown, [{ wake_to_first_word: 6300, stt_latency: 850 }]);
  });

  test('without a status bar to show it in, the notification is harmless', () => {
    assert.doesNotThrow(() => controller().handleNotification('voice.latency', { latency_ms: { stt_latency: 1 } }));
  });
});


describe('listening from launch (no button press)', () => {
  const IDS = ['btn-chat-listen', 'voice-indicator', 'voice-indicator-text', 'chat-textarea'];

  function rig(settings, { failSettings = 0 } = {}) {
    let settingsCalls = 0;
    const env = makeEnv(async (m) => {
      if (m === 'voice.get_settings') {
        settingsCalls += 1;
        if (settingsCalls <= failSettings) throw new Error('harness not up yet');
        return { settings };
      }
      if (m === 'voice.listen_start') return { listening: true, state: 'waiting', mode: 'continuous', listening_for: 'Hey Tala' };
      return {};
    });
    const { doc, els } = makeDoc(IDS);
    els['voice-indicator'].hidden = true;
    const chat = { messages: [], appendMessage(...a) { this.messages.push(a); } };
    const ui = load().createController({ doc, rpc: env.rpc, agentChat: chat, confirmFn: () => true,
      getUserMedia: env.getUserMedia, AudioContext: env.AudioContext, AudioWorkletNode: env.AudioWorkletNode });
    return { env, els, ui, chat };
  }

  test('when the setting is on, she starts listening by herself, in conversation mode, and the indicator says so', async () => {
    const r = rig({ listen_on_start: true, wake_enabled: true, wake_phrase: 'Hey Tala' });

    await r.ui.autoStart([0]);

    assert.equal(r.env.calls.filter(([m]) => m === 'voice.listen_start').length, 1);
    assert.equal(json(r.env.calls.find(([m]) => m === 'voice.listen_start')), json(['voice.listen_start', { mode: 'continuous' }]));
    assert.equal(r.ui.isListening(), true);
    assert.equal(r.env.gum.length, 1, 'the microphone was opened');
    assert.match(r.els['voice-indicator-text'].textContent, /MIC LIVE/);
    assert.equal(r.els['voice-indicator'].hidden, false);
  });

  test('when the setting is off or absent, nothing starts and the microphone is never touched', async () => {
    for (const settings of [{ listen_on_start: false }, {}, { listen_on_start: 'true' }, { listen_on_start: 1 }]) {
      const r = rig(settings);
      await r.ui.autoStart([0]);
      assert.equal(r.env.calls.filter(([m]) => m === 'voice.listen_start').length, 0);
      assert.equal(r.env.gum.length, 0);
      assert.equal(r.ui.isListening(), false);
    }
  });

  test('a harness that is still starting is retried, then given up on quietly', async () => {
    const r = rig({ listen_on_start: true }, { failSettings: 2 });
    await r.ui.autoStart([0, 0, 0]);
    assert.equal(r.ui.isListening(), true, 'the third try found it');

    const dead = rig({ listen_on_start: true }, { failSettings: 99 });
    await assert.doesNotReject(dead.ui.autoStart([0, 0]));
    assert.equal(dead.ui.isListening(), false);
    assert.equal(dead.chat.messages.length, 0, 'no error message for a harness that never came up');
  });

  test('a refused microphone or listen_start is reported once, not retried forever', async () => {
    const env = makeEnv(async (m) => (m === 'voice.get_settings' ? { settings: { listen_on_start: true } }
      : m === 'voice.listen_start' ? { error: { message: 'refused: the emergency halt is engaged' } } : {}));
    const { doc } = makeDoc(IDS);
    const chat = { messages: [], appendMessage(...a) { this.messages.push(a); } };
    const ui = load().createController({ doc, rpc: env.rpc, agentChat: chat, confirmFn: () => true,
      getUserMedia: env.getUserMedia, AudioContext: env.AudioContext, AudioWorkletNode: env.AudioWorkletNode });

    await ui.autoStart([0, 0, 0]);

    assert.equal(env.calls.filter(([m]) => m === 'voice.listen_start').length, 1);
    assert.match(chat.messages[0][2], /Voice stopped: refused/);
  });

  test('it never starts twice if she is already listening', async () => {
    const r = rig({ listen_on_start: true });
    await r.ui.toggle();
    await r.ui.autoStart([0]);
    assert.equal(r.env.calls.filter(([m]) => m === 'voice.listen_start').length, 1);
  });
});

describe('the listen-on-start switch', () => {
  const IDS = ['voice-listen-on-start', 'voice-settings-note', 'voice-wake-enabled'];

  function rig(confirmAnswer, saved = false) {
    const env = makeEnv(async (m, p) => {
      if (m === 'voice.get_settings') return { settings: { listen_on_start: saved, wake_enabled: true } };
      if (m === 'voice.set_settings') return { ...p.settings, warnings: [] };
      return {};
    });
    const { doc, els } = makeDoc(IDS);
    const asked = [];
    return { env, els, asked, run: () => load().initSettings(doc, env.rpc, (t) => { asked.push(t); return confirmAnswer; }) };
  }

  test('shows what is saved', async () => {
    const r = rig(true, true);
    await r.run();
    assert.equal(r.els['voice-listen-on-start'].checked, true);
  });

  test('turning it on asks first: the microphone will be live from the moment she starts', async () => {
    const r = rig(false);
    await r.run();
    r.els['voice-listen-on-start'].checked = true;
    await r.els['voice-listen-on-start'].fire('change');

    assert.match(r.asked[0], /microphone/i);
    assert.equal(r.els['voice-listen-on-start'].checked, false, 'declining leaves it off');
    assert.equal(r.env.calls.filter(([m]) => m === 'voice.set_settings').length, 0);
  });

  test('once confirmed it is saved, and turning it off needs no confirmation', async () => {
    const r = rig(true);
    await r.run();
    r.els['voice-listen-on-start'].checked = true;
    await r.els['voice-listen-on-start'].fire('change');
    assert.equal(json(r.env.calls.at(-1)), json(['voice.set_settings', { settings: { listen_on_start: true } }]));

    const off = rig(false, true);
    await off.run();
    off.els['voice-listen-on-start'].checked = false;
    await off.els['voice-listen-on-start'].fire('change');
    assert.equal(off.asked.length, 0);
    assert.equal(json(off.env.calls.at(-1)), json(['voice.set_settings', { settings: { listen_on_start: false } }]));
  });
});
