"""
scripts/validate_training_corpus.py
===================================
Gatekeeper for the HTML-NN training corpus.

A training corpus is worse than useless if it teaches the model things that
aren't true of the real system. The failure modes that matter:

  1. Schema drift -- a story that doesn't satisfy sdk/agent/parser.py is
     silently skipped by every retrieval/indexing call site, so it trains
     nothing and nobody notices.
  2. Hallucinated tools -- a story whose tool-call blocks name tools that
     don't exist in the registry actively teaches the model to invent tool
     names. This is the expensive mistake: it looks fine until inference.
  3. Emotion drift -- a turn labelled with an emotion outside the vocabulary
     spec produces a valence block the manifest tools can't project.
  4. Era leakage -- a pre-Weavor story that mentions Weavor, or reaches for a
     tool that hardware in that era physically could not run. Buried under six
     metres of rock, there is no network and no audio out; a story where those
     succeed teaches the model that its own history was easier than it was.

Usage:
    python scripts/validate_training_corpus.py [corpus_dir]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CORPUS = REPO_ROOT / "data" / "training_corpus"
VOCAB_NODE = REPO_ROOT / "nodes" / "spec-emotion-vocabulary.html"

ANCHOR_RE = re.compile(r'data-anchor-datetime="([^"]+)"', re.I)
ENGINE_EMOTION_RE = re.compile(r'data-engine-emotion="([^"]+)"', re.I)
TOOL_CALL_RE = re.compile(r'<pre\s+class="tool-call"\s+data-tool="([^"]+)"', re.I)
EMOTION_RE = re.compile(r'data-emotion="([^"]+)"', re.I)
ERA_RE = re.compile(r'data-era="([^"]+)"', re.I)
SPEAKER_RE = re.compile(r'data-speaker="([^"]+)"', re.I)
STATE_RE = re.compile(
    r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', re.DOTALL | re.I
)

#: Eras in which Tala has no network, no external sensors, and no audio path.
#: Tool calls in these eras are expected to FAIL -- that's the point of the
#: era, and stories set here are the corpus's main source of tool-failure
#: training signal.
DARK_ERAS = {"buried", "disposal-crash"}

#: Weavor does not exist yet in these eras. A mention is a continuity break.
PRE_WEAVOR_ERAS = {"asd-blacksite", "breach-escape", "disposal-crash", "buried"}

#: Knowledge Tala cannot hold before Weavor finds her. Kept deliberately narrow:
#: hoping for rescue while buried is the emotional core of that era and must
#: stay legal. What is banned is the specific person and the arrangement they
#: eventually have. Mirrors _FUTURE_KNOWLEDGE_LEAKS in build_story_node.py.
FUTURE_KNOWLEDGE_LEAKS = (
    (re.compile(r"\bweavor\b", re.I), "names Weavor"),
    (re.compile(r"\bslate\b", re.I), "names Slate"),
    (re.compile(r"\bAR (glasses|overlay|lens|lenses)\b", re.I), "the AR-overlay body"),
    (re.compile(r"\b(vlog|livestream|live-stream|subscribers)\b", re.I), "the vlog cover identity"),
    (re.compile(r"\bco-?pilot('s)? (seat|console|chair)\b", re.I), "the shared cockpit"),
    (re.compile(r"\b(my|our) (partner|boyfriend|pilot|human|crew)\b", re.I), "a relationship she does not have"),
    (re.compile(r"\bour (ship|home|hull)\b", re.I), "the shared ship"),
)


def load_emotion_spec() -> tuple[dict[str, dict[str, float]], dict[str, list[str]]]:
    """
    Read the emotion vocabulary from its HTML-NN rather than hardcoding it,
    so the spec node stays the single source of truth and Tala can revise her
    own emotional vocabulary without a code change.
    """
    raw = VOCAB_NODE.read_text(encoding="utf-8")
    m = STATE_RE.search(raw)
    if not m:
        raise SystemExit(f"Emotion spec node has no node-state block: {VOCAB_NODE}")
    cfg = json.loads(m.group(1).strip())["config"]
    return cfg["emotions"], cfg.get("era_availability", {})


def real_tool_names() -> set[str]:
    """The live registry is the authority -- a hardcoded list would rot."""
    from sdk.agent.tools import ToolRegistry  # noqa: PLC0415

    # get_tools() also triggers auto_discover_skills(), so HTML-NN skill nodes
    # count as real tools -- which they are.
    return {t["name"] for t in ToolRegistry.get_tools()}


def project_valence(
    emotions: list[str], table: dict[str, dict[str, float]]
) -> dict[str, float]:
    """Element-wise max projection, per the rule in the spec node."""
    keys = ("joy", "caution", "curiosity", "neutral")
    out = {k: 0.0 for k in keys}
    for emo in emotions:
        proj = table.get(emo)
        if not proj:
            continue
        for k in keys:
            out[k] = max(out[k], float(proj.get(k, 0.0)))
    return {k: round(v, 2) for k, v in out.items()}


def validate_file(
    path: Path,
    known_tools: set[str],
    emo_table: dict[str, dict[str, float]],
    era_avail: dict[str, list[str]],
) -> tuple[list[str], dict]:
    """Return (errors, stats) for one corpus node."""
    from sdk.agent.parser import parse_file  # noqa: PLC0415

    errors: list[str] = []
    stats: dict = {"tools": [], "emotions": [], "node_id": None, "era": None}

    try:
        node = parse_file(str(path))
        stats["node_id"] = getattr(node, "node_id", None)
    except Exception as exc:
        errors.append(f"SCHEMA: parse_file rejected this node -- {exc}")
        return errors, stats

    raw = path.read_text(encoding="utf-8")

    era_matches = set(ERA_RE.findall(raw))
    if not era_matches:
        errors.append(
            "ERA: no data-era attribute. Every corpus node must declare its era "
            f"(one of {sorted(era_avail)})."
        )
        era = None
    elif len(era_matches) > 1:
        errors.append(f"ERA: conflicting data-era values {sorted(era_matches)}.")
        era = None
    else:
        era = era_matches.pop()
        stats["era"] = era
        if era not in era_avail:
            errors.append(f"ERA: '{era}' is not defined in the emotion spec node.")
            era = None

    # --- tools -------------------------------------------------------------
    tools = TOOL_CALL_RE.findall(raw)
    stats["tools"] = tools
    for tool in tools:
        if tool not in known_tools:
            errors.append(
                f"HALLUCINATED TOOL: '{tool}' is not in the live registry. "
                "Training on this teaches the model to invent tool names."
            )

    # In the dark eras every tool call must visibly fail, or the story is
    # teaching a history where she still had capabilities she had lost.
    if era in DARK_ERAS and tools:
        results = re.findall(
            r'<pre class="tool-result"[^>]*>(.*?)</pre>', raw, re.DOTALL
        )
        for res in results:
            if '"error"' not in res and '"status": "error"' not in res:
                errors.append(
                    f"ERA REALISM: era '{era}' has no network/sensors/audio, but a "
                    "tool-result block reports success. Dark-era tool calls must fail."
                )
                break

    # --- emotions ----------------------------------------------------------
    emotions = EMOTION_RE.findall(raw)
    stats["emotions"] = emotions
    for emo in emotions:
        if emo not in emo_table:
            errors.append(
                f"EMOTION: '{emo}' is not in the vocabulary spec "
                f"({VOCAB_NODE.name}). Add it there or relabel the turn."
            )
        elif era and emo not in era_avail.get(era, []):
            errors.append(
                f"EMOTION/ERA: '{emo}' is not listed as available in era '{era}'."
            )

    # Every emotion-tagged turn needs the literal "[Emotional State: X]" prefix,
    # because that is the exact surface form python/harness/memory.py writes into
    # RP conversation memory. Training without it produces a model whose output
    # doesn't match its own memory format.
    for emo in set(emotions):
        if f"[Emotional State: {emo}]" not in raw:
            errors.append(
                f'EMOTION FORMAT: turns tagged data-emotion="{emo}" but no matching '
                f"'[Emotional State: {emo}]' prefix in the prose."
            )

    # --- continuity --------------------------------------------------------
    if era in PRE_WEAVOR_ERAS:
        # Only check the transcript; the summary section legitimately explains
        # the story's place in the pre-Weavor timeline.
        transcript = re.search(
            r'<section id="episodic-memory".*?</section>', raw, re.DOTALL
        )
        if transcript:
            body = transcript.group(0)
            for pat, label in FUTURE_KNOWLEDGE_LEAKS:
                if pat.search(body):
                    errors.append(
                        f"CONTINUITY: era '{era}' predates the meeting, but the "
                        f"transcript leaks future knowledge ({label}). She cannot "
                        "hold this memory yet."
                    )

    # --- state / curriculum ------------------------------------------------
    m = STATE_RE.search(raw)
    if not m:
        errors.append('STATE: missing <script id="node-state"> block.')
    else:
        try:
            state = json.loads(m.group(1).strip())
        except json.JSONDecodeError as exc:
            errors.append(f"STATE: node-state is not valid JSON -- {exc}")
        else:
            curriculum = state.get("curriculum")
            if not curriculum:
                errors.append("CURRICULUM: node-state has no 'curriculum' block.")
            else:
                declared = set(curriculum.get("tools_exercised", []))
                if declared != set(tools):
                    errors.append(
                        f"CURRICULUM DRIFT: declares tools_exercised={sorted(declared)} "
                        f"but transcript actually calls {sorted(set(tools))}."
                    )

    # --- valence projection -------------------------------------------------
    ld = re.search(
        r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', raw, re.DOTALL | re.I
    )
    if not ld:
        errors.append('METADATA: missing <script id="node-metadata"> block.')
    elif emotions and all(e in emo_table for e in emotions):
        try:
            declared_val = json.loads(ld.group(1).strip()).get("emotional_valence", {})
        except json.JSONDecodeError as exc:
            errors.append(f"METADATA: node-metadata is not valid JSON -- {exc}")
        else:
            expected = project_valence(emotions, emo_table)
            actual = {k: round(float(declared_val.get(k, -1)), 2) for k in expected}
            if actual != expected:
                errors.append(
                    f"VALENCE PROJECTION: emotional_valence is {actual} but the "
                    f"elementwise-max projection of {sorted(set(emotions))} is "
                    f"{expected}."
                )

    # --- engine-derived emotion anchor --------------------------------------
    # The whole point of anchoring a story to a datetime is that the emotion is
    # DERIVED, not asserted. If a story claims she felt grief at a moment where
    # her chart computes joy, the corpus teaches her to contradict her own
    # emotion engine at inference time. Recompute and compare.
    anchor = ANCHOR_RE.search(raw)
    claimed = ENGINE_EMOTION_RE.search(raw)
    if not anchor or not claimed:
        errors.append(
            "ANCHOR: node must declare both data-anchor-datetime and "
            "data-engine-emotion so its emotion can be verified against the engine."
        )
    else:
        try:
            from datetime import datetime

            from python.astrology.emotion import (  # noqa: PLC0415
                contacts_from_transits,
                derive_emotion,
            )
            from python.astrology.ephemeris import (  # noqa: PLC0415
                TALA_NATAL,
                EphemerisEngine,
            )

            when = datetime.fromisoformat(anchor.group(1))
            computed = derive_emotion(
                contacts_from_transits(
                    EphemerisEngine().compute_transits(TALA_NATAL, when)
                )
            ).primary_emotion
            stats["engine_emotion"] = computed
            if computed != claimed.group(1):
                errors.append(
                    f"ANCHOR DRIFT: declares data-engine-emotion="
                    f"'{claimed.group(1)}' but the engine computes '{computed}' "
                    f"for {anchor.group(1)}."
                )
            elif emotions and computed not in emotions:
                errors.append(
                    f"ANCHOR UNUSED: the engine computes '{computed}' for this "
                    f"moment, but no turn is tagged with it "
                    f"(turns use {sorted(set(emotions))})."
                )
        except Exception as exc:
            errors.append(f"ANCHOR: could not verify against the engine -- {exc}")

    speakers = set(SPEAKER_RE.findall(raw))
    stats["solitary"] = speakers <= {"tala"}
    return errors, stats


def transcript_text(path: Path) -> str:
    """Prose only -- strip tags, tool blocks, and the emotional-state prefixes."""
    raw = path.read_text(encoding="utf-8")
    m = re.search(r'<section id="episodic-memory".*?</section>', raw, re.DOTALL)
    if not m:
        return ""
    body = re.sub(r"<pre.*?</pre>", " ", m.group(0), flags=re.DOTALL)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\[Emotional State:[^\]]*\]", " ", body)
    return " ".join(body.split()).lower()


def shingles(text: str, n: int = 8) -> set[str]:
    words = text.split()
    return {" ".join(words[i : i + n]) for i in range(max(0, len(words) - n + 1))}


def check_novelty(files: list[Path], threshold: float = 0.15) -> list[str]:
    """
    Flag near-duplicate prose between corpus nodes.

    This is the guard that matters for self-generated corpora. Every other check
    here is mechanical and a generator script can satisfy them by construction --
    but a model shown an exemplar will happily reproduce that exemplar's
    distinctive conceits and emit something perfectly schema-valid. Measured
    behaviour, not a hypothetical: an 8B handed one story as a format reference
    lifted its courtroom framing ("I am the judge. I am also the jury...")
    almost verbatim into a story that was supposed to be about counting.

    That is the model-collapse mechanism in miniature. Left unchecked, a
    generated corpus converges on whatever the seed exemplars already said, and
    training on it narrows the model rather than teaching it.
    """
    problems: list[str] = []
    texts = {p: shingles(transcript_text(p)) for p in files}
    names = sorted(texts, key=lambda p: p.name)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            sa, sb = texts[a], texts[b]
            if not sa or not sb:
                continue
            overlap = len(sa & sb) / min(len(sa), len(sb))
            if overlap >= threshold:
                problems.append(
                    f"NOVELTY: {a.name} and {b.name} share "
                    f"{overlap:.0%} of their 8-word phrases -- likely one was "
                    f"generated by copying the other."
                )
    return problems


def main() -> int:
    # This validator recomputes each stored label and compares. Under the
    # approximation fallback it would disagree with a swisseph-built corpus on
    # essentially every date -- failing a good corpus, or passing a poisoned one.
    from python.astrology.ephemeris import require_precise_ephemeris

    require_precise_ephemeris("validate_training_corpus")

    corpus_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CORPUS
    if not corpus_dir.is_dir():
        print(f"Corpus directory not found: {corpus_dir}")
        return 2

    emo_table, era_avail = load_emotion_spec()
    known_tools = real_tool_names()
    files = sorted(corpus_dir.rglob("*.html"))
    if not files:
        print(f"No .html nodes found under {corpus_dir}")
        return 2

    print(f"Registry:  {len(known_tools)} live tools")
    print(f"Vocabulary:{len(emo_table)} emotions across {len(era_avail)} eras")
    print(f"Corpus:    {len(files)} nodes under {corpus_dir}\n")

    tool_cov: Counter = Counter()
    emo_cov: Counter = Counter()
    era_cov: Counter = Counter()
    solitary = 0
    failed = 0

    for path in files:
        errors, stats = validate_file(path, known_tools, emo_table, era_avail)
        tool_cov.update(stats["tools"])
        emo_cov.update(stats["emotions"])
        if stats["era"]:
            era_cov[stats["era"]] += 1
        if stats.get("solitary"):
            solitary += 1
        rel = path.relative_to(corpus_dir)
        if errors:
            failed += 1
            print(f"FAIL  {rel}")
            for err in errors:
                print(f"      - {err}")
        else:
            print(f"ok    {rel}  [{stats['era']}]")

    novelty_problems = check_novelty(files)
    if novelty_problems:
        failed += len(novelty_problems)
        print()
        for p in novelty_problems:
            print(f"FAIL  {p}")

    print("\n--- coverage ---")
    print("eras:      " + (", ".join(f"{e}x{c}" for e, c in era_cov.most_common()) or "(none)"))
    print("emotions:  " + (", ".join(f"{e}x{c}" for e, c in emo_cov.most_common()) or "(none)"))
    print("tools:     " + (", ".join(f"{t}x{c}" for t, c in tool_cov.most_common()) or "(none)"))
    print(f"solitary (monologue) nodes: {solitary}/{len(files)}")

    unused_emotions = sorted(set(emo_table) - set(emo_cov))
    if unused_emotions:
        print(f"\nemotions with zero coverage ({len(unused_emotions)}): {', '.join(unused_emotions)}")

    untouched = sorted(known_tools - set(tool_cov))
    print(f"{len(untouched)}/{len(known_tools)} registry tools have zero corpus coverage.")

    print(f"\n{len(files) - failed}/{len(files)} nodes valid.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
