import httpx
import pytest

from qcode.agent import wants_web
from qcode.tools import WEB_TOOLS, ToolContext, ToolError
from qcode.tools import web
from test_agent import FakeClient, call, make_agent


@pytest.mark.parametrize("text,expected", [
    ("what's the weather today in my location", True),
    ("search the web for the latest Python release", True),
    ("look up how to use asyncio.TaskGroup", True),
    ("read https://docs.python.org/3/whatsnew/3.13.html and summarize it", True),
    ("who won the match last night", True),
    ("fix the failing test in calc.py", False),
    ("add a multiply function to utils.py", False),
    ("rename calc_total to compute_total", False),
    ("what does this regex do?", False),
])
def test_wants_web(text, expected):
    assert wants_web(text) is expected


class RecordingClient(FakeClient):
    def stream_chat(self, model, messages, tools, on_text, **kw):
        self.offered = [t["function"]["name"] for t in tools]
        return super().stream_chat(model, messages, tools, on_text)


def test_web_tools_are_only_offered_when_asked(root):
    agent, *_ = make_agent(root, [])
    agent.client = RecordingClient([("ok", []), ("ok", []), ("ok", []), ("ok", [])])
    agent.run("add a function to utils.py")
    assert not set(WEB_TOOLS) & set(agent.client.offered)
    agent.run("what's the weather today?")
    assert set(WEB_TOOLS) <= set(agent.client.offered)
    assert agent.messages[-2]["content"].startswith("[Web access is on")
    agent.run("explain this code", web=True)  # /web forces it
    assert set(WEB_TOOLS) <= set(agent.client.offered)
    agent.config.web_access = "off"
    agent.run("search the web for x", web=True)
    assert not set(WEB_TOOLS) & set(agent.client.offered)


def test_location_is_only_shared_on_web_turns(root):
    agent, *_ = make_agent(root, [("ok", []), ("ok", [])])
    agent.config.location = "Bangkok"
    agent.run("rename a variable")
    assert "Bangkok" not in agent.messages[1]["content"]
    agent.run("weather today?")
    assert "The user's location: Bangkok." in agent.messages[3]["content"]


def test_model_calling_web_tools_without_permission_gets_told_why(root):
    agent, *_ = make_agent(root, [("", [call("web_search", query="x")]), ("ok", [])])
    agent.run("fix the bug")
    assert "only available when the user asks" in agent.messages[3]["content"]


def test_retry_doesnt_stack_the_web_note(root):
    agent, *_ = make_agent(root, [("a", []), ("b", [])])
    agent.run("weather today?")
    agent.run(agent.pop_last_turn())
    assert agent.messages[1]["content"].count("[Web access is on") == 1


def test_weather_needs_a_location(root):
    with pytest.raises(ToolError, match="Ask the user which city"):
        web.weather(ToolContext(root), {})


def test_weather_formats_open_meteo(monkeypatch, root):
    def fake_get(url, params=None, timeout=None):
        if "geocoding" in url:
            body = {"results": [{"name": "Bangkok", "admin1": "Bangkok", "country": "Thailand", "latitude": 13.75, "longitude": 100.5}]}
        else:
            body = {"current": {"time": "2026-10-20T15:45", "temperature_2m": 29.2, "apparent_temperature": 34.9,
                                "relative_humidity_2m": 77, "weather_code": 3, "wind_speed_10m": 5.8},
                    "current_units": {"temperature_2m": "°C", "apparent_temperature": "°C", "wind_speed_10m": "km/h"},
                    "daily": {"time": ["2026-10-20"], "weather_code": [95], "temperature_2m_min": [25.9],
                              "temperature_2m_max": [32.4], "precipitation_probability_max": [100]}}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(web.httpx, "get", fake_get)
    out = web.weather(ToolContext(root, location="Bangkok"), {})
    assert "Bangkok, Thailand" in out and "overcast, 29.2°C" in out and "Tuesday 2026-10-20: thunderstorm, 25.9–32.4°C, 100% chance of rain" in out


def test_search_results_are_parsed_and_rate_limits_reported(monkeypatch, root):
    page = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpeps.python.org%2Fpep-0719%2F">'
            'PEP 719 – Python 3.13</a><a class="result__snippet">Released on 2024-10-07.</a>')
    monkeypatch.setattr(web.httpx, "post", lambda url, **kw: httpx.Response(200, text=page, request=httpx.Request("POST", url)))
    out = web.web_search(ToolContext(root), {"query": "python 3.13"})
    assert "1. PEP 719 – Python 3.13\n   https://peps.python.org/pep-0719/\n   Released on 2024-10-07." in out
    monkeypatch.setattr(web.httpx, "post", lambda url, **kw: httpx.Response(202, text="anomaly", request=httpx.Request("POST", url)))
    with pytest.raises(ToolError, match="rate-limiting"):
        web.web_search(ToolContext(root), {"query": "x"})


def test_html_to_text_drops_scripts_and_menus():
    title, text = web.html_to_text("<html><head><title>Docs</title><script>evil()</script></head><body><nav>Home | About</nav>"
                                   "<h1>Install</h1><p>Run <code>pip install x</code>.</p><ul><li>fast</li></ul><footer>©</footer></body></html>")
    assert title == "Docs" and "evil" not in text and "Home" not in text and "©" not in text
    assert "## Install" in text and "Run pip install x." in text and "- fast" in text


def test_fetch_rejects_non_http(root):
    with pytest.raises(ToolError, match="http"):
        web.web_fetch(ToolContext(root), {"url": "file:///etc/passwd"})
