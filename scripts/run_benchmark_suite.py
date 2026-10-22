#!/usr/bin/env python
"""
scripts/run_benchmark_suite.py

Score the agent benchmarks and record the result as an HTML-NN.

WHY THIS EXISTS
---------------
Nothing in the system could say whether a change made Tala better. The six
benchmarks in ``tests/benchmarks/`` run the real GoalEngine against a live
model, but they are ``pytest`` assertions: they answer "did the suite pass",
which is the wrong question for a capability that is expected to sit at 4/6 and
climb. A red suite is not a regression here, it is Tuesday.

The learning loop needs a *fitness function* -- one number per configuration,
comparable across runs -- before any of it means anything. Re-enabling
auto-distillation, revising a skill after a failure, and evolutionary
optimisation of prompts all reduce to "did the number go up", and none of them
can be judged without it.

WHAT IT RECORDS, AND WHY AS A NODE
----------------------------------
Everything the harness creates is an HTML-NN, and a benchmark result is not an
exception: it is a measurement Tala should be able to recall and reason about
("am I getting better at multi-file edits?"). A JSON file beside the repo would
be invisible to activation. The run is written through
``create_documentation_node``, so it carries the same memory authority as any
other generated node and is recallable rather than inert.

The configuration is recorded with the score, because a score without the model
that produced it is not comparable to anything: model label, git sha, adapter
set, and per-case timings all travel with the number.

ONE SCORE PER ENGINE
--------------------
``--engine`` pins generation to a single provider for the run, through
``TALA_PIN_PROVIDER`` rather than by writing ``is_forced`` into the stored
configuration -- a benchmark that mutates what it measures leaves the
application pinned if the run dies partway. Fallback is disabled by default
(``TALA_PIN_STRICT``), because the generation path falls through to the next
provider on error and a pinned run that quietly finishes on another engine
would otherwise be scored as the first.

The result records what *served*, read back from the harness log, not what was
asked for. They disagree more often than anyone expects, and the run says so.

USAGE
    python scripts/run_benchmark_suite.py                  # run the suite (~30 min, live model)
    python scripts/run_benchmark_suite.py --label llama3.2-3b # name the configuration
    python scripts/run_benchmark_suite.py --junit out.xml  # score an existing JUnit report
    python scripts/run_benchmark_suite.py --compare        # show the delta against the last run
    python scripts/run_benchmark_suite.py --no-write       # score and print, record nothing

    # one score per engine
    python scripts/run_benchmark_suite.py --engine embedded-builtin
    python scripts/run_benchmark_suite.py --engine ollama-llama3-2-3b
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

BENCHMARK_PATH = "tests/benchmarks"

#: doc_target groups every run node into one family so they can be listed and
#: compared without a naming convention doing that job implicitly.
DOC_TARGET = "agent-benchmark-run"


@dataclass
class CaseResult:
    """One benchmark case."""

    name: str
    passed: bool
    seconds: float
    detail: str = ""


@dataclass
class SuiteRun:
    """A scored run of the whole suite, plus the configuration that produced it."""

    cases: list[CaseResult] = field(default_factory=list)
    label: str = "unspecified"
    git_sha: str = "unknown"
    adapters: list[str] = field(default_factory=list)
    started_at: str = ""
    #: The engine the run was pinned to, if any. A request, not a result.
    engine_requested: str = ""
    #: What actually served, read back from the log after the run. A score is
    #: attributed to this, never to engine_requested: the generation path falls
    #: through on error, so a pinned run can complete on a different engine.
    models_observed: list[str] = field(default_factory=list)
    providers_observed: list[str] = field(default_factory=list)
    #: Tracked files that differed from HEAD while this ran. Non-empty means
    #: the recorded sha does not describe the code that produced the score.
    uncommitted: list[str] = field(default_factory=list)

    def attribution_warning(self) -> str:
        """A sentence naming the mismatch, or empty when there is nothing to say."""
        if self.uncommitted:
            # Checked before the engine, because it invalidates the run more
            # completely: an unknown engine makes the score unattributable, but
            # a dirty tree means the sha names code that never ran.
            shown = ", ".join(self.uncommitted[:4])
            more = f" and {len(self.uncommitted) - 4} more" if len(self.uncommitted) > 4 else ""
            return (
                f"The working tree was not clean while this ran ({shown}{more}), "
                f"so commit {self.git_sha} does not describe the code that "
                f"produced this score."
            )
        if not self.engine_requested:
            return ""
        if not self.providers_observed:
            return (
                f"Pinned to {self.engine_requested!r} but nothing recorded serving a "
                f"generation. The score cannot be attributed to any engine."
            )
        others = [p for p in self.providers_observed if p != self.engine_requested]
        if others:
            return (
                f"Pinned to {self.engine_requested!r} but {', '.join(repr(o) for o in others)} "
                f"also served. The score is not attributable to one engine."
            )
        return ""

    #: How many times the suite was run. `cases` holds every attempt flattened,
    #: so `score` is already a mean pass rate rather than one binary outcome.
    repeats: int = 1

    def per_case(self) -> list[tuple[str, int, int, float]]:
        """(name, passes, attempts, total_seconds), in first-seen order."""
        order: list[str] = []
        tally: dict[str, list[float]] = {}
        for case in self.cases:
            if case.name not in tally:
                order.append(case.name)
                tally[case.name] = [0, 0, 0.0]
            tally[case.name][0] += 1 if case.passed else 0
            tally[case.name][1] += 1
            tally[case.name][2] += case.seconds
        return [(n, int(tally[n][0]), int(tally[n][1]), tally[n][2]) for n in order]

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def score(self) -> float:
        """Fraction passed, 0.0-1.0. Zero cases scores 0.0 rather than dividing."""
        return (self.passed / self.total) if self.total else 0.0

    @property
    def duration(self) -> float:
        return sum(c.seconds for c in self.cases)


def parse_junit(xml_path: Path) -> list[CaseResult]:
    """
    Turn a JUnit report into case results.

    JUnit is used rather than a bespoke pytest plugin because it is built in
    (``--junit-xml``), so scoring works against any run of the suite including
    one somebody else produced on other hardware -- which matters for a build
    that is meant to move between machines.

    A case with no ``failure``/``error``/``skipped`` child passed. A *skipped*
    case is not a pass: deselecting the live-model tests and reporting 6/6 is
    exactly the false success this repository keeps finding.
    """
    root = ET.parse(xml_path).getroot()
    results: list[CaseResult] = []

    for case in root.iter("testcase"):
        name = case.get("name") or "<unnamed>"
        try:
            seconds = float(case.get("time") or 0.0)
        except ValueError:
            seconds = 0.0

        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")

        if failure is not None:
            detail = (failure.get("message") or "failed").strip()
            passed = False
        elif error is not None:
            detail = (error.get("message") or "errored").strip()
            passed = False
        elif skipped is not None:
            detail = "skipped -- not counted as a pass"
            passed = False
        else:
            detail = ""
            passed = True

        results.append(
            CaseResult(name=name, passed=passed, seconds=seconds, detail=detail[:300])
        )

    return results


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def uncommitted_files() -> list[str]:
    """
    Tracked files differing from HEAD, which the sha does not describe.

    A run records the sha as the configuration it measured. That is a lie
    whenever the tree is dirty -- and it is dirty in exactly the situation
    where the number matters, mid-change. Measured once today: a 50-minute run
    stamped a sha while the working tree held a half-finished edit that crashed
    every case, and the result was recorded with no indication anything was
    wrong.

    Untracked files are ignored on purpose. Benchmark run nodes and scratch
    output land in the tree constantly and describe nothing about the code
    under test; failing on those would train everyone to pass --allow-dirty
    every time, which is worse than not checking.
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return []
    return [
        line[3:].strip()
        for line in (out.stdout or "").splitlines()
        if line.strip()
    ]


def _discover_adapters() -> list[str]:
    """
    Name the adapters present in the active nodes directory.

    Recorded because an adapter changes behaviour without changing any file the
    git sha covers, so a score attributed to a sha alone would be wrong the
    moment adapters start moving.
    """
    try:
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir

        nodes_dir = resolve_active_nodes_dir()
    except Exception:
        return []

    found: list[str] = []
    try:
        for path in sorted(nodes_dir.glob("*_lora/adapter_model.*")):
            found.append(f"{path.parent.name}/{path.name} ({path.stat().st_size} bytes)")
    except Exception:
        return found
    return found


def benchmark_db_url() -> str:
    """
    The one database both sides of a run must agree on.

    ``conftest`` redirects the suite to a ``_test`` sibling so a run cannot
    write into the live application, which is right -- but it means the child
    process records what served somewhere the parent was not looking, and the
    readback reports "nothing recorded" for a run that generated perfectly
    well. A false negative there is worse than no check at all: it would make
    every pinned run look unattributable.

    So the parent decides, and pins both ends to the identical string via
    ``TALA_TEST_DB_URL`` -- the escape hatch conftest already honours ahead of
    its own derivation. Returns "" when nothing is configured, in which case
    the child keeps its own behaviour and the readback simply finds nothing,
    which is then true rather than an artefact.
    """
    try:
        from python.harness.config import load_config

        configured = str(getattr(load_config(), "DB_URL", "") or "")
    except Exception:
        configured = str(os.environ.get("TALA_DB_URL", "") or "")

    if not configured:
        return ""

    base, sep, database = configured.rpartition("/")
    if not sep or not database:
        return ""
    database, _, query = database.partition("?")
    if database.endswith("_test"):
        return configured
    suffix = f"?{query}" if query else ""
    return f"{base}/{database}_test{suffix}"


def observe_models_served(since: str, until: str) -> tuple[list[str], list[str]]:
    """
    Read back which providers and models actually served during the window.

    Observed, not asserted. ``--engine`` states an intention; the generation
    path falls through to the next provider when one errors, so the only honest
    source for "what produced this score" is what the harness recorded while it
    was producing it.

    The trace column is JSONB but its writers ``json.dumps`` into it, so it
    holds a JSON *string* and server-side ``->>`` silently returns nothing.
    Filtering happens on ``message`` and decoding happens here.
    """
    import asyncio
    import json as _json

    async def _read() -> tuple[list[str], list[str]]:
        agreed = benchmark_db_url()
        if not agreed:
            return [], []

        # Connected directly rather than through python.harness.db.
        #
        # That module does `from .config import settings`, binding a singleton
        # built when config is first imported -- and benchmark_db_url() has to
        # import config to find the DSN in the first place. Config is a frozen
        # dataclass, so the DSN cannot be redirected afterwards by environment
        # or by assignment, and the readback silently queried the *production*
        # database, found nothing, and reported every pinned run as
        # unattributable.
        #
        # This is one read-only SELECT against a database whose name the caller
        # already knows. It does not need the application's pool, its settings,
        # or its startup path.
        import asyncpg

        conn = await asyncpg.connect(agreed)
        try:
            # Bound as datetimes, not ISO strings. asyncpg binds a parameter
            # before any ``::timestamptz`` in the SQL can apply to it, so a
            # string argument raises DataError.
            rows = await conn.fetch(
                "SELECT trace FROM agent_log "
                "WHERE message LIKE $1 AND created_at >= $2 AND created_at <= $3",
                "%inference.served%",
                datetime.fromisoformat(since),
                datetime.fromisoformat(until),
            )
        finally:
            await conn.close()

        models: list[str] = []
        providers: list[str] = []
        for row in rows:
            trace = row["trace"]
            for _ in range(2):  # double-encoded in this codebase; decode twice
                if isinstance(trace, str):
                    try:
                        trace = _json.loads(trace)
                    except Exception:
                        break
            if not isinstance(trace, dict):
                continue
            model = str(trace.get("model", "")).strip()
            provider = str(trace.get("provider_id", "")).strip()
            if model and model not in models:
                models.append(model)
            if provider and provider not in providers:
                providers.append(provider)
        return sorted(models), sorted(providers)

    try:
        return asyncio.run(_read())
    except Exception as exc:
        print(f"  (could not read back what served: {type(exc).__name__}: {exc})", file=sys.stderr)
        return [], []


def run_suite(
    junit_out: Path,
    extra_args: list[str] | None = None,
    engine: str = "",
    allow_fallback: bool = False,
) -> None:
    """Run the benchmarks, writing a JUnit report. Exit status is ignored on purpose."""
    cmd = [
        sys.executable, "-m", "pytest", BENCHMARK_PATH,
        "-m", "llm", "-q", f"--junit-xml={junit_out}",
    ]
    if extra_args:
        cmd.extend(extra_args)

    env = dict(os.environ)
    agreed = benchmark_db_url()
    if agreed:
        # Both ends on the identical DSN, so what the child records is what the
        # parent reads back. conftest honours TALA_TEST_DB_URL ahead of its own
        # sibling derivation.
        env["TALA_TEST_DB_URL"] = agreed

    if engine:
        # Pinned through the environment rather than by writing `is_forced`
        # into the provider row: a benchmark that mutates the configuration it
        # is measuring leaves the application pinned if the run dies partway.
        env["TALA_PIN_PROVIDER"] = engine
        if not allow_fallback:
            env["TALA_PIN_STRICT"] = "1"

    print(f"  running: {' '.join(cmd)}", flush=True)
    # A failing suite is the expected case -- the point is the score, not the
    # exit code, so the return value is deliberately not checked.
    subprocess.run(cmd, cwd=REPO_ROOT, env=env)


def _render_semantic_memory(run: SuiteRun) -> str:
    """Prose the graph can actually retrieve on, not a serialised table."""
    served = ", ".join(run.models_observed) or "nothing recorded"
    lines = [
        f"Agent benchmark run on {run.started_at}: {run.passed} of {run.total} "
        f"cases passed, a score of {run.score:.2f}.",
        "",
        f"Configuration: model {run.label}, git {run.git_sha}, "
        f"{len(run.adapters)} adapter(s) present. Total run time "
        f"{run.duration:.0f}s.",
        "",
        f"Engine requested: {run.engine_requested or 'not pinned'}. "
        f"Actually served by: {served}. The second is observed from what the "
        f"harness recorded while generating; the first is only what was asked "
        f"for, and the generation path falls through to another provider when "
        f"one fails.",
        "",]
    warning = run.attribution_warning()
    if warning:
        lines += [f"ATTRIBUTION WARNING: {warning}", ""]
    lines += [
        "This is a measurement of whether Tala can complete real autonomous "
        "goals, not a health check of the code. The suite is expected to sit "
        "below full marks and to move as the agent, the model or the prompts "
        "change; the number exists so that a change can be shown to help or "
        "not, rather than argued about.",
        "",
        "Case results:",
    ]
    for case in run.cases:
        verdict = "passed" if case.passed else "FAILED"
        line = f"- {case.name}: {verdict} in {case.seconds:.0f}s"
        if case.detail:
            line += f" -- {case.detail}"
        lines.append(line)

    return "\n".join(lines)


def write_run_node(run: SuiteRun) -> dict[str, Any]:
    """Record the run as an HTML-NN so it is recallable rather than a side file."""
    from sdk.agent.documentation_nodes import create_documentation_node

    stamp = run.started_at.replace(":", "").replace("-", "").replace("T", "-")[:15]
    node_id = f"benchmark-run-{stamp}"

    return create_documentation_node(
        node_id=node_id,
        title=f"Benchmark run {run.started_at[:16]} — {run.passed}/{run.total}",
        doc_target=DOC_TARGET,
        node_type="documentation",
        semantic_memory=_render_semantic_memory(run),
        source_files=[f"{BENCHMARK_PATH}/test_agent_benchmarks.py"],
        tags=["benchmark", "fitness", "self-improvement", "measurement"],
        state_extra={
            "benchmark_run": {
                "score": round(run.score, 4),
                "passed": run.passed,
                "total": run.total,
                "started_at": run.started_at,
                "duration_seconds": round(run.duration, 1),
                "repeats": run.repeats,
                "per_case": [
                    {"name": n, "passes": p, "attempts": a, "seconds": round(sec, 1)}
                    for n, p, a, sec in run.per_case()
                ],
                "model_label": run.label,
                "engine_requested": run.engine_requested,
                "models_observed": run.models_observed,
                "providers_observed": run.providers_observed,
                "attribution_warning": run.attribution_warning(),
                "uncommitted": run.uncommitted,
                "git_sha": run.git_sha,
                "adapters": run.adapters,
                "cases": [
                    {
                        "name": c.name,
                        "passed": c.passed,
                        "seconds": round(c.seconds, 1),
                        "detail": c.detail,
                    }
                    for c in run.cases
                ],
            }
        },
    )


def prune_run_nodes(keep: int) -> list[str]:
    """
    Keep the newest ``keep`` run nodes, delete the rest. Returns what went.

    Run records land in the same flat directory activation globs, so they
    compete for recall against actual knowledge. One per benchmark is fine at
    three and noise at fifty: a graph where the most retrievable thing about
    multi-file edits is forty near-identical score sheets is worse at answering
    than one with a handful.

    Node ids are timestamped, so lexical order is chronological.
    """
    if keep <= 0:
        return []
    try:
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir

        paths = sorted(resolve_active_nodes_dir().glob("benchmark-run-*.html"))
    except Exception:
        return []

    removed: list[str] = []
    for path in paths[:-keep] if len(paths) > keep else []:
        try:
            path.unlink()
            removed.append(path.name)
        except Exception:
            continue
    return removed


def refresh_architecture_contract() -> bool:
    """
    Regenerate the contract after the node count changes.

    Writing a run node changes how many nodes exist, and the contract states
    that number -- so every benchmark left it stale and failed
    test_it_is_measured_not_asserted until someone re-ran the generator by
    hand. The tool that moved the count is the right one to put it back;
    a contract that has drifted is worse than none, because it is believed.
    """
    try:
        result = subprocess.run(
            [sys.executable, "scripts/generate_architecture_contract.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        return result.returncode == 0
    except Exception:
        return False


def load_previous_runs() -> list[dict[str, Any]]:
    """Every prior run record, oldest first, for comparison."""
    try:
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir
        from sdk.agent.parser import parse_file
    except Exception:
        return []

    runs: list[dict[str, Any]] = []
    try:
        paths = sorted(resolve_active_nodes_dir().glob("benchmark-run-*.html"))
    except Exception:
        return []

    for path in paths:
        try:
            state = parse_file(str(path)).state_data or {}
        except Exception:
            continue
        record = state.get("benchmark_run")
        if not isinstance(record, dict) or "score" not in record:
            continue

        # A run whose engine could not be established is kept on disk as a
        # record of what happened, but never returned as a baseline. The first
        # run this suite ever produced scored 0/6 because the embedded provider
        # raised before generating a token; comparing anything against it would
        # read as an enormous improvement over a number that measured a crash.
        if str(record.get("attribution_warning", "") or "").strip():
            continue

        runs.append(record)

    runs.sort(key=lambda r: str(r.get("started_at", "")))
    return runs


def print_report(run: SuiteRun, previous: list[dict[str, Any]]) -> None:
    print()
    print(f"  Benchmark score: {run.passed}/{run.total}  ({run.score:.0%})")
    print(f"  Model: {run.label}   git: {run.git_sha}   time: {run.duration:.0f}s")
    if run.engine_requested:
        print(f"  Engine requested: {run.engine_requested}")
    print(f"  Actually served by: {', '.join(run.models_observed) or 'nothing recorded'}")
    warning = run.attribution_warning()
    if warning:
        print(f"  ** ATTRIBUTION WARNING: {warning}")
    if run.adapters:
        print(f"  Adapters present: {', '.join(run.adapters)}")
    print()

    for name, passes, attempts, seconds in run.per_case():
        if attempts == 1:
            mark = "PASS" if passes else "FAIL"
            print(f"    [{mark}] {name}  ({seconds:.0f}s)")
        else:
            print(f"    [{passes}/{attempts}] {name}  ({seconds:.0f}s total)")
        if passes < attempts:
            detail = next(
                (c.detail for c in run.cases if c.name == name and not c.passed and c.detail),
                "",
            )
            if detail:
                print(f"           {detail.splitlines()[0][:110]}")

    if previous:
        last = previous[-1]
        delta = run.score - float(last.get("score", 0.0))
        arrow = "no change" if abs(delta) < 1e-9 else (f"up {delta:+.0%}" if delta > 0 else f"down {delta:+.0%}")
        print()
        print(
            f"  Against the previous run ({last.get('started_at', '?')[:16]}, "
            f"{last.get('passed')}/{last.get('total')} on {last.get('model_label', '?')}): {arrow}"
        )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="", help="name of the configuration under test (model id)")
    parser.add_argument("--junit", default="", help="score an existing JUnit XML instead of running")
    parser.add_argument("--no-write", action="store_true", help="print the score, record nothing")
    parser.add_argument("--compare", action="store_true", help="list previous runs and exit")
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="record a run from a modified working tree; the result says so and is "
             "never offered as a comparison baseline",
    )
    parser.add_argument(
        "--keep", type=int, default=10, metavar="N",
        help="how many run nodes to retain (0 disables pruning); they share the "
             "directory activation globs, so they accumulate as retrieval noise",
    )
    parser.add_argument(
        "--repeat", type=int, default=1, metavar="N",
        help="run the suite N times and score the mean pass rate; n=1 cannot "
             "separate a real change from run-to-run variance",
    )
    parser.add_argument(
        "--engine", default="",
        help="pin generation to one provider id or name (e.g. embedded-builtin) for this run only",
    )
    parser.add_argument(
        "--allow-fallback", action="store_true",
        help="with --engine, permit falling back to the embedded engine; the score is then not attributable",
    )
    parser.add_argument("--pytest-arg", action="append", default=[], help="extra argument passed to pytest")
    args = parser.parse_args()

    if args.compare:
        previous = load_previous_runs()
        if not previous:
            print("No recorded benchmark runs yet.")
            return 0
        print(f"{'when':18} {'score':>7}  {'cases':>7}  model")
        for record in previous:
            print(
                f"{str(record.get('started_at',''))[:16]:18} "
                f"{float(record.get('score',0)):>6.0%}  "
                f"{record.get('passed')}/{record.get('total'):<5} "
                f"{record.get('model_label','?')}"
            )
        return 0

    # Refused before the run, not after: the point is to save the hour, not to
    # annotate a wasted one.
    dirty = uncommitted_files()
    if dirty and not args.allow_dirty and not args.junit:
        print(
            "The working tree is not clean, so a recorded sha would not describe "
            "the code under test:",
            file=sys.stderr,
        )
        for path in dirty[:10]:
            print(f"  {path}", file=sys.stderr)
        if len(dirty) > 10:
            print(f"  ... and {len(dirty) - 10} more", file=sys.stderr)
        print(
            "\nCommit them, stash them, or pass --allow-dirty to record the run "
            "with that stated on it.",
            file=sys.stderr,
        )
        return 2

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.junit:
        junit_path = Path(args.junit)
        if not junit_path.exists():
            print(f"No such JUnit report: {junit_path}", file=sys.stderr)
            return 2
        cases = parse_junit(junit_path)
    else:
        repeats = max(1, int(args.repeat))
        cases = []
        with tempfile.TemporaryDirectory() as tmp:
            for attempt in range(1, repeats + 1):
                junit_path = Path(tmp) / f"benchmarks-{attempt}.xml"
                print(
                    f"Running the agent benchmarks against the live model "
                    f"({attempt} of {repeats}). This takes a while."
                )
                run_suite(junit_path, args.pytest_arg, engine=args.engine,
                          allow_fallback=args.allow_fallback)
                if not junit_path.exists():
                    print(
                        f"pytest produced no JUnit report on attempt {attempt} -- "
                        f"the run did not start.",
                        file=sys.stderr,
                    )
                    continue
                cases.extend(parse_junit(junit_path))

    if not cases:
        print(
            "The report contains no test cases. This usually means every "
            "benchmark was deselected -- they are marked `llm`. Nothing scored.",
            file=sys.stderr,
        )
        return 2

    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    models_observed, providers_observed = ([], [])
    if not args.junit:
        # Only meaningful for a run this invocation performed; scoring somebody
        # else's report cannot claim to know what served it.
        models_observed, providers_observed = observe_models_served(started_at, finished_at)

    run = SuiteRun(
        cases=cases,
        label=args.label or "unspecified",
        git_sha=_git_sha(),
        adapters=_discover_adapters(),
        started_at=started_at,
        engine_requested=args.engine,
        models_observed=models_observed,
        providers_observed=providers_observed,
        # Empty when scoring a report this invocation did not produce: that
        # run happened elsewhere, possibly on another machine, and the state of
        # this tree says nothing about it. Claiming otherwise would put a
        # warning on a result it cannot describe.
        uncommitted=[] if args.junit else dirty,
        repeats=max(1, int(args.repeat)) if not args.junit else 1,
    )

    previous = load_previous_runs()
    print_report(run, previous)

    if args.no_write:
        print("  --no-write: nothing recorded.")
        return 0

    try:
        result = write_run_node(run)
    except Exception as exc:
        # The score is the deliverable; failing to file it must not look like a
        # failed benchmark run, and must not be silent either.
        print(
            f"  Scored, but the run node was NOT written ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return 1

    print(f"  Recorded as {result.get('node_id') or result.get('path') or 'a run node'}")

    removed = prune_run_nodes(int(args.keep))
    if removed:
        print(f"  Pruned {len(removed)} older run node(s), keeping the newest {args.keep}.")

    # The node count just changed, and the architecture contract states it.
    if not refresh_architecture_contract():
        print(
            "  The architecture contract could not be regenerated; it now "
            "understates the node count. Run scripts/generate_architecture_contract.py.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
