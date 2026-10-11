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
