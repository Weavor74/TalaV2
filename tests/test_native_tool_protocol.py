"""
Native Ollama function-calling adapter.

The harness dispatches on its own ``<call:name attr="v" />`` text convention. On
the local Ollama path it now *sends* tool schemas natively instead, because the
small models that fit in 6GB reliably emit native tool_calls but do not reliably
reproduce an XML tag from prose instructions. These tests cover the two halves of
that adapter: the request side must leave exactly one protocol in the prompt, and
the response side must turn tool_calls back into a tag the existing parser reads.
"""
import json
import re
from pathlib import Path

from python.harness.server import (
    _BUILTIN_RESEARCH_TOOL_SPECS,
    _NATIVE_CALL_REMINDER,
    _NATIVE_TOOL_DIRECTIVE,
    _XML_CALL_REMINDER,
    _accumulate_streamed_tool_calls,
    _find_tool_call,
    _ollama_native_tool_specs,
    _swap_xml_protocol_for_native,
    _tool_calls_to_call_tags,
)

# The attribute parser in handle_inference_generate, which is what the converted
# tags are actually consumed by.
_ATTR_RE = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|(\S+))')


def _parse_call_tag(text):
    """Mirror of the tag -> (name, attrs) parsing in handle_inference_generate."""
    match = _find_tool_call(text)
    if not match:
        return None, None
    attr_str = match.group(2)
    if attr_str.endswith("/"):
        attr_str = attr_str[:-1]
    attrs = {}
    for m in _ATTR_RE.finditer(attr_str):
        key = m.group(1)
        v_dq, v_sq, v_uq = m.group(2), m.group(3), m.group(4)
        if v_dq is not None:
            attrs[key] = v_dq.replace('\\"', '"').replace("\\\\", "\\")
        elif v_sq is not None:
            attrs[key] = v_sq.replace("\\'", "'").replace("\\\\", "\\")
        elif v_uq is not None:
            try:
                attrs[key] = json.loads(v_uq.replace("'", '"'))
            except Exception:
                attrs[key] = v_uq
    return match.group(1), attrs


# --- request side: one protocol, not two -----------------------------------


def _messages(system, user):
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def test_swap_removes_the_xml_documentation_from_the_system_prompt():
    xml = "[Agent Capability: Tools]\nUse `<call:web_search query=\"x\" />`.\n"
    messages = _messages(f"You are Tala.\n\n{xml}\nBe concise.", "hello")

    swapped, removed = _swap_xml_protocol_for_native(messages, xml)

    assert removed is True
    assert "<call:" not in swapped[0]["content"]
    assert _NATIVE_TOOL_DIRECTIVE in swapped[0]["content"]
    # Everything that was not the protocol survives untouched.
    assert "You are Tala." in swapped[0]["content"]
    assert "Be concise." in swapped[0]["content"]


def test_swap_matches_even_after_the_prompt_assembler_strips_it():
    """The block is appended to the system prompt and the result is .strip()ed,
    which eats its trailing newline. A verbatim-only match would silently miss,
    and a silent miss means `tools` ships next to the XML instructions -- the
    two-competing-protocols case that made models narrate their confusion
    instead of acting."""
    xml = "[Agent Capability: Tools]\nUse `<call:web_search query=\"x\" />`.\n\n"
    assembled = f"You are Tala.\n\n{xml}".strip()
    assert xml not in assembled  # the exact condition being guarded

    swapped, removed = _swap_xml_protocol_for_native(_messages(assembled, "hi"), xml)

    assert removed is True
    assert "<call:" not in swapped[0]["content"]


def test_swap_reports_failure_when_the_protocol_block_has_drifted():
    """If the documentation can no longer be found, the caller must fail closed
    and keep the XML protocol rather than send both."""
    messages = _messages("You are Tala. Use <call:web_search query=\"x\" /> to search.", "hi")

    _, removed = _swap_xml_protocol_for_native(messages, "a block that is not in the prompt")

    assert removed is False


def test_swap_replaces_the_xml_reminder_appended_to_the_user_message():
    """The tail reminder lives in the *user* message, not the system prompt.
    Left in place it tells a model with no XML documentation to emit an XML tag
    anyway, which produces a hand-written tag with invented syntax."""
    xml = "[Agent Capability: Tools]\ndocs here\n"
    messages = _messages(xml, f"what is the webb telescope status?\n\n{_XML_CALL_REMINDER}")

    swapped, removed = _swap_xml_protocol_for_native(messages, xml)

    assert removed is True
    assert _XML_CALL_REMINDER not in swapped[1]["content"]
    assert _NATIVE_CALL_REMINDER in swapped[1]["content"]
    assert "<call:" not in swapped[1]["content"]
    assert "what is the webb telescope status?" in swapped[1]["content"]


def test_swap_leaves_non_string_content_alone():
    """Vision turns carry a content list of parts, not a string."""
    messages = [
        {"role": "system", "content": "xml block\n"},
        {"role": "user", "content": [{"type": "text", "text": "describe this"}]},
    ]

    swapped, _ = _swap_xml_protocol_for_native(messages, "xml block\n")

    assert swapped[1]["content"] == [{"type": "text", "text": "describe this"}]


def test_every_provider_keeps_a_way_to_call_tools():
    """Native tools go to Ollama, to the OpenAI-shaped /v1 providers, and to the
    embedded model via its chat template. The ones that do not get them --
    anthropic, and any Ollama model whose capabilities omit "tools" -- must keep
    the XML documentation, so the choice is always which mechanism, never
    whether."""
    source = Path("python/harness/server.py").read_text(encoding="utf-8")

    # Each exclusion branch reports itself and falls through without setting tools.
    assert '_tools_verdict = "anthropic endpoint takes a different tools schema"' in source
    assert 'model reports no tool capability' in source
    assert 'keeping the <call:/> text protocol' in source


def test_removing_the_xml_block_is_always_guarded_by_its_own_result():
    """Every caller must act on the `removed` flag. An unguarded call would strip
    the documentation whether or not the replacement landed, which is the one way
    to end up with no tool protocol at all -- worse than either mechanism alone.
    Asserted over call sites rather than a fixed count, so adding a provider does
    not fail this test; forgetting the guard does."""
    source = Path("python/harness/server.py").read_text(encoding="utf-8")

    call_sites = source.count("_swap_xml_protocol_for_native(\n") + source.count(
        "_swap_xml_protocol_for_native("
    ) - source.count("def _swap_xml_protocol_for_native(")
    destructures = source.count("_swapped, _removed = _swap_xml_protocol_for_native")
    guards = source.count("if _removed:") + source.count("if not _removed:")

    assert destructures >= 1
    assert guards >= destructures, "a call site does not check whether the swap happened"
    assert call_sites >= destructures


def test_a_400_with_tools_attached_falls_back_for_any_provider():
    """The retry used to match Ollama's literal "does not support tools" string.
    Now that every /v1 provider gets a tools array and none of them can be probed
    for capability first, matching one provider's wording means the rest fail the
    whole request instead of falling back to XML."""
    source = Path("python/harness/server.py").read_text(encoding="utf-8")

    assert 'if response.status_code == 400 and "tools" in payload:' in source
    assert '"does not support tools" in err_str' not in source
    assert '"does not support tools" in response.text' not in source
    # And the fallback restores the documentation rather than just dropping tools.
    assert '_retry["messages"] = payload_messages' in source


def test_no_think_is_dropped_when_native_tools_are_attached():
    """/no_think is added for qwen3 because reasoning text can bury an XML tag.
    Native tool_calls are a structured field and cannot be buried, so the switch
    loses its justification -- and leaving it on suppresses the thinking the
    renderer shows as an expandable section."""
    source = Path("python/harness/server.py").read_text(encoding="utf-8")

    # The filter lives at the one site where `tools` is attached.
    assert 'm.get("content") == "/no_think"' in source
    attach = source.index('payload["tools"] = native_tool_specs')
    drop = source.index('m.get("content") == "/no_think"')
    assert drop < attach, "/no_think must be filtered out before tools are attached"


# --- request side: schema translation ---------------------------------------


def test_registry_schemas_translate_into_ollama_function_specs():
    dyn_tools = [{
        "name": "web_search",
        "description": "Search the web.",
        "schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "search_provider_id": {"type": "string"}},
            "required": ["query"],
        },
    }]

    specs = _ollama_native_tool_specs(dyn_tools)

    assert specs == [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "search_provider_id": {"type": "string"}},
                "required": ["query"],
            },
        },
    }]


def test_tool_without_a_usable_schema_still_produces_a_valid_spec():
    """Ollama rejects the whole request if any parameters block is not an object
    schema -- one malformed tool must not take every other tool down with it."""
    specs = _ollama_native_tool_specs([
        {"name": "no_schema", "description": "d", "schema": {}},
        {"name": "bad_schema", "description": "d", "schema": {"type": "string"}},
    ])

    assert len(specs) == 2
    for spec in specs:
        assert spec["function"]["parameters"]["type"] == "object"
        assert spec["function"]["parameters"]["properties"] == {}


def test_nameless_tool_is_skipped():
    assert _ollama_native_tool_specs([{"description": "d", "schema": {}}]) == []


def test_research_tools_are_declared_natively():
    """research.search and research.crawl_and_summarize are dispatched through
    the RPC METHODS table, not ToolRegistry, so they are documented only in the
    XML block. Swapping the protocol must not take web search with it."""
    names = {s["function"]["name"] for s in _BUILTIN_RESEARCH_TOOL_SPECS}

    assert names == {"research.search", "research.crawl_and_summarize"}
    crawl = next(
        s for s in _BUILTIN_RESEARCH_TOOL_SPECS
        if s["function"]["name"] == "research.crawl_and_summarize"
    )
    # The handler rejects anything that is not a list, so the schema has to ask
    # for an array rather than a comma-joined string.
    assert crawl["function"]["parameters"]["properties"]["urls"]["type"] == "array"


# --- response side: tool_calls back into <call:/> ---------------------------


def test_string_argument_round_trips_through_the_harness_parser():
    tag = _tool_calls_to_call_tags(
        [{"function": {"name": "web_search", "arguments": {"query": "webb telescope status"}}}]
    )

    assert _parse_call_tag(tag) == ("web_search", {"query": "webb telescope status"})


def test_arguments_given_as_a_json_string_round_trip():
    """Ollama returns arguments as an object; the OpenAI-shaped providers return
    them as a JSON string. Both reach this converter."""
    tag = _tool_calls_to_call_tags(
        [{"function": {"name": "web_search", "arguments": '{"query": "hello"}'}}]
    )

    assert _parse_call_tag(tag) == ("web_search", {"query": "hello"})


def test_quotes_and_backslashes_in_a_value_survive():
    args = {"query": 'the "Artemis" disaster', "path": "D:\\src\\TalaV2\\a.py"}

    tag = _tool_calls_to_call_tags([{"function": {"name": "read_file", "arguments": args}}])

    assert _parse_call_tag(tag) == ("read_file", args)


def test_list_argument_round_trips_as_a_real_list():
    """research.crawl_and_summarize checks isinstance(urls, list) and errors out
    otherwise, so the array has to come back as a list -- and it has to be
    rendered without spaces, because the parser's unquoted branch is \\S+ and
    would truncate at the first space."""
    args = {"urls": ["https://a.example/x", "https://b.example/y"], "subject": "webb"}

    tag = _tool_calls_to_call_tags(
        [{"function": {"name": "research.crawl_and_summarize", "arguments": args}}]
    )
    name, attrs = _parse_call_tag(tag)

    assert name == "research.crawl_and_summarize"
    assert attrs == args
    assert isinstance(attrs["urls"], list)


def test_non_string_scalars_round_trip_with_their_types():
    tag = _tool_calls_to_call_tags(
        [{"function": {"name": "shell_run", "arguments": {"timeout": 30, "capture": True}}}]
    )

    assert _parse_call_tag(tag) == ("shell_run", {"timeout": 30, "capture": True})


def test_first_of_several_calls_is_the_one_dispatched():
    tag = _tool_calls_to_call_tags([
        {"function": {"name": "web_search", "arguments": {"query": "a"}}},
        {"function": {"name": "read_file", "arguments": {"path": "b"}}},
    ])

    assert _parse_call_tag(tag)[0] == "web_search"


# --- response side: streamed tool calls -------------------------------------


def test_streamed_argument_fragments_are_joined_before_conversion():
    """OpenAI-style streaming sends the name once and then `arguments` as string
    fragments. Converting per-delta would emit a tag holding half a JSON object."""
    acc = {}
    for delta in (
        [{"index": 0, "id": "call_1", "function": {"name": "web_search", "arguments": ""}}],
        [{"index": 0, "function": {"arguments": '{"que'}}],
        [{"index": 0, "function": {"arguments": 'ry": "webb '}}],
        [{"index": 0, "function": {"arguments": 'status"}'}}],
    ):
        _accumulate_streamed_tool_calls(delta, acc)

    tag = _tool_calls_to_call_tags([acc[k] for k in sorted(acc)])

    assert _parse_call_tag(tag) == ("web_search", {"query": "webb status"})


def test_streamed_parallel_tool_calls_stay_separate():
    acc = {}
    _accumulate_streamed_tool_calls(
        [{"index": 0, "function": {"name": "web_search", "arguments": '{"query":"a"}'}},
         {"index": 1, "function": {"name": "read_file", "arguments": '{"path":"b"}'}}],
        acc,
    )

    assert len(acc) == 2
    tags = _tool_calls_to_call_tags([acc[k] for k in sorted(acc)]).splitlines()
    assert tags[0].startswith("<call:web_search ")
    assert tags[1].startswith("<call:read_file ")


def test_streamed_arguments_delivered_as_one_object_are_accepted():
    """Not every provider fragments the arguments."""
    acc = {}
    _accumulate_streamed_tool_calls(
        [{"index": 0, "function": {"name": "web_search", "arguments": {"query": "webb"}}}], acc
    )

    tag = _tool_calls_to_call_tags([acc[0]])

    assert _parse_call_tag(tag) == ("web_search", {"query": "webb"})


def test_accumulator_ignores_malformed_deltas():
    acc = {}
    _accumulate_streamed_tool_calls(None, acc)
    _accumulate_streamed_tool_calls("not a list", acc)
    _accumulate_streamed_tool_calls([None, 7, "x"], acc)

    assert acc == {}


def test_empty_or_nameless_tool_calls_produce_no_tag():
    """A bogus '<call: />' would be dispatched as a nonexistent tool and reported
    as a tool failure rather than as no tool call at all."""
    assert _tool_calls_to_call_tags([]) == ""
    assert _tool_calls_to_call_tags([{"function": {}}]) == ""
    assert _tool_calls_to_call_tags([{}]) == ""
