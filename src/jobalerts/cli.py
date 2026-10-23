"""Command line: jobalerts run | poll | discover | test-notify | digest | stats"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
from contextlib import asynccontextmanager

from . import discover
from .config import Config, load_config
from .http import HttpClient
from .models import utcnow
from .notify import Notifier
from .scheduler import Runner
from .scorer import Scorer
from .store import Store

log = logging.getLogger("jobalerts")

# Claude Haiku 5.5 list prices (USD per million tokens), for the cost estimate in `stats`.
HAIKU_INPUT_PER_M, HAIKU_OUTPUT_PER_M = 0.10, 0.50


def make_scorer(cfg: Config, store: Store) -> Scorer | None:
    if not cfg.scoring.enabled:
        return None
    profile_path = cfg.resolve(cfg.scoring.profile_path)
    if not profile_path.is_file():
        log.warning("Scoring enabled but %s is missing; alerts will use keywords only", profile_path)
        return None
    try:
        return Scorer(cfg.scoring, store, profile_path.read_text(encoding="utf-8"))
    except Exception as e:  # e.g. no Anthropic credentials configured
        log.warning("AI scoring unavailable (%s); alerts will use keywords only", e)
        return None


@asynccontextmanager
async def open_runner(cfg: Config, *, dry_run: bool = False, memory_store: bool | None = None):
    store = Store(":memory:" if (dry_run if memory_store is None else memory_store) else cfg.database)
    http = HttpClient(cfg.contact_email, cfg.polling.host_min_interval)
    notifier = Notifier(cfg.notify, dry_run=dry_run)
    try:
        yield Runner(cfg, store, http, notifier, None if dry_run else make_scorer(cfg, store))
    finally:
        store.add_host_stats(http.drain_stats())
        await http.aclose()
        await notifier.aclose()
        store.close()


def select_boards(cfg: Config, text: str | None):
    boards = cfg.boards()
    if text:
        t = text.lower()
        boards = [b for b in boards if t in b.key.lower() or t in b.name.lower()]
    return boards


async def cmd_run(cfg: Config, args) -> int:
    async with open_runner(cfg) as runner:
        await runner.run_forever()
    return 0


async def cmd_poll(cfg: Config, args) -> int:
    boards = select_boards(cfg, args.board)
    if not boards:
        print("No boards match.")
        return 1
    async with open_runner(cfg, dry_run=args.dry_run) as runner:
        if args.dry_run:
            results = await asyncio.gather(*(runner.preview_board(b) for b in boards), return_exceptions=True)
        else:
            results = await asyncio.gather(*(runner.poll_board(b) for b in boards), return_exceptions=True)
        failed = 0
        all_matches = []
        for board, res in zip(boards, results):
            if isinstance(res, BaseException):
                failed += 1
                print(f"{board.key:45} ERROR {type(res).__name__}: {res}")
            elif args.dry_run:
                fetched, matches = res
                all_matches.extend(matches)
                print(f"{board.key:45} {fetched:5} jobs {len(matches):4} matches")
            else:
                note = "baseline (silent)" if res.baseline else f"{res.new} new, {len(res.matched)} matched, {res.alerted} alerted"
                print(f"{board.key:45} {res.fetched:5} jobs  {note}")
        if args.dry_run and all_matches:
            print(f"\nMatches published in the last {cfg.criteria.max_age_days} days (would alert if new):")
            for j in sorted(all_matches, key=lambda j: j.published_at or utcnow(), reverse=True):
                age = j.age_days()
                when = f"{age * 24:.0f}h" if age is not None and age < 2 else (f"{age:.0f}d" if age is not None else "?")
                extra = " · ".join(filter(None, [j.location, j.salary]))
                print(f"  [{when:>4}] {j.company}: {j.title} — {extra}\n         {j.url}")
            if args.score:
                await preview_scores(cfg, runner, boards, all_matches[: args.score])
        print("\nRequests per host:")
        for host, c in sorted(runner.http.stats.items()):
            print(f"  {host:45} {c['requests']:4} requests  {c['not_modified']} not-modified  "
                  f"{c['rate_limited']} rate-limited  {c['errors']} errors")
        if args.dry_run:
            print("\nDry run: nothing stored, nothing sent.")
    return 1 if failed == len(boards) else 0


async def preview_scores(cfg: Config, runner: Runner, boards, jobs) -> None:
    scorer = make_scorer(cfg.model_copy(update={"scoring": cfg.scoring.model_copy(update={"enabled": True})}),
                         runner.store)
    if not scorer:
        print("\n(--score skipped: AI scoring isn't configured)")
        return
    by_key = {b.slug: b for b in boards}
    from .sources import SOURCES
    print(f"\nAI scores ({cfg.scoring.model}):")
    for job in jobs:
        board = by_key.get(job.board)
        if board and not job.description:
            try:
                await SOURCES[job.source].enrich(job, board, runner.http)
            except Exception as e:
                print(f"  (couldn't fetch description for {job.title}: {e})")
        s = await scorer.score(job)
        print(f"  {s.score if s else '--':>3}  {job.company}: {job.title}")
        if s:
            print(f"       {s.summary}" + (f"  ⚠ {'; '.join(s.red_flags)}" if s.red_flags else ""))


async def cmd_discover(cfg: Config, args) -> int:
    http = HttpClient(cfg.contact_email, cfg.polling.host_min_interval)
    try:
        target = args.target
        if target.startswith(("http://", "https://")):
            found = await discover.find_by_url(target, http, name=args.name or "")
        else:
            found = await discover.find_by_name(target, http)
            if args.name:
                for f in found:
                    f.name = args.name
    finally:
        await http.aclose()
    if not found:
        print(f"No public Greenhouse/Lever/Ashby/SmartRecruiters/Workday board found for {args.target!r}.\n"
              "Try its careers page URL instead: jobalerts discover https://company.com/careers")
        return 1
    print("Found:")
    for f in found:
        print(f"  {f.ats:16} {f.slug:35} {f.jobs:5} open jobs")
    best = max(found, key=lambda f: f.jobs)
    path = cfg.resolve(cfg.companies_file)
    if args.add:
        if discover.append_to_companies(path, best, args.tier):
            print(f"Added {best.ats}:{best.slug} to {path} (restart the service to pick it up)")
        else:
            print(f"{best.ats}:{best.slug} is already in {path}")
    else:
        import json
        print(f"\ncompanies.yaml entry:\n  - {json.dumps(best.entry(args.tier))}\n(or rerun with --add)")
    return 0


async def cmd_test_notify(cfg: Config, args) -> int:
    if not cfg.notify.topic:
        print("Set NTFY_TOPIC in .env (and notify.topic: \"${NTFY_TOPIC}\" in config.yaml) first.")
        return 1
    notifier = Notifier(cfg.notify)
    try:
        await notifier.send("Example Co: Senior Network Engineer",
                            "Remote · $150K-180K\nFit 88/100: test alert from jobalerts. Tap to check the link opens.",
                            priority=4, tags=["briefcase"], click="https://docs.ntfy.sh/",
                            actions=[{"action": "view", "label": "Open posting", "url": "https://docs.ntfy.sh/"}])
    finally:
        await notifier.aclose()
    print(f"Sent to {cfg.notify.server.rstrip('/')}/{cfg.notify.topic}")
    return 0


async def cmd_digest(cfg: Config, args) -> int:
    async with open_runner(cfg, dry_run=args.dry_run, memory_store=False) as runner:
        print(await runner.send_digest(mark=not args.dry_run))
    return 0


async def cmd_stats(cfg: Config, args) -> int:
    store = Store(cfg.database)
    try:
        print("Boards:")
        for b in store.boards():
            state = f"FAILING x{b['failures']}: {(b['last_error'] or '')[:70]}" if b["failures"] else "ok"
            print(f"  {b['key']:45} {b['last_count'] or 0:5} jobs  last ok {b['last_ok_at'] or 'never'}  {state}")
        for label, days in (("Today", 1), ("Last 7 days", 7)):
            hosts = store.host_stats(days)
            total = sum(c["requests"] for c in hosts.values())
            print(f"\nRequests, {label.lower()}: {total}")
            for host, c in sorted(hosts.items(), key=lambda kv: -kv[1]["requests"])[:15]:
                print(f"  {host:45} {c['requests']:6} req  {c['not_modified']:5} 304  "
                      f"{c['rate_limited']:3} 429/503  {c['errors']:3} errors")
        from datetime import timedelta
        for label, days in (("24h", 1), ("7d", 7)):
            activity = store.activity_since(utcnow() - timedelta(days=days))
            print(f"\nJobs first seen, {label}: " + (", ".join(f"{k} {v}" for k, v in activity.most_common()) or "none"))
        latency = store.latency_minutes()
        if latency:
            q = statistics.quantiles(latency, n=10) if len(latency) >= 2 else latency * 9
            print(f"\nDetection delay (first seen - published), 7d, {len(latency)} jobs: "
                  f"median {statistics.median(latency):.0f} min, p90 {q[-1]:.0f} min")
        usage = store.ai_usage()
        if usage:
            calls = sum(r["calls"] for r in usage)
            cost = sum(r["input_tokens"] * HAIKU_INPUT_PER_M + r["output_tokens"] * HAIKU_OUTPUT_PER_M
                       for r in usage) / 1e6
            print(f"\nAI scoring, 7d: {calls} calls, ~${cost:.3f} at Haiku 5.5 list prices")
    finally:
        store.close()
    return 0


COMMANDS = {"run": cmd_run, "poll": cmd_poll, "discover": cmd_discover, "test-notify": cmd_test_notify,
            "digest": cmd_digest, "stats": cmd_stats}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobalerts", description="Watch employer job boards and push new matches to your phone.")
    parser.add_argument("-c", "--config", default=os.environ.get("JOBALERTS_CONFIG", "config/config.yaml"),
                        help="path to config.yaml (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the service: poll every board on its tier interval, forever")
    p = sub.add_parser("poll", help="poll every board once")
    p.add_argument("--once", action="store_true", help="single pass (the default; accepted for clarity)")
    p.add_argument("--dry-run", action="store_true", help="fetch and filter only: store nothing, send nothing")
    p.add_argument("--board", help="only boards whose key or company name contains this text")
    p.add_argument("--score", type=int, default=0, metavar="N", help="with --dry-run: AI-score the first N matches")
    d = sub.add_parser("discover", help="find a company's ATS board by name or careers URL")
    d.add_argument("target", help='company name ("Cloudflare") or careers page URL')
    d.add_argument("--add", action="store_true", help="append the result to companies.yaml")
    d.add_argument("--tier", default="normal", help="polling tier for --add (default: %(default)s)")
    d.add_argument("--name", help="display name to use for the company")
    sub.add_parser("test-notify", help="send a sample alert to your ntfy topic")
    g = sub.add_parser("digest", help="send the daily digest now")
    g.add_argument("--dry-run", action="store_true", help="print it instead of sending")
    sub.add_parser("stats", help="board health, request rates, detection delay, AI spend")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    try:
        return asyncio.run(COMMANDS[args.command](cfg, args)) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
