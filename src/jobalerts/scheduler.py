"""The pipeline (fetch -> dedupe -> filter -> score -> route -> notify) and the polling loops."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import discover
from .config import Board, Config
from .filters import JobFilter
from .http import HttpClient
from .models import Job, normalize_company, utcnow
from .notify import Notifier
from .scorer import FitScore, Scorer
from .sources import SOURCES, Source, SourceConfigError
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class PollResult:
    board: str
    fetched: int = 0
    new: int = 0
    baseline: bool = False
    alerted: int = 0
    matched: list[Job] = field(default_factory=list)


class Runner:
    def __init__(self, cfg: Config, store: Store, http: HttpClient, notifier: Notifier,
                 scorer: Scorer | None = None):
        self.cfg = cfg
        self.store = store
        self.http = http
        self.notifier = notifier
        self.scorer = scorer
        self.filter = JobFilter(cfg.criteria)
        self.watched = {normalize_company(name) for name in cfg.watched_companies()}
        self._failure_alerted: set[str] = set()

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
        if new:
            log.info("%s: %d new, %d matched, %d alerted", board.key, len(new), len(result.matched), result.alerted)
        return result

    async def _process(self, job: Job, board: Board, source: Source) -> tuple[str, FitScore | None]:
        if self.store.seen_elsewhere(job):
            self.store.insert_job(job, "duplicate", "already seen from another source")
            return "duplicate", None
        if self.filter.is_stale(job):
            self.store.insert_job(job, "stale", f"published {job.age_days():.0f} days ago")
            return "stale", None
        ok, why = self.filter.check_basic(job)
        if not ok:
            self.store.insert_job(job, "filtered", why)
            return "filtered", None

        self.store.insert_job(job, "processing", why)
        if not board.watched and normalize_company(job.company) not in self.watched:
            self.store.add_suggestion(job.company)

        if not job.description and (self.scorer or self.filter.description_exclude):
            try:
                await source.enrich(job, board, self.http)
            except Exception as e:
                log.warning("%s: couldn't fetch description for %s: %s", board.key, job.key, e)
            if self.filter.is_stale(job):  # enrichment can replace an approximate date with the real one
                self.store.update_job(job.key, status="stale", reason="published date after enrichment")
                return "stale", None
        ok, why_desc = self.filter.check_description(job)
        if not ok:
            self.store.update_job(job.key, status="filtered", reason=why_desc)
            return "filtered", None

        score = await self.scorer.score(job) if self.scorer else None
        if score is None:
            status = "alert"  # keyword match and no score available: better to over-notify than miss it
        elif score.score >= self.cfg.scoring.push_threshold:
            status = "alert"
        elif score.score >= self.cfg.scoring.digest_threshold:
            status = "digest"
        else:
            status = "low"
        self.store.update_job(
            job.key, status=status,
            score=score.score if score else None,
            score_summary=score.summary if score else None,
            red_flags="; ".join(score.red_flags) if score and score.red_flags else None,
        )
        return status, score

    async def _notify(self, job: Job, score: FitScore | None) -> int:
        try:
            await self.notifier.job_alert(job, score)
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
        boards = self.cfg.boards()
        if not boards:
            raise SystemExit("No boards configured: add companies to companies.yaml or enable an aggregator.")
        log.info("Watching %d boards", len(boards))
        await self.notifier.send("Job alerts running", f"Watching {len(boards)} boards.", priority=2, tags=["eyes"])
        tasks = [asyncio.create_task(self._board_loop(b), name=b.key) for b in boards]
        tasks.append(asyncio.create_task(self._daily_loop(), name="digest"))
        await asyncio.gather(*tasks)

    async def _board_loop(self, board: Board) -> None:
        # Spread the first polls over the first couple of minutes instead of one burst.
        await asyncio.sleep(random.uniform(0, min(board.interval, 120)))
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
            await asyncio.sleep(delay * random.uniform(1 - jitter, 1 + jitter))

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
                await self.send_digest()
            except Exception:
                log.exception("Digest failed")

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
                     f"{activity['digest'] + activity['digested']} for digest, {activity['low']} low-fit. "
                     f"{requests} requests, {limited} rate-limited, {errors} errors.")
        failing = [b for b in self.store.boards() if b["failures"]]
        if failing:
            lines.append("Failing boards: " + "; ".join(
                f"{b['key']} ({b['failures']}x: {(b['last_error'] or '')[:80]})" for b in failing[:5]))

        message = "\n".join(lines)
        await self.notifier.send(f"Job digest: {len(rows)} to review", message, priority=3, tags=["clipboard"])
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
