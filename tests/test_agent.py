import json
import sys

from qcode.agent import DENIED, Agent
from qcode.client import ToolCall
from qcode.config import Config
from qcode.permissions import Permissions
from qcode.ui import NullUI


class FakeClient:
    """Replays scripted (content, tool_calls) responses."""

    def __init__(self, responses, summary="summary"):
        self.responses = list(responses)
        self.summary = summary
        self.seen: list[list[dict]] = []

    def stream_chat(self, model, messages, tools, on_text):
        self.seen.append([dict(m) for m in messages])
        content, calls = self.responses.pop(0)
        if content:
            on_text(content)
        return content, calls

    def complete(self, model, messages):
        return self.summary


def make_agent(root, responses, permission="y", **cfg):
    ui = NullUI(permission)
    client = FakeClient(responses)
    config = Config(models={"m": {"num_ctx": cfg.pop("num_ctx", 8192)}}, model="m", **cfg)
    agent = Agent(client, config, root, Permissions(root, [], ui.ask_permission), ui)
    return agent, client, ui


def call(name, **args):
    return ToolCall(f"id_{name}", name, json.dumps(args))


def test_native_tool_call_round_trip(root):
    agent, client, ui = make_agent(root, [
        ("", [call("write_file", path="hi.py", content="print('hi')\n")]),
        ("", [call("bash", command=f'"{sys.executable}" hi.py')]),
        ("Done.", []),
    ])
    agent.run("make hi.py and run it")
    assert (root / "hi.py").exists()
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert "hi" in agent.messages[5]["content"]
    assert agent.messages[-1]["content"] == "Done."


def test_text_tool_call_fallback(root):
    (root / "a.txt").write_text("secret sauce\n")
    agent, client, ui = make_agent(root, [
        ('Reading.\n<tool_call>{"name": "read_file", "arguments": {"path": "a.txt"}}</tool_call>', []),
        ("It says secret sauce.", []),
    ])
    agent.run("what's in a.txt")
    assistant = agent.messages[2]
    assert assistant["content"] == "Reading."
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    assert "secret sauce" in agent.messages[3]["content"]


def test_denied_call_stops_turn_and_answers_every_call(root):
    agent, client, ui = make_agent(root, [
        ("", [call("bash", command="rm -rf /"), call("read_file", path="x")]),
    ], permission="n")
    agent.run("clean up")
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert [m["content"] for m in tool_msgs] == [DENIED, "Skipped."]
    assert client.responses == []  # the model wasn't called again


def test_tool_errors_go_back_to_model(root):
    agent, client, ui = make_agent(root, [
        ("", [call("read_file", path="nope.txt")]),
        ("", [ToolCall("x", "read_file", "{not json")]),
        ("", [call("no_such_tool")]),
        ("ok", []),
    ])
    agent.run("go")
    errors = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "does not exist" in errors[0]
    assert "invalid arguments JSON" in errors[1]
    assert "unknown tool" in errors[2]


def test_max_steps(root):
    agent, client, ui = make_agent(root, [("", [call("list_dir")])] * 3, max_steps=3)
    agent.run("loop forever")
    assert client.responses == []


def test_compact_keeps_current_turn(root):
    agent, client, ui = make_agent(root, [("first answer", []), ("second answer", [])])
    agent.run("first question")
    agent.run("second question")
    agent.messages.append({"role": "user", "content": "third question"})
    agent.compact(keep_current_turn=True)
    assert agent.messages[1]["content"].endswith("summary")
    assert agent.messages[-1] == {"role": "user", "content": "third question"}
    assert len(agent.messages) == 4


def test_session_save_and_resume(root):
    agent, *_ = make_agent(root, [("hello there", [])])
    agent.run("hi")
    agent2, *_ = make_agent(root, [])
    agent2.load_session(agent.session_path)
    assert [m["content"] for m in agent2.messages[1:]] == ["hi", "hello there"]
    assert agent2.messages[0]["role"] == "system"


def test_memory_in_system_prompt(root):
    (root / "QCODE.md").write_text("Always use tabs.")
    agent, *_ = make_agent(root, [])
    assert "Always use tabs." in agent.messages[0]["content"]


def test_bare_json_call_not_kept_as_content(root):
    agent, client, ui = make_agent(root, [
        ('{"name": "list_dir", "arguments": {}}', []),
        ("empty", []),
    ])
    agent.run("ls")
    assert agent.messages[2]["content"] == ""
    assert agent.messages[2]["tool_calls"][0]["function"]["name"] == "list_dir"
