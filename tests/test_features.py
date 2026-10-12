"""Tests for /config, /undo, /retry, /permissions, /export, /context and the updater."""

import json
import re

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
    assert re.search(r"(?m)^max_steps = 40 +#", text) and re.search(r'(?m)^model = "qcoder-7b" +#', text)
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
    ("compact_at", "99", None),
    ("auto_continue", "off", False),
    ("default_mode", "yolo", "auto"),
    ("temperature", "0.2", 0.2),
    ("temperature", "default", "default"),
    ("temperature", "3", None),
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


# --- context size -------------------------------------------------------------------
class _CtxClient:
    base_url = "http://ollama/v1"

    def __init__(self, info):
        self.info = info

    def context_info(self, model):
        return self.info


@pytest.mark.parametrize("info,models,expected", [
    (("model", 14336), {}, 14336),                           # num_ctx baked into the model
    (("loaded", 8192), {"m": {"num_ctx": 32768}}, 8192),     # what's actually running beats config
    (("ollama-default", 4096), {}, 4096),                    # unconfigured Ollama model: 4096, not a guess
    (("ollama-default", 4096), {"m": {"num_ctx": 16000}}, 16000),  # user config beats the default
    (("", None), {}, config.DEFAULT_NUM_CTX),                 # non-Ollama server, nothing known
])
def test_context_for_sources(root, info, models, expected):
    from qcode.client import ContextInfo

    agent, *_ = make_agent(root, [])
    agent.config.models = models
    agent.client = _CtxClient(ContextInfo(info[1], 32768, info[0]))
    assert agent.context_for("m") == expected


# --- v0.4: settings, modes, safety --------------------------------------------------
def test_old_config_files_get_defaults_and_bad_values_are_ignored(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('model = "m"\ncompact_at = 500\ndefault_mode = "banana"\nauto_continue = false\n')
    cfg = config.load_config(path)
    assert cfg.compact_at == 80 and cfg.default_mode == "ask" and cfg.auto_continue is False and cfg.read_before_write


def test_set_model_context_adds_and_updates_tables(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(config.DEFAULT_TOML)
    config.set_model_context("qwen2.5:1.5b", 32768, path)
    config.set_model_context("qcoder-14b:latest", 12288, path)
    config.set_model_context("qwen2.5:1.5b", 16384, path)
    models = config.load_config(path).models
    assert models["qwen2.5:1.5b"] == {"num_ctx": 16384} and models["qcoder-14b"] == {"num_ctx": 12288}
    assert path.read_text().count('[models."qwen2.5:1.5b"]') == 1


@pytest.mark.parametrize("raw,tokens", [("16384", 16384), ("16k", 16384), ("32K", 32768), ("1.5k", 1536), ("1m", 1048576)])
def test_parse_tokens(raw, tokens):
    assert config.parse_tokens(raw) == tokens


def test_overwrite_needs_a_read_first(root):
    (root / "page.html").write_text("<h1>precious</h1>\n" * 50)
    agent, *_ = make_agent(root, [
        ("", [call("write_file", path="page.html", content="<html></html>")]),
        ("", [call("read_file", path="page.html")]),
        ("", [call("write_file", path="page.html", content="<html>new</html>")]),
        ("done", []),
    ])
    agent.run("rewrite page.html")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "haven't read it" in results[0]
    assert (root / "page.html").read_text() == "<html>new</html>"  # allowed after the read


def test_new_files_and_guard_off_dont_need_a_read(root):
    (root / "a.txt").write_text("old")
    agent, *_ = make_agent(root, [("", [call("write_file", path="a.txt", content="new"), call("write_file", path="b.txt", content="b")]), ("ok", [])])
    agent.ctx.guard_overwrite = False
    agent.run("go")
    assert (root / "a.txt").read_text() == "new" and (root / "b.txt").read_text() == "b"


def test_plan_mode_refuses_changes_but_keeps_going(root):
    agent, *_ = make_agent(root, [("", [call("write_file", path="x.txt", content="x")]), ("1. create x.txt", [])])
    agent.permissions.mode = "plan"
    agent.refresh_system()
    agent.run("make x")
    assert not (root / "x.txt").exists()
    assert "Plan mode is on" in agent.messages[3]["content"] and agent.messages[-1]["content"] == "1. create x.txt"
    assert "PLAN MODE IS ON" in agent.messages[0]["content"]


@pytest.mark.parametrize("text,unfinished", [
    ("I'll write this back to the file.\n\nHere's the command:", True),
    ("Sure, I'll add some basic CSS for better styling.", True),
    ("Done! The page is ready.", False),
    ("Would you like me to add a dark theme?", False),
    ("```html\n<p>x</p>\n```", False),
])
def test_looks_unfinished(text, unfinished):
    from qcode.agent import looks_unfinished
    assert looks_unfinished(text) is unfinished


def test_nudge_once_when_model_stalls(root):
    agent, client, _ = make_agent(root, [
        ("Here's the updated page. I'll write this back to the file.\n\nHere's the command:", []),
        ("", [call("write_file", path="p.html", content="<p>hi</p>")]),
        ("Saved.", []),
    ])
    agent.run("polish p.html")
    assert (root / "p.html").read_text() == "<p>hi</p>"
    agent2, client2, _ = make_agent(root, [("I'll do it now:", []), ("I'll do it now:", []), ("unused", [])])
    agent2.run("go")
    assert len(client2.responses) == 1  # nudged once, then gave up
    agent3, client3, _ = make_agent(root, [("I'll do it now:", []), ("unused", [])])
    agent3.config.auto_continue = False
    agent3.run("go")
    assert len(client3.responses) == 1


def test_undo_history_survives_resume(root):
    (root / "a.py").write_text("v1\n")
    agent, *_ = make_agent(root, [("", [call("read_file", path="a.py")]), ("", [call("write_file", path="a.py", content="v2\n")]), ("ok", [])])
    agent.run("change a.py")
    agent2, *_ = make_agent(root, [])
    agent2.load_session(agent.session_path)
    assert agent2.undo().prompt == "change a.py"
    assert (root / "a.py").read_text() == "v1\n"


def test_temperature_is_passed_only_when_set(root):
    seen = {}

    class Client:
        def stream_chat(self, model, messages, tools, on_text, **kw):
            seen.update(kw)
            return "ok", []

    agent, *_ = make_agent(root, [])
    agent.client = Client()
    agent.run("hi")
    assert seen == {}
    agent.config.temperature = 0.3
    agent.run("hi")
    assert seen == {"temperature": 0.3}
