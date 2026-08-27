"""
architecture_contract.py — read the system's own declaration of how it works.

Steven, on why this exists:

    "you created your prob under default understandings of how systems work.
    But I am building a diffrent type of system what needs to happen is systems
    need to be able to loook at mine and determine its default system.
    Otherwise Ill run into the same problem with other remote LLM"

The contract itself is an HTML-NN (``system-architecture-contract``), generated
by inspecting the running system so it cannot drift into a confident
description of something no longer true. This module is the read side: one
function, used by the ``describe_architecture`` tool and the
``system.architecture_contract`` RPC, so an agent and an external caller get
the same answer.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

NODE_ID = "system-architecture-contract"

SECTIONS = ("vocabulary", "differs_from_default", "stores", "capabilities")


def load_contract(section: str = "") -> dict[str, Any]:
    """
    Return the architecture contract, or one section of it.

    Never raises: a caller asking how the system works is usually about to make
    an assumption, and an exception here sends it back to its defaults, which
    is the exact failure this exists to prevent. A missing contract returns a
    result that says so and still tells the caller what to do next.
    """
    try:
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir
        from sdk.agent.parser import parse_file

        path = resolve_active_nodes_dir() / f"{NODE_ID}.html"
        if not path.exists():
            return {
                "available": False,
                "error": (
                    f"No architecture contract has been generated. Run "
                    f"scripts/generate_architecture_contract.py. Until then, do "
                    f"not assume this system follows common conventions: verify "
                    f"with logging.get_system_health and tools.catalogue."
                ),
            }
        state = parse_file(str(path)).state_data or {}
    except Exception as exc:
        return {
            "available": False,
            "error": f"Could not read the architecture contract: {type(exc).__name__}: {exc}",
        }

    contract = state.get("architecture_contract")
    if not isinstance(contract, dict):
        return {
            "available": False,
            "error": (
                "The architecture contract node carries no contract. "
                "Regenerate it with scripts/generate_architecture_contract.py."
            ),
        }

    wanted = str(section or "").strip().lower()
    if not wanted:
        return {"available": True, **contract}

    if wanted not in SECTIONS:
        return {
            "available": False,
            "error": f"Unknown section {wanted!r}. Available: {', '.join(SECTIONS)}.",
        }

    return {
        "available": True,
        "section": wanted,
        wanted: contract.get(wanted),
        # Carried on every partial answer: a caller that asked for one section
        # is mid-assumption about the rest.
        "read_this_first": contract.get("read_this_first", ""),
    }


def orientation_line() -> str:
    """
    One line for the system prompt, pointing a model at the contract.

    Deliberately states that defaults do not apply rather than summarising how
    the system works. A summary invites the model to proceed on the summary; a
    warning plus a pointer makes it go and read.
    """
    return (
        "[System] Tala does not follow several common architectural defaults - "
        "memory is the HTML Neural Node store rather than a database, the data "
        "root travels with a portable install, and a configured provider is not "
        "a working one until probed. Call `describe_architecture` before "
        "reasoning about how this system is built, or before writing anything "
        "that inspects, migrates, backs up or health-checks it."
    )
