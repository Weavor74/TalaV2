"""
Guardrails govern the harness, not only the text.

Steven's scope for the panel: *"This isnt just about what the agent can talk
about. This is about the entire harness. Gaurdrails should be able to address
firewall rules, and port access... I could configure a user to have access to
one of my agents. But that agent can only access a certain search API, on a
certain port, at a single allocated time."*

Guardrails were prompt-in / response-out only (UI-061), and nothing anywhere
enforced a port or a time window (UI-063). The port and the allocated time were
already expressible as grant constraints; what was missing was the place that
asks before a connection is made.

**Default on + default-deny (Highs H7).** Fresh installs enable the guard with
default deny (loopback always allowed). Tests that need the historical opt-out
set ``enabled=False`` explicitly. An unreadable rules row still leaves the
guard off so a broken DB cannot brick the box.
"""
from __future__ import annotations

import pytest

from python.harness import network_guard as ng
from python.harness.access import AGENT, Principal

AGENT_P = Principal(AGENT, "default")


@pytest.fixture
def rules(monkeypatch):
    """Network rules in memory, with the environment set to local."""
    current: dict = {"value": dict(ng.DEFAULT_RULES)}

    async def load():
        return current["value"]
    monkeypatch.setattr(ng, "load_rules", load)

    from python.harness import environment as env

    async def state():
        return {"environment": "local", "lockdown": False}
    monkeypatch.setattr(env, "get_state", state)

    return current


def _set(rules, **kwargs):
    rules["value"] = {**ng.DEFAULT_RULES, **kwargs}


# -- target parsing -----------------------------------------------------------

@pytest.mark.parametrize("target,host,port", [
    ("https://api.tavily.com/search", "api.tavily.com", 443),
    ("http://example.com/x", "example.com", 80),
    ("https://host:8443/y", "host", 8443),
    ("db.internal:5432", "db.internal", 5432),
    ("plainhost", "plainhost", None),
    ("ftp://files.example.com", "files.example.com", 21),
])
def test_targets_are_parsed(target, host, port):
    assert ng.split_target(target) == (host, port)


# -- opt-in -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nothing_is_blocked_when_rules_are_explicitly_disabled(rules):
    """Settings can still disable the guard for first-run / lab use."""
    _set(rules, enabled=False, default="allow")
    decision = await ng.check("https://anything.example", AGENT_P)
    assert decision.allowed
    assert "not enabled" in decision.reason


@pytest.mark.asyncio
async def test_defaults_deny_outbound_non_loopback(rules):
    """Highs H7: code defaults are enabled + default-deny."""
    # fixture already starts from DEFAULT_RULES
    decision = await ng.check("https://anything.example", AGENT_P)
    assert not decision.allowed
    assert "deny" in decision.reason.lower() or decision.matched == "default:deny"


@pytest.mark.asyncio
async def test_an_unreadable_rule_row_leaves_the_guard_off(monkeypatch):
    """An unreadable setting must not silently cut the machine off."""
    from python.harness import db

    async def boom(*a, **k):
        raise RuntimeError("no database")
    monkeypatch.setattr(db, "fetchrow", boom)

    loaded = await ng.load_rules()
    assert loaded["enabled"] is False


# -- allow and deny -----------------------------------------------------------

@pytest.mark.asyncio
async def test_an_allowlisted_host_and_port(rules):
    _set(rules, enabled=True, default="deny",
         allow=[{"host": "api.tavily.com", "ports": [443]}])

    assert await ng.check("https://api.tavily.com/search", AGENT_P)


@pytest.mark.asyncio
async def test_the_same_host_on_another_port_is_refused(rules):
    """This is the "on a certain port" half of Steven's example."""
    _set(rules, enabled=True, default="deny",
         allow=[{"host": "api.tavily.com", "ports": [443]}])

    assert not await ng.check("http://api.tavily.com:8080/x", AGENT_P)


@pytest.mark.asyncio
async def test_an_unlisted_host_is_refused_under_default_deny(rules):
    _set(rules, enabled=True, default="deny", allow=[{"host": "a.com"}])
    assert not await ng.check("https://b.com", AGENT_P)


@pytest.mark.asyncio
async def test_an_unlisted_host_is_permitted_under_default_allow(rules):
    _set(rules, enabled=True, default="allow")
    assert await ng.check("https://b.com", AGENT_P)


@pytest.mark.asyncio
async def test_deny_beats_allow(rules):
    """A blocklist entry an allowlist can override is not a blocklist."""
    _set(rules, enabled=True, default="allow",
         allow=[{"host": "*"}], deny=[{"host": "*.evil.com"}])

    assert await ng.check("https://good.com", AGENT_P)
    decision = await ng.check("https://x.evil.com", AGENT_P)
    assert not decision.allowed
    assert decision.matched.startswith("deny:")


@pytest.mark.asyncio
async def test_wildcards_match(rules):
    _set(rules, enabled=True, default="deny", allow=[{"host": "*.example.com"}])

    assert await ng.check("https://api.example.com", AGENT_P)
    assert not await ng.check("https://example.org", AGENT_P)


@pytest.mark.asyncio
async def test_a_rule_naming_ports_does_not_match_an_unknown_port(rules):
    """Assuming the port would defeat the rule."""
    _set(rules, enabled=True, default="deny", allow=[{"host": "h", "ports": [443]}])
    assert not await ng.check("h", AGENT_P)


@pytest.mark.asyncio
async def test_loopback_stays_reachable(rules):
    """Cutting the machine off from its own local model turns a security
    setting into a broken app, and the traffic never leaves the box."""
    _set(rules, enabled=True, default="deny")

    assert await ng.check("http://localhost:11434/api/tags", AGENT_P)
    assert await ng.check("http://127.0.0.1:5432", AGENT_P)


@pytest.mark.asyncio
async def test_loopback_can_still_be_denied_explicitly(rules):
    """Deny is evaluated before the loopback exemption, so a user who really
    means it can still say so."""
    _set(rules, enabled=True, default="allow", deny=[{"host": "localhost"}])
    assert not await ng.check("http://localhost:11434", AGENT_P)


# -- grants -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_grant_opens_a_host_under_default_deny(rules, monkeypatch):
    """The other half of the example: the *user* grants the agent one API."""
    from python.harness import access

    async def granted(principal, resource, permission="read", context=None):
        return access.Decision(
            resource == "net:api.openai.com", "granted by invitation",
            str(principal), resource, permission, "g1", {"ports": [443]})
    monkeypatch.setattr(access, "check", granted)

    _set(rules, enabled=True, default="deny")

    decision = await ng.check("https://api.openai.com/v1", AGENT_P)
    assert decision.allowed
    assert decision.matched.startswith("grant:")

    assert not await ng.check("https://elsewhere.com", AGENT_P)


@pytest.mark.asyncio
async def test_without_a_principal_no_grant_can_apply(rules):
    _set(rules, enabled=True, default="deny")

    decision = await ng.check("https://api.openai.com/v1", principal=None)
    assert not decision.allowed
    assert "No principal" in decision.reason


# -- the USB preset -----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_usb_preset_blocks_outbound_traffic(monkeypatch):
    """USB (ISOLATED) read as a security control and changed nothing (UI-044).
    It is the one preset whose name is a promise."""
    from python.harness import environment as env

    async def usb():
        return {"environment": "usb", "lockdown": False}
    monkeypatch.setattr(env, "get_state", usb)

    decision = await ng.check("https://api.tavily.com", AGENT_P)
    assert not decision.allowed
    assert decision.matched == "environment:usb"


@pytest.mark.asyncio
async def test_usb_still_reaches_the_local_engine(monkeypatch):
    from python.harness import environment as env

    async def usb():
        return {"environment": "usb", "lockdown": False}
    monkeypatch.setattr(env, "get_state", usb)

    assert await ng.check("http://localhost:11434/api/tags", AGENT_P)


# -- raising form -------------------------------------------------------------

@pytest.mark.asyncio
async def test_assert_may_reach_raises_on_a_refusal(rules, monkeypatch):
    from python.harness import db

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(db, "log_to_db", noop)

    _set(rules, enabled=True, default="deny")
    with pytest.raises(ng.NetworkBlocked):
        await ng.assert_may_reach("https://nope.example", AGENT_P)


@pytest.mark.asyncio
async def test_a_refusal_is_audited(rules, monkeypatch):
    """A connection that did not happen is exactly what someone will later
    need explained."""
    from python.harness import db
    written = []

    async def capture(level, message, trace=None, tx_id=None):
        written.append((level, message))
    monkeypatch.setattr(db, "log_to_db", capture)

    _set(rules, enabled=True, default="deny")
    with pytest.raises(ng.NetworkBlocked):
        await ng.assert_may_reach("https://nope.example", AGENT_P, purpose="test")

    assert written and written[0][0] == "WARNING"
    assert "nope.example" in written[0][1]


# -- the call sites -----------------------------------------------------------

def test_search_providers_are_checked():
    import inspect
    from python.harness.search_manager import SearchManager

    src = inspect.getsource(SearchManager.query_detailed)
    assert "net_check" in src
    # reported as a provider failure rather than raising through the search
    assert "blocked by network guardrails" in src


def test_git_remotes_are_checked():
    import inspect
    from python.harness import git_ops

    src = inspect.getsource(git_ops.run_git)
    assert "assert_may_reach" in src
    # a local operation carries no URL and is unaffected
    assert '"://" in str(argument)' in src


def test_storage_targets_can_be_checked():
    import inspect
    from python.harness import storage_clients

    src = inspect.getsource(storage_clients.assert_reachable)
    assert "assert_may_reach" in src
    # a local folder is not a network connection
    assert 'kind == "folder"' in src


def test_the_rpcs_exist():
    from python.harness.server import _HANDLERS

    for method in ("guardrails.get_network", "guardrails.set_network",
                   "network.check"):
        assert method in _HANDLERS
