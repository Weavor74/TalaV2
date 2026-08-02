from python.harness.server import _find_tool_call, _normalize_tool_call_format, _close_unclosed_thinking


def test_ignores_call_tag_quoted_inside_closed_thinking_block():
    """Regression test for a real production failure: a reasoning model
    quoted '<call:system.capabilities />' as an example while explaining
    its own plan inside <thinking>, then emitted the real call afterward.
    An unanchored search matched the quoted example first (tool name
    captured as a stray '...' inside the reasoning text), swallowing the
    real call as bogus arguments to a nonexistent tool."""
    text = (
        "<thinking>\n"
        "The syntax given: `<call:system.capabilities  />`\n"
        "So I should output that call tag...\n"
        "</thinking>\n\n"
        "<call:system.capabilities  />"
    )
    match = _find_tool_call(text)
    assert match is not None
    assert match.group(1) == "system.capabilities"


def test_finds_call_with_no_thinking_block():
    match = _find_tool_call('sure, one sec <call:harness.health />')
    assert match is not None
    assert match.group(1) == "harness.health"


def test_returns_none_when_no_call_present():
    assert _find_tool_call("just a normal reply, no tags here") is None


def test_match_offsets_are_relative_to_original_text():
    """Callers slice generated_text[:tool_match.end()] using the returned
    match's offsets -- they must stay valid against the original string,
    not some internally-stripped copy."""
    text = "<thinking>ignore <call:fake.one /> here</thinking>\n\n<call:real.two />"
    match = _find_tool_call(text)
    assert match is not None
    assert match.group(1) == "real.two"
    assert text[:match.end()].endswith("<call:real.two />")


# --- _normalize_tool_call_format -------------------------------------------

def test_normalizes_call_colon_format():
    result = _normalize_tool_call_format('CALL:search_workspace_files(query="foo")')
    assert result == '<call:search_workspace_files query="foo" />'


def test_normalizes_call_colon_wrapped_in_xml_fence():
    result = _normalize_tool_call_format('```xml\nCALL:tool.name(key="val")\n```')
    assert result == '<call:tool.name key="val" />'


def test_normalizes_call_colon_wrapped_in_backticks():
    result = _normalize_tool_call_format('`CALL:tool.name(key="val")`')
    assert result == '<call:tool.name key="val" />'


def test_normalizes_allcaps_fallback_format():
    result = _normalize_tool_call_format('SEARCH_WORKSPACE_FILES(query="val")')
    assert result == '<call:search_workspace_files query="val" />'


def test_leaves_plain_text_unchanged():
    text = "no call here at all"
    assert _normalize_tool_call_format(text) == text


def test_preserves_surrounding_text():
    result = _normalize_tool_call_format('Sure, let me check.\nCALL:harness.health()')
    assert result == 'Sure, let me check.\n\n<call:harness.health  />'


def test_ignores_allcaps_pattern_quoted_inside_closed_thinking_block():
    """Same vulnerability class as _find_tool_call: the ALL-CAPS(...)
    fallback is broad enough to match a model narrating the format it's
    about to use, or incidental text like TODO(fix this), while still
    inside <thinking>. Must not be mistaken for a real invocation."""
    text = (
        '<thinking>I will use the SEARCH_WORKSPACE_FILES(query="x") format next.'
        '</thinking>\n\nJust a normal reply.'
    )
    assert _normalize_tool_call_format(text) == text


def test_ignores_call_colon_pattern_quoted_inside_closed_thinking_block():
    text = (
        '<thinking>The docs say to use CALL:tool.name(key="val") for this.'
        '</thinking>\n\nHere is my answer without calling anything.'
    )
    assert _normalize_tool_call_format(text) == text


def test_still_normalizes_real_call_after_closed_thinking_block():
    text = (
        '<thinking>I should search for this.</thinking>\n\n'
        'CALL:search_workspace_files(query="foo")'
    )
    result = _normalize_tool_call_format(text)
    assert '<call:search_workspace_files query="foo" />' in result


# --- _close_unclosed_thinking -----------------------------------------------

def test_closes_an_unclosed_thinking_block():
    """Regression test for a real, live production bug caught by the
    end-to-end adversarial conversation fixture: a model opened <thinking>
    and never transitioned to a real answer. The raw RPC response text
    (not just the renderer's display layer) must never leave an unclosed
    tag -- every consumer of inference.generate's `text` field needs the
    <thinking>...</thinking> convention to hold, not just the Electron UI."""
    text = "<thinking>\nreasoning that never finishes"
    result = _close_unclosed_thinking(text)
    assert result == "<thinking>\nreasoning that never finishes\n</thinking>"


def test_leaves_a_closed_thinking_block_untouched():
    text = "<thinking>reasoning</thinking>\n\nHere is my answer."
    assert _close_unclosed_thinking(text) == text


def test_leaves_text_without_thinking_untouched():
    text = "just a normal reply, no tags here"
    assert _close_unclosed_thinking(text) == text
