import pytest
from unittest.mock import patch, MagicMock
from python.harness.server import _peer_can_resolve_node_reference

@pytest.mark.asyncio
async def test_firewall_access_granted_sufficient_clearance_and_matching_category():
    """
    NAME
           test_firewall_access_granted_sufficient_clearance_and_matching_category - verifies access is granted under valid conditions
           
    SYNOPSIS
           test_firewall_access_granted_sufficient_clearance_and_matching_category() -> None
           
    DESCRIPTION
           Mocks a peer contact with clearance 5 and allowed categories ["rp", "coding"].
           Mocks a node requiring clearance 3 and having tag "coding".
           Asserts that _peer_can_resolve_node_reference returns True.
    """
    contact = {
        "wallet_address": "0x123456",
        "permissions": {"nodes": True}
    }
    
    mock_rights = {
        "clearance_level": 5,
        "allowed_categories": ["rp", "coding"]
    }
    
    mock_node = MagicMock()
    mock_node.node_type = "documentation"
    mock_node.state_data = {
        "clearance": 3,
        "tags": ["coding", "other"]
    }
    
    with patch("python.harness.server._get_firewall_contact_access_rights", return_value=mock_rights), \
         patch("python.harness.server._resolve_local_html_nn_reference", return_value={"source_path": "mock.html"}), \
         patch("sdk.agent.parser.parse_file", return_value=mock_node):
         
        allowed, reason = await _peer_can_resolve_node_reference(contact, {"node_name"})
        assert allowed is True
        assert reason is None

@pytest.mark.asyncio
async def test_firewall_access_denied_insufficient_clearance():
    """
    NAME
           test_firewall_access_denied_insufficient_clearance - verifies access is denied when clearance is too low
           
    SYNOPSIS
           test_firewall_access_denied_insufficient_clearance() -> None
           
    DESCRIPTION
           Mocks a peer contact with clearance 2 and allowed categories ["rp"].
           Mocks a node requiring clearance 4.
           Asserts that access is denied with a clearance error.
    """
    contact = {
        "wallet_address": "0x123456",
        "permissions": {"nodes": True}
    }
    
    mock_rights = {
        "clearance_level": 2,
        "allowed_categories": ["rp"]
    }
    
    mock_node = MagicMock()
    mock_node.node_type = "documentation"
    mock_node.state_data = {
        "clearance": 4,
        "tags": ["rp"]
    }
    
    with patch("python.harness.server._get_firewall_contact_access_rights", return_value=mock_rights), \
         patch("python.harness.server._resolve_local_html_nn_reference", return_value={"source_path": "mock.html"}), \
         patch("sdk.agent.parser.parse_file", return_value=mock_node):
         
        allowed, reason = await _peer_can_resolve_node_reference(contact, {"node_name"})
        assert allowed is False
        assert "clearance_level_insufficient" in reason

@pytest.mark.asyncio
async def test_firewall_access_denied_category_mismatch():
    """
    NAME
           test_firewall_access_denied_category_mismatch - verifies access is denied when node categories do not match allowed peer categories
           
    SYNOPSIS
           test_firewall_access_denied_category_mismatch() -> None
           
    DESCRIPTION
           Mocks a peer contact with clearance 5 and allowed categories ["rp"].
           Mocks a node requiring clearance 3 and having tags ["workflows"].
           Asserts that access is denied with a category error.
    """
    contact = {
        "wallet_address": "0x123456",
        "permissions": {"nodes": True}
    }
    
    mock_rights = {
        "clearance_level": 5,
        "allowed_categories": ["rp"]
    }
    
    mock_node = MagicMock()
    mock_node.node_type = "documentation"
    mock_node.state_data = {
        "clearance": 3,
        "tags": ["workflows"]
    }
    
    with patch("python.harness.server._get_firewall_contact_access_rights", return_value=mock_rights), \
         patch("python.harness.server._resolve_local_html_nn_reference", return_value={"source_path": "mock.html"}), \
         patch("sdk.agent.parser.parse_file", return_value=mock_node):
         
        allowed, reason = await _peer_can_resolve_node_reference(contact, {"node_name"})
        assert allowed is False
        assert "category_access_denied" in reason

@pytest.mark.asyncio
async def test_firewall_access_granted_wildcard():
    """
    NAME
           test_firewall_access_granted_wildcard - verifies wildcard allows access to any node
           
    SYNOPSIS
           test_firewall_access_granted_wildcard() -> None
           
    DESCRIPTION
           Mocks a peer contact with wildcard "*" category access.
           Asserts that access is granted to any node name.
    """
    contact = {
        "wallet_address": "0x123456",
        "permissions": {"nodes": True}
    }
    
    mock_rights = {
        "clearance_level": 1,
        "allowed_categories": ["*"]
    }
    
    with patch("python.harness.server._get_firewall_contact_access_rights", return_value=mock_rights):
        allowed, reason = await _peer_can_resolve_node_reference(contact, {"secret_node"})
        assert allowed is True
        assert reason is None
