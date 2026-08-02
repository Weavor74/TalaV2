import pytest

from python.harness.capabilities import get_capability_manifest


def test_manifest_shape():
    manifest = get_capability_manifest()
    assert "generated_at" in manifest
    assert isinstance(manifest["rpc_methods"], list)
    assert isinstance(manifest["agent_tools"], list)
    assert isinstance(manifest["categories"], dict)
    assert len(manifest["rpc_methods"]) > 100


def test_manifest_contains_known_methods():
    manifest = get_capability_manifest()
    methods = {m["method"] for m in manifest["rpc_methods"]}
    assert "harness.health" in methods
    assert "workflow.generate_from_tot" in methods
    assert "system.capabilities" in methods


def test_no_duplicate_methods():
    manifest = get_capability_manifest()
    methods = [m["method"] for m in manifest["rpc_methods"]]
    assert len(methods) == len(set(methods))


def test_categories_match_method_prefixes():
    manifest = get_capability_manifest()
    for method_entry in manifest["rpc_methods"]:
        method = method_entry["method"]
        category = method_entry["category"]
        assert method.split(".", 1)[0] == category
        assert method in manifest["categories"][category]


def test_agent_tools_have_expected_keys():
    manifest = get_capability_manifest()
    assert manifest["agent_tools"], "expected at least one registered agent tool"
    for tool in manifest["agent_tools"]:
        assert "name" in tool
        assert "description" in tool
        assert "schema" in tool
