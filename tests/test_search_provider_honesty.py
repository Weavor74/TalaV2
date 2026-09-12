"""
A failing search provider must be visible, and each provider must be routed
to correctly.

Two findings, one shape. UI-018: a non-200 is not an exception, so every
provider branch fell off its end without returning and without logging, the
loop exhausted, and the user got DuckDuckGo scrapings presented exactly like a
successful paid-API result. UI-019: dispatch matched the bare substring
``/search``, which is in almost every commercial search API's path, so Brave
and SerpAPI were handed SearXNG's auth header and read for SearXNG's response
key -- and the resulting failure then degraded silently via UI-018.

Steven's direction (Q-9): "No. A failed provider must be visible."
"""
from __future__ import annotations


import pytest

from python.harness import search_manager as sm
from python.harness.search_manager import SearchOutcome, _extract, _provider_kind


# -- UI-019: dispatch ---------------------------------------------------------

@pytest.mark.parametrize("endpoint,name,expected", [
    ("https://serpapi.com/search", "SerpAPI", "serpapi"),
    ("https://api.search.brave.com/res/v1/web/search", "Brave", "brave"),
    ("https://api.tavily.com/search", "Tavily", "tavily"),
    ("https://www.googleapis.com/customsearch/v1", "Google", "google_cse"),
    ("https://api.bing.microsoft.com/v7.0/search", "Bing", "bing"),
    ("https://searx.example.org/search", "SearXNG", "searxng"),
    ("https://my.box:8080/search?format=json", "self-hosted", "searxng"),
    ("https://internal.corp/api/search", "Internal", "generic"),
])
def test_providers_route_by_host_not_by_the_search_substring(endpoint, name, expected):
    assert _provider_kind(endpoint, name) == expected


@pytest.mark.parametrize("kind,body", [
    ("serpapi", {"organic_results": [{"title": "T", "link": "u", "snippet": "s"}]}),
    ("brave", {"web": {"results": [{"title": "T", "url": "u", "description": "s"}]}}),
    ("google_cse", {"items": [{"title": "T", "link": "u", "snippet": "s"}]}),
    ("bing", {"webPages": {"value": [{"name": "T", "url": "u", "snippet": "s"}]}}),
    ("tavily", {"results": [{"title": "T", "url": "u", "content": "s"}]}),
])
def test_each_provider_is_read_from_its_own_response_key(kind, body):
    results = _extract(kind, body)
    assert len(results) == 1
    assert results[0]["title"] == "T"
    assert results[0]["url"] == "u"
    assert results[0]["snippet"] == "s"


def test_a_serpapi_body_read_as_searxng_yields_nothing():
    """The old bug, pinned: this is what the misroute actually produced."""
    assert _extract("searxng", {"organic_results": [{"title": "T"}]}) == []


# -- UI-018: failure is reported ---------------------------------------------

class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def one_provider(monkeypatch):
    """A single configured provider, with the crawler stubbed."""
    async def providers():
        return [{
            "id": "1", "name": "Brave",
            "endpoint": "https://api.search.brave.com/res/v1/web/search",
            "api_key": "k", "priority": 1, "locked": False, "credential_id": None,
        }]

    async def crawl(query, max_results=5):
        return [{"title": "ddg", "url": "d", "snippet": "scraped"}]

    monkeypatch.setattr(sm, "get_search_providers", providers)
    monkeypatch.setattr(sm, "crawl_web", crawl)


def _respond_with(monkeypatch, response):
    async def _call(cls, kind, endpoint, api_key, query):
        return response
    monkeypatch.setattr(sm.SearchManager, "_call", classmethod(_call))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,fragment", [
    (401, "the API key was rejected"),
    (429, "rate limited or out of quota"),
    (500, "the provider had a server error"),
])
async def test_a_non_200_is_reported_not_silently_crawled(
    one_provider, monkeypatch, status, fragment
):
    _respond_with(monkeypatch, _Response(status))
    outcome = await sm.SearchManager.query_detailed("test")

    assert outcome.source == "crawler"
    assert outcome.degraded is True
    assert len(outcome.errors) == 1
    assert fragment in outcome.errors[0]["reason"]
    assert "Brave" in outcome.error_summary()


@pytest.mark.asyncio
async def test_a_200_that_is_not_json_is_a_failure(one_provider, monkeypatch):
    _respond_with(monkeypatch, _Response(200))
    outcome = await sm.SearchManager.query_detailed("test")
    assert outcome.degraded is True
    assert "not JSON" in outcome.errors[0]["reason"]


@pytest.mark.asyncio
async def test_a_200_with_no_usable_rows_is_a_failure(one_provider, monkeypatch):
    _respond_with(monkeypatch, _Response(200, {"unexpected": "shape"}))
    outcome = await sm.SearchManager.query_detailed("test")
    assert outcome.degraded is True
    assert "no usable results" in outcome.errors[0]["reason"]


@pytest.mark.asyncio
async def test_a_healthy_provider_is_not_reported_as_degraded(one_provider, monkeypatch):
    _respond_with(monkeypatch, _Response(200, {
        "web": {"results": [{"title": "real", "url": "u", "description": "s"}]}
    }))
    outcome = await sm.SearchManager.query_detailed("test")

    assert outcome.source == "provider"
    assert outcome.provider_name == "Brave"
    assert outcome.degraded is False
    assert outcome.error_summary() == ""
    assert outcome.results[0]["title"] == "real"


@pytest.mark.asyncio
async def test_choosing_the_crawler_deliberately_is_not_a_degradation(one_provider):
    """The user picking 'crawler' in the Research panel is a choice, not a failure."""
    outcome = await sm.SearchManager.query_detailed("test", provider_id="crawler")
    assert outcome.source == "crawler"
    assert outcome.degraded is False
    assert outcome.errors == []


@pytest.mark.asyncio
async def test_a_timeout_is_recorded_rather_than_swallowed(one_provider, monkeypatch):
    import httpx

    async def _call(cls, kind, endpoint, api_key, query):
        raise httpx.TimeoutException("too slow")
    monkeypatch.setattr(sm.SearchManager, "_call", classmethod(_call))

    outcome = await sm.SearchManager.query_detailed("test")
    assert outcome.errors[0]["reason"] == "timed out"
    assert outcome.degraded is True


@pytest.mark.asyncio
async def test_the_legacy_list_api_still_works(one_provider, monkeypatch):
    """query() has four callers; it must keep returning a bare list."""
    _respond_with(monkeypatch, _Response(200, {
        "web": {"results": [{"title": "real", "url": "u", "description": "s"}]}
    }))
    results = await sm.SearchManager.query("test")
    assert isinstance(results, list)
    assert results[0]["title"] == "real"


def test_no_errors_means_no_banner():
    assert SearchOutcome(source="provider").error_summary() == ""
