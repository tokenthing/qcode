import os

import pytest

from qcode.tools import ALL_TOOLS, ToolContext, ToolError


def run(root, name, **args):
    return ALL_TOOLS[name].run(ToolContext(root), args)


def preview(root, name, **args):
    return ALL_TOOLS[name].preview(ToolContext(root), args)


def test_write_then_read(root):
    assert "Created" in run(root, "write_file", path="a/b.py", content="x = 1\ny = 2\n")
    out = run(root, "read_file", path="a/b.py")
    assert "    1\tx = 1" in out and "    2\ty = 2" in out


def test_read_pages_large_files(root):
    (root / "big.txt").write_text("\n".join(f"line {i}" for i in range(1, 501)))
    out = run(root, "read_file", path="big.txt", offset=100, limit=10)
    assert "  100\tline 100" in out and "line 110" not in out
    assert "showing lines 100-109 of 500" in out


def test_paths_are_confined_to_root(root):
    with pytest.raises(ToolError, match="outside the project root"):
        run(root, "read_file", path="../secret.txt")
    with pytest.raises(ToolError, match="outside the project root"):
        run(root, "write_file", path="/etc/passwd", content="")


def test_edit_requires_unique_match(root):
    (root / "f.py").write_text("a = 1\na = 1\n")
    with pytest.raises(ToolError, match="matches 2 places"):
        run(root, "edit_file", path="f.py", old_string="a = 1", new_string="a = 2")
    run(root, "edit_file", path="f.py", old_string="a = 1", new_string="a = 2", replace_all=True)
    assert (root / "f.py").read_text() == "a = 2\na = 2\n"


def test_edit_missing_snippet(root):
    (root / "f.py").write_text("hello\n")
    with pytest.raises(ToolError, match="not found"):
        run(root, "edit_file", path="f.py", old_string="bye", new_string="x")


def test_edit_preview_is_a_diff(root):
    (root / "f.py").write_text("def f():\n    return 1\n")
    diff = preview(root, "edit_file", path="f.py", old_string="return 1", new_string="return 2")
    assert "-    return 1" in diff and "+    return 2" in diff
    assert (root / "f.py").read_text() == "def f():\n    return 1\n"  # preview doesn't write


def test_search_tools(root):
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("def hello():\n    pass\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.py").write_text("def hello(): pass\n")
    assert "src/" in run(root, "list_dir")
    assert run(root, "glob", pattern="*.py") == "src/main.py"
    out = run(root, "grep", pattern=r"def hello")
    assert "src/main.py:1:" in out and "node_modules" not in out


def test_bash(root):
    out = run(root, "bash", command="echo hi && " + ("cd" if os.name == "nt" else "pwd"))
    assert out.startswith("exit code 0") and "hi" in out and str(root) in out
    assert run(root, "bash", command="exit 3").startswith("exit code 3")


def test_normalize_args_fixes_common_wrong_names():
    from qcode.tools import normalize_args
    write = ALL_TOOLS["write_file"]
    assert normalize_args(write, {"file_path": "a", "new_string": "x"}) == {"path": "a", "content": "x"}
    edit = ALL_TOOLS["edit_file"]
    # new_string is a real edit_file parameter, so it must not be renamed there
    assert normalize_args(edit, {"filename": "a", "old_str": "x", "new_string": "y"}) == {"path": "a", "old_string": "x", "new_string": "y"}
    assert normalize_args(ALL_TOOLS["bash"], {"cmd": "ls"}) == {"command": "ls"}


def test_edit_missing_file_points_to_write_file(root):
    with pytest.raises(ToolError, match="call write_file"):
        run(root, "edit_file", path="new.html", old_string="", new_string="<html>")


def test_clean_file_content_fixes_small_model_mistakes():
    from qcode.tools.registry import clean_file_content
    # exactly what qwen3 wrote for the Pluse prototype: fenced, unclosed, with a JSON tail
    mangled = '```python\n# Pluse CLI Prototype\n\nhabits = []\nprint("ok")\n"}'
    assert clean_file_content("src/main.py", mangled) == '# Pluse CLI Prototype\n\nhabits = []\nprint("ok")'
    assert clean_file_content("a.py", "```py\nx = 1\n```\n") == "x = 1\n"
    # double-escaped: one long line full of literal \n
    assert clean_file_content("a.py", 'def main():\\n    print(\\"hi\\")\\n\\nmain()') == 'def main():\n    print("hi")\n\nmain()'
    # left alone: normal code, Markdown that starts with a fence, code containing real \n escapes
    normal = 'print("a\\nb")\nx = "}"\n'
    assert clean_file_content("a.py", normal) == normal
    md = "```bash\nls\n```\n\nMore docs\n"
    assert clean_file_content("README.md", md) == md


def test_write_file_tool_gets_cleaned_content():
    from qcode.tools import normalize_args
    args = normalize_args(ALL_TOOLS["write_file"], {"path": "x.py", "content": "```python\nx = 1\n```"})
    assert args["content"] == "x = 1"


def test_broken_json_is_reported_right_away(root):
    out = run(root, "write_file", path="settings.json", content='{"debug": true "version": "2.0"}')
    assert "no longer valid JSON" in out
    assert "WARNING" not in run(root, "write_file", path="ok.json", content='{"debug": true}')
    out = run(root, "edit_file", path="ok.json", old_string="true}", new_string="true,}")
    assert "no longer valid JSON" in out


def test_edit_forgives_wrong_indentation(root):
    (root / "settings.json").write_text('{\n  "name": "demo",\n  "debug": false,\n  "retries": 3\n}\n')
    # exactly what qwen3:14b kept sending: 4 spaces where the file has 2
    run(root, "edit_file", path="settings.json", old_string='    "debug": false,\n',
        new_string='    "debug": true,\n    "version": "2.0",\n')
    assert (root / "settings.json").read_text() == '{\n  "name": "demo",\n  "debug": true,\n  "version": "2.0",\n  "retries": 3\n}\n'


def test_indent_fallback_needs_a_unique_match_and_hints_otherwise(root):
    (root / "a.py").write_text("if x:\n    y = 1\nif z:\n    y = 1\n")
    with pytest.raises(ToolError, match="not found"):
        run(root, "edit_file", path="a.py", old_string="\ty = 1", new_string="\ty = 2")   # two places: refuse to guess
    with pytest.raises(ToolError, match="Closest line is 2"):
        run(root, "edit_file", path="a.py", old_string="y = 11", new_string="y = 2")
