import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("install_qcode", Path(__file__).parents[1] / "installer" / "install_qcode.py")
installer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(installer)


@pytest.mark.parametrize("version,expected", [
    ((3, 8, 18), False),
    ((3, 9, 6), True),
    ((3, 10, 0), False),  # ForwardRef(is_class=...) missing; breaks pydantic
    ((3, 10, 1), True),
    ((3, 14, 4), True),
])
def test_python_ok(version, expected):
    assert installer.python_ok(version) is expected


def test_find_python_skips_broken_and_picks_newest(monkeypatch):
    versions = {"/bad/3.10.0": (3, 10, 0), "/ok/3.9": (3, 9, 6), "/ok/3.12": (3, 12, 4), "/dead": None}
    monkeypatch.setattr(installer, "python_candidates", lambda: [[p] for p in versions])
    monkeypatch.setattr(installer, "probe_python", lambda cmd: versions[cmd[0]])
    monkeypatch.setattr(installer.sys, "version_info", (3, 10, 0, "final", 0))
    assert installer.find_python() == ["/ok/3.12"]


def test_find_python_explains_when_nothing_works(monkeypatch, capsys):
    monkeypatch.setattr(installer, "python_candidates", lambda: [["/bad"]])
    monkeypatch.setattr(installer, "probe_python", lambda cmd: (3, 10, 0))
    monkeypatch.setattr(installer.sys, "version_info", (3, 10, 0, "final", 0))
    with pytest.raises(SystemExit):
        installer.find_python()
    assert "has a bug" in capsys.readouterr().out
