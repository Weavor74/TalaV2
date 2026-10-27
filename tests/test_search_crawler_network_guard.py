"""
The direct-crawler fallback must ask the network guard before it connects.

Found during the voice smoke test (feat/voice-finish): a spoken "what time is it?"
made the model call ``web_search``; with no provider configured the search manager
fell back to its DuckDuckGo crawler, which opened a connection to html.duckduckgo.com
carrying the query text -- under a guard whose default is deny. The provider branch
asked the guard (``net_check``); the two crawler paths never did, so the one route a
fresh install actually takes was the one the guard did not cover.
"""
from __future__ import annotations

import pytest

from python.harness import search_manager as sm
from python.harness.network_guard import NetDecision


@pytest.fixture
def crawler_spy(monkeypatch):
    calls = []

    async def crawl(query, max_results=5):
        calls.append(query)
        return [{"title": "T", "url": "u", "snippet": "s"}]

    async def no_providers():
        return []

    monkeypatch.setattr(sm, "_crawl_web", crawl)
    monkeypatch.setattr(sm, "get_search_providers", no_providers)
    return calls


def _guard(monkeypatch, allowed, seen=None):
    async def check(target, principal=None, purpose="", default_port=None):
        if seen is not None:
            seen.append((target, purpose))
        return NetDecision(allowed, "refused by the test" if not allowed else "allowed", "html.duckduckgo.com", 443,
                           matched="default" if not allowed else None)

    monkeypatch.setattr("python.harness.network_guard.check", check)


@pytest.mark.asyncio
async def test_the_fallback_crawler_is_not_run_when_the_guard_denies_it(crawler_spy, monkeypatch):
    _guard(monkeypatch, allowed=False)
    outcome = await sm.SearchManager.query_detailed("current weather")
    assert crawler_spy == []
    assert outcome.results == []
    assert any("network" in e["reason"] for e in outcome.errors)


@pytest.mark.asyncio
async def test_an_explicitly_chosen_crawler_is_guarded_too(crawler_spy, monkeypatch):
    _guard(monkeypatch, allowed=False)
    outcome = await sm.SearchManager.query_detailed("current weather", provider_id="crawler")
    assert crawler_spy == []
    assert outcome.results == []
    assert outcome.errors


@pytest.mark.asyncio
async def test_the_crawler_runs_when_the_guard_allows_it(crawler_spy, monkeypatch):
    seen = []
    _guard(monkeypatch, allowed=True, seen=seen)
    outcome = await sm.SearchManager.query_detailed("current weather")
    assert crawler_spy == ["current weather"]
    assert outcome.results
    assert seen and "duckduckgo" in seen[0][0]
    assert "query" not in seen[0][0] and "weather" not in seen[0][0]  # the guard is asked about the host, never given the text


@pytest.mark.asyncio
async def test_a_blocked_crawler_says_nothing_was_sent(crawler_spy, monkeypatch):
    _guard(monkeypatch, allowed=False)
    outcome = await sm.SearchManager.query_detailed("current weather")
    assert "Nothing was sent" in outcome.error_summary()
    assert "Showing DuckDuckGo" not in outcome.error_summary()
