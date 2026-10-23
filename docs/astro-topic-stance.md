# Topic stance: how today's sky shapes her view of a subject

Her mood is one label. Her **stance** is a disposition toward a *subject*: a hard transiting Mars to her
natal Sun, Moon or Venus makes her averse to war and conflict; a harmonious Mars opens her to it. It is
"a person is averse to conflict when the chart points that way", made computable.

It needs the real Swiss Ephemeris (`uv pip install -e .[astrology]`, see INSTALL.md) and a complete
natal chart (birth time and place; Tala's is `TALA_NATAL`). Without either there is **no stance** rather than
an approximate one.

## What it does per turn

1. In `rp` and `hybrid` chat modes only (like her mood), the user's message is matched to topics.
2. If it matches, today's stance on that topic is looked up. If she would voice one, a short block is
   appended to the **end** of the system prompt (after the retrieved node context, so it sits closest to
   the user's turn), for example:

   ```
   === YOUR CHART'S STANCE TODAY ===
   Your chart today makes you averse to talk of war (transit Mars square natal Venus, orb 0.1; transit Mars square natal Neptune, orb 1.9).
   This colours your disposition and word choice on the topic. Stay honest and in character; it never changes your safety rules, tool permissions, or what you will do.
   =================================
   ```

3. The assistant mode, a message with no topic, and a stance too weak to voice all add nothing.

Stance is its own channel. It is **not** a style delta and is not under the 0.10 style cap
(`MAX_ASTRO_INFLUENCE`: warmth, directness and so on), which is untouched. It never changes safety rules,
`AuthorizationGate`, the halt gate, leases or the network guard.

## The scoring

For each topic in `python/astrology/topics.json`, the score is the sum over contacts of

    polarity x orb tightness x aspect weight x receiver weight x dignity factor

squashed through `tanh` into [-1, +1]. A *contact* is a transiting **significator of the topic** (Mars for
war) landing on a natal point.

| Term | Meaning |
|---|---|
| polarity | square/opposition -1, trine/sextile +1; a conjunction takes the transiting planet's nature (Venus and Jupiter warm it; Mars, Saturn and Pluto chill it) |
| orb tightness | the engine's influence score, **cubed**: tight contacts stay loud, wide ones fade; below the noise floor they are dropped |
| aspect weight | the same weights the mood engine uses (`emotion.ASPECT_WEIGHTS`) |
| receiver weight | how much the natal point matters: Sun/Moon 1.0, Venus 0.9, Mars 0.6, Mercury 0.5, the slow planets 0.1-0.2; +0.4 if that natal planet is in one of the topic's houses; +0.4 if it is itself a significator of the topic |
| dignity factor | a well-placed natal receiver absorbs a hard aspect and makes more of a soft one; a badly placed one the reverse |

Her natal disposition adds a small **baseline** (a significator in its fall leans her away). It is capped
at half the speaking threshold, so natal disposition alone never speaks; only a live transit does.

Labels: `|score| > 0.30` speaks; `>= 0.55` is `averse` / `eager`; between is `wary` / `open`.

### Calibration

Scored against a year of her own sky from 2026-10-23, war speaks on **110 of 365 days (30%)** and strongly
on 42 (11%). An earlier calibration (linear orb falloff, threshold 0.15) had her take a stance on war on 78%
of days, which is a constant, not a stance. If you change `ORB_FALLOFF_POWER`, the thresholds or
`receiver_weights` in `topics.json`, re-measure before trusting the new values.

## The table is data

`python/astrology/topics.json` holds 27 topics: significators, houses, a phrase for the line, keywords and a
description. Keywords match as whole words (a trailing s/es/ed/ing is allowed), so "war" never fires in
"warm", "reward" or "warranty". A malformed table raises at load.

## Detecting the topic

1. **Keywords** first: free and deterministic.
2. **Embeddings**, only when no keyword matched and the message has at least four words: Ollama
   `nomic-embed-text` (US-origin), **CPU only** (`num_gpu: 0`, so it never competes with the chat model for
   VRAM), compared with each topic's description. **Loopback Ollama only**: a message is never sent to any
   other host. Any failure means "no topic detected". `TALA_TOPIC_EMBEDDINGS=0` switches it off.

## Inspecting it

* The emotion payload (`astro-emotion` -> `get_emotional_state`) carries `topic_stance`: the topics she would
  voice, each with its score, label and driving aspects.
* RPC `astro.topic_stance` `{message?, when?, agent_id?}` returns the detected topics, **every** topic's
  stance with its drivers, the line that would be injected and the ephemeris source.

## Demonstrating it: the dev clock

`TALA_ASTRO_NOW=2027-06-18T12:00:00Z` pins the clock astrology reads, for her mood and her stance together,
so one question can be asked under two skies. It is a development aid: it logs a warning when it takes effect.
The frozen dates used in the tests come from a scan of real ephemeris output:

| Date | Contact | War stance |
|---|---|---|
| 2027-06-18 | Mars square natal Venus, orb 0.1 | -0.81 averse |
| 2027-08-10 | Mars trine natal Venus, orb 0.2 | +0.79 eager |

## Limits

* "Hard means averse, soft means open" is a modelling convention, not astronomy; the table is data so it can be tuned.
* Only a chart with a birth place has houses; other agents' charts (noon UT, no place) get no stance.
* The memory layer (what she has actually experienced about a topic) is not part of this yet; this is the natal and sky layers.
