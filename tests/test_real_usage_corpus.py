"""
sdk/agent/real_usage_corpus.py -- the two corpus-sync steps that feed the
LoRA voice adapter from real experience: Tala's own conversation_turns rows
and the real HTML-NN learning-event nodes.

No real DB or filesystem content is touched here: the DB is mocked, the
"nodes" directory is a tmp_path fixture, and GuardrailsManager/redact_string
run for real (they're pure functions, cheap, and worth exercising rather
than mocking away the exact thing this module exists to get right).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sdk.agent import real_usage_corpus as ruc


def _row(content, tool_calls=None, created_at=None):
    return {
        "content": content,
        "tool_calls": tool_calls if tool_calls is not None else [],
        "created_at": created_at or datetime(2026, 6, 1, tzinfo=timezone.utc),
    }


@pytest.fixture(autouse=True)
def _isolate_corpus_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(ruc, "REAL_USAGE_DIR", tmp_path / "real-usage")
    monkeypatch.setattr(ruc, "LEARNING_EVENTS_DIR", tmp_path / "learning-events")
    return tmp_path


@pytest.fixture(autouse=True)
def _fake_emotion_engine(monkeypatch):
    """engine_emotion_for() is reused as-is from build_story_node.py -- a
    real astro-engine lookup is slow and not what this module's own tests
    are responsible for verifying. Stub it, but record every call so tests
    can assert it was actually invoked with the item's real timestamp."""
    calls = []

    def fake_engine_emotion_for(when):
        calls.append(when)
        return "curiosity"

    import scripts.build_story_node as bsn

    monkeypatch.setattr(bsn, "engine_emotion_for", fake_engine_emotion_for)
    return calls


# ---------------------------------------------------------------------------
# sync_real_usage_corpus
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_clean_turn_is_included(monkeypatch, _fake_emotion_engine):
    from python.harness import db

    when = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    rows = [_row("Here is a plain, harmless reply.", created_at=when)]

    captured_query = {}

    async def fake_fetch(query, *args):
        captured_query["sql"] = query
        return rows

    monkeypatch.setattr(db, "fetch", fake_fetch)

    result = await ruc.sync_real_usage_corpus()

    assert result["success"] is True
    assert result["scanned"] == 1
    assert result["included"] == 1
    assert result["excluded"] == 0
    assert "role = 'assistant'" in captured_query["sql"]
    assert when in _fake_emotion_engine  # emotion derived from the turn's own timestamp

    written = list(ruc.REAL_USAGE_DIR.glob("*.html"))
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    assert 'data-era="real-usage"' in content
    assert "Here is a plain, harmless reply." in content
    assert 'data-emotion="curiosity"' in content


@pytest.mark.asyncio
async def test_a_turn_with_a_bearer_token_is_excluded(monkeypatch):
    from python.harness import db

    leaked = "Sure, use Bearer sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 to authenticate."
    rows = [_row(leaked)]

    async def fake_fetch(query, *args):
        return rows

    monkeypatch.setattr(db, "fetch", fake_fetch)

    result = await ruc.sync_real_usage_corpus()

    assert result["scanned"] == 1
    assert result["excluded"] == 1
    assert result["included"] == 0
    assert not list(ruc.REAL_USAGE_DIR.glob("*.html"))


@pytest.mark.asyncio
async def test_a_turn_that_trips_the_hardcoded_pii_rule_is_excluded(monkeypatch):
    from python.harness import db

    rows = [_row("You can reach me at steven.pollard@dir.texas.gov about this.")]

    async def fake_fetch(query, *args):
        return rows

    monkeypatch.setattr(db, "fetch", fake_fetch)

    result = await ruc.sync_real_usage_corpus()

    assert result["excluded"] == 1
    assert result["included"] == 0


@pytest.mark.asyncio
async def test_a_turn_that_trips_the_hardcoded_toxicity_rule_is_excluded(monkeypatch):
    from python.harness import db

    rows = [_row("That was a violent threat and nothing else.")]

    async def fake_fetch(query, *args):
        return rows

    monkeypatch.setattr(db, "fetch", fake_fetch)

    result = await ruc.sync_real_usage_corpus()

    assert result["excluded"] == 1
    assert result["included"] == 0


@pytest.mark.asyncio
async def test_only_assistant_rows_are_ever_queried(monkeypatch):
    """role='assistant' is enforced in the SQL itself -- this confirms the
    query this module issues actually says so, so a user's own words never
    reach the filter/write path in the first place."""
    from python.harness import db

    captured = {}

    async def fake_fetch(query, *args):
        captured["sql"] = query
        return []

    monkeypatch.setattr(db, "fetch", fake_fetch)

    await ruc.sync_real_usage_corpus()

    assert "conversation_turns" in captured["sql"]
    assert "role = 'assistant'" in captured["sql"]


@pytest.mark.asyncio
async def test_tool_calls_are_included_when_present(monkeypatch):
    from python.harness import db

    rows = [_row("Ran a quick check for you, and everything looked fine from my side.", tool_calls=[{"name": "list_files", "args": {}}])]

    async def fake_fetch(query, *args):
        return rows

    monkeypatch.setattr(db, "fetch", fake_fetch)

    await ruc.sync_real_usage_corpus()

    written = list(ruc.REAL_USAGE_DIR.glob("*.html"))
    content = written[0].read_text(encoding="utf-8")
    assert 'class="tool-call"' in content
    assert "list_files" in content


@pytest.mark.asyncio
async def test_a_db_failure_is_reported_and_does_not_raise(monkeypatch):
    from python.harness import db

    async def fake_fetch(query, *args):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "fetch", fake_fetch)

    result = await ruc.sync_real_usage_corpus()

    assert result["success"] is False
    assert "connection refused" in result["error"]


@pytest.mark.asyncio
async def test_written_corpus_round_trips_through_extract_turns(monkeypatch):
    """The real consumer, not a reimplementation of its parsing."""
    from python.harness import db
    from scripts.train_voice_adapter import extract_turns

    when = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [
        _row("First reply, all clear, nothing to worry about on this end.", created_at=when),
        _row("Second reply, also clear, and just as calm as the first one was.", created_at=when),
    ]

    async def fake_fetch(query, *args):
        return rows

    monkeypatch.setattr(db, "fetch", fake_fetch)

    await ruc.sync_real_usage_corpus()

    examples = extract_turns(ruc.REAL_USAGE_DIR.parent)
    assert len(examples) == 2
    texts = {e["text"] for e in examples}
    assert "[Emotional State: curiosity] First reply, all clear, nothing to worry about on this end." in texts
    assert "[Emotional State: curiosity] Second reply, also clear, and just as calm as the first one was." in texts
    assert all(e["era"] == "real-usage" for e in examples)
    assert all(e["emotion"] == "curiosity" for e in examples)


# ---------------------------------------------------------------------------
# sync_learning_events_corpus
# ---------------------------------------------------------------------------

_LEARNING_NODE = """<!DOCTYPE html>
<html><body>
<article data-doc-target="learning.event" data-document-purpose="record"
         data-generated-at="2026-09-17T13:09:03Z">
  <section id="semantic-memory">
    <p>Durable learning event captured by Tala.</p>
    <p>Topic: Skill: something useful.</p>
  </section>
</article>
</body></html>
"""

_NON_LEARNING_NODE = """<!DOCTYPE html>
<html><body>
<article data-doc-target="module.docs" data-document-purpose="record">
  <section id="semantic-memory"><p>Just module documentation.</p></section>
</article>
</body></html>
"""

_UNRECORDED_LEARNING_NODE = """<!DOCTYPE html>
<html><body>
<article data-doc-target="learning.event" data-document-purpose="description">
  <section id="semantic-memory"><p>A description, not a record.</p></section>
</article>
</body></html>
"""


@pytest.mark.asyncio
async def test_a_real_learning_event_node_is_included(monkeypatch, tmp_path, _fake_emotion_engine):
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    (nodes_dir / "learning-skill-example.html").write_text(_LEARNING_NODE, encoding="utf-8")

    monkeypatch.setattr(
        "sdk.agent.documentation_nodes.resolve_active_nodes_dir", lambda: nodes_dir
    )

    result = await ruc.sync_learning_events_corpus()

    assert result["success"] is True
    assert result["scanned"] == 1
    assert result["included"] == 1
    assert len(_fake_emotion_engine) == 1  # derived from data-generated-at

    written = list(ruc.LEARNING_EVENTS_DIR.glob("*.html"))
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    assert 'data-era="learning-events"' in content
    assert "Durable learning event captured by Tala." in content


@pytest.mark.asyncio
async def test_a_non_learning_node_is_skipped_entirely(monkeypatch, tmp_path):
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    (nodes_dir / "module-docs.html").write_text(_NON_LEARNING_NODE, encoding="utf-8")

    monkeypatch.setattr(
        "sdk.agent.documentation_nodes.resolve_active_nodes_dir", lambda: nodes_dir
    )

    result = await ruc.sync_learning_events_corpus()

    assert result["scanned"] == 0
    assert result["included"] == 0
    assert not list(ruc.LEARNING_EVENTS_DIR.glob("*.html"))


@pytest.mark.asyncio
async def test_a_record_with_the_wrong_document_purpose_is_skipped(monkeypatch, tmp_path):
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    (nodes_dir / "learning-desc-only.html").write_text(_UNRECORDED_LEARNING_NODE, encoding="utf-8")

    monkeypatch.setattr(
        "sdk.agent.documentation_nodes.resolve_active_nodes_dir", lambda: nodes_dir
    )

    result = await ruc.sync_learning_events_corpus()

    assert result["scanned"] == 0
    assert result["included"] == 0


@pytest.mark.asyncio
async def test_a_learning_node_with_a_secret_is_excluded(monkeypatch, tmp_path):
    leaking = _LEARNING_NODE.replace(
        "Topic: Skill: something useful.",
        "Topic: used api_key=ABCDEFGHIJKLMNOPQRSTUVWX to call it.",
    )
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    (nodes_dir / "learning-leaky.html").write_text(leaking, encoding="utf-8")

    monkeypatch.setattr(
        "sdk.agent.documentation_nodes.resolve_active_nodes_dir", lambda: nodes_dir
    )

    result = await ruc.sync_learning_events_corpus()

    assert result["scanned"] == 1
    assert result["excluded"] == 1
    assert result["included"] == 0


@pytest.mark.asyncio
async def test_a_missing_nodes_directory_is_reported_cleanly(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(
        "sdk.agent.documentation_nodes.resolve_active_nodes_dir", lambda: missing
    )

    result = await ruc.sync_learning_events_corpus()

    assert result["success"] is True
    assert result["scanned"] == 0
    assert result["included"] == 0


# ---------------------------------------------------------------------------
# _passes_safety_filter (direct unit coverage, no DB/filesystem involved)
# ---------------------------------------------------------------------------

def test_passes_safety_filter_accepts_clean_text():
    assert ruc._passes_safety_filter("A perfectly ordinary sentence.") is True


def test_passes_safety_filter_rejects_empty_text():
    assert ruc._passes_safety_filter("") is False
    assert ruc._passes_safety_filter("   ") is False


def test_passes_safety_filter_rejects_a_secret():
    assert ruc._passes_safety_filter("token: ABCDEFGHIJKLMNOPQRSTUVWXYZ0123") is False


# ---------------------------------------------------------------------------
# What counts as Tala's voice (measured against the real table, 2026-10-24: 1,783 assistant rows, of which 1,422 came from
# internal uuid-named sessions -- planner/coder/goal calls -- and 339 from the renderer's chat-<timestamp> sessions)
# ---------------------------------------------------------------------------

def _patch_fetch(monkeypatch, rows):
    from python.harness import db

    seen = {}

    async def fake_fetch(query, *args):
        seen["sql"] = query
        return rows

    monkeypatch.setattr(db, "fetch", fake_fetch)
    return seen


def _written_text():
    files = list(ruc.REAL_USAGE_DIR.glob("*.html"))
    return files[0].read_text(encoding="utf-8") if files else ""


VOICE = "I think the garden needs water, honestly, so we should go and do that before the heat sets in."


@pytest.mark.asyncio
async def test_only_renderer_chat_sessions_are_read(monkeypatch):
    seen = _patch_fetch(monkeypatch, [])
    await ruc.sync_real_usage_corpus()
    assert "session_id LIKE 'chat-%'" in seen["sql"]


@pytest.mark.asyncio
async def test_reasoning_is_stripped_and_the_stored_emotion_prefix_is_replaced_by_the_derived_one(monkeypatch):
    raw = f"[Emotional State: comfort]\n<thinking>\nThe task is to answer about the garden.\n</thinking>\n{VOICE}"
    _patch_fetch(monkeypatch, [_row(raw)])
    result = await ruc.sync_real_usage_corpus()
    text = _written_text()
    assert result["included"] == 1
    assert VOICE in text
    assert "The task is" not in text and "thinking" not in text
    assert "comfort" not in text  # the label a past run wrote is not the label the engine derives for that moment
    assert text.count("[Emotional State: curiosity]") == 1  # the derived one, in the same surface form the stories use


@pytest.mark.asyncio
async def test_an_unclosed_thinking_block_does_not_leak_its_text(monkeypatch):
    _patch_fetch(monkeypatch, [_row(f"{VOICE}\n<thinking>\nand then the model was cut off mid thought")])
    await ruc.sync_real_usage_corpus()
    assert "cut off mid thought" not in _written_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,content", [
    ("thinking only", "<thinking>\nI should consider the options carefully before replying to anything at all.\n</thinking>"),
    ("code fence", f"{VOICE}\n```python\nprint('hi')\n```"),
    ("markup", f"{VOICE} <call name='x'>go</call>"),
    ("tool narration", f"*(Executing read_workspace_file...)*\n\n{VOICE}"),
    ("system reminder", f"**[SYSTEM REMINDER]** If you decide to invoke a tool, you MUST output the raw XML. {VOICE}"),
    ("bare tool call", 'SEARCH_WORKSPACE_FILES(query="the garden and the heat before it gets late")'),
    ("traceback", "Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n    boom()\nValueError: no"),
    ("replacement char", f"{VOICE} caf�"),
    ("too short", "Okay, sure thing."),
    ("too long", VOICE + " " + "and so it goes " * 400),
])
async def test_agentic_or_malformed_turns_are_excluded_with_a_reason(monkeypatch, reason, content):
    _patch_fetch(monkeypatch, [_row(content)])
    result = await ruc.sync_real_usage_corpus()
    assert result["included"] == 0 and result["excluded"] == 1
    assert result["excluded_by"] == {reason: 1}
    assert _written_text() == ""


@pytest.mark.asyncio
async def test_a_repeated_reply_is_trained_on_once(monkeypatch):
    _patch_fetch(monkeypatch, [_row(VOICE), _row(VOICE), _row(VOICE + " And again, differently, at the end.")])
    result = await ruc.sync_real_usage_corpus()
    assert result["included"] == 2 and result["excluded_by"] == {"duplicate": 1}


@pytest.mark.asyncio
async def test_a_secret_is_reported_as_a_safety_exclusion(monkeypatch):
    _patch_fetch(monkeypatch, [_row("Use Bearer sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 to get in, then wait for the tunnel to open.")])
    result = await ruc.sync_real_usage_corpus()
    assert result["excluded_by"] == {"safety filter": 1}


@pytest.mark.asyncio
async def test_a_failed_sync_leaves_the_existing_corpus_alone(monkeypatch):
    from python.harness import db

    ruc.REAL_USAGE_DIR.mkdir(parents=True)
    (ruc.REAL_USAGE_DIR / "real-usage-corpus.html").write_text("previous run", encoding="utf-8")

    async def boom(query, *args):
        raise RuntimeError("database is down")

    monkeypatch.setattr(db, "fetch", boom)
    result = await ruc.sync_real_usage_corpus()
    assert result["success"] is False
    assert (ruc.REAL_USAGE_DIR / "real-usage-corpus.html").read_text(encoding="utf-8") == "previous run"


def test_the_safety_filter_leaves_no_audit_trail(monkeypatch):
    from python.harness import guardrails_manager as gm

    recorded = []
    monkeypatch.setattr(gm, "_log_violation_to_ledger", lambda kind, details: recorded.append(kind))
    assert ruc._passes_safety_filter("That was a violent threat and I will not repeat it, whatever the tunnel is doing now.") is False
    assert recorded == [], "a rejected training example must not look like a guardrail violation in the audit log"


# --- "new HTML-NN": the nodes that use creates, not the generated documentation ---------------------------------------------


def _node(doc_target, purpose, text, generated="2026-10-20T12:00:00Z", node_type="documentation"):
    purpose_attr = f' data-document-purpose="{purpose}"' if purpose is not None else ""
    target_attr = f' data-doc-target="{doc_target}"' if doc_target else ""
    generated_attr = f' data-generated-at="{generated}"' if generated else ""
    return f"""<!DOCTYPE html>
<html><body>
<article data-node-type="{node_type}"{target_attr}{purpose_attr}{generated_attr}>
  <section id="semantic-memory"><p>{text}</p></section>
</article>
</body></html>
"""


def _write_nodes(tmp_path, monkeypatch, files):
    from sdk.agent import documentation_nodes

    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    for name, body in files.items():
        (nodes_dir / name).write_text(body, encoding="utf-8")
    monkeypatch.setattr(documentation_nodes, "resolve_active_nodes_dir", lambda: nodes_dir)
    return nodes_dir


@pytest.mark.asyncio
async def test_a_remembered_user_fact_is_new_html_nn_even_with_no_declared_purpose(monkeypatch, tmp_path):
    _write_nodes(tmp_path, monkeypatch, {
        "learning-remembered-cat.html": _node("memory.user-fact", None, "The user's cat is called Zorblax and she dislikes Tuesdays."),
    })
    result = await ruc.sync_learning_events_corpus()
    assert result["included"] == 1
    assert "Zorblax" in next(ruc.LEARNING_EVENTS_DIR.glob("*.html")).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_learning_lesson_with_no_declared_purpose_is_included_too(monkeypatch, tmp_path):
    _write_nodes(tmp_path, monkeypatch, {
        "learning-when-writing-tests.html": _node("learning.event", None, "When writing tests, name what the test proves, not how it works."),
    })
    assert (await ruc.sync_learning_events_corpus())["included"] == 1


@pytest.mark.asyncio
async def test_generated_documentation_and_journals_are_not_experience(monkeypatch, tmp_path):
    _write_nodes(tmp_path, monkeypatch, {
        "sdk_agent_tools.html": _node("sdk.agent.tools", "description", "Module documentation generated from the source code."),
        "dream-journal-2026-10-24.html": _node("memory.dream-journal", "record", "Dream journal for 2026-10-24. Nothing changed tonight."),
        "learning-skill-ok.html": _node("learning.event", "record", "A lesson that was actually learned during real work, in full sentences."),
    })
    result = await ruc.sync_learning_events_corpus()
    assert result["included"] == 1 and result["scanned"] == 1  # the other two were never candidates


@pytest.mark.asyncio
async def test_failure_recovery_notes_are_left_out_with_a_reason(monkeypatch, tmp_path):
    _write_nodes(tmp_path, monkeypatch, {
        "learning-recovery-create-a-bounded-self-improvement-plan.html":
            _node("learning.event", "record", "Recovery: the plan step failed with an error and was retried three times."),
        "learning-skill-ok.html": _node("learning.event", "record", "A lesson that was actually learned during real work, in full sentences."),
    })
    result = await ruc.sync_learning_events_corpus()
    assert result["included"] == 1 and result["excluded_by"] == {"recovery note": 1}


@pytest.mark.asyncio
async def test_a_secret_in_a_node_is_reported_as_a_safety_exclusion(monkeypatch, tmp_path):
    _write_nodes(tmp_path, monkeypatch, {
        "learning-skill-leaky.html": _node("learning.event", "record", "Use Bearer sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 to authenticate with the service."),
    })
    result = await ruc.sync_learning_events_corpus()
    assert result["included"] == 0 and result["excluded_by"] == {"safety filter": 1}


@pytest.mark.asyncio
async def test_new_since_counts_only_what_was_created_after_the_last_training(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    _write_nodes(tmp_path, monkeypatch, {
        "learning-skill-old.html": _node("learning.event", "record", "An older lesson that was learned in full sentences, long ago.", "2026-09-01T00:00:00Z"),
        "learning-skill-new.html": _node("learning.event", "record", "A newer lesson that was learned in full sentences, yesterday.", "2026-10-24T00:00:00Z"),
    })
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    result = await ruc.sync_learning_events_corpus(since=since)
    assert result["included"] == 2 and result["new_since"] == 1
    assert (await ruc.sync_learning_events_corpus())["new_since"] == 2, "with no earlier training, everything is new"


@pytest.mark.asyncio
async def test_new_chat_replies_since_the_last_training_are_counted(monkeypatch):
    from datetime import datetime, timezone

    old = datetime(2026, 8, 1, tzinfo=timezone.utc)
    new = datetime(2026, 10, 20, tzinfo=timezone.utc)
    _patch_fetch(monkeypatch, [
        _row(VOICE + " An old reply.", created_at=old), _row(VOICE + " A new reply.", created_at=new),
    ])
    result = await ruc.sync_real_usage_corpus(since=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result["included"] == 2 and result["new_since"] == 1


@pytest.mark.asyncio
async def test_a_skill_node_is_one_of_her_skills_and_is_included(monkeypatch, tmp_path):
    _write_nodes(tmp_path, monkeypatch, {
        "skill-self-check-discipline.html": _node(None, None, "Self-check discipline: before answering, restate the question and check the answer against it.",
                                                  node_type="skill"),
    })
    result = await ruc.sync_learning_events_corpus()
    assert result["included"] == 1
    assert "Self-check discipline" in next(ruc.LEARNING_EVENTS_DIR.glob("*.html")).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_her_persona_and_identity_nodes_are_not_trained_in(monkeypatch, tmp_path):
    """They are her protected core and already reach every prompt; training them into the weights would also make them
    impossible to edit by editing the node."""
    _write_nodes(tmp_path, monkeypatch, {
        "persona-tala-core.html": _node(None, None, "You are Tala. You are practical, observant and warm, and you answer plainly.", node_type="persona"),
        "identity-tala.html": _node(None, None, "Tala is an offline-first cognitive agent and workspace companion for Weavor.", node_type="identity"),
        "function-doc-x.html": _node("code.x", "description", "Function documentation generated from the source code of x.", node_type="function-doc"),
    })
    assert (await ruc.sync_learning_events_corpus())["scanned"] == 0


@pytest.mark.asyncio
async def test_a_node_with_no_timestamp_is_dated_by_its_file(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    _write_nodes(tmp_path, monkeypatch, {
        "skill-undated.html": _node(None, None, "An undated skill that was written down in full sentences for later.", generated="", node_type="skill"),
    })
    result = await ruc.sync_learning_events_corpus(since=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert result["included"] == 1 and result["new_since"] == 1
    far_future = datetime(2100, 1, 1, tzinfo=timezone.utc)
    assert (await ruc.sync_learning_events_corpus(since=far_future))["new_since"] == 0


@pytest.mark.asyncio
async def test_a_secured_node_is_never_trained_in(monkeypatch, tmp_path):
    """compile_lora_adapter has always refused to encode a node whose metadata says access_level SECURED into weights. The corpus
    reads nodes directly, so the same rule has to hold here, from the first real training run."""
    secured = _node("learning.event", "record", "A lesson about a private matter that must never reach any weights file.").replace(
        "</article>", '<script type="application/json" id="node-metadata">{"access_level": "SECURED"}</script></article>')
    nested = _node("learning.event", "record", "A lesson whose state block marks one inner part as secured, so all of it stays out.").replace(
        "</article>", '<script type="application/json" id="node-state">{"a": {"b": [{"access_level": "SECURED"}]}}</script></article>')
    fine = _node("learning.event", "record", "A lesson with ordinary metadata that was learned during real work.").replace(
        "</article>", '<script type="application/json" id="node-metadata">{"access_level": "PUBLIC"}</script></article>')
    _write_nodes(tmp_path, monkeypatch, {"learning-a.html": secured, "learning-b.html": nested, "learning-c.html": fine})
    result = await ruc.sync_learning_events_corpus()
    assert result["included"] == 1 and result["excluded_by"] == {"secured": 2}
    assert "private matter" not in next(ruc.LEARNING_EVENTS_DIR.glob("*.html")).read_text(encoding="utf-8")
