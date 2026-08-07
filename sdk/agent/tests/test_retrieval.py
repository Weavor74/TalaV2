"""
test_retrieval.py - Auto-generated documentation base.

test_tokenize/test_compute_tf were removed: commit f4384c4 replaced the old
TF-IDF-based retrieval implementation (tokenize()/compute_tf() functions)
with ChromaDB vector similarity search (index_nodes()/retrieve_relevant_nodes()),
but this test file was never updated -- the import below broke, permanently
blocking collection of this entire file (and everything after it in the
same pytest run) since neither function exists in python.harness.retrieval
anymore. retrieve_relevant_nodes() itself is still current and covered below.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from python.harness.retrieval import retrieve_relevant_nodes

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

def test_retrieve_relevant_nodes(tmp_path: Path):
    # Filenames must match each node's data-node-id: retrieve_relevant_nodes
    # queries ChromaDB by node_id, then reloads the NeuralNode from disk via
    # nodes_dir / f"{node_id}.html" -- a mismatched filename (this used to be
    # "apples.html"/"cars.html" against ids "apples_node"/"cars_node") means
    # that reload silently fails to find the file and the match gets
    # dropped, even though ChromaDB itself found and scored it correctly.
    node1_file = tmp_path / "apples_node.html"
    node1_file.write_text(VALID_NODE_1, encoding="utf-8")

    node2_file = tmp_path / "cars_node.html"
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
