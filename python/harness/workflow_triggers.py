"""
python/harness/workflow_triggers.py — workflows that start themselves.
======================================================================

The editor's trigger node declared where a flow begins and nothing more:
every workflow ran exactly when a human pressed Run or a model called
workflow.execute. The trigger's config now names a *source* — schedule,
channel, startup — and this service watches those sources and starts the
flows, so "trigger" stops being a label and becomes behaviour.

Flows are read through workflows_util.get_all_workflows(), which prefers the
compiled HTML-NN over its JSON sidecar: the node is the document the system
consumes, so a trigger Tala writes into a node herself is exactly as live as
one drawn in the editor.

Tick- and poll-based like channels.py, and for the same reason: Tala runs on
a laptop behind NAT with no public URL. There are no webhooks here to be
missing. Event payloads flow into the run as the executor's initial inputs,
so steps reference them the way they reference anything else: a channel-
triggered flow reads {{text}}, {{sender}}, {{chat_id}} and can answer back
with a tool step aimed at that chat_id.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

#: Cron resolution is one minute; ticking three times a minute means a matching
#: minute cannot be slept over, and the per-binding minute stamp keeps one
#: match from firing three times.
_TICK_SECONDS = 20.0


@dataclass
class TriggerBinding:
    """One flow's live trigger, extracted from its trigger node's config."""

    flow_id: str
    flow_name: str
    source: str  # "schedule" | "channel" | "startup"
    cron: Any = None
    cron_error: str = ""
    channel: str = ""
    pattern_text: str = ""
    pattern: re.Pattern | None = None
    last_fired_minute: str = field(default="", compare=False)


def _binding_of(flow: dict[str, Any]) -> TriggerBinding | None:
    """The flow's trigger binding, or None when it only starts manually."""
    trigger = next(
        (n for n in flow.get("nodes") or [] if n.get("type") == "trigger"), None
    )
    if trigger is None:
        return None
    config = trigger.get("config") or {}
    source = str(config.get("source") or "manual").strip().lower()
    if source not in ("schedule", "channel", "startup"):
        return None
    binding = TriggerBinding(
        flow_id=str(flow.get("flow_id") or ""),
        flow_name=str(flow.get("name") or ""),
        source=source,
        channel=str(config.get("channel") or "").strip().lower(),
        pattern_text=str(config.get("pattern") or "").strip(),
    )
    if not binding.flow_id:
        return None
    if source == "schedule":
        from python.harness.heartbeat_scheduler import CronExpression

        try:
            binding.cron = CronExpression(str(config.get("cron") or "").strip())
        except Exception as exc:
            # Kept, not dropped: an unhealthy trigger that shows up in status()
            # with its reason is diagnosable; one silently filtered out reads
            # as "triggers don't work".
            binding.cron_error = str(exc)
    if binding.pattern_text:
        try:
            binding.pattern = re.compile(binding.pattern_text, re.IGNORECASE)
        except re.error:
            binding.pattern = None  # falls back to substring matching
    return binding


class WorkflowTriggerService:
    """Singleton that runs schedule/startup/channel workflow triggers."""

    _instance: "WorkflowTriggerService | None" = None

    @classmethod
    def get(cls) -> "WorkflowTriggerService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._bindings: list[TriggerBinding] = []
        self._task: asyncio.Task | None = None
        self._running = False
        #: flow_ids currently executing. One trigger firing while its previous
        #: run is still going must not stack a second run on top: a chatty
        #: channel or a slow flow on a fast cron would otherwise pile up.
        self._active: set[str] = set()
        self._run_tasks: set[asyncio.Task] = set()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.reload()
        try:
            from python.harness.channels import ChannelManager

            ChannelManager.get().register_listener(self.on_channel_message)
        except Exception as exc:
            log.warning("workflow_triggers: channel listener not registered: %s", exc)
        for binding in [b for b in self._bindings if b.source == "startup"]:
            self._spawn(binding, {"source": "startup"})
        self._task = asyncio.create_task(self._tick_loop())
        log.info(
            "workflow_triggers: started with %d binding(s)", len(self._bindings)
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def reload(self) -> None:
        """Re-scan saved flows. Called at start and by save_workflow_data."""
        from python.harness.workflows_util import get_all_workflows

        flows = await asyncio.to_thread(get_all_workflows)
        bindings: list[TriggerBinding] = []
        for flow in flows:
            try:
                binding = _binding_of(flow)
            except Exception as exc:
                log.warning(
                    "workflow_triggers: unreadable trigger in %r: %s",
                    flow.get("flow_id"), exc,
                )
                continue
            if binding is None:
                continue
            if binding.cron_error:
                log.warning(
                    "workflow_triggers: %r has an invalid cron and will not fire: %s",
                    binding.flow_id, binding.cron_error,
                )
            bindings.append(binding)
        # A reload inside a matched minute must not re-arm that minute.
        stamps = {b.flow_id: b.last_fired_minute for b in self._bindings}
        for binding in bindings:
            binding.last_fired_minute = stamps.get(binding.flow_id, "")
        self._bindings = bindings
        log.info("workflow_triggers: %d trigger binding(s) loaded", len(bindings))

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "active_runs": sorted(self._active),
            "bindings": [
                {
                    "flow_id": b.flow_id,
                    "name": b.flow_name,
                    "source": b.source,
                    "channel": b.channel,
                    "pattern": b.pattern_text,
                    "error": b.cron_error,
                }
                for b in self._bindings
            ],
        }

    # -- schedule ------------------------------------------------------------

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                self._check_schedules(datetime.now())
            except Exception as exc:
                log.warning("workflow_triggers: tick failed: %s", exc)
            await asyncio.sleep(_TICK_SECONDS)

    def _check_schedules(self, now: datetime) -> None:
        minute = now.strftime("%Y-%m-%d %H:%M")
        for binding in self._bindings:
            if binding.source != "schedule" or binding.cron is None:
                continue
            if binding.last_fired_minute == minute:
                continue
            if binding.cron.matches(now):
                binding.last_fired_minute = minute
                self._spawn(binding, {
                    "source": "schedule",
                    "fired_at": now.isoformat(timespec="seconds"),
                })

    # -- channel -------------------------------------------------------------

    async def on_channel_message(self, msg: dict[str, Any]) -> None:
        """Fed every allowlisted inbound message by ChannelManager."""
        text = str(msg.get("text") or "")
        for binding in self._bindings:
            if binding.source != "channel":
                continue
            if not self._channel_matches(binding, msg):
                continue
            if not self._text_matches(binding, text):
                continue
            payload = {"source": "channel"}
            for key in ("channel_id", "platform", "chat_id", "sender", "text"):
                payload[key] = msg.get(key)
            self._spawn(binding, payload)

    @staticmethod
    def _channel_matches(binding: TriggerBinding, msg: dict[str, Any]) -> bool:
        if not binding.channel:
            return True
        candidates = {
            str(msg.get("platform") or "").lower(),
            str(msg.get("channel_id") or "").lower(),
        }
        try:
            from python.harness.channels import ChannelManager

            adapter = ChannelManager.get()._adapters.get(msg.get("channel_id"))
            if adapter is not None:
                candidates.add(str(adapter.name).lower())
        except Exception:
            pass
        return binding.channel in candidates

    @staticmethod
    def _text_matches(binding: TriggerBinding, text: str) -> bool:
        if not binding.pattern_text:
            return True
        if binding.pattern is not None:
            return bool(binding.pattern.search(text))
        return binding.pattern_text.lower() in text.lower()

    # -- firing --------------------------------------------------------------

    def _spawn(self, binding: TriggerBinding, inputs: dict[str, Any]) -> None:
        if binding.flow_id in self._active:
            log.info(
                "workflow_triggers: %r is still running; %s trigger skipped",
                binding.flow_id, inputs.get("source"),
            )
            return
        self._active.add(binding.flow_id)
        task = asyncio.create_task(self._run(binding, inputs))
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)

    async def _run(self, binding: TriggerBinding, inputs: dict[str, Any]) -> None:
        try:
            from python.harness.workflow_executor import WorkflowExecutor
            from python.harness.workflows_util import load_flow_document

            document, doc_source = await asyncio.to_thread(
                load_flow_document, binding.flow_id
            )
            if document is None:
                log.warning(
                    "workflow_triggers: %r fired but no longer loads", binding.flow_id
                )
                return

            async def step_callback(node_id, status, message, logs) -> None:
                log.debug(
                    "workflow_triggers: %s/%s %s: %s",
                    binding.flow_id, node_id, status, message,
                )

            result = await WorkflowExecutor(document, step_callback).execute(inputs)
            outcome = "completed" if result.get("success", True) else "failed"
            log.info(
                "workflow_triggers: %r %s (trigger=%s, from=%s)",
                binding.flow_id, outcome, inputs.get("source"), doc_source,
            )
            try:
                from python.harness import db

                await db.log_to_db(
                    "INFO" if outcome == "completed" else "WARNING",
                    f"Trigger ({inputs.get('source')}) ran workflow "
                    f"'{binding.flow_name or binding.flow_id}': {outcome}",
                    {"flow_id": binding.flow_id, "trigger": inputs.get("source")},
                )
            except Exception:
                pass  # a run without a DB row is still a run
        except Exception:
            log.exception("workflow_triggers: %r crashed", binding.flow_id)
        finally:
            self._active.discard(binding.flow_id)
