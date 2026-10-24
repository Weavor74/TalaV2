# Local voice

Tala's voice is fully local: a wake word, streaming speech-to-text with voice-activity detection, neural text-to-speech,
and barge-in (talking over her stops her). No audio and no text leaves the machine. Cloud speech exists, is **off by
default**, and says so wherever it can be turned on.

```
microphone (renderer, echo-cancelled)
   │  16 kHz PCM16, ~100 ms at a time, voice.audio
   ▼
harness ── openWakeWord ──┐
        └─ Silero VAD ────┴─▶ VoiceSession (the rules) ─▶ faster-whisper ─▶ voice.partial / voice.transcript
                                   │                                         voice.turn ─▶ UI sends the text to the model
                                   │                                                          (inference.generate, stream)
                                   ◀── barge-in: speech while she is thinking/speaking ──┐
   sentence-chunked Piper ◀── streamed reply (voice_stream) ──▶ voice.audio_chunk ──▶ UI plays it, gapless
```

## What runs where

| piece | model / library | where | origin |
|---|---|---|---|
| speech (default) | Piper, `en_US-lessac-medium` (configurable), ONNX on the CPU | `D:\models\voice\piper` | Rhasspy / Open Home Foundation, US |
| hearing | faster-whisper (CTranslate2), `base.en` int8 (0.7 s per utterance; `small.en` is 2.6 s), **CPU** so the 6 GB GPU stays for the language model | `D:\models\voice\whisper` | OpenAI Whisper weights, converted by SYSTRAN |
| end of utterance | Silero VAD (ONNX, ships inside the pip package) | in the venv | Silero (Russia-based team); chosen explicitly, MIT, runs locally |
| wake word | openWakeWord (ONNX) | `D:\models\voice\wakeword` | US |
| microphone, playback | the Electron renderer (`getUserMedia`, WebAudio) | the app | — |

`piper-tts` is **GPL-3.0-or-later**. It is an optional dependency (the `voice` extra) and is imported only when a Piper
voice is used.

## Setting up on Windows (everything on D:)

```powershell
$env:UV_CACHE_DIR = "D:\caches\uv"; $env:TEMP = "D:\caches\tmp"; $env:TMP = "D:\caches\tmp"
$env:HF_HOME = "D:\hf-cache"; $env:TALA_MODELS_DIR = "D:\models"
uv pip install -e .[voice]
python scripts\setup_voice.py            # the one step that uses the network
python scripts\setup_voice.py --check
```

Without `TALA_MODELS_DIR` the models go under `<user data>/models/voice` (no drive letter is assumed: Tala runs from a USB stick on any OS); on Kagura the commands above put them in `D:\models\voice`.

`scripts\kagura-stack.ps1` reports whether the voice models are present. `setup_voice.py` refuses any path on `C:`. At run
time the models load from those files only; there is no download path in the harness.

## Using it

* **Always listen** (the waveform button by the chat box): switches the microphone on. A red **MIC LIVE** indicator appears
  in the status bar and stays for exactly as long as the microphone is on. Say the wake phrase, then talk. The indicator
  says what it is doing: waiting for the phrase, listening, transcribing, thinking, speaking.
* **Dictate** (the microphone button): one utterance, ended when you stop talking, transcribed into the input box. No wake
  word, no fixed five-second window.
* After she finishes speaking you can answer without the wake word for a few seconds (**follow-up window**).
* **Interrupt:** talk over her. Speech that lasts a quarter of a second stops her mid-sentence, cancels the rest of the
  reply (closing the stream makes the model stop generating) and your words become the next turn. A cough does nothing.
  `Esc` also stops her.
* Partial transcripts appear in the input box as you speak.

The wake word only gates. Speech that does not follow it is never transcribed, and what is held in memory is the current
utterance and half a second before it.

## Settings

Settings → VOICE, stored in `<user data>/voice.json`:

| setting | default | |
|---|---|---|
| `wake_enabled` | on | off = every utterance is a command while listening |
| `wake_phrase` | Hey Tala | |
| `wake_model` | (auto) | explicit path wins; else `hey_tala.onnx` if trained; else the pretrained `hey jarvis` fallback |
| `wake_threshold` | (auto) | the trained model's own measured threshold (0.8 for `hey_tala`), else 0.5; a number here is your explicit choice |
| `piper_voice` | en_US-lessac-medium | any Piper voice installed on D: |
| `whisper_model` | base.en | `small.en` is more accurate but about four times slower; `tiny.en` is faster |
| `followup_seconds` | 6 | |
| `allow_cloud_tts` | **off** | edge-tts: sends the text to Microsoft. Asks for confirmation to turn on; only ever tried after the local voices |
| `listen_on_start` | off | the microphone is never on unless you switched it on |

A provider-table TTS (an OpenAI-style endpoint with a key) is used only if its endpoint is on this machine
(loopback); a remote one is refused without being contacted unless `TALA_ALLOW_REMOTE_PROVIDERS=1`.

## Privacy

* The microphone is captured in the renderer. Electron allows exactly one permission: **audio capture, for the app's own
  window, from a `file://` page** (`electron/media_permissions.js`). Video, other windows, remote pages and every other
  permission stay denied.
* Audio goes to the harness over the existing local websocket and nowhere else.
* **Nothing is recorded to disk.** Setting `TALA_VOICE_DEBUG_DIR` (it must be on D:) writes each utterance as a WAV for
  debugging, and is off unless you set it.
* Listening is a UI action. It is not an agent tool: the agent can speak (`speak_text`) but cannot switch the
  microphone on. The emergency halt refuses every `voice.*` method and `voice.listen_start` refuses on its own as well.

## The wake word

openWakeWord has no pretrained "Hey Tala". `scripts/train_wakeword.py` builds one locally: it synthesises the phrase with
many voices (Piper's US voices including 900-speaker LibriTTS, at several speeds and noise levels), trains a small
classifier over openWakeWord's own embeddings, exports ONNX, and **measures it on voices it never trained on** (other
Piper voices, unseen LibriTTS speakers, and the Windows system voices) through the real streaming detector: false rejects at
several noise levels and false accepts per hour of ordinary speech. It installs only if it clears the bar, and only with
`--install`. The result is `hey_tala.metrics.json` next to the model.

**Everything it measures is synthetic speech.** Real human voices and real rooms are not represented; the real test is
saying the phrase to it. If it misses you, set `wake_model` to the pretrained fallback (or lower `wake_threshold`).

```powershell
python scripts\setup_voice.py --training-voices
uv pip install -e .[wakeword-training]
python scripts\train_wakeword.py --phrase "Hey Tala" --install
```

## Latency

The harness logs one line when playback starts (`voice latency (ms): ...`) and `voice.status` returns the last one:

| field | meaning |
|---|---|
| `wake_to_first_audio` / `wake_to_playback` | the wake word firing to her first sentence being sent / starting to play |
| `end_of_speech_to_first_audio` | you stop talking to her first sentence being sent (the time-to-first-audio) |
| `stt` | transcription |
| `model_first_text` | the turn starting to the model's first token |
| `first_sentence_tts` | first sentence complete to its audio being ready |

## Tests

`tests/test_voice_session.py` (state machine), `test_voice_vad.py` (segmentation on a fixture WAV), `test_voice_wake.py`
(wake gating), `test_voice_backends.py` (edge-tts is never chosen by default), `test_voice_chunker.py`,
`test_voice_stream.py`, `test_voice_runtime.py` (the whole loop with stand-in models), `test_voice_rpc.py`,
`test_voice_stt.py`, `test_train_wakeword.py`, and `test_voice_models.py` which runs the real models and is skipped where
they are not installed. Jest: `tests/renderer/voice_ui.test.js`, `tests/electron/media_permissions.test.js`.

## Measured on Kagura (2026-10-23)

A live smoke against the real harness with the real models: Silero, openWakeWord with the trained `hey_tala` model, faster-whisper
`base.en`, Piper, and llama3.2:3b through Ollama (with a lean system prompt, see below). **Stood in:** the person (Windows' own
TTS voices, which the wake model never trained on), the microphone and the speakers (a timer plays each sentence for its true
duration). The microphone path through Electron, `getUserMedia` and the browser's echo cancellation was **not** exercised.

| | measured |
|---|---|
| ordinary speech with no wake word | ignored: never transcribed |
| "Hey Tala, what time is it?", no button | the wake word fired as the phrase ended; transcribed with the phrase removed; turn started |
| speech-to-text | 0.77 to 1.45 s per utterance (`base.en`; `small.en` took 2.7 to 4.7 s) |
| end of speech to first audio, warm | 5.4 to 8.9 s: STT about 0.8 s, the model's first token 3.4 to 3.7 s, first sentence synthesised 0.24 to 0.86 s, hand-off to playback about 0.1 s (not counting the 0.5 s of silence that ends an utterance) |
| cold first turn | about 40 s, nearly all of it Ollama loading the 3B model |
| talking over her | playback stopped 0.53 s after the interrupting speech began; the interrupted request was answered within 20 ms |
| reply to the interruption | first audio about 5 to 8 s later (it was 59 s before a cancelled generation was made to stop) |
| answer without the wake word, just after she finished | worked |
| network | no connection to a non-loopback address, sampled every 0.5 s across the harness and its children; no non-loopback request in the harness log |
| audio on disk | none written |

The "Hey Tala" model, measured on 19 held-out voices through the real streaming detector at its chosen threshold of 0.8:
false rejects 0% clean, 2.5% at 10 dB SNR, 3.7% at 5 dB; false accepts 0 in 25 minutes of ordinary speech (2 in the same
25 minutes at a threshold of 0.5, which is why the threshold is measured, not assumed). **Twenty-five minutes with no events
is thin evidence of a low false-accept rate, and all of it is synthetic speech.** See `hey_tala.metrics.json`.

## Known gaps

* **The default chat prompt is heavy.** On the first message of a session the harness assembled a 25,013-token prompt for "what
  time is it?" and the 3B model timed out reading it; the smoke therefore used `raw_prompt` with a short system prompt. In the
  app a voice turn goes through the normal context assembly, which will dominate time-to-first-audio until voice turns get a
  lean context. That is not built.
* **About 2.3 s of every inference request was spent in the token gate** (`token_counter`, logged as `Token gate PASSED ...
  elapsed=2.3 s` even for a 33-token prompt). That was on a smoke harness whose chain endpoint is deliberately closed, so it
  is not established that it happens on a normal install.
* Microphone capture, echo cancellation on speakers, and playback have only been tested with fakes (Jest). Talking over her
  on speakers depends on the browser's echo cancellation working; headphones avoid the question.
* The wake model has never heard a real person. If it misses you, use the pretrained fallback or lower `wake_threshold`.
* `piper-tts` is GPL-3.0-or-later.
* `uv.lock` does not match `pyproject.toml` (it did not before this change either; regenerating it rewrites about 2,900
  lines), so the voice packages are in the `voice` extra but not in the lock file.
