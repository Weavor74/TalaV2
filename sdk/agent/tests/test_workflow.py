"""
test_workflow.py - Auto-generated documentation base.
"""

from __future__ import annotations

import json
from sdk.agent.workflow_compiler import compile_workflow_to_html

def test_compile_workflow_to_html():
    workflow_json = {
        "flow_id": "test_123",
        "name": "My Test Workflow",
        "description": "Tests workflow compilation.",
        "tags": ["test", "compile"],
        "nodes": [
            {
                "id": "node_1",
                "type": "command",
                "label": "Run Command",
                "connections": {"onSuccess": "node_2"}
            }
        ]
    }
    
    html = compile_workflow_to_html(workflow_json)
    
    # Assert Neural Node meta tags are present
    assert '<meta name="tala-schema" content="neural-node/v1">' in html
    assert '<meta name="tala-node-id" content="flow-test_123">' in html
    assert '<meta name="tala-node-type" content="workflow">' in html
    assert '<meta name="tala-authority" content="1.00">' in html
    
    # Assert content
    assert "My Test Workflow" in html
    assert "Run Command" in html
    
    # Assert JSON state block is present and parses
    start_idx = html.find('<script type="application/json" id="node-state">')
    assert start_idx != -1
    
    # Verify we can extract and parse the JSON block back
    # (Just a basic string containment since actual HTML parsing is tested in test_parser)
    assert '"flow_id": "test_123"' in html
