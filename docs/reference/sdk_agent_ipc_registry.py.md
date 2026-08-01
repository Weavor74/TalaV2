# File: `ipc_registry.py`

## Module Description

sdk/agent/ipc_registry.py
=========================
IPC Registry — Tala Cognitive Engine Room
Milestone 9 | Task 9.1

Discovers all registered RPC methods from server.py and all slash commands
from renderer.js, and cross-references them to detect gaps between what
the backend exposes and what the frontend actually calls.

## Classes

### `class IPCRegistry`
Discovers registered RPC methods and slash commands across the TalaV2
codebase, then cross-references them to surface integration gaps.

- **`def __init__(self, root_dir)`**
- **`def discover_rpc_methods(self)`**
  - Read python/harness/server.py and find all string keys registered in
  the ``_HANDLERS`` dict.  Two regex patterns are tried:
  
  1. ``'method.name': handle_*``  (primary, handler-function suffix)
  2. ``'method.name': <any_lower_identifier>``  (fallback for lambdas /
     other callables inside the _HANDLERS section)
  
  Returns a sorted, de-duplicated list of method names.
- **`def discover_slash_commands(self)`**
  - Read renderer/renderer.js and find all slash command strings matched by
  ``message.startsWith('/cmd')`` patterns.
  
  Returns a sorted, de-duplicated list of slash command strings (e.g.
  ``['/help', '/clear']``).
- **`def audit(self)`**
  - Run full discovery across RPC methods and slash commands, then check
  which RPC method names appear anywhere in renderer.js.
  
  Returns
  -------
  dict with keys:
      rpc_methods               — sorted list of all registered RPC methods
      slash_commands            — sorted list of all slash commands
      rpc_without_renderer_caller — RPC methods not referenced in renderer.js
      total_rpc                 — int count of rpc_methods
      total_slash               — int count of slash_commands

## Source Code

```python
"""
sdk/agent/ipc_registry.py
=========================
IPC Registry — Tala Cognitive Engine Room
Milestone 9 | Task 9.1

Discovers all registered RPC methods from server.py and all slash commands
from renderer.js, and cross-references them to detect gaps between what
the backend exposes and what the frontend actually calls.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)


class IPCRegistry:
    """
    Discovers registered RPC methods and slash commands across the TalaV2
    codebase, then cross-references them to surface integration gaps.
    """

    def __init__(self, root_dir: str) -> None:
        self.root_dir = os.path.abspath(root_dir)

    def discover_rpc_methods(self) -> list[str]:
        """
        Read python/harness/server.py and find all string keys registered in
        the ``_HANDLERS`` dict.  Two regex patterns are tried:

        1. ``'method.name': handle_*``  (primary, handler-function suffix)
        2. ``'method.name': <any_lower_identifier>``  (fallback for lambdas /
           other callables inside the _HANDLERS section)

        Returns a sorted, de-duplicated list of method names.
        """
        server_path = os.path.join(self.root_dir, "python", "harness", "server.py")
        if not os.path.exists(server_path):
            log.warning("server.py not found at %s", server_path)
            return []

        content = Path(server_path).read_text(encoding="utf-8", errors="replace")

        methods: set[str] = set()

        # Primary pattern: keys mapped to handle_* functions
        primary = re.findall(r"['\"]([a-z][a-z0-9_.]+)['\"]\s*:\s*handle_", content)
        methods.update(primary)

        # Isolate the _HANDLERS section (from assignment to the next top-level
        # statement) so the fallback pattern doesn't pick up unrelated strings.
        handlers_section = ""
        match = re.search(r"_HANDLERS\s*=\s*\{(.+?)\}", content, re.DOTALL)
        if match:
            handlers_section = match.group(1)

        if handlers_section:
            fallback = re.findall(
                r"['\"]([a-z][a-z0-9_.]+)['\"]\s*:\s*[a-z]", handlers_section
            )
            methods.update(fallback)

        result = sorted(methods)
        log.info("IPCRegistry: discovered %d RPC methods.", len(result))
        return result

    def discover_slash_commands(self) -> list[str]:
        """
        Read renderer/renderer.js and find all slash command strings matched by
        ``message.startsWith('/cmd')`` patterns.

        Returns a sorted, de-duplicated list of slash command strings (e.g.
        ``['/help', '/clear']``).
        """
        renderer_path = os.path.join(self.root_dir, "renderer", "renderer.js")
        if not os.path.exists(renderer_path):
            log.warning("renderer.js not found at %s", renderer_path)
            return []

        content = Path(renderer_path).read_text(encoding="utf-8", errors="replace")
        commands = []
        commands.extend(re.findall(r"command\s*===\s*['\"](/\w+)['\"]", content))
        commands.extend(re.findall(r"case\s*['\"](/\w+)['\"]\s*:", content))
        commands.extend(re.findall(r"cmd\s*:\s*['\"](/\w+)['\"]", content))
        result = sorted(set(commands))
        log.info("IPCRegistry: discovered %d slash commands.", len(result))
        return result

    def audit(self) -> dict:
        """
        Run full discovery across RPC methods and slash commands, then check
        which RPC method names appear anywhere in renderer.js.

        Returns
        -------
        dict with keys:
            rpc_methods               — sorted list of all registered RPC methods
            slash_commands            — sorted list of all slash commands
            rpc_without_renderer_caller — RPC methods not referenced in renderer.js
            total_rpc                 — int count of rpc_methods
            total_slash               — int count of slash_commands
        """
        rpc_methods = self.discover_rpc_methods()
        slash_commands = self.discover_slash_commands()

        # Read renderer.js once for cross-reference check
        renderer_path = os.path.join(self.root_dir, "renderer", "renderer.js")
        renderer_text = ""
        if os.path.exists(renderer_path):
            try:
                renderer_text = Path(renderer_path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except Exception as exc:
                log.warning("Could not read renderer.js: %s", exc)

        rpc_without_renderer_caller = [
            m for m in rpc_methods if m not in renderer_text
        ]

        return {
            "rpc_methods": rpc_methods,
            "slash_commands": slash_commands,
            "rpc_without_renderer_caller": rpc_without_renderer_caller,
            "total_rpc": len(rpc_methods),
            "total_slash": len(slash_commands),
        }

```
