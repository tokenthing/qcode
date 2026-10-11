import json

import pytest

from qcode.servers import Server, ServerStore, normalize_url


@pytest.mark.parametrize("raw,expected", [
    ("192.168.1.50", "http://192.168.1.50:11434/v1"),
    ("192.168.1.50:8000", "http://192.168.1.50:8000/v1"),
    ("gpu-box.local", "http://gpu-box.local:11434/v1"),
    ("http://10.0.0.5:11434", "http://10.0.0.5:11434/v1"),
    ("http://10.0.0.5:1234/v1/", "http://10.0.0.5:1234/v1"),
    ("https://api.example.com/v1", "https://api.example.com/v1"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "ftp://x", "10.0.0.1:notaport"])
def test_normalize_url_rejects(raw):
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_store_seeds_adds_switches_removes(tmp_path):
    path = tmp_path / "servers.json"
    store = ServerStore(path, seed=Server("home", "http://192.168.1.50:11434/v1"))
    assert store.current.name == "home" and store.current.address == "192.168.1.50:11434"

    store.add("laptop", "10.0.0.9")
    store.use("laptop")
    reloaded = ServerStore(path, seed=Server("ignored", "http://x/v1"))
    assert set(reloaded.servers) == {"home", "laptop"}
    assert reloaded.current.base_url == "http://10.0.0.9:11434/v1"

    reloaded.remove("laptop")
    assert reloaded.active == "home"
    with pytest.raises(ValueError, match="only server"):
        reloaded.remove("home")
    assert json.loads(path.read_text())["active"] == "home"


def test_store_rejects_bad_names(tmp_path):
    store = ServerStore(tmp_path / "s.json", seed=Server("home", "http://h/v1"))
    with pytest.raises(ValueError, match="name"):
        store.add("bad name!", "10.0.0.1")
    with pytest.raises(KeyError):
        store.use("nope")
