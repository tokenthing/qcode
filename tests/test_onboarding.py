import pytest
import typer

from qcode import onboarding
from qcode.config import Config
from qcode.servers import Ping, ServerStore


@pytest.fixture
def answers(monkeypatch):
    """Feed scripted answers to text prompts and menus."""
    script = {"text": [], "select": []}
    monkeypatch.setattr(onboarding.ui, "text_input", lambda label, default="": script["text"].pop(0))
    monkeypatch.setattr(onboarding.ui, "select", lambda q, opts, cancel: script["select"].pop(0))
    return script


def test_first_run_saves_the_server(answers, monkeypatch, tmp_path):
    monkeypatch.setattr(onboarding, "ping", lambda server, timeout: Ping(True, 12, ["qcoder-14b:latest"]))
    answers["text"] += ["192.168.1.50", "", "gpu"]
    store = ServerStore(tmp_path / "servers.json")
    server = onboarding.first_run_setup(store)
    assert server.base_url == "http://192.168.1.50:11434/v1" and server.name == "gpu"
    assert server.api_key == "ollama"
    assert ServerStore(tmp_path / "servers.json").current.name == "gpu"  # persisted


def test_first_run_with_password(answers, monkeypatch, tmp_path):
    recorded = []
    monkeypatch.setattr(onboarding, "ping", lambda server, timeout: recorded.append(server) or Ping(True, 12, ["qcoder-14b:latest"]))
    answers["text"] += ["192.168.1.50", "secret-pass", "gpu"]
    store = ServerStore(tmp_path / "servers.json")
    server = onboarding.first_run_setup(store)
    assert server.base_url == "http://192.168.1.50:11434/v1" and server.name == "gpu"
    assert server.api_key == "secret-pass"
    assert recorded[0].api_key == "secret-pass"
    assert ServerStore(tmp_path / "servers.json").current.api_key == "secret-pass"


def test_unreachable_server_can_be_retried_or_saved_anyway(answers, monkeypatch, tmp_path):
    monkeypatch.setattr(onboarding, "ping", lambda server, timeout: Ping(False, error="timed out"))
    answers["text"] += ["not a url!!", "10.0.0.9", "", "10.0.0.7", "", "home"]
    answers["select"] += ["retry", "save"]
    server = onboarding.first_run_setup(ServerStore(tmp_path / "servers.json"))
    assert server.base_url == "http://10.0.0.7:11434/v1"


def test_empty_answer_cancels(answers, tmp_path):
    answers["text"] += [""]
    store = ServerStore(tmp_path / "servers.json")
    assert onboarding.first_run_setup(store) is None and not store.servers


def test_no_default_address_without_a_terminal(monkeypatch, tmp_path):
    from qcode import cli, servers

    monkeypatch.setattr(servers, "SERVERS_FILE", tmp_path / "servers.json")
    monkeypatch.setattr(cli.ServerStore.__init__, "__defaults__", (tmp_path / "servers.json", None))
    monkeypatch.setattr(onboarding, "can_ask", lambda: False)
    with pytest.raises(typer.BadParameter, match="no model server saved yet"):
        cli.pick_server(Config(), None, None)
    _, one_off = cli.pick_server(Config(), None, "10.0.0.5")
    assert one_off.base_url == "http://10.0.0.5:11434/v1"


def test_old_configs_with_an_address_still_work(monkeypatch, tmp_path):
    from qcode import cli

    monkeypatch.setattr(cli.ServerStore.__init__, "__defaults__", (tmp_path / "servers.json", None))
    store, server = cli.pick_server(Config(base_url="http://10.3.0.1:11434/v1"), None, None)
    assert server.name == "home" and server.base_url == "http://10.3.0.1:11434/v1"
