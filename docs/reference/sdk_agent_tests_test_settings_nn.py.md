# File: `test_settings_nn.py`

## Functions

### `async def test_save_agent_persona_node(mock_mkdir, mock_write_text, mock_anchor)`
*No docstring provided.*

## Source Code

```python
from __future__ import annotations

import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock

from python.harness.settings_nn import save_agent_persona_node

@pytest.mark.asyncio
@patch("python.harness.settings_nn.canonicalize_and_anchor")
@patch("pathlib.Path.write_text")
@patch("pathlib.Path.mkdir")
async def test_save_agent_persona_node(mock_mkdir, mock_write_text, mock_anchor):
    # Mock anchor return
    mock_anchor.return_value = ("fake_hash", "fake_chaincode", "pending", "none", "key")
    
    config = {
        "system_prompt": "You are a test agent.",
        "tag": "test_tag"
    }
    
    await save_agent_persona_node(
        agent_id="agent_123",
        name="Test Persona",
        role="tester",
        config=config
    )
    
    # Assert file writing
    assert mock_write_text.call_count == 1
    
    html_content = mock_write_text.call_args_list[0][0][0]
    
    # Verify strict neural-node v2 attributes
    assert 'data-node-id="agent_123"' in html_content
    assert 'data-node-type="persona"' in html_content
    assert 'data-schema-version="neural-node/v2"' in html_content
    assert 'data-execution-authority="0.50"' in html_content
    
    # Verify semantic memory contains prompt
    assert "You are a test agent." in html_content
    
    # Verify state data
    state_idx = html_content.find('<script type="application/json" id="node-state"')
    assert state_idx != -1
    assert '"role": "tester"' in html_content
    assert '"title": "Test Persona"' in html_content
    
    # Verify provenance block wrapper hash replacement
    prov_idx = html_content.find('<script type="application/json" id="node-provenance"')
    assert prov_idx != -1
    assert '"wrapper_hash": "sha256:' in html_content
    assert '"wrapper_hash": "sha256:..."' not in html_content

```
