from __future__ import annotations

import pytest
from pathlib import Path

from sdk.agent.parser import parse_html, parse_file, NeuralNode, InvalidNodeSchemaError

VALID_V2_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="tala-version" content="neural-node/v2">
</head>
<body>
    <article 
        data-node-id="test_node_01" 
        data-node-type="test-type" 
        data-execution-authority="0.8"
        data-user-approval-strength="0.9"
        data-schema-version="neural-node/v2">
        <section id="semantic-memory">
            This is a valid V2 node.
        </section>
        <a href="other.html" data-relationship-type="depends-on" data-relationship-weight="0.7">Other</a>
        <script type="application/json" id="node-state">
            {"key": "value"}
        </script>
    </article>
</body>
</html>
"""

INVALID_NO_AUTHORITY_HTML = """
<!DOCTYPE html>
<html>
<body>
    <article data-node-id="test_node_02" data-node-type="test-type">
        <section id="semantic-memory">Missing authority</section>
    </article>
</body>
</html>
"""

def test_valid_v2_node_parsing():
    node = parse_html(VALID_V2_HTML)
    assert isinstance(node, NeuralNode)
    assert node.node_id == "test_node_01"
    assert node.node_type == "test-type"
    assert node.execution_authority == 0.8
    assert node.user_approval_strength == 0.9
    assert node.schema_version == "neural-node/v2"
    assert "valid V2 node" in node.semantic_memory
    assert len(node.relationships) == 1
    assert node.relationships[0].relationship_type == "depends-on"
    assert node.relationships[0].relationship_weight == 0.7
    assert node.state_data == {"key": "value"}

def test_reject_legacy_schemas():
    with pytest.raises(InvalidNodeSchemaError, match="missing data-execution-authority"):
        parse_html(INVALID_NO_AUTHORITY_HTML)
        
def test_active_node_sweep(tmp_path: Path):
    valid_file = tmp_path / "valid.html"
    invalid_file = tmp_path / "invalid.html"
    
    valid_file.write_text(VALID_V2_HTML, encoding="utf-8")
    invalid_file.write_text(INVALID_NO_AUTHORITY_HTML, encoding="utf-8")
    
    # Active node sweep simulating parsing a directory
    parsed_nodes = []
    for f in tmp_path.glob("*.html"):
        try:
            node = parse_file(str(f))
            parsed_nodes.append(node)
        except InvalidNodeSchemaError:
            pass
            
    assert len(parsed_nodes) == 1
    assert parsed_nodes[0].node_id == "test_node_01"
