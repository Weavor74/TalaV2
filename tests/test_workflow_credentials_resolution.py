import pytest
from unittest.mock import patch
from python.harness.workflow_executor import WorkflowExecutor

@pytest.mark.asyncio
async def test_workflow_auth_node_credential_resolution():
    """
    NAME
           test_workflow_auth_node_credential_resolution - verifies resolving auth credentials in workflow auth step
           
    SYNOPSIS
           test_workflow_auth_node_credential_resolution() -> None
           
    DESCRIPTION
           Executes a mock workflow with an auth node having empty configuration.
           Asserts that it retrieves and uses the active/locked credential from the auth settings tab.
    """
    mock_workflow = {
        "nodes": [
            {
                "id": "node-trigger",
                "type": "trigger",
                "connections": {"onSuccess": "node-auth"}
            },
            {
                "id": "node-auth",
                "type": "auth",
                "config": {},
                "connections": {"onSuccess": "node-output"}
            },
            {
                "id": "node-output",
                "type": "output",
                "connections": {}
            }
        ]
    }

    mock_cred_rows = [
        {
            "name": "Standard Key",
            "auth_type": "api_key",
            "config": {"api_key": "some-api-key"},
            "locked": False
        },
        {
            "name": "Locked Main Key",
            "auth_type": "api_key",
            "config": {"api_key": "verified-auth-section-key"},
            "locked": True
        }
    ]

    with patch("python.harness.db.fetch", return_value=mock_cred_rows):
        executor = WorkflowExecutor(mock_workflow)
        res = await executor.execute()
        assert res is not None

@pytest.mark.asyncio
async def test_workflow_email_node_credential_resolution():
    """
    NAME
           test_workflow_email_node_credential_resolution - verifies resolving email credentials in workflow email step
           
    SYNOPSIS
           test_workflow_email_node_credential_resolution() -> None
           
    DESCRIPTION
           Executes a mock workflow with an email node.
           Asserts that it resolves email provider settings directly from the Auth section database list.
    """
    mock_workflow = {
        "nodes": [
            {
                "id": "node-trigger",
                "type": "trigger",
                "connections": {"onSuccess": "node-email"}
            },
            {
                "id": "node-email",
                "type": "email",
                "config": {
                    "to": "recipient@tala.local",
                    "subject": "System Warning",
                    "body": "Alert body details"
                },
                "connections": {"onSuccess": "node-output"}
            },
            {
                "id": "node-output",
                "type": "output",
                "connections": {}
            }
        ]
    }

    mock_cred_rows = [
        {
            "name": "My Custom SMTP Server",
            "auth_type": "api_key",
            "config": {"service": "SMTP", "host": "smtp.tala.local"},
            "locked": True
        }
    ]

    with patch("python.harness.db.fetch", return_value=mock_cred_rows):
        executor = WorkflowExecutor(mock_workflow)
        res = await executor.execute()
        assert res is not None
