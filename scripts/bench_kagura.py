"""
scripts/bench_kagura.py -- measure the main model on Kagura's 6 GB GPU, to choose its context size and offload.

Starts llama-server directly on its own port (never the harness, never the production port, never a process it did not
start) and, for each configuration, records what a turn costs: VRAM, time to first token, prefill speed and decode speed on a
short and on a long prompt. The settings varied are the ones that trade context for speed:

* ``--ctx-size``     how much working memory she has
* ``--n-cpu-moe``    how many layers' experts stay in system RAM (Kagura has 128 GB: more offload frees VRAM for context)
* KV cache type      f16 or q8_0 (q8_0 halves the cache; it needs flash attention)
* flash attention    auto (the build decides), or forced on

Stage 1 sweeps ``--n-cpu-moe`` 16..24 and both KV types at 16k, plus the current production setting as the baseline.
Stage 2 takes the two best and tries 4k / 8k / 16k. ``--stage all`` runs both. The winner is the highest context up to 16k that
still decodes at >= 15 tok/s, ranked by the time a representative turn takes (first token of an 8k prompt plus 200 tokens).

Results go to D: (``D:\\tala-audit\\bench`` by default). It refuses to run when too little VRAM is free, because a number taken
beside a game or another model describes that, not this setting; every row records the free VRAM and CPU load it ran under.

    python scripts/bench_kagura.py --stage all --model D:\\ollama\\models\\blobs\\sha256-...
    python scripts/bench_kagura.py --stage 1 --dry-run        # list what would run
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PORT = 28099           # never the harness's own 8089/8090
MIN_CTX, TARGET_CTX, MIN_TPS = 8192, 16384, 15.0
#: A setting that uses more than this leaves too little of a 6 GB card for the desktop: --n-cpu-moe 16 (4.4 GB) fell off a cliff.
DEFAULT_VRAM_CAP_MB = 4100
ANSWER_TOKENS = 200            # what a representative turn's reply is assumed to be
MOE_RANGE = (16, 18, 20, 22, 24)
STAGE2_CTX = (4096, 8192, 16384)
STAGE3_UBATCH = (1024, 2048, 4096)


# --- what is run (pure; unit-tested) -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    ctx: int
    moe: int
    kv: str = "f16"
    fa: str = "auto"
    baseline: bool = False
    ub: int | None = None  # micro-batch (-ub/-b); None leaves the build's default

    @property
    def key(self) -> str:
        return f"ctx{self.ctx}-moe{self.moe}-kv{self.kv}-fa-{self.fa}" + (f"-ub{self.ub}" if self.ub else "")


def flash_attn_args(help_text: str, mode: str) -> list[str]:
    """The flash-attention flag for this build. Newer builds take a value (``-fa on|off|auto``), older ones a bare ``-fa``."""
    if mode == "auto" or "--flash-attn" not in help_text:
        return []
    if "--flash-attn [" in help_text:
        return ["-fa", mode]
    return ["-fa"] if mode == "on" else []


def kv_args(kv: str) -> list[str]:
    if kv == "f16":
        return []
    if kv == "q8_0":
        return ["-ctk", "q8_0", "-ctv", "q8_0"]
    raise ValueError(f"unsupported KV cache type {kv!r}")


def parse_key(key: str) -> Config:
    m = re.fullmatch(r"ctx(\d+)-moe(\d+)-kv(\w+?)-fa-(\w+?)(?:-ub(\d+))?", key)
    if not m:
        raise ValueError(f"not a configuration key: {key!r}")
    return Config(int(m[1]), int(m[2]), m[3], m[4], ub=int(m[5]) if m[5] else None)


def build_configs(stage: int, winners: list[Config] | None = None) -> list[Config]:
    if stage == 1:
        grid = [Config(TARGET_CTX, moe, kv, "on") for kv in ("f16", "q8_0") for moe in MOE_RANGE]
        return [Config(TARGET_CTX, 20, "f16", "auto", baseline=True), *grid]
    if stage == 2:
        if not winners:
            raise ValueError("stage 2 needs the stage 1 winners")
        return [Config(ctx, w.moe, w.kv, w.fa) for w in winners for ctx in STAGE2_CTX]
    if stage == 3:
        if not winners:
            raise ValueError("stage 3 needs a setting to refine")
        return [Config(w.ctx, w.moe, w.kv, w.fa, ub=ub) for w in winners[:1] for ub in STAGE3_UBATCH]
    raise ValueError(f"unknown stage {stage}")


def prompt_sizes(ctx: int) -> list[int]:
    """A short and a long prompt, both leaving room for the reply inside the context."""
    limit = ctx - 768
    return sorted({min(2000, limit), min(8000, limit)})


def server_argv(exe: str, model: str, port: int, cfg: Config, profile_args: Iterable[str], help_text: str) -> list[str]:
    """Production's command line (see llama_server_manager._spawn), with only the flags under test replaced."""
    kept: list[str] = []
    skip = False
    for arg in profile_args:
        if skip:
            skip = False
        elif arg == "--n-cpu-moe":
            skip = True
        else:
            kept.append(arg)
    return [exe, "-m", model, "--port", str(port), "--host", "127.0.0.1", "--ctx-size", str(cfg.ctx), "--jinja",
            *kept, "--n-cpu-moe", str(cfg.moe), *kv_args(cfg.kv), *flash_attn_args(help_text, cfg.fa),
            *(["-ub", str(cfg.ub), "-b", str(cfg.ub)] if cfg.ub else [])]


# --- reading a streamed reply (pure) -------------------------------------------------------------------------------------------------------


def parse_stream(events: Iterable[tuple[float, dict[str, Any]]], started: float) -> dict[str, Any]:
    """Time to first token and token rates from ``(arrival_time, sse_object)`` pairs.

    The server's own ``timings`` (sent on the last chunk) are used when present; otherwise the rate is estimated from the
    chunk arrival times (one chunk is about one token)."""
    first = last = None
    texts = 0
    timings: dict[str, Any] = {}
    for t, obj in events:
        if obj.get("timings"):
            timings = obj["timings"]
        delta = ((obj.get("choices") or [{}])[0]).get("delta") or {}
        if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
            first = t if first is None else first
            last = t
            texts += 1
    if first is None:
        return {"ok": False}
    decode = timings.get("predicted_per_second")
    if decode is None and texts > 1 and last > first:
        decode = (texts - 1) / (last - first)
    return {"ok": decode is not None, "ttft_s": round(first - started, 3), "decode_tps": decode,
            "prefill_tps": timings.get("prompt_per_second"), "prompt_tokens": timings.get("prompt_n"),
            "generated_tokens": timings.get("predicted_n", texts)}


def median(values: list[float]) -> float:
    return float(statistics.median(values))


def turn_latency_s(ttft_s: float, decode_tps: float, answer_tokens: int = ANSWER_TOKENS) -> float:
    return ttft_s + answer_tokens / decode_tps


def rank(rows: list[dict[str, Any]], *, min_ctx: int = MIN_CTX, target_ctx: int = TARGET_CTX, min_tps: float = MIN_TPS,
         vram_budget_mb: int | None = None) -> list[dict[str, Any]]:
    """Acceptable rows, best first: reach the target context, then the quickest representative turn."""
    ok = [r for r in rows if r.get("ok") and r["ctx"] >= min_ctx and (r.get("decode_tps") or 0) >= min_tps
          and (vram_budget_mb is None or r.get("vram_mb") is None or r["vram_mb"] <= vram_budget_mb)]
    return sorted(ok, key=lambda r: (-min(r["ctx"], target_ctx), turn_latency_s(r["ttft_big_s"], r["decode_tps"])))


def choose_best(rows: list[dict[str, Any]], **kw: Any) -> dict[str, Any] | None:
    ranked = rank(rows, **kw)
    return ranked[0] if ranked else None


def refuse_c_drive(path: Path) -> None:
    if str(Path(path).resolve()).upper().startswith("C:"):
        raise SystemExit(f"refusing to write to {path}: results, caches and logs go on D: only")


# --- running one configuration (needs the GPU) ------------------------------------------------------------------------------------------


def _nvidia(field: str) -> int:
    out = subprocess.run(["nvidia-smi", f"--query-gpu={field}", "--format=csv,noheader,nounits"], capture_output=True, text=True)
    return int(out.stdout.strip().splitlines()[0])


def vram_used_mb() -> int:
    return _nvidia("memory.used")


def vram_free_mb() -> int:
    return _nvidia("memory.free")


def _post(base: str, path: str, body: dict[str, Any], timeout: float = 900.0) -> dict[str, Any]:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/health", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _filler(repo: Path) -> str:
    parts = [p.read_text(encoding="utf-8", errors="ignore") for p in sorted((repo / "docs").glob("*.md"))[:12]]
    return "\n\n".join(parts) or "The quick brown fox jumps over the lazy dog. " * 400


def prompt_of(base: str, filler: str, tokens: int) -> str:
    """Text that tokenises to about ``tokens`` tokens, calibrated against the server's own tokenizer."""
    sample = filler[:20000]
    n = len(_post(base, "/tokenize", {"content": sample}).get("tokens", [])) or 1
    chars = int(len(sample) / n * tokens)
    text = (filler * (chars // max(1, len(filler)) + 1))[:chars]
    got = len(_post(base, "/tokenize", {"content": text}).get("tokens", [])) or 1
    return (filler * (int(chars * tokens / got) // max(1, len(filler)) + 1))[:int(chars * tokens / got)]


def stream_chat(base: str, text: str, n_predict: int) -> dict[str, Any]:
    body = {"model": "local", "messages": [{"role": "user", "content": text}], "max_tokens": n_predict, "temperature": 0.0,
            "stream": True, "cache_prompt": False, "chat_template_kwargs": {"reasoning_effort": "low"}}
    req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    events: list[tuple[float, dict[str, Any]]] = []
    with urllib.request.urlopen(req, timeout=900) as response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                try:
                    events.append((time.perf_counter(), json.loads(line[5:])))
                except ValueError:
                    continue
    return parse_stream(events, started=started)


def run_config(cfg: Config, *, exe: str, model: str, profile_args: tuple[str, ...], port: int, out: Path, trials: int,
               n_predict: int, help_text: str, filler: str, startup_s: float = 600.0) -> dict[str, Any]:
    import psutil

    base = f"http://127.0.0.1:{port}"
    row: dict[str, Any] = {"config": cfg.key, "baseline": cfg.baseline, "ctx": cfg.ctx, "moe": cfg.moe, "kv": cfg.kv, "fa": cfg.fa,
                           "free_vram_mb_before": vram_free_mb(), "cpu_pct_before": psutil.cpu_percent(interval=1.0)}
    used_before = vram_used_mb()
    argv = server_argv(exe, model, port, cfg, profile_args, help_text)
    log = open(out / f"server-{cfg.key}.log", "wb")
    started = time.perf_counter()
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
    try:
        while time.perf_counter() - started < startup_s:
            if proc.poll() is not None:
                row.update(ok=False, error=f"llama-server exited with {proc.returncode} (see {log.name})")
                return row
            if _healthy(base):
                break
            time.sleep(2)
        else:
            row.update(ok=False, error="did not become healthy in time")
            return row
        row["load_s"] = round(time.perf_counter() - started, 1)
        row["vram_after_load_mb"] = vram_used_mb() - used_before
        stream_chat(base, "Say hello.", 8)  # warm-up: first request pays one-off costs
        sizes = prompt_sizes(cfg.ctx)
        row["by_prompt"] = {}
        for size in sizes:
            text = prompt_of(base, filler, size)
            runs = [stream_chat(base, text, n_predict) for _ in range(trials)]
            good = [r for r in runs if r.get("ok")]
            if not good:
                row.update(ok=False, error=f"no tokens for a {size}-token prompt")
                return row
            row["by_prompt"][str(size)] = {
                "ttft_s": round(median([r["ttft_s"] for r in good]), 2),
                "prefill_tps": round(median([r["prefill_tps"] for r in good if r.get("prefill_tps")] or [0.0]), 1),
                "decode_tps": round(median([r["decode_tps"] for r in good]), 2), "runs": len(good)}
        big = row["by_prompt"][str(sizes[-1])]
        row.update(ok=True, prompt_big=sizes[-1], ttft_big_s=big["ttft_s"], decode_tps=big["decode_tps"],
                   prefill_tps=big["prefill_tps"], vram_mb=vram_used_mb() - used_before)
        return row
    except Exception as exc:  # one broken configuration must not end the sweep
        row.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        return row
    finally:
        proc.terminate()  # our own child only; nothing else is ever stopped
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        time.sleep(4)


def markdown(rows: list[dict[str, Any]], best: dict[str, Any] | None) -> str:
    lines = ["| config | ok | VRAM MB | load s | TTFT 2k | TTFT big | prefill tok/s | decode tok/s | turn s |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        mark = " (baseline)" if r.get("baseline") else ""
        if not r.get("ok"):
            lines.append(f"| {r['config']}{mark} | no: {r.get('error', '')[:60]} | | | | | | | |")
            continue
        small = r["by_prompt"][min(r["by_prompt"], key=int)]
        lines.append(f"| {r['config']}{mark}{' **best**' if best and r['config'] == best['config'] else ''} | yes | {r['vram_mb']} | "
                     f"{r.get('load_s')} | {small['ttft_s']} | {r['ttft_big_s']} | {r['prefill_tps']} | {r['decode_tps']} | "
                     f"{turn_latency_s(r['ttft_big_s'], r['decode_tps']):.1f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=["1", "2", "3", "all"], default="all")
    ap.add_argument("--from-key", default=None, help="stage 3: the configuration to refine (a key from an earlier run)")
    ap.add_argument("--vram-cap-mb", type=int, default=DEFAULT_VRAM_CAP_MB, help="never choose a setting using more VRAM than this")
    ap.add_argument("--profile", default="kagura")
    ap.add_argument("--model", default=None, help="GGUF to load (default: the profile's, else TALA_MODEL_PATH)")
    ap.add_argument("--server-bin", default=None)
    ap.add_argument("--out", default=r"D:\tala-audit\bench")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--n-predict", type=int, default=64)
    ap.add_argument("--min-free-mb", type=int, default=4500, help="refuse to run with less VRAM free than this")
    ap.add_argument("--only", default=None, help="run just the configuration with this key")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    out = Path(args.out)
    refuse_c_drive(out)
    for var in ("TEMP", "TMP", "HF_HOME"):
        import os
        if os.environ.get(var, "").upper().startswith("C:"):
            raise SystemExit(f"refusing to run with {var}={os.environ[var]}: point it at D: first")

    from python.inference import profiles

    profile = profiles.get(args.profile)
    if profile is None:
        raise SystemExit(f"no profile named {args.profile!r}")
    exe = args.server_bin or profile.main.server_bin
    import os
    model = args.model or (profile.main.model_path if Path(profile.main.model_path).is_file() else os.environ.get("TALA_MODEL_PATH", ""))
    if not args.dry_run and (not Path(exe).is_file() or not Path(model).is_file()):
        raise SystemExit(f"need a llama-server ({exe}) and a model ({model or 'none given: pass --model'})")

    stages = [1, 2, 3] if args.stage == "all" else [int(args.stage)]
    if args.dry_run:
        for s in stages:
            for cfg in build_configs(s, winners=[Config(TARGET_CTX, 20, "f16", "on")] if s >= 2 else None):
                print(f"stage {s}: {cfg.key}{'  (baseline)' if cfg.baseline else ''}  prompts {prompt_sizes(cfg.ctx)}")
        return 0

    free = vram_free_mb()
    if free < args.min_free_mb:
        print(f"only {free} MB of VRAM is free (need {args.min_free_mb}). Close what is using the GPU (games, other models) and retry; "
              "a number measured beside them describes them, not this setting.")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    help_text = subprocess.run([exe, "--help"], capture_output=True, text=True).stdout
    filler = _filler(repo)
    rows: list[dict[str, Any]] = []
    jsonl = out / f"bench-{stamp}.jsonl"

    def run_all(configs: list[Config]) -> list[dict[str, Any]]:
        done = []
        for cfg in configs:
            if args.only and cfg.key != args.only:
                continue
            print(f"== {cfg.key}", flush=True)
            r = run_config(cfg, exe=exe, model=model, profile_args=profile.main.args, port=args.port, out=out, trials=args.trials,
                           n_predict=args.n_predict, help_text=help_text, filler=filler)
            print(json.dumps({k: v for k, v in r.items() if k != "by_prompt"}), flush=True)
            with open(jsonl, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(r) + "\n")
            done.append(r)
        return done

    winners: list[Config] = [parse_key(args.from_key)] if args.from_key else []
    for s in stages:
        if s == 1:
            rows += run_all(build_configs(1))
            winners = [Config(r["ctx"], r["moe"], r["kv"], r["fa"]) for r in
                       rank([r for r in rows if not r.get("baseline")], vram_budget_mb=args.vram_cap_mb)[:2]]
            print("stage 1 winners:", [w.key for w in winners], flush=True)
        else:
            if not winners:
                print(f"stage {s} has no winners to refine (nothing in stage 1 cleared the bar)")
                continue
            rows += run_all(build_configs(s, winners=winners))
    best = choose_best(rows, vram_budget_mb=min(profile.vram_total_mb - 1024, args.vram_cap_mb))
    report = markdown(rows, best)
    (out / f"bench-{stamp}.md").write_text(report, encoding="utf-8")
    print(report)
    print("\nBEST:", best["config"] if best else "none cleared the bar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
