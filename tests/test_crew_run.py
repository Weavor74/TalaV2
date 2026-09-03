"""
Running a crew: many agents on one goal, at once.

``crew.assign`` routes a single task to a single member. ``crew.run`` is the
other thing a team is for — everyone works the same goal concurrently and, when
the crew has a manager, that manager synthesises the answers into one. These
tests drive the real ``crew_runs`` persistence through the isolated test
database while injecting a fake model, so they assert the orchestration
(concurrency, manager synthesis, persistence) without needing a live LLM.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from python.harness import crews, db


async def _mk_crew(crew_id: str, name: str = "Test Team") -> None:
    await db.execute(
        "INSERT INTO crews (id, name, kind) VALUES ($1, $2, 'team') "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        crew_id, name, caller="test")


async def _mk_agent(agent_id: str, name: str, role: str, crew_id: str,
                    reports_to: str | None = None, system_prompt: str = "") -> None:
    await db.execute(
        "INSERT INTO agents (id, name, type, role, crew_id, reports_to, config, updated_at) "
        "VALUES ($1, $2, 'crew', $3, $4, $5, $6::jsonb, NOW()) "
        "ON CONFLICT (id) DO UPDATE SET crew_id = EXCLUDED.crew_id, "
        "reports_to = EXCLUDED.reports_to, config = EXCLUDED.config",
        agent_id, name, role, crew_id, reports_to,
        json.dumps({"system_prompt": system_prompt}), caller="test")


async def _cleanup(crew_id: str, agent_ids: list[str]) -> None:
    await db.execute("DELETE FROM crew_runs WHERE crew_id = $1", crew_id, caller="test")
    # Clear reports_to first so the self-referential FK does not block deletion.
    for a in agent_ids:
        await db.execute("UPDATE agents SET reports_to = NULL WHERE id = $1", a, caller="test")
    for a in agent_ids:
        await db.execute("DELETE FROM agents WHERE id = $1", a, caller="test")
    await db.execute("DELETE FROM crews WHERE id = $1", crew_id, caller="test")


class _Recorder:
    """A fake model that records concurrency and marks the synthesis call."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple[str, str]] = []

    async def generate(self, system_prompt: str, user_prompt: str,
                       max_tokens: int | None = None) -> str:
        self.calls.append((system_prompt, user_prompt))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            # Hold the turn so genuinely concurrent workers pile up here and
            # max_active reflects how many ran at once.
            await asyncio.sleep(0.05)
        finally:
            self.active -= 1
        if "synthesise" in user_prompt or "team's goal" in user_prompt:
            return "SYNTHESIS"
        return f"answer[{system_prompt[:24]}]"


@pytest.mark.asyncio
async def test_a_team_runs_every_member_at_once_and_the_manager_synthesises():
    await db.init_pool()
    crew_id = "crew-test-run-hier"
    ids = ["agent-test-mgr", "agent-test-w1", "agent-test-w2"]
    await _cleanup(crew_id, ids)
    try:
        await _mk_crew(crew_id, "Research Team")
        await _mk_agent("agent-test-mgr", "Lead", "manager", crew_id,
                        system_prompt="You are the lead.")
        await _mk_agent("agent-test-w1", "Ana", "researcher", crew_id,
                        reports_to="agent-test-mgr", system_prompt="You are Ana.")
        await _mk_agent("agent-test-w2", "Bo", "writer", crew_id,
                        reports_to="agent-test-mgr", system_prompt="You are Bo.")

        rec = _Recorder()
        result = await crews.run_crew(
            crew_id, "Summarise the plan.", generate=rec.generate)

        # The two workers ran concurrently; the manager is not one of them.
        assert rec.max_active == 2, f"workers did not run at once (max={rec.max_active})"
        assert result["status"] == "completed"
        assert result["manager"] == {"id": "agent-test-mgr", "name": "Lead"}
        assert {m["agent_id"] for m in result["members"]} == {"agent-test-w1", "agent-test-w2"}
        # The final is the manager's synthesis, produced from the workers' answers.
        assert result["final"] == "SYNTHESIS"
        synth_calls = [c for c in rec.calls if "team's goal" in c[1]]
        assert len(synth_calls) == 1, "manager did not synthesise exactly once"

        # It is persisted and readable back.
        persisted = await crews.get_run(result["run_id"])
        assert persisted is not None
        assert persisted["status"] == "completed"
        assert len(persisted["members"]) == 2
        assert persisted["manager_id"] == "agent-test-mgr"
        listed = await crews.list_runs(crew_id=crew_id)
        assert any(r["id"] == result["run_id"] for r in listed)
    finally:
        await _cleanup(crew_id, ids)
        await db.close_pool()


@pytest.mark.asyncio
async def test_a_flat_team_collects_every_answer_with_no_manager():
    await db.init_pool()
    crew_id = "crew-test-run-flat"
    ids = ["agent-test-f1", "agent-test-f2", "agent-test-f3"]
    await _cleanup(crew_id, ids)
    try:
        await _mk_crew(crew_id, "Panel")
        await _mk_agent("agent-test-f1", "Cy", "analyst", crew_id)
        await _mk_agent("agent-test-f2", "Di", "designer", crew_id)
        await _mk_agent("agent-test-f3", "Ed", "engineer", crew_id)

        rec = _Recorder()
        result = await crews.run_crew(crew_id, "Propose an approach.",
                                      generate=rec.generate)

        # No reporting structure -> no manager, all three run at once.
        assert result["manager"] is None
        assert rec.max_active == 3
        assert len(result["members"]) == 3
        # The final collects each member's answer under their name.
        for name in ("Cy", "Di", "Ed"):
            assert name in result["final"]
    finally:
        await _cleanup(crew_id, ids)
        await db.close_pool()


@pytest.mark.asyncio
async def test_a_run_needs_a_goal_and_a_populated_crew():
    await db.init_pool()
    crew_id = "crew-test-run-empty"
    await _cleanup(crew_id, [])
    try:
        await _mk_crew(crew_id, "Empty")
        with pytest.raises(crews.CrewError):
            await crews.run_crew(crew_id, "", generate=lambda *a: _noop())
        with pytest.raises(crews.CrewError):
            await crews.run_crew(crew_id, "a goal", generate=lambda *a: _noop())
    finally:
        await _cleanup(crew_id, [])
        await db.close_pool()


async def _noop() -> str:
    return ""
