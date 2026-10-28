"""Summarise the voice latency log: the median of each number over the last N spoken turns.

    python scripts\voice_latency_report.py            # the last 5 turns
    python scripts\voice_latency_report.py --last 20

Reads ``voice-latency.jsonl`` in ``TALA_VOICE_LOG_DIR`` (default ``<user data>/logs/voice``). The log holds numbers and model names only,
so this prints no transcript and touches no network.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from python.harness import voice_latency  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--last", type=int, default=5, help="how many of the most recent turns to summarise")
    args = parser.parse_args(argv)
    directory = voice_latency.log_dir()
    if directory is None:
        print("The latency log directory is on C: or unset; set TALA_VOICE_LOG_DIR to a folder on D:.")
        return 1
    entries = voice_latency.read_recent(args.last)
    if not entries:
        print(f"No turns logged in {directory}.")
        return 1
    summary = voice_latency.medians(entries)
    print(f"median over the last {summary.pop('turns')} turn(s) in {directory}  (milliseconds)")
    for key, value in summary.items():
        print(f"  {key:<26}{value:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
