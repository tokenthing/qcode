"""REPL-level behaviour that doesn't need a terminal: /context set and settings."""

import pytest

from qcode.client import ContextInfo
from qcode.repl import Repl
from qcode.servers import Server, ServerStore
from test_agent import make_agent


class FakeServer:
    base_url = "http://fake/v1"

    def __init__(self, info, fit=(100, 9000)):
        self.info, self.fit, self.set_calls = info, fit, []

    def context_info(self, model):
        return self.info

    def set_context(self, model, tokens):
        self.set_calls.append((model, tokens))
        self.info = ContextInfo(tokens, self.info.maximum, "model")

    def gpu_fit(self, model):
        return self.fit


@pytest.fixture
def make_repl(root, tmp_path):
    def build(info, **kw):
        agent, *_ = make_agent(root, [])
        agent.client = FakeServer(info, **kw)
        store = ServerStore(tmp_path / "servers.json", seed=Server("home", "http://fake/v1"))
        repl = Repl(agent, store, store.current)
        repl._confirm = lambda question: True
        return repl, agent
    return build


def test_context_set_on_ollama_changes_the_server_model(make_repl):
    repl, agent = make_repl(ContextInfo(8192, 32768, "model"))
    repl.resize_context("16k")
    assert agent.client.set_calls == [("m", 16384)] and agent.num_ctx == 16384
    repl.resize_context("max")
    assert agent.client.set_calls[-1] == ("m", 32768)


def test_context_set_rejects_too_big_and_too_small(make_repl):
    repl, agent = make_repl(ContextInfo(8192, 32768, "model"))
    repl.resize_context("64k")
    repl.resize_context("1000")
    repl.resize_context("lots")
    assert agent.client.set_calls == []


def test_context_set_on_other_servers_only_updates_config(make_repl):
    repl, agent = make_repl(ContextInfo(None, None, ""))
    repl.resize_context("24k")
    assert agent.client.set_calls == [] and agent.num_ctx == 24576
    assert agent.config.models["m"] == {"num_ctx": 24576}


def test_declining_the_confirmation_changes_nothing(make_repl):
    repl, agent = make_repl(ContextInfo(8192, 32768, "model"))
    repl._confirm = lambda question: False
    repl.resize_context("16k")
    assert agent.client.set_calls == [] and agent.num_ctx == 8192


def test_apply_setting_takes_effect_immediately(make_repl):
    repl, agent = make_repl(ContextInfo(8192, 32768, "model"))
    repl.apply_setting("read_before_write", False)
    assert agent.ctx.guard_overwrite is False
    repl.apply_setting("default_mode", "plan")
    assert repl.mode == "plan" and "PLAN MODE IS ON" in agent.messages[0]["content"]
    repl.set_mode("ask")
    assert "PLAN MODE IS ON" not in agent.messages[0]["content"]
