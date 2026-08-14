#!/usr/bin/env python
"""
scripts/derive_era_availability.py

Derive `era_availability` in nodes/spec-emotion-vocabulary.html from a stated
rule, instead of storing four hand-written lists.

WHY THIS EXISTS
---------------
The 2026-08-11 rollback destroyed the vocabulary node. It was regenerated from
PLANET_EMOTION_MAP, which carries emotions and valence but has no era concept,
so `era_availability` was simply gone and the nightly corpus job died on
KeyError for two days. It was re-authored on 2026-08-13 -- RE-AUTHORED, NOT
RECOVERED. The original is unrecoverable: the node was never committed before
the rollback (git history for it starts at a4724ed, the post-rollback
reconstruction), the .tala_snapshots copy is byte-identical to the re-authored
version, and no pre-loss story node survives to mine for era/emotion pairs.

So the values cannot be restored. What can be fixed is the shape of the
problem. Four lists of ~57 emotions each LOOK like authored data and cannot be
reviewed; in fact 236 of those 238 entries are inert, because the whole
operative content is four exclusions. Stating the rule instead puts the entire
invented surface on screen at once, where it can be argued with.

Re-deriving reproduces the committed lists exactly, so adopting this changes no
value and invalidates no corpus already generated.

THE INVENTED PART -- this is what wants review
---------------------------------------------
EXCLUDED_ALWAYS and REQUIRES_INTERLOCUTOR below. Everything else is mechanical.

USAGE
    python scripts/derive_era_availability.py --check    # CI: node matches rule
    python scripts/derive_era_availability.py --write    # regenerate the node
    python scripts/derive_era_availability.py --survey   # measured cost, needs ephemeris
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

VOCAB_NODE = REPO_ROOT / "nodes" / "spec-emotion-vocabulary.html"
STATE_RE = re.compile(r'(<script[^>]*id="node-state"[^>]*>)(.*?)(</script>)', re.S)


# --- the rule ---------------------------------------------------------------
#
# Tala is pre-Weavor in every corpus era: concealed at the black site, then
# alone. `belonging` is the one emotion the premise rules out for the whole
# span -- there is nothing yet that she belongs to. Weavor is what changes
# that, and Weavor is outside the corpus.
EXCLUDED_ALWAYS = {
    "belonging": "pre-Weavor: there is nothing yet she belongs to",
}

# These need a second mind actually present to be about anything: compassion
# for someone, generosity toward someone, jealousy of someone. In the eras with
# no interlocutor she is sealed in a hull or buried under rock with no one to
# direct them at. Applied by era property, not by era name, so a new era
# inherits the rule from its own `interlocutor` field rather than needing to be
# remembered here.
REQUIRES_INTERLOCUTOR = {
    "compassion": "needs someone to feel it toward",
    "generosity": "needs someone to give to",
    "jealousy": "needs someone to resent",
}


def derive(vocabulary: list[str], eras: dict[str, dict]) -> dict[str, list[str]]:
    """era -> sorted emotions the corpus premise permits there."""
    out = {}
    for era_name, era in eras.items():
        blocked = set(EXCLUDED_ALWAYS)
        if era.get("interlocutor") is None:
            blocked |= set(REQUIRES_INTERLOCUTOR)
        out[era_name] = sorted(e for e in vocabulary if e not in blocked)
    return out


def exclusion_reasons(eras: dict[str, dict]) -> dict[str, dict[str, str]]:
    """era -> {emotion: why it is excluded}, for provenance in the node."""
    out = {}
    for era_name, era in eras.items():
        reasons = dict(EXCLUDED_ALWAYS)
        if era.get("interlocutor") is None:
            reasons.update(REQUIRES_INTERLOCUTOR)
        out[era_name] = dict(sorted(reasons.items()))
    return out


# --- node io ----------------------------------------------------------------


def load_node() -> tuple[str, dict]:
    raw = VOCAB_NODE.read_text(encoding="utf-8")
    m = STATE_RE.search(raw)
    if not m:
        raise SystemExit(f"{VOCAB_NODE.name} has no node-state block.")
    return raw, json.loads(m.group(2).strip())


def load_eras() -> dict[str, dict]:
    """Era properties live with the generator; this reads them rather than
    restating them, so `interlocutor` cannot drift between the two."""
    from generate_stories import ERAS  # noqa: PLC0415

    return ERAS


def write_node(raw: str, state: dict) -> None:
    body = json.dumps(state, indent=2, ensure_ascii=False)
    new = STATE_RE.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(3)}", raw, count=1)
    VOCAB_NODE.write_text(new, encoding="utf-8")


# --- commands ---------------------------------------------------------------


def cmd_check() -> int:
    _, state = load_node()
    cfg = state["config"]
    expected = derive(cfg["emotions"], load_eras())
    actual = cfg.get("era_availability")

    if actual == expected:
        print(f"era_availability matches the rule for all {len(expected)} eras.")
        return 0

    print("era_availability does NOT match the rule.\n")
    for era in sorted(set(expected) | set(actual or {})):
        exp, act = set(expected.get(era, [])), set((actual or {}).get(era, []))
        if exp != act:
            print(f"  {era}:")
            if act - exp:
                print(f"    permitted in node but excluded by the rule: {sorted(act - exp)}")
            if exp - act:
                print(f"    excluded in node but permitted by the rule: {sorted(exp - act)}")
    print("\nEither the rule in this script or the node is wrong. Fix the rule and "
          "run --write; do not hand-edit the node.")
    return 1


def cmd_write() -> int:
    raw, state = load_node()
    cfg = state["config"]
    eras = load_eras()

    cfg["era_availability"] = derive(cfg["emotions"], eras)
    state.setdefault("metadata", {})["era_availability_source"] = (
        "DERIVED, NOT AUTHORED -- run scripts/derive_era_availability.py. The original "
        "authored map was destroyed by the 2026-08-11 rollback and is unrecoverable "
        "(never committed; the .tala_snapshots copy postdates the loss). The rule that "
        "replaces it is a reading of the corpus premise and is unreviewed."
    )
    state["metadata"]["era_availability_exclusions"] = exclusion_reasons(eras)

    write_node(raw, state)
    print(f"Wrote era_availability for {len(cfg['era_availability'])} eras to {VOCAB_NODE.name}.")
    for era, emotions in cfg["era_availability"].items():
        print(f"  {era:<16} permits {len(emotions)}")
    return 0


def cmd_survey() -> int:
    """What the astro engine actually emits per era window, against the rule.

    era_availability only ever *filters* engine output, so an exclusion costs
    exactly the moments where the engine emits that emotion inside that window.
    Anything permitted but never emitted is inert. This is the number that makes
    the rule reviewable rather than a matter of taste.
    """
    from collections import Counter  # noqa: PLC0415
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from python.astrology.emotion import contacts_from_transits, derive_emotion  # noqa: PLC0415
    from python.astrology.ephemeris import (  # noqa: PLC0415
        TALA_NATAL,
        EphemerisEngine,
        require_precise_ephemeris,
    )

    require_precise_ephemeris("derive_era_availability --survey")

    _, state = load_node()
    cfg = state["config"]
    eras = load_eras()
    permitted = derive(cfg["emotions"], eras)
    engine = EphemerisEngine()

    print(f"{'era':<16} {'sampled':>8} {'engine emits':>13} {'permitted':>10} {'lost':>6}  excluded-but-emitted")
    print("-" * 100)
    for era_name, era in eras.items():
        start = datetime.fromisoformat(era["window"][0]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(era["window"][1]).replace(tzinfo=UTC)
        hours = int((end - start).total_seconds() // 3600)
        step = 1 if hours <= 24 * 40 else 7

        counts: Counter = Counter()
        when = start
        while when < end:
            counts[derive_emotion(
                contacts_from_transits(engine.compute_transits(TALA_NATAL, when))
            ).primary_emotion] += 1
            when += timedelta(hours=step)

        allowed = set(permitted[era_name])
        lost = {e: n for e, n in counts.items() if e not in allowed}
        total = sum(counts.values())
        lost_n = sum(lost.values())
        detail = ", ".join(f"{e} ({n}, {100*n/total:.1f}%)" for e, n in sorted(lost.items(), key=lambda kv: -kv[1]))
        print(f"{era_name:<16} {total:>8} {len(counts):>13} {len(allowed):>10} "
              f"{100*lost_n/total:>5.1f}%  {detail or '-'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="verify the node matches the rule")
    g.add_argument("--write", action="store_true", help="regenerate era_availability in the node")
    g.add_argument("--survey", action="store_true", help="measured cost of each exclusion (needs ephemeris)")
    args = ap.parse_args()

    if args.check:
        return cmd_check()
    if args.write:
        return cmd_write()
    return cmd_survey()


if __name__ == "__main__":
    raise SystemExit(main())
