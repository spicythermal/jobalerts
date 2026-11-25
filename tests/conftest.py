import asyncio
import json
import os
from pathlib import Path

import httpx

from jobalerts.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).parent.parent
# Tests see the shipped defaults only, never a developer's own data/config.local.yaml.
os.environ["JOBALERTS_LOCAL_CONFIG"] = str(Path(__file__).parent / "no-local-config.yaml")


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


def with_reference_home(cfg):
    """A filled-in pay reference (the shipped defaults leave it to the Settings page): $155K in
    Portland, OR, staying in a house that costs $3,400 a month. The map and pay tests are worked
    out against these numbers."""
    cfg.pay.reference_salary = 155_000
    cfg.pay.reference_location = "Portland, OR"
    cfg.pay.current_home = cfg.pay.current_home.model_copy(update={"payment": 3400, "principal": 750,
                                                                   "upkeep": 450, "rate": 4.5})
    return cfg
