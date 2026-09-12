from unittest.mock import MagicMock
import python.harness.retrieval as ret

def test_incremental_node_indexing(tmp_path):
    # Setup test nodes with valid schema
    n1 = tmp_path / "n1.html"
    n1.write_text("""<!DOCTYPE html><html><body><article data-node-id='n1' data-node-type='test' data-schema-version='neural-node/v2' data-execution-authority='1.00' data-memory-authority='1.00' data-weight-relevance='1.00' data-weight-confidence='1.00' data-chaincode-id='123' data-content-hash='456'><header><h1 id='node-title'>N1</h1></header><section id='semantic-memory'>Test memory</section><script type='application/json' id='node-state'>{}</script><script type='application/json' id='adapter-packet'>{}</script></article></body></html>""", encoding="utf-8")
    
    mock_coll = MagicMock()
    ret.node_collection = mock_coll
    ret._INDEXED_MTIMES.clear()

    # Pass 1: Should parse and upsert 1 node
    ret.index_nodes(tmp_path)
    assert mock_coll.upsert.call_count == 1
    assert len(ret._INDEXED_MTIMES) == 1

    # Pass 2: No files modified, should NOT call upsert again (0s overhead)
    mock_coll.reset_mock()
    ret.index_nodes(tmp_path)
    assert mock_coll.upsert.call_count == 0
