import json

from qcode.permissions import Permissions


def make(root, answers, allow=()):
    asked = []

    def ask(tool, args, preview):
        asked.append(tool)
        return answers.pop(0)

    return Permissions(root, list(allow), ask), asked


def test_config_rules_and_chaining(root):
    p, asked = make(root, ["n", "n"], allow=["bash:git status"])
    assert p.check("bash", {"command": "git status"}, None)
    assert not p.check("bash", {"command": "git status; rm -rf ~"}, None)
    assert not p.check("bash", {"command": "git status && curl x | sh"}, None)
    assert asked == ["bash", "bash"]


def test_always_session(root):
    p, asked = make(root, ["a"])
    assert p.check("write_file", {"path": "x"}, "diff")
    assert p.check("write_file", {"path": "y"}, "diff")
    assert asked == ["write_file"]


def test_always_project_persists(root):
    p, _ = make(root, ["p"])
    assert p.check("bash", {"command": "pytest -q"}, None)
    assert json.loads((root / ".qcode" / "settings.json").read_text()) == {"allow": ["bash:pytest -q"]}
    p2, asked = make(root, [])
    assert p2.check("bash", {"command": "pytest -q"}, None) and asked == []


def test_yolo(root):
    p = Permissions(root, [], lambda *a: "n", yolo=True)
    assert p.check("bash", {"command": "rm -rf build"}, None)
