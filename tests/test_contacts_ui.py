"""
The UI for the peer flow, and the wiring it depends on.

Every backend step of "A finds C, asks, C approves, A reads a document"
existed and none of it could be driven by hand: `contacts.pending`,
`contacts.approve`, `contacts.deny` and the whole `/chain/peers` directory
had no caller in the renderer. A capability with no way to reach it is
indistinguishable from an absent one.

Two failure modes are checked here rather than assumed, because both are
silent at runtime -- a button that calls a method the harness does not have
throws into a console nobody is reading, and a handler that reaches for an
element id that does not exist simply does nothing when clicked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "renderer" / "renderer.js"
INDEX = ROOT / "renderer" / "index.html"


@pytest.fixture(scope="module")
def renderer_source() -> str:
    return RENDERER.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def index_source() -> str:
    return INDEX.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# The UI can only call methods that exist
# ---------------------------------------------------------------------------


def test_every_method_the_ui_calls_is_registered(renderer_source):
    """
    A general ratchet, not only for this feature. The renderer names methods
    as strings, so a rename on the Python side or a typo on the JavaScript
    side fails at the moment a user clicks something, in a console nobody is
    watching.
    """
    from python.harness import server

    called = set(re.findall(r"method:\s*'([a-z_][a-z0-9_.]*)'", renderer_source))
    called |= set(re.findall(r'method:\s*"([a-z_][a-z0-9_.]*)"', renderer_source))

    # Electron main-process channels, not harness RPC -- handled by ipcMain
    # and deliberately never reaching _HANDLERS.
    rpc_methods = {m for m in called if ":" not in m}

    # main.js answers some of these itself before the request reaches the
    # WebSocket -- app.version reads package.json rather than asking Python.
    main_js = (ROOT / "electron" / "main.js").read_text(encoding="utf-8", errors="replace")
    intercepted = set(re.findall(r"method\s*===\s*'([a-z_][a-z0-9_.]*)'", main_js))

    missing = sorted(m for m in rpc_methods - intercepted if m not in server._HANDLERS)
    assert not missing, (
        f"the renderer calls {missing}, which neither the harness nor main.js serves"
    )


def test_the_peer_flow_is_reachable_from_the_ui(renderer_source):
    """The specific methods this feature exists to expose."""
    for method in ("contacts.pending", "contacts.approve", "contacts.deny", "chain.peers"):
        assert f"'{method}'" in renderer_source, f"{method} has no caller in the UI"


def test_the_directory_lookup_is_a_read_only_surface(renderer_source):
    """
    Registration and heartbeat belong to the harness's own connection
    lifecycle, not to a button. The UI discovers; it does not publish.
    """
    assert "chain/peers/register" not in renderer_source
    assert "chain/peers/heartbeat" not in renderer_source


# ---------------------------------------------------------------------------
# The handlers can only reach elements that exist
# ---------------------------------------------------------------------------


CONTACTS_ELEMENT_IDS = [
    "contacts-requests-card",
    "contacts-requests-container",
    "contacts-requests-count",
    "contacts-directory-card",
    "contacts-directory-container",
    "contacts-directory-search",
    "contacts-directory-online",
    "btn-contacts-discover",
    "btn-contacts-directory-close",
    "btn-contacts-directory-refresh",
    "modal-approve-contact",
    "approve-contact-username",
    "approve-contact-wallet",
    "approve-contact-requested",
    "approve-contact-permissions",
    "input-approve-clearance",
    "input-approve-categories",
    "btn-modal-approve-close",
    "btn-modal-approve-cancel",
    "btn-modal-approve-confirm",
]


@pytest.mark.parametrize("element_id", CONTACTS_ELEMENT_IDS)
def test_the_markup_has_the_element_the_script_reaches_for(element_id, index_source, renderer_source):
    assert f'id="{element_id}"' in index_source, f"{element_id} is queried but never rendered"
    assert element_id in renderer_source, f"{element_id} is rendered but never used"


def test_the_request_queue_hides_itself_when_empty(renderer_source):
    """
    A permanent empty box is furniture. This should read as a notification:
    present when somebody is waiting, gone otherwise.
    """
    section = renderer_source.split("async loadPendingRequests()")[1].split("openApproveModal(")[0]
    assert "card.style.display = 'none'" in section


def test_approval_starts_from_nothing_granted(renderer_source):
    """
    What a peer asked for is shown so the decision is informed, and ticks no
    box. A request that pre-selects itself is a request that approves itself
    on a distracted click.
    """
    section = renderer_source.split("openApproveModal(username)")[1].split("closeApproveModal()")[0]
    assert 'type="checkbox" data-permission="${key}" />' in section, (
        "a permission checkbox is rendered pre-ticked"
    )
    assert "checked" not in section.split("_sharePermissionKeys.forEach")[1].split("});")[0]


def test_a_directory_that_cannot_be_reached_says_so(renderer_source):
    """
    This codebase's most common defect is an interface that cannot tell
    'found nothing' from 'could not look'.
    """
    section = renderer_source.split("async searchDirectory()")[1].split("},")[0]
    assert "Could not reach the peer directory" in section
    assert "No peers registered" in section


# ---------------------------------------------------------------------------
# chain.peers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_peers_reports_an_unreachable_directory_as_an_error(monkeypatch):
    from python.harness import server

    async def no_endpoint():
        return "http://127.0.0.1:1"  # nothing listening

    monkeypatch.setattr("python.harness.chains.real_endpoint", no_endpoint)
    body = json.loads(await server.handle_chain_peers({}, 1))

    assert "error" in body, "an unreachable directory came back as an empty one"
    assert "peer directory" in body["error"]["message"]


@pytest.mark.asyncio
async def test_chain_peers_says_so_when_no_chain_is_configured(monkeypatch):
    from python.harness import server

    async def unset():
        return ""

    monkeypatch.setattr("python.harness.chains.real_endpoint", unset)
    result = json.loads(await server.handle_chain_peers({}, 1))["result"]

    assert result["peers"] == []
    assert "no chain endpoint" in result["reason"]


@pytest.mark.asyncio
async def test_chain_peers_marks_who_is_already_a_contact(monkeypatch):
    """
    So the UI offers 'Add' only for strangers, and shows the standing
    decision for everyone else.
    """
    from python.harness import server

    async def fake_fetch(*args, **kwargs):
        return [{"username": "@known", "status": "approved"}]

    def fake_urlopen(*args, **kwargs):
        raise AssertionError("should not be called; _fetch is patched")

    async def fake_to_thread(fn, *args, **kwargs):
        return {"peers": [{"username": "@known"}, {"username": "@stranger"}]}

    monkeypatch.setattr("python.harness.chains.real_endpoint", lambda: _completed("http://chain"))
    monkeypatch.setattr(server.db, "fetch", fake_fetch)
    monkeypatch.setattr(server.asyncio, "to_thread", fake_to_thread)

    result = json.loads(await server.handle_chain_peers({}, 1))["result"]
    by_name = {p["username"]: p for p in result["peers"]}

    assert by_name["@known"]["local_status"] == "approved"
    assert by_name["@stranger"]["local_status"] == ""


async def _completed(value):
    return value
