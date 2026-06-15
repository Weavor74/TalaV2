"""
sdk/gateway/resolver.py
=======================
MCP Command Resolution Layer

Normalizes bare commands by searching:
1. System PATH
2. Bundled fallback paths
"""
import os
import shutil
import logging
from pathlib import Path

log = logging.getLogger(__name__)

BUNDLED_PATHS = [
    "/usr/local/bin",
    "/usr/bin",
    "/opt/homebrew/bin",
    "C:\\Program Files\\nodejs",
    "C:\\Program Files\\Git\\bin",
]

class CommandResolutionError(Exception):
    pass

def resolve_command(command: str) -> str:
    """
    Resolve a bare command to an absolute executable path.
    Raises CommandResolutionError if not found.
    """
    if os.path.isabs(command) and os.path.isfile(command) and os.access(command, os.X_OK):
        log.debug(f"Resolver: Command {command} is already an absolute path.")
        return command
        
    system_path = shutil.which(command)
    if system_path:
        log.debug(f"Resolver: Found {command} in system PATH at {system_path}.")
        return system_path
        
    for fallback_dir in BUNDLED_PATHS:
        candidate = Path(fallback_dir) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            log.debug(f"Resolver: Found {command} in bundled path at {candidate}.")
            return str(candidate)
            
    if os.name == 'nt':
        for ext in ['.exe', '.cmd', '.bat']:
            if not command.lower().endswith(ext):
                candidate_ext = command + ext
                system_path_ext = shutil.which(candidate_ext)
                if system_path_ext:
                    log.debug(f"Resolver: Found {candidate_ext} in system PATH at {system_path_ext}.")
                    return system_path_ext
            
    log.error(f"Resolver: Failed to resolve command '{command}'")
    raise CommandResolutionError(f"Failed to resolve command: '{command}'. Please ensure it is installed and available in your PATH.")
