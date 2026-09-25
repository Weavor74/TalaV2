"""
test_gateway.py - Auto-generated documentation base.
"""

import pytest
import os
import sys
from pathlib import Path

_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from sdk.gateway.resolver import resolve_command, CommandResolutionError
from sdk.gateway.plugins import load_and_validate_manifest, PluginValidationError
from sdk.gateway.permissions import _check_permission, enforce_tool_permissions, PermissionDeniedError
from sdk.gateway.lifecycle import register_process, shutdown_all, get_process_status

def test_command_resolution():
    # Should resolve python or python3
    py_cmd = "python.exe" if os.name == "nt" else "python"
    
    # We can't guarantee 'python' vs 'python3' across environments but we can test resolution of known tools
    try:
        res = resolve_command(py_cmd)
        assert os.path.isabs(res)
    except CommandResolutionError:
        pass # Maybe not in path in this strict env, that's okay.
        
    with pytest.raises(CommandResolutionError):
        resolve_command("some_fake_nonexistent_command_12345")

def test_plugin_validation(tmp_path):
    manifest_path = tmp_path / "plugin.json"
    
    # Valid manifest
    manifest_path.write_text('{"id":"test-plugin", "name":"Test", "version":"1.0", "entrypoint":"node ."}')
    manifest = load_and_validate_manifest(str(manifest_path))
    assert manifest["id"] == "test-plugin"
    assert manifest["trust_level"] == "untrusted"
    
    # Invalid ID
    manifest_path.write_text('{"id":"test plugin!", "name":"Test", "version":"1.0", "entrypoint":"node ."}')
    with pytest.raises(PluginValidationError):
        load_and_validate_manifest(str(manifest_path))
        
    # Invalid permission
    manifest_path.write_text('{"id":"test", "name":"Test", "version":"1.0", "entrypoint":"node .", "permissions":["god_mode"]}')
    with pytest.raises(PluginValidationError):
        load_and_validate_manifest(str(manifest_path))

def test_permissions():
    manifest = {"id": "test", "permissions": ["fs:read", "network:outbound"]}
    
    # Allowed
    _check_permission(manifest, "fs:read")
    enforce_tool_permissions(manifest, "read_file", {})
    enforce_tool_permissions(manifest, "http_fetch", {})
    
    # Denied
    with pytest.raises(PermissionDeniedError):
        _check_permission(manifest, "fs:write")
        
    with pytest.raises(PermissionDeniedError):
        enforce_tool_permissions(manifest, "write_file", {})
        
    with pytest.raises(PermissionDeniedError):
        enforce_tool_permissions(manifest, "run_command", {})

@pytest.mark.asyncio
async def test_lifecycle():
    # Mock a process
    class MockProcess:
        def __init__(self, pid):
            self.pid = pid
            self.returncode = None
            self.terminated = False
            self.killed = False
            
        def terminate(self):
            self.terminated = True
            self.returncode = 0 # Simulate exit
            
        def kill(self):
            self.killed = True
            self.returncode = -9
            
    proc = MockProcess(99999)
    register_process(proc, {"name": "test_mcp"})
    
    status = get_process_status()
    assert len(status) == 1
    assert status[0]["is_alive"] is True
    
    # Test graceful shutdown
    await shutdown_all(timeout_secs=1)
    
    assert proc.terminated is True
    assert len(get_process_status()) == 0

@pytest.mark.asyncio
async def test_tool_registry_enforcement():
    from sdk.agent.tools import ToolRegistry
    
    # Register a tool that requires 'fs:write' under a plugin that only has 'fs:read' and 'shell:execute'
    @ToolRegistry.register(name="fs_write_dummy", description="dummy", plugin_id="tala.test.mcp")
    async def fs_write_dummy():
        return True

    res = await ToolRegistry.run_tool("fs_write_dummy", {})
    assert res["success"] == False
    assert "Plugin 'tala.test.mcp' lacks required permission" in res["error"]

    # Register a tool that requires 'shell:execute'
    @ToolRegistry.register(name="shell_dummy", description="dummy", plugin_id="tala.test.mcp")
    async def shell_dummy():
        return "success!"

    res2 = await ToolRegistry.run_tool("shell_dummy", {})
    assert res2["success"] == True
    assert res2["output"] == "success!"
