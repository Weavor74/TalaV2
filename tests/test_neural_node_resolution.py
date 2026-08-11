import pytest
from sdk.agent.tools import read_neural_node, list_neural_nodes, _find_neural_node_path

@pytest.mark.asyncio
async def test_neural_node_path_resolution():
    # 1. Path-style lookup
    res_path = await read_neural_node("sdk/identity/whoami.html")
    assert res_path["status"] == "success"
    assert res_path["node_id"] == "tala-identity-master-v1"

    # 2. Simple ID 'whoami' lookup
    res_whoami = await read_neural_node("whoami")
    assert res_whoami["status"] == "success"
    assert res_whoami["node_id"] == "tala-identity-master-v1"

    # 3. Simple ID 'tala-core-identity' lookup
    res_core = await read_neural_node("tala-core-identity")
    assert res_core["status"] == "success"
    assert res_core["node_id"] == "tala-core-identity"

    # 4. Simple ID lookup by filename stem in nodes/. This used to look up
    # 'skill-tree-of-thought-ideation' — a generated skill node that only
    # ever existed as machine-local state on the machine the test was
    # written on (never tracked in git), so the test broke permanently the
    # day that junk node was pruned. persona-tala-rp.html is tracked and
    # exercises the same bare-id -> nodes/<id>.html resolution path.
    res_tot = await read_neural_node("persona-tala-rp")
    assert res_tot["status"] == "success"

    # 5. data-node-id fallback search
    res_fallback = await read_neural_node("tala-identity-master-v1")
    assert res_fallback["status"] == "success"

@pytest.mark.asyncio
async def test_list_neural_nodes():
    res = await list_neural_nodes()
    assert res["status"] == "success"
    assert res["total_count"] > 0
    node_ids = [n["node_id"] for n in res["nodes"]]
    assert "tala-identity-master-v1" in node_ids
    assert "tala-core-identity" in node_ids
