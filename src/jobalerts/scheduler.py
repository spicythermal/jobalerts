"""The pipeline (fetch -> dedupe -> filter -> score -> route -> notify) and the polling loops."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import col, colmap, discover, runstate, updater
from .dashboard import MqttPublisher, ai_cost, job_link, top_matches
from .config import Board, Config
from .filters import JobFilter
from .http import HttpClient, posting_gone
from .models import Job, normalize_company, parse_dt, utcnow
from .notify import Notifier
from .prescreen import Prescreener
from .scorer import FitScore, Scorer
from .sources import SOURCES, Source, SourceConfigError, without_missing_keys
from .store import Store, job_from_row, stories_from_row
from .taxes import Taxes
from .users import OWNER

log = logging.getLogger(__name__)

STEP_UP_SHOWN = 60  # step-up scores from here on are shown with the job (the scorer's "real step up")


@dataclass
class PollResult:
    board: str
    fetched: int = 0
    new: int = 0
    baseline: bool = False
    alerted: int = 0
    matched: list[Job] = field(default_factory=list)      # matched for at least one user
    matched_by: dict[int, int] = field(default_factory=dict)  # user id -> matches


def _pay_cols(pay: col.PayResult | None, score: FitScore | None = None) -> dict:
    if pay is None:
        return {"pay_note": col.NO_PAY} if score is not None else {}
    cols = {"pay_index": round(pay.index, 3), "pay_low": round(pay.low, 3), "pay_high": round(pay.high, 3),
            "pay_note": pay.text()}
    if pay.buy_low is not None:
        cols |= {"buy_index": round(pay.buy_index, 3), "buy_low": round(pay.buy_low, 3),
                 "buy_high": round(pay.buy_high, 3)}
    return cols


def _score_cols(score: FitScore | None) -> dict:
    if score is None:
        return {}
    return {"score": score.score, "score_summary": score.summary,
            "red_flags": "; ".join(score.red_flags) or None, "best_resume": score.best_resume or None,
            "step_up": score.step_up, "step_up_note": score.step_up_note or None,
            "top_stories": json.dumps(list(zip(score.top_stories, score.top_story_titles))) if score.top_stories else None,
            "score_basis": score.basis or None}


AI_ONLY_WAITING = "AI only: waiting for the AI's first look"


# Runner attributes that are user 1's (Person) – the single user before accounts, and the CLI's.
FIRST_USERS = {"backfill", "reprice", "rescore", "rescore_todo", "run_prescreen", "route", "pay_index", "needs_move",
               "pay_floor", "load_pay", "_pay_paths", "_cap_notice", "_notify", "preview_board", "catchup_report",
               "score_anyway", "digest_missed", "digest_due", "top_matches", "map_enabled", "refresh_map", "current_map",
               "map_data", "map_page", "send_digest", "_resolve_suggestions", "_finish", "process",
               "_backfill_budget_spent", "_pay_too_low", "key_source"}


def _first_users(name: str) -> property:
    return property(lambda self: getattr(self.me, name), lambda self, value: setattr(self.me, name, value))


class Person:
    """One user's pipeline: their settings, keyword filter, pay index, AI scorer and pre-screen,
    notifications, digest and map. Every board is polled once (Runner); each new job then goes
    through each user's Person, which writes that user's row (store.user(uid))."""

    SHARED = {"http", "_dashboard_stale", "_prescreen_wake", "watched", "active_boards", "update_status", "running"}

    def __init__(self, runner: Runner, uid: int, cfg: Config, notifier: Notifier, scorer: Scorer | None = None,
                 prescreener: Prescreener | None = None, admin: bool = True):
        self.runner = runner
        self.uid = uid
        self.admin = admin
        self.cfg = cfg
        self.store = runner.store.user(uid)
        self.notifier = notifier
        self.scorer = scorer
        self.prescreener = prescreener if scorer else None  # its "yes" jobs need the full score
        self.filter = JobFilter(cfg.criteria)
        self.map_html: str | None = None    # the latest cost-of-living map page (web UI /map)
        self.map_html_shared: str | None = None  # the same for the read-only Home Assistant link
        self._map_built = 0.0
        self._cap_noticed = self.store.get_meta(runstate.CAP_NOTICED) or ""  # UTC day of the last "AI daily cap reached" push
        self._collecting: list | None = None       # during a catch-up pass: (job, score) instead of pushes
        self.pay: col.PayIndex | None = None
        self.pay_problem = ""  # shown in the digest until fixed
        self.load_pay()

    @property
    def base_cfg(self) -> Config:
        """This user's settings before the Settings page's (see Runner.personal_base)."""
        return self.runner.personal_base(self.uid)

    @property
    def ui_settings(self):
        """settings.UiSettings with this user's (and the install's) Settings-page values."""
        return self.runner.settings_for(self.uid)

    def __getattr__(self, name: str):
        if name in Person.SHARED:     # the service's, not this user's
            return getattr(self.runner, name)
        raise AttributeError(name)

    def reconfigure(self, cfg: Config) -> None:
        """New settings for this user: filter, notifications, pay index, AI scoring."""
        self.cfg = cfg
        self.filter = JobFilter(cfg.criteria)
        self.notifier.cfg = cfg.notify
        self.pay, self.pay_problem = None, ""
        self.load_pay()
        self.rebuild_services()
        if self.prescreener and cfg.criteria.mode == "ai":
            self._prescreen_wake.set()
        self.map_html = None

    def rebuild_services(self) -> None:
        """The AI scorer and pre-screen from this user's profile, stories, resumes and key."""
        rebuild = self.runner.rebuild
        if not rebuild:
            return
        if self.uid == OWNER:
            self.scorer, prescreener, self.runner.homeassistant = rebuild(self.cfg, self.store)
        else:
            key, shared = self.runner.anthropic_key(self.uid, self.admin, self.cfg.users.share_keys)
            self.store.shared_key, self.store.shared_cap = shared, self.cfg.users.shared_daily_max_calls
            self.scorer, prescreener, _ = rebuild(self.cfg, self.store, api_key=key, homeassistant=False)
        self.prescreener = prescreener if self.scorer else None

    @property
    def key_source(self) -> str:
        """Whose Anthropic key this user's AI calls use: "own", "shared" (the install's), "install" (an
        admin's use of the install's) or "" (none)."""
        if not self.runner.vault:
            return "install" if self.scorer else ""
        if self.uid != OWNER and self.runner.vault.get("ANTHROPIC_API_KEY", self.uid):
            return "own"
        key, shared = self.runner.anthropic_key(self.uid, self.admin, self.cfg.users.share_keys)
        return ("shared" if shared else "install") if key else ""

    # ------------------------------------------------------------------ pipeline
    async def process(self, job: Job, board: Board, source: Source,
                      check_duplicate: bool = False) -> tuple[str, FitScore | None]:
        """This user's decision on a new job (the posting is stored already)."""
        if check_duplicate and self.store.seen_elsewhere(job):
            self.store.update_job(job.key, status="duplicate", reason="already seen from another source")
            return "duplicate", None
        if self.filter.is_stale(job):
            self.store.insert_job(job, "stale", f"published {job.age_days():.0f} days ago")
            return "stale", None
        if self.cfg.criteria.mode == "ai" and self.prescreener:
            # AI only: the hard excludes still apply; everything else gets the AI's first look in batches
            if hit := self.filter.hard_exclude(job):
                self.store.insert_job(job, "filtered", f"title excludes '{hit}'")
                return "filtered", None
            self.store.hold_for_prescreen(job, AI_ONLY_WAITING)
            self._prescreen_wake.set()
            return "prescreen", None
        ok, why = self.filter.check_basic(job)
        if not ok:
            if self.prescreener and not self.filter.hard_exclude(job):
                self.store.hold_for_prescreen(job, why)  # the AI pre-screen takes a second look
                return "prescreen", None
            self.store.insert_job(job, "filtered", why)
            return "filtered", None

        self.store.insert_job(job, "processing", why)
        return await self._finish(job, board, source)

    async def _finish(self, job: Job, board: Board, source: Source) -> tuple[str, FitScore | None]:
        """Everything after the title check: description, pay gate, AI score, routing. The job's row exists."""
        if not board.watched and normalize_company(job.company) not in self.watched:
            self.store.add_suggestion(job.company)

        if self.cfg.intent.move == "stay" and self.needs_move(job):
            self.store.update_job(job.key, status="filtered", reason="needs a move (Settings: stay where you live)")
            return "filtered", None

        if not job.description and (self.scorer or self.pay or self.filter.description_exclude):
            try:
                await source.enrich(job, board, self.http)
            except Exception as e:
                if posting_gone(e):  # taken down before we got to it: not worth an AI call
                    self.store.update_job(job.key, status="closed",
                                          reason=f"posting taken down (HTTP {e.response.status_code})")
                    return "closed", None
                log.warning("%s: couldn't fetch description for %s: %s", board.key, job.key, e)
            if self.filter.is_stale(job):  # enrichment can replace an approximate date with the real one
                self.store.update_job(job.key, status="stale", reason="published date after enrichment")
                return "stale", None
        ok, why_desc = self.filter.check_description(job)
        if not ok:
            self.store.update_job(job.key, status="filtered", reason=why_desc)
            return "filtered", None

        # Pay gate before AI scoring: a posting whose stated pay is under pay.min_index even at the top of
        # its range isn't worth a Claude call. No stated pay -> scored anyway; the AI may find it.
        pay = self.pay_index(job)
        if self._pay_too_low(job, pay):
            return "low_pay", None

        score = await self.scorer.score(job) if self.scorer else None
        status = self.route(score.score, score.step_up) if score else self.route(None)
        if score is None and self.scorer and self.scorer.capped():
            status = "digest"  # the day's AI budget is spent: no unscored pushes, it waits in the digest
            await self._cap_notice()
        if pay is None and score is not None:  # pay the AI read from the posting
            pay = self.pay_index(job, score)
            if self._pay_too_low(job, pay, score):
                return "low_pay", score
        self.store.update_job(job.key, status=status, **_pay_cols(pay, score), **_score_cols(score))
        return status, score

    async def run_prescreen(self, boards: list[Board]) -> dict[str, int]:
        """Second look at the jobs waiting in the pre-screen queue (see prescreen.py). Clear misfits by
        title, then by description excerpt, are filtered; the rest go through the normal scoring and routing.
        If Claude can't be reached the jobs keep waiting for the next run. Returns counts per outcome."""
        counts: dict[str, int] = {}

        def count(what: str, n: int = 1) -> None:
            counts[what] = counts.get(what, 0) + n

        if not self.prescreener:
            return counts
        by_slug = {(b.source, b.slug): b for b in boards}
        waiting = []
        for row in self.store.prescreen_queue():
            job = job_from_row(row)
            if self.filter.is_stale(job):
                self.store.update_job(job.key, status="stale", reason="waited too long for the pre-screen")
                count("stale")
            else:
                waiting.append(job)
        if not waiting:
            return counts

        cfg = self.cfg.prescreen
        closer: list[Job] = []
        for i in range(0, len(waiting), cfg.title_batch):
            group = waiting[i:i + cfg.title_batch]
            misfits = await self.prescreener.clear_misfits(group)
            if misfits is None:
                count("waiting", len(waiting) - i)
                waiting = waiting[:i]
                break
            for n, job in enumerate(group):
                if n in misfits:
                    self.store.update_job(job.key, status="filtered", reason="pre-screen: title")
                    count("title_no")
                else:
                    closer.append(job)

        for job in closer:
            board = by_slug.get((job.source, job.board))
            if not job.description and board is not None:
                try:
                    await SOURCES[job.source].enrich(job, board, self.http)
                except Exception as e:
                    log.warning("pre-screen: couldn't fetch the description of %s: %s", job.key, e)

        alerts_left = self.cfg.polling.max_alerts_per_poll
        for i in range(0, len(closer), cfg.posting_batch):
            group = closer[i:i + cfg.posting_batch]
            verdicts = await self.prescreener.judge(group)
            if verdicts is None:
                count("waiting", len(closer) - i)
                break
            for n, job in enumerate(group):
                v = verdicts[n]
                if v.verdict == "no":
                    self.store.update_job(job.key, status="filtered", reason=f"pre-screen: {v.reason}"[:200])
                    count("posting_no")
                    continue
                board = by_slug.get((job.source, job.board))
                if board is None:  # board removed from the config since the job was queued
                    self.store.update_job(job.key, status="filtered", reason="pre-screen: board no longer watched")
                    continue
                self.store.update_job(job.key, status="processing", reason=f"pre-screen: {v.reason}"[:200])
                try:
                    status, score = await self._finish(job, board, SOURCES[job.source])
                except Exception as e:
                    log.exception("pre-screen: failed processing %s", job.key)
                    self.store.update_job(job.key, status="error", reason=str(e)[:200])
                    continue
                count(status)
                if status == "alert":
                    if alerts_left > 0:
                        alerts_left -= 1
                        await self._notify(job, score)
                    else:
                        self.store.update_job(job.key, status="digest", reason="per-run alert cap reached")
        if any(counts.get(s) for s in ("alert", "digest", "low")):
            self._dashboard_stale.set()
        if counts:
            log.info("pre-screen: %s", ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
        return counts

    def route(self, score: int | None, step_up: int | None = None) -> str:
        """alert, digest or low from the AI's fit score, or its step-up score for a credible promotion."""
        cfg = self.cfg.scoring
        if score is None:
            return "alert"  # keyword match and no score available: better to over-notify than miss it
        if score >= cfg.push_threshold or (cfg.step_up_threshold and (step_up or 0) >= cfg.step_up_threshold):
            return "alert"
        return "digest" if score >= cfg.digest_threshold else "low"

    def pay_index(self, job: Job, score: FitScore | None = None) -> col.PayResult | None:
        """Cost-of-living pay index from the posting's salary field, else the pay stated in its description,
        else (given a score) the pay the AI read."""
        if not self.pay:
            return None
        pay = col.parse_salary(job.salary) or col.pay_in_description(job.description) or (score.pay() if score else None)
        return self.pay.index(pay, job.location, job.remote)

    def rescore_todo(self, days: int | None = None) -> int:
        """How many scored matches a rescore would score again (their score was made with other
        stories, profile, resumes or model)."""
        if not self.scorer:
            return 0
        since = utcnow() - timedelta(days=days or self.cfg.criteria.max_age_days)
        return sum(r["score_basis"] != self.scorer.basis for r in self.store.scored_since(since))

    def needs_move(self, job: Job) -> bool | None:
        """Would taking the job mean moving? Another metro than home (or another state or country when
        home isn't a metro); remote jobs don't, unless they name a foreign country. None if unknown."""
        if not self.pay:
            return None
        place = self.pay.resolve(job.location or "", job.remote)
        home = self.pay.home
        if place is None or home is None:
            return None
        if home.metro:
            return place.metro != home.metro
        if home.state:
            return place.state != home.state
        return place.country != home.country

    def pay_floor(self, job: Job) -> float:
        """pay.min_index, or pay.move_min_index for a job that needs a move when you're open to moving."""
        floor = self.cfg.pay.min_index
        if self.cfg.intent.move == "open" and self.needs_move(job):
            return max(floor, self.cfg.pay.move_min_index)
        return floor

    def _pay_too_low(self, job: Job, pay: col.PayResult | None, score: FitScore | None = None) -> bool:
        """Stores the job as low_pay (listed in the digest, never pushed) when even the top of its range
        is under its pay floor (pay.min_index, higher for a move)."""
        floor = self.pay_floor(job)
        if pay is None or pay.best_high >= floor:
            return False
        what = "move" if floor > self.cfg.pay.min_index else "range"
        self.store.update_job(job.key, status="low_pay", reason=f"top of {what} {pay.best_high:.2f} < {floor:g}",
                              **_pay_cols(pay, score), **_score_cols(score))
        return True

    async def backfill(self, boards: list[Board], push: bool = False,
                       days: int | None = None) -> list[tuple[Job, str, FitScore | None]]:
        """Run the jobs that were already open when their board was first polled (the silent baseline)
        through the same filter -> score -> route steps as a job found live. Only jobs posted within
        max_age_days (or `days`) are considered, and each is done once. Without `push`, strong fits go
        to the digest instead of the phone."""
        by_slug = {(b.source, b.slug): b for b in boards}
        days = days or self.cfg.criteria.max_age_days
        since = utcnow() - timedelta(days=days)
        alerts_left = self.cfg.polling.max_alerts_per_poll
        results = []
        for row in self.store.recent_candidates(since):
            if row["status"] != "baseline":
                continue
            job = job_from_row(row)
            board = by_slug.get((job.source, job.board))
            if board is None or (job.age_days() or 0) > days or not self.filter.check_basic(job)[0]:
                continue
            if self._backfill_budget_spent():
                log.warning("backfill: stopping with %d of today's AI calls kept for new jobs; run it again after "
                            "00:00 UTC to score the rest", self.cfg.scoring.backfill_reserve)
                break
            try:
                status, score = await self.process(job, board, SOURCES[job.source], check_duplicate=True)
            except Exception as e:
                log.exception("backfill: failed processing %s", job.key)
                self.store.update_job(job.key, status="error", reason=str(e)[:200])
                continue
            if status == "alert" and score is None:
                # Couldn't score (bad key, API down, daily cap): leave it for the next backfill to retry.
                self.store.update_job(job.key, status="baseline", reason="backfill: not scored")
                status = "unscored"
            elif status == "alert":
                if push and alerts_left > 0:
                    alerts_left -= 1
                    status = "alerted" if await self._notify(job, score) else "notify_failed"
                else:
                    status = "digest"
                    self.store.update_job(job.key, status=status, reason="baseline backfill, not pushed")
            results.append((job, status, score))
        if results:
            self._dashboard_stale.set()
        return results

    async def reprice(self, boards: list[Board], store: Store | None = None, write: bool = True,
                      days: int | None = None) -> list[tuple[Job, str, str, col.PayResult | None, bool]]:
        """Re-check the pay index of matches that already went through the pipeline, posted within
        max_age_days (or `days`). Pay comes from the salary field or the re-fetched description; when neither
        states it, the job is AI-scored again (once: afterwards its pay_note is col.NO_PAY) so the AI
        can read it, and the new score replaces the old. Only with `write`: a dry run makes no AI calls.
        Under pay.min_index -> low_pay. A low_pay job that now passes goes back to where its AI score
        puts it (digest or low, never pushed), or to baseline for `backfill` to score if it had none.
        Returns (job, old status, new status, pay index, re-scored)."""
        if not self.pay:
            return []
        store = store or self.store
        by_slug = {(b.source, b.slug): b for b in boards}
        since = utcnow() - timedelta(days=days or self.cfg.criteria.max_age_days)
        out = []
        for row in store.processed_since(since):
            job = job_from_row(row)
            board = by_slug.get((job.source, job.board))
            if board is not None:
                try:
                    await SOURCES[job.source].enrich(job, board, self.http)
                except Exception as e:
                    if posting_gone(e):  # filled or withdrawn: off the dashboard and out of the digest
                        log.info("reprice: %s was taken down (HTTP %s)", job.key, e.response.status_code)
                        if write:
                            store.update_job(job.key, status="closed",
                                             reason=f"posting taken down (HTTP {e.response.status_code})")
                        out.append((job, row["status"], "closed", None, False))
                        continue
                    log.warning("reprice: couldn't fetch %s: %s", job.key, e)
            pay = self.pay_index(job)
            fit = None
            asked = row["pay_index"] is not None or row["pay_note"] == col.NO_PAY  # the AI already read the pay
            if pay is None and not asked and write and self.scorer:
                try:
                    fit = await self.scorer.score(job)
                except Exception as e:  # e.g. no API key: leave this one for the next run
                    log.warning("reprice: couldn't AI-score %s: %s", job.key, e)
                pay = self.pay_index(job, fit) if fit else None
            old = status = row["status"]
            score = fit.score if fit else row["score"]
            if pay:
                high = pay.best_high
            else:  # the pay the AI read earlier, if that's all there is
                high = max((v for v in (row["pay_high"], row["buy_high"]) if v is not None), default=None)
            step_up = fit.step_up if fit else row["step_up"]
            floor = self.pay_floor(job)
            if high is not None and high < floor:
                status = "low_pay"
            elif old == "low_pay":  # back where its AI score puts it, never pushed
                status = "baseline" if score is None else self.route(score, step_up).replace("alert", "digest")
            if write:
                reason = (f"top of range {high:.2f} < {floor:g}" if status == "low_pay"
                          else "repriced" if status != old else None)
                store.update_job(job.key, status=status, reason=reason, **_pay_cols(pay, fit), **_score_cols(fit))
            out.append((job, old, status, pay, fit is not None))
        if out and write:
            self._dashboard_stale.set()
        return out

    def _backfill_budget_spent(self) -> bool:
        """True when backfill / rescore should stop: only scoring.backfill_reserve calls are left today."""
        return bool(self.scorer) and self.scorer.calls_left() <= self.cfg.scoring.backfill_reserve

    async def rescore(self, boards: list[Board], days: int | None = None,
                      progress=None) -> tuple[list[tuple[Job, str, str, FitScore | None]], int]:
        """AI-score again the matches already scored and posted within max_age_days (or `days`), e.g. after
        the work stories or profile changed. The new score, summary and top stories replace the old ones and
        the job moves between digest and low to match; it is never pushed, and a job already delivered
        (pushed or in a digest) keeps its status. Pay is left alone: reprice handles it.
        Jobs already scored on the scorer's current basis are skipped, and it stops (before fetching the next
        posting) when only scoring.backfill_reserve calls are left today, so running it again continues.
        Returns ([(job, old status, new status, new score or None if it couldn't be scored)], jobs left)."""
        if not self.scorer:
            return [], 0
        by_slug = {(b.source, b.slug): b for b in boards}
        since = utcnow() - timedelta(days=days or self.cfg.criteria.max_age_days)
        todo = [r for r in self.store.scored_since(since) if r["score_basis"] != self.scorer.basis]
        out = []
        for n, row in enumerate(todo):
            if progress:
                progress(n, len(todo))   # the Stories page shows how far it got
            if self._backfill_budget_spent():
                log.warning("rescore: stopping with %d of today's AI calls kept for new jobs; %d jobs left, "
                            "run it again after 00:00 UTC", self.cfg.scoring.backfill_reserve, len(todo) - n)
                if out:
                    self._dashboard_stale.set()
                return out, len(todo) - n
            job, old = job_from_row(row), row["status"]
            board = by_slug.get((job.source, job.board))
            if board is not None:
                try:
                    await SOURCES[job.source].enrich(job, board, self.http)
                except Exception as e:
                    if posting_gone(e):
                        self.store.update_job(job.key, status="closed",
                                              reason=f"posting taken down (HTTP {e.response.status_code})")
                        out.append((job, old, "closed", None))
                        continue
                    log.warning("rescore: couldn't fetch %s: %s", job.key, e)
            fit = await self.scorer.score(job)
            if fit is None:  # API down, daily cap: keep the old score
                out.append((job, old, old, None))
                continue
            status = old
            if old in ("alert", "digest", "low"):
                status = self.route(fit.score, fit.step_up).replace("alert", "digest")
            self.store.update_job(job.key, status=status, reason="rescored" if status != old else None,
                                  **_score_cols(fit))
            out.append((job, old, status, fit))
        if out:
            self._dashboard_stale.set()
        return out, 0

    async def score_anyway(self, key: str) -> str:
        """Full AI score for a job the filter turned down (the Filter page's button). It's never pushed:
        a high score lands in the digest. Returns the new status, or why it couldn't be scored."""
        row = self.store.get_job(key)
        if row is None:
            return "not found"
        if not self.scorer:
            return "AI scoring is off"
        if self.scorer.capped():
            return "today's AI calls are used up"
        job = job_from_row(row)
        board = next((b for b in self.active_boards() if (b.source, b.slug) == (job.source, job.board)), None)
        if board is None:
            return "its board isn't watched any more"
        self.store.update_job(key, status="processing", reason="scored on request (Filter page)")
        try:
            status, _ = await self._finish(job, board, SOURCES[job.source])
        except Exception as e:
            log.exception("score anyway: %s failed", key)
            self.store.update_job(key, status="error", reason=str(e)[:200])
            return "error"
        if status == "alert":
            status = "digest"
            self.store.update_job(key, status="digest")
        self._dashboard_stale.set()
        return status

    async def _notify(self, job: Job, score: FitScore | None) -> int:
        if self._collecting is not None:   # catching up: one summary instead of a push per job
            self._collecting.append((job, score))
            return 1
        pay = self.pay_index(job, score)
        try:
            await self.notifier.job_alert(job, score, pay.text() if pay else "", STEP_UP_SHOWN,
                                          link=job_link(self.cfg.web.public_url, job.key))
        except Exception as e:
            log.error("Notification failed for %s: %s (will go in the digest)", job.key, e)
            self.store.update_job(job.key, status="notify_failed")
            return 0
        self.store.mark_notified(job.key)
        return 1

    async def _cap_notice(self) -> None:
        today = utcnow().date().isoformat()
        if self._cap_noticed == today:
            return
        self._cap_noticed = today
        self.store.set_meta(runstate.CAP_NOTICED, today)
        try:
            await self.notifier.send(
                "AI daily cap reached",
                f"All {self.cfg.scoring.daily_max_calls} of today's Claude calls are used (scoring.daily_max_calls). "
                "New matches go to the digest unscored until 00:00 UTC instead of being pushed.",
                priority=3, tags=["warning"])
        except Exception as e:
            log.warning("Couldn't send the AI cap notice: %s", e)

    async def preview_board(self, board: Board) -> tuple[int, list[Job]]:
        """Dry run: fetch and filter only. Nothing is stored or sent."""
        jobs = await SOURCES[board.source].fetch(board, self.http)
        matches = [j for j in jobs if not self.filter.is_stale(j) and self.filter.check_basic(j)[0]]
        return len(jobs), matches

    # ------------------------------------------------------------------ cost-of-living data
    def _pay_paths(self) -> tuple[Path, Path]:
        """(committed seed in the config dir, refreshed copy next to the database)"""
        return self.cfg.resolve(self.cfg.pay.col_data), Path(self.cfg.database).parent / "col_data.json"

    def load_pay(self) -> None:
        cfg = self.cfg.pay
        if not cfg.enabled:
            return
        found = col.newest_data(*self._pay_paths())
        if not found:
            self.pay_problem = "no cost-of-living data: run `jobalerts pay --update`"
            log.warning("Pay index off: %s", self.pay_problem)
            return
        data, path = found
        taxes = None
        if cfg.taxes:
            try:
                taxes = Taxes.load(self.cfg.resolve(cfg.tax_rules), self.cfg.resolve(cfg.tax_data),
                                   self.cfg.household.model_dump())
            except (OSError, KeyError, TypeError, ValueError) as e:
                self.pay_problem = f"tax tables unusable ({type(e).__name__}: {e}); comparing gross pay"
                log.warning("Pay index: %s", self.pay_problem)
        try:
            ownership = col.Ownership(**cfg.buy.model_dump(exclude={"enabled"})) if cfg.buy.enabled else None
            self.pay = col.PayIndex(data, cfg.reference_salary, cfg.reference_location, cfg.locations, taxes,
                                    cfg.housing_share, ownership, col.CurrentHome(**cfg.current_home.model_dump()),
                                    cfg.city_weight)
        except (KeyError, ValueError) as e:
            self.pay_problem = f"cost-of-living data in {path} unusable: {e}"
            log.warning("Pay index off: %s", self.pay_problem)
            return
        log.info("Pay index: your salary at %s = 1.0 (%s); data built %s from %s",
                 self.pay.home.label, f"take-home, {taxes.year} taxes" if taxes else "gross pay",
                 data.get("built"), path)
        if taxes and (problem := taxes.problem()):
            log.warning("Pay index: %s", problem)

    # ------------------------------------------------------------------ digest, catch-up report, map
    def catchup_report(self) -> dict | None:
        return runstate.load_report(self.store, self.cfg.run.report_hours)

    def digest_missed(self, now: datetime | None = None) -> bool:
        """A digest was due while the service was off: the last one sent is older than the latest
        scheduled time. (Never true before the first digest.)"""
        last = self.store.get_meta(runstate.LAST_DIGEST)
        if not last:
            return False
        tz = ZoneInfo(self.cfg.notify.timezone)
        hour, minute = (int(x) for x in self.cfg.notify.digest_time.split(":"))
        now = (now or datetime.now(tz)).astimezone(tz)
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due > now:
            due -= timedelta(days=1)
        return last < due.date().isoformat()

    def digest_due(self, now: datetime | None = None) -> bool:
        """The scheduled digest time passed since the last one. The first time, the latest scheduled time
        counts as done: the first digest comes at the next one."""
        if self.store.get_meta(runstate.LAST_DIGEST):
            return self.digest_missed(now)
        tz = ZoneInfo(self.cfg.notify.timezone)
        hour, minute = (int(x) for x in self.cfg.notify.digest_time.split(":"))
        now = (now or datetime.now(tz)).astimezone(tz)
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due > now:
            due -= timedelta(days=1)
        self.store.set_meta(runstate.LAST_DIGEST, due.date().isoformat())
        return False

    async def send_digest(self, mark: bool = True) -> str:
        """Daily summary: held-back matches, unwatched companies hiring for your search, and health.
        Always sent, so silence from this tool never means 'broken'."""
        rows = self.store.digest_jobs(self.cfg.notify.digest_max_items)
        lines: list[str] = []
        if rows:
            lines.append(f"{len(rows)} jobs worth a look:")
            for r in rows:
                score = f"[{r['score']}] " if r["score"] is not None else ""
                lines.append(f"• {score}{r['company']}: {r['title']} ({r['location'] or 'n/a'})\n  {r['url']}")
                if r["pay_note"] and r["pay_note"] != col.NO_PAY:
                    lines.append(f"  {r['pay_note']}")
                if r["step_up"] is not None and r["step_up"] >= STEP_UP_SHOWN:
                    lines.append(f"  step up {r['step_up']}: {r['step_up_note'] or ''}".rstrip(": "))
                if stories := stories_from_row(r):
                    lines.append("  stories: " + "; ".join(title for _, title in stories))
                if r["best_resume"]:
                    lines.append(f"  apply with: {r['best_resume']}")
        else:
            lines.append("No held-back matches today.")

        suggestions = await self._resolve_suggestions() if self.admin else []  # the watch list is the admins'
        if suggestions:
            lines.append("\nHiring for your search, not on your watch list:")
            for s, resolved in suggestions:
                hint = f" → {resolved}" if resolved else ""
                lines.append(f"• {s['company']} ({s['matches']} matches){hint}")
            lines.append("Add one with: jobalerts discover \"<company>\" --add")

        activity = self.store.activity_since(utcnow() - timedelta(days=1))
        seen = sum(n for status, n in activity.items() if status != "baseline")
        hosts = self.store.host_stats(days=1)
        requests = sum(c["requests"] for c in hosts.values())
        limited = sum(c["rate_limited"] for c in hosts.values())
        errors = sum(c["errors"] for c in hosts.values())
        lines.append(f"\nLast 24h: {seen} new postings seen, {activity['alerted']} pushed, "
                     f"{activity['digest'] + activity['digested']} for digest, {activity['low']} low-fit, "
                     f"{activity['low_pay']} low pay. "
                     f"{requests} requests, {limited} rate-limited, {errors} errors.")
        skipped = self.store.low_pay_since(utcnow() - timedelta(days=1))
        if skipped:
            lines.append(f"\nSkipped for pay (top of range under {self.cfg.pay.min_index:g}), check for misreads:")
            lines += [f"• {r['company']}: {r['title']}: {r['pay_note']}\n  {r['url']}" for r in skipped[:10]]
            if len(skipped) > 10:
                lines.append(f"  …and {len(skipped) - 10} more")
        if self.pay_problem:
            lines.append(f"Pay index: {self.pay_problem}")
        if self.pay and self.pay.taxes and (problem := self.pay.taxes.problem()):
            lines.append(f"Pay index: {problem}")
        if self.admin and (update := self.update_status()) and update.available and self.cfg.update.mode != "off":
            lines.append(f"Update available: {update.behind} change(s) (Settings → Updates)"
                         + (f"; can't install on its own: {update.problem}" if update.problem else ""))
        failing = [b for b in self.store.boards() if b["failures"]] if self.admin else []
        if failing:
            lines.append("Failing boards: " + "; ".join(
                f"{b['key']} ({b['failures']}x: {(b['last_error'] or '')[:80]})" for b in failing[:5]))

        message = "\n".join(lines)
        home = self.cfg.web.public_url.rstrip("/")
        await self.notifier.send(f"Job digest: {len(rows)} to review", message, priority=3, tags=["clipboard"],
                                 click=f"{home}/" if home else None)
        if mark:
            self.store.mark_digested([r["key"] for r in rows])
            today = datetime.now(ZoneInfo(self.cfg.notify.timezone)).date().isoformat()
            self.store.set_meta(runstate.LAST_DIGEST, today)
            self.store.mark_suggestions_reported([s["company_key"] for s, _ in suggestions])
        return message

    async def _resolve_suggestions(self, limit: int = 5) -> list[tuple]:
        out = []
        for s in self.store.pending_suggestions(limit):
            resolved = s["resolved"]
            if resolved is None:
                try:
                    found = await discover.find_by_name(s["company"], self.http)
                except Exception as e:
                    log.warning("Couldn't resolve ATS for %s: %s", s["company"], e)
                    found = []
                resolved = ", ".join(f"{f.ats}:{f.slug}" for f in found)
                self.store.set_suggestion_resolved(s["company_key"], resolved)
            out.append((s, resolved))
        return out
    def top_matches(self, n: int | None = None, days: int | None = None, by: str = "score") -> tuple[int, list[dict]]:
        """Best recent matches by AI fit score, or with by="step_up" the biggest credible promotions."""
        cfg = self.cfg.homeassistant
        return top_matches(self.store, self.filter, days or cfg.days, n or cfg.top_n, self.pay, by,
                           self.cfg.web.public_url)

    @property
    def map_enabled(self) -> bool:
        return bool(self.cfg.map.enabled and self.pay)

    async def refresh_map(self) -> str:
        data = await self.map_data()
        self.map_html = colmap.render(data)
        self.map_html_shared = colmap.render(colmap.for_sharing(data))
        self._map_built = time.monotonic()
        return self.map_html

    async def current_map(self, shared: bool = False) -> str:
        """The map page for the web UI (`shared`: the version for the read-only Home Assistant link).
        The dashboard loop keeps it fresh in `run`; `jobalerts web` (no loops) rebuilds it when it's
        older than homeassistant.interval."""
        if self.map_html is None or time.monotonic() - self._map_built > self.cfg.homeassistant.interval:
            await self.refresh_map()
        return self.map_html_shared if shared else self.map_html

    async def map_data(self) -> dict:
        """The cost-of-living map's data with the current top jobs (the price math runs off the event loop)."""
        _, jobs = self.top_matches()
        _, step_ups = self.top_matches(by="step_up")
        pay = self.pay
        return await asyncio.to_thread(colmap.build_map_data, pay, jobs, step_ups)

    async def map_page(self) -> str:
        return colmap.render(await self.map_data())

    async def report_catch_up(self, since: datetime, gap: float, new: int, collected: list, failed: int) -> dict:
        """After a catch-up pass: one summary push, then the best few jobs; the rest go to the digest."""
        cfg = self.cfg.scoring

        def strength(item):
            job, score = item
            return max(score.score if score else 0, score.step_up if score and cfg.step_up_threshold else 0)
        ranked = sorted(collected, key=strength, reverse=True)
        top = ranked[:self.cfg.run.catchup_top_pushes]
        home = self.cfg.web.public_url.rstrip("/")
        summary = (f"{new} new match{'es' if new != 1 else ''} since {since.astimezone(ZoneInfo(self.cfg.notify.timezone)):%a %H:%M}"
                   f" ({len(collected)} strong)")
        for job, _score in ranked[len(top):]:
            self.store.update_job(job.key, status="digest", reason="catch-up: in the report")
        report = {"at": runstate.now().isoformat(timespec="seconds"), "since": since.isoformat(timespec="seconds"),
                  "gap": runstate.human(gap), "summary": summary + (f"; {failed} boards failed" if failed else ""),
                  "new": new, "strong": len(collected), "note": runstate.HONEST_GAPS}
        runstate.save_report(self.store, report)
        if new or collected:
            await self.notifier.send(f"Catch-up: {summary}",
                                     f"You were away {runstate.human(gap)}. "
                                     + (f"The best {len(top)} follow" if top else "None was strong enough to push")
                                     + (", the rest are on the jobs page." if home else ", the rest are in the digest."),
                                     priority=3, tags=["inbox_tray"], click=f"{home}/" if home else None)
        for job, score in top:
            await self._notify(job, score)
        return report


class Runner:
    """The service: polls every board once, runs each new job through every user's pipeline (Person),
    and the loops (boards, pre-screen, digests, heartbeat and catch-up, updates, Home Assistant).
    The first user's Person (`me`, id 1) stands in for "the" user where there is one: `runner.cfg`,
    `runner.scorer`, `runner.send_digest()` and the like are theirs."""

    def __init__(self, cfg: Config, store: Store, http: HttpClient, notifier: Notifier,
                 scorer: Scorer | None = None, homeassistant: MqttPublisher | None = None,
                 prescreener: Prescreener | None = None):
        self.store = store
        self.http = http
        self.watched = {normalize_company(name) for name in cfg.watched_companies()}
        self._failure_alerted: set[str] = set(json.loads(store.get_meta(runstate.FAILURE_ALERTED) or "[]"))
        self.homeassistant = homeassistant
        self._dashboard_stale = asyncio.Event()  # set after new matches so Home Assistant updates right away
        self.vault = None                   # API keys set on the Settings page (vault.Vault), set by cli.open_runner
        self.ui_settings = None             # settings.UiSettings of user 1, set by cli.open_runner / the web app
        self.users = None                   # users.Users (accounts), set by cli.open_runner / the web app
        self.base_cfg = cfg                 # config.yaml + config.local.yaml, before the UI's settings
        self.rebuild = None                 # (cfg, store, **kw) -> (scorer, prescreener, homeassistant); cli.open_runner
        self.running = False                # True inside run_forever (board loops are live)
        self.config_path = str(cfg.config_dir / "config.yaml")   # cli.open_runner sets the real -c path
        self.restart_requested = False      # set after an update installed itself: cli.cmd_run restarts
        self._main_task: asyncio.Task | None = None
        self.board_tasks: dict[str, tuple[Board, asyncio.Task]] = {}
        self.next_poll: dict[str, float] = {}  # board key -> unix time of its next poll
        self._catching_up = False
        self._board_locks: dict[str, asyncio.Lock] = {}
        self._prescreen_wake = asyncio.Event()     # AI-only mode: new jobs are waiting for the pre-screen
        self._prescreen_task: asyncio.Task | None = None
        self._boards_started = asyncio.Event()     # set once a start-up catch-up and missed digest are done
        self._shipped: dict | None = None
        self._ui: dict[int, object] = {}
        self.me = Person(self, OWNER, cfg, notifier, scorer, prescreener, admin=True)
        self.people: dict[int, Person] = {OWNER: self.me}

    # The first user's, for code (and the CLI) that has one user in mind.
    cfg = _first_users("cfg")
    scorer = _first_users("scorer")
    prescreener = _first_users("prescreener")
    notifier = _first_users("notifier")
    filter = _first_users("filter")
    pay = _first_users("pay")
    pay_problem = _first_users("pay_problem")
    map_html = _first_users("map_html")
    map_html_shared = _first_users("map_html_shared")
    _map_built = _first_users("_map_built")
    _collecting = _first_users("_collecting")
    _cap_noticed = _first_users("_cap_noticed")

    def __getattr__(self, name: str):
        if name in FIRST_USERS:
            return getattr(self.me, name)
        raise AttributeError(name)

    # ------------------------------------------------------------------ users
    def person(self, uid: int) -> Person | None:
        return self.people.get(uid)

    def everyone(self) -> list[Person]:
        return list(self.people.values())

    @property
    def data_dir(self) -> Path:
        """The data folder: user 1's profile is at its top."""
        return self.base_cfg.resolve(self.base_cfg.scoring.profile_path).parent

    def settings_for(self, uid: int):
        """settings.UiSettings for one user (None before the web UI or the CLI set up the vault)."""
        if uid == OWNER or self.ui_settings is None:
            return self.ui_settings
        if uid not in self._ui:
            from .settings import UiSettings
            self._ui[uid] = UiSettings(self.ui_settings.store, self.vault, uid)
        return self._ui[uid]

    def personal_base(self, uid: int) -> Config:
        """What a user other than user 1 starts from: the install's config with the personal sections at
        the shipped defaults (not user 1's config.local.yaml or .env), and their own folder."""
        if uid == OWNER:
            return self.base_cfg
        from .config import PERSONAL_SECTIONS, shipped_personal
        from .users import user_folder
        if self._shipped is None:
            path = self.base_cfg.config_dir / "config.yaml"
            self._shipped = shipped_personal(path) if path.is_file() else {
                k: v for k, v in Config().model_dump().items() if k in PERSONAL_SECTIONS}
        base = self.base_cfg
        data = base.model_dump(exclude={"companies", "config_dir", "value_sources", "config_problems"})
        data.update(json.loads(json.dumps(self._shipped)))
        folder = user_folder(self.data_dir, uid)
        data["scoring"].update(profile_path=str(folder / "profile.md"), resume_dir=str(folder / "resumes"),
                               stories_dir=str(folder / "stories"))
        data["notify"].update(topic="", token="")
        cfg = Config.model_validate(data)
        cfg.config_dir, cfg.companies = base.config_dir, base.companies
        cfg.value_sources = {k: v for k, v in base.value_sources.items() if k.split(".")[0] not in PERSONAL_SECTIONS}
        return cfg

    def config_for(self, uid: int) -> Config:
        """A user's settings: their base with the install's and their own Settings-page values on top."""
        from pydantic import ValidationError
        from .settings import apply as apply_settings
        base, ui = self.personal_base(uid), self.settings_for(uid)
        if ui is None:
            return base
        try:
            return apply_settings(base, ui.overlay())
        except ValidationError as e:
            log.warning("Settings of user %d don't fit this version's config; using the defaults: %s", uid, e)
            return base

    def anthropic_key(self, uid: int, admin: bool, share: bool) -> tuple[str, bool]:
        return self.vault.anthropic_key(uid, admin, share) if self.vault else ("", False)

    def add_person(self, uid: int, admin: bool) -> Person:
        from .userdata import seed_user_files
        cfg = self.config_for(uid)
        seed_user_files(cfg, watch_list=False)
        vault = self.vault
        notifier = Notifier(cfg.notify, dry_run=self.me.notifier._dry_run,
                            secrets=lambda name: vault.get(name, uid) if vault else "")
        person = Person(self, uid, cfg, notifier, admin=admin)
        person.rebuild_services()
        self.people[uid] = person
        return person

    def sync_people(self) -> None:
        """One Person per enabled account (after users are added, removed, enabled or their role changed)."""
        if self.users is None:
            return
        wanted = {u.id: u for u in self.users.all() if not u.disabled}
        for uid in list(self.people):
            if uid != OWNER and uid not in wanted:
                del self.people[uid]
        for uid, user in wanted.items():
            if uid == OWNER:
                continue
            if uid not in self.people:
                self.add_person(uid, user.admin)
            elif self.people[uid].admin != user.admin:
                self.people[uid].admin = user.admin
                self.people[uid].rebuild_services()
        self._start_prescreen_loop()

    def refresh(self, uid: int | None = None) -> None:
        """Settings changed: one user's (their own sections, keys, files), or everyone's (`uid` None)."""
        for person in ([self.people[uid]] if uid in self.people else self.everyone() if uid is None else []):
            person.reconfigure(self.config_for(person.uid))
        self._after_settings()

    def reconfigure(self, cfg: Config) -> None:
        """New settings for user 1 (and the install's parts: boards, Home Assistant) without a restart."""
        self.me.reconfigure(cfg)
        self._after_settings()

    def _after_settings(self) -> None:
        self.watched = {normalize_company(name) for name in self.cfg.watched_companies()}
        self._start_prescreen_loop()
        self._dashboard_stale.set()
        self.sync_boards()   # a key saved in Settings starts its aggregator searches

    def _start_prescreen_loop(self) -> None:
        if self.running and any(p.prescreener for p in self.everyone()) and (
                self._prescreen_task is None or self._prescreen_task.done()):
            self._prescreen_task = asyncio.create_task(self._prescreen_loop(), name="prescreen")

    async def notify_admins(self, title: str, message: str, **kwargs) -> None:
        """Service news (boards failing, updates) to every admin with notifications set up."""
        for person in self.everyone():
            if person.admin:
                try:
                    await person.notifier.send(title, message, **kwargs)
                except Exception as e:
                    log.warning("Couldn't notify user %d: %s", person.uid, e)

    # ------------------------------------------------------------------ pipeline
    async def poll_board(self, board: Board) -> PollResult:
        # One poll per board at a time: a catch-up after a wake runs while the board loops do.
        async with self._board_locks.setdefault(board.key, asyncio.Lock()):
            return await self._poll_board(board)

    async def _poll_board(self, board: Board) -> PollResult:
        source = SOURCES[board.source]
        jobs = {j.key: j for j in await source.fetch(board, self.http)}
        known = self.store.known_keys(list(jobs))
        new = [j for k, j in jobs.items() if k not in known]
        result = PollResult(board.key, fetched=len(jobs), new=len(new), baseline=self.store.is_baseline(board.key))

        if result.baseline:
            # First time we see this board: remember what's already open, alert on nothing.
            self.store.insert_jobs(new, "baseline")
            self.store.mark_board_ok(board.key, len(jobs))
            log.info("%s: baseline of %d existing jobs recorded silently", board.key, len(new))
            return result

        people = self.everyone()
        alerts_left = {p.uid: self.cfg.polling.max_alerts_per_poll for p in people}
        overflow = {p.uid: 0 for p in people}
        for job in new:
            if self.store.seen_elsewhere(job):
                self.store.insert_shared(job, "duplicate", "already seen from another source")
                continue
            self.store.insert_shared(job)
            matched = False
            for person in people:     # the description fetched for one is there for the next
                try:
                    status, score = await person.process(job, board, source)
                except Exception as e:  # one bad posting must not stop the rest of the board
                    log.exception("%s: failed processing %s", board.key, job.key)
                    person.store.insert_job(job, "error", str(e)[:200])
                    person.store.update_job(job.key, status="error", reason=str(e)[:200])
                    continue
                if status in ("alert", "digest", "low"):
                    matched = True
                    result.matched_by[person.uid] = result.matched_by.get(person.uid, 0) + 1
                if status != "alert":
                    continue
                if person._collecting is not None:   # catching up: no per-board cap, one summary at the end
                    result.alerted += await person._notify(job, score)
                elif alerts_left[person.uid] > 0:
                    alerts_left[person.uid] -= 1
                    result.alerted += await person._notify(job, score)
                else:
                    overflow[person.uid] += 1
                    person.store.update_job(job.key, status="digest", reason="per-poll alert cap reached")
            if matched:
                result.matched.append(job)
        for person in people:
            if n := overflow[person.uid]:
                await person.notifier.send(
                    f"{board.name}: {n} more matches",
                    f"{n} more new matching jobs at {board.name} were held back to avoid a flood. "
                    "They'll be in the next digest.", priority=3, tags=["inbox_tray"])
        self.store.mark_board_ok(board.key, len(jobs))
        if result.matched:
            self._dashboard_stale.set()
        if new:
            log.info("%s: %d new, %d matched, %d alerted", board.key, len(new), len(result.matched), result.alerted)
        return result

    async def refresh_pay_data(self, force: bool = False) -> str:
        """Rebuild the cost-of-living table when it's older than pay.refresh_days. A download that fails
        or doesn't pass col.check_data leaves the current table in place. Returns what happened."""
        cfg = self.cfg.pay
        if not cfg.enabled or not (force or cfg.refresh_days):
            return ""
        age = col.data_age_days(self.pay.data) if self.pay else None
        if not force and age is not None and age < cfg.refresh_days:
            return ""
        try:
            data = await col.build_data()
            col.check_data(data, cfg.reference_location, cfg.housing_share, cfg.buy.enabled)
        except Exception as e:
            built = self.pay.data.get("built") if self.pay else "never"
            self.pay_problem = f"cost-of-living refresh failed ({type(e).__name__}: {e}); using data built {built}"
            log.warning(self.pay_problem)
            return self.pay_problem
        if "us_metro_coords" not in data and self.pay and self.pay.data.get("us_metro_coords"):
            data["us_metro_coords"] = self.pay.data["us_metro_coords"]  # Census was down: keep the map's points
            data["sources"]["places"] = self.pay.data["sources"].get("places", "")
        path = self._pay_paths()[1]
        col.write_data(data, path)
        for person in self.everyone():
            person.pay, person.pay_problem = None, ""
            person.load_pay()
        self._dashboard_stale.set()  # the map's numbers changed
        return f"Cost-of-living data rebuilt into {path}: {data['sources']['us']}; {len(data['countries'])} countries"

    def _save_failure_alerted(self) -> None:
        self.store.set_meta(runstate.FAILURE_ALERTED, json.dumps(sorted(self._failure_alerted)))

    # ------------------------------------------------------------------ service loops
    async def run_forever(self) -> None:
        boards, skipped = without_missing_keys(self.cfg.boards())
        if not boards:
            raise SystemExit("No boards configured: add companies to companies.yaml or enable an aggregator.")
        note = ""
        if skipped:
            note = " Not running (no API key yet; Settings > API keys): " + ", ".join(sorted(skipped)) + "."
            for source, keys in sorted(skipped.items()):
                log.info("%s is enabled but skipped until %s %s set (.env or Settings > API keys)", source,
                         " and ".join(keys), "are" if len(keys) > 1 else "is")
        for person in self.everyone():
            if not person.prescreener and (n := person.store.drop_prescreen_queue()):
                log.info("AI pre-screen off for user %d: %d waiting jobs marked filtered", person.uid, n)
        log.info("Watching %d boards", len(boards))
        await self.notify_admins("Job alerts running", f"Watching {len(boards)} boards.{note}",
                                 priority=2, tags=["eyes"])
        last = runstate.last_heartbeat(self.store)
        runstate.beat(self.store)
        self.running = True
        self._main_task = asyncio.current_task()
        tasks = [asyncio.create_task(self._start_boards(last), name="boards"),
                 asyncio.create_task(self._daily_loop(), name="digest"),
                 asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
                 asyncio.create_task(self._update_loop(), name="updates")]
        if any(p.prescreener for p in self.everyone()):
            self._prescreen_task = asyncio.create_task(self._prescreen_loop(), name="prescreen")
            tasks.append(self._prescreen_task)
        tasks.append(asyncio.create_task(self.refresh_pay_data(), name="col-refresh"))
        if self.cfg.map.enabled and not self.pay:
            log.warning("map.enabled is set but the pay index is off (pay.enabled); no map")
        if self.homeassistant or self.map_enabled:
            tasks.append(asyncio.create_task(self._dashboard_loop(), name="dashboard"))
        if self.cfg.web.enabled:
            from .web.app import serve
            tasks.append(asyncio.create_task(serve(self), name="web"))
        try:
            await asyncio.gather(*tasks)
        finally:   # a pre-screen loop started later by a Settings change isn't in `tasks`
            if self._prescreen_task and not self._prescreen_task.done():
                self._prescreen_task.cancel()

    # ------------------------------------------------------------------ the boards being polled
    def active_boards(self) -> list[Board]:
        return without_missing_keys(self.cfg.boards())[0]

    def sync_boards(self) -> None:
        """Start, stop or restart board loops to match the watch list and the keys that are set
        (after an edit on the Boards page or a key saved in Settings). Only while the service runs."""
        if not self.running:
            return
        wanted = {b.key: b for b in self.active_boards()}
        for key, (board, task) in list(self.board_tasks.items()):
            if wanted.get(key) != board:
                task.cancel()
                del self.board_tasks[key]
                self.next_poll.pop(key, None)
                log.info("Stopped polling %s", key)
        for key, board in wanted.items():
            if key not in self.board_tasks:
                self.board_tasks[key] = (board, asyncio.create_task(self._board_loop(board), name=key))

    async def _start_boards(self, last_heartbeat: datetime | None) -> None:
        """Catch up first if the service was off for a while, send a digest that came due meanwhile,
        then run the board loops. (The web UI and the other loops run meanwhile.)"""
        if last_heartbeat and (runstate.now() - last_heartbeat).total_seconds() > self.cfg.run.catchup_after:
            try:
                await self.catch_up(last_heartbeat)
            except Exception:
                log.exception("Catch-up failed; polling normally")
        await self._send_missed_digests()
        self._boards_started.set()
        self.sync_boards()
        await self._supervise_boards()

    async def _supervise_boards(self) -> None:
        """Waits on the board loops (they run until stopped); returns when none are left."""
        while self.board_tasks:
            tasks = {task for _, task in self.board_tasks.values()}
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for key, (_board, task) in list(self.board_tasks.items()):
                if task in done:
                    del self.board_tasks[key]
                    if not task.cancelled() and task.exception():
                        log.error("Board loop %s crashed: %r", key, task.exception())

    def reload_watchlist(self) -> None:
        """Re-read companies.yaml after the Boards page changed it, and poll accordingly."""
        from .config import Company
        from .watchlist import load
        companies = [Company.model_validate(c) for c in load(self.cfg.resolve(self.cfg.companies_file))]
        self.base_cfg.companies = companies
        for person in self.everyone():
            person.cfg.companies = companies
        self.watched = {normalize_company(name) for name in self.cfg.watched_companies()}
        self.sync_boards()
        self._dashboard_stale.set()

    # ------------------------------------------------------------------ updates from the repository
    def update_status(self) -> updater.UpdateStatus | None:
        return updater.UpdateStatus.from_json(self.store.get_meta(updater.STATUS_META))

    async def _update_loop(self) -> None:
        await asyncio.sleep(600)   # not in the way of a start-up catch-up
        while True:
            if self.cfg.update.mode != "off":
                try:
                    await self.check_updates()
                except Exception:
                    log.exception("Update check failed")
            await asyncio.sleep(max(1, self.cfg.update.interval_hours) * 3600)

    async def check_updates(self, act: bool = True) -> updater.UpdateStatus:
        """Look for a new version; with update.mode "notify", push once per new version; with "install",
        install it and restart (when nothing of yours is in the way; otherwise just notify)."""
        status = await asyncio.to_thread(updater.check)
        self.store.set_meta(updater.STATUS_META, status.to_json())
        if not act or not status.available or self.cfg.update.mode == "off":
            return status
        if self.cfg.update.mode == "install" and status.can_install:
            await self.install_update()
        elif self.store.get_meta("update.notified") != status.latest:
            if status.commits:
                changes = "\n".join(f"• {c.split(' ', 1)[-1]}" for c in status.commits[:5])
                more = f"\n…and {status.behind - 5} more" if status.behind > 5 else ""
            else:   # the desktop app: a release and its notes
                changes, more = "\n".join(f"• {n}" for n in status.notes[:5]) or f"Version {status.latest}", ""
            how = (f"Can't install it on its own: {status.problem}" if status.problem
                   else "Install it on Settings → Updates (or `jobalerts update`).")
            home = self.cfg.web.public_url.rstrip("/")
            await self.notify_admins(f"Job alerts update: {status.behind} change{'s' if status.behind != 1 else ''}",
                                     f"{changes}{more}\n{how}", priority=2, tags=["arrow_up"],
                                     click=f"{home}/settings/updates" if home else None)
            self.store.set_meta("update.notified", status.latest)
        return status

    async def install_update(self) -> tuple[bool, str]:
        """Install the newest version (see updater.install) and, in the service, restart into it."""
        ok, message = await asyncio.to_thread(updater.install, None, config=self.config_path, store=self.store)
        self.store.set_meta(updater.STATUS_META, (await asyncio.to_thread(updater.check, None, False)).to_json())
        await self.notify_admins("Job alerts updated" if ok else "Job alerts update failed", message,
                                 priority=2 if ok else 3, tags=["arrow_up"] if ok else ["warning"])
        if ok and self.running:
            asyncio.get_running_loop().call_later(1.0, self.request_restart)   # after the web page answered
        return ok, message

    def request_restart(self) -> None:
        """Stop the service so cli.cmd_run starts the updated version."""
        self.restart_requested = True
        if self._main_task:
            self._main_task.cancel()

    # ------------------------------------------------------------------ catching up after a gap
    async def _heartbeat_loop(self) -> None:
        """Beat every run.heartbeat seconds; a much longer wall-clock jump means the PC slept."""
        last = runstate.now()
        while True:
            await asyncio.sleep(self.cfg.run.heartbeat)
            now = runstate.now()
            if (now - last).total_seconds() > self.cfg.run.catchup_after:
                log.info("Woke after %s; catching up", runstate.human((now - last).total_seconds()))
                try:
                    await self.catch_up(last)
                except Exception:
                    log.exception("Catch-up failed")
                await self._send_missed_digests()
            runstate.beat(self.store, now)
            last = now

    async def catch_up(self, since: datetime) -> dict:
        """Poll every board now, look back over the gap, pre-screen, then for each user one summary push
        plus their best few; the rest wait on the jobs page and in the digest. Returns user 1's report."""
        if self._catching_up:
            return {}
        self._catching_up = True
        gap = (runstate.now() - since).total_seconds()
        boards = self.active_boards()
        log.info("Catching up on %d boards after %s away", len(boards), runstate.human(gap))
        last_ok = {r["key"]: parse_dt(r["last_ok_at"]) for r in self.store.boards()}
        for board in boards:
            SOURCES[board.source].resume(board, last_ok.get(board.key) or since)
        people = self.everyone()
        collected: dict[int, list] = {}
        for person in people:
            person._collecting = collected[person.uid] = []
        try:
            results = await asyncio.gather(*(self.poll_board(b) for b in boards), return_exceptions=True)
            for person in people:
                if person.prescreener:
                    await person.run_prescreen(boards)
        finally:
            for person in people:
                person._collecting = None
            self._catching_up = False
        for board, r in zip(boards, results):
            if isinstance(r, Exception):
                self.store.mark_board_failed(board.key, f"{type(r).__name__}: {r}")
        failed = sum(isinstance(r, Exception) for r in results)
        reports = {}
        for person in people:
            new = sum(r.matched_by.get(person.uid, 0) for r in results if not isinstance(r, Exception))
            reports[person.uid] = await person.report_catch_up(since, gap, new, collected[person.uid], failed)
        self._dashboard_stale.set()
        return reports.get(OWNER, {})

    async def _board_loop(self, board: Board) -> None:
        # Spread the first polls over the first couple of minutes instead of one burst.
        first = random.uniform(0, min(board.interval, 120))
        self.next_poll[board.key] = time.time() + first
        await asyncio.sleep(first)
        jitter = self.cfg.polling.jitter
        while True:
            delay = board.interval
            try:
                await self.poll_board(board)
                if board.key in self._failure_alerted:
                    self._failure_alerted.discard(board.key)
                    self._save_failure_alerted()
                    await self.notify_admins(f"Job board recovered: {board.name}", f"{board.key} is polling again.",
                                             priority=2, tags=["white_check_mark"])
            except SourceConfigError as e:
                log.error("%s disabled: %s", board.key, e)
                self.store.mark_board_failed(board.key, str(e))
                await self.notify_admins(f"Job board disabled: {board.name}", str(e), priority=3, tags=["warning"])
                return
            except Exception as e:
                failures = self.store.mark_board_failed(board.key, f"{type(e).__name__}: {e}")
                delay = min(board.interval * 2 ** min(failures, 6), self.cfg.polling.max_backoff)
                log.warning("%s failed (%d in a row): %s; next try in %ds", board.key, failures, e, delay)
                if failures >= self.cfg.polling.failure_alert_threshold and board.key not in self._failure_alerted:
                    self._failure_alerted.add(board.key)
                    self._save_failure_alerted()
                    await self.notify_admins(
                        f"Job board failing: {board.name}",
                        f"{failures} failures in a row. Last error: {type(e).__name__}: {e}\n"
                        f"Retrying every {delay // 60} min. Check the slug/URL or whether the site changed.",
                        priority=3, tags=["warning"])
            finally:
                self.store.add_host_stats(self.http.drain_stats())
            wait = delay * random.uniform(1 - jitter, 1 + jitter)
            self.next_poll[board.key] = time.time() + wait
            await asyncio.sleep(wait)

    async def _prescreen_loop(self) -> None:
        """Every prescreen.interval; in AI-only mode also prescreen.ai_delay after new jobs arrive, so
        alerts don't wait an hour (the delay gathers a poll round's jobs into one batch)."""
        while True:
            try:
                await asyncio.wait_for(self._prescreen_wake.wait(), timeout=self.cfg.prescreen.interval)
                await asyncio.sleep(self.cfg.prescreen.ai_delay)
            except asyncio.TimeoutError:
                pass
            self._prescreen_wake.clear()
            for person in self.everyone():
                if not person.prescreener:
                    continue
                try:
                    await person.run_prescreen(self.active_boards())
                except Exception:
                    log.exception("Pre-screen run for user %d failed", person.uid)

    async def _send_missed_digests(self) -> None:
        """A digest that was due while the service was off (or the PC slept), once, for each user."""
        for person in self.everyone():
            missed, send = ((self.digest_missed, self.send_digest) if person is self.me   # (tests stand in for user 1's)
                            else (person.digest_missed, person.send_digest))
            if missed():
                try:
                    await send()
                except Exception:
                    log.exception("Missed digest for user %d failed", person.uid)

    async def _daily_loop(self) -> None:
        """Each user's digest at their own time; the cost-of-living data is refreshed when it's due."""
        await self._boards_started.wait()
        refreshed = 0.0
        while True:
            if time.monotonic() - refreshed > 3600:
                refreshed = time.monotonic()
                try:
                    await self.refresh_pay_data()
                except Exception:
                    log.exception("Cost-of-living refresh failed")
            for person in self.everyone():
                try:
                    if person.digest_due():
                        await person.send_digest()
                except Exception:
                    log.exception("Digest for user %d failed", person.uid)
            await asyncio.sleep(30)

    async def _dashboard_loop(self) -> None:
        """Home Assistant sensor and map page, every homeassistant.interval and right after new matches."""
        cfg = self.cfg.homeassistant
        while True:
            self._dashboard_stale.clear()
            if self.homeassistant:
                try:
                    await self.publish_dashboard()
                except Exception as e:  # HA down or token wrong: log it, keep polling jobs
                    log.warning("Home Assistant (MQTT) update failed: %s", e)
            if self.map_enabled:
                try:
                    await self.refresh_map()
                except Exception as e:  # never let the map stop the dashboard
                    log.exception("Map page update failed: %s", e)
            try:
                await asyncio.wait_for(self._dashboard_stale.wait(), cfg.interval)
            except asyncio.TimeoutError:
                pass

    async def publish_dashboard(self) -> int:
        total, jobs = self.top_matches()
        _, step_ups = self.top_matches(by="step_up")
        await self.homeassistant.publish(total, jobs, ai_cost(self.store, self.cfg.scoring), step_ups)
        return total

