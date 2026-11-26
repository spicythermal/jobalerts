"""The cost-of-living map: every BEA metro's pay index (renting and buying) on a salary grid, the salary
that breaks even with staying home, and the top jobs, as one self-contained HTML page (map.html).

All the numbers come from col.PayIndex, so the map and `jobalerts pay` always agree; the page only
picks which precomputed number to color by.
"""
from __future__ import annotations

import json
import math
import re
from importlib.resources import files

from . import col
from .models import utcnow, web_url

SALARIES = list(range(100_000, 400_001, 5_000))   # the slider's steps, $5K apart
BREAK_EVEN_RANGE = (10_000, 5_000_000)
JITTER_DEG = 0.18                                 # spreads several jobs in one metro around its point


def _short(metro: str) -> str:
    return re.sub(r"\s*\*$|\s*Metropolitan Statistical Area$", "", metro).strip()


def break_even(pi: col.PayIndex, place: col.Place, buy: bool = False) -> float | None:
    """Gross salary (USD) whose index at the place is 1.0: as well off as at the reference.
    None when there is no buy index there, or not even $5M gets there."""
    def ratio(s: float) -> float | None:
        rent, b = pi.ratios(s, place)
        return b if buy else rent
    lo, hi = BREAK_EVEN_RANGE
    if ratio(hi) is None or ratio(hi) < 1:
        return None
    if ratio(lo) >= 1:
        return float(lo)
    for _ in range(40):  # take-home rises with gross, so the index does too: bisect
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if ratio(mid) < 1 else (lo, mid)
    return round(hi / 100) * 100


def _jobs(pi: col.PayIndex, coords: dict, rows: list[dict], kind: str, taken: dict[str, int]) -> tuple[list, list]:
    placed, unplaced = [], []
    for j in rows:
        loc = j.get("location") or ""
        place = pi.resolve(loc, remote="remote" in loc.lower())
        job = {"kind": kind, "title": j.get("title", ""), "company": j.get("company", ""), "location": loc,
               "salary": j.get("salary", ""), "url": web_url(j.get("url")), "fit": j.get("score"),
               "step_up": j.get("step_up"), "step_up_note": j.get("step_up_note", ""), "age": j.get("age", ""),
               "rent": j.get("pay_index"), "rent_low": j.get("pay_low"), "rent_high": j.get("pay_high"),
               "buy": j.get("buy_index"), "buy_low": j.get("buy_low"), "buy_high": j.get("buy_high"),
               "stories": j.get("story_ids") or []}
        point = coords.get(place.metro) if place and place.metro else None
        if point is None:
            job["where"] = place.label if place else "location not recognized"
            unplaced.append(job)
            continue
        n = taken.get(place.metro, 0)
        taken[place.metro] = n + 1
        angle = n * 2.4  # golden-angle spiral: neighbours never land on each other
        r = JITTER_DEG * math.sqrt(n) if n else 0.0
        job["lat"] = round(point[0] + r * math.sin(angle), 4)
        job["lon"] = round(point[1] + r * math.cos(angle) / max(0.3, math.cos(math.radians(point[0]))), 4)
        job["metro"] = _short(place.metro)
        placed.append(job)
    return placed, unplaced


def build_map_data(pi: col.PayIndex, jobs: list[dict] | None = None, step_ups: list[dict] | None = None,
                   salaries: list[int] = SALARIES) -> dict:
    """Everything the page shows. `jobs` / `step_ups` are dashboard.top_matches rows."""
    coords = pi.data.get("us_metro_coords") or {}
    home_metro = pi.home.metro
    metros = []
    for name in pi.metros:
        if name not in coords:
            continue
        place = pi.metro_place(name)
        parts = (pi.data.get("us_metro_parts") or {}).get(name) or {}
        grid = [pi.ratios(s, place) for s in salaries]
        has_buy = grid[0][1] is not None
        metros.append({
            "name": _short(name), "lat": coords[name][0], "lon": coords[name][1],
            "home": name == home_metro,
            "rent": [round(r, 3) for r, _ in grid],
            "buy": [round(b, 3) for _, b in grid] if has_buy else None,
            "be_rent": break_even(pi, place), "be_buy": break_even(pi, place, buy=True) if has_buy else None,
            "rpp": pi.metros[name], "fmr4": parts.get("fmr4"), "home4": parts.get("home4"),
            "own_note": place.buy_note,
        })
    taken: dict[str, int] = {}
    top, top_off = _jobs(pi, coords, jobs or [], "top", taken)
    seen = {j.get("url") for j in jobs or []}  # a job in both lists shows once, as a top job
    steps, steps_off = _jobs(pi, coords, [j for j in step_ups or [] if j.get("url") not in seen], "step", taken)
    sources = pi.data.get("sources") or {}
    return {
        "generated": utcnow().isoformat(timespec="minutes"),
        "data_built": pi.data.get("built", ""),
        "reference": {"salary": pi.reference_salary, "label": pi.home.label, "metro": _short(home_metro or "")},
        "salaries": salaries,
        "start": min(salaries, key=lambda s: abs(s - pi.reference_salary)),
        "has_buy": bool(pi.ownership),
        "metros": metros,
        "jobs": top + steps,
        "unplaced": top_off + steps_off,
        "sources": [v for k, v in sources.items() if v],
    }


def for_sharing(data: dict) -> dict:
    """The map for the read-only Home Assistant link: without your home's label or housing figures,
    and with "your current salary" instead of the amount in its text. (Break-even salaries still
    show, and they imply roughly what you earn; the Account page says so.)"""
    out = json.loads(json.dumps(data))
    out["reference"] = {"salary": data["reference"]["salary"], "label": out["reference"]["metro"] or "your home",
                        "metro": out["reference"]["metro"], "private": True}
    for m in out["metros"]:
        if m.get("home"):
            m["own_note"] = "staying in your home"
    return out


def render(data: dict) -> str:
    """The page with its data inlined (one file: nothing to fetch but the map library and tiles)."""
    template = files("jobalerts").joinpath("map_template.html").read_text(encoding="utf-8")
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return template.replace("/*__MAP_DATA__*/null", blob, 1)
