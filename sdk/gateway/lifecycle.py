"""
sdk/gateway/lifecycle.py
========================
Process tracking, lifecycle management, and graceful shutdown.
"""
import os
import sys
import time
import asyncio
import logging
from typing import Dict, Any, List

log = logging.getLogger(__name__)

_ACTIVE_PROCESSES: Dict[int, asyncio.subprocess.Process] = {}
_PROCESS_METADATA: Dict[int, Dict[str, Any]] = {}

def register_process(proc: asyncio.subprocess.Process, metadata: Dict[str, Any]):
    """Register a subprocess for tracked lifecycle management."""
    _ACTIVE_PROCESSES[proc.pid] = proc
    _PROCESS_METADATA[proc.pid] = metadata
    log.info(f"Lifecycle: Registered process {proc.pid} ({metadata.get('name', 'unknown')})")

def unregister_process(pid: int):
    """Remove a process from tracking after it exits cleanly."""
    if pid in _ACTIVE_PROCESSES:
        del _ACTIVE_PROCESSES[pid]
    if pid in _PROCESS_METADATA:
        del _PROCESS_METADATA[pid]

async def shutdown_all(timeout_secs: int = 5):
    """
    Graceful shutdown protocol:
    1. Send SIGTERM.
    2. Wait timeout_secs.
    3. Send SIGKILL for stragglers.
    4. Log forensics.
    """
    log.info(f"Lifecycle: Initiating shutdown of {len(_ACTIVE_PROCESSES)} active processes.")
    
    for pid, proc in _ACTIVE_PROCESSES.items():
        if proc.returncode is None:
            try:
                proc.terminate()
                log.debug(f"Lifecycle: Sent SIGTERM to {pid}")
            except ProcessLookupError:
                pass
                
    start_time = time.time()
    while time.time() - start_time < timeout_secs:
        all_dead = all(p.returncode is not None for p in _ACTIVE_PROCESSES.values())
        if all_dead:
            break
        await asyncio.sleep(0.5)
        
    forensics = []
    for pid, proc in list(_ACTIVE_PROCESSES.items()):
        if proc.returncode is None:
            try:
                proc.kill()
                log.warning(f"Lifecycle: Sent SIGKILL to straggler {pid}")
            except ProcessLookupError:
                pass
        
        meta = _PROCESS_METADATA.get(pid, {})
        forensics.append({
            "pid": pid,
            "name": meta.get("name"),
            "command": meta.get("command"),
            "exit_code": proc.returncode if hasattr(proc, "returncode") else "unknown"
        })
        
    if forensics:
        log.info(f"Lifecycle: Shutdown forensics: {forensics}")
        
    _ACTIVE_PROCESSES.clear()
    _PROCESS_METADATA.clear()
    log.info("Lifecycle: Shutdown complete.")

def get_process_status() -> List[Dict[str, Any]]:
    """Return status of all tracked processes for the Gateway status endpoint."""
    status_list = []
    for pid, proc in _ACTIVE_PROCESSES.items():
        meta = _PROCESS_METADATA.get(pid, {})
        status_list.append({
            "pid": pid,
            "name": meta.get("name", "unknown"),
            "is_alive": proc.returncode is None
        })
    return status_list
