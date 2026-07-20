"""
test_workflow_terminal_backends.py - Auto-generated documentation base.
"""

import pytest

from python.harness.workflow_executor import WorkflowExecutor
from python.inference.terminal_backends.base import ProcessResult


class RecordingEnvironment:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self._snapshot_ready = False
        self.init_called = False
        self.cleanup_called = False
        RecordingEnvironment.instances.append(self)

    def init_session(self):
        self.init_called = True
        self._snapshot_ready = True

    def execute(self, command, cwd=None):
        self.command = command
        self.cwd = cwd
        return ProcessResult(output="recorded\n", returncode=0)

    def cleanup(self):
        self.cleanup_called = True


def _workflow(config):
    return {
        "flow_id": "terminal-backend-smoke",
        "name": "Terminal Backend Smoke",
        "nodes": [
            {"id": "trigger", "type": "trigger", "connections": {"onSuccess": "bash"}},
            {"id": "bash", "type": "bash", "config": config},
        ],
    }


@pytest.mark.asyncio
async def test_bash_workflow_defaults_to_local_backend(tmp_path):
    result = await WorkflowExecutor(
        _workflow({"command": "echo local-default"}),
        root_dir=tmp_path,
    ).execute({})

    step = result["variables"]["step_bash"]
    assert step["success"] is True
    assert step["result"]["backend"] == "local"
    assert "local-default" in step["result"]["stdout"]


@pytest.mark.asyncio
async def test_bash_workflow_explicitly_uses_docker_backend(monkeypatch, tmp_path):
    RecordingEnvironment.instances = []

    import python.inference.terminal_backends.docker_backend as docker_backend

    monkeypatch.setattr(docker_backend, "DockerEnvironment", RecordingEnvironment)

    result = await WorkflowExecutor(
        _workflow({"command": "echo docker", "backend": "docker", "image": "ubuntu:test", "cwd": "/workspace"}),
        root_dir=tmp_path,
    ).execute({})

    env = RecordingEnvironment.instances[0]
    step = result["variables"]["step_bash"]
    assert step["success"] is True
    assert step["result"]["backend"] == "docker"
    assert env.kwargs["image"] == "ubuntu:test"
    assert env.kwargs["cwd"] == "/workspace"
    assert env.cleanup_called is True


@pytest.mark.asyncio
async def test_bash_workflow_explicitly_uses_ssh_backend(monkeypatch, tmp_path):
    RecordingEnvironment.instances = []

    import python.inference.terminal_backends.ssh_backend as ssh_backend

    monkeypatch.setattr(ssh_backend, "SSHEnvironment", RecordingEnvironment)

    result = await WorkflowExecutor(
        _workflow({"command": "echo ssh", "backend": "ssh", "host": "example.test", "user": "tala"}),
        root_dir=tmp_path,
    ).execute({})

    env = RecordingEnvironment.instances[0]
    step = result["variables"]["step_bash"]
    assert step["success"] is True
    assert step["result"]["backend"] == "ssh"
    assert env.kwargs["host"] == "example.test"
    assert env.kwargs["user"] == "tala"
    assert env.cleanup_called is True


@pytest.mark.asyncio
async def test_ssh_backend_requires_host_and_user(tmp_path):
    result = await WorkflowExecutor(
        _workflow({"command": "echo ssh", "backend": "ssh", "host": "example.test"}),
        root_dir=tmp_path,
    ).execute({})

    step = result["variables"]["step_bash"]
    assert result["status"] == "FAILED"
    assert step["success"] is False
    assert "requires 'host' and 'user'" in step["message"]
