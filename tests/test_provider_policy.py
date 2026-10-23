"""
Tala is local and US-origin only. The harness must enforce that, not hope the rows in the database comply.

The live dev database still held ``Kimi3`` (a remote Moonshot service, online, and tier-sorted AHEAD of the local
models), a Gemini remote row, and several Qwen-based local rows. A pytest run against it logged
"Attempting generation with provider 'Kimi3' (remote)". No request left the machine that time, but only because
the stored key was an encrypted dict and ``.strip()`` raised first -- an accident, not a rule. Separately, every
start-up "verified" each remote provider by sending ``GET <endpoint>/models`` with the stored API key, whether or
not that provider was ever used.

The rules, one place (python/harness/provider_policy.py):

* a provider from a non-US lab (Qwen, DeepSeek, Mistral, Kimi/Moonshot, ...) is never used, even if named;
* a remote provider is used only when the profile allows remote, TALA_ALLOW_REMOTE_PROVIDERS=1, or the caller
  named that provider by id;
* a refused provider is neither tried nor probed: no request, and no key, leaves the machine.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from python.harness import provider_policy, provider_selection


def row(pid, name, ptype, **config):
    return {"id": pid, "name": name, "type": ptype, "status": "online", "priority": 1, "config": config}


KIMI = row("prov-1", "Kimi3", "remote", endpoint="https://api.moonshot.cn/v1", model="kimi-k2", apiKey="k")
GEMINI = row("prov-2", "Gemini", "remote", service="gemini", model="gemini-flash", apiKey="k")
QWEN = row("ollama-qwen", "OLLAMA - huihui_ai/qwen3-abliterated:4b", "local", model="huihui_ai/qwen3-abliterated:4b")
GWEN = row("ollama-gwen", "OLLAMA - oknncelik/okse-ai-gwen3.5:2b", "local", model="oknncelik/okse-ai-gwen3.5:2b")
LLAMA = row("ollama-llama", "OLLAMA - llama3.1:8b", "local", model="llama3.1:8b")
EMBEDDED = row("embedded-builtin", "Tala Embedded Engine", "embedded", model="Llama-3.2-3B")


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    """No profile and no opt-in: the default."""
    monkeypatch.delenv("TALA_ALLOW_REMOTE_PROVIDERS", raising=False)
    monkeypatch.setattr(provider_policy.profiles, "active", lambda: None)


# --- the policy ------------------------------------------------------------------------------


@pytest.mark.parametrize("provider", [KIMI, QWEN, GWEN])
def test_a_non_us_model_is_refused_even_when_named(provider):
    assert provider_policy.refusal(provider, requested=False)
    assert provider_policy.refusal(provider, requested=True)


def test_a_non_us_model_is_refused_even_when_remote_is_allowed(monkeypatch):
    monkeypatch.setenv("TALA_ALLOW_REMOTE_PROVIDERS", "1")
    assert provider_policy.refusal(KIMI, requested=True)


@pytest.mark.parametrize("hint", [
    {"name": "Custom", "endpoint": "https://api.moonshot.ai/v1"},
    {"name": "Custom", "model": "deepseek-chat"},
    {"name": "Custom", "model": "Mistral-7B-Instruct"},
    {"name": "Custom", "model": "mixtral-8x7b"},
    {"name": "KIMI-K2", "model": "x"},
    {"name": "Custom", "model": "Qwen2.5-1.5B"},
])
def test_the_marker_is_found_in_the_name_the_model_or_the_endpoint(hint):
    provider = row("p", hint.pop("name"), "remote", **hint)
    assert "non-US" in provider_policy.refusal(provider, requested=True)


def test_the_marker_is_read_from_a_config_that_is_a_json_string():
    """JSONB columns hold a JSON *string* in this codebase; the policy must not skip those rows."""
    provider = dict(KIMI, config=json.dumps(KIMI["config"]))
    assert provider_policy.refusal(provider, requested=True)


@pytest.mark.parametrize("provider", [LLAMA, EMBEDDED, row("p", "OLLAMA - gemma3:4b", "local", model="gemma3:4b"),
                                      row("p", "OLLAMA - gpt-oss-heretic:20b", "local", model="gpt-oss-heretic:20b")])
def test_local_us_origin_providers_are_untouched(provider):
    assert provider_policy.refusal(provider, requested=False) is None


def test_a_remote_provider_is_refused_by_default():
    reason = provider_policy.refusal(GEMINI, requested=False)
    assert reason and "remote" in reason and "allow_remote" in reason


def test_naming_a_remote_provider_is_allowed():
    assert provider_policy.refusal(GEMINI, requested=True) is None


def test_the_profile_can_allow_remote(monkeypatch):
    monkeypatch.setattr(provider_policy.profiles, "active", lambda: SimpleNamespace(allow_remote=True))
    assert provider_policy.refusal(GEMINI, requested=False) is None


def test_a_local_only_profile_still_refuses_remote(monkeypatch):
    monkeypatch.setattr(provider_policy.profiles, "active", lambda: SimpleNamespace(allow_remote=False))
    assert provider_policy.refusal(GEMINI, requested=False)


def test_the_environment_can_allow_remote(monkeypatch):
    monkeypatch.setenv("TALA_ALLOW_REMOTE_PROVIDERS", "1")
    assert provider_policy.refusal(GEMINI, requested=False) is None


# --- the generation loops use it (all three go through skip_provider_attempt) ------------------------


def skip(provider, requested=None):
    return provider_selection.skip_provider_attempt(provider, None, requested)


def test_the_generate_loops_skip_remote_and_non_us_providers():
    assert skip(KIMI) is True
    assert skip(GEMINI) is True
    assert skip(QWEN) is True
    assert skip(LLAMA) is False
    assert skip(EMBEDDED) is False


def test_a_named_remote_provider_is_still_tried():
    assert skip(GEMINI, requested=GEMINI) is False


def test_a_named_non_us_provider_is_not():
    assert skip(KIMI, requested=KIMI) is True


def test_the_offline_rule_is_unchanged():
    offline = dict(LLAMA, status="offline")
    assert skip(offline) is True
    assert skip(offline, requested=offline) is False


# --- the start-up probe: no request, no key, for a refused provider ---------------------------------------


class _Spy:
    """A client factory that counts how often a request client was built. It counts rather than raising: the probe
    wraps its network code in try/except, so a raised AssertionError would be swallowed and the test would pass
    even though a request had been attempted (a mutation check caught exactly that)."""

    def __init__(self):
        self.built = 0

    def __call__(self, *a, **k):
        self.built += 1
        raise OSError("no network in tests")


class _Models:
    def __init__(self, ids, seen):
        self.ids, self.seen = ids, seen

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.seen.append((url, dict(headers or {})))
        return SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": i} for i in self.ids]})


def probe(provider, client, cache=None):
    from python.harness import provider_probe

    return asyncio.run(provider_probe.probe_remote_provider(
        provider, provider["config"], {} if cache is None else cache, lambda e: e.rstrip("/"), client))


def test_a_refused_remote_provider_is_never_contacted():
    spy = _Spy()
    assert probe(KIMI, spy) == "offline"
    assert probe(GEMINI, spy) == "offline"
    assert spy.built == 0, "a request client was built for a provider the policy refused"


def test_an_allowed_remote_provider_is_probed_and_reported_online(monkeypatch):
    monkeypatch.setenv("TALA_ALLOW_REMOTE_PROVIDERS", "1")
    seen = []
    provider = row("p", "Gemini", "remote", endpoint="https://example.test/v1", model="gem", apiKey="secret")
    assert probe(provider, _Models(["gem"], seen)) == "online"
    assert seen and seen[0][0].endswith("/models") and seen[0][1]["Authorization"] == "Bearer secret"


def test_a_missing_key_or_endpoint_is_offline_without_a_request(monkeypatch):
    monkeypatch.setenv("TALA_ALLOW_REMOTE_PROVIDERS", "1")
    spy = _Spy()
    assert probe(row("p", "G", "remote", endpoint="https://e.test/v1", model="m"), spy) == "offline"
    assert probe(row("p", "G", "remote", apiKey="k", model="m"), spy) == "offline"
    assert spy.built == 0


def test_a_model_the_endpoint_does_not_list_is_offline(monkeypatch):
    monkeypatch.setenv("TALA_ALLOW_REMOTE_PROVIDERS", "1")
    provider = row("p", "G", "remote", endpoint="https://e.test/v1", model="wanted", apiKey="k")
    assert probe(provider, _Models(["other"], [])) == "offline"


def test_the_start_up_verification_uses_the_policy_and_the_probe():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "python" / "harness" / "server.py").read_text(encoding="utf-8")
    start = source.index("async def _auto_detect_and_verify_providers")
    body = source[start:source.index("\nasync def ", start + 10)]
    assert "provider_refusal(" in body and "probe_remote_provider(" in body
    assert 'f"Bearer {api_key}"' not in body, "the start-up pass must not build its own authenticated request"
