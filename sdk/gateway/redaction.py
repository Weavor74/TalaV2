"""
sdk/gateway/redaction.py
========================
Redacts sensitive information (tokens, keys, session hashes, PII) from logs and observability events.
"""
import re
import logging
from typing import Dict, Any

log = logging.getLogger(__name__)

SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)([a-zA-Z0-9_\-\.]{20,})"),
    re.compile(r"(?i)(api[_-]?key[\"'\s:=]+)([\w\-]{16,})"),
    re.compile(r"(?i)(password[\"'\s:=]+)([\w\-!@#$%^&*]{8,})"),
    re.compile(r"(?i)(secret[\"'\s:=]+)([\w\-]{16,})"),
    re.compile(r"(?i)(token[\"'\s:=]+)([\w\-]{16,})"),
]

def redact_string(text: str) -> str:
    """Masks secrets in a string."""
    if not isinstance(text, str):
        return text
        
    redacted_text = text
    for pattern in SECRET_PATTERNS:
        redacted_text = pattern.sub(r"\1***REDACTED***", redacted_text)
        
    return redacted_text

def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redacts secrets in a dictionary."""
    redacted = {}
    for k, v in data.items():
        if isinstance(v, str):
            redacted[k] = redact_string(v)
        elif isinstance(v, dict):
            redacted[k] = redact_dict(v)
        elif isinstance(v, list):
            redacted[k] = [redact_dict(i) if isinstance(i, dict) else (redact_string(i) if isinstance(i, str) else i) for i in v]
        else:
            redacted[k] = v
            
    for sensitive_key in ["password", "secret", "token", "api_key", "authorization", "cookie"]:
        keys_lower = [key.lower() for key in redacted.keys()]
        if sensitive_key in keys_lower:
            actual_key = list(redacted.keys())[keys_lower.index(sensitive_key)]
            redacted[actual_key] = "***REDACTED***"
            
    return redacted

def create_tool_observability_event(plugin_id: str, tool_name: str, duration_ms: float, status: str, args: Dict[str, Any], result: Any) -> Dict[str, Any]:
    """Creates a sanitized observability event for a tool invocation."""
    result_preview = str(result)[:500] + ("..." if len(str(result)) > 500 else "")
    
    return {
        "event_type": "mcp_tool_call",
        "plugin_id": plugin_id,
        "tool_name": tool_name,
        "duration_ms": duration_ms,
        "status": status,
        "args_redacted": redact_dict(args) if isinstance(args, dict) else redact_string(str(args)),
        "result_preview": redact_string(result_preview)
    }
