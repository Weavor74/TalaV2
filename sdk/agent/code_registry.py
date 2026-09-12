"""
sdk/agent/code_registry.py
==========================
Centralized registry for Code Styles and Boilerplate Templates (like Windows Registry).
Persists configuration settings in PostgreSQL table client_settings.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Fallback in-memory cache for tests/disconnected states
_MEMORY_REGISTRY: dict[str, Any] = {}

DEFAULT_REGISTRY_KEYS = {
    "tala.code.default.language": "python",
    "tala.code.style.python.indent": "4",
    "tala.code.style.python.naming": "snake_case",
    "tala.code.style.python.docstrings": "Google",
    "tala.code.style.nodejs.indent": "2",
    "tala.code.style.nodejs.naming": "camelCase",
    
    "tala.code.template.python.skill": (
        '"""{description}"""\n\n'
        'from __future__ import annotations\n'
        'from typing import Any, Dict\n'
        'from sdk.agent.tools import register_tool\n\n'
        '@register_tool(name="{tool_name}", description="{description}")\n'
        'async def {function_name}() -> Dict[str, Any]:\n'
        '    """{description}"""\n'
        '    pass\n'
    ),

    "tala.code.template.python.class": (
        '"""\n'
        '{description}\n'
        '"""\n\n'
        'class {class_name}:\n'
        '    def __init__(self) -> None:\n'
        '        pass\n'
    ),
    "tala.code.template.python.main": (
        '"""\n'
        '{description}\n'
        '"""\n'
        'import sys\n\n'
        'def main():\n'
        '    print("Running {class_name}")\n\n'
        'if __name__ == "__main__":\n'
        '    main()\n'
    ),
    "tala.code.template.python.test": (
        'import pytest\n'
        'from {module_name} import {class_name}\n\n'
        'def test_{class_name_lower}_basic():\n'
        '    assert True\n'
    ),
    "tala.code.template.nodejs.class": (
        '/**\n'
        ' * {description}\n'
        ' */\n'
        'class {class_name} {\n'
        '  constructor() {}\n'
        '}\n\n'
        'module.exports = { {class_name} };\n'
    ),
    "tala.code.template.nodejs.main": (
        '/**\n'
        ' * {description}\n'
        ' */\n'
        'function main() {\n'
        '  console.log("Running {class_name}");\n'
        '}\n\n'
        'if (require.main === module) {\n'
        '  main();\n'
        '}\n'
    ),
    "tala.code.template.nodejs.test": (
        'const assert = require("assert");\n'
        'const { {class_name} } = require("./{file_name}");\n\n'
        'describe("{class_name}", () => {\n'
        '  it("should work", () => {\n'
        '    assert.ok(true);\n'
        '  });\n'
        '});\n'
    )
}

class CodeRegistry:
    @staticmethod
    async def seed_registry(pool=None) -> None:
        """
        Seed default registry settings into the client_settings table if absent.
        """
        log.info("CodeRegistry: Seeding default styles and templates...")
        for key, val in DEFAULT_REGISTRY_KEYS.items():
            _MEMORY_REGISTRY[key] = val
            try:
                from python.harness import db
                # Ensure the pool is initialized or we fallback
                db_pool = pool or db.get_pool()
                if db_pool:
                    await db.execute(
                        """
                        INSERT INTO client_settings (key, value, updated_at)
                        VALUES ($1, $2::jsonb, NOW())
                        ON CONFLICT (key) DO NOTHING
                        """,
                        key,
                        {"value": val}
                    )
            except Exception as e:
                # Silently skip if DB not ready
                pass

    @staticmethod
    async def get(key: str) -> Any:
        """
        Get value of registry key. Fallback to memory registry if not found or DB unavailable.
        """
        try:
            from python.harness import db
            row = await db.fetchrow("SELECT value FROM client_settings WHERE key = $1", key)
            if row and row["value"]:
                val = row["value"]
                if isinstance(val, dict):
                    return val.get("value")
                return val
        except Exception:
            pass
        return _MEMORY_REGISTRY.get(key, DEFAULT_REGISTRY_KEYS.get(key))

    @staticmethod
    async def set(key: str, value: Any) -> None:
        """
        Set value of registry key. Also updates in-memory copy.
        """
        _MEMORY_REGISTRY[key] = value
        try:
            from python.harness import db
            await db.execute(
                """
                INSERT INTO client_settings (key, value, updated_at)
                VALUES ($1, $2::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW()
                """,
                key,
                {"value": value}
            )
        except Exception as e:
            log.warning("CodeRegistry: Failed to write to DB: %s", e)

    @staticmethod
    async def list_keys(prefix: str | None = None) -> dict[str, Any]:
        """
        List all keys starting with prefix. Falls back to combined DEFAULT / MEMORY registry.
        """
        results = {}
        # Start with defaults/memory
        for k, v in DEFAULT_REGISTRY_KEYS.items():
            if not prefix or k.startswith(prefix):
                results[k] = _MEMORY_REGISTRY.get(k, v)

        try:
            from python.harness import db
            rows = await db.fetch("SELECT key, value FROM client_settings")
            for r in rows:
                k = r["key"]
                if not prefix or k.startswith(prefix):
                    val = r["value"]
                    results[k] = val.get("value") if isinstance(val, dict) else val
        except Exception:
            pass

        return results
