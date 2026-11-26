"""The Boards page: which job boards are polled, how each is doing, and the watch list's editor.

Rows come from the watch list (companies.yaml) and the aggregator searches in the config, joined with
each board's health from the database (last success, jobs listed, failures, last error), its matches
in the last 7 days and, while the service runs, when it's polled next. The table refreshes itself.
Edits (add via discovery, enable or disable, tier, remove) change data/companies.yaml and take effect
at once in `jobalerts run`. Everyone sees the boards (matches are their own); only admins edit them.
"""
from __future__ import annotations

import time
from datetime import timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import discover, watchlist
from ..models import parse_dt, utcnow
from ..sources import SOURCES


def ago(iso: str | None) -> str:
    when = parse_dt(iso) if iso else None
    if when is None:
        return ""
    minutes = (utcnow() - when).total_seconds() / 60
    if minutes < 90:
        return f"{max(minutes, 0):.0f} min ago"
    return f"{minutes / 60:.0f} h ago" if minutes < 48 * 60 else f"{minutes / 1440:.0f} d ago"


def until(ts: float | None) -> str:
    if not ts:
        return ""
    minutes = (ts - time.time()) / 60
    return "now" if minutes < 1 else f"in {minutes:.0f} min" if minutes < 90 else f"in {minutes / 60:.1f} h"


def add_board_routes(app: FastAPI, state, page, require_csrf) -> None:
    runner = state.runner

    def path():
        return runner.cfg.resolve(runner.cfg.companies_file)

    def board_rows(me) -> tuple[list[dict], list[dict], dict]:
        cfg, store = runner.cfg, runner.store
        health = {r["key"]: r for r in store.boards()}
        matches = me.store.matches_by_board(utcnow() - timedelta(days=7))
        tier_of = {v: k for k, v in cfg.polling.tiers.items()}
        companies, searches = [], []
        counts = {"ok": 0, "failing": 0, "disabled": 0, "waiting": 0, "needs_key": 0}
        for i, c in enumerate(cfg.companies):
            try:
                board, problem = cfg.company_board(c), ""
            except ValueError as e:
                board, problem = None, str(e)
            row = {"index": i, "name": c.name, "ats": c.ats, "tier": c.tier, "enabled": c.enabled,
                   "key": board.key if board else "", "interval": board.interval if board else None}
            companies.append(_with_health(row, health.get(row["key"]), matches, runner, problem, counts))
        for board in cfg.boards():
            if board.watched:
                continue
            missing = SOURCES[board.source].missing_keys()
            row = {"name": board.name, "ats": board.source, "tier": tier_of.get(board.interval, ""),
                   "enabled": True, "key": board.key, "interval": board.interval}
            searches.append(_with_health(row, health.get(board.key), matches, runner,
                                         f"needs {' and '.join(missing)}" if missing else "", counts))
        companies.sort(key=lambda r: r["state"] != "failing")   # problems first; the rest in watch-list order
        hosts = store.host_stats(days=1)
        counts["requests"] = sum(c["requests"] for c in hosts.values())
        counts["not_modified"] = sum(c["not_modified"] for c in hosts.values())
        counts["rate_limited"] = sum(c["rate_limited"] for c in hosts.values())
        return companies, searches, counts

    def context(request: Request, **extra):
        companies, searches, counts = board_rows(state.me(request))
        return dict(companies=companies, searches=searches, counts=counts, tiers=list(runner.cfg.polling.tiers),
                    running=runner.running, file=str(path()), can_edit=request.state.user.admin, **extra)

    @app.get("/boards")
    async def boards_page(request: Request, saved: str = "", error: str = ""):
        return page(request, "boards.html", **context(request, saved=saved, error=error))

    @app.get("/boards/table")
    async def boards_table(request: Request):
        return page(request, "boards_table.html", **context(request))

    @app.post("/boards/discover", dependencies=[Depends(require_csrf)])
    async def find_board(request: Request, target: str = Form("")):
        target = target.strip()
        if not target:
            return page(request, "boards_found.html", found=[], target="", error="Type a company name or a careers page URL.")
        try:
            if target.startswith(("http://", "https://")):
                found = await discover.find_by_url(target, runner.http)
            else:
                found = await discover.find_by_name(target, runner.http)
        except Exception as e:  # a careers page that won't load
            return page(request, "boards_found.html", found=[], target=target, error=f"Lookup failed: {type(e).__name__}")
        rows = [{"f": f, "tier": "slow" if f.ats == "workday" else "normal"} for f in found]
        return page(request, "boards_found.html", found=rows, target=target, tiers=list(runner.cfg.polling.tiers),
                    error="" if found else "No supported job board found. Try the careers page URL.")

    @app.post("/boards/add", dependencies=[Depends(require_csrf)])
    async def add_board(request: Request, name: str = Form(...), ats: str = Form(...), slug: str = Form(""),
                        url: str = Form(""), region: str = Form(""), tier: str = Form("normal")):
        found = discover.Found(ats, slug, name.strip() or slug, 0, url=url, region=region)
        entry = found.entry(tier if tier in runner.cfg.polling.tiers else "normal")
        try:
            added = watchlist.add(path(), entry)
        except ValueError as e:
            return RedirectResponse(f"/boards?error={type(e).__name__}", 303)
        runner.reload_watchlist()
        return RedirectResponse(f"/boards?saved={'added' if added else 'already'}", 303)

    def edit(index: int, change):
        entries = watchlist.load(path())
        if not 0 <= index < len(entries):
            raise HTTPException(404)
        watchlist.change(path(), index, change(dict(entries[index])))
        runner.reload_watchlist()

    @app.post("/boards/{index}/enabled", dependencies=[Depends(require_csrf)])
    async def toggle_board(request: Request, index: int, enabled: str = Form("")):
        edit(index, lambda e: {**e, "enabled": enabled == "1"} if enabled != "1" else
             {k: v for k, v in e.items() if k != "enabled"})
        return RedirectResponse("/boards?saved=changed", 303)

    @app.post("/boards/{index}/tier", dependencies=[Depends(require_csrf)])
    async def board_tier(request: Request, index: int, tier: str = Form(...)):
        if tier not in runner.cfg.polling.tiers:
            raise HTTPException(400)
        edit(index, lambda e: {**e, "tier": tier})
        return RedirectResponse("/boards?saved=changed", 303)

    @app.post("/boards/{index}/remove", dependencies=[Depends(require_csrf)])
    async def remove_board(request: Request, index: int):
        edit(index, lambda e: None)
        return RedirectResponse("/boards?saved=removed", 303)


def _with_health(row: dict, h, matches: dict, runner, problem: str, counts: dict) -> dict:
    if problem:
        row["state"], row["detail"] = ("needs_key" if problem.startswith("needs") else "failing"), problem
    elif not row["enabled"]:
        row["state"], row["detail"] = "disabled", ""
    elif h is not None and h["failures"]:
        row["state"], row["detail"] = "failing", f"{h['failures']} failures in a row: {(h['last_error'] or '')[:160]}"
    elif h is not None and h["last_ok_at"]:
        row["state"], row["detail"] = "ok", ""
    else:
        row["state"], row["detail"] = "waiting", "not polled yet"
    counts[row["state"]] = counts.get(row["state"], 0) + 1
    row["last_ok"] = ago(h["last_ok_at"]) if h is not None else ""
    row["listed"] = h["last_count"] if h is not None else None
    row["matches"] = matches.get(row["key"], 0)
    row["next"] = until(runner.next_poll.get(row["key"])) if runner.running and row["enabled"] else ""
    return row
