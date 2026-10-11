from qcode.toolparse import parse_text_tool_calls, strip_tool_markup

KNOWN = {"read_file", "bash"}


def test_tagged_call():
    text = 'Let me look.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'
    calls = parse_text_tool_calls(text, KNOWN)
    assert [(c.name, c.arguments) for c in calls] == [("read_file", {"path": "a.py"})]


def test_fenced_json_and_string_arguments():
    text = '```json\n{"name": "bash", "arguments": "{\\"command\\": \\"ls\\"}"}\n```'
    assert parse_text_tool_calls(text, KNOWN)[0].arguments == {"command": "ls"}


def test_bare_json_and_openai_shape():
    text = '{"function": {"name": "bash", "parameters": {"command": "pwd"}}}'
    assert parse_text_tool_calls(text, KNOWN)[0].name == "bash"


def test_ignores_unknown_tools_and_plain_code():
    assert parse_text_tool_calls('```json\n{"name": "rm_rf", "arguments": {}}\n```', KNOWN) == []
    assert parse_text_tool_calls("```python\nprint({'a': 1})\n```", KNOWN) == []
    assert parse_text_tool_calls("just text", KNOWN) == []


def test_strip_markup_hides_partial_blocks():
    assert strip_tool_markup('Hi <tool_call>{"name": "x"}</tool_call>') == "Hi"
    assert strip_tool_markup('Hi <tool_call>{"name": "rea') == "Hi"


def test_bare_json_is_hidden_while_streaming():
    assert strip_tool_markup('{"name": "bash", "argu') == ""
    assert strip_tool_markup('```json\n{"name": "bash"') == ""
    assert strip_tool_markup('Here is {"a": 1}') == 'Here is {"a": 1}'
    for partial in ("{", '{\n  "na', "```", "```js", '```json\n{"'):
        assert strip_tool_markup(partial) == "", partial
    assert strip_tool_markup("Sure") == "Sure"


def test_extract_keeps_surrounding_prose():
    from qcode.toolparse import extract_text_tool_calls
    calls, rest = extract_text_tool_calls('Checking.\n```json\n{"name": "bash", "arguments": {"command": "ls"}}\n```', KNOWN)
    assert calls[0].name == "bash" and rest == "Checking."


def test_call_after_prose_like_llama31():
    from qcode.toolparse import extract_text_tool_calls
    text = ('The file does not exist. I\'ll create it instead with the provided HTML code.\n\n'
            '{"name": "write_file", "parameters": {"new_string":"\\u003c!DOCTYPE html\\u003e\\u003chtml\\u003e\\u003c/html\\u003e",'
            '"path":"/tmp/x.html"}}')
    calls, rest = extract_text_tool_calls(text, {"write_file"})
    assert calls[0].name == "write_file" and calls[0].arguments["new_string"].startswith("<!DOCTYPE html>")
    assert rest == "The file does not exist. I'll create it instead with the provided HTML code."
    assert strip_tool_markup(text) == rest


def test_several_calls_and_prose_between():
    from qcode.toolparse import extract_text_tool_calls
    text = 'First {"name": "bash", "arguments": {"command": "ls"}} then {"name": "read_file", "arguments": {"path": "a"}} done'
    calls, rest = extract_text_tool_calls(text, KNOWN)
    assert [c.name for c in calls] == ["bash", "read_file"] and rest == "First  then  done"


def test_streaming_call_after_prose_is_hidden_but_ordinary_json_is_not():
    assert strip_tool_markup('Creating it now. {"name": "write_file", "parameters": {"path": "a.ht') == "Creating it now."
    assert strip_tool_markup('Sure.\n```json\n{"name": "bash", "arguments": {"command": "ls"}}\n```') == "Sure."
    pkg = 'Your package.json:\n```json\n{"name": "my-app", "version": "1.0.0"}\n```'
    assert strip_tool_markup(pkg) == pkg
    assert parse_text_tool_calls(pkg, KNOWN) == []


def test_raw_newlines_inside_json_strings_are_accepted():
    text = '{"name": "write_file", "parameters": {"path": "a.html", "content": "<html>\n  <body></body>\n</html>"}}'
    calls = parse_text_tool_calls(text, {"write_file"})
    assert calls and calls[0].arguments["content"] == "<html>\n  <body></body>\n</html>"


def test_broken_call_error():
    from qcode.toolparse import broken_call_error
    assert "invalid" in broken_call_error('{"name": "write_file", "parameters": {"path": "a", "content": "x}', {"write_file"})
    assert "no tool named 'make_file'" in broken_call_error('{"name": "make_file", "arguments": {}}', {"write_file"})
    assert broken_call_error("all good, no calls here", {"write_file"}) is None
