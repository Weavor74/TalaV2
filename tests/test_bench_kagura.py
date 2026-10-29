"""
scripts/bench_kagura.py -- the logic around the measurements, not the measurements themselves.

The benchmark starts llama-server directly on its own port and varies the context size, how many MoE layers stay on the CPU,
the KV cache type and flash attention. What can be tested without a GPU is everything that decides what is run and how the
answer is read: the command line (it must match production's except for the flags under test), the flash-attention flag
(its syntax differs between llama.cpp builds), how a streamed reply becomes time-to-first-token and tokens per second, and
the rule that picks the winner.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_kagura.py"
spec = importlib.util.spec_from_file_location("bench_kagura", SCRIPT)
bk = importlib.util.module_from_spec(spec)
sys.modules["bench_kagura"] = bk  # dataclasses look the module up by name
spec.loader.exec_module(bk)

NEW_HELP = "-fa,   --flash-attn [on|off|auto]       set Flash Attention use ('on', 'off', or 'auto', default: 'auto')"
OLD_HELP = "-fa,   --flash-attn                      enable Flash Attention (default: disabled)"


# --- flags ----------------------------------------------------------------------------------------------------------------------


def test_flash_attention_uses_the_value_syntax_when_the_build_takes_one():
    assert bk.flash_attn_args(NEW_HELP, "on") == ["-fa", "on"]
    assert bk.flash_attn_args(NEW_HELP, "off") == ["-fa", "off"]


def test_flash_attention_uses_the_bare_flag_on_older_builds():
    assert bk.flash_attn_args(OLD_HELP, "on") == ["-fa"]
    assert bk.flash_attn_args(OLD_HELP, "off") == []


def test_auto_leaves_the_build_to_decide_and_passes_nothing():
    assert bk.flash_attn_args(NEW_HELP, "auto") == []


def test_a_build_without_the_flag_gets_none():
    assert bk.flash_attn_args("no such option", "on") == []


def test_the_default_kv_cache_adds_no_flags_and_q8_adds_both():
    assert bk.kv_args("f16") == []
    assert bk.kv_args("q8_0") == ["-ctk", "q8_0", "-ctv", "q8_0"]
    with pytest.raises(ValueError):
        bk.kv_args("q2_k")


# --- what is run -----------------------------------------------------------------------------------------------------------------


def test_stage_one_covers_the_requested_expert_offload_range_and_both_kv_types_at_16k_plus_the_baseline():
    configs = bk.build_configs(1)
    grid = [c for c in configs if not c.baseline]
    assert {c.moe for c in grid} == {16, 18, 20, 22, 24}
    assert {c.kv for c in grid} == {"f16", "q8_0"}
    assert {c.ctx for c in grid} == {16384}
    assert len(grid) == 10
    baselines = [c for c in configs if c.baseline]
    assert len(baselines) == 1 and (baselines[0].ctx, baselines[0].moe, baselines[0].kv, baselines[0].fa) == (16384, 20, "f16", "auto")


def test_stage_two_tries_the_contexts_on_the_given_winners():
    winners = [bk.Config(16384, 20, "q8_0", "on"), bk.Config(16384, 22, "f16", "on")]
    configs = bk.build_configs(2, winners=winners)
    assert {(c.ctx, c.moe, c.kv) for c in configs} == {(ctx, w.moe, w.kv) for w in winners for ctx in (4096, 8192, 16384)}


def test_every_config_has_a_unique_readable_key():
    keys = [c.key for c in bk.build_configs(1)]
    assert len(set(keys)) == len(keys)
    assert bk.Config(16384, 20, "q8_0", "on").key == "ctx16384-moe20-kvq8_0-fa-on"


def test_prompt_sizes_never_exceed_the_context():
    for ctx in (4096, 8192, 16384, 32768):
        for size in bk.prompt_sizes(ctx):
            assert size + 256 < ctx
    assert bk.prompt_sizes(16384) == [2000, 8000]
    assert max(bk.prompt_sizes(4096)) < 4096


def test_the_command_line_matches_production_except_for_the_flags_under_test():
    profile_args = ("-ngl", "99", "--n-cpu-moe", "20")
    argv = bk.server_argv("S.exe", "M.gguf", 28099, bk.Config(8192, 22, "q8_0", "on"), profile_args, NEW_HELP)
    assert argv[:2] == ["S.exe", "-m"] and "M.gguf" in argv
    assert argv[argv.index("--port") + 1] == "28099" and argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--ctx-size") + 1] == "8192" and "--jinja" in argv
    assert argv[argv.index("--n-cpu-moe") + 1] == "22", "the profile's own value is replaced, not duplicated"
    assert argv.count("--n-cpu-moe") == 1 and argv.count("-ngl") == 1
    assert argv[argv.index("-ctk") + 1] == "q8_0" and argv[argv.index("-fa") + 1] == "on"


def test_the_benchmark_never_uses_the_production_port():
    assert bk.DEFAULT_PORT not in (8089, 8090)


# --- reading a streamed reply ------------------------------------------------------------------------------------------------------


def events(*pairs):
    return list(pairs)


def test_time_to_first_token_is_the_first_chunk_that_carries_text_or_reasoning():
    stream = events((0.0, {"choices": [{"delta": {"role": "assistant"}}]}),
                    (2.5, {"choices": [{"delta": {"reasoning_content": "Thinking"}}]}),
                    (3.0, {"choices": [{"delta": {"content": "Hi"}}]}))
    assert bk.parse_stream(stream, started=0.0)["ttft_s"] == 2.5


def test_the_servers_own_timings_are_used_when_it_reports_them():
    stream = events((1.0, {"choices": [{"delta": {"content": "a"}}]}),
                    (5.0, {"choices": [{"delta": {}}], "timings": {"prompt_n": 8000, "prompt_per_second": 620.5, "predicted_n": 64,
                                                                    "predicted_per_second": 19.6}}))
    result = bk.parse_stream(stream, started=0.0)
    assert result["prefill_tps"] == 620.5 and result["decode_tps"] == 19.6
    assert result["prompt_tokens"] == 8000 and result["generated_tokens"] == 64


def test_without_server_timings_the_rate_is_estimated_from_the_chunks():
    stream = events((1.0, {"choices": [{"delta": {"content": "a"}}]}), (2.0, {"choices": [{"delta": {"content": "b"}}]}),
                    (3.0, {"choices": [{"delta": {"content": "c"}}]}))
    result = bk.parse_stream(stream, started=0.0)
    assert result["decode_tps"] == pytest.approx(1.0, rel=0.01)


def test_a_reply_with_no_tokens_reports_a_failure_rather_than_zeros():
    result = bk.parse_stream(events((1.0, {"choices": [{"delta": {}}]})), started=0.0)
    assert result["ok"] is False


def test_the_median_of_trials_ignores_one_slow_outlier():
    assert bk.median([19.6, 19.8, 4.0]) == 19.6
    assert bk.median([1.0, 3.0]) == 2.0


# --- picking the winner --------------------------------------------------------------------------------------------------------------


def row(ctx, moe, kv, tps, ttft_big, vram=5000, ok=True, fa="on"):
    return {"ok": ok, "ctx": ctx, "moe": moe, "kv": kv, "fa": fa, "decode_tps": tps, "ttft_big_s": ttft_big, "vram_mb": vram,
            "config": bk.Config(ctx, moe, kv, fa).key}


def test_the_winner_reaches_the_target_context_before_anything_else():
    rows = [row(8192, 20, "f16", 22.0, 5.0), row(16384, 20, "f16", 19.0, 14.0)]
    assert bk.choose_best(rows)["ctx"] == 16384


def test_a_setting_below_the_minimum_speed_or_context_is_never_chosen():
    rows = [row(16384, 24, "f16", 12.0, 9.0), row(4096, 16, "f16", 25.0, 2.0)]
    assert bk.choose_best(rows) is None


def test_among_equals_the_faster_turn_wins():
    """A representative turn is the first token of an 8k prompt plus 200 tokens of answer."""
    rows = [row(16384, 20, "f16", 19.0, 14.0), row(16384, 22, "q8_0", 17.0, 8.0), row(16384, 24, "f16", 16.0, 7.0)]
    best = bk.choose_best(rows)
    assert (best["moe"], best["kv"]) == (24, "f16")
    assert bk.turn_latency_s(14.0, 19.0) == pytest.approx(14.0 + 200 / 19.0)


def test_failed_runs_are_not_candidates():
    rows = [row(16384, 20, "f16", 30.0, 1.0, ok=False), row(16384, 22, "f16", 17.0, 9.0)]
    assert bk.choose_best(rows)["moe"] == 22


def test_without_the_target_context_the_largest_acceptable_one_wins():
    rows = [row(8192, 20, "f16", 22.0, 5.0), row(12288, 22, "f16", 18.0, 9.0)]
    assert bk.choose_best(rows)["ctx"] == 12288


def test_a_row_that_uses_more_vram_than_is_free_is_flagged_not_hidden():
    rows = [row(16384, 20, "f16", 19.0, 14.0, vram=5900)]
    assert bk.choose_best(rows, vram_budget_mb=5500) is None


# --- where it writes -----------------------------------------------------------------------------------------------------------------


def test_it_refuses_to_write_to_the_c_drive(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: self)  # a drive letter only means something on Windows; elsewhere resolve() prefixes the cwd
    with pytest.raises(SystemExit):
        bk.refuse_c_drive(Path(r"C:\Users\someone\bench"))
    bk.refuse_c_drive(Path(r"D:\tala-audit\bench"))


def test_it_never_stops_a_process_it_did_not_start():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "taskkill" not in source and "kill_by_name" not in source and "psutil.process_iter" not in source
    assert "proc.terminate()" in source


# --- stage 3: the micro-batch size, which sets how well prefill amortises moving expert weights to the GPU ---------------------------------


def test_stage_three_tries_larger_micro_batches_on_the_chosen_setting():
    chosen = bk.Config(16384, 20, "f16", "on")
    configs = bk.build_configs(3, winners=[chosen])
    assert [c.ub for c in configs] == [1024, 2048, 4096]
    assert all((c.ctx, c.moe, c.kv, c.fa) == (16384, 20, "f16", "on") for c in configs)
    assert len({c.key for c in configs}) == 3


def test_a_micro_batch_adds_both_batch_flags_and_the_default_adds_none():
    base = bk.Config(16384, 20, "f16", "on")
    plain = bk.server_argv("S", "M", 1, base, ("-ngl", "99"), NEW_HELP)
    assert "-ub" not in plain and "-b" not in plain
    big = bk.server_argv("S", "M", 1, bk.Config(16384, 20, "f16", "on", ub=2048), ("-ngl", "99"), NEW_HELP)
    assert big[big.index("-ub") + 1] == "2048" and big[big.index("-b") + 1] == "2048"


def test_the_key_of_a_default_config_is_unchanged_and_a_batched_one_says_so():
    assert bk.Config(16384, 20, "f16", "on").key == "ctx16384-moe20-kvf16-fa-on"
    assert bk.Config(16384, 20, "f16", "on", ub=2048).key == "ctx16384-moe20-kvf16-fa-on-ub2048"


def test_a_key_can_be_turned_back_into_a_config():
    for config in (bk.Config(16384, 20, "q8_0", "on"), bk.Config(8192, 18, "f16", "auto"), bk.Config(16384, 20, "f16", "on", ub=2048)):
        assert bk.parse_key(config.key) == config
    with pytest.raises(ValueError):
        bk.parse_key("not-a-key")


# --- headroom: a setting that nearly fills the card sits on a cliff -----------------------------------------------------------------------


def test_a_setting_that_leaves_no_headroom_is_not_chosen_even_if_it_is_fastest():
    """--n-cpu-moe 16 collapsed to 50 s to first token when the card filled up; the desktop's own VRAM use varies by gigabytes."""
    fast_but_tight = row(16384, 18, "f16", 21.5, 17.8, vram=4417)
    safe = row(16384, 20, "f16", 20.2, 18.7, vram=3625)
    assert bk.choose_best([fast_but_tight, safe])["moe"] == 18
    assert bk.choose_best([fast_but_tight, safe], vram_budget_mb=4100)["moe"] == 20


def test_the_default_headroom_cap_is_below_the_cards_size_less_a_gigabyte():
    assert bk.DEFAULT_VRAM_CAP_MB <= 6144 - 1024
