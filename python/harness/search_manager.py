"""
search_manager.py — Tala Cognitive Engine Room: Search Providers & Web Crawler
=============================================================================
Orchestrates prioritized web search providers and falls back to direct
web crawling if no providers are present.

Milestone 19 — Task 19.1.2
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Any
import httpx
from lxml import html

from python.harness import db

logger = logging.getLogger(__name__)


async def get_search_providers() -> list[dict[str, Any]]:
    """Retrieve all configured search providers sorted by priority from DB."""
    try:
        rows = await db.fetch(
            "SELECT p.id, p.name, p.endpoint, p.api_key, p.priority, p.locked, p.credential_id, "
            "c.config as cred_config "
            "FROM search_providers p "
            "LEFT JOIN auth_credentials c ON p.credential_id = c.id "
            "ORDER BY p.priority ASC"
        )
        providers = []
        for r in rows:
            api_key = r["api_key"]
            if r["credential_id"] and r["cred_config"]:
                # Credential secrets are encrypted at rest (UI-080); decrypt so a
                # referenced credential still yields a usable key.
                from python.harness.credential_crypto import decrypt_config
                cred_cfg = decrypt_config(r["cred_config"]) or {}
                api_key = cred_cfg.get("api_key", api_key)

            providers.append({
                "id": r["id"],
                "name": r["name"],
                "endpoint": r["endpoint"],
                "api_key": api_key,
                "priority": r["priority"],
                "locked": r["locked"],
                "credential_id": r["credential_id"],
            })
        return providers
    except Exception as exc:
        logger.error("Failed to fetch search providers: %s", exc)
        return []


CRAWLER_URL = "https://html.duckduckgo.com/html/"


async def _guarded_crawl(query: str, outcome: "SearchOutcome") -> list[dict[str, str]]:
    """
    Run the DuckDuckGo crawler only if the network guard permits that host.

    The provider branch of ``query_detailed`` has always asked the guard; the crawler
    fallback did not, and it is the route a fresh install takes (no providers
    configured), so a query could leave the machine under a default-deny guard. The
    guard is asked about the host only -- never given the query text.
    """
    from python.harness.access import AGENT, Principal
    from python.harness.network_guard import check as net_check

    permitted = await net_check(CRAWLER_URL, Principal(AGENT, "default"), purpose="search:crawler")
    if not permitted.allowed:
        logger.warning("The web crawler is blocked by the network guard: %s", permitted.reason)
        outcome.errors.append({
            "provider": "DuckDuckGo crawler",
            "reason": f"blocked by network guardrails ({permitted.matched})",
        })
        return []
    return await _crawl_web(query)


async def _crawl_web(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """
    Crawl DuckDuckGo HTML search directly as a fallback.
    Extracts title, url (resolving DDG redirect), and snippet.
    """
    url = f"{CRAWLER_URL}?q={urllib.parse.quote(query)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                logger.warning(
                    "DuckDuckGo HTML search failed with status %d", response.status_code
                )
                return []

            tree = html.fromstring(response.content)
            results: list[dict[str, str]] = []

            # DDG HTML search results are in div.web-result
            for node in tree.xpath('//div[contains(@class, "web-result")]'):
                title_nodes = node.xpath('.//a[contains(@class, "result__a")]')
                snippet_nodes = node.xpath('.//a[contains(@class, "result__snippet")]')

                if title_nodes:
                    title = title_nodes[0].text_content().strip()
                    href = title_nodes[0].get("href", "")

                    # Extract clean URL if it's a redirect
                    if "uddg=" in href:
                        parsed_href = urllib.parse.urlparse(href)
                        qs = urllib.parse.parse_qs(parsed_href.query)
                        if "uddg" in qs:
                            href = qs["uddg"][0]

                    snippet = ""
                    if snippet_nodes:
                        snippet = snippet_nodes[0].text_content().strip()

                    results.append({
                        "title": title,
                        "url": href,
                        "snippet": snippet,
                    })

                    if len(results) >= max_results:
                        break

            return results
    except Exception as exc:
        logger.error("Web crawler fallback encountered error: %s", exc)
        return []


@dataclass
class SearchOutcome:
    """
    The result of a search *and* what happened on the way to it.

    ``SearchManager.query`` returns a bare list, which gave callers no way to
    tell a real provider answer from a DuckDuckGo scrape that ran because a
    paid API returned 401 (UI-018). The list return is kept for the existing
    callers; anything that wants to be honest with the user calls
    :meth:`SearchManager.query_detailed` and reads these fields.
    """

    results: list[dict[str, str]] = field(default_factory=list)
    #: 'provider' | 'crawler' | 'none'
    source: str = "none"
    #: The provider that actually answered, if any.
    provider_name: str | None = None
    #: One entry per provider that was tried and did not answer.
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """True when results came from the crawler after a provider failed."""
        return self.source == "crawler" and bool(self.errors)

    def error_summary(self) -> str:
        """A single line fit for the renderer's existing red banner."""
        if not self.errors:
            return ""
        head = "; ".join(f"{e['provider']}: {e['reason']}" for e in self.errors)
        if self.source == "crawler" and not self.results:
            return f"Search unavailable ({head}). Nothing was sent."
        if self.source == "crawler":
            return (
                f"Search provider unavailable ({head}). "
                "Showing DuckDuckGo crawler results instead."
            )
        return f"Search provider unavailable ({head})."


def _provider_kind(endpoint: str, name: str = "") -> str:
    """
    Classify a provider by host, not by substring-matching the whole URL.

    The previous dispatch routed anything containing ``/search`` into the
    SearXNG branch (UI-019). Almost every commercial search API's path ends in
    ``/search``, so Brave got a Bearer token instead of its subscription header
    and SerpAPI got read for ``results`` when it returns ``organic_results`` --
    and both failures then degraded silently to the crawler.
    """
    host = urllib.parse.urlparse(endpoint).netloc.lower()
    label = f"{host} {name}".lower()

    if "tavily.com" in host:
        return "tavily"
    if "serpapi.com" in host:
        return "serpapi"
    if "search.brave.com" in host or "brave" in label:
        return "brave"
    if "googleapis.com" in host and "customsearch" in endpoint.lower():
        return "google_cse"
    if "bing.microsoft.com" in host or "api.bing.com" in host:
        return "bing"
    # SearXNG identifies itself by name or by asking for JSON explicitly --
    # never by a bare /search path.
    if "searx" in label or "format=json" in endpoint.lower():
        return "searxng"
    return "generic"


def _first(mapping: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = mapping.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _normalise(raw: Any, limit: int = 5) -> list[dict[str, str]]:
    """Map any provider's result rows onto title/url/snippet."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": _first(r, "title", "name"),
            "url": _first(r, "url", "link", "href"),
            "snippet": _first(r, "content", "snippet", "description", "body"),
        })
        if len(out) >= limit:
            break
    return out


#: Where each provider puts its result rows.
_RESULT_PATHS: dict[str, tuple[str, ...]] = {
    "tavily": ("results",),
    "serpapi": ("organic_results",),
    "brave": ("web.results",),
    "google_cse": ("items",),
    "bing": ("webPages.value",),
    "searxng": ("results",),
    "generic": ("results", "items", "data"),
}


def _dig(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _extract(kind: str, data: Any) -> list[dict[str, str]]:
    if isinstance(data, list):
        return _normalise(data)
    for path in _RESULT_PATHS.get(kind, ("results",)):
        found = _dig(data, path)
        if isinstance(found, list):
            return _normalise(found)
    return []


def _with_params(endpoint: str, params: dict[str, str]) -> str:
    """Merge query parameters into an endpoint, preserving what is already there."""
    parsed = urllib.parse.urlparse(endpoint)
    existing = urllib.parse.parse_qs(parsed.query)
    for k, v in params.items():
        existing[k] = [v]
    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(existing, doseq=True))
    )


def _http_reason(status: int) -> str:
    return {
        400: "bad request",
        401: "the API key was rejected",
        403: "forbidden -- check the key's permissions",
        404: "the endpoint was not found",
        429: "rate limited or out of quota",
        500: "the provider had a server error",
        502: "bad gateway",
        503: "the provider is unavailable",
    }.get(status, "unexpected status")


class SearchManager:
    """Manages search queries via prioritized API providers or raw crawler failovers."""

    @classmethod
    async def query(cls, query: str, provider_id: str | None = None) -> list[dict[str, str]]:
        """Backwards-compatible list return. Prefer :meth:`query_detailed`."""
        outcome = await cls.query_detailed(query, provider_id=provider_id)
        return outcome.results

    @classmethod
    async def query_detailed(
        cls, query: str, provider_id: str | None = None
    ) -> SearchOutcome:
        """
        Execute a search, reporting *how* the answer was obtained.

        Every provider that fails is recorded on the outcome, including the
        non-200 responses that used to fall off the end of their branch without
        raising -- so a 401 from a paid API is no longer indistinguishable from
        success (UI-018, and Q-9: "a failed provider must be visible").
        """
        outcome = SearchOutcome()
        providers = await get_search_providers()

        # Check if we forced a specific provider via arguments (from Research Library selection)
        selected_providers = []
        if provider_id:
            if provider_id == "crawler":
                logger.info("SearchManager: Direct crawling fallback selected.")
                outcome.results = await _guarded_crawl(query, outcome)
                # Explicitly chosen, so this is not a degradation.
                outcome.source = "crawler"
                outcome.provider_name = "DuckDuckGo crawler"
                return outcome

            selected_providers = [p for p in providers if p["id"] == provider_id]
            if selected_providers:
                logger.info("SearchManager: Specific provider '%s' requested.", provider_id)
                providers = selected_providers
            else:
                logger.warning(
                    "SearchManager: Requested provider ID '%s' not found. "
                    "Falling back to default routing.", provider_id
                )
                outcome.errors.append({
                    "provider": str(provider_id),
                    "reason": "the selected provider is no longer configured",
                })

        # Check if there is a forced (locked) search provider
        if not provider_id or not selected_providers:
            locked_providers = [p for p in providers if p.get("locked")]
            if locked_providers:
                logger.info(
                    "SearchManager: Locked search providers active. Filtering search backends."
                )
                providers = locked_providers

        for p in providers:
            name = p["name"]
            endpoint = p["endpoint"]
            api_key = p["api_key"]
            kind = _provider_kind(endpoint, name)

            logger.info(
                "Attempting web search with provider '%s' (%s) as kind=%s",
                name, endpoint, kind,
            )

            try:
                # The harness half of the guardrails (UI-061, UI-063). Opt-in:
                # with no rules configured this permits everything, exactly as
                # before. Refused here rather than inside _call so a blocked
                # provider is reported like any other failed provider instead
                # of raising through the whole search.
                from python.harness.network_guard import check as net_check
                from python.harness.access import AGENT, Principal

                permitted = await net_check(
                    endpoint, Principal(AGENT, "default"), purpose=f"search:{name}")
                if not permitted.allowed:
                    logger.warning(
                        "Search provider '%s' blocked by the network guard: %s",
                        name, permitted.reason,
                    )
                    outcome.errors.append({
                        "provider": name,
                        "reason": f"blocked by network guardrails ({permitted.matched})",
                    })
                    continue

                response = await cls._call(kind, endpoint, api_key, query)

                if response.status_code != 200:
                    # This is the UI-018 hole: a non-200 is not an exception, so
                    # the old code left the branch without returning and without
                    # logging, and the loop quietly ran out into the crawler.
                    reason = _http_reason(response.status_code)
                    logger.warning(
                        "Search provider '%s' returned HTTP %d (%s). Trying next...",
                        name, response.status_code, reason,
                    )
                    outcome.errors.append({
                        "provider": name,
                        "reason": f"HTTP {response.status_code} ({reason})",
                    })
                    continue

                try:
                    data = response.json()
                except (ValueError, json.JSONDecodeError):
                    logger.warning(
                        "Search provider '%s' returned HTTP 200 but not JSON. Trying next...",
                        name,
                    )
                    outcome.errors.append({
                        "provider": name,
                        "reason": "responded with 200 but the body was not JSON",
                    })
                    continue

                results = _extract(kind, data)
                if not results:
                    # 200 with nothing usable means either a genuinely empty
                    # result set or a shape we misread -- either way the user
                    # should not be handed crawler output as if it were theirs.
                    logger.warning(
                        "Search provider '%s' returned no usable results for %r.",
                        name, query,
                    )
                    outcome.errors.append({
                        "provider": name,
                        "reason": "returned no usable results",
                    })
                    continue

                outcome.results = results
                outcome.source = "provider"
                outcome.provider_name = name
                return outcome

            except httpx.TimeoutException:
                logger.warning("Search provider '%s' timed out. Trying next...", name)
                outcome.errors.append({"provider": name, "reason": "timed out"})
                continue
            except Exception as exc:
                logger.warning(
                    "Search provider '%s' query failed: %s. Trying next...", name, exc
                )
                outcome.errors.append({
                    "provider": name,
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue

        # If all providers fail or the list is empty, crawl the web directly.
        if outcome.errors:
            logger.warning(
                "All %d search provider(s) failed; falling back to the direct web "
                "crawler. The caller is told this happened.", len(outcome.errors),
            )
        else:
            logger.info("No search providers configured. Using the direct web crawler.")
        outcome.results = await _guarded_crawl(query, outcome)
        outcome.source = "crawler"
        outcome.provider_name = "DuckDuckGo crawler"
        return outcome

    # -- per-provider request shapes -----------------------------------------

    @classmethod
    async def _call(
        cls, kind: str, endpoint: str, api_key: str | None, query: str
    ) -> httpx.Response:
        """Issue the request each provider actually expects."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            if kind == "tavily":
                return await client.post(
                    endpoint, json={"api_key": api_key, "query": query}
                )

            if kind == "brave":
                # Brave rejects Authorization: Bearer -- it wants its own header.
                headers = {"Accept": "application/json"}
                if api_key:
                    headers["X-Subscription-Token"] = api_key
                return await client.get(
                    _with_params(endpoint, {"q": query}), headers=headers
                )

            if kind == "serpapi":
                # SerpAPI takes the key as a query parameter, not a header.
                params = {"q": query}
                if api_key:
                    params["api_key"] = api_key
                return await client.get(_with_params(endpoint, params))

            if kind == "google_cse":
                params = {"q": query}
                if api_key:
                    params["key"] = api_key
                return await client.get(_with_params(endpoint, params))

            if kind == "bing":
                headers = {"Accept": "application/json"}
                if api_key:
                    headers["Ocp-Apim-Subscription-Key"] = api_key
                return await client.get(
                    _with_params(endpoint, {"q": query}), headers=headers
                )

            if kind == "searxng":
                headers = {}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                return await client.get(
                    _with_params(endpoint, {"q": query, "format": "json"}),
                    headers=headers,
                )

            # generic: send both common auth headers and hope one fits
            headers = {"Accept": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
                headers["X-API-Key"] = api_key
            return await client.get(_with_params(endpoint, {"q": query}), headers=headers)
