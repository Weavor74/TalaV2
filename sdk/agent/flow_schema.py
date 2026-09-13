"""
flow_schema.py — the one workflow representation
================================================

Tala had two workflow documents that could not read each other:

* ``tala-flow/1.0`` — ``nodes[]`` joined by ``connections``, authored in the
  visual builder and run by
  :class:`~python.harness.workflow_executor.WorkflowExecutor`.
* the self-extension spec — ``workflow.steps[]`` joined by ``depends_on``,
  authored inside ``nodes/workflow-self-extension.html`` and run by
  :class:`~sdk.agent.graph_engine.DAGExecutionEngine`.

Same idea, two vocabularies, so a workflow Tala wrote autonomously could not be
opened in the editor and a workflow the user drew could not drive the goal
engine. This module makes them one document: ``tala-flow/1.1``. Both shapes
normalise into it, it converts back to the DAG spec, and it is the single place
that says which node types exist.

Three things live here on purpose:

**The node-type registry.** The palette used to be five hand-written entries in
``renderer/index.html`` while the executor implemented nine, and the two lists
drifted in both directions — ``interactive_prompt`` had a properties panel and
no executor branch, ``node``/``bash``/``mcp_tool`` had executor branches and no
palette. A registry both sides read cannot drift.

**Normalisation.** One canonical shape means the validator, the compiler, the
editor and both engines are looking at the same fields.

**Validation.** Nothing validated a workflow anywhere — not the editor, not
``save_workflow_data``, not the compiler — so a flow with a dangling target or a
tool step missing its required argument saved successfully and then failed at
run time, or worse, ran past the broken step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

SCHEMA = "tala-flow/1.1"

#: Schemas this module reads. 1.0 documents are upgraded in place.
KNOWN_SCHEMAS = ("tala-flow/1.0", "tala-flow/1.1")

#: The five families the editor palette groups by. They are presentation
#: groupings over the type registry, not a second taxonomy: every family is a
#: view onto types the executors already run.
FAMILIES = ("start", "action", "decision", "state", "end")

#: Which engine runs a node type.
#:
#: ``flow`` — WorkflowExecutor, deterministic step-by-step with explicit
#:            connections. What the visual builder produces.
#: ``dag``  — DAGExecutionEngine, LLM phases over a worktree with healing and
#:            retries. What autonomous goals run.
#: ``both`` — runs under either. ``tool`` is the bridge: the same call through
#:            the same registry in both engines.
ENGINES = ("flow", "dag", "both")


@dataclass(frozen=True)
class FieldSpec:
    """
    One configuration field of a node type.

    The properties panel is generated from these, so a config key the executor
    reads is a key the user can set — the two cannot drift apart the way a
    hand-written form does.
    """

    name: str
    label: str
    kind: str = "text"  # text|textarea|code|number|json|select|node_ref|tool_ref
    required: bool = False
    default: Any = None
    options: tuple[str, ...] = ()
    help: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "default": self.default,
            "options": list(self.options),
            "help": self.help,
        }


@dataclass(frozen=True)
class NodeTypeSpec:
    """A node type, as both engines and the editor understand it."""

    type: str
    family: str
    label: str
    description: str
    engine: str = "flow"
    fields: tuple[FieldSpec, ...] = ()
    #: Connection keys this type always has.
    ports: tuple[str, ...] = ("onSuccess", "onFailure")
    #: True when the type emits branch names decided at run time, so the editor
    #: must let the user name connections rather than offering a fixed pair.
    dynamic_branches: bool = False
    #: A terminal type has no outgoing connections.
    terminal: bool = False

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "family": self.family,
            "label": self.label,
            "description": self.description,
            "engine": self.engine,
            "fields": [f.as_dict() for f in self.fields],
            "ports": list(self.ports),
            "dynamic_branches": self.dynamic_branches,
            "terminal": self.terminal,
            "defaults": {
                f.name: f.default for f in self.fields if f.default is not None
            },
        }


_BACKENDS = ("local", "docker", "ssh")

NODE_TYPES: dict[str, NodeTypeSpec] = {
    # ── start ────────────────────────────────────────────────────────────
    "trigger": NodeTypeSpec(
        type="trigger",
        family="start",
        label="Start / Trigger",
        description="Where the workflow begins and what it is given to start with.",
        fields=(
            FieldSpec(
                "source", "Starts when", "select", default="manual",
                options=("manual", "schedule", "channel", "startup"),
                help=(
                    "manual: only the Run button or an explicit call. schedule: "
                    "the cron expression below. channel: an inbound Telegram or "
                    "Discord message. startup: once, when the harness starts. "
                    "Watched by the workflow trigger service."
                ),
            ),
            FieldSpec(
                "cron", "Cron (schedule)", "text",
                help="Five-field cron, e.g. '0 9 * * 1'. Used when Starts when = schedule.",
            ),
            FieldSpec(
                "channel", "Channel filter", "text",
                help=(
                    "Platform (telegram/discord), channel name or id that may fire "
                    "this flow. Empty matches any channel. Used when Starts when = channel."
                ),
            ),
            FieldSpec(
                "pattern", "Message pattern", "text",
                help=(
                    "Regex (or plain substring) the message text must match; empty "
                    "matches every message. The message arrives as {{text}}, with "
                    "{{sender}}, {{chat_id}} and {{platform}} alongside it."
                ),
            ),
            FieldSpec(
                "source_node_id", "Source node", "node_ref", default="default",
                help="The HTML-NN this flow was started from, when it was started by one.",
            ),
            FieldSpec(
                "inputs", "Declared inputs", "json", default={},
                help="Named inputs the flow expects. Steps reference them as {{name}}.",
            ),
            FieldSpec("description", "Description", "textarea"),
        ),
    ),
    # ── action ───────────────────────────────────────────────────────────
    "tool": NodeTypeSpec(
        type="tool",
        family="action",
        label="Tool call",
        description=(
            "Run a registered tool. Passes the same governance gates as any "
            "other invocation."
        ),
        engine="both",
        fields=(
            FieldSpec("tool", "Tool", "tool_ref", required=True),
            FieldSpec(
                "args", "Arguments", "json", default={},
                help="Values may reference earlier steps as {{step_<id>.result.<key>}}.",
            ),
            FieldSpec("description", "Description", "textarea"),
        ),
    ),
    "node": NodeTypeSpec(
        type="node",
        family="action",
        label="Run HTML-NN",
        description=(
            "Run another neural node. The target's own type, declared authority "
            "and admission apply, so coordinating a node cannot grant it "
            "permissions it does not have alone."
        ),
        fields=(
            FieldSpec("node_id", "Node", "node_ref", required=True),
            FieldSpec("inputs", "Inputs", "json", default={}),
            FieldSpec("description", "Description", "textarea"),
        ),
        dynamic_branches=True,
    ),
    "bash": NodeTypeSpec(
        type="bash",
        family="action",
        label="Shell command",
        description="Run a command through the local, Docker or SSH terminal backend.",
        fields=(
            FieldSpec("command", "Command", "code", required=True),
            FieldSpec("backend", "Backend", "select", default="local", options=_BACKENDS),
            FieldSpec("cwd", "Working directory", "text"),
            FieldSpec("timeout", "Timeout (s)", "number", default=120),
            FieldSpec("host", "SSH host", "text", help="Required when backend is ssh."),
            FieldSpec("user", "SSH user", "text", help="Required when backend is ssh."),
            FieldSpec("image", "Docker image", "text", default="ubuntu:latest"),
        ),
    ),
    "custom_script": NodeTypeSpec(
        type="custom_script",
        family="action",
        label="Python script",
        description=(
            "Run Python in a subprocess. The script is given the accumulated "
            "workflow variables as `variables` and returns by assigning `result`."
        ),
        fields=(
            FieldSpec("code", "Script", "code", required=True, default="result = None"),
            FieldSpec("timeout", "Timeout (s)", "number", default=60),
            FieldSpec("description", "Description", "textarea"),
        ),
    ),
    "mcp_tool": NodeTypeSpec(
        type="mcp_tool",
        family="action",
        label="MCP tool",
        description="Call a tool on a connected MCP server.",
        fields=(
            FieldSpec("server_name", "Server", "text", required=True, default="default"),
            FieldSpec("tool_name", "Tool", "text", required=True),
            FieldSpec("args", "Arguments", "json", default={}),
            FieldSpec(
                "server", "Server config", "json",
                help="Optional inline connection config, if the server is not already connected.",
            ),
        ),
    ),
    "interactive_prompt": NodeTypeSpec(
        type="interactive_prompt",
        family="action",
        label="Ask a question",
        description=(
            "Suspend and ask for an answer. In a chat-driven run the answer comes "
            "from the conversation; in an unattended run it comes from a trigger "
            "input, or the step fails rather than inventing one."
        ),
        fields=(
            FieldSpec(
                "message", "Question", "textarea", required=True,
                default="What is your input?",
            ),
            FieldSpec(
                "input_key", "Answer from input", "text",
                help="Trigger input to use when nobody is there to answer.",
            ),
        ),
    ),
    "email": NodeTypeSpec(
        type="email",
        family="action",
        label="Send email",
        description="Send a message through the credential configured in Settings → Auth.",
        fields=(
            FieldSpec("to", "To", "text", required=True),
            FieldSpec("subject", "Subject", "text", default="Tala Flow Alert"),
            FieldSpec("body", "Body", "textarea"),
        ),
    ),
    "auth": NodeTypeSpec(
        type="auth",
        family="action",
        label="Authenticate",
        description="Resolve a stored credential before the steps that need it.",
        fields=(FieldSpec("description", "Description", "textarea"),),
    ),
    # ── decision ─────────────────────────────────────────────────────────
    "decision": NodeTypeSpec(
        type="decision",
        family="decision",
        label="Decision",
        description=(
            "Route by asking a decision HTML-NN which way to go. The choice lives "
            "in the node; the shape of the fork lives in the workflow."
        ),
        fields=(
            FieldSpec("node_id", "Decision node", "node_ref", required=True),
            FieldSpec("inputs", "Inputs", "json", default={}),
            FieldSpec(
                "branches", "Branches", "json", default=[],
                help="Names the decision may answer with. Each needs a connection.",
            ),
        ),
        ports=("onFailure",),
        dynamic_branches=True,
    ),
    # ── state ────────────────────────────────────────────────────────────
    "state": NodeTypeSpec(
        type="state",
        family="state",
        label="State",
        description=(
            "Read or write a state HTML-NN — what is currently true, shared "
            "between runs."
        ),
        fields=(
            FieldSpec("node_id", "State node", "node_ref", required=True),
            FieldSpec(
                "mode", "Mode", "select", required=True, default="read",
                options=("read", "write"),
            ),
            FieldSpec(
                "values", "Values to write", "json", default={},
                help="Write mode only. Merged into what the node already holds.",
            ),
            FieldSpec("title", "Title", "text", help="Used when creating the node."),
        ),
    ),
    # ── end ──────────────────────────────────────────────────────────────
    "output": NodeTypeSpec(
        type="output",
        family="end",
        label="Result",
        description="Collect the workflow's result and state how completion is verified.",
        fields=(
            FieldSpec("format", "Format", "select", default="json", options=("json", "text")),
            FieldSpec(
                "value", "Result", "textarea",
                help="What to return. References earlier steps as {{step_<id>.result}}.",
            ),
            FieldSpec(
                "verify", "Verification", "textarea",
                help=(
                    "A {{reference}} that must be present and truthy for the run to "
                    "count as complete."
                ),
            ),
        ),
        ports=(),
        terminal=True,
    ),
    # ── goal-engine phases ───────────────────────────────────────────────
    # Authored in workflow-self-extension.html rather than on the canvas, but
    # they are the same document now, so the editor can show and edit them.
    "research": NodeTypeSpec(
        type="research", family="action", label="Research (goal engine)",
        description="Gather codebase references for the goal.", engine="dag",
        fields=(FieldSpec("description", "Description", "textarea"),),
    ),
    "synthesis": NodeTypeSpec(
        type="synthesis", family="action", label="Synthesis (goal engine)",
        description="Design and synthesise the change.", engine="dag",
        fields=(FieldSpec("description", "Description", "textarea"),),
    ),
    "writing": NodeTypeSpec(
        type="writing", family="action", label="Writing (goal engine)",
        description="Write the synthesised change to the target file.", engine="dag",
        fields=(FieldSpec("description", "Description", "textarea"),),
    ),
    "validation": NodeTypeSpec(
        type="validation", family="end", label="Validation (goal engine)",
        description="Check the written change compiles, imports and passes tests.",
        engine="dag",
        fields=(
            FieldSpec("description", "Description", "textarea"),
            FieldSpec(
                "checks", "Checks", "json",
                default=["py_compile", "import_probe", "register_assert", "pytest"],
            ),
        ),
    ),
}

#: Types the flow engine runs. Derived, so WorkflowExecutor cannot claim a
#: different set than the palette offers.
FLOW_TYPES: frozenset[str] = frozenset(
    t for t, s in NODE_TYPES.items() if s.engine in ("flow", "both")
)
#: Types the DAG engine runs.
DAG_TYPES: frozenset[str] = frozenset(
    t for t, s in NODE_TYPES.items() if s.engine in ("dag", "both")
)

_STEP_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_REF = re.compile(r"\{\{\s*([A-Za-z0-9_.\[\]-]+)\s*\}\}")


def describe_node_types() -> dict[str, Any]:
    """
    The palette, as data. Served to the renderer so the UI is generated from
    the same registry the executors dispatch on.
    """
    return {
        "schema": SCHEMA,
        "families": list(FAMILIES),
        "types": [NODE_TYPES[t].as_dict() for t in NODE_TYPES],
    }


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _engine_of(flow: dict[str, Any]) -> str:
    """
    Which engine runs this flow.

    Declared if the document says so, otherwise inferred from its node types.
    A flow of nothing but bridge types (``tool``) is a flow-engine flow, since
    that is the engine with explicit connections to follow.
    """
    declared = str(flow.get("engine") or "").strip().lower()
    if declared in ("flow", "dag"):
        return declared
    for node in flow.get("nodes") or []:
        spec = NODE_TYPES.get(str(node.get("type") or node.get("node_type") or ""))
        if spec is not None and spec.engine == "dag":
            return "dag"
    return "flow"


def _dag_steps_to_nodes(steps: list[Any]) -> list[dict[str, Any]]:
    """
    Convert ``depends_on`` steps into connected nodes.

    ``depends_on`` stays authoritative for the DAG engine — it is what lets two
    steps run in parallel, which a single ``onSuccess`` cannot express — but the
    editor needs cables to draw, so an ``onSuccess`` is derived from each
    dependency. The round trip is lossless: :func:`to_dag_spec` reads
    ``depends_on`` back out.
    """
    nodes: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step") or f"step-{index}")
        nodes.append({
            "id": step_id,
            "type": str(step.get("node_type") or "unknown"),
            "label": step_id.replace("_", " ").replace("-", " ").title(),
            "description": str(step.get("description") or ""),
            "config": dict(step.get("config") or {}),
            "connections": {},
            "depends_on": [str(d) for d in (step.get("depends_on") or [])],
            "success_criteria": dict(step.get("success_criteria") or {}),
            "max_retries": int(step.get("max_retries", 3) or 3),
            "position": {"x": 80 + 220 * index, "y": 140},
        })

    by_id = {n["id"]: n for n in nodes}
    order = [n["id"] for n in nodes]
    for node in nodes:
        for dep in node["depends_on"]:
            parent = by_id.get(dep)
            if parent is not None and not parent["connections"].get("onSuccess"):
                parent["connections"]["onSuccess"] = node["id"]
    # A spec authored as a plain list with no dependencies still reads as a
    # chain on the canvas rather than a row of disconnected cards.
    for previous, current in zip(order, order[1:]):
        parent, child = by_id[previous], by_id[current]
        if not parent["connections"].get("onSuccess") and not child["depends_on"]:
            parent["connections"]["onSuccess"] = child["id"]
    return nodes


def _empty_flow() -> dict[str, Any]:
    return {
        "schema": SCHEMA, "flow_id": "", "name": "Unnamed Workflow",
        "description": "", "tags": [], "trigger_keywords": [], "nodes": [],
        "engine": "flow", "policy": {"on_failure": {}, "on_success": {}},
        "metadata": {},
    }


def normalize(document: dict[str, Any]) -> dict[str, Any]:
    """
    Return a canonical ``tala-flow/1.1`` document.

    Accepts a 1.0 flow, a 1.1 flow, a raw DAG spec (``{"steps": [...]}``) or a
    node's whole ``#node-state`` object containing one (``{"workflow": {...}}``).

    Never raises on odd input: an unrecognised document normalises to an empty
    flow, which :func:`validate` then reports on. Raising here would hand the
    caller an exception where what it needs is a list of problems to show.
    """
    if not isinstance(document, dict):
        return _empty_flow()

    source = document
    # A node's state object wraps the flow.
    if "nodes" not in source and isinstance(source.get("workflow"), dict):
        wrapper = source
        source = dict(source["workflow"])
        for key in ("title", "description", "tags"):
            if source.get(key) is None and wrapper.get(key) is not None:
                source[key] = wrapper[key]
        if not source.get("metadata"):
            source["metadata"] = wrapper.get("metadata") or {}

    raw_nodes = source.get("nodes")
    if not isinstance(raw_nodes, list):
        steps = source.get("steps")
        raw_nodes = _dag_steps_to_nodes(steps) if isinstance(steps, list) else []

    nodes: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            continue
        node_type = str(raw.get("type") or raw.get("node_type") or "unknown").strip()
        config = raw.get("config")
        config = dict(config) if isinstance(config, dict) else {}
        # 1.0 stored a step's prose inside config; 1.1 gives it a field of its
        # own so both shapes describe a step the same way.
        description = str(raw.get("description") or config.pop("description", "") or "")
        raw_connections = raw.get("connections")
        connections: dict[str, str | None] = {}
        if isinstance(raw_connections, dict):
            for key, value in raw_connections.items():
                connections[str(key)] = str(value) if value else None
        position = raw.get("position")
        if not isinstance(position, dict) or "x" not in position or "y" not in position:
            position = {"x": 80 + 220 * (index % 5), "y": 120 + 150 * (index // 5)}

        nodes.append({
            "id": str(raw.get("id") or raw.get("step") or f"step-{index}"),
            "type": node_type,
            "label": str(raw.get("label") or node_type.replace("_", " ").title()),
            "description": description,
            "config": config,
            "connections": connections,
            "depends_on": [str(d) for d in (raw.get("depends_on") or [])],
            "success_criteria": dict(raw.get("success_criteria") or {}),
            "max_retries": int(raw.get("max_retries", 3) or 3),
            "position": {"x": float(position["x"]), "y": float(position["y"])},
        })

    flow = {
        "schema": SCHEMA,
        "flow_id": str(source.get("flow_id") or document.get("flow_id") or ""),
        "name": str(source.get("name") or source.get("title") or "Unnamed Workflow"),
        "description": str(source.get("description") or ""),
        "tags": [str(t) for t in (source.get("tags") or [])],
        "trigger_keywords": [str(k) for k in (source.get("trigger_keywords") or [])],
        "nodes": nodes,
        # Accepted both as the DAG spec spells it (top level) and as this
        # schema stores it (under `policy`), so normalising an already
        # normalised flow is a no-op rather than a quiet loss of its policy.
        "policy": {
            "on_failure": dict(
                source.get("on_failure") or (source.get("policy") or {}).get("on_failure") or {}
            ),
            "on_success": dict(
                source.get("on_success") or (source.get("policy") or {}).get("on_success") or {}
            ),
        },
        "metadata": dict(source.get("metadata") or {}),
    }
    if source.get("consumer") and "consumer" not in flow["metadata"]:
        flow["metadata"]["consumer"] = source["consumer"]
    flow["engine"] = _engine_of({**flow, "engine": source.get("engine")})
    return flow


def to_dag_spec(flow: dict[str, Any]) -> dict[str, Any]:
    """
    Render a normalised flow back into the ``workflow.steps[]`` spec the DAG
    engine builds its chain from.

    ``depends_on`` is rebuilt from connections when a node does not carry one,
    so a goal-engine chain rearranged on the canvas executes in the order the
    user drew rather than the order it was authored in.
    """
    flow = normalize(flow)
    nodes = flow["nodes"]
    incoming: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for node in nodes:
        for key, target in node["connections"].items():
            if target and target in incoming and key != "onFailure":
                incoming[target].append(node["id"])

    steps = []
    for node in nodes:
        depends = node["depends_on"] or incoming.get(node["id"], [])
        step: dict[str, Any] = {
            "step": node["id"],
            "node_type": node["type"],
            "depends_on": list(dict.fromkeys(depends)),
            "description": node["description"] or node["label"],
            "success_criteria": node["success_criteria"],
            "max_retries": node["max_retries"],
        }
        if node["config"]:
            step["config"] = node["config"]
        steps.append(step)

    spec: dict[str, Any] = {"flow_id": flow["flow_id"], "steps": steps}
    consumer = flow.get("metadata", {}).get("consumer")
    if consumer:
        spec["consumer"] = consumer
    for key in ("on_failure", "on_success"):
        if flow["policy"].get(key):
            spec[key] = flow["policy"][key]
    return spec


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One problem with a flow. ``error`` blocks saving; ``warning`` does not."""

    severity: str
    code: str
    message: str
    node_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity, "code": self.code,
            "message": self.message, "node_id": self.node_id,
        }


_UNKNOWN = NodeTypeSpec(type="unknown", family="action", label="Unknown", description="")


def _collect_refs(value: Any) -> list[str]:
    """Every ``{{reference}}`` anywhere inside a config value."""
    found: list[str] = []
    if isinstance(value, str):
        found.extend(_REF.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_collect_refs(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_collect_refs(item))
    return found


def _reachable(nodes: list[dict[str, Any]], starts: Iterable[str]) -> set[str]:
    by_id = {n["id"]: n for n in nodes}
    seen: set[str] = set()
    queue = [s for s in starts if s in by_id]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for target in by_id[current]["connections"].values():
            if target and target in by_id and target not in seen:
                queue.append(target)
        # A dependency is an edge too, in the direction the DAG runs it.
        for other in nodes:
            if current in other["depends_on"] and other["id"] not in seen:
                queue.append(other["id"])
    return seen


def validate(
    flow: dict[str, Any],
    known_tools: Iterable[str] | None = None,
    known_nodes: Iterable[str] | None = None,
) -> list[Finding]:
    """
    Everything wrong with a flow, worst first.

    ``known_tools`` and ``known_nodes`` are optional: when the caller can supply
    the live registry and nodes directory, a step naming something that does not
    exist is caught here instead of at run time. When it cannot, those checks
    are skipped rather than guessed at — a validator that invents failures is a
    validator people switch off.
    """
    flow = normalize(flow)
    findings: list[Finding] = []
    nodes = flow["nodes"]
    tools = {str(t) for t in known_tools} if known_tools is not None else None
    available = {str(n) for n in known_nodes} if known_nodes is not None else None

    if not nodes:
        return [Finding("error", "empty", "The workflow has no steps.")]

    ids = [n["id"] for n in nodes]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        findings.append(Finding(
            "error", "duplicate_id", f"More than one step is called {dup!r}.", dup))

    by_id = {n["id"]: n for n in nodes}
    engine = flow["engine"]

    # Engines cannot be mixed: they are different machinery, so a flow holding
    # both would execute half of itself and report on the half it ran.
    flow_only = sorted(
        n["id"] for n in nodes
        if NODE_TYPES.get(n["type"]) is not None and NODE_TYPES[n["type"]].engine == "flow"
    )
    dag_only = sorted(
        n["id"] for n in nodes
        if NODE_TYPES.get(n["type"]) is not None and NODE_TYPES[n["type"]].engine == "dag"
    )
    if flow_only and dag_only:
        findings.append(Finding(
            "error", "mixed_engines",
            f"This flow mixes goal-engine steps ({', '.join(dag_only[:3])}) with "
            f"builder steps ({', '.join(flow_only[:3])}). One workflow runs under "
            f"one engine.",
        ))

    for node in nodes:
        nid, ntype = node["id"], node["type"]
        spec = NODE_TYPES.get(ntype)
        if spec is None:
            findings.append(Finding(
                "error", "unknown_type",
                f"{nid} has type {ntype!r}, which nothing runs. Known types: "
                f"{', '.join(sorted(NODE_TYPES))}.", nid,
            ))
            continue
        if not _STEP_ID.fullmatch(nid):
            findings.append(Finding(
                "warning", "odd_id",
                f"{nid!r} is an unusual step id; letters, digits, - and _ travel best.",
                nid,
            ))

        config = node["config"]
        for required in spec.required_fields:
            if config.get(required) in (None, "", [], {}):
                findings.append(Finding(
                    "error", "missing_config",
                    f"{nid} ({spec.label}) needs {required!r} and has none.", nid))

        if ntype == "tool" and tools is not None:
            name = str(config.get("tool") or "")
            if name and name not in tools:
                findings.append(Finding(
                    "error", "unknown_tool",
                    f"{nid} calls tool {name!r}, which is not registered.", nid))
        if ntype == "bash" and str(config.get("backend") or "local") == "ssh":
            if not config.get("host") or not config.get("user"):
                findings.append(Finding(
                    "error", "missing_config",
                    f"{nid} uses the ssh backend, which needs both 'host' and 'user'.",
                    nid))
        if ntype in ("node", "decision", "state"):
            target = str(config.get("node_id") or "")
            if target and available is not None and target not in available:
                findings.append(Finding(
                    "error", "unknown_node",
                    f"{nid} points at node {target!r}, which does not exist.", nid))

        for key, target in node["connections"].items():
            if not target:
                continue
            if target == nid:
                findings.append(Finding(
                    "error", "self_link", f"{nid} connects to itself.", nid))
            elif target not in by_id:
                findings.append(Finding(
                    "error", "dangling_link",
                    f"{nid}'s {key!r} connection points at {target!r}, which is not "
                    f"in this flow.", nid,
                ))
        if spec.terminal and any(node["connections"].values()):
            findings.append(Finding(
                "warning", "terminal_link",
                f"{nid} is a {spec.label} step; connections leaving it are never "
                f"followed.", nid,
            ))

        # A decision that can answer with a branch nobody connected stops the
        # run. The executor refuses at that point on purpose — this catches it
        # while the flow is still being drawn.
        if spec.dynamic_branches:
            declared = config.get("branches")
            declared = declared if isinstance(declared, list) else []
            if spec.family == "decision" and not declared:
                findings.append(Finding(
                    "error", "no_branches",
                    f"{nid} is a decision and declares no branches, so nothing "
                    f"routes it.", nid,
                ))
            for branch in declared:
                if not node["connections"].get(str(branch)):
                    findings.append(Finding(
                        "error", "unconnected_branch",
                        f"{nid} can answer {str(branch)!r}, which is connected to "
                        f"nothing.", nid,
                    ))

        for ref in _collect_refs(config):
            head = ref.split(".")[0]
            if head.startswith("step_") and head[len("step_"):] not in by_id:
                findings.append(Finding(
                    "error", "unknown_reference",
                    f"{nid} references {{{{{ref}}}}}, but there is no step "
                    f"{head[len('step_'):]!r}.", nid,
                ))

    starts = [n["id"] for n in nodes if NODE_TYPES.get(n["type"], _UNKNOWN).family == "start"]
    if engine == "flow":
        if not starts:
            findings.append(Finding(
                "error", "no_start",
                "No Start step, so nothing says where the workflow begins."))
        elif len(starts) > 1:
            findings.append(Finding(
                "warning", "many_starts",
                f"{len(starts)} Start steps; execution begins at {starts[0]!r} and "
                f"ignores the rest.",
            ))
    if not starts:
        # A chain with no Start step begins wherever nothing points: no
        # incoming connection and nothing of its own to wait for. Being named
        # in another step's depends_on makes a node a *predecessor*, so it must
        # not disqualify it — reading that edge backwards left a valid
        # goal-engine chain with no entry point and every step "unreachable".
        targeted = {t for n in nodes for t in n["connections"].values() if t}
        starts = [n["id"] for n in nodes if n["id"] not in targeted and not n["depends_on"]]

    reached = _reachable(nodes, starts[:1] if engine == "flow" else starts)
    for node in nodes:
        if node["id"] not in reached:
            findings.append(Finding(
                "warning", "unreachable",
                f"{node['id']} is never reached from the start of the flow.",
                node["id"],
            ))

    if engine == "flow" and not any(
        NODE_TYPES.get(by_id[n]["type"], _UNKNOWN).family == "end" for n in reached
    ):
        findings.append(Finding(
            "warning", "no_end",
            "No Result step is reachable, so the workflow has no defined output."))

    order = {"error": 0, "warning": 1}
    findings.sort(key=lambda f: (order.get(f.severity, 2), f.node_id or ""))
    return findings


def errors(findings: Iterable[Finding]) -> list[Finding]:
    """Just the blocking ones."""
    return [f for f in findings if f.severity == "error"]
