"""
The chat box has tabs, and the tabs have to keep two things straight.

Steven's shape: the agent chat is always on, and a tab is added when a new
conversation happens -- which means join/create and leave.

Two things in this UI are load-bearing rather than cosmetic, and both fail
silently if they regress.

**One input, two destinations.** The same textarea now sends to Tala or to a
room. A message meant for a room that reaches the agent path is not just
misrouted: it is run through slash commands, peer mentions and inference,
so "/undo" typed at people becomes a command against this machine.

**Joining is a decision.** Under the hybrid rule the room-level yes is the
one thing reserved for a person. A tab that silently worked before it was
joined would make that decision for them, and leaving has to actually close
the room rather than tidy the tab away -- which is why Leave sets a status
the receive path checks, and why this file pins that status against the
harness rather than trusting the label on the button.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def renderer() -> str:
    return (ROOT / "renderer" / "renderer.js").read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def page() -> str:
    return (ROOT / "renderer" / "index.html").read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def styles() -> str:
    return (ROOT / "renderer" / "styles.css").read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# The tab strip
# ---------------------------------------------------------------------------


def test_the_agent_tab_is_always_on(page):
    """
    It is what this panel is. Rooms are added beside it, so there is no
    control that closes or leaves it.
    """
    assert 'id="chat-tab-agent"' in page
    strip = page.split('id="chat-tabs"')[1].split("</div>")[0]
    for gone in ("data-close", "btn-conversation-leave"):
        assert gone not in strip, f"the agent tab can be closed via {gone}"


def test_there_is_a_way_to_start_one_and_a_way_out(page):
    """Create, join, decline, leave: the whole set of decisions about a room."""
    for control in (
        "btn-conversation-new",
        "btn-conversation-join",
        "btn-conversation-decline",
        "btn-conversation-leave",
    ):
        assert f'id="{control}"' in page, f"no {control} in the chat panel"


def test_rooms_get_their_own_pane(page, styles):
    """
    Separate from the agent transcript, and actually hideable: both panes set
    `display`, which beats the browser's own `[hidden]` rule, so without an
    explicit rule `el.hidden = true` does nothing and they stack.
    """
    assert 'id="conversation-messages"' in page
    assert ".chat-messages[hidden]" in styles


# ---------------------------------------------------------------------------
# One input, two destinations
# ---------------------------------------------------------------------------


def test_a_room_message_never_reaches_the_agent_path(renderer):
    """
    The handover has to happen before anything interprets the text. Below
    the slash-command branch, "/undo" typed at people in a room would run
    against this machine.
    """
    body = renderer.split("  async sendMessage() {")[1].split("\n  async ")[0]

    handover = body.find("ConversationTabs.isRoomActive")
    assert handover != -1, "the chat box does not hand a room message over at all"

    for interpreter, what in (
        ("parsePeerMention", "peer mentions"),
        ("startsWith('/')", "slash commands"),
        ("sendInferenceRequest", "the model"),
    ):
        at = body.find(interpreter)
        if at == -1:
            continue
        assert handover < at, f"a room message would reach {what} first"


def test_the_command_bar_stays_pointed_at_tala(renderer):
    """
    It types into the same textarea, so without this a command would be
    posted into whichever room happened to be open.
    """
    where = renderer.find("[Command: ${rawText}]")
    assert where != -1
    preceding = renderer[max(0, where - 700):where]
    assert "ConversationTabs.selectAgent" in preceding, (
        "the command bar can send a command into a conversation"
    )


# ---------------------------------------------------------------------------
# Joining is a decision
# ---------------------------------------------------------------------------


def test_an_unjoined_room_says_so_instead_of_taking_the_message(renderer):
    module = renderer.split("const ConversationTabs = {")[1]
    send = module.split("  async send(text) {")[1].split("\n  },")[0]
    assert "status !== 'joined'" in send, (
        "the chat box will post into a room nobody has joined"
    )
    assert "conversation.post" in send


def test_leave_sets_the_status_the_harness_actually_checks(renderer):
    """
    The button is only a label. What ends the scope is the status the
    receive path reads, so the two have to be the same word.
    """
    from python.harness import conversations

    module = renderer.split("const ConversationTabs = {")[1]
    assert "this.setMembership('left')" in module
    assert "this.setMembership('joined')" in module
    assert "this.setMembership('declined')" in module

    for status in ("left", "joined", "declined"):
        assert status in (
            conversations.STATUS_LEFT,
            conversations.STATUS_JOINED,
            conversations.STATUS_DECLINED,
        ), f"the UI sends {status!r}, which the harness does not know"


def test_a_left_room_is_not_a_tab(renderer):
    module = renderer.split("const ConversationTabs = {")[1]
    assert "r.status === 'joined' || r.status === 'invited'" in module, (
        "rooms this install is not in would keep their tabs"
    )


# ---------------------------------------------------------------------------
# The hybrid rule, on screen
# ---------------------------------------------------------------------------


def test_an_unapproved_participant_is_marked_and_not_hidden(renderer):
    """
    They are in the room and allowed to speak in it. Hiding them would leave
    you in a room with somebody you could not see; passing them off as
    approved would be worse.
    """
    module = renderer.split("const ConversationTabs = {")[1]
    assert "sender_approved === false" in module, "nothing marks an unapproved sender"
    assert "unapproved-mark" in module
    assert "(unapproved)" in module, "the participant list does not say who is unapproved"


def test_the_room_picker_does_not_filter_out_the_unapproved(renderer):
    """
    The hybrid rule is what makes an unapproved participant possible. A
    picker that only offered approved contacts would quietly implement the
    strict rule instead.
    """
    module = renderer.split("const ConversationTabs = {")[1]
    picker = module.split("async openNewModal()")[1].split("\n  },")[0]
    assert "'denied'" in picker, "a denied contact can be put in a room"
    assert "'approved'" not in picker.replace("unapproved", ""), (
        "the picker filters on approval, which is the strict rule"
    )


def test_the_invitation_notice_says_nothing_is_open_yet(renderer):
    module = renderer.split("const ConversationTabs = {")[1]
    handler = module.split("conversation.invited")[1][:500]
    assert "until you join" in handler, (
        "an invitation reads as a room you are already in"
    )
