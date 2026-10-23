import time

import httpx
import pytest

from jobalerts.http import HttpClient, RateLimited

from conftest import mock_http, run


def test_conditional_get_serves_304_from_cache():
    seen_headers = []

    def handler(request):
        seen_headers.append(request.headers.get("If-None-Match"))
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"jobs": [1, 2]}, headers={"ETag": '"v1"'})

    async def go():
        http = mock_http(handler)
        first = await http.get_json("https://api.example.com/jobs")
        second = await http.get_json("https://api.example.com/jobs")
        await http.aclose()
        return http, first, second

    http, first, second = run(go())
    assert first == second == {"jobs": [1, 2]}
    assert seen_headers == [None, '"v1"']
    assert http.stats["api.example.com"]["not_modified"] == 1


def test_429_raises_and_backs_off_host():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "30"})

    async def go():
        http = mock_http(handler)
        with pytest.raises(RateLimited):
            await http.get_json("https://api.example.com/jobs")
        await http.aclose()
        return http

    http = run(go())
    assert http.stats["api.example.com"]["rate_limited"] == 1
    assert http._next_ok["api.example.com"] - time.monotonic() > 25  # next request to that host waits ~30s


def test_per_host_pacing():
    def handler(request):
        return httpx.Response(200, json={})

    async def go():
        http = mock_http(handler, **{"slow.example.com": 0.3})
        start = time.monotonic()
        for _ in range(3):
            await http.get_json("https://slow.example.com/x", conditional=False)
        elapsed = time.monotonic() - start
        await http.get_json("https://fast.example.com/x", conditional=False)  # other hosts aren't held up
        await http.aclose()
        return elapsed

    assert run(go()) >= 0.55


def test_min_interval_longest_suffix():
    http = HttpClient(host_min_interval={"default": 1.0, "myworkdayjobs.com": 5.0, "wd5.myworkdayjobs.com": 7.0})
    assert http.min_interval("nvidia.wd5.myworkdayjobs.com") == 7.0
    assert http.min_interval("acme.wd1.myworkdayjobs.com") == 5.0
    assert http.min_interval("api.lever.co") == 1.0
    assert http.min_interval("notmyworkdayjobs.com") == 1.0
    run(http.aclose())


def test_404_allowed_returns_none():
    async def go():
        http = mock_http(lambda r: httpx.Response(404))
        result = await http.get_json("https://api.example.com/missing", allow_404=True)
        await http.aclose()
        return result

    assert run(go()) is None
