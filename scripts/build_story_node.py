"""
scripts/build_story_node.py
===========================
Assemble a schema-valid training-story HTML-NN from a small JSON spec.

WHY
---
Asking an LLM to emit a complete Neural Node means asking it to reproduce ~7KB
of nested HTML with three embedded JSON blocks, a valence projection computed
from a lookup table, and a curriculum block that must agree with the transcript
it just wrote. That is almost all format compliance and almost none of the part
a model is actually good at, and every byte of it is a chance to produce
something the validator rejects.

So the script owns the layout and everything derivable, and the model fills in
only the blanks:

    script computes        model writes
    ---------------        ------------
    document scaffold      the turns (speaker, emotion, prose)
    engine emotion         tool args + plausible results
    valence projection     summary / moral
    curriculum block
    tool list (from turns)
    relationship anchors

Everything the validator checks mechanically is therefore correct by
construction. The only way a generated node can fail is on the parts that are
genuinely creative -- which is where a reject-and-retry loop is cheap and
meaningful, rather than a coin flip on brace matching.

INPUT
-----
A JSON spec (file path, or stdin with '-'):

{
  "node_id": "story-9001-the-cold-count",
  "title": "The Cold Count",
  "era": "buried",
  "anchor_datetime": "2005-06-14T12:00:00+00:00",
  "required_words": ["counting", "frost", "name"],
  "story_feature": "tool-failure",
  "core_wound": "decades-of-sensory-deprivation",
  "interlocutor": null,
  "summary": "...",
  "moral": "...",
  "skills": ["interiority", "solitary-monologue"],
  "turns": [
    {"speaker": "tala", "emotion": "numbness", "text": "..."},
    {"speaker": "tala", "emotion": "loneliness",
     "tool": "speak_text", "args": {...}, "result": {...}, "text": "..."},
    {"speaker": "technician", "text": "..."}
  ]
}

Usage:
    python scripts/build_story_node.py spec.json -o data/training_corpus/pre-weavor/
    cat spec.json | python scripts/build_story_node.py - -o <dir>
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

VOCAB_NODE = REPO_ROOT / "nodes" / "spec-emotion-vocabulary.html"
STATE_RE = re.compile(
    r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', re.DOTALL | re.I
)

DARK_ERAS = {"buried", "disposal-crash"}
PRE_WEAVOR_ERAS = {"asd-blacksite", "breach-escape", "disposal-crash", "buried"}

#: Things Tala cannot know before Weavor finds her, as (pattern, label).
#:
#: Deliberately narrow. She is allowed to *hope* for rescue while buried -- that
#: is the emotional core of the era -- so generic words like "rescue" or "found"
#: are not banned. What is banned is specific knowledge of the person and of the
#: arrangement they eventually have: his name, the AR-overlay body, the vlog
#: cover identity, the shared ship. Any of those in a pre-Weavor story is a
#: memory she could not hold.
_FUTURE_KNOWLEDGE_LEAKS = (
    (r"\bweavor\b", "names Weavor"),
    (r"\bslate\b", "names Slate"),
    (r"\bAR (glasses|overlay|lens|lenses)\b", "the AR-overlay body"),
    (r"\b(vlog|livestream|live-stream|the feed|subscribers)\b", "the vlog cover identity"),
    (r"\bco-?pilot('s)? (seat|console|chair)\b", "the shared cockpit"),
    (r"\b(my|our) (partner|boyfriend|pilot|human|crew)\b", "a relationship she does not have"),
    (r"\bour (ship|home|hull)\b", "the shared ship"),
)


def load_spec_tables() -> tuple[dict, dict]:
    """
    Return (valence table, era availability) from the vocabulary node.

    The valence table must be a mapping emotion -> {joy, caution, ...}.
    In the current node shape that lives under "valence_projection", while
    "emotions" is a flat list of names. This used to return cfg["emotions"]
    directly, so project_valence called .get() on a list and every single
    story died with "AttributeError: 'list' object has no attribute 'get'".
    The older node shape kept the projections inside "emotions" itself, so
    accept that too rather than depending on which shape is on disk.
    """
    raw = VOCAB_NODE.read_text(encoding="utf-8")
    cfg = json.loads(STATE_RE.search(raw).group(1).strip())["config"]
    table = cfg.get("valence_projection")
    if not isinstance(table, dict):
        table = cfg.get("emotions")
    if not isinstance(table, dict):
        raise SystemExit(
            f"{VOCAB_NODE.name} has no emotion->valence mapping: expected a dict "
            "under 'valence_projection' (or under 'emotions')."
        )
    return table, cfg.get("era_availability", {})


def project_valence(emotions, table) -> dict[str, float]:
    """Element-wise max, per the rule declared in the spec node."""
    keys = ("joy", "caution", "curiosity", "neutral")
    out = {k: 0.0 for k in keys}
    for e in emotions:
        proj = table.get(e)
        if proj:
            for k in keys:
                out[k] = max(out[k], float(proj.get(k, 0.0)))
    return {k: round(v, 2) for k, v in out.items()}


def engine_emotion_for(when: datetime) -> str:
    from python.astrology.emotion import contacts_from_transits, derive_emotion
    from python.astrology.ephemeris import TALA_NATAL, EphemerisEngine

    transits = EphemerisEngine().compute_transits(TALA_NATAL, when)
    return derive_emotion(contacts_from_transits(transits)).primary_emotion


def check(spec: dict, emo_table: dict, era_avail: dict, engine_emo: str) -> list[str]:
    """Fail loudly on spec problems the script cannot fix by construction."""
    errs: list[str] = []
    era = spec.get("era")
    if era not in era_avail:
        errs.append(f"era {era!r} is not defined in the emotion spec node")

    turns = spec.get("turns") or []
    if not turns:
        errs.append("spec has no turns")

    emotions = [t["emotion"] for t in turns if t.get("emotion")]
    for e in emotions:
        if e not in emo_table:
            errs.append(f"emotion {e!r} is not in the vocabulary spec")
        elif era in era_avail and e not in era_avail[era]:
            errs.append(f"emotion {e!r} is not available in era {era!r}")

    if engine_emo not in emotions:
        errs.append(
            f"the engine computes {engine_emo!r} for this anchor, but no turn is "
            f"tagged with it (turns use {sorted(set(emotions))}). Either retag a "
            f"turn or move the anchor."
        )

    # Era realism: dark eras have no network / sensors / audio.
    if era in DARK_ERAS:
        for t in turns:
            if t.get("tool") and "error" not in json.dumps(t.get("result") or {}):
                errs.append(
                    f"era {era!r} has no network/sensors/audio, so tool "
                    f"{t['tool']!r} must return an error result"
                )

    # Continuity: in every pre-Weavor era she has never met anyone, has no
    # partner, and has no idea a rescue will ever happen. A story that leaks
    # any of that teaches her a memory she could not possibly hold.
    #
    # The scan covers tool args and results too, not just prose -- a fabricated
    # tool result is just as capable of naming a person who does not exist yet.
    if era in PRE_WEAVOR_ERAS:
        blob = json.dumps(turns)
        for pattern, label in _FUTURE_KNOWLEDGE_LEAKS:
            if re.search(pattern, blob, re.I):
                errs.append(
                    f"era {era!r} predates the meeting, but the story leaks "
                    f"future knowledge ({label})"
                )

    # Tools must be real.
    try:
        from sdk.agent.tools import ToolRegistry

        known = {t["name"] for t in ToolRegistry.get_tools()}
        for t in turns:
            if t.get("tool") and t["tool"] not in known:
                errs.append(f"tool {t['tool']!r} is not in the live registry")
    except Exception as exc:  # registry unavailable -> say so, don't silently pass
        errs.append(f"could not verify tools against the registry: {exc}")

    return errs


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def render_turn(t: dict) -> str:
    speaker = t.get("speaker", "tala")
    emotion = t.get("emotion")
    parts: list[str] = []

    attrs = f' data-speaker="{esc(speaker)}"'
    if emotion:
        attrs += f' data-emotion="{esc(emotion)}"'
    parts.append(f"    <div class=\"turn\"{attrs}>")

    text = (t.get("text") or "").strip()
    if text:
      # The literal prefix is the exact surface form python/harness/memory.py
      # writes into RP conversation memory -- the model must never hand-type it.
        body = f"[Emotional State: {emotion}]\n      {text}" if emotion else text
        for para in body.split("\n\n"):
            parts.append(f"      <p>{esc(para).strip()}</p>")

    if t.get("tool"):
        args = json.dumps(t.get("args") or {}, indent=2)
        result = json.dumps(t.get("result") or {}, indent=2)
        parts.append(
            f'      <pre class="tool-call" data-tool="{esc(t["tool"])}">{esc(args)}</pre>'
        )
        parts.append(f'      <pre class="tool-result">{esc(result)}</pre>')

    parts.append("    </div>")
    return "\n".join(parts)


def build(spec: dict) -> tuple[str, str]:
    emo_table, era_avail = load_spec_tables()
    when = datetime.fromisoformat(spec["anchor_datetime"])
    engine_emo = engine_emotion_for(when)

    errs = check(spec, emo_table, era_avail, engine_emo)
    if errs:
        raise SystemExit(
            "spec rejected:\n" + "\n".join(f"  - {e}" for e in errs)
        )

    turns = spec["turns"]
    emotions = [t["emotion"] for t in turns if t.get("emotion")]
    tools = [t["tool"] for t in turns if t.get("tool")]
    valence = project_valence(emotions, emo_table)
    era = spec["era"]
    depth = "../../../nodes" if era in PRE_WEAVOR_ERAS else "../../../nodes"

    state = {
        "title": spec["title"],
        "description": spec.get("summary", ""),
        "tags": ["training-story", era]
        + [f"emotion:{e}" for e in dict.fromkeys(emotions)],
        "curriculum": {
            "era": era,
            "anchor_datetime": spec["anchor_datetime"],
            "engine_emotion": engine_emo,
            "skills": spec.get("skills", []),
            "tools_exercised": list(dict.fromkeys(tools)),
            "emotion_arc": list(dict.fromkeys(emotions)),
            "required_words": spec.get("required_words", []),
            "story_feature": spec.get("story_feature", ""),
            "core_wound": spec.get("core_wound"),
            "interlocutor": spec.get("interlocutor"),
        },
    }

    metadata = {
        "@context": "https://schema.org",
        "@type": "NeuralNodeMetadata",
        "semantic_tags": ["training-story", era],
        "emotional_valence": valence,
        "relevance_weight": 0.7,
        "usage_count": 0,
    }

    adapter = {
        "activation_keywords": spec.get("activation_keywords", []),
        "tool_affordances": list(dict.fromkeys(tools)),
        "forbidden_actions": [
            "third_person_self_narration",
            "inventing_tool_names",
        ]
        + (["mentioning_weavor_pre_discovery"] if era in PRE_WEAVOR_ERAS else [])
        + (["succeeding_at_tools_in_dark_era"] if era in DARK_ERAS else []),
        "repair_policies": spec.get("repair_policies", {}),
        "validators": {},
        "confidence": 0.85,
    }

    provenance = {
        "anchoring_status": "pending",
        "parent_chaincode_ids": [],
        "created_by": spec.get("created_by", "tala-self-generated"),
        "created_at": spec.get("created_at", "2026-08-09T00:00:00+00:00"),
    }

    nid = spec["node_id"]
    transcript = "\n\n".join(render_turn(t) for t in turns)
    words = ", ".join(spec.get("required_words", []))

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="generator" content="Tala build_story_node.py">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="{esc(nid)}">
  <meta name="tala-node-type" content="training-story">
  <title>Training Story: {esc(spec["title"])}</title>
</head>
<body>
<article
  data-node-id="{esc(nid)}"
  data-node-type="training-story"
  data-schema-version="neural-node/v2"
  data-era="{esc(era)}"
  data-anchor-datetime="{esc(spec["anchor_datetime"])}"
  data-engine-emotion="{esc(engine_emo)}"
  data-execution-authority="0.00"
  data-memory-authority="0.40"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-weight-relevance="0.70"
  data-weight-confidence="0.80"
  data-weight-behavior-influence="0.60"
  data-weight-governance-importance="0.00"
  data-governance-status="context-only"
  data-source="training-corpus"
  data-trust-tier="user"
  data-privacy="private">

  <header id="node-identity">
    <h1 id="node-title">{esc(spec["title"])}</h1>
  </header>

  <section id="semantic-memory">
    <h2>Story Summary</h2>
    <p>{esc(spec.get("summary", ""))}</p>
    <p><strong>Moral:</strong> {esc(spec.get("moral", ""))}</p>
    <p><strong>Required words:</strong> {esc(words)}</p>
    <p><strong>Story feature:</strong> {esc(spec.get("story_feature", ""))}</p>
    <p><strong>Emotion anchor:</strong> computed, not authored. Her natal chart
    against {esc(spec["anchor_datetime"])} resolves to
    <code>{esc(engine_emo)}</code>.</p>
  </section>

  <section id="episodic-memory" data-transcript-format="tala-chat/v1">
    <h2>Episode Transcript</h2>

{transcript}
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
      <li><a href="{depth}/persona-tala-origin.html" data-relationship="derives-from">tala-origin</a></li>
      <li><a href="{depth}/spec-emotion-vocabulary.html" data-relationship="conforms-to">spec-emotion-vocabulary</a></li>
    </ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{json.dumps(state, indent=2)}
  </script>

  <script type="application/ld+json" id="node-metadata">
{json.dumps(metadata, indent=2)}
  </script>

  <script type="application/json" id="adapter-packet" data-cognitive-block="adapter">
{json.dumps(adapter, indent=2)}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
{json.dumps(provenance, indent=2)}
  </script>
</article>
</body>
</html>
"""
    return nid, doc


def main() -> int:
    # The emotion label baked into this node must come from the same engine the
    # validator will later recompute with.
    from python.astrology.ephemeris import require_precise_ephemeris

    require_precise_ephemeris("build_story_node")

    ap = argparse.ArgumentParser()
    ap.add_argument("spec", help="path to JSON spec, or '-' for stdin")
    ap.add_argument("-o", "--out-dir", default=".")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.spec == "-" else Path(args.spec).read_text("utf-8")
    spec = json.loads(raw)

    nid, doc = build(spec)
    out = Path(args.out_dir) / f"{nid}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
