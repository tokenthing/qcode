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
