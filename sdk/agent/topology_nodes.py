"""
sdk/agent/topology_nodes.py
===========================
HTML-NN persistence for swarm topologies.

A swarm is not defined in Python and then documented somewhere else -- the
HTML-NN file *is* the swarm definition. Creating a swarm writes one of these
nodes; running a swarm reads one back. The file stays in the active nodes
directory afterwards, so a topology that worked once can be listed, inspected,
edited by hand, and run again without being re-derived.

Node shape (mirrors ``nodes/workflow-self-extension.html``, the other
executable HTML-NN in this codebase):

* ``data-node-type="swarm-topology"`` -- how the loader finds them.
* ``#node-state`` carries a ``topology`` object: ``topology_id``, ``name``,
  ``nodes[]``, ``edges[]``, ``failure_policy{}``. That object is the spec;
  everything else in the file is prose for humans and retrieval.
* ``#semantic-memory`` describes what the swarm is *for*, so the node is a
  real memory rather than a config file wearing an HTML costume.

Validation lives in ``sdk/agent/topology.topology_from_spec`` and is applied on
both write and read: a node that cannot produce a valid topology is never
written, and one that has been hand-edited into an invalid state is skipped on
load with the reason logged rather than silently half-registered.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterator

from sdk.agent.documentation_nodes import (
    _render_paragraphs,
    _render_relationships,
    _safe_node_id,
    resolve_active_nodes_dir,
)
from sdk.agent.topology import SwarmTopology, TopologySpecError, topology_from_spec

log = logging.getLogger(__name__)

NODE_TYPE = "swarm-topology"
NODE_ID_PREFIX = "swarm-topology-"

#: Cheap pre-filter so loading does not run the full HTML parser over every
#: node in the corpus (there are ~100) just to discover the handful that are
#: topologies.
_TYPE_MARKER = f'data-node-type="{NODE_TYPE}"'


def topology_node_id(topology_id: str) -> str:
    """Node id for a topology. ``swarm-code-repair`` -> ``swarm-topology-code-repair``."""
    slug = re.sub(r"^swarm[-_]?", "", str(topology_id or "").strip().lower())
    return _safe_node_id(f"{NODE_ID_PREFIX}{slug or topology_id}")


def _default_semantic_memory(topology: SwarmTopology) -> str:
    """
    Describe the swarm in prose derived from its own shape.

    Written from the spec rather than left blank so an auto-created topology
    still carries real content: the doc-comparison heuristics in wiki.py treat
    an empty or placeholder #semantic-memory as a defect, and a node nobody can
    read is not a memory.
    """
    starts = [n.id for n in topology.start_nodes()]
    joins = [
        (n.id, [e.from_node for e in topology.incoming_edges(n.id)])
        for n in topology.nodes
        if len(topology.incoming_edges(n.id)) > 1
    ]
    fans = [
        (n.id, [e.to_node for e in topology.outgoing_edges(n.id)])
        for n in topology.nodes
        if len(topology.outgoing_edges(n.id)) > 1
    ]

    paragraphs = [
        f"This node is the executable definition of the {topology.name!r} swarm. It is not "
        f"documentation about the swarm; it is the swarm. When a run is started with "
        f"topology_id {topology.topology_id!r}, SwarmEngine reads the 'topology' object in this "
        f"node's #node-state block and builds the run from the nodes, edges and failure policy "
        f"defined there. Editing this node changes how that swarm behaves on its next run.",
        "Roles in this swarm: "
        + "; ".join(f"{n.id} ({n.role}) — {n.description or 'no description'}" for n in topology.nodes)
        + ".",
        "Flow: "
        + (
            "starts at " + ", ".join(starts) + ". "
            if starts
            else ""
        )
        + (
            "".join(
                f"{node_id} fans out to {', '.join(targets)}. " for node_id, targets in fans
            )
            or ""
        )
        + (
            "".join(
                f"{node_id} is a join and waits for all of {', '.join(sources)} before it runs. "
                for node_id, sources in joins
            )
            or ""
        )
        + "Every edge carries a gate: "
        + "; ".join(
            f"{e.from_node} -> {e.to_node} requires '{e.condition}'" for e in topology.edges
        )
        + ".",
        "Rules for editing: node ids must be lowercase tokens and unique; every edge must "
        "reference nodes defined in this same spec; the edge graph must stay acyclic (backward "
        "movement is expressed with failure_policy.route_to, which is bounded by max_retries, "
        "not with an edge); and every edge condition must name a rule GatedVerifier actually "
        "implements, otherwise that branch could never advance. If this node is deleted or its "
        "spec fails validation, the registry keeps the last valid definition of this "
        "topology_id and logs why the edit was rejected.",
    ]
    return "\n\n".join(paragraphs)


def render_topology_node(
    topology: SwarmTopology,
    *,
    semantic_memory: str | None = None,
    created_by: str = "tala-system",
    generated_at: str | None = None,
) -> tuple[str, str]:
    """Render a topology to (node_id, html). Pure -- touches no filesystem.

    ``generated_at`` is injectable so ``write_topology_node`` can re-render an
    existing node with its original timestamp and detect that nothing actually
    changed; otherwise every save would rewrite the file (and re-anchor it in
    the ledger) purely because the clock moved.
    """
    node_id = topology_node_id(topology.topology_id)
    title = f"Swarm Topology: {topology.name}"
    generated_at = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prose = (semantic_memory or "").strip() or _default_semantic_memory(topology)

    # to_dict() is the authored spec only; load-time annotations
    # (source_node_id, semantic_memory) live on to_wire() and deliberately
    # never round-trip into #node-state.
    spec = topology.to_dict()
    spec["consumer"] = "sdk.agent.topology.TopologyRegistry.load_from_nodes"

    state_data: dict[str, Any] = {
        "title": title,
        "description": f"Executable swarm topology '{topology.topology_id}', read by SwarmEngine at run time.",
        "tags": ["html-nn", "swarm", "topology", "executable", topology.topology_id],
        "topology": spec,
        "metadata": {
            "author": created_by,
            "created_or_updated": generated_at,
            "schema": "neural-node/v2",
        },
    }

    relationships = [
        {
            "target_node_id": "system-neural-node-v2-schema",
            "label": "HTML Neural Node v2 Schema",
            "relationship_type": "bounded-by",
            "relationship_weight": 1.0,
            "navigation_role": "schema-contract",
        },
        {
            "target_node_id": "system-html-nn-authoring-guide",
            "label": "HTML-NN Authoring Guide",
            "relationship_type": "depends-on",
            "relationship_weight": 1.0,
            "navigation_role": "authoring-guide",
        },
        {
            "target_node_id": "system-tala-agent-architecture",
            "label": "Tala Agent Architecture",
            "relationship_type": "extends",
            "relationship_weight": 0.9,
            "navigation_role": "context",
        },
    ]

    director = {
        "when": {"intent": "modify_swarm_topology", "doc_target": topology.topology_id},
        "reference": [
            {"type": "html-nn", "node_id": "system-neural-node-v2-schema", "required": True},
        ],
        "then": {
            "instruction": (
                "Before editing the topology spec in this node, read the editing rules in "
                "#semantic-memory. After editing, the registry re-validates this spec on the "
                "next load; check the harness log for a 'rejecting topology node' warning, "
                "which names the exact reason an edit was refused."
            )
        },
    }

    node_list_html = "\n".join(
        f"      <li><code>{escape(n.id)}</code> ({escape(n.role)}) — {escape(n.description or 'no description')}</li>"
        for n in topology.nodes
    )
    edge_list_html = "\n".join(
        f"      <li><code>{escape(e.from_node)}</code> → <code>{escape(e.to_node)}</code> "
        f"gated on <code>{escape(e.condition)}</code></li>"
        for e in topology.edges
    ) or "      <li>No edges: every node is a start node and runs concurrently.</li>"

    return node_id, f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="tala-schema" content="neural-node/v2">
  <title>Neural Node: {escape(title)}</title>
</head>
<body>
<article
  data-node-id="{escape(node_id)}"
  data-node-type="{NODE_TYPE}"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.60"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-weight-behavior-influence="0.60"
  data-weight-governance-importance="0.90"
  data-governance-status="active"
  data-topology-id="{escape(topology.topology_id)}"
  data-generated-at="{generated_at}"
  data-source="local"
  data-trust-tier="system">

  <header id="node-identity">
    <h1 id="node-title">{escape(title)}</h1>
  </header>

  <section id="semantic-memory">
{_render_paragraphs(prose)}
  </section>

  <section id="source-evidence">
    <h2>Source Evidence</h2>
    <ul>
      <li><code>sdk/agent/swarm_engine.py</code> — the engine that runs this topology</li>
      <li><code>sdk/agent/topology.py</code> — spec validation and the registry that loads this node</li>
      <li><code>sdk/agent/gated_verifier.py</code> — implements the edge gate conditions used below</li>
    </ul>
    <h3>Agents</h3>
    <ul>
{node_list_html}
    </ul>
    <h3>Edges</h3>
    <ul>
{edge_list_html}
    </ul>
  </section>

  <section id="audit-activation-trace">
    <h2>Audit / Activation Trace</h2>
    <p>Written {escape(generated_at)} by {escape(created_by)} as the canonical, editable
    definition of this swarm. The registry validates this spec on every load and logs the exact
    reason whenever it rejects an edit.</p>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
{_render_relationships(relationships)}
    </ul>
  </nav>

  <script type="application/tala-director+json"
          id="director-reference-{escape(node_id)}"
          data-script-kind="reference"
          data-side-effects="none">
{json.dumps(director, indent=2)}
  </script>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{json.dumps(state_data, indent=2)}
  </script>
</article>
</body>
</html>
"""


def write_topology_node(
    topology: SwarmTopology,
    *,
    semantic_memory: str | None = None,
    created_by: str = "tala-system",
    overwrite: bool = True,
) -> dict[str, Any]:
    """
    Persist a topology as an HTML-NN and return a result dict.

    The spec is re-validated through ``topology_from_spec`` *after* rendering,
    so a node is never written in a state the loader would refuse to read
    back -- the failure mode this avoids is a swarm that "saved successfully"
    and then vanishes from the list on the next reload.
    """
    node_id = topology_node_id(topology.topology_id)
    nodes_dir = resolve_active_nodes_dir()
    nodes_dir.mkdir(parents=True, exist_ok=True)
    node_path = nodes_dir / f"{node_id}.html"

    if node_path.exists() and not overwrite:
        raise FileExistsError(f"topology node already exists: {node_path}")

    previous = node_path.read_text(encoding="utf-8") if node_path.exists() else None

    # Re-render at the existing node's timestamp first. If that reproduces the
    # file byte for byte, the topology genuinely has not changed and the node
    # is left alone -- saving an identical swarm should not churn the file or
    # produce a new ledger anchor.
    if previous is not None:
        prior_stamp = re.search(r'data-generated-at="([^"]+)"', previous)
        if prior_stamp:
            _, unchanged_html = render_topology_node(
                topology,
                semantic_memory=semantic_memory,
                created_by=created_by,
                generated_at=prior_stamp.group(1),
            )
            if unchanged_html == previous:
                return {
                    "success": True,
                    "created": False,
                    "updated": False,
                    "unchanged": True,
                    "node_id": node_id,
                    "path": str(node_path),
                    "topology_id": topology.topology_id,
                }

    node_id, html = render_topology_node(
        topology, semantic_memory=semantic_memory, created_by=created_by
    )

    if previous == html:
        return {
            "success": True,
            "created": False,
            "updated": False,
            "unchanged": True,
            "node_id": node_id,
            "path": str(node_path),
            "topology_id": topology.topology_id,
        }

    node_path.write_text(html, encoding="utf-8")

    # Read it straight back through the real loader. Writing and then trusting
    # the write is how a "saved" topology becomes an unrunnable one.
    reloaded = _parse_topology_node(node_path)
    if reloaded is None:
        if previous is None:
            node_path.unlink(missing_ok=True)
        else:
            node_path.write_text(previous, encoding="utf-8")
        raise TopologySpecError(
            f"topology node {node_id!r} failed to parse back after writing; the file was rolled back"
        )

    try:
        from sdk.agent.anchor import canonicalize_and_anchor

        canonicalize_and_anchor(
            node_id,
            html,
            json.loads(json.dumps(topology.to_dict())),
            None,
            creator=created_by,
            document_type="html-nn",
        )
    except Exception:
        log.debug("write_topology_node: ledger anchoring unavailable for %s", node_id, exc_info=True)

    from sdk.agent.topology import TopologyRegistry

    TopologyRegistry.register(reloaded)

    return {
        "success": True,
        "created": previous is None,
        "updated": previous is not None,
        "unchanged": False,
        "node_id": node_id,
        "path": str(node_path),
        "topology_id": topology.topology_id,
    }


def _parse_topology_node(path: Path) -> SwarmTopology | None:
    """Parse one topology node. Returns None (with a logged reason) on any problem."""
    try:
        from sdk.agent.parser import parse_file

        parsed = parse_file(str(path))
    except Exception as exc:
        log.warning("Rejecting topology node %s: it is not a valid neural node (%s)", path.name, exc)
        return None

    if parsed.node_type != NODE_TYPE:
        return None

    spec = (parsed.state_data or {}).get("topology")
    if not isinstance(spec, dict):
        log.warning(
            "Rejecting topology node %s: #node-state has no 'topology' object", path.name
        )
        return None

    try:
        topology = topology_from_spec(spec, source_node_id=parsed.node_id)
    except TopologySpecError as exc:
        log.warning("Rejecting topology node %s: %s", path.name, exc)
        return None

    # Carried back so the editor can show — and preserve — the prose the
    # author wrote, rather than regenerating over it on the next save.
    topology.semantic_memory = parsed.semantic_memory or ""
    return topology


def delete_topology_node(topology_id: str) -> dict[str, Any]:
    """
    Delete a topology's HTML-NN and drop it from the registry.

    Note this removes the *authored* definition. If a built-in fallback of the
    same id exists in `topology.py`, the reset below re-registers it and the
    topology reappears in its original shape — deleting the node reverts an
    edit, it does not remove a built-in. The result says which happened so the
    caller can tell the user the truth instead of reporting a deletion that
    visibly did not stick.
    """
    from sdk.agent.topology import TopologyRegistry

    node_id = topology_node_id(topology_id)
    node_path = resolve_active_nodes_dir() / f"{node_id}.html"
    existed = node_path.exists()
    if existed:
        node_path.unlink()

    TopologyRegistry.reset()
    reverted_to_builtin = TopologyRegistry.get(topology_id) is not None

    return {
        "success": existed,
        "node_deleted": existed,
        "node_id": node_id,
        "path": str(node_path),
        "topology_id": topology_id,
        "reverted_to_builtin": reverted_to_builtin,
    }


def iter_topology_node_paths() -> Iterator[Path]:
    """Yield every file in the active nodes dir that claims to be a topology."""
    nodes_dir = resolve_active_nodes_dir()
    if not nodes_dir.is_dir():
        return
    for path in sorted(nodes_dir.glob("*.html")):
        try:
            if _TYPE_MARKER in path.read_text(encoding="utf-8", errors="ignore"):
                yield path
        except OSError as exc:
            log.warning("Could not read candidate topology node %s: %s", path.name, exc)


def load_topology_nodes() -> list[SwarmTopology]:
    """Load and validate every topology HTML-NN. Invalid ones are skipped, not raised."""
    topologies: list[SwarmTopology] = []
    for path in iter_topology_node_paths():
        topology = _parse_topology_node(path)
        if topology is not None:
            topologies.append(topology)
    return topologies
