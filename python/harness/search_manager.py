"""
search_manager.py — Tala Cognitive Engine Room: Search Providers & Web Crawler
=============================================================================
Orchestrates prioritized web search providers and falls back to direct
web crawling if no providers are present.

Milestone 19 — Task 19.1.2
"""

from __future__ import annotations

import logging
import urllib.parse
import re
from typing import Any
import httpx
from lxml import html

from python.harness import db

logger = logging.getLogger(__name__)


async def get_search_providers() -> list[dict[str, Any]]:
    """Retrieve all configured search providers sorted by priority from DB."""
    try:
        rows = await db.fetch(
            "SELECT id, name, endpoint, api_key, priority FROM search_providers ORDER BY priority ASC"
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "endpoint": r["endpoint"],
                "api_key": r["api_key"],
                "priority": r["priority"],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error("Failed to fetch search providers: %s", exc)
        return []


async def crawl_web(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """
    Crawl DuckDuckGo HTML search directly as a fallback.
    Extracts title, url (resolving DDG redirect), and snippet.
    """
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
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


class SearchManager:
    """Manages search queries via prioritized API providers or raw crawler failovers."""

    @classmethod
    async def query(cls, query: str) -> list[dict[str, str]]:
        """
        Execute search query.
        Tries providers in priority order, falling back to DDG HTML crawl on failure/none.
        """
        providers = await get_search_providers()
        
        for p in providers:
            name = p["name"]
            endpoint = p["endpoint"]
            api_key = p["api_key"]

            logger.info("Attempting web search with provider '%s' (%s)", name, endpoint)
            
            try:
                # 1. Tavily API handler
                if "tavily.com" in endpoint.lower():
                    async with httpx.AsyncClient(timeout=8.0) as client:
                        payload = {"api_key": api_key, "query": query}
                        response = await client.post(endpoint, json=payload)
                        if response.status_code == 200:
                            data = response.json()
                            raw_results = data.get("results", [])
                            return [
                                {
                                    "title": r.get("title", ""),
                                    "url": r.get("url", ""),
                                    "snippet": r.get("content", ""),
                                }
                                for r in raw_results
                            ][:5]
                            
                # 2. Searxng API handler
                elif "format=json" in endpoint.lower() or "/search" in endpoint.lower():
                    # Parse Searxng URL parameters, appending format=json if missing
                    parsed_url = urllib.parse.urlparse(endpoint)
                    query_params = urllib.parse.parse_qs(parsed_url.query)
                    query_params["q"] = [query]
                    query_params["format"] = ["json"]
                    
                    # Reconstruct URL with parameters
                    clean_query = urllib.parse.urlencode(query_params, doseq=True)
                    target_url = urllib.parse.urlunparse(
                        parsed_url._replace(query=clean_query)
                    )
                    
                    headers = {}
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"

                    async with httpx.AsyncClient(timeout=8.0) as client:
                        response = await client.get(target_url, headers=headers)
                        if response.status_code == 200:
                            data = response.json()
                            raw_results = data.get("results", [])
                            return [
                                {
                                    "title": r.get("title", ""),
                                    "url": r.get("url", ""),
                                    "snippet": r.get("content", r.get("snippet", "")),
                                }
                                for r in raw_results
                            ][:5]

                # 3. Generic JSON handler (standard GET search)
                else:
                    target_url = endpoint
                    if "?" in endpoint:
                        target_url += f"&q={urllib.parse.quote(query)}"
                    else:
                        target_url += f"?q={urllib.parse.quote(query)}"
                        
                    headers = {
                        "Accept": "application/json"
                    }
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                        headers["X-API-Key"] = api_key

                    async with httpx.AsyncClient(timeout=8.0) as client:
                        response = await client.get(target_url, headers=headers)
                        if response.status_code == 200:
                            data = response.json()
                            # Standard formats might return a list of items or under a 'results' key
                            results_list = []
                            if isinstance(data, list):
                                results_list = data
                            elif isinstance(data, dict):
                                results_list = data.get("results", data.get("items", []))
                                
                            if isinstance(results_list, list):
                                return [
                                    {
                                        "title": r.get("title", r.get("name", "")),
                                        "url": r.get("url", r.get("link", "")),
                                        "snippet": r.get("snippet", r.get("content", r.get("description", ""))),
                                    }
                                    for r in results_list if isinstance(r, dict)
                                ][:5]

            except Exception as exc:
                logger.warning(
                    "Search provider '%s' query failed: %s. Trying next...", name, exc
                )
                continue

        # If all providers fail or list is empty, crawl the web directly
        logger.info("No active search providers succeeded. Falling back to direct web crawler.")
        return await crawl_web(query)
