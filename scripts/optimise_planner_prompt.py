#!/usr/bin/env python
"""
scripts/optimise_planner_prompt.py

One optimisation round: propose planner prompts from recorded failures, measure
them, and keep one only if it wins by a margin.

WHY A SCRIPT AND NOT A GOAL
---------------------------
This changes the prompt the planner plans with. Routing it through the goal
engine would mean a bad promotion could break the machinery needed to undo it,
and the recovery path would depend on the thing that was just broken. It runs
standalone, on a schedule, and touches nothing except the champion node.

SHAPE OF A ROUND
----------------
Screening is the expensive part: roughly sixteen minutes per evaluation on this
hardware. So candidates are screened at n=1 -- enough to discard the clearly
worse -- and only the leader is confirmed at n=3, which is the sample size at
which a difference means anything here. Two identical runs of one engine were
measured scoring 3/6 both times while flipping four of six cases; a promotion
decided on a single run would be deciding on that.

    propose  ->  validate (free)  ->  screen n=1  ->  confirm leader n=3
                                                          |
                                              promote only past the margin

An overnight window holds about six screens plus one confirmation.

USAGE
    python scripts/optimise_planner_prompt.py --engine ollama-...   # a full round
    python scripts/optimise_planner_prompt.py --dry-run             # propose only, no evaluation
    python scripts/optimise_planner_prompt.py --variants 3          # a shorter round
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _score_of(record: dict) -> float:
    try:
        return float(record.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def evaluate(engine: str, label: str, repeat: int) -> dict | None:
    """
    Run the benchmark against whatever prompt is currently installed.

    Shelling out rather than importing: the runner refuses a dirty tree, pins
    the engine, and reads back what actually served, and all of that should
    apply to an optimiser round exactly as it applies to a person running it by
    hand. Reimplementing any of it here would be a second path with its own
    bugs.
    """
    cmd = [
        sys.executable, "scripts/run_benchmark_suite.py",
        "--engine", engine, "--label", label, "--repeat", str(repeat),
        "--allow-dirty",  # the champion node is data; the tree it lives in is not the code under test
    ]
    print(f"    {' '.join(cmd[2:])}", flush=True)
    subprocess.run(cmd, cwd=REPO_ROOT)

    # The runner records the round; read the score back from what it wrote
    # rather than parsing its stdout.
    from scripts.run_benchmark_suite import load_previous_runs  # noqa: E402

    runs = [r for r in load_previous_runs() if r.get("model_label") == label]
    return runs[-1] if runs else None


async def propose(champion: str, evidence: list[str], count: int) -> list:
    from sdk.agent.prompt_optimiser import (
        MUTATION_FRAMINGS,
        Variant,
        propose_variant,
        validate_variant,
    )

    variants: list[Variant] = []
    for index in range(count):
        framing = MUTATION_FRAMINGS[index % len(MUTATION_FRAMINGS)]
        print(f"  proposing {index + 1}/{count}: {framing}", flush=True)

        template = await propose_variant(champion, evidence, framing)
        variant = Variant(template=template, origin=framing)

        reason = validate_variant(template, champion) if template else "the model returned no prompt"
        if reason:
            variant.rejected = reason
            print(f"    rejected: {reason}", flush=True)
        variants.append(variant)
    return variants


def record_round(round_obj, started_at: str) -> None:
    from sdk.agent.documentation_nodes import create_documentation_node
    from sdk.agent.prompt_optimiser import ROUND_DOC_TARGET

    accepted = [v for v in round_obj.variants if not v.rejected]
    stamp = started_at.replace(":", "").replace("-", "").replace("T", "-")[:15]

    lines = [
        f"Planner prompt optimisation round on {started_at}. The champion scored "
        f"{round_obj.champion_score:.0%}; {len(round_obj.variants)} variant(s) were "
        f"proposed and {len(accepted)} survived validation.",
        "",
        f"Outcome: {round_obj.reason}",
        "",
    ]
    if round_obj.evidence:
        lines += ["Proposed from these recorded failures, most frequent first:"]
        lines += [f"- {item}" for item in round_obj.evidence]
        lines += [""]
    lines += ["Variants:"]
    for variant in round_obj.variants:
        if variant.rejected:
            lines.append(f"- {variant.origin}: rejected -- {variant.rejected}")
        elif variant.score is None:
            lines.append(f"- {variant.origin}: not evaluated")
        else:
            lines.append(f"- {variant.origin}: {variant.score:.0%} ({variant.passed}/{variant.total})")

    create_documentation_node(
        node_id=f"planner-optimisation-{stamp}",
        title=f"Planner prompt round {started_at[:16]}",
        doc_target=ROUND_DOC_TARGET,
        semantic_memory="\n".join(lines),
        tags=["planner", "prompt", "optimisation", "measurement"],
        state_extra={"optimisation_round": round_obj.as_dict()},
    )


async def main_async(args) -> int:
    from sdk.agent.prompt_optimiser import (
        Round,
        choose_promotion,
        current_champion,
        gather_evidence,
        install_template,
        promote,
    )

    # Inference reads providers and guardrails from the database, so the pool
    # has to exist before the first mutation call. Without it every proposal
    # fails identically and the round reports "the model returned no prompt",
    # which describes the symptom and hides the cause.
    from python.harness import db

    await db.init_pool()

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    champion, origin = current_champion()
    evidence = gather_evidence()

    print(f"Champion: {origin}, {len(champion)} chars")
    print(f"Evidence: {len(evidence)} recorded obstacle(s)")
    for item in evidence[:5]:
        print(f"  - {item}")
    if not evidence:
        print("  (none yet -- variants will be proposed from the champion alone)")
    print()

    variants = await propose(champion, evidence, args.variants)
    accepted = [v for v in variants if not v.rejected]
    print(f"\n{len(accepted)} of {len(variants)} variant(s) survived validation.")

    if args.dry_run:
        for index, variant in enumerate(accepted, 1):
            print(f"\n--- variant {index} ({variant.origin}) ---")
            print(variant.template[:600])
        return 0

    if not accepted:
        print("Nothing to evaluate; the champion stands.", file=sys.stderr)
        return 0

    print("\nScoring the champion...", flush=True)
    champion_record = evaluate(args.engine, "optimiser: champion", 1)
    if not champion_record:
        print("The champion could not be scored; abandoning the round.", file=sys.stderr)
        return 2
    champion_score = _score_of(champion_record)
    print(f"  champion: {champion_score:.0%}")

    round_obj = Round(champion_score=champion_score, variants=variants, evidence=evidence)

    for index, variant in enumerate(accepted, 1):
        print(f"\nScreening variant {index}/{len(accepted)} ({variant.origin})...", flush=True)
        install_template(variant.template)
        record = evaluate(args.engine, f"optimiser: variant {index}", 1)
        if record:
            variant.score = _score_of(record)
            variant.passed = int(record.get("passed") or 0)
            variant.total = int(record.get("total") or 0)
            print(f"  {variant.score:.0%} ({variant.passed}/{variant.total})")
        else:
            variant.rejected = "the evaluation produced no record"

    # The champion is restored before deciding, so a failure anywhere below
    # leaves the prompt as it was found. install_template rather than promote:
    # putting something back is not a promotion and must not overwrite the
    # provenance of the one that is standing.
    install_template(champion)

    leader, reason = choose_promotion(champion_score, round_obj.variants)
    if leader is None:
        round_obj.reason = reason
        print(f"\nNo promotion: {reason}")
        record_round(round_obj, started_at)
        return 0

    print(f"\nConfirming the leader at n={args.confirm}: {reason}", flush=True)
    install_template(leader.template)
    confirm = evaluate(args.engine, "optimiser: confirmation", args.confirm)
    confirmed = _score_of(confirm) if confirm else 0.0

    final, final_reason = choose_promotion(champion_score, [
        type(leader)(template=leader.template, origin=leader.origin, score=confirmed,
                     passed=int((confirm or {}).get("passed") or 0),
                     total=int((confirm or {}).get("total") or 0))
    ])

    if final is None:
        install_template(champion)
        round_obj.reason = f"did not survive confirmation: {final_reason}"
        print(f"\nReverted to the champion. {final_reason}")
    else:
        # The only real promotion in a round, and the only place provenance is
        # written: it records the score that justified it and the prompt it
        # replaced.
        promote(final.template, score=confirmed, beat=champion_score, evidence=evidence)
        round_obj.promoted = True
        round_obj.promoted_score = confirmed
        round_obj.reason = f"promoted after confirmation: {final_reason}"
        print(f"\nPromoted. {final_reason}")

    record_round(round_obj, started_at)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="ollama-huihui_ai-qwen3-abliterated-4b",
                        help="provider to evaluate against; every run in a round uses the same one")
    parser.add_argument("--variants", type=int, default=6,
                        help="candidates to propose; about six fits an overnight window")
    parser.add_argument("--confirm", type=int, default=3,
                        help="repeats for the confirmation run of the leader")
    parser.add_argument("--dry-run", action="store_true",
                        help="propose and validate only; evaluate nothing and change nothing")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
