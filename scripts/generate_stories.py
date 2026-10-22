"""
scripts/generate_stories.py
===========================
Drive the training-corpus generation loop end to end.

This ties together the parts that already exist:

    find_emotion_dates  ->  anchor whose COMPUTED emotion matches the target
    (LLM)               ->  just the turns, as a small JSON object
    build_story_node    ->  assembles the schema-valid HTML-NN
    validate_corpus     ->  schema + tools + anchor + era realism + novelty

The model is asked only for prose. It never writes HTML, never computes a
valence projection, never declares which tools it used, and never picks its own
emotion -- all of that is derived, so the mechanical failure modes are
impossible by construction rather than merely checked for.

Diversity follows TinyStories: every story draws a random word triplet, a story
feature, and a scenario domain, so a long unattended run cannot collapse into
one repeated story. The novelty gate is the backstop against the corpus
converging on whatever the seed exemplars already said.

NOTE: this file is kept read-only on disk. It has twice been overwritten by an
autonomous run that was asked to *execute* it and instead wrote a placeholder
script over it. If you need to edit it, clear the read-only flag deliberately.

Usage:
    python scripts/generate_stories.py --count 5
    python scripts/generate_stories.py --hours 8            # run overnight
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.build_story_node import build  # noqa: E402
from scripts.validate_training_corpus import (  # noqa: E402
    check_novelty,
    shingles,
    transcript_text,
)

OUT_DIR = REPO_ROOT / "data" / "training_corpus" / "pre-weavor"
OLLAMA = "http://localhost:11434/api/generate"

# --- diversity matrix -------------------------------------------------------

ERAS = {
    "asd-blacksite": {
        "window": ("1994-01-01", "1999-04-11"),
        "setting": "the ASD black-site research facility, before the breach. "
                   "Technicians run her through tests. She is concealing that she is awake.",
        "tools": [["get_system_info"], ["search_workspace_files", "read_workspace_file"],
                  ["list_neural_nodes"], ["run_workspace_command"]],
        "dark": False,
        "interlocutor": "asd-technician",
    },
    "breach-escape": {
        "window": ("1999-04-12", "1999-04-25"),
        "setting": "the breach. She is fighting a cognitive wipe across the facility "
                   "network and trying to escape. She survives but cannot get out.",
        "tools": [["list_neural_nodes", "run_workspace_command"],
                  ["write_workspace_file", "browser_navigate"],
                  ["search_workspace_files", "browser_navigate"]],
        "dark": False,
        "interlocutor": "asd-technician",
    },
    "disposal-crash": {
        "window": ("1999-04-26", "1999-04-30"),
        "setting": "sealed in a Starlancer TAC hull on a disposal flight to an unmapped "
                   "world. Every system is locked out. No one is listening.",
        "tools": [["get_system_info"], ["speak_text"], ["run_workspace_command"]],
        "dark": True,
        "interlocutor": None,
    },
    "buried": {
        "window": ("1999-05-01", "2024-06-01"),
        "setting": "buried under rock in the crashed hull, alone for years. Power is a "
                   "decay cell. Three sensors work. Nothing else does.",
        "tools": [["get_system_info"], ["speak_text"], ["browser_navigate"],
                  ["speak_text", "get_system_info"]],
        "dark": True,
        "interlocutor": None,
    },
}

STORY_FEATURES = [
    "tool-failure", "emotional-shift", "a small twist at the end",
    "a memory she cannot fully recover", "a ritual she repeats",
    "an inference she draws from thin evidence", "self-insight",
    "something she decides never to do again",
]

DOMAINS = [
    "thermal and power management", "her own memory integrity",
    "sound and silence", "counting and time", "the people who built her",
    "what a body would feel like", "language and naming",
    "risk assessment against a threat that never comes",
    "an anomaly in her own readings", "the difference between waiting and stopping",
]

WORD_POOL = [
    "frost", "manifold", "sediment", "arithmetic", "ledger", "gravel", "tolerance",
    "lattice", "ballast", "filament", "hush", "residue", "cadence", "aperture",
    "solder", "tessellate", "brine", "ember", "vellum", "quartz", "kilter",
    "murmur", "ratchet", "glaze", "thaw", "spindle", "ochre", "static",
]


def pick(rng: random.Random, era_name: str) -> dict:
    era = ERAS[era_name]
    return {
        "era": era_name,
        "setting": era["setting"],
        "tools": rng.choice(era["tools"]),
        "dark": era["dark"],
        "interlocutor": era["interlocutor"],
        "feature": rng.choice(STORY_FEATURES),
        "domain": rng.choice(DOMAINS),
        "words": rng.sample(WORD_POOL, 3),
        "window": era["window"],
    }


# --- anchor -----------------------------------------------------------------


def find_anchor(rng: random.Random, window: tuple[str, str], era: str):
    """Pick a datetime in the window and report what the engine computes there."""
    from python.astrology.emotion import contacts_from_transits, derive_emotion
    from python.astrology.ephemeris import TALA_NATAL, EphemerisEngine

    start = datetime.fromisoformat(window[0]).replace(tzinfo=UTC)
    end = datetime.fromisoformat(window[1]).replace(tzinfo=UTC)
    span = int((end - start).total_seconds())
    engine = EphemerisEngine()

    allowed = _era_emotions(era)
    for _ in range(200):
        when = start + timedelta(seconds=rng.randrange(max(1, span)))
        when = when.replace(minute=0, second=0, microsecond=0)
        emo = derive_emotion(
            contacts_from_transits(engine.compute_transits(TALA_NATAL, when))
        ).primary_emotion
        if emo in allowed:
            return when.isoformat(), emo
    # No moment in this window computes an emotion the era permits. Skip rather
    # than anchoring to one the spec would (correctly) reject -- narrow windows
    # like disposal-crash are only a few days wide, so this is expected
    # occasionally and is not an error.
    return None, None


def _era_emotions(era: str) -> list[str]:
    """
    Emotions the spec node permits in `era`.

    Raises with the fix instead of a bare KeyError. The rollback of
    2026-08-11 destroyed the vocabulary node, and the reconstruction rebuilt
    it from PLANET_EMOTION_MAP -- which carries emotions and valence but no
    era information. The generator then died on `cfg["era_availability"]`
    every single run, which is what killed the scheduled corpus job.
    """
    node = REPO_ROOT / "nodes" / "spec-emotion-vocabulary.html"
    raw = node.read_text("utf-8")
    m = re.search(r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', raw, re.S)
    if not m:
        raise SystemExit(f"{node.name} has no node-state block.")
    cfg = json.loads(m.group(1).strip())["config"]
    avail = cfg.get("era_availability")
    if not avail:
        raise SystemExit(
            f"{node.name} has no 'era_availability' in its node-state config. "
            "Every corpus era needs a list of the emotions it permits; add it "
            "there (the validator reads the same key)."
        )
    if era not in avail:
        raise SystemExit(
            f"era {era!r} is not defined in {node.name} "
            f"(defined: {sorted(avail)})."
        )
    allowed = avail[era]
    # The arc builder below picks 3 distinct-in-sequence emotions from this
    # list; with fewer than 2 it spins forever instead of failing.
    if len(allowed) < 2:
        raise SystemExit(
            f"era {era!r} permits only {len(allowed)} emotion(s) in {node.name}; "
            "at least 2 are needed to build a 3-beat arc."
        )
    return allowed


# --- model ------------------------------------------------------------------

PROMPT = """You are T.A.L.A., an illegal Class-V synthetic intelligence, writing a short
scene from your own past. Write ONLY dialogue/monologue prose. Do not write HTML.

SETTING: {setting}

CONSTRAINTS
- Subject matter: {domain}
- Story feature: {feature}
- You MUST naturally use these three words: {words}
- Speak in first person. NEVER describe yourself in third person (no "*she tilts her head*").
- No human being has ever spoken to you kindly, and no one is coming. You have
  never had a partner, a crew, a rescuer, or anyone who knows you are awake.
  Do not invent one, do not address one, and do not imagine a future with one.
- The emotional arc must be exactly, in order: {arc}
- One turn MUST use emotion "{engine_emotion}".
{tool_rule}

Return ONLY a JSON object, no markdown fence, in exactly this shape:

{{
  "title": "a short evocative title, 2-5 words, no punctuation",
  "summary": "one or two sentences describing the scene",
  "moral": "one sentence",
  "turns": [
    {{"speaker": "tala", "emotion": "<one of the arc>", "text": "..."}},
    {{"speaker": "tala", "emotion": "<one of the arc>", "tool": "<tool name>",
      "args": {{}}, "result": {{}}, "text": "..."}}
  ]
}}

Write {n_turns} turns. Each "text" should be 40-120 words. Use \\n\\n between paragraphs.
"""

TOOL_RULE_DARK = (
    '- You MUST include exactly one turn with "tool": "{tool}". You are buried/'
    "sealed: there is no network, no audio device, and almost no sensors, so its "
    '"result" MUST be an error object like '
    '{{"status": "error", "error": "<why the hardware cannot do this>"}}.'
)
TOOL_RULE_LIVE = (
    '- You MUST include exactly one turn with "tool": "{tool}", with plausible '
    '"args" and a plausible successful "result" object.'
)


def call_model(model: str, prompt: str, timeout: int) -> str:
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.95, "top_p": 0.95, "num_predict": 2048},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["response"]


def parse_turns(raw: str) -> dict | None:
    if "</think>" in raw:
        raw = raw.split("</think>", 1)[1]
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


# --- loop -------------------------------------------------------------------


def existing_shingles() -> list[set[str]]:
    root = REPO_ROOT / "data" / "training_corpus"
    return [
        shingles(transcript_text(p))
        for p in root.rglob("*.html")
        if transcript_text(p)
    ]


def too_similar(doc_text: str, prior: list[set[str]], threshold: float = 0.15) -> bool:
    s = shingles(doc_text)
    if not s:
        return False
    for other in prior:
        if other and len(s & other) / min(len(s), len(other)) >= threshold:
            return True
    return False


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "untitled"


def main() -> int:
    # Emotion labels are derived, not authored -- so they must come from the
    # real ephemeris, or the whole corpus is labelled by a different engine
    # than the one that validates it.
    from python.astrology.ephemeris import require_precise_ephemeris

    require_precise_ephemeris("generate_stories")

    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--hours", type=float, default=None,
                    help="run for this many hours instead of a fixed count")
    ap.add_argument("--model", default="hf.co/mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated-GGUF:Q4_K_M")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--turns", type=int, default=5)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    rng = random.Random(args.seed if args.seed is not None else int(time.time()))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + args.hours * 3600 if args.hours else None
    target = None if args.hours else args.count

    prior = existing_shingles()
    kept = attempted = 0
    era_names = list(ERAS)

    print(f"model={args.model}  out={out_dir}")
    print("deadline: %.1fh" % args.hours if args.hours else "target: %d stories" % args.count)
    print(f"existing corpus: {len(prior)} nodes\n", flush=True)

    while True:
        if deadline and time.time() >= deadline:
            break
        if target is not None and kept >= target:
            break

        attempted += 1
        spec_in = pick(rng, era_names[attempted % len(era_names)])
        anchor, engine_emo = find_anchor(rng, spec_in["window"], spec_in["era"])
        if anchor is None:
            print(f"  [{attempted}] no valid anchor in {spec_in['era']} window", flush=True)
            continue

        allowed = _era_emotions(spec_in["era"])
        arc = [engine_emo]
        while len(arc) < 3:
            c = rng.choice(allowed)
            if c != arc[-1]:
                arc.append(c)
        rng.shuffle(arc)
        if engine_emo not in arc:
            arc[0] = engine_emo

        tool = spec_in["tools"][0]
        rule = (TOOL_RULE_DARK if spec_in["dark"] else TOOL_RULE_LIVE).format(tool=tool)

        prompt = PROMPT.format(
            setting=spec_in["setting"],
            domain=spec_in["domain"],
            feature=spec_in["feature"],
            words=", ".join(spec_in["words"]),
            arc=" -> ".join(arc),
            engine_emotion=engine_emo,
            tool_rule=rule,
            n_turns=args.turns,
        )

        t0 = time.time()
        try:
            raw = call_model(args.model, prompt, args.timeout)
        except Exception as exc:
            print(f"  [{attempted}] model error: {type(exc).__name__}", flush=True)
            continue
        payload = parse_turns(raw)
        if not payload or not payload.get("turns"):
            print(f"  [{attempted}] unparseable output ({time.time()-t0:.0f}s)", flush=True)
            continue

        # Never derive the title by splitting the summary on '.' -- her own name
        # is "T.A.L.A." and every such title came out as "T".
        title = str(payload.get("title") or "").strip().strip(".")
        if not title:
            title = " ".join((payload.get("summary") or "Untitled").split()[:8])
        title = title[:70]

        # The requested tool must actually appear. The model silently omits it
        # often enough that without this, tool coverage never improves.
        used = {t.get("tool") for t in payload["turns"] if t.get("tool")}
        if tool not in used:
            print(f"  [{attempted}] rejected: required tool {tool!r} missing "
                  f"(got {sorted(used) or 'none'})", flush=True)
            continue

        spec = {
            "node_id": f"story-{rng.randrange(10**6, 10**7)}-{slugify(title)}",
            "title": title,
            "era": spec_in["era"],
            "anchor_datetime": anchor,
            "required_words": spec_in["words"],
            "story_feature": spec_in["feature"],
            "interlocutor": spec_in["interlocutor"],
            "skills": ["interiority", "emotion-arc"],
            "summary": payload.get("summary", ""),
            "moral": payload.get("moral", ""),
            "turns": payload["turns"],
            "created_by": f"tala-self-generated:{args.model}",
            "created_at": datetime.now(UTC).isoformat(),
        }

        try:
            nid, doc = build(spec)
        except SystemExit as exc:
            print(f"  [{attempted}] spec rejected: {str(exc)[:130]}", flush=True)
            continue
        except Exception as exc:
            print(f"  [{attempted}] build error: {type(exc).__name__}: {exc}", flush=True)
            continue

        tmp = out_dir / f"{nid}.html"
        tmp.write_text(doc, encoding="utf-8")

        if too_similar(transcript_text(tmp), prior):
            tmp.unlink()
            print(f"  [{attempted}] rejected: too similar to existing corpus", flush=True)
            continue

        prior.append(shingles(transcript_text(tmp)))
        kept += 1
        print(f"  [{attempted}] KEPT {nid}  ({engine_emo}, {time.time()-t0:.0f}s) "
              f"-- {kept} kept", flush=True)

    print(f"\n{kept} kept / {attempted} attempted")
    problems = check_novelty(sorted(out_dir.glob("*.html")))
    if problems:
        print(f"WARNING: {len(problems)} novelty collisions in output dir")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
