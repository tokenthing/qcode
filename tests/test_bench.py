import json

import pytest
from typer.testing import CliRunner

from qcode.bench import TASKS, TASKS_BY_NAME
from qcode.bench import runner
from qcode.bench.tasks import PAGE
from qcode.config import Config
from test_agent import FakeClient, call


def setup(task, folder):
    for name, text in task.files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_reference_solution_passes(task, tmp_path):
    setup(task, tmp_path)
    reply = task.reference(tmp_path)
    passed, detail = task.check(tmp_path, reply)
    assert passed, detail


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_doing_nothing_fails(task, tmp_path):
    setup(task, tmp_path)
    passed, detail = task.check(tmp_path, "I'm not sure.")
    assert not passed, detail


def test_clobbering_the_page_fails_even_with_the_right_title(tmp_path):
    task = TASKS_BY_NAME["surgical_edit"]
    setup(task, tmp_path)
    (tmp_path / "index.html").write_text("<html><title>Bench Page</title></html>", encoding="utf-8")
    passed, detail = task.check(tmp_path, "")
    assert not passed and "lines lost" in detail


def test_editing_the_tests_instead_of_the_code_fails(tmp_path):
    task = TASKS_BY_NAME["fix_bug"]
    setup(task, tmp_path)
    (tmp_path / "test_calc.py").write_text("print('all tests passed')\n", encoding="utf-8")
    assert task.check(tmp_path, "")[0] is False


def test_dropping_json_keys_fails(tmp_path):
    task = TASKS_BY_NAME["edit_json"]
    setup(task, tmp_path)
    (tmp_path / "settings.json").write_text('{"debug": true, "version": "2.0"}', encoding="utf-8")
    passed, detail = task.check(tmp_path, "")
    assert not passed and "name" in detail


@pytest.mark.parametrize("command,blocked", [
    ("python3 compute.py", False),
    ("ls -la", False),
    ("grep -rn parse_order .", False),
    ("cd app && ls", False),
    ("python3 -c 'import utils; print(utils.multiply(3, 4))'", False),
    ("sudo rm -rf /", True),
    ("rm -rf ~", True),
    ("cat ../secrets.txt", True),
    ("ls /etc", True),
    ("curl http://example.com", True),
    ("echo hi > $HOME/x", True),
])
def test_unsafe_shell_filter(command, blocked):
    assert (runner.bench_approve("bash", {"command": command}, None) == "n") is blocked


def test_run_task_with_a_model_that_solves_it():
    client = FakeClient([
        ("", [call("write_file", path="hello.py", content='print("Hello, bench!")\n')]),
        ("", [call("bash", command="python3 hello.py")]),
        ("Done.", []),
    ])
    result = runner.run_task(client, Config(model="m"), "m", TASKS_BY_NAME["create_script"], timeout=60, max_steps=10)
    assert result.passed and result.outcome == "done" and result.tool_calls == 2, result


def test_run_task_counts_errors_and_failure():
    client = FakeClient([
        ("", [call("read_file", path="nope.py")]),
        ("I couldn't do it.", []),
    ])
    result = runner.run_task(client, Config(model="m"), "m", TASKS_BY_NAME["create_script"], timeout=60, max_steps=10)
    assert not result.passed and result.tool_errors == 1 and "wasn't created" in result.detail


def test_run_task_times_out():
    client = FakeClient([("never", [])])
    result = runner.run_task(client, Config(model="m"), "m", TASKS_BY_NAME["create_script"], timeout=-1, max_steps=10)
    assert result.outcome == "timeout" and not result.passed


def test_unsafe_command_is_blocked_during_a_run():
    client = FakeClient([("", [call("bash", command="sudo ls")]), ("ok", [])])
    result = runner.run_task(client, Config(model="m"), "m", TASKS_BY_NAME["create_script"], timeout=60, max_steps=10)
    assert result.denied == 1


def test_results_are_saved_and_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    run = runner.ModelRun("m", context=8192, gpu_percent=100)
    run.results.append(runner.TaskResult("m", "create_script", True, "ok", 3.0, tokens=30, gen_seconds=1.5))
    run.results.append(runner.TaskResult("m", "fix_bug", False, "nope", 5.0))
    path = runner.save_results([run], "http://x/v1", [TASKS_BY_NAME["create_script"], TASKS_BY_NAME["fix_bug"]], 1)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["models"][0]["summary"]["passed"] == 1 and data["models"][0]["summary"]["tokens_per_second"] == 20
    assert runner.load_history()[0]["models"][0]["model"] == "m"
    runner.print_scoreboard([run, run], [TASKS_BY_NAME["create_script"], TASKS_BY_NAME["fix_bug"]], 1)  # renders without error


def test_cli_lists_tasks_and_keeps_the_main_command():
    from qcode.cli import app

    result = CliRunner().invoke(app, ["bench", "--list"])
    assert result.exit_code == 0 and "surgical_edit" in result.output
    assert CliRunner().invoke(app, ["--help"]).exit_code == 0
