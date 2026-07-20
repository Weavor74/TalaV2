import pytest
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from sdk.agent.tools import send_a2a_message, register_p2p_send_callback
from python.harness.server import handle_p2p_receive_message, _send_a2a_message_helper, _handle_inbound_a2a


@pytest.mark.asyncio
async def test_send_a2a_message_tool():
    """
    NAME
           test_send_a2a_message_tool - verify the send_a2a_message tool execution
           
    SYNOPSIS
           test_send_a2a_message_tool() -> None
           
    DESCRIPTION
           Registers a mock callback with the tools subsystem, invokes the send_a2a_message
           tool, and asserts that the callback was triggered and returned success.
    """
    mock_cb = AsyncMock(return_value={"recipient": "test-peer", "response": {"accepted": True}})
    register_p2p_send_callback(mock_cb)

    res = await send_a2a_message("test-peer", "Hello agent!")
    assert res["success"] is True
    assert res["output"]["recipient"] == "test-peer"
    assert mock_cb.call_count == 1
    mock_cb.assert_called_with("test-peer", "Hello agent!")


@pytest.mark.asyncio
@patch("python.harness.server._get_contact")
@patch("python.harness.server._get_master_profile_config")
@patch("python.harness.server._call_peer_rpc")
@patch("python.harness.server._record_p2p_message")
async def test_send_a2a_message_helper(mock_record, mock_call, mock_profile, mock_get_contact):
    """
    NAME
           test_send_a2a_message_helper - test A2A message helper function
           
    SYNOPSIS
           test_send_a2a_message_helper(...) -> None
           
    DESCRIPTION
           Verifies that _send_a2a_message_helper correctly fetches contact details,
           constructs a P2P packet of type "a2a", signs it, sends it to the remote
           peer endpoint, and records the outbound message.
    """
    mock_get_contact.return_value = {
        "username": "peer-agent",
        "wallet_address": "0xABCDEF",
        "endpoint_url": "ws://peer-endpoint:8765",
        "shared_secret": "secret_key_123",
        "permissions": {}
    }
    mock_profile.return_value = {}
    mock_call.return_value = {"accepted": True}

    res = await _send_a2a_message_helper("peer-agent", "Query status")
    assert res["recipient"] == "peer-agent"
    assert res["response"] == {"accepted": True}
    assert mock_call.call_count == 1
    assert mock_record.call_count == 1
    
    # Assert type is "a2a" in signed packet
    args, kwargs = mock_record.call_args
    assert args[0] == "peer-agent"
    assert args[1] == "outbound"
    packet = args[2]
    assert packet["type"] == "a2a"
    assert packet["message"] == "Query status"


@pytest.mark.asyncio
@patch("python.harness.server._get_contact")
@patch("python.harness.server._verify_packet")
@patch("python.harness.server._record_p2p_message")
@patch("python.harness.server._handle_inbound_a2a")
async def test_handle_p2p_receive_message_triggers_a2a(mock_handle_inbound, mock_record, mock_verify, mock_get_contact):
    """
    NAME
           test_handle_p2p_receive_message_triggers_a2a - test receiving A2A packets
           
    SYNOPSIS
           test_handle_p2p_receive_message_triggers_a2a(...) -> None
           
    DESCRIPTION
           Verifies that handle_p2p_receive_message triggers the async background task
           _handle_inbound_a2a when the received packet is of type "a2a".
    """
    mock_get_contact.return_value = {
        "username": "peer-agent",
        "wallet_address": "0xABCDEF",
        "shared_secret": "secret_key_123"
    }
    mock_verify.return_value = True

    params = {
        "packet": {
            "type": "a2a",
            "sender_wallet": "0xABCDEF",
            "message": "Inbound hello"
        }
    }

    # Execute
    res_str = await handle_p2p_receive_message(params, req_id=99)
    assert "accepted" in res_str
    
    # Wait briefly for async task trigger
    await asyncio.sleep(0.05)
    
    assert mock_handle_inbound.call_count == 1
    mock_handle_inbound.assert_called_with("peer-agent", "Inbound hello")


@pytest.mark.asyncio
@patch("python.harness.db.fetch")
@patch("python.harness.db.fetchrow")
@patch("httpx.AsyncClient")
@patch("python.harness.server._send_a2a_message_helper")
async def test_handle_inbound_a2a_success(mock_send_helper, mock_httpx, mock_fetchrow, mock_fetch):
    """
    NAME
           test_handle_inbound_a2a_success - test successful inbound A2A processing
           
    SYNOPSIS
           test_handle_inbound_a2a_success(...) -> None
           
    DESCRIPTION
           Mocks active agent config, conversation history, and remote LLM provider,
           verifying that _handle_inbound_a2a runs the agent inference and replies to the peer.
    """
    # Mock no recent runaway loop messages
    # Mock active agent configuration
    mock_fetchrow.return_value = {
        "name": "Tala Core",
        "config": '{"system_prompt": "You are Tala agent."}'
    }

    # Mock DB history and providers list
    mock_fetch.side_effect = [
        [], # runaway loop check: 0 messages returned
        [ # history retrieval (last 20 messages)
            {"direction": "inbound", "message_json": '{"message": "How are you?"}'}
        ],
        [ # active providers list
            {
                "id": "remote-openai",
                "type": "remote",
                "name": "Remote OpenAI",
                "priority": 1,
                "status": "online",
                "config": '{"endpoint": "https://api.openai.com/v1", "model": "gpt-4o", "apiKey": "key-xyz"}'
            }
        ]
    ]

    # Mock HTTP response for LLM completion
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "I am fully operational."
                }
            }
        ]
    }
    
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_httpx.return_value.__aenter__.return_value = mock_client

    # Run
    await _handle_inbound_a2a("peer-agent", "Inbound hello")

    # Assert reply was generated and sent
    assert mock_send_helper.call_count == 1
    mock_send_helper.assert_called_with("peer-agent", "I am fully operational.")


@pytest.mark.asyncio
@patch("python.harness.db.fetch")
@patch("python.harness.server._send_a2a_message_helper")
async def test_handle_inbound_a2a_loop_prevention(mock_send_helper, mock_fetch):
    """
    NAME
           test_handle_inbound_a2a_loop_prevention - test runaway loop protection
           
    SYNOPSIS
           test_handle_inbound_a2a_loop_prevention(...) -> None
           
    DESCRIPTION
           Verifies that if there are 6 or more messages received in the last 15 seconds,
           _handle_inbound_a2a halts execution and does not trigger agent inference or auto-reply.
    """
    # Mock runaway loop check: 6 recent messages returned
    mock_fetch.return_value = [
        {"created_at": "timestamp"},
        {"created_at": "timestamp"},
        {"created_at": "timestamp"},
        {"created_at": "timestamp"},
        {"created_at": "timestamp"},
        {"created_at": "timestamp"}
    ]

    # Run
    await _handle_inbound_a2a("peer-agent", "Rapid spam")

    # Assert no reply sent
    assert mock_send_helper.call_count == 0
