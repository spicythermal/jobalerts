"""The Filter page: what reaches the AI score, what's turned down and why, and the controls.

- The funnel: every job first seen in the last N days, grouped by what decided it (too old, a title
  word, the location, the AI's first look, pay, or a score), each group a link to its jobs.
- The jobs in a group, with the reason, and "Score it anyway" for one the filter turned down.
- The controls: keywords first or AI only, the model for each stage, guidance for the AI's first look,
  and the word lists. "Preview" runs the edited lists over the last 30 days' titles before saving;
  "Try a title" says what the edited filter would do with one title.
Saving goes through the same validation and storage as the Settings pages (settings.UiSettings).
"""
from __future__ import annotations

import math
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from .. import settings as st
from ..config import Criteria
from ..filters import GROUP_LABEL, GROUP_STAGE, GROUPS, JobFilter, group_of
from ..models import utcnow
from ..store import job_from_row
from .boards_pages import ago
from .settings_pages import validate_fields

FIELDS = [f for f in st.FIELDS if f.section == "search"]
SHOW = 100            # jobs listed per group
PREVIEW_DAYS = 30
PREVIEW_SHOW = 40
# AI-only estimate when there's no history to measure: share of titles that pass the first pass, and
# share of all jobs that reach the full score (measured on a week of postings, 2026-11-06)
TITLE_PASS, SCORED = 0.35, 0.17


def funnel(rows) -> dict:
    counts = {g: 0 for g, _, _ in GROUPS}
    baseline = duplicates = 0
    for r in rows:
        if r["status"] == "duplicate":
            duplicates += 1
            continue
        g = group_of(r["status"], r["reason"])
        if g is None:
            baseline += 1
        else:
            counts[g] += 1
    total = sum(counts.values())
    stages = {s: sum(n for g, n in counts.items() if GROUP_STAGE[g] == s) for s in ("out", "wait", "in", "other")}
    return {"groups": [(g, label, stage, counts[g]) for g, label, stage in GROUPS], "total": total,
            "stages": stages, "baseline": baseline, "duplicates": duplicates}


def estimate(runner, counts: dict, days: int) -> dict:
    """Rough AI calls a day in AI-only mode, from the last `days` days of new jobs."""
    f = counts["groups"]
    n = {g: c for g, _, _, c in f}
    judged = counts["total"] - n["too_old"] - n["hard_exclude"] - n["other"]
    per_day = judged / max(days, 1)
    cfg = runner.cfg.prescreen
    titles = math.ceil(per_day / cfg.title_batch) if per_day else 0
    excerpts = math.ceil(per_day * TITLE_PASS / cfg.posting_batch) if per_day else 0
    scores = round(per_day * SCORED)
    usage = runner.store.ai_usage(7)
    used = round(sum(r["calls"] for r in usage) / 7) if usage else 0
    return {"jobs": round(per_day), "titles": titles, "excerpts": excerpts, "scores": scores,
            "total": titles + excerpts + scores, "cap": runner.cfg.scoring.daily_max_calls, "used": used,
            "keyword_scores": round(sum(c for g, _, s, c in f if s == "in") / max(days, 1))}


def decide(job_filter: JobFilter, criteria: Criteria, prescreen_on: bool, job) -> tuple[bool, str]:
    """(reaches the AI, why) for one job under these lists and this mode."""
    if criteria.mode == "ai" and prescreen_on:
        if hit := job_filter.hard_exclude(job):
            return False, f"title excludes '{hit}'"
        return True, "goes to the AI's first look"
    ok, why = job_filter.check_basic(job)
    if ok:
        return True, why
    if prescreen_on and not job_filter.hard_exclude(job):
        return False, f"{why} (the AI second look still sees it)"
    return False, why


def add_filter_routes(app: FastAPI, state, page, require_csrf) -> None:
    runner = state.runner

    def form_rows(submitted: dict | None = None):
        data = runner.cfg.model_dump()
        rows = {}
        for f in FIELDS:
            value = st.get_path(data, f.key)
            shown = st.format_value(f, value) if submitted is None else submitted.get(f.key, "")
            checked = bool(value) if submitted is None else f.key in submitted
            choices = f.choices
            if f.kind == "model" and shown and shown not in dict(choices):
                choices = (*choices, (shown, shown))   # a model set in a config file
            rows[f.key] = {"f": f, "value": shown, "checked": checked, "choices": choices,
                           "source": runner.cfg.value_sources.get(f.key, "default")}
        return rows

    def job_rows(rows, group: str):
        out = []
        for r in rows:
            g = group_of(r["status"], r["reason"])
            if g is None or r["status"] == "duplicate":
                continue
            if (group == "out" and GROUP_STAGE[g] != "out") or (group not in ("out", "all") and g != group):
                continue
            out.append({"r": r, "group": g, "seen": ago(r["first_seen_at"]),
                        "can_score": r["status"] in ("filtered", "stale", "low_pay")})
            if len(out) >= SHOW:
                break
        return out

    def context(days: int, group: str, **extra):
        rows = runner.store.decisions_since(utcnow() - timedelta(days=days))
        counts = funnel(rows)
        cfg = runner.cfg
        return dict(days=days, group=group, counts=counts, group_label=GROUP_LABEL, jobs=job_rows(rows, group),
                    est=estimate(runner, counts, days), fields=form_rows(extra.pop("submitted", None)),
                    mode=cfg.criteria.mode, prescreen_on=cfg.prescreen.enabled, scoring_on=bool(runner.scorer),
                    first_look=bool(runner.prescreener), errors=extra.pop("errors", {}), **extra)

    def draft(submitted: dict) -> tuple[Criteria | None, bool, str]:
        """The criteria as edited in the form (not saved), whether the pre-screen would be on, or an error."""
        values = {}
        for f in FIELDS:
            if not f.key.startswith("criteria."):
                continue
            try:
                values[f.key.removeprefix("criteria.")] = st.parse(f, submitted.get(f.key))
            except st.FieldError as e:
                return None, False, f"{f.label}: {e}"
        try:
            criteria = Criteria.model_validate({**runner.cfg.criteria.model_dump(), **values})
        except ValidationError as e:
            return None, False, str(e.errors()[0]["msg"])
        return criteria, "prescreen.enabled" in submitted and bool(runner.scorer), ""

    async def submitted_form(request: Request) -> dict[str, str]:
        form = await request.form()
        return {k: v for k, v in form.items() if isinstance(v, str)}

    @app.get("/filter")
    async def filter_page(request: Request, days: int = 7, group: str = "out", saved: str = ""):
        days = min(max(days, 1), 90)
        if group not in GROUP_LABEL and group not in ("out", "all"):
            group = "out"
        return page(request, "filter.html", **context(days, group, saved=saved))

    @app.post("/filter/save", dependencies=[Depends(require_csrf)])
    async def save(request: Request):
        submitted = await submitted_form(request)
        errors, values, changed, new_cfg = validate_fields(runner, FIELDS, submitted)
        if not errors and values.get("criteria.mode") == "ai" and not values.get("prescreen.enabled"):
            errors["prescreen.enabled"] = "AI-only mode needs the AI first look on."
        if errors:
            return page(request, "filter.html", **context(7, "out", saved="", submitted=submitted, errors=errors))
        runner.ui_settings.save(changed)
        runner.reconfigure(new_cfg)
        return RedirectResponse("/filter?saved=1", 303)

    @app.post("/filter/reset", dependencies=[Depends(require_csrf)])
    async def reset(request: Request):
        runner.ui_settings.reset([f.key for f in FIELDS])
        runner.reconfigure(st.apply(runner.base_cfg, runner.ui_settings.overlay()))
        return RedirectResponse("/filter?saved=reset", 303)

    @app.post("/filter/preview", dependencies=[Depends(require_csrf)])
    async def preview(request: Request):
        submitted = await submitted_form(request)
        criteria, prescreen_on, error = draft(submitted)
        if criteria is None:
            return page(request, "filter_preview.html", error=error)
        new_filter = JobFilter(criteria)
        cur_cfg = runner.cfg
        now_in, now_out, total, passing_now, passing_new = [], [], 0, 0, 0
        for r in runner.store.decisions_since(utcnow() - timedelta(days=PREVIEW_DAYS)):
            if r["status"] in ("duplicate", "stale"):
                continue
            job = job_from_row(r)
            total += 1
            before, _ = decide(runner.filter, cur_cfg.criteria, bool(runner.prescreener), job)
            after, why = decide(new_filter, criteria, prescreen_on, job)
            passing_now += before
            passing_new += after
            if after and not before:
                now_in.append({"r": r, "why": why, "can_score": r["status"] in ("filtered", "low_pay")})
            elif before and not after:
                now_out.append({"r": r, "why": why})
        return page(request, "filter_preview.html", error="", total=total, days=PREVIEW_DAYS,
                    passing_now=passing_now, passing_new=passing_new, now_in=now_in[:PREVIEW_SHOW],
                    now_out=now_out[:PREVIEW_SHOW], n_in=len(now_in), n_out=len(now_out),
                    ai_mode=criteria.mode == "ai" and prescreen_on)

    @app.post("/filter/try", dependencies=[Depends(require_csrf)])
    async def try_title(request: Request):
        submitted = await submitted_form(request)
        title = submitted.get("try_title", "").strip()
        if not title:
            return page(request, "filter_try.html", error="Type a job title.", ok=False, why="", title="")
        criteria, prescreen_on, error = draft(submitted)
        if criteria is None:
            return page(request, "filter_try.html", error=error, ok=False, why="", title=title)
        from ..models import Job
        job = Job("try", "try", "1", "", title, "", location=submitted.get("try_location", "").strip(),
                  remote="try_remote" in submitted)
        ok, why = decide(JobFilter(criteria), criteria, prescreen_on, job)
        return page(request, "filter_try.html", error="", ok=ok, why=why, title=title)

    @app.post("/filter/score/{key:path}", dependencies=[Depends(require_csrf)])
    async def score_anyway(request: Request, key: str):
        if runner.store.get_job(key) is None:
            raise HTTPException(404)
        result = await runner.score_anyway(key)
        row = runner.store.get_job(key)
        return page(request, "filter_scored.html", result=result, r=row)
