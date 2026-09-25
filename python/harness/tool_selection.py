"""
tool_selection.py — choose which tools a given turn actually offers the model.

Every provider was handed all 73 registry tools on every turn: ~5,975 prompt
tokens, measured, on a box whose local context ceiling is 16k and whose real
prompts already reach 11k. Adding a tool taxed every request, including the
embedded 0.5B, so the registry could not grow.

Selection here is a BUDGET mechanism, not an accuracy fix. Measured evidence
says the 4B chooses correctly among all 73 -- what it cannot do is afford them.
That distinction sets the design: a crude selector is fine as long as missing a
tool is cheap, so the effort goes into the floor (an always-on core), the
ceiling (a per-tier budget), and visibility (logging what was withheld), rather
than into ranking sophistication.

Ranking is keyword matching over each tool's own name and description, with no
authoring required. Word boundaries are mandatory: bare substring gates have
short-circuited turns in this codebase three separate times ('doc' inside
'docker', 'hi' inside 'everything'). This gate only ever ADDS tools, which is
the safe half of that pattern -- it can never hijack a turn.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Tokens carried by so many tools, or so little meaning, that matching on them
# selects noise. Derived by inspection of the live registry, where `html`
# appears in 21 tool descriptions and `node` in 14.
_STOPWORDS = frozenset("""
a an the and or of to for in on with by from at is are be been being can could this
that these those your you it its as into use uses used using return returns retrieve
get set list all any via if then than when where which what how not no do does done
new only also each per them they their we our us will would should may might must
""".split())

# Sent on every turn regardless of the prompt.
#
# These are the tools whose absence changes what the agent can do at all rather
# than how well it does one task: look something up, read the workspace, read its
# own memory, run a command, see the machine. A selector that misses is survivable
# because these are always present; the core is the floor that makes a weak
# ranker safe. Kept deliberately short -- at the embedded budget of 8 a large
# core would leave no room for anything the prompt actually asked for.
DEFAULT_CORE_TOOLS: tuple[str, ...] = (
    "web_search",
    "search_workspace_files",
    "read_workspace_file",
    "search_memory",
    "get_system_info",
)

# Tools offered per turn, by provider tier. Remote models have context to spare
# and no local VRAM ceiling, so they keep the full registry and behave exactly as
# they do today.
DEFAULT_BUDGETS: dict[str, int] = {
    "embedded": 8,     # 0.5B: structured tool calling works, but not at 73 schemas
    "local": 25,       # 4B-8B on consumer VRAM, where the 16k ceiling bites
    "remote": 0,       # 0 == unlimited
}

_WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_SPLIT_NAME_RE = re.compile(r"[._\W]+")


def _name_tokens(name: str) -> set[str]:
    return {t for t in _SPLIT_NAME_RE.split(name.lower()) if len(t) > 2 and t not in _STOPWORDS}


def _description_tokens(description: str) -> set[str]:
    return {t for t in _WORD_RE.findall((description or "").lower())
            if len(t) > 3 and t not in _STOPWORDS}


class ToolIndex:
    """Keyword index over the registry, plus any authored keyword supplement."""

    def __init__(self, tools: Iterable[dict[str, Any]], supplement: dict[str, list[str]] | None = None):
        self._entries: dict[str, tuple[set[str], set[str]]] = {}
        supplement = supplement or {}

        for tool in tools:
            name = tool.get("name")
            if not name:
                continue
            name_toks = _name_tokens(name)
            desc_toks = _description_tokens(tool.get("description", ""))
            # Authored keywords are treated as name-strength: they exist precisely
            # because the tool's own vocabulary does not match how a person asks
            # for it ("docker" reaching a shell tool, say).
            for keyword in supplement.get(name, []):
                name_toks |= _name_tokens(str(keyword))
            self._entries[name] = (name_toks, desc_toks - name_toks)

        # Document frequency: a term shared by 21 tools discriminates nothing, so
        # its contribution is damped rather than counted equally.
        self._df: Counter = Counter()
        for name_toks, desc_toks in self._entries.values():
            for token in name_toks | desc_toks:
                self._df[token] += 1

    def score(self, text: str) -> dict[str, float]:
        """Relevance of each tool to `text`. Absent means zero."""
        words = set(_WORD_RE.findall((text or "").lower()))
        scores: dict[str, float] = {}
        for name, (name_toks, desc_toks) in self._entries.items():
            score = 0.0
            for token in words & name_toks:
                score += 3.0 / (1 + self._df[token] / 8)
            for token in words & desc_toks:
                score += 1.0 / (1 + self._df[token] / 8)
            if score > 0:
                scores[name] = score
        return scores

    def __len__(self) -> int:
        return len(self._entries)


def _select_tool_names(
    text: str,
    index: ToolIndex,
    budget: int,
    *,
    core: Iterable[str] = DEFAULT_CORE_TOOLS,
    sticky: Iterable[str] = (),
    available: Iterable[str] | None = None,
) -> list[str]:
    """
    Names to offer this turn: core, plus anything already used this request,
    plus the highest-scoring remainder up to `budget`.

    `sticky` is every tool dispatched earlier in this same request. A multi-turn
    task that searched on turn 1 and writes on turn 3 must not lose the search
    tool from under itself mid-task, and re-scoring per turn would otherwise do
    exactly that once the conversation drifts.

    PRECEDENCE: core and sticky are guaranteed and are never dropped to fit the
    budget -- the budget bounds the ranked fill, not the guarantees. A budget
    smaller than the core therefore returns the core. Truncating the floor to
    honour a ceiling would remove exactly the property that makes a keyword
    ranker safe to ship, so the ceiling yields instead.

    budget <= 0 means unlimited, which is how remote providers keep today's
    behaviour untouched.
    """
    pool = set(available) if available is not None else None

    def _keep(names: Iterable[str]) -> list[str]:
        return [n for n in names if pool is None or n in pool]

    chosen: list[str] = []
    seen: set[str] = set()
    for name in [*_keep(core), *_keep(sticky)]:
        if name not in seen:
            seen.add(name)
            chosen.append(name)

    if budget <= 0:
        for name in _keep(sorted(pool or [])):
            if name not in seen:
                seen.add(name)
                chosen.append(name)
        return chosen

    ranked = sorted(index.score(text).items(), key=lambda kv: (-kv[1], kv[0]))
    for name, _ in ranked:
        if len(chosen) >= budget:
            break
        if name in seen or (pool is not None and name not in pool):
            continue
        seen.add(name)
        chosen.append(name)
    return chosen


def budget_for(provider_type: str, provider_config: dict[str, Any] | None = None) -> int:
    """Per-turn tool budget, overridable per provider via `tool_budget`."""
    if provider_config:
        override = provider_config.get("tool_budget")
        if isinstance(override, int) and override >= 0:
            return override
    return DEFAULT_BUDGETS.get(provider_type, 0)


def select_specs(
    specs: list[dict[str, Any]],
    text: str,
    index: ToolIndex,
    budget: int,
    *,
    core: Iterable[str] = DEFAULT_CORE_TOOLS,
    sticky: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Filter already-built tool specs down to the selection.

    Returns ``(kept_specs, withheld_names)``. The withheld list is returned
    rather than discarded so the caller can log it: quietly reducing what the
    model can do is the exact failure family this branch exists to close, and an
    unlogged selection is indistinguishable from a broken registry.
    """
    if budget <= 0:
        return specs, []

    by_name = {s.get("function", {}).get("name"): s for s in specs}
    keep = _select_tool_names(
        text, index, budget, core=core, sticky=sticky, available=list(by_name),
    )
    keep_set = set(keep)
    kept = [by_name[n] for n in keep if n in by_name]
    withheld = [n for n in by_name if n and n not in keep_set]
    return kept, withheld


def conversation_text(messages: list[dict[str, Any]], turns: int = 3) -> str:
    """
    The text selection is scored against: the last few non-system messages.

    System content is excluded deliberately -- it carries the persona and the
    tool documentation itself, which would match nearly every tool and flatten
    the ranking into noise.
    """
    parts: list[str] = []
    for message in reversed(messages or []):
        if message.get("role") == "system":
            continue
        content = message.get("content")
        if isinstance(content, list):  # vision turns carry a parts list
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str) and content.strip():
            parts.append(content)
        if len(parts) >= turns:
            break
    return "\n".join(reversed(parts))
