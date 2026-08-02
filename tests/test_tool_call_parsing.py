from python.harness.server import _find_tool_call


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
