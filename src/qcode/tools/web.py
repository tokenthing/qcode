"""web_search (DuckDuckGo), web_fetch (page → readable text) and weather (Open-Meteo). No API keys.

These are only offered to the model on turns where the user asked for something online
(see agent.wants_web); otherwise qcode never touches the network for tools.
"""

from __future__ import annotations

import datetime
import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .registry import Tool, ToolContext, ToolError, params

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
TIMEOUT = 15
MAX_RESULTS = 6
MAX_PAGE_BYTES = 3_000_000


# --- web_search ---------------------------------------------------------------------------
# Both DuckDuckGo HTML layouts: the regular one (class="result__a") and the lite one (class='result-link').
_TITLE_RE = re.compile(r"""<a[^>]+?(?:class=["']result__a["'][^>]*?href=["'](?P<h1>[^"']+)["']|href=["'](?P<h2>[^"']+)["'][^>]*?class=["'](?:result__a|result-link)["'])[^>]*>(?P<title>.*?)</a>""", re.S)
_SNIPPET_RE = re.compile(r"""class=["'](?:result__snippet|result-snippet)["'][^>]*>(?P<snippet>.*?)</(?:a|div|td|span)>""", re.S)


def _clean(fragment: str | None) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment or "")).split())


def _real_url(href: str) -> str:
    """DuckDuckGo wraps links as //duckduckgo.com/l/?uddg=<url>; unwrap them."""
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    query = parse_qs(urlparse(href).query)
    return unquote(query["uddg"][0]) if "uddg" in query else href


def parse_results(page: str) -> list[tuple[str, str, str]]:
    """(title, url, snippet) for each organic result, ads and duplicates removed."""
    anchors = list(_TITLE_RE.finditer(page))
    results, seen = [], set()
    for i, m in enumerate(anchors):
        url = _real_url(m.group("h1") or m.group("h2"))
        if "duckduckgo.com/y.js" in url or not url.startswith("http") or url in seen:
            continue  # ads and repeats
        seen.add(url)
        block = page[m.end(): anchors[i + 1].start() if i + 1 < len(anchors) else len(page)]
        snippet = _SNIPPET_RE.search(block)
        results.append((_clean(m.group("title")), url, _clean(snippet.group("snippet") if snippet else "")))
    return results


def web_search(ctx: ToolContext, args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("give a search query")
    pages = []
    try:
        for url in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
            r = httpx.post(url, data={"q": query}, headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True)
            pages.append(r)
            results = parse_results(r.text) if r.status_code == 200 else []
            if results:
                break
    except httpx.HTTPError as e:
        raise ToolError(f"couldn't reach DuckDuckGo ({type(e).__name__}); is this computer online?") from None
    if not results:
        if any(p.status_code in (202, 403, 429) or "anomaly" in p.text.lower() for p in pages):
            raise ToolError("DuckDuckGo is rate-limiting searches right now; wait a minute and try again")
        return f"No results for {query!r}."
    lines = [f"Results for {query!r}:"]
    for i, (title, url, snippet) in enumerate(results[:MAX_RESULTS], 1):
        lines.append(f"{i}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else ""))
    lines.append("Use web_fetch on a result URL to read the page.")
    return "\n".join(lines)


# --- web_fetch ----------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "template", "iframe"}
    BLOCK = {"p", "div", "section", "article", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")
            if tag in ("h1", "h2", "h3"):
                self.parts.append("## ")
            elif tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip_depth:
            self.parts.append(data)


def html_to_text(page: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(page)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    text = "\n".join(line for line in lines if line and line not in ("##", "-"))
    return " ".join(parser.title.split()), re.sub(r"\n{3,}", "\n\n", text)


def web_fetch(ctx: ToolContext, args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not re.match(r"https?://", url):
        raise ToolError("url must start with http:// or https://")
    try:
        with httpx.stream("GET", url, headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True) as r:
            if r.status_code >= 400:
                raise ToolError(f"{url} returned HTTP {r.status_code}")
            kind = r.headers.get("content-type", "")
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_PAGE_BYTES:
                    break
            final_url = str(r.url)
    except httpx.HTTPError as e:
        raise ToolError(f"couldn't fetch {url} ({type(e).__name__})") from None
    text = body.decode(r.encoding or "utf-8", errors="replace")
    if "html" in kind or text.lstrip().lower().startswith(("<!doctype html", "<html")):
        title, text = html_to_text(text)
    elif not any(t in kind for t in ("text", "json", "xml", "javascript")) and kind:
        raise ToolError(f"{url} isn't a text page ({kind})")
    else:
        title = ""
    find = (args.get("find") or "").strip()
    if find:
        words = [w.lower() for w in find.split()]
        paras = [p for p in re.split(r"\n\s*\n|\n(?=## )", text) if any(w in p.lower() for w in words)]
        text = "\n\n".join(paras) if paras else f"(nothing on the page mentions {find!r}; showing the start)\n\n{text}"
    header = f"{title}\n{final_url}\n" if title else f"{final_url}\n"
    return header + "\n" + text.strip()


# --- weather ------------------------------------------------------------------------------
WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def _geocode(place: str) -> dict:
    name, _, region = (s.strip() for s in place.partition(","))
    r = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                  params={"name": name, "count": 10, "language": "en", "format": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    hits = r.json().get("results") or []
    if region:
        wanted = region.lower()
        hits = [h for h in hits if wanted in " ".join(str(h.get(k, "")) for k in ("country", "country_code", "admin1")).lower()] or hits
    if not hits:
        raise ToolError(f"couldn't find a place called {place!r}; try a nearby city name")
    return hits[0]


def weather(ctx: ToolContext, args: dict) -> str:
    place = (args.get("location") or ctx.location or "").strip()
    if not place:
        raise ToolError("the user's location isn't set. Ask the user which city they're in (they can also save it "
                        "with /settings → Location), then call weather again with location.")
    days = max(1, min(int(args.get("days") or 3), 7))
    try:
        spot = _geocode(place)
        r = httpx.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": spot["latitude"], "longitude": spot["longitude"], "timezone": "auto", "forecast_days": days,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        }, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        raise ToolError(f"couldn't reach the weather service ({type(e).__name__})") from None
    where = ", ".join(str(spot[k]) for k in ("name", "admin1", "country") if spot.get(k))
    cur, units = data["current"], data.get("current_units", {})
    out = [
        f"Weather for {where} (local time {cur.get('time', '').replace('T', ' ')})",
        f"Now: {WEATHER_CODES.get(cur.get('weather_code'), 'unknown')}, {cur['temperature_2m']}{units.get('temperature_2m', '°C')} "
        f"(feels like {cur['apparent_temperature']}{units.get('apparent_temperature', '°C')}), humidity {cur['relative_humidity_2m']}%, "
        f"wind {cur['wind_speed_10m']} {units.get('wind_speed_10m', 'km/h')}",
    ]
    d = data["daily"]
    for i, day in enumerate(d["time"]):
        rain = d["precipitation_probability_max"][i]
        label = datetime.date.fromisoformat(day).strftime("%A %Y-%m-%d") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else day
        out.append(f"{label}: {WEATHER_CODES.get(d['weather_code'][i], 'unknown')}, "
                   f"{d['temperature_2m_min'][i]}–{d['temperature_2m_max'][i]}°C"
                   + (f", {rain}% chance of rain" if rain is not None else ""))
    return "\n".join(out)


TOOLS = [
    Tool("web_search", "Search the web with DuckDuckGo. Returns titles, URLs and snippets; then use web_fetch to read a page.",
         params(["query"], query={"type": "string", "description": "What to search for"}),
         web_search, read_only=True),
    Tool("web_fetch", "Read a web page as plain text (scripts, menus and ads removed). Use `find` to keep only the parts about something.",
         params(["url"], url={"type": "string", "description": "Full http(s) URL"},
                find={"type": "string", "description": "Optional words; only paragraphs mentioning them are returned"}),
         web_fetch, read_only=True),
    Tool("weather", "Current weather and forecast for a place. Leave location empty to use the user's saved location.",
         params([], location={"type": "string", "description": "City, optionally with country, e.g. 'Bangkok' or 'Paris, France'"},
                days={"type": "integer", "description": "Days of forecast, 1-7 (default 3)"}),
         weather, read_only=True),
]
