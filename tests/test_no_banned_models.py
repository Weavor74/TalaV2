"""
US-origin base models only: no Qwen, DeepSeek or Mistral anywhere in the live
code, config, docs or shipped data.

This is the one place that spells the banned names, because it has to. The
exceptions are records that must keep naming what they record, not live
configuration: the hash-chained ledger, historical benchmark runs, and the
migration that retires database rows still on a banned model.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BANNED = re.compile(r"qwen|deepseek|mistral", re.IGNORECASE)

#: Known gap, not an exemption: the assistant that wrote this change cannot read
#: or edit .env* files, so `.env.example`'s VLLM_MODEL line still needs the
#: owner to set it to meta-llama/Llama-3.2-3B-Instruct (config.py already
#: defaults to that). Delete this entry once that line is changed.
PENDING_OWNER_EDIT = (".env.example",)

#: The tracked synthetic story corpus records which model wrote each story in its
#: `created_by` provenance line. That is a true record, and rewriting it would
#: falsify the data's origin, so only that one line is allowed to name a model.
PROVENANCE_ONLY_PREFIXES = ("data/training_corpus/",)

ALLOWED_PREFIXES = (
    "blockchain/data/",                    # hash-chained ledger: cannot be rewritten
    "nodes/benchmark-run-",                # historical measurement records
    "python/harness/db_ddl_extra.py",      # migration that must name Qwen to retire it
    "docs/reference/",                     # generated snapshots of older source
    "tests/test_no_banned_models.py",      # this file
    "tests/test_external_engine.py",       # asserts the names are absent
    "uv.lock",                             # a transitive dependency's package name, not a model
)


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True, timeout=60,
        ).stdout
    except Exception:
        pytest.skip("not a git checkout")
    return [line for line in out.splitlines() if line]


def test_no_banned_model_names_in_tracked_files():
    offenders: list[str] = []
    for rel in _tracked_files():
        if rel.startswith(ALLOWED_PREFIXES) or rel in PENDING_OWNER_EDIT:
            continue
        path = REPO / rel
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        provenance_only = rel.startswith(PROVENANCE_ONLY_PREFIXES)
        for lineno, line in enumerate(text.splitlines(), 1):
            if provenance_only and '"created_by"' in line:
                continue
            if BANNED.search(line):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, "banned model names found:\n" + "\n".join(offenders[:40])


def test_the_qwen_adapter_is_gone():
    assert not (REPO / "nodes" / "corpus-tala-training-stories_lora").exists()


def test_profiles_name_only_us_origin_models():
    from python.inference import profiles

    for name in profiles.available():
        assert not BANNED.search(str(profiles.get(name).summary())), name
