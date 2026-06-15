from sdk.agent.command_parser import CommandParser


def test_undo_routes_to_named_restore_checkpoint():
    result = CommandParser().parse("/undo")

    assert result.success is True
    assert result.action is not None
    assert result.action.command == "undo"
    assert result.action.routed_to == "repo.restore"
    assert result.action.requires_confirmation is True
    assert result.action.args["checkpoint_name"] == "undo-last-command"


def test_undo_can_restore_explicit_checkpoint_name():
    result = CommandParser().parse("/undo before-doc-generation")

    assert result.success is True
    assert result.action is not None
    assert result.action.args["checkpoint_name"] == "before-doc-generation"


def test_self_test_routes_to_system_self_test_with_audit_enabled():
    result = CommandParser().parse("/self-test")

    assert result.success is True
    assert result.action is not None
    assert result.action.command == "self-test"
    assert result.action.routed_to == "system.self_test"
    assert result.action.args["include_audit"] is True


def test_self_test_can_skip_audit_for_fast_inner_loop():
    result = CommandParser().parse("/self-test --no-audit")

    assert result.success is True
    assert result.action is not None
    assert result.action.args["include_audit"] is False
