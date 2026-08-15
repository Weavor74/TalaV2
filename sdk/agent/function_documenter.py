"""
sdk/agent/function_documenter.py
=================================
Per-function self-documentation.

For a given Python function (typically a registered tool), generates and
maintains an HTML-NN node carrying two paired explanations:

  technical_explanation  logical / pseudocode-level description of the
                          mechanism, for a technical reader.
  plain_explanation       one short, non-technical sentence stating what the
                          function is *for*, e.g. "This function is to help
                          refine searches."

Regeneration is keyed off a hash of the function's own source text: if the
source hasn't changed since the node was last written, no LLM call is made
and the node is left untouched. This is the "whenever something got
modified" trigger for function-level self-documentation.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Callable

from sdk.agent.anchor import canonicalize_and_anchor
from sdk.agent.documentation_nodes import resolve_active_nodes_dir, _safe_node_id
from sdk.agent.parser import parse_file

log = logging.getLogger(__name__)

_TECH_PLAIN_RE = re.compile(
    r"TECHNICAL:\s*(.+?)\s*\nPLAIN:\s*(.+)", re.DOTALL | re.IGNORECASE
)

_PENDING_TECHNICAL = (
    "Technical explanation pending — the last generation attempt did not "
    "return a parseable result."
)
_PENDING_PLAIN = (
    "Plain-English explanation pending — the last generation attempt did "
    "not return a parseable result."
)


def _source_fingerprint(source: str) -> str:
    return f"sha256:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"


def _existing_fingerprint(node_path: Path) -> str | None:
    if not node_path.exists():
        return None
    try:
        html = node_path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.search(r'data-source-fingerprint="([^"]+)"', html)
    return m.group(1) if m else None


async def _generate_dual_explanation(function_name: str, source_code: str) -> tuple[str, str]:
    """
    Ask the configured inference provider for a paired technical + plain
    explanation of a function. Never fabricates a confident-looking
    explanation if the model doesn't answer in the expected shape — falls
    back to a clearly-marked pending placeholder instead.
    """
    prompt = (
        f"Function name: {function_name}\n\n"
        f"Source code:\n```python\n{source_code}\n```\n\n"
        "Respond in exactly this format and nothing else:\n"
        "TECHNICAL: <one or two precise, logical/pseudocode-style sentences describing the mechanism>\n"
        "PLAIN: <one short, plain-English sentence a non-technical reader would understand, e.g. "
        "\"This function is to help refine searches.\">"
    )
    system_prompt = (
        "You are Tala, documenting your own source code for your self-updating "
        "memory. Follow the requested TECHNICAL/PLAIN format exactly."
    )

    text = ""
    try:
        from python.harness.server import handle_inference_generate

        res_str = await handle_inference_generate(
            {
                "prompt": prompt,
                "stream": False,
                "raw_prompt": True,
                "system_prompt": system_prompt,
                "max_tokens": 300,
            },
            ws=None,
            req_id="function-documenter",
        )
        res_dict = json.loads(res_str)
        if isinstance(res_dict, dict):
            text = res_dict.get("result", {}).get("text", "")
    except Exception as exc:
        log.warning("FunctionDocumenter: LLM call failed for %s: %s", function_name, exc)

    m = _TECH_PLAIN_RE.search(text)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    log.warning(
        "FunctionDocumenter: could not parse dual explanation for %s; writing pending placeholder.",
        function_name,
    )
    return _PENDING_TECHNICAL, _PENDING_PLAIN


def _render_function_node(
    *,
    node_id: str,
    function_name: str,
    source_file: str,
    source_fingerprint: str,
    technical_explanation: str,
    plain_explanation: str,
    doc_target: str,
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_data = {
        "function_name": function_name,
        "source_file": source_file,
        "doc_target": doc_target,
        "technical_explanation": technical_explanation,
        "plain_explanation": plain_explanation,
        "generated_at": generated_at,
    }

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="tala-schema" content="neural-node/v2">
  <title>Neural Node: {escape(function_name)}</title>
</head>
<body>
<article
  data-node-id="{escape(node_id)}"
  data-node-type="function-doc"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.60"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-weight-behavior-influence="0.00"
  data-weight-governance-importance="0.80"
  data-governance-status="context-only"
  data-doc-target="{escape(doc_target)}"
  data-source-files="{escape(source_file)}"
  data-source-fingerprint="{escape(source_fingerprint)}"
  data-generated-at="{generated_at}"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity">
    <h1 id="node-title">{escape(function_name)}</h1>
    <p><code>{escape(source_file)}</code></p>
  </header>

  <section id="technical-explanation">
    <h2>Technical Explanation</h2>
    <p>{escape(technical_explanation)}</p>
  </section>

  <section id="plain-explanation">
    <h2>Plain-English Explanation</h2>
    <p>{escape(plain_explanation)}</p>
  </section>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{json.dumps(state_data, indent=2)}
  </script>
</article>
</body>
</html>
"""


async def document_function(
    *,
    func: Callable[..., Any],
    doc_target: str = "tool.registry",
    force: bool = False,
) -> dict[str, Any]:
    """
    Generate or refresh the paired technical/plain-English HTML-NN node for a
    single Python function/tool. No-ops (no LLM call, no file write) if the
    function's source hasn't changed since the node was last written, unless
    force=True.
    """
    function_name = func.__name__
    try:
        source_code = inspect.getsource(func)
        source_file = inspect.getsourcefile(func) or "unknown"
    except (OSError, TypeError) as exc:
        return {"success": False, "changed": False, "error": f"Could not read source for {function_name}: {exc}"}

    fingerprint = _source_fingerprint(source_code)
    node_id = _safe_node_id(f"function-doc-{function_name}")
    nodes_dir = resolve_active_nodes_dir()
    nodes_dir.mkdir(parents=True, exist_ok=True)
    node_path = nodes_dir / f"{node_id}.html"

    if not force and _existing_fingerprint(node_path) == fingerprint:
        return {"success": True, "changed": False, "node_id": node_id, "path": str(node_path)}

    technical_explanation, plain_explanation = await _generate_dual_explanation(function_name, source_code)

    html = _render_function_node(
        node_id=node_id,
        function_name=function_name,
        source_file=source_file,
        source_fingerprint=fingerprint,
        technical_explanation=technical_explanation,
        plain_explanation=plain_explanation,
        doc_target=doc_target,
    )
    node_path.write_text(html, encoding="utf-8")

    try:
        canonicalize_and_anchor(
            node_id, html, {"function_name": function_name}, None,
            creator="tala-function-documenter", document_type="html-nn",
        )
    except Exception:
        pass

    parsed = parse_file(node_path)
    errors: list[str] = []
    if parsed.node_id != node_id:
        errors.append(f"parsed node_id mismatch: {parsed.node_id}")

    return {
        "success": not errors,
        "changed": True,
        "node_id": node_id,
        "path": str(node_path),
        "errors": errors,
        "technical_explanation": technical_explanation,
        "plain_explanation": plain_explanation,
    }


async def document_registered_tools(
    tool_names: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    Run document_function() over some or all tools currently registered in
    ToolRegistry. Returns a summary of changed/unchanged/error counts.
    """
    from sdk.agent.tools import ToolRegistry

    ToolRegistry.auto_discover_skills()
    all_tools = ToolRegistry._tools
    targets = tool_names or list(all_tools.keys())

    results = []
    for name in targets:
        entry = all_tools.get(name.lower())
        if not entry:
            results.append({"success": False, "changed": False, "error": f"Unknown tool: {name}"})
            continue
        result = await document_function(func=entry["func"], doc_target="tool.registry", force=force)
        result["tool_name"] = name
        results.append(result)

    return {
        "success": all(r.get("success") for r in results),
        "changed": sum(1 for r in results if r.get("changed")),
        "unchanged": sum(1 for r in results if r.get("success") and not r.get("changed")),
        "errors": [r["error"] for r in results if r.get("error")],
        "results": results,
    }
