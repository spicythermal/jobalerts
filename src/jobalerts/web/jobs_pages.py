"""The Jobs page (the web UI's home, where alerts land) and each job's own page.

Tabs: Recommended (AI fit at or above the digest threshold, or not scored yet), Step-ups (the AI's
promotion score), Saved, Applied and All recent. Save / Dismiss / Applied are kept per job
(`user_state`); dismissed jobs leave every list but All recent, and Home Assistant's top list.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import col
from ..dashboard import job_row, rank
from ..models import utcnow
from ..store import job_from_row

TABS = [("recommended", "Recommended"), ("step_ups", "Step-ups"), ("saved", "Saved"), ("applied", "Applied"),
        ("all", "All recent")]
STATES = {"saved": "Saved", "dismissed": "Dismissed", "applied": "Applied"}
STATUS_TEXT = {"alerted": "pushed", "alert": "pushed", "digest": "in the digest", "digested": "in the digest",
               "notify_failed": "push failed", "baseline": "open at first poll", "low": "low fit",
               "low_pay": "pay under your floor"}
LIMIT = 100


def _number(text: str) -> float | None:
    try:
        return float(text) if text.strip() else None
    except ValueError:
        return None


def add_job_routes(app: FastAPI, state, page, require_csrf) -> None:
    runner = state.runner

    def rows_for(tab: str, days: int) -> list[dict]:
        store, cfg = runner.store, runner.cfg
        if tab in ("saved", "applied"):
            raw = store.jobs_by_user_state(tab)
        elif tab == "all":
            raw = store.jobs_since(utcnow() - timedelta(days=days))
        else:
            raw = store.recent_candidates(utcnow() - timedelta(days=days))
        out = []
        for r in raw:
            if tab != "all" and r["user_state"] == "dismissed":
                continue
            if r["status"] == "baseline" and tab != "saved" and tab != "applied" \
                    and not runner.filter.check_basic(job_from_row(r))[0]:
                continue
            out.append(job_row(r, runner.pay, cfg.web.public_url))
        if tab == "recommended":
            floor = cfg.scoring.digest_threshold
            return rank([m for m in out if m["score"] is None or m["score"] >= floor])
        if tab == "step_ups":
            return rank(out, "step_up")
        return sorted(out, key=lambda m: m["posted"] or "", reverse=True)

    @app.get("/")
    async def jobs_page(request: Request, tab: str = "recommended", days: int = 14, min_score: str = "",
                        min_pay: str = "", q: str = "", remote: str = ""):
        if tab not in dict(TABS):
            tab = "recommended"
        days = max(1, min(days, 365))
        rows = rows_for(tab, days)
        score_floor, pay_floor = _number(min_score), _number(min_pay)
        words = q.lower().split()
        if score_floor is not None:
            rows = [m for m in rows if m["score"] is not None and m["score"] >= score_floor]
        if pay_floor is not None:
            rows = [m for m in rows if max(m["pay_high"] or 0, m["buy_high"] or 0) >= pay_floor]
        if words:
            rows = [m for m in rows if all(w in f"{m['title']} {m['company']} {m['location']}".lower() for w in words)]
        if remote:
            rows = [m for m in rows if m["remote"] or "remote" in m["location"].lower()]
        return page(request, "jobs.html", tabs=TABS, tab=tab, rows=rows[:LIMIT], total=len(rows), limit=LIMIT,
                    days=days, min_score=min_score, min_pay=min_pay, q=q, remote=bool(remote), states=STATES,
                    status_text=STATUS_TEXT, report=getattr(runner, "catchup_report", lambda: None)())

    @app.get("/jobs/{key:path}")
    async def job_page(request: Request, key: str):
        r = runner.store.get_job(key)
        if r is None:
            raise HTTPException(404)
        job = job_row(r, runner.pay, runner.cfg.web.public_url)
        explain = []
        if runner.pay and (pay := col.parse_salary(r["salary"] or "")):
            if result := runner.pay.index(pay, r["location"] or "", bool(r["remote"])):
                explain = runner.pay.explain(result)
        return page(request, "job.html", job=job, explain=explain, states=STATES, status_text=STATUS_TEXT)

    @app.post("/jobs/{key:path}/state", dependencies=[Depends(require_csrf)])
    async def set_state(request: Request, key: str, state_: str = Form("", alias="state"), where: str = Form("page")):
        if state_ not in STATES and state_ != "clear":
            raise HTTPException(400)
        if not runner.store.set_user_state(key, None if state_ == "clear" else state_):
            raise HTTPException(404)
        runner._dashboard_stale.set()   # Home Assistant's top list drops dismissed jobs
        if "hx-request" not in request.headers:
            return RedirectResponse(f"/jobs/{key}" if where == "page" else "/", 303)
        if where == "list" and state_ == "dismissed":
            return HTMLResponse("")     # the card leaves the list
        job = job_row(runner.store.get_job(key), runner.pay, runner.cfg.web.public_url)
        return page(request, "job_actions.html", job=job, states=STATES, where=where)
