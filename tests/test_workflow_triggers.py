"""NAME
       test_workflow_triggers - triggers fire flows; nothing fires twice
DESCRIPTION
       Bindings come from the flow documents (HTML-NN first), schedules fire
       once per matched minute, channel messages respect filter and pattern,
       and a still-running flow is never stacked.
"""
from datetime import datetime

import pytest

from python.harness import workflow_triggers as wt


def _flow(flow_id, config):
    return {
        "flow_id": flow_id,
        "name": f"flow {flow_id}",
        "nodes": [{"id": "n1", "type": "trigger", "config": config}],
    }


def test_manual_and_absent_triggers_bind_nothing():
    assert wt._binding_of(_flow("a", {})) is None
    assert wt._binding_of(_flow("b", {"source": "manual"})) is None
    assert wt._binding_of({"flow_id": "c", "nodes": []}) is None


def test_schedule_binding_keeps_invalid_cron_visible():
    good = wt._binding_of(_flow("a", {"source": "schedule", "cron": "*/5 * * * *"}))
    assert good.cron is not None and not good.cron_error
    bad = wt._binding_of(_flow("b", {"source": "schedule", "cron": "not cron"}))
    assert bad is not None and bad.cron is None and bad.cron_error


def _service_with(bindings):
    service = wt.WorkflowTriggerService()
    service._bindings = bindings
    fired = []
    service._spawn = lambda binding, inputs: fired.append((binding.flow_id, inputs))
    return service, fired


def test_schedule_fires_once_per_matched_minute():
    binding = wt._binding_of(_flow("a", {"source": "schedule", "cron": "* * * * *"}))
    service, fired = _service_with([binding])
    now = datetime(2026, 9, 13, 9, 30, 5)
    service._check_schedules(now)
    service._check_schedules(now.replace(second=45))  # same minute: no re-fire
    assert [f[0] for f in fired] == ["a"]
    service._check_schedules(datetime(2026, 9, 13, 9, 31, 2))
    assert len(fired) == 2
    assert fired[0][1]["source"] == "schedule" and "fired_at" in fired[0][1]


@pytest.mark.asyncio
async def test_channel_dispatch_respects_filter_and_pattern():
    match = wt._binding_of(_flow("a", {
        "source": "channel", "channel": "telegram", "pattern": r"^deploy\b",
    }))
    any_channel = wt._binding_of(_flow("b", {"source": "channel"}))
    other_platform = wt._binding_of(_flow("c", {
        "source": "channel", "channel": "discord",
    }))
    service, fired = _service_with([match, any_channel, other_platform])
    msg = {"channel_id": "ch-1", "platform": "telegram", "chat_id": 7,
           "sender": "steven", "text": "deploy the beta"}
    await service.on_channel_message(msg)
    assert sorted(f[0] for f in fired) == ["a", "b"]
    payload = dict(fired[0][1])
    assert payload["source"] == "channel" and payload["text"] == "deploy the beta"
    assert payload["chat_id"] == 7 and payload["platform"] == "telegram"

    fired.clear()
    await service.on_channel_message({**msg, "text": "please deploy later"})
    assert sorted(f[0] for f in fired) == ["b"]  # pattern anchors at start


def test_invalid_regex_falls_back_to_substring():
    binding = wt._binding_of(_flow("a", {"source": "channel", "pattern": "deploy ("}))
    assert binding.pattern is None
    assert service_matches(binding, "Please DEPLOY ( now")
    assert not service_matches(binding, "nothing relevant")


def service_matches(binding, text):
    return wt.WorkflowTriggerService._text_matches(binding, text)


def test_spawn_skips_flow_already_running():
    binding = wt._binding_of(_flow("a", {"source": "startup"}))
    service = wt.WorkflowTriggerService()
    ran = []
    service._run = lambda b, i: ran.append(b.flow_id)  # never awaited below
    service._active.add("a")
    service._spawn(binding, {"source": "startup"})
    assert ran == [] and not service._run_tasks


@pytest.mark.asyncio
async def test_run_executes_document_with_trigger_inputs(monkeypatch):
    binding = wt._binding_of(_flow("a", {"source": "startup"}))
    service = wt.WorkflowTriggerService()
    monkeypatch.setattr(
        "python.harness.workflows_util.load_flow_document",
        lambda flow_id: ({"flow_id": flow_id, "nodes": []}, "html-nn"),
    )
    seen = {}

    class FakeExecutor:
        def __init__(self, document, callback):
            seen["document"] = document

        async def execute(self, inputs):
            seen["inputs"] = inputs
            return {"status": "COMPLETED"}

    monkeypatch.setattr("python.harness.workflow_executor.WorkflowExecutor", FakeExecutor)
    service._active.add("a")  # as _spawn would have
    await service._run(binding, {"source": "startup"})
    assert seen["document"]["flow_id"] == "a"
    assert seen["inputs"] == {"source": "startup"}
    assert "a" not in service._active  # released even on this path


@pytest.mark.asyncio
async def test_reload_reads_flows_and_keeps_minute_stamps(monkeypatch):
    service = wt.WorkflowTriggerService()
    flows = [_flow("a", {"source": "schedule", "cron": "* * * * *"})]
    monkeypatch.setattr(
        "python.harness.workflows_util.get_all_workflows", lambda: flows
    )
    await service.reload()
    assert [b.flow_id for b in service._bindings] == ["a"]
    service._bindings[0].last_fired_minute = "2026-09-13 09:30"
    await service.reload()  # a reload inside a matched minute must not re-arm it
    assert service._bindings[0].last_fired_minute == "2026-09-13 09:30"


@pytest.mark.asyncio
async def test_channel_listener_tee_survives_a_failing_listener():
    from python.harness.channels import ChannelManager

    manager = ChannelManager()
    heard = []

    def bad(msg):
        raise RuntimeError("listener bug")

    manager.register_listener(bad)
    manager.register_listener(heard.append)
    manager.register_listener(heard.append)  # duplicates register once
    await manager._on_message({"text": "hello"})
    assert heard == [{"text": "hello"}]
