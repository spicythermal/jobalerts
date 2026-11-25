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

from . import col, colmap, discover
from .dashboard import MqttPublisher, ai_cost, job_link, top_matches
from .config import Board, Config
from .filters import JobFilter
from .http import HttpClient, posting_gone
from .models import Job, normalize_company, utcnow
from .notify import Notifier
from .prescreen import Prescreener
from .scorer import FitScore, Scorer
from .sources import SOURCES, Source, SourceConfigError, without_missing_keys
from .store import Store, job_from_row, stories_from_row
from .taxes import Taxes

log = logging.getLogger(__name__)

STEP_UP_SHOWN = 60  # step-up scores from here on are shown with the job (the scorer's "real step up")


@dataclass
class PollResult:
    board: str
    fetched: int = 0
    new: int = 0
    baseline: bool = False
    alerted: int = 0
    matched: list[Job] = field(default_factory=list)


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


class Runner:
    def __init__(self, cfg: Config, store: Store, http: HttpClient, notifier: Notifier,
                 scorer: Scorer | None = None, homeassistant: MqttPublisher | None = None,
                 prescreener: Prescreener | None = None):
        self.cfg = cfg
        self.store = store
        self.http = http
        self.notifier = notifier
        self.scorer = scorer
        self.prescreener = prescreener if scorer else None  # its "yes" jobs need the full score
        self.filter = JobFilter(cfg.criteria)
        self.watched = {normalize_company(name) for name in cfg.watched_companies()}
        self._failure_alerted: set[str] = set()
        self.homeassistant = homeassistant
        self._dashboard_stale = asyncio.Event()  # set after new matches so Home Assistant updates right away
        self.map_html: str | None = None    # the latest cost-of-living map page (web UI /map)
        self.vault = None                   # API keys set on the Settings page (vault.Vault), set by cli.open_runner
        self.ui_settings = None             # settings.UiSettings, set by cli.open_runner / the web app
        self.base_cfg = cfg                 # config.yaml + config.local.yaml, before the UI's settings
        self.rebuild = None                 # (cfg, store) -> (scorer, prescreener, homeassistant); cli.open_runner
        self.running = False                # True inside run_forever (board loops are live)
        self.board_tasks: dict[str, tuple[Board, asyncio.Task]] = {}
        self.next_poll: dict[str, float] = {}  # board key -> unix time of its next poll
        self._map_built = 0.0
        self._cap_noticed = ""                     # UTC day of the last "AI daily cap reached" push
        self.pay: col.PayIndex | None = None
        self.pay_problem = ""  # shown in the digest until fixed
        self.load_pay()

    def reconfigure(self, cfg: Config) -> None:
        """Use new settings without a restart (Settings page): filter, notifications, pay index, AI
        scoring and Home Assistant. Board intervals and the watch list reload with the boards."""
        self.cfg = cfg
        self.filter = JobFilter(cfg.criteria)
        self.notifier.cfg = cfg.notify
        self.watched = {normalize_company(name) for name in cfg.watched_companies()}
        self.pay, self.pay_problem = None, ""
        self.load_pay()
        if self.rebuild:
            self.scorer, prescreener, self.homeassistant = self.rebuild(cfg, self.store)
            self.prescreener = prescreener if self.scorer else None
        self.map_html = None
        self._dashboard_stale.set()
        self.sync_boards()   # a key saved in Settings starts its aggregator searches

    # ------------------------------------------------------------------ pipeline
    async def poll_board(self, board: Board) -> PollResult:
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

        alerts_left = self.cfg.polling.max_alerts_per_poll
        overflow = 0
        for job in new:
            try:
                status, score = await self._process(job, board, source)
            except Exception as e:  # one bad posting must not stop the rest of the board
                log.exception("%s: failed processing %s", board.key, job.key)
                self.store.insert_job(job, "error", str(e)[:200])
                self.store.update_job(job.key, status="error", reason=str(e)[:200])
                continue
            if status in ("alert", "digest", "low"):
                result.matched.append(job)
            if status != "alert":
                continue
            if alerts_left > 0:
                alerts_left -= 1
                result.alerted += await self._notify(job, score)
            else:
                overflow += 1
                self.store.update_job(job.key, status="digest", reason="per-poll alert cap reached")
        if overflow:
            await self.notifier.send(
                f"{board.name}: {overflow} more matches",
                f"{overflow} more new matching jobs at {board.name} were held back to avoid a flood. "
                "They'll be in the next digest.", priority=3, tags=["inbox_tray"])
        self.store.mark_board_ok(board.key, len(jobs))
        if result.matched:
            self._dashboard_stale.set()
        if new:
            log.info("%s: %d new, %d matched, %d alerted", board.key, len(new), len(result.matched), result.alerted)
        return result

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
                status, score = await self._process(job, board, SOURCES[job.source])
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

    async def rescore(self, boards: list[Board], days: int | None = None) -> tuple[list[tuple[Job, str, str, FitScore | None]], int]:
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

    async def _process(self, job: Job, board: Board, source: Source) -> tuple[str, FitScore | None]:
        if self.store.seen_elsewhere(job):
            self.store.insert_job(job, "duplicate", "already seen from another source")
            return "duplicate", None
        if self.filter.is_stale(job):
            self.store.insert_job(job, "stale", f"published {job.age_days():.0f} days ago")
            return "stale", None
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
        log.info("Pay index: %s %s = 1.0 (%s); data built %s from %s", f"${cfg.reference_salary:,.0f}",
                 self.pay.home.label, f"take-home, {taxes.year} taxes" if taxes else "gross pay",
                 data.get("built"), path)
        if taxes and (problem := taxes.problem()):
            log.warning("Pay index: %s", problem)

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
        self.pay_problem = ""
        self.load_pay()
        self._dashboard_stale.set()  # the map's numbers changed
        return f"Cost-of-living data rebuilt into {path}: {data['sources']['us']}; {len(data['countries'])} countries"

    async def _cap_notice(self) -> None:
        today = utcnow().date().isoformat()
        if self._cap_noticed == today:
            return
        self._cap_noticed = today
        try:
            await self.notifier.send(
                "AI daily cap reached",
                f"All {self.cfg.scoring.daily_max_calls} of today's Claude calls are used (scoring.daily_max_calls). "
                "New matches go to the digest unscored until 00:00 UTC instead of being pushed.",
                priority=3, tags=["warning"])
        except Exception as e:
            log.warning("Couldn't send the AI cap notice: %s", e)

    async def _notify(self, job: Job, score: FitScore | None) -> int:
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

    async def preview_board(self, board: Board) -> tuple[int, list[Job]]:
        """Dry run: fetch and filter only. Nothing is stored or sent."""
        jobs = await SOURCES[board.source].fetch(board, self.http)
        matches = [j for j in jobs if not self.filter.is_stale(j) and self.filter.check_basic(j)[0]]
        return len(jobs), matches

    # ------------------------------------------------------------------ service loops
    async def run_forever(self) -> None:
        boards, skipped = without_missing_keys(self.cfg.boards())
        if not boards:
            raise SystemExit("No boards configured: add companies to companies.yaml or enable an aggregator.")
        note = ""
        if skipped:
            note = " Not running (no API key in .env): " + ", ".join(sorted(skipped)) + "."
            for source, keys in sorted(skipped.items()):
                log.info("%s is enabled but skipped until %s is set in .env", source, " and ".join(keys))
        if not self.prescreener and (n := self.store.drop_prescreen_queue()):
            log.info("AI pre-screen off: %d waiting jobs marked filtered", n)
        log.info("Watching %d boards", len(boards))
        await self.notifier.send("Job alerts running", f"Watching {len(boards)} boards.{note}",
                                 priority=2, tags=["eyes"])
        self.running = True
        self.sync_boards()
        tasks = [asyncio.create_task(self._supervise_boards(), name="boards"),
                 asyncio.create_task(self._daily_loop(), name="digest")]
        if self.prescreener:
            tasks.append(asyncio.create_task(self._prescreen_loop(), name="prescreen"))
        tasks.append(asyncio.create_task(self.refresh_pay_data(), name="col-refresh"))
        if self.cfg.map.enabled and not self.pay:
            log.warning("map.enabled is set but the pay index is off (pay.enabled); no map")
        if self.homeassistant or self.map_enabled:
            tasks.append(asyncio.create_task(self._dashboard_loop(), name="dashboard"))
        if self.cfg.web.enabled:
            from .web.app import serve
            tasks.append(asyncio.create_task(serve(self), name="web"))
        await asyncio.gather(*tasks)

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

    async def _supervise_boards(self) -> None:
        """Waits on the board loops (they run until stopped); returns when none are left."""
        while self.board_tasks:
            tasks = {task for _, task in self.board_tasks.values()}
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for key, (board, task) in list(self.board_tasks.items()):
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
        self.cfg.companies = companies
        self.watched = {normalize_company(name) for name in self.cfg.watched_companies()}
        self.sync_boards()
        self._dashboard_stale.set()

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
                    await self.notifier.send(f"Job board recovered: {board.name}", f"{board.key} is polling again.",
                                             priority=2, tags=["white_check_mark"])
            except SourceConfigError as e:
                log.error("%s disabled: %s", board.key, e)
                self.store.mark_board_failed(board.key, str(e))
                await self.notifier.send(f"Job board disabled: {board.name}", str(e), priority=3, tags=["warning"])
                return
            except Exception as e:
                failures = self.store.mark_board_failed(board.key, f"{type(e).__name__}: {e}")
                delay = min(board.interval * 2 ** min(failures, 6), self.cfg.polling.max_backoff)
                log.warning("%s failed (%d in a row): %s; next try in %ds", board.key, failures, e, delay)
                if failures >= self.cfg.polling.failure_alert_threshold and board.key not in self._failure_alerted:
                    self._failure_alerted.add(board.key)
                    await self.notifier.send(
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
        while True:
            await asyncio.sleep(self.cfg.prescreen.interval)
            try:
                await self.run_prescreen(self.active_boards())
            except Exception:
                log.exception("Pre-screen run failed")

    async def _daily_loop(self) -> None:
        tz = ZoneInfo(self.cfg.notify.timezone)
        hour, minute = (int(x) for x in self.cfg.notify.digest_time.split(":"))
        while True:
            now = datetime.now(tz)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                await self.refresh_pay_data()
            except Exception:
                log.exception("Cost-of-living refresh failed")
            try:
                await self.send_digest()
            except Exception:
                log.exception("Digest failed")

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

    def top_matches(self, n: int | None = None, days: int | None = None, by: str = "score") -> tuple[int, list[dict]]:
        """Best recent matches by AI fit score, or with by="step_up" the biggest credible promotions."""
        cfg = self.cfg.homeassistant
        return top_matches(self.store, self.filter, days or cfg.days, n or cfg.top_n, self.pay, by,
                           self.cfg.web.public_url)

    @property
    def map_enabled(self) -> bool:
        return bool(self.cfg.map.enabled and self.pay)

    async def refresh_map(self) -> str:
        self.map_html = await self.map_page()
        self._map_built = time.monotonic()
        return self.map_html

    async def current_map(self) -> str:
        """The map page for the web UI: the dashboard loop keeps it fresh in `run`; `jobalerts web`
        (no loops) rebuilds it when it's older than homeassistant.interval."""
        if self.map_html is None or time.monotonic() - self._map_built > self.cfg.homeassistant.interval:
            await self.refresh_map()
        return self.map_html

    async def map_page(self) -> str:
        """The cost-of-living map with the current top jobs (the price math runs off the event loop)."""
        _, jobs = self.top_matches()
        _, step_ups = self.top_matches(by="step_up")
        pay = self.pay
        return await asyncio.to_thread(lambda: colmap.render(colmap.build_map_data(pay, jobs, step_ups)))

    async def publish_dashboard(self) -> int:
        total, jobs = self.top_matches()
        _, step_ups = self.top_matches(by="step_up")
        await self.homeassistant.publish(total, jobs, ai_cost(self.store, self.cfg.scoring), step_ups)
        return total

    # ------------------------------------------------------------------ digest
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

        suggestions = await self._resolve_suggestions()
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
        failing = [b for b in self.store.boards() if b["failures"]]
        if failing:
            lines.append("Failing boards: " + "; ".join(
                f"{b['key']} ({b['failures']}x: {(b['last_error'] or '')[:80]})" for b in failing[:5]))

        message = "\n".join(lines)
        home = self.cfg.web.public_url.rstrip("/")
        await self.notifier.send(f"Job digest: {len(rows)} to review", message, priority=3, tags=["clipboard"],
                                 click=f"{home}/" if home else None)
        if mark:
            self.store.mark_digested([r["key"] for r in rows])
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
