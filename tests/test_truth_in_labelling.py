"""
A status badge must report what was measured, not what was configured.

Four findings, one habit. UI-091: nine of twelve health indicators were
hardcoded literals, so OVERALL read DEGRADED forever because two constants said
so, and six subsystems read HEALTHY forever for the same reason. UI-046: a
remote inference provider showed ONLINE whenever its apiKey string was
non-empty, with no request made. UI-064 and UI-073: the search and database
panels rendered a fixed green dot with no variable, no probe and no
conditional.

The rule applied throughout: where something is measured, report the
measurement; where nothing is measured, say so. Reporting healthy because
nobody looked is the failure this whole audit keeps finding, and it is worst on
the screen you would check to find out whether anything is wrong.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

RENDERER = Path("renderer/renderer.js")


# -- UI-091: the health panel -------------------------------------------------

def test_no_indicator_is_a_hardcoded_literal():
    from python.harness import server

    src = inspect.getsource(server.handle_logging_get_health)
    # the old shape: services["NAME"] = "HEALTHY"
    literals = re.findall(r'services\[\s*["\'][A-Z_]+["\']\s*\]\s*=\s*["\'][A-Z]+["\']', src)
    assert not literals, f"hardcoded health verdicts remain: {literals}"


@pytest.mark.asyncio
async def test_every_indicator_reports_its_basis(monkeypatch):
    """A colour with no reason cannot be acted on, and Tala cannot read it
    either (UI-086)."""
    from python.harness import server

    async def fail(*a, **k):
        raise RuntimeError("no database in this test")
    monkeypatch.setattr(server.db, "fetchval", fail)
    monkeypatch.setattr(server.db, "fetchrow", fail)
    monkeypatch.setattr(server.db, "get_pool", lambda: None)

    result = json.loads(await server.handle_logging_get_health({}, 1))["result"]

    # The count is not the assertion -- the coverage is. Pinning a literal here
    # meant that adding a thirteenth probe (ARCHITECTURAL_INVARIANTS) failed a
    # test about whether indicators explain themselves, which is not what this
    # test is for. What must hold is that every indicator the panel reports has
    # a basis, and that no probe was quietly dropped.
    assert len(result["services"]) >= 12
    assert set(result["details"]) == set(result["services"])
    for name, detail in result["details"].items():
        assert detail, f"{name} reports a status with no basis"


@pytest.mark.asyncio
async def test_unmeasured_is_unknown_not_healthy(monkeypatch):
    from python.harness import server

    async def fail(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(server.db, "fetchval", fail)
    monkeypatch.setattr(server.db, "fetchrow", fail)
    monkeypatch.setattr(server.db, "get_pool", lambda: None)

    result = json.loads(await server.handle_logging_get_health({}, 1))["result"]

    # Nothing that failed to measure may claim health.
    for name, status in result["services"].items():
        if status == "UNKNOWN":
            assert name in result["unmeasured"]
    # The accounting identity is the point: every service is either measured or
    # named as unmeasured, with nothing falling between the two and silently
    # counting as fine.
    assert result["measured"] == len(result["services"]) - len(result["unmeasured"])


@pytest.mark.asyncio
async def test_a_probe_that_raises_does_not_take_down_the_panel(monkeypatch):
    """The screen you check when things are broken must survive things being
    broken."""
    from python.harness import server

    def boom():
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(server, "_probe_tools", boom)

    result = json.loads(await server.handle_logging_get_health({}, 1))["result"]
    assert result["services"]["TOOL_EXECUTION_COORDINATOR"] == "UNKNOWN"
    assert "probe" in result["details"]["TOOL_EXECUTION_COORDINATOR"].lower()


def test_all_measured_and_all_good_is_the_only_route_to_healthy():
    from python.harness import server

    src = inspect.getsource(server.handle_logging_get_health)
    # PARTIAL exists precisely so unknowns cannot round up to HEALTHY
    assert '"PARTIAL"' in src
    assert "unknowns" in src


# -- UI-046: remote inference providers --------------------------------------

def test_a_remote_provider_is_probed_not_assumed():
    """An expired key, a revoked key, a typo, a wrong endpoint, a suspended
    account and no network all used to render the same green dot."""
    # The probe lives in provider_probe.py (it moved out of server.py so the provider policy can gate it);
    # the start-up pass must delegate to it rather than assume.
    block = Path("python/harness/provider_probe.py").read_text(encoding="utf-8")
    assert "probe_remote_provider(" in Path("python/harness/server.py").read_text(encoding="utf-8")

    assert "httpx" in block or "cached_models" in block, "no request is made"
    assert "/models" in block
    assert "Authorization" in block, "the key is not actually presented"
    # and a non-200 must be a failure, not a fall-through
    assert "status_code" in block


def test_a_remote_provider_without_an_endpoint_is_offline():
    block = Path("python/harness/provider_probe.py").read_text(encoding="utf-8")
    assert "not endpoint" in block


# -- UI-064 / UI-073: the panel badges ---------------------------------------

def test_no_panel_renders_a_hardcoded_green_status():
    js = RENDERER.read_text(encoding="utf-8")
    pattern = re.compile(
        r'<span class="status-dot status-ok"></span>\s*'
        r'<span class="provider-status-text">(ACTIVE|MOUNTED|CONNECTED|ONLINE|TRUSTED)</span>'
    )
    assert not pattern.search(js), "a panel still asserts health it never measured"


def test_the_badge_defaults_to_configured_not_ok():
    js = RENDERER.read_text(encoding="utf-8")
    block = js.split("function providerStatusBadge(", 1)[1][:900]
    assert "CONFIGURED" in block
    assert "status-unknown" in block, "unmeasured must not look measured"
    assert "map[state] || map.configured" in block, "an unknown state must not fall to ok"


def test_every_badge_call_passes_a_real_variable():
    """Three of the seven loops use inst/cred/contact, not p -- a wrong name
    here is a ReferenceError at render time, not a wrong colour."""
    js = RENDERER.read_text(encoding="utf-8")
    for line in js.splitlines():
        if "providerStatusBadge(" not in line or "function providerStatusBadge" in line:
            continue
        match = re.search(r"providerStatusBadge\((\w+)\._probe", line)
        assert match, f"badge call with no probe variable: {line.strip()[:70]}"


def test_both_panels_can_run_a_real_probe():
    js = RENDERER.read_text(encoding="utf-8")
    assert "search.test_provider" in js
    assert "database.test_provider" in js
    assert "btn-search-provider-test" in js
    assert "btn-database-provider-test" in js


def test_the_probe_is_on_demand():
    """A search probe spends the user's own API quota; running it on every
    panel load would be spending it without being asked."""
    js = RENDERER.read_text(encoding="utf-8")
    block = js.split("async function runProviderProbe(", 1)[1][:900]
    assert "addEventListener" not in block, "the probe should be invoked by a click"
    assert "'testing'" in block, "the badge should show it is working"


@pytest.mark.asyncio
async def test_search_test_provider_reports_a_crawler_fallback_as_failure():
    """Falling through to the crawler is exactly the failure this panel hid."""
    from python.harness import server
    from python.harness.search_manager import SearchOutcome

    async def fake(query, provider_id=None):
        return SearchOutcome(results=[{"title": "x"}], source="crawler",
                             provider_name="DuckDuckGo crawler",
                             errors=[{"provider": "Tavily", "reason": "HTTP 401"}])

    from python.harness import search_manager
    original = search_manager.SearchManager.query_detailed
    search_manager.SearchManager.query_detailed = staticmethod(fake)
    try:
        result = json.loads(
            await server.handle_search_test_provider({"id": "sp-1"}, 1)
        )["result"]
    finally:
        search_manager.SearchManager.query_detailed = original

    assert result["state"] == "failed"
    assert "401" in result["detail"]


@pytest.mark.asyncio
async def test_a_probe_needs_an_id():
    from python.harness import server
    for handler in (server.handle_search_test_provider,
                    server.handle_database_test_provider):
        out = json.loads(await handler({}, 1))
        assert "error" in out
