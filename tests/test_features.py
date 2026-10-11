"""Tests for /config, /undo, /retry, /permissions, /export, /context and the updater."""

import json

import httpx
import pytest

from qcode import config, updater
from qcode.permissions import Permissions
from test_agent import call, make_agent


# --- config -------------------------------------------------------------------------
def test_set_config_value_replaces_and_inserts(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(config.DEFAULT_TOML)
    config.set_config_value("max_steps", 40, path)
    config.set_config_value("model", "qcoder-7b", path)
    text = path.read_text()
    assert "max_steps = 40\n" in text and 'model = "qcoder-7b"\n' in text
    assert "[models.qcoder-14b]" in text and "# qcode configuration" in text  # rest untouched
    cfg = config.load_config(path)
    assert cfg.max_steps == 40 and cfg.model == "qcoder-7b"

    path.write_text('base_url = "http://x/v1"\n\n[models.a]\nnum_ctx = 1\n')
    config.set_config_value("auto_update", "off", path)
    cfg = config.load_config(path)
    assert cfg.auto_update == "off" and cfg.models == {"a": {"num_ctx": 1}}


@pytest.mark.parametrize("key,raw,ok", [
    ("max_steps", "30", 30),
    ("max_steps", "0", None),
    ("max_steps", "lots", None),
    ("auto_update", "auto", "auto"),
    ("auto_update", "sometimes", None),
    ("model", "qcoder-7b", "qcoder-7b"),
    ("base_url", "x", None),
])
def test_parse_setting(key, raw, ok):
    if ok is None:
        with pytest.raises(ValueError):
            config.parse_setting(key, raw)
    else:
        assert config.parse_setting(key, raw) == ok


# --- undo / retry -------------------------------------------------------------------
def test_undo_restores_edits_and_deletes_new_files(root):
    (root / "a.py").write_text("x = 1\n")
    agent, *_ = make_agent(root, [
        ("", [call("edit_file", path="a.py", old_string="x = 1", new_string="x = 2"),
              call("write_file", path="new.py", content="print(1)\n")]),
        ("done", []),
    ])
    agent.run("change things")
    assert (root / "a.py").read_text() == "x = 2\n" and (root / "new.py").exists()
    cp = agent.undo()
    assert cp.prompt == "change things"
    assert (root / "a.py").read_text() == "x = 1\n" and not (root / "new.py").exists()
    assert agent.undo() is None


def test_undo_keeps_first_version_when_file_edited_twice(root):
    (root / "a.py").write_text("v1\n")
    agent, *_ = make_agent(root, [
        ("", [call("write_file", path="a.py", content="v2\n")]),
        ("", [call("write_file", path="a.py", content="v3\n")]),
        ("ok", []),
    ])
    agent.run("edit twice")
    agent.undo()
    assert (root / "a.py").read_text() == "v1\n"


def test_retry_pops_last_turn(root):
    agent, client, _ = make_agent(root, [("first", []), ("second", []), ("second again", [])])
    agent.run("q1")
    agent.run("q2")
    assert agent.pop_last_turn() == "q2"
    assert [m["content"] for m in agent.messages[1:]] == ["q1", "first"]
    assert agent.last_reply() == "first"


def test_context_breakdown_and_export(root):
    agent, *_ = make_agent(root, [("", [call("list_dir")]), ("All **good**.", [])])
    agent.run("look around")
    rows = dict(agent.context_breakdown())
    assert set(rows) == {"system prompt + memory", "tool definitions", "your messages", "replies", "tool results"}
    assert all(v > 0 for v in rows.values())
    md = agent.export_markdown()
    assert "## You\n\nlook around" in md and "All **good**." in md and "list_dir" in md


# --- permissions --------------------------------------------------------------------
def test_permissions_list_remove_reset(root):
    p = Permissions(root, ["bash:ls"], lambda *a: "p")
    p.check("bash", {"command": "make"}, None)
    p.session_allow.append("write_file")
    assert p.rules() == [("config", "bash:ls"), ("project", "bash:make"), ("session", "write_file")]
    assert p.remove("bash:make")
    assert json.loads((root / ".qcode" / "settings.json").read_text())["allow"] == []
    assert p.reset() == 1 and p.rules() == [("config", "bash:ls")]


# --- updater ------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [("v1.2.3", (1, 2, 3)), ("0.10.0", (0, 10, 0)), ("v2.0.0-rc1", (2, 0, 0))])
def test_parse_version(text, expected):
    assert updater.parse_version(text) == expected


def test_is_newer():
    rel = updater.Release("0.10.0", "v0.10.0", "", "")
    assert updater.is_newer(rel, "0.9.9") and not updater.is_newer(rel, "0.10.0") and not updater.is_newer(None)


def test_fetch_latest_and_cache(monkeypatch, tmp_path):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return httpx.Response(200, json={"tag_name": "v9.9.9", "body": "notes", "html_url": "https://x"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(updater.httpx, "get", fake_get)
    monkeypatch.setattr(updater, "CHECK_FILE", tmp_path / "check.json")
    rel = updater.cached_latest()
    assert rel.version == "9.9.9" and rel.archive_url.endswith("/archive/refs/tags/v9.9.9.zip")
    assert updater.cached_latest().version == "9.9.9"
    assert len(calls) == 1  # second call came from the cache


def test_fetch_latest_offline(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(updater.httpx, "get", boom)
    assert updater.fetch_latest() is None


def test_sessions_and_config_round_trip_non_ascii(root, tmp_path):
    agent, *_ = make_agent(root, [("Ça marche 🎉 — 完成", [])])
    agent.run("héllo wörld ✓")
    agent2, *_ = make_agent(root, [])
    agent2.load_session(agent.session_path)
    assert agent2.messages[-1]["content"] == "Ça marche 🎉 — 完成"
    path = tmp_path / "config.toml"
    config.set_config_value("model", "modèle-✓", path)
    assert config.load_config(path).model == "modèle-✓"
