"""The Stories page: your work stories, which the AI judges every job against.

List (title, employer, dates, role, how many recent jobs each story was among the best three for),
an editor with the front-matter fields and the body, a live "what the AI sees" preview, new,
duplicate and delete (moved to a .trash folder). Saving rebuilds the AI scorer at once; scores made
before the change can be redone in the background ("re-score"), within the day's AI budget.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import stories as st
from ..models import utcnow
from ..scorer import ai_cost_usd

log = logging.getLogger(__name__)
TEXT_FIELDS = ("title", "employer", "dates")


def add_story_routes(app: FastAPI, state, page, require_csrf) -> None:
    runner = state.runner
    rescores: dict[int, dict] = {}     # user id -> progress of their re-score

    def rescore_state(me) -> dict:
        return rescores.setdefault(me.uid, {"running": False, "done": 0, "total": 0, "message": ""})

    def folder(me):
        return me.cfg.resolve(me.cfg.scoring.stories_dir)

    def usage(me, days: int = 30) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in me.store.scored_since(utcnow() - timedelta(days=days)):
            for sid, _ in json.loads(r["top_stories"] or "[]"):
                counts[sid] = counts.get(sid, 0) + 1
        return counts

    def cost_per_call(me) -> float:
        rows = me.store.ai_usage(days=30)
        calls = sum(r["calls"] for r in rows)
        return ai_cost_usd(rows, me.cfg.scoring.model) / calls if calls else 0.002

    def rescore_info(me) -> dict:
        todo = me.rescore_todo()
        return {**rescore_state(me), "todo": todo, "cost": todo * cost_per_call(me), "scoring": me.scorer is not None,
                "days": me.cfg.criteria.max_age_days}

    def form_story(form) -> tuple[dict, str]:
        meta = {}
        for key in TEXT_FIELDS:
            if (value := (form.get(key) or "").strip()):
                meta[key] = value
        if form.get("role") in st.ROLES:
            meta["role"] = form["role"]
        for key in st.LIST_FIELDS:
            items = [line.strip() for line in (form.get(key) or "").splitlines() if line.strip()]
            if items:
                meta[key] = items
        if form.get("public") in st.PUBLIC:
            meta["public"] = form["public"]
        return meta, (form.get("body") or "").replace("\r\n", "\n")

    def editor(request, story_id: str, meta: dict, body: str, new: bool, error: str = "", saved: str = ""):
        me = state.me(request)
        preview = st.story_text(meta, body)[:st.MAX_CHARS]
        lists = {k: "\n".join(str(v) for v in (meta.get(k) or [])) for k in st.LIST_FIELDS}
        return page(request, "story.html", story_id=story_id, meta=meta, body=body, lists=lists, new=new,
                    preview=preview, error=error, saved=saved, roles=st.ROLES, public=st.PUBLIC,
                    uses=usage(me).get(story_id, 0))

    def after_change(me) -> None:
        runner.refresh(me.uid)     # the scorer reads the stories again (new score basis)

    @app.get("/stories")
    async def stories_page(request: Request, saved: str = ""):
        me = state.me(request)
        counts = usage(me)
        rows = st.list_stories(folder(me))
        rows.sort(key=lambda r: (-counts.get(r["id"], 0), r["title"].lower()))
        return page(request, "stories.html", rows=rows, counts=counts, saved=saved, folder=str(folder(me)),
                    rescore=rescore_info(me))

    @app.get("/stories/new")
    async def new_story(request: Request):
        return editor(request, "", {"role": "lead"}, st.TEMPLATE, new=True)

    @app.post("/stories/new", dependencies=[Depends(require_csrf)])
    async def create_story(request: Request):
        form = await request.form()
        meta, body = form_story(form)
        if not meta.get("title"):
            return editor(request, "", meta, body, new=True, error="A story needs a title.")
        story_id = (form.get("id") or "").strip() or st.slugify(meta["title"])
        me = state.me(request)
        path = st.story_path(folder(me), story_id)
        if path is None:
            return editor(request, "", meta, body, new=True, error="The file name may only have a-z, 0-9 and dashes.")
        if path.exists():
            return editor(request, "", meta, body, new=True, error=f"There is already a story called {story_id}.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(st.dump_story({"id": story_id, **meta}, body), encoding="utf-8")
        after_change(me)
        return RedirectResponse(f"/stories/{story_id}?saved=1", 303)

    @app.post("/stories/preview", dependencies=[Depends(require_csrf)])
    async def preview_story(request: Request):
        meta, body = form_story(await request.form())
        return page(request, "story_preview.html", preview=st.story_text(meta, body)[:st.MAX_CHARS])

    @app.get("/stories/rescore")
    async def rescore_status(request: Request):
        return page(request, "story_rescore.html", rescore=rescore_info(state.me(request)))

    @app.post("/stories/rescore", dependencies=[Depends(require_csrf)])
    async def start_rescore(request: Request):
        me = state.me(request)
        progress_of = rescore_state(me)
        if not progress_of["running"] and me.scorer:
            progress_of.update(running=True, done=0, total=me.rescore_todo(), message="")

            def progress(n, total):
                progress_of.update(done=n, total=total)

            async def go():
                try:
                    done, left = await me.rescore(runner.active_boards() or runner.cfg.boards(), progress=progress)
                    progress_of["message"] = (f"Re-scored {len(done)} jobs." + (
                        f" {left} left for tomorrow (today's AI budget is kept for new jobs)." if left else ""))
                except Exception as e:  # e.g. Claude unreachable
                    log.exception("Re-score from the Stories page failed")
                    progress_of["message"] = f"Stopped: {type(e).__name__}"
                finally:
                    progress_of["running"] = False
            asyncio.create_task(go(), name=f"rescore-{me.uid}")
        return page(request, "story_rescore.html", rescore=rescore_info(me))

    @app.get("/stories/{story_id}")
    async def edit_story(request: Request, story_id: str, saved: str = ""):
        path = st.story_path(folder(state.me(request)), story_id)
        if path is None or not path.is_file():
            raise HTTPException(404)
        meta, body, _ = st.read_story(path)
        return editor(request, story_id, meta, body, new=False, saved=saved)

    @app.post("/stories/{story_id}", dependencies=[Depends(require_csrf)])
    async def save_story(request: Request, story_id: str):
        me = state.me(request)
        path = st.story_path(folder(me), story_id)
        if path is None or not path.is_file():
            raise HTTPException(404)
        old_meta, _, original = st.read_story(path)
        meta, body = form_story(await request.form())
        if not meta.get("title"):
            return editor(request, story_id, meta, body, new=False, error="A story needs a title.")
        # Keep what the form doesn't show (id, your own extra fields) in its place.
        merged = {k: v for k, v in old_meta.items() if k not in st.FACTS + TEXT_FIELDS + st.LIST_FIELDS + ("public",)}
        merged.update(meta)
        order = list(old_meta) + [k for k in merged if k not in old_meta]
        merged = {k: merged[k] for k in order if k in merged}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(st.dump_story(merged, body, original), encoding="utf-8")
        tmp.replace(path)
        after_change(me)
        return RedirectResponse(f"/stories/{story_id}?saved=1", 303)

    @app.post("/stories/{story_id}/duplicate", dependencies=[Depends(require_csrf)])
    async def duplicate_story(request: Request, story_id: str):
        me = state.me(request)
        path = st.story_path(folder(me), story_id)
        if path is None or not path.is_file():
            raise HTTPException(404)
        meta, body, _ = st.read_story(path)
        n, new_id = 2, f"{story_id}-copy"
        while (folder(me) / f"{new_id}.md").exists():
            new_id, n = f"{story_id}-copy-{n}", n + 1
        meta = {**meta, "id": new_id, "title": f"{meta.get('title', story_id)} (copy)"}
        (folder(me) / f"{new_id}.md").write_text(st.dump_story(meta, body), encoding="utf-8")
        after_change(me)
        return RedirectResponse(f"/stories/{new_id}?saved=copied", 303)

    @app.post("/stories/{story_id}/delete", dependencies=[Depends(require_csrf)])
    async def delete_story(request: Request, story_id: str):
        me = state.me(request)
        path = st.story_path(folder(me), story_id)
        if path is None or not path.is_file():
            raise HTTPException(404)
        trash = folder(me) / ".trash"
        trash.mkdir(exist_ok=True)
        shutil.move(str(path), trash / path.name)
        after_change(me)
        return RedirectResponse("/stories?saved=deleted", 303)
