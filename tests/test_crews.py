"""
Crews: the organisation, and what the hierarchy is *for*.

Steven's taxonomy, and the reason crews and swarms are separate things:

============  ==========================================  =========================================
              Crew                                        Swarm
============  ==========================================  =========================================
topology      structured — pipelines or manager trees      decentralised mesh, peer-to-peer routing
control flow  deterministic, defined upfront               emergent — handoffs as conditions evolve
roles         deep personas, fixed responsibilities        thin utility functions
============  ==========================================  =========================================

The dividing line is **control flow**, not headcount. Everything here is
deliberately deterministic: work goes where the structure says, and when it
cannot be done it goes up the reporting line. An agent choosing its own
successor would be a swarm, and this is not that.

Three findings met here:

* **UI-049** — hierarchy could not be written down at all. No ``parent_id``,
  ``manager_id``, ``reports_to`` or ``supervisor`` existed anywhere.
* **UI-050** — ``crew_type`` is a *category*. Two agents both labelled
  "Business" are not colleagues, so a crew had to become an entity before a
  picker could mean anything.
* **UI-051** — the crew fields were write-only. Marking an agent as crew
  changed a chip on a card and nothing else, which is why most of this file is
  about :func:`assign` and :func:`escalate` rather than about storage.
"""
from __future__ import annotations

import pytest

from python.harness import crews


@pytest.fixture
def org(monkeypatch):
    """An organisation in memory, with the two tables the module touches."""
    state: dict = {
        "crews": {},
        "agents": {},
        "tasks": {},
        "logged": [],
    }

    from python.harness import db

    async def fetchval(query, *args):
        q = " ".join(query.split())
        if "FROM crews WHERE id" in q:
            return 1 if args[0] in state["crews"] else None
        if "FROM agents WHERE id" in q:
            return 1 if args[0] in state["agents"] else None
        if "COUNT(*) FROM agents WHERE crew_id" in q:
            return sum(1 for a in state["agents"].values()
                       if a.get("crew_id") == args[0])
        if "COUNT(*) FROM crews WHERE parent_crew_id" in q:
            return sum(1 for c in state["crews"].values()
                       if c.get("parent_crew_id") == args[0])
        return None

    async def fetchrow(query, *args):
        q = " ".join(query.split())
        if "SELECT parent_crew_id FROM crews" in q:
            crew = state["crews"].get(args[0])
            return {"parent_crew_id": crew["parent_crew_id"]} if crew else None
        if "JOIN agents m ON m.id = a.reports_to" in q:
            agent = state["agents"].get(args[0])
            manager = state["agents"].get(agent["reports_to"]) if agent else None
            if not manager:
                return None
            return {"id": manager["id"], "name": manager["name"],
                    "role": manager["role"], "crew_id": manager.get("crew_id")}
        if "SELECT assigned_agent_id FROM tasks" in q:
            return state["tasks"].get(args[0])
        return None

    async def fetch(query, *args):
        q = " ".join(query.split())
        if "FROM agents WHERE crew_id" in q:
            found = [a for a in state["agents"].values()
                     if a.get("crew_id") == args[0]]
            return sorted(found, key=lambda a: (not a["is_active"], a["name"]))
        if "WHERE reports_to = ANY" in q:
            return [a for a in state["agents"].values()
                    if a.get("reports_to") in args[0]]
        if "WHERE reports_to = $1" in q:
            return [a for a in state["agents"].values()
                    if a.get("reports_to") == args[0]]
        if "WHERE reports_to IS NOT NULL" in q:
            return [{"id": a["id"], "reports_to": a["reports_to"]}
                    for a in state["agents"].values() if a.get("reports_to")]
        if "FROM crews c" in q:
            return [{**c, "members": sum(1 for a in state["agents"].values()
                                         if a.get("crew_id") == c["id"])}
                    for c in state["crews"].values()]
        if "FROM agents ORDER BY name" in q:
            return list(state["agents"].values())
        return []

    async def execute(query, *args, caller=""):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO crews"):
            state["crews"][args[0]] = {
                "id": args[0], "name": args[1], "kind": args[2],
                "parent_crew_id": args[3], "purpose": args[4],
                "description": args[5]}
        elif "UPDATE agents SET crew_id" in q:
            state["agents"][args[0]]["crew_id"] = args[1]
            state["agents"][args[0]]["reports_to"] = args[2]
        elif "UPDATE tasks SET assigned_agent_id" in q:
            if args[0] not in state["tasks"]:
                return "UPDATE 0"
            state["tasks"][args[0]] = {"assigned_agent_id": args[1]}
        elif "DELETE FROM crews" in q:
            state["crews"].pop(args[0], None)
        elif "UPDATE crews SET parent_crew_id" in q:
            for crew in state["crews"].values():
                if crew["parent_crew_id"] == args[0]:
                    crew["parent_crew_id"] = args[1]
        return "UPDATE 1"

    async def log_to_db(level, message, trace=None, tx_id=None):
        state["logged"].append((level, message))

    monkeypatch.setattr(db, "fetchval", fetchval)
    monkeypatch.setattr(db, "fetchrow", fetchrow)
    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "log_to_db", log_to_db)

    def add_agent(agent_id, name="", role="", crew_id=None, reports_to=None,
                  is_active=True):
        state["agents"][agent_id] = {
            "id": agent_id, "name": name or agent_id, "role": role,
            "crew_type": "", "crew_id": crew_id, "reports_to": reports_to,
            "is_active": is_active}
        return agent_id

    def add_task(task_id, assigned=None):
        state["tasks"][task_id] = {"assigned_agent_id": assigned}
        return task_id

    state["add_agent"] = add_agent
    state["add_task"] = add_task
    return state


# -- crews are entities -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_crew_is_a_thing_not_a_label(org):
    """`crew_type` holds classifications, so two agents sharing one are not
    colleagues (UI-050). Membership needed an entity to point at."""
    team = await crews.create("Repair", crews.TEAM, purpose="fix things")

    assert team["id"].startswith("crew-")
    assert org["crews"][team["id"]]["purpose"] == "fix things"


@pytest.mark.asyncio
async def test_teams_sit_inside_departments(org):
    """200 members in teams of 5, each reporting to a department."""
    dept = await crews.create("Engineering", crews.DEPARTMENT)
    team = await crews.create("Repair", crews.TEAM, dept["id"])

    assert team["parent_crew_id"] == dept["id"]


@pytest.mark.asyncio
async def test_a_crew_needs_a_name(org):
    with pytest.raises(crews.CrewError):
        await crews.create("   ")


@pytest.mark.asyncio
async def test_an_unknown_kind_is_refused(org):
    with pytest.raises(crews.CrewError):
        await crews.create("Repair", "battalion")


@pytest.mark.asyncio
async def test_a_crew_cannot_sit_inside_itself(org):
    dept = await crews.create("Engineering", crews.DEPARTMENT)

    with pytest.raises(crews.CrewError):
        await crews.update(dept["id"], parent_crew_id=dept["id"])


@pytest.mark.asyncio
async def test_a_crew_cannot_sit_inside_its_own_descendant(org):
    """Departments containing each other is not a hierarchy, and every walk
    over it would loop."""
    dept = await crews.create("Engineering", crews.DEPARTMENT)
    team = await crews.create("Repair", crews.TEAM, dept["id"])

    with pytest.raises(crews.CrewError):
        await crews.update(dept["id"], parent_crew_id=team["id"])


# -- reporting ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_chain_of_command_reads_upward(org):
    """Before this, agents named `Boss` and `Worker` were siblings as far as
    the system was concerned (UI-049)."""
    org["add_agent"]("boss", "Boss", "Manager")
    org["add_agent"]("arch", "Arch", "Architect", reports_to="boss")
    org["add_agent"]("dev", "Dev", "Developer", reports_to="arch")

    chain = await crews._chain_of_command("dev")

    assert [link["id"] for link in chain] == ["arch", "boss"]


@pytest.mark.asyncio
async def test_subordinates_reach_all_the_way_down(org):
    org["add_agent"]("boss")
    org["add_agent"]("arch", reports_to="boss")
    org["add_agent"]("dev", reports_to="arch")

    assert {a["id"] for a in await crews.subordinates("boss")} == {"arch", "dev"}
    assert {a["id"] for a in await crews.subordinates("boss", direct_only=True)} == {"arch"}


@pytest.mark.asyncio
async def test_ownership_reaches_any_depth(org):
    """A department head who cannot touch anything two levels down is not a
    department head."""
    org["add_agent"]("boss")
    org["add_agent"]("arch", reports_to="boss")
    org["add_agent"]("dev", reports_to="arch")

    assert await crews.owns("boss", "dev")
    assert await crews.owns("arch", "dev")
    assert not await crews.owns("dev", "boss")


@pytest.mark.asyncio
async def test_an_agent_cannot_report_to_itself(org):
    org["add_agent"]("solo")

    with pytest.raises(crews.CrewError):
        await crews.set_membership("solo", reports_to="solo")


@pytest.mark.asyncio
async def test_a_reporting_loop_is_refused(org):
    """Not a strange configuration to be tolerated: a cycle makes every upward
    walk run forever, so work stops rather than misroutes."""
    org["add_agent"]("boss")
    org["add_agent"]("dev", reports_to="boss")

    with pytest.raises(crews.CrewError) as raised:
        await crews.set_membership("boss", reports_to="dev")

    assert "loop" in str(raised.value)


@pytest.mark.asyncio
async def test_a_longer_loop_is_refused_too(org):
    org["add_agent"]("a")
    org["add_agent"]("b", reports_to="a")
    org["add_agent"]("c", reports_to="b")

    with pytest.raises(crews.CrewError):
        await crews.set_membership("a", reports_to="c")


@pytest.mark.asyncio
async def test_a_walk_over_an_existing_loop_terminates(org):
    """set_membership will not create one, but a row written another way is
    not covered by that — and an unbounded walk over a loop never returns."""
    org["add_agent"]("a", reports_to="b")
    org["add_agent"]("b", reports_to="a")

    chain = await crews._chain_of_command("a")

    assert len(chain) < crews.MAX_DEPTH


@pytest.mark.asyncio
async def test_loops_are_reported_not_repaired(org):
    """Which link to break is the owner's decision; guessing would silently
    restructure their organisation."""
    org["add_agent"]("a", reports_to="b")
    org["add_agent"]("b", reports_to="a")

    cycles = await crews._find_cycles()

    assert len(cycles) == 1
    assert set(cycles[0]) == {"a", "b"}
    # and nothing was changed to "fix" it
    assert org["agents"]["a"]["reports_to"] == "b"


@pytest.mark.asyncio
async def test_a_healthy_organisation_reports_no_cycles(org):
    org["add_agent"]("boss")
    org["add_agent"]("dev", reports_to="boss")

    assert await crews._find_cycles() == []


@pytest.mark.asyncio
async def test_membership_needs_agents_and_crews_that_exist(org):
    org["add_agent"]("dev")

    with pytest.raises(crews.CrewError):
        await crews.set_membership("nobody")
    with pytest.raises(crews.CrewError):
        await crews.set_membership("dev", "crew-nope")
    with pytest.raises(crews.CrewError):
        await crews.set_membership("dev", reports_to="nobody")


# -- what the hierarchy is for ------------------------------------------------

@pytest.mark.asyncio
async def test_assigning_writes_the_column_the_executors_read(org):
    """UI-051: the crew fields round-tripped to the UI and changed a chip on a
    card. This is the consumer they never had."""
    team = await crews.create("Repair")
    org["add_agent"]("dev", "Dev", "Developer", crew_id=team["id"])
    org["add_task"]("t1")

    result = await crews.assign("t1", team["id"], role="Developer")

    assert result["assigned_agent_id"] == "dev"
    assert org["tasks"]["t1"]["assigned_agent_id"] == "dev"


@pytest.mark.asyncio
async def test_the_matching_role_is_preferred(org):
    team = await crews.create("Repair")
    org["add_agent"]("dev", "Dev", "Developer", crew_id=team["id"])
    org["add_agent"]("qa", "QA", "Verifier", crew_id=team["id"])
    org["add_task"]("t1")

    result = await crews.assign("t1", team["id"], role="Verifier")

    assert result["assigned_agent_id"] == "qa"
    assert result["matched_role"]


@pytest.mark.asyncio
async def test_selection_is_deterministic(org):
    """A crew runs a process defined upfront. Agents negotiating who takes the
    work would make it a swarm."""
    team = await crews.create("Repair")
    org["add_agent"]("bravo", "Bravo", "Developer", crew_id=team["id"])
    org["add_agent"]("alpha", "Alpha", "Developer", crew_id=team["id"])
    org["add_task"]("t1")
    org["add_task"]("t2")

    first = await crews.assign("t1", team["id"])
    second = await crews.assign("t2", team["id"])

    assert first["assigned_agent_id"] == second["assigned_agent_id"] == "alpha"


@pytest.mark.asyncio
async def test_an_unmatched_role_falls_back_to_an_active_member(org):
    team = await crews.create("Repair")
    org["add_agent"]("dev", "Dev", "Developer", crew_id=team["id"])
    org["add_task"]("t1")

    result = await crews.assign("t1", team["id"], role="Nonexistent")

    assert result["assigned_agent_id"] == "dev"
    assert not result["matched_role"]


@pytest.mark.asyncio
async def test_an_all_inactive_crew_is_assigned_and_flagged(org):
    """Refusing would lose the work; assigning silently would leave nobody
    wondering why nothing moves."""
    team = await crews.create("Repair")
    org["add_agent"]("dev", "Dev", "Developer", crew_id=team["id"], is_active=False)
    org["add_task"]("t1")

    result = await crews.assign("t1", team["id"])

    assert result["assigned_agent_id"] == "dev"
    assert result["active"] is False


@pytest.mark.asyncio
async def test_an_empty_crew_cannot_take_work(org):
    team = await crews.create("Repair")
    org["add_task"]("t1")

    with pytest.raises(crews.CrewError) as raised:
        await crews.assign("t1", team["id"])
    assert "no members" in str(raised.value)


@pytest.mark.asyncio
async def test_a_task_that_does_not_exist_is_refused(org):
    team = await crews.create("Repair")
    org["add_agent"]("dev", crew_id=team["id"])

    with pytest.raises(crews.CrewError):
        await crews.assign("nope", team["id"])


# -- escalation ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_escalation_goes_up_one_step(org):
    """Straight to the top would skip whoever is most likely to be able to
    help; looping to the top inside one call would hide where it stuck."""
    org["add_agent"]("boss")
    org["add_agent"]("arch", reports_to="boss")
    org["add_agent"]("dev", reports_to="arch")
    org["add_task"]("t1", assigned="dev")

    first = await crews.escalate("t1", "cannot reproduce")
    assert first["to"] == "arch"

    second = await crews.escalate("t1")
    assert second["to"] == "boss"


@pytest.mark.asyncio
async def test_escalation_stops_at_the_top(org):
    org["add_agent"]("boss")
    org["add_task"]("t1", assigned="boss")

    result = await crews.escalate("t1")

    assert not result["escalated"]
    assert "top of its reporting line" in result["reason"]
    assert org["tasks"]["t1"]["assigned_agent_id"] == "boss"


@pytest.mark.asyncio
async def test_escalation_is_recorded(org):
    """Work moving up the organisation is exactly what somebody will later
    need explained."""
    org["add_agent"]("boss")
    org["add_agent"]("dev", reports_to="boss")
    org["add_task"]("t1", assigned="dev")

    await crews.escalate("t1", "needs a decision")

    assert org["logged"]
    assert org["logged"][0][0] == "WARNING"
    assert "needs a decision" in org["logged"][0][1]


@pytest.mark.asyncio
async def test_an_unassigned_task_has_nobody_to_escalate_from(org):
    org["add_task"]("t1")

    with pytest.raises(crews.CrewError):
        await crews.escalate("t1")


# -- disbanding ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_disbanding_releases_members_rather_than_deleting_them(org):
    team = await crews.create("Repair")
    org["add_agent"]("dev", crew_id=team["id"])

    result = await crews.delete(team["id"])

    assert result["released"] == 1
    assert "dev" in org["agents"], "disbanding a crew deleted a person"


@pytest.mark.asyncio
async def test_sub_crews_are_lifted_not_orphaned(org):
    top = await crews.create("Company", crews.DEPARTMENT)
    dept = await crews.create("Engineering", crews.DEPARTMENT, top["id"])
    team = await crews.create("Repair", crews.TEAM, dept["id"])

    await crews.delete(dept["id"])

    assert org["crews"][team["id"]]["parent_crew_id"] == top["id"]


# -- the wiring ---------------------------------------------------------------

def test_the_rpcs_exist():
    from python.harness.server import _HANDLERS

    for method in ("crew.list", "crew.create", "crew.update", "crew.delete",
                   "crew.set_membership", "crew.chart", "crew.assign",
                   "crew.escalate"):
        assert method in _HANDLERS


def test_saving_an_agent_goes_through_the_cycle_check():
    """Writing reports_to inline would skip the only place that refuses a
    loop."""
    import ast
    import inspect

    from python.harness import server

    tree = ast.parse(inspect.getsource(server.handle_agent_save_agent))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "set_membership" in called


def test_agents_report_their_place_in_the_organisation():
    import ast
    import inspect

    from python.harness import server

    tree = ast.parse(inspect.getsource(server.handle_agent_get_agents))
    keys = {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert {"crew_id", "reports_to"} <= keys


def test_nothing_here_routes_emergently():
    """A crew is deterministic by definition. An agent picking its own
    successor at runtime would make this a swarm."""
    import inspect

    source = inspect.getsource(crews)
    for word in ("random", "choice", "shuffle"):
        assert word not in source
