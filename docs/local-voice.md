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
   sentence-chunked Kokoro ◀── streamed reply (voice_stream) ──▶ voice.audio_chunk ──▶ UI plays it, gapless
```

## What runs where

| piece | model / library | where | origin |
|---|---|---|---|
| speech (default) | Kokoro-82M, voice `af_heart` (configurable), on the **CPU**, about 0.4x real time once warm | `D:\models\voice\kokoro` | hexgrad (no country stated); StyleTTS 2 architecture from academic authors; Apache-2.0 |
| hearing | faster-whisper (CTranslate2), `base.en` int8 (0.7 s per utterance; `small.en` is 2.6 s), **CPU** so the 6 GB GPU stays for the language model | `D:\models\voice\whisper` | OpenAI Whisper weights, converted by SYSTRAN |
| end of utterance | Silero VAD (ONNX, ships inside the pip package) | in the venv | Silero (Russia-based team); chosen explicitly, MIT, runs locally; **accepted again by Steven on 2026-10-24** under the rule that the US-origin requirement is on the model itself |
| wake word | openWakeWord (ONNX) | `D:\models\voice\wakeword` | US |
| microphone, playback | the Electron renderer (`getUserMedia`, WebAudio) | the app | — |

### Licensing: why the voice is not `pip install kokoro`

Kokoro's weights, the `kokoro` code and `misaki` (its text-to-phoneme library) are Apache-2.0. But `pip install kokoro` pulls in
`phonemizer-fork` (GPL-3.0) and `espeakng-loader` (which bundles espeak-ng, GPL), and `kokoro/pipeline.py` imports them
unconditionally, so even a build that never calls them is GPL. Piper had the same problem (`piper-tts` is GPL-3.0-or-later),
which is why it was replaced. So:

* the network is **vendored** in `python/harness/kokoro_model` (four Apache-2.0 files, licence text alongside, changes noted at
  the top of each), loading from local files only;
* English is phonemised by misaki's own dictionary with `fallback=None`, and the `voice` extra depends on `misaki` *without*
  its `en` extra (that extra is what carries the two GPL packages);
* `tests/test_voice_kokoro.py` fails if any file under `python/` or `sdk/` imports a GPL speech package, if the vendored
  network does, or if the `voice` extra names one. Piper survives only in the `wakeword-training` extra, because
  `scripts/train_wakeword.py` renders its training clips with Piper voices; that is a developer's tool, not the voice that ships.

**The cost of no espeak fallback:** a word misaki's dictionary does not know is silent (it is logged once, with the word). The
names that matter are built in (`Weavor`, `Starlancer`, `Kagura`; `Tala` is already known). For anything else, put the word and its
phonemes (misaki's IPA, e.g. `"zorblax": "zˈɔɹblæks"`) in `D:\models\voice\kokoro\lexicon.json`; it is read when the voice first
loads and its entries win over the built-in ones.

**Origin:** the maintainer states no country and the training run is credited to a Discord user; the architecture is StyleTTS 2.
Nothing ties the weights to a non-US company, but it has not been confirmed to be US-origin either. **Accepted by Steven on
2026-10-24 on that basis**; the rule applies to the model itself, and this is the model he accepted.

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
  in the status bar and stays for exactly as long as the microphone is on. **By default this is a conversation: just talk.**
  Each time you stop, what you said becomes a turn, she answers aloud, and the microphone stays open for your reply, with no
  phrase to say. Turn on **require wake phrase** (the `wake_enabled` setting) if you would rather she answered only after
  "Hey Tala". The indicator says what it is doing: just talk (or the phrase), listening, transcribing, thinking, speaking.
* **On speakers she can hear herself.** The browser's echo cancellation removes most of her voice from the microphone and speech
  in the 0.6 s after she stops is ignored as echo, but if she keeps interrupting herself, use headphones.
* **Dictate** (the microphone button): one utterance, ended when you stop talking, transcribed into the input box. No wake
  word, no fixed five-second window.
* After she finishes speaking you can answer without the wake word for a few seconds (**follow-up window**).
* **Interrupt:** talk over her. Speech that lasts a quarter of a second stops her mid-sentence, cancels the rest of the
  reply (closing the stream makes the model stop generating) and your words become the next turn. A cough does nothing.
  `Esc` also stops her.
* Partial transcripts appear in the input box as you speak.

With the wake phrase required, it only gates: speech that does not follow it is never transcribed, and what is held in memory
is the current utterance and half a second before it. In conversation style there is no gate, so anything said near the
microphone (a television, another person) becomes a turn; that is the price of not saying the phrase.

## Settings

Settings → VOICE, stored in `<user data>/voice.json`:

| setting | default | |
|---|---|---|
| `wake_enabled` | **off** | off = conversation style: every utterance while listening is a turn. on = she answers only after the wake phrase |
| `wake_phrase` | Hey Tala | |
| `wake_model` | (auto) | explicit path wins; else `hey_tala.onnx` if trained; else the pretrained `hey jarvis` fallback |
| `wake_threshold` | (auto) | the trained model's own measured threshold (0.8 for `hey_tala`), else 0.5; a number here is your explicit choice |
| `tts_voice` | af_heart | any Kokoro voice installed on D: (`setup_voice.py --voice am_adam`) |
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
* Audio goes to the harness over the existing local websocket and nowhere else. The modules that hear and answer (wake word, voice
  activity, recogniser, session, runtime, stream, chunker, meter, latency log) import **no network library at all**, so they cannot
  open a connection; `tests/test_voice_privacy.py` fails if one is added. The only voice module that can reach a network is
  `voice.py`, which holds the two optional text-to-speech backends that can, both refused unless explicitly allowed:
  edge-tts (`allow_cloud_tts`, **off by default**, labelled CLOUD, asks before it is switched on) and a provider-table TTS that must be
  on this machine.
* **Nothing is recorded to disk.** A whole spoken turn writes no audio file anywhere (tested). Setting `TALA_VOICE_DEBUG_DIR`
  (it must not be on C:) writes each utterance as a WAV for debugging, only there, and is off unless you set it.
* **The latency log holds numbers and model names only**, never a transcript and never audio (see Latency).
* Nothing but plain sentences reaches the speech engine: thinking, tool calls in any shape, code and tags are removed before synthesis.
* Listening is a UI action. It is not an agent tool: the agent can speak (`speak_text`) but cannot switch the
  microphone on. The emergency halt refuses every `voice.*` method and `voice.listen_start` refuses on its own as well.
* There is one way to listen. The old harness-side five-second recorder (`multimodal.transcribe_voice`) is gone: nothing called it, and
  it was a second place a microphone could be opened.

## The wake word

openWakeWord has no pretrained "Hey Tala". `scripts/train_wakeword.py` builds one locally, **with Kokoro, the voice Tala already speaks
with** (Apache-2.0, US-origin; nothing GPL): sixteen of its twenty US-English voices plus 24 blends of pairs of them, at several speeds
and noise levels, trained into a small classifier over openWakeWord's own embeddings and exported as ONNX. It then **measures it on
voices it never trained on**, through the real streaming detector: four Kokoro voices held out of training and of every blend (two
female, two male) and the two Windows system voices, which are a different engine entirely. The numbers are false rejects at clean,
10 dB and 5 dB noise, and false accepts per hour of ordinary speech. It installs only if it clears the bar, and only with `--install`.
The result is `hey_tala.metrics.json` next to the model; the threshold the runtime uses is the one measured there.

**Everything it measures is synthetic speech.** Real human voices and real rooms are not represented; the real test is saying the
phrase to it. If it misses you, set `wake_model` to the pretrained fallback (or lower `wake_threshold`). The wake phrase is on by
default; the microphone is not (it is never on until you turn listening on, and the indicator reads "MIC LIVE" for as long as it is).

```powershell
$env:TALA_MODELS_DIR = "D:\models"; $env:HF_HOME = "D:\hf-cache"; $env:TEMP = "D:\caches\tmp"; $env:TMP = $env:TEMP
python scripts\setup_voice.py --training-voices        # Kokoro's other US voices, 10 MB, pinned revision
uv pip install -e ".[voice,wakeword-training]"
python scripts\train_wakeword.py --phrase "Hey Tala" --install --device cpu --train-clips 1200 --negative-ratio 1.25 --mine 800
```

Synthesis is the slow part. `--device auto` uses the GPU, which is several times faster, but a game or anything else on the card
slows both down: use `--device cpu` then. The run lowers its own priority, writes only to D:, and downloads nothing.
`tests/test_voice_wake_fixture.py` runs four short WAVs (two say "Hey Tala", a near miss and an ordinary sentence do not; made by
`scripts/make_wake_fixtures.py` in held-out voices) through the detector: with a stand-in model everywhere, and with the real model on
a machine that has it.

## Latency

Three numbers per spoken turn, measured on the runtime's own clock:

| number | from | to |
|---|---|---|
| `wake_to_first_word` | the wake word is detected | the first sound of her answer starts playing |
| `stt_latency` | you stop speaking | the final transcript exists |
| `time_to_first_audio` | you stop speaking | the first piece of her answer has been synthesised |

and the parts they are made of: `stt_compute` (the recogniser alone), `model_first_text` (the turn starting to the model's first
token), `first_sentence_tts` (first sentence complete to its audio being ready) and `first_audio_to_playback`. A follow-up that needed
no wake word has no `wake_to_first_word`; a number whose two marks were not both seen is left out, never zero. Note that "you stop
speaking" is the voice detector's end of speech, which comes about 0.5 s of silence after you actually stop.

Each turn is also appended as one JSON line to `voice-latency.jsonl` in `TALA_VOICE_LOG_DIR` (default `<user data>/logs/voice`; on
Kagura `kagura-stack.ps1` sets it to `D:\tala-logs\voice`). A line holds the turn id, the numbers and the names of the models in use,
**and nothing else: no transcript, no audio**. A directory on C: is refused, the file is rotated at 2 MB, and a log that cannot be
written is a warning, never an error in the voice loop. `voice.status` returns the last turn, a `voice.latency` notification carries
each one to the renderer, and the status bar shows it next to the model's speed:
`voice: wake to first word 6.3 s · speech to text 850 ms · first audio 4.2 s`.

## Tests

`tests/test_voice_session.py` (state machine, with barge-in as a table), `test_voice_vad.py` (segmentation on a fixture WAV),
`test_voice_wake.py` and `test_voice_wake_fixture.py` (wake gating, on WAVs), `test_voice_backends.py` (edge-tts is never chosen,
imported or defaulted unless enabled), `test_voice_privacy.py` (no network library, no audio on disk, no markup to the speech
engine), `test_voice_latency.py`, `test_multimodal.py` (the old recorder stays gone), `test_voice_chunker.py`,
`test_voice_stream.py`, `test_voice_runtime.py` (the whole loop with stand-in models), `test_voice_rpc.py`,
`test_voice_stt.py`, `test_train_wakeword.py`, and `test_voice_models.py` which runs the real models and is skipped where
they are not installed. Jest: `tests/renderer/voice_ui.test.js`, `tests/electron/media_permissions.test.js`.

## Measured on Kagura (2026-10-23)

A live smoke against the real harness with the real models: Silero, openWakeWord with the trained `hey_tala` model, faster-whisper
`base.en`, Piper (the voice at the time; replaced by Kokoro on 2026-10-24, see "Licensing" above), and llama3.2:3b through Ollama (with a lean system prompt, see below). **Stood in:** the person (Windows' own
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

## Kokoro, measured (2026-10-24)

On this CPU, with Kokoro warm: 0.39 to 0.46 seconds of compute per second of speech (a 94-character sentence, 5.9 s of audio,
in 2.5 s), about 2 s for a short phrase on the first call, 27 s to load the first time. The same with and without the GPL packages
present. `tests/test_voice_models.py` runs the real chain: what Kokoro says, faster-whisper hears.

## Known gaps

* **The model-facing prompt is lighter than the logs suggest, and voice turns are leaner still.** The "25k tokens" in the logs is
  the token gate's count *before* the tool documentation is swapped for the provider's native tools; a local model is sent about
  5 to 7k. Since 2026-10-24 a voice turn also skips the settings snapshot unless you ask about the system, sends 6 turns of
  history instead of 10, and no retrieved node is longer than about 800 tokens (`python/harness/prompt_budget.py`).
* The ~2.3 s per request once seen in the token gate was a down chain node: Windows takes about 2 s to refuse a loopback
  connection and every turn records a payment. Fixed 2026-10-24 (a 0.5 s connect cap and a 15 s back-off in `chains.py`).
* **A word Kokoro does not know is silent** (see "Licensing"). Add it to `lexicon.json`.
* Kokoro has not been heard by a person here, only measured and transcribed back by faster-whisper; whether the voice suits Tala
  is untested. (Its weights' unconfirmed origin was accepted on 2026-10-24.)
* Microphone capture, echo cancellation on speakers, and playback have only been tested with fakes (Jest). Talking over her
  on speakers depends on the browser's echo cancellation working; headphones avoid the question.
* The wake model has never heard a real person. If it misses you, use the pretrained fallback or lower `wake_threshold`.
