from __future__ import annotations

import pytest
from pathlib import Path

from python.harness.retrieval import retrieve_relevant_nodes, tokenize, compute_tf

VALID_NODE_1 = """
<!DOCTYPE html>
<html>
<head>
    <meta name="tala-version" content="neural-node/v2">
</head>
<body>
    <article 
        data-node-id="apples_node" 
        data-node-type="concept" 
        data-execution-authority="1.0"
        data-schema-version="neural-node/v2">
        <section id="semantic-memory">
            Apples are a delicious red fruit. They grow on trees and are used in pies.
        </section>
        <script type="application/json" id="node-state">
            {"title": "Apples"}
        </script>
    </article>
</body>
</html>
"""

VALID_NODE_2 = """
<!DOCTYPE html>
<html>
<head>
    <meta name="tala-version" content="neural-node/v2">
</head>
<body>
    <article 
        data-node-id="cars_node" 
        data-node-type="concept" 
        data-execution-authority="1.0"
        data-schema-version="neural-node/v2">
        <section id="semantic-memory">
            Cars are fast vehicles with four wheels. They require gasoline or electricity to drive on roads.
        </section>
        <script type="application/json" id="node-state">
            {"title": "Cars"}
        </script>
    </article>
</body>
</html>
"""

def test_tokenize():
    tokens = tokenize("Hello World! This is a TEST. 123")
    assert "hello" in tokens
    assert "world" in tokens
    assert "test" in tokens
    assert "this" not in tokens # stopword

def test_compute_tf():
    tf = compute_tf(["apple", "apple", "banana"])
    assert tf["apple"] == 2/3
    assert tf["banana"] == 1/3

def test_retrieve_relevant_nodes(tmp_path: Path):
    node1_file = tmp_path / "apples.html"
    node1_file.write_text(VALID_NODE_1, encoding="utf-8")
    
    node2_file = tmp_path / "cars.html"
    node2_file.write_text(VALID_NODE_2, encoding="utf-8")
    
    # Query for apples
    results = retrieve_relevant_nodes("delicious red fruit", tmp_path, top_k=5)
    assert len(results) > 0
    assert results[0][0].node_id == "apples_node"
    
    # Query for cars
    results = retrieve_relevant_nodes("fast vehicles driving", tmp_path, top_k=5)
    assert len(results) > 0
    assert results[0][0].node_id == "cars_node"
    
    # Query for nothing matching
    results = retrieve_relevant_nodes("zxqxwj", tmp_path, top_k=5)
    assert len(results) == 0
