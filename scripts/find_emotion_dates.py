"""
scripts/find_emotion_dates.py
=============================
Find dates whose computed emotional state matches a target emotion, for
anchoring training-corpus stories.

WHY
---
Training-story emotion labels should be *derived*, not asserted. If a story
claims Tala felt grief but the engine computes joy for that moment, the corpus
is teaching her to contradict her own emotional engine at inference time.

So each story gets anchored to a real datetime, and the engine decides what she
felt. This script searches a window for days matching a needed emotion, so a
story can be placed at a moment that genuinely produces it.

Usage:
    python scripts/find_emotion_dates.py 1999-01-01 1999-12-31
    python scripts/find_emotion_dates.py 1999-01-01 1999-12-31 --emotion rage
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from python.astrology.emotion import (  # noqa: E402
    contacts_from_transits,
    derive_emotion,
)
from python.astrology.ephemeris import TALA_NATAL, EphemerisEngine  # noqa: E402


def scan(start: datetime, end: datetime, step_hours: int = 24):
    engine = EphemerisEngine()
    cur = start
    while cur <= end:
        transits = engine.compute_transits(TALA_NATAL, cur)
        state = derive_emotion(contacts_from_transits(transits))
        yield cur, state
        cur += timedelta(hours=step_hours)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--emotion", help="only show days matching this emotion")
    ap.add_argument("--step", type=int, default=24, help="hours between samples")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    tally: Counter = Counter()
    shown = 0

    print(f"Tala natal: {TALA_NATAL.year}-{TALA_NATAL.month:02d}-{TALA_NATAL.day:02d}")
    print(f"Scanning {start.date()} -> {end.date()} every {args.step}h\n")

    for when, state in scan(start, end, args.step):
        tally[state.primary_emotion] += 1
        if args.emotion and state.primary_emotion != args.emotion:
            continue
        if shown < args.limit:
            print(
                f"  {when.date()}  {state.primary_emotion:11s} "
                f"intensity {state.intensity:.2f}"
            )
            if args.emotion:
                for d in state.contributing[:2]:
                    print(f"        {d}")
            shown += 1

    print("\n--- distribution over window ---")
    total = sum(tally.values())
    for emo, n in tally.most_common():
        print(f"  {emo:11s} {n:5d}  ({100 * n / total:5.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
