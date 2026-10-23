import asyncio
import json
from pathlib import Path

import httpx

from jobalerts.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).parent.parent


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def run(coro):
    return asyncio.run(coro)


def mock_http(handler, **intervals) -> HttpClient:
    """HttpClient whose requests go to handler(request) -> httpx.Response, with no pacing by default."""
    return HttpClient(host_min_interval={"default": 0.0, **intervals}, transport=httpx.MockTransport(handler))


def json_routes(routes: dict[str, object]):
    """Handler serving JSON by URL substring; unknown URLs get a 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for fragment, body in routes.items():
            if fragment in url:
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "not found"})
    return handler
