import pytest
import platform
import json
from contextlib import asynccontextmanager
from python.harness.server import handle_inference_generate
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_handle_inference_generate_os_metadata_raw():
    ws_mock = AsyncMock()
    params = {
        "prompt": "echo hello",
        "raw_prompt": True,
        "system_prompt": "Test system prompt"
    }
    
    # We mock guardrails loading and validate_prompt to bypass checks
    with patch("python.harness.guardrails_manager.GuardrailsManager.load_current") as mock_load:
        mgr_mock = MagicMock()
        mgr_mock.validate_prompt.return_value = None
        mgr_mock.validate_output.return_value = {"violations": []}
        mock_load.return_value = mgr_mock
        
        # Mock db.transaction context manager
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {"id": 1}
        
        @asynccontextmanager
        async def mock_transaction():
            yield mock_conn
            
        with patch("python.harness.db.transaction", side_effect=mock_transaction):
            # We patch the final token counter and vllm/provider call to inspect messages
            with patch("python.harness.token_counter.count_and_check") as mock_check:
                # mock token counter result
                mock_result = MagicMock()
                mock_result.ghost_mode = True
                mock_result.prompt_tokens = 5
                mock_result.cost_usd = 0.0
                mock_result.balance_before = 999999.0
                mock_result.balance_after = 999999.0
                mock_result.ledger_id = 1
                mock_check.return_value = mock_result
                
                # Mock db.fetch to return a single mock local provider
                with patch("python.harness.db.fetch") as mock_fetch:
                    mock_fetch.return_value = [{
                        "id": "mock-local",
                        "type": "local",
                        "name": "Mock Local Engine",
                        "priority": 1,
                        "status": "online",
                        "config": json.dumps({"endpoint": "http://localhost:11434/v1", "model": "llama3.2:3b"}),
                        "locked": False
                    }]
                    
                    # Mock db.log_to_db transaction call since we are inside handle_inference_generate log
                    with patch("python.harness.db.log_to_db") as mock_log:
                        # Mock httpx.AsyncClient.post to capture payload and return success
                        mock_response = MagicMock()
                        mock_response.status_code = 200
                        mock_response.json.return_value = {
                            "choices": [{
                                "message": {
                                    "role": "assistant",
                                    "content": "mock response text"
                                }
                            }]
                        }
                        
                        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                            mock_post.return_value = mock_response
                            
                            resp_str = await handle_inference_generate(params, ws_mock, req_id="test-os-metadata")
                            
                            # Inspect mock_post args to see the messages payload sent to LLM
                            called_args, called_kwargs = mock_post.call_args
                            payload = called_kwargs.get("json", {})
                            messages = payload.get("messages", [])
                            
                            system_msg = next((msg for msg in messages if msg["role"] == "system"), None)
                            
                            assert system_msg is not None
                            expected_prefix = f"[System Info: OS Platform: {platform.system()}]"
                            assert expected_prefix in system_msg["content"]
                            assert "Test system prompt" in system_msg["content"]
