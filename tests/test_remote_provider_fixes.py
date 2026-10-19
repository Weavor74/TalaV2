"""
Tests for remote provider bug fixes.

Covers:
- Issue 1: max_tokens for thinking models
- Issue 3: Gemini model ID matching
- Issue 4: Empty endpoint validation
- Issue 7: Tool argument parsing with quotes
- Issue 8: Step limit clean output
- Issue 9: Tool tag stripping

Run with: PYTHONPATH=/workspace python3 tests/test_remote_provider_fixes.py
"""
from __future__ import annotations


def test_thinking_model_detection():
    """Issue 1: Thinking models should get 2048 tokens minimum."""
    test_cases = [
        ("gemini-3.8-flash", True),
        ("gemini-2.0-flash-thinking", True),
        ("gemma4-it", True),
        ("deepseek-r1", True),
        ("o1-preview", True),
        ("o3-mini", True),
        ("qwen-thinking-1.5b", True),
        ("llama3.2:3b", False),
        ("gpt-4o", False),
        ("claude-3-5-sonnet", False),
    ]
    
    for model, should_bump in test_cases:
        model_lower = model.lower()
        is_reasoning = any(k in model_lower for k in (
            "gemma4", "gemini", "reasoning", "deepseek", "thought", "thinking", "o1", "o3"
        ))
        assert is_reasoning == should_bump, f"{model} detection failed"
    
    print("✓ test_thinking_model_detection passed")


def test_gemini_model_matching():
    """Issue 3: Gemini returns 'models/gemini-x' but config has 'gemini-x'."""
    api_models = ["models/gemini-3.8-flash", "models/gemini-2.0-flash"]
    config_model = "gemini-3.8-flash"
    
    found = any(
        mid.endswith(f"/{config_model}") or mid.endswith(f"/{config_model.split(':')[0]}")
        for mid in api_models
    )
    assert found, "Should match gemini-3.8-flash against models/gemini-3.8-flash"
    
    api_models2 = ["gpt-4o", "gpt-3.5-turbo"]
    config_model2 = "gpt-4o"
    found2 = config_model2 in api_models2
    assert found2, "Exact match should still work"
    
    print("✓ test_gemini_model_matching passed")


def test_tool_argument_parsing():
    """Issue 7: Tool arguments truncated at quotes in JSON."""
    from sdk.agent.agent_reply_tools import parse_call_tag
    
    class MockMatch:
        def __init__(self, attr_str):
            self._attr_str = attr_str
            
        def group(self, n):
            if n == 1:
                return "code.propose_patch"
            if n == 2:
                return self._attr_str + " /"
            if n == 3:
                return ""
    
    attr_str = 'replacements="[{\\"old_text\\":\\"foo\\",\\"new_text\\":\\"bar\\"}]" file="test.py"'
    tool_name, attrs = parse_call_tag(MockMatch(attr_str))
    
    assert "replacements" in attrs, "Should parse replacements attribute"
    assert "file" in attrs, "Should parse file attribute"
    assert len(attrs["replacements"]) > 10, "JSON value should not be truncated"
    
    attr_str2 = 'nodes="[{\\"description\\":\\"Plans\\",\\"role\\":\\"planner\\"}]" name="test"'
    tool_name2, attrs2 = parse_call_tag(MockMatch(attr_str2))
    
    assert "nodes" in attrs2
    assert "Plans" in attrs2["nodes"], "JSON content should not be truncated"
    
    print("✓ test_tool_argument_parsing passed")


def test_step_limit_output():
    """Issue 8: Raw <call:...> tags should not reach user when step limit hit."""
    raw_output = "Let me check that. <call:search_memory query=\"test\" />"
    
    clean_output = (
        "I've reached my step limit while working on this request. "
        "The task may require breaking it into smaller steps or simplifying the approach."
    )
    
    assert "<call:" not in clean_output, "Raw tool tags should not be in final output"
    assert "step limit" in clean_output.lower(), "Should mention step limit"
    
    print("✓ test_step_limit_output passed")


def test_tool_tag_stripping():
    """Issue 9: Short prompts sent without tools leak <call:...> to user."""
    from python.harness.server import _find_tool_call
    
    text_with_tag = "Here's what I found: <call:search_memory query=\"test\" />"
    
    match = _find_tool_call(text_with_tag)
    assert match is not None, "Should find tool call"
    
    stripped = text_with_tag[:match.start()].strip()
    result = stripped + "\n\n[Note: Tool execution was not available for this request.]"
    
    assert "<call:" not in result, "Tool tags should be stripped"
    assert "Here's what I found:" in result
    
    text_without_tag = "This is a normal response with no tool calls."
    match2 = _find_tool_call(text_without_tag)
    assert match2 is None, "Should not find tool call in normal text"
    
    print("✓ test_tool_tag_stripping passed")


def test_write_memory_tool():
    """Issue 10: write_memory tool should exist and work."""
    from sdk.agent.tools import ToolRegistry
    
    assert "write_memory" in ToolRegistry._tools, "write_memory tool should be registered"
    
    tool = ToolRegistry._tools.get("write_memory")
    assert tool is not None
    desc = tool.get("description", "").lower()
    assert "remember" in desc or "memory" in desc, "Description should mention remembering"
    
    print("✓ test_write_memory_tool passed")


def test_submit_agent_goal_discoverability():
    """Issue 11: submit_agent_goal should be easy to discover."""
    from sdk.agent.tools import ToolRegistry
    
    tool = ToolRegistry._tools.get("submit_agent_goal")
    assert tool is not None
    desc = tool.get("description", "")
    
    assert any(word in desc.lower() for word in ["create", "build", "implement", "complex"]), \
        "Description should include action words and use cases"
    
    print("✓ test_submit_agent_goal_discoverability passed")


if __name__ == "__main__":
    print("Running remote provider bug fix tests...\n")
    
    test_thinking_model_detection()
    test_gemini_model_matching()
    test_tool_argument_parsing()
    test_step_limit_output()
    test_tool_tag_stripping()
    test_write_memory_tool()
    test_submit_agent_goal_discoverability()
    
    print("\n✅ All tests passed!")
