"""System diagnostics skill for the Tala agent SDK."""

from __future__ import annotations

from typing import Any, Dict

from sdk.agent.tools import register_tool

try:
    import psutil
except ImportError:
    psutil = None


@register_tool(name="system_diagnostics", description="Returns CPU, memory, and disk usage for the current host")
async def get_system_diagnostics() -> Dict[str, Any]:
    """Return CPU, memory, and disk usage for the current host."""
    if psutil is None:
        return {
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": None,
            "status": "error: psutil is not available",
        }

    try:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory_percent = psutil.virtual_memory().percent
        disk_percent = psutil.disk_usage("/").percent
        return {
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "disk_percent": disk_percent,
            "status": "ok",
        }
    except Exception as exc:
        return {
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": None,
            "status": f"error: {exc}",
        }