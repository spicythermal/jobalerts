"""Command line: jobalerts run | web | set-password | poll | discover | test-notify | digest | top | stats | backfill | map | pay"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from . import col, discover
from .config import Config, load_config
from .dashboard import MqttPublisher, ai_cost
from .http import HttpClient
from .models import utcnow
from .notify import Notifier
from .prescreen import Prescreener
from .resumes import load_resumes
from .stories import load_stories
from .scheduler import Runner
from .scorer import Scorer, ai_cost_usd
from .sources import without_missing_keys
from .store import Store, job_from_row
from .runstate import InstanceLock
from .userdata import seed_user_files
from .settings import UiSettings, apply as apply_settings
from .vault import Vault, install as install_vault, install_log_redaction, secret_key

log = logging.getLogger("jobalerts")


INTENT = {
    "stay": "I want to stay where I live ({home}): only local or remote roles.",
    "open": ("I'd move for an exceptional role, if it clearly pays off after cost of living (pay index "
             "{floor:g}+ against staying in {home})."),
    "any": "I'm happy to relocate for the right role; where I live now ({home}) isn't a constraint.",
}


def intent_note(cfg: Config) -> str:
    """The Settings page's stay-or-move choice, as a line of the profile the AI reads."""
    home = cfg.pay.reference_location
    text = INTENT[cfg.intent.move].format(home=home, floor=cfg.pay.move_min_index)
    return f"\n\n## Stay or move (from the Settings page)\n- {text}\n"


def _profile_and_resumes(cfg: Config):
    profile_path = cfg.resolve(cfg.scoring.profile_path)
    profile = profile_path.read_text(encoding="utf-8") if profile_path.is_file() else ""
    profile = profile.rstrip() + intent_note(cfg) if profile.strip() else ""
    return (profile_path, profile, load_resumes(cfg.resolve(cfg.scoring.resume_dir)),
            load_stories(cfg.resolve(cfg.scoring.stories_dir)))


def make_prescreener(cfg: Config, store: Store, scorer: Scorer | None) -> Prescreener | None:
    """The AI second look at jobs the keywords turned down; needs scoring, since its picks get scored."""
    if not cfg.prescreen.enabled or scorer is None:
        return None
    _, profile, resumes, _ = _profile_and_resumes(cfg)
    log.info("AI pre-screen on: jobs the keywords turn down get a second look every %d min",
             cfg.prescreen.interval // 60)
    return Prescreener(cfg.prescreen, cfg.scoring, store, profile, resumes, client=scorer.client)


def make_scorer(cfg: Config, store: Store) -> Scorer | None:
    if not cfg.scoring.enabled:
        log.info("AI scoring off (scoring.enabled: false); alerts use keywords only")
        return None
    profile_path, profile, resumes, stories = _profile_and_resumes(cfg)
    if not profile and not resumes and not stories:
        log.warning("Scoring enabled but there's no %s, no resumes in %s and no stories in %s; alerts will use "
                    "keywords only", profile_path, cfg.resolve(cfg.scoring.resume_dir),
                    cfg.resolve(cfg.scoring.stories_dir))
        return None
    if stories:
        log.info("AI scoring against %d work stories (resumes only pick the version to apply with: %s)",
                 len(stories), ", ".join(r.name for r in resumes) or "none")
    else:
        log.info("AI scoring against %d resume(s): %s", len(resumes), ", ".join(r.name for r in resumes) or "none")
    try:
        return Scorer(cfg.scoring, store, profile, resumes=resumes, stories=stories)
    except Exception as e:  # e.g. no Anthropic credentials configured
        log.warning("AI scoring unavailable (%s); alerts will use keywords only", e)
        return None


def make_homeassistant(cfg: Config) -> MqttPublisher | None:
    if not cfg.homeassistant.enabled:
        return None
    publisher = MqttPublisher(cfg.homeassistant)
    if not publisher.configured:
        log.warning("homeassistant.enabled is on but MQTT_HOST is empty; not updating Home Assistant")
        return None
    return publisher


def rebuild_services(cfg: Config, store: Store):
    """The parts that depend on settings, profile, stories, resumes and keys (after a Settings change)."""
    scorer = make_scorer(cfg, store)
    return scorer, make_prescreener(cfg, store, scorer), make_homeassistant(cfg)


@asynccontextmanager
async def open_runner(cfg: Config, *, dry_run: bool = False, memory_store: bool | None = None):
    store = Store(":memory:" if (dry_run if memory_store is None else memory_store) else cfg.database)
    # API keys set on the Settings page live encrypted in the real database (also for dry runs).
    key_store = store if str(cfg.database) and store_is_file(store) else (
        Store(cfg.database) if Path(cfg.database).is_file() else None)
    vault = Vault(key_store, secret_key(cfg)) if key_store else None
    install_vault(vault)
    base_cfg, ui = cfg, (UiSettings(key_store, vault) if vault else None)
    if ui:
        try:
            cfg = apply_settings(base_cfg, ui.overlay())
        except ValidationError as e:
            log.warning("Settings saved on the web UI don't fit this version's config; using the files only: %s", e)
    http = HttpClient(cfg.contact_email, cfg.polling.host_min_interval)
    notifier = Notifier(cfg.notify, dry_run=dry_run)
    homeassistant = None if dry_run else make_homeassistant(cfg)
    try:
        scorer = None if dry_run else make_scorer(cfg, store)
        runner = Runner(cfg, store, http, notifier, scorer, homeassistant, make_prescreener(cfg, store, scorer))
        runner.vault, runner.ui_settings, runner.base_cfg = vault, ui, base_cfg
        if not dry_run:
            runner.rebuild = rebuild_services
        yield runner
    finally:
        install_vault(None)
        if key_store is not None and key_store is not store:
            key_store.close()
        store.add_host_stats(http.drain_stats())
        await http.aclose()
        await notifier.aclose()
        store.close()


def store_is_file(store: Store) -> bool:
    return store.db.execute("PRAGMA database_list").fetchone()["file"] != ""


def select_boards(cfg: Config, text: str | None):
    boards, skipped = without_missing_keys(cfg.boards())
    for source, keys in sorted(skipped.items()):
        log.info("Skipping %s: set %s in .env to use it", source, " and ".join(keys))
    if text:
        t = text.lower()
        boards = [b for b in boards if t in b.key.lower() or t in b.name.lower()]
    return boards


async def cmd_run(cfg: Config, args) -> int:
    lock = InstanceLock(Path(cfg.database).resolve().parent / "jobalerts.lock")
    if not lock.acquire():
        print(f"jobalerts is already running on {cfg.database} (process {lock.holder()}). "
              "One `run` per database; `jobalerts web` can run alongside it.")
        return 1
    stop_on_signals()
    try:
        async with open_runner(cfg) as runner:
            try:
                await runner.run_forever()
            except asyncio.CancelledError:
                log.info("Stopped")
    finally:
        lock.release()
    return 0


def stop_on_signals() -> None:
    """SIGTERM (docker stop, systemd) and SIGINT end the current command cleanly: the running task is
    cancelled, so every loop and the web UI stop and the database and lock are closed. Needed in a
    container, where the process is PID 1 and would otherwise ignore SIGTERM. (Windows has no such
    handlers; Ctrl-C still stops it.)"""
    import signal
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except (NotImplementedError, RuntimeError, ValueError):
            pass


async def cmd_web(cfg: Config, args) -> int:
    """The web UI alone, over the same database: no polling, no pushes (for a PC that only browses)."""
    from .web.app import serve
    stop_on_signals()
    async with open_runner(cfg, memory_store=False) as runner:
        try:
            await serve(runner)
        except asyncio.CancelledError:
            log.info("Stopped")
    return 0


async def cmd_set_password(cfg: Config, args) -> int:
    """Set or reset the web UI password from the machine running jobalerts (logs out every session)."""
    import getpass
    from .web.app import META_HASH, META_VER
    from .web.auth import hash_password, password_problem
    password = getpass.getpass("New web UI password: ")
    if problem := password_problem(password, getpass.getpass("Again: ")):
        print(problem)
        return 1
    store = Store(cfg.database)
    try:
        store.set_meta(META_HASH, hash_password(password))
        store.set_meta(META_VER, str(int(store.get_meta(META_VER) or 0) + 1))
    finally:
        store.close()
    print("Password set. Every logged-in browser has to log in again.")
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
        if not args.dry_run and runner.prescreener:
            counts = await runner.run_prescreen(boards)
            if counts:
                print("\nAI pre-screen of jobs the keywords turned down: "
                      + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
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
            if s.top_story_titles:
                print("       stories: " + "; ".join(s.top_story_titles))
            if s.best_resume:
                print(f"       apply with: {s.best_resume}")


def read_targets(path: str) -> list[tuple[str, str]]:
    """Lines of `Company Name` or `Company Name | https://careers.url`; # starts a comment."""
    targets = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, url = (part.strip() for part in line.partition("|"))
        targets.append((name, url))
    return targets


async def discover_one(http: HttpClient, target: str, name: str = "") -> list[discover.Found]:
    if target.startswith(("http://", "https://")):
        return await discover.find_by_url(target, http, name=name)
    found = await discover.find_by_name(target, http)
    for f in found:
        f.name = name or f.name
    return found


async def cmd_discover(cfg: Config, args) -> int:
    targets = [(t, args.name or "") for t in args.target]
    if args.file:
        targets += [(url or name, name) for name, url in read_targets(args.file)]
    if not targets:
        print("Give a company name, a careers URL, or --file")
        return 1
    path = cfg.resolve(cfg.companies_file)
    missing = []
    http = HttpClient(cfg.contact_email, cfg.polling.host_min_interval)
    try:
        for target, name in targets:
            try:
                found = await discover_one(http, target, name)
            except Exception as e:  # a careers page that won't load shouldn't stop a batch
                print(f"{name or target}: lookup failed ({type(e).__name__}: {e})")
                found = []
            if not found:
                missing.append(name or target)
                if len(targets) == 1:
                    print(f"No Greenhouse/Lever/Ashby/SmartRecruiters/Personio/Workable board found for {target!r}.\n"
                          "Try its careers page URL instead: jobalerts discover https://company.com/careers")
                continue
            print(f"{name or target}:")
            for f in found:
                print(f"  {f.ats:16} {f.slug:35} {f.jobs:5} open jobs")
            best = max(found, key=lambda f: f.jobs)
            tier = args.tier or ("slow" if best.ats == "workday" else "normal")
            if args.add:
                if discover.append_to_companies(path, best, tier):
                    print(f"  added {best.ats}:{best.slug} to {path}")
                else:
                    print(f"  {best.ats}:{best.slug} is already in {path}")
            else:
                import json
                print(f"  companies.yaml entry:  - {json.dumps(best.entry(tier))}")
    finally:
        await http.aclose()
    if len(targets) > 1:
        print(f"\n{len(targets) - len(missing)} of {len(targets)} found."
              + (f" No supported board for: {', '.join(missing)}" if missing else ""))
    if args.add and len(missing) < len(targets):
        print("Restart the service to pick up new companies.")
    elif not args.add:
        print("(rerun with --add to append to companies.yaml)")
    return 1 if len(missing) == len(targets) else 0


def print_repriced(repriced, cfg: Config) -> None:
    closed = [r for r in repriced if r[2] == "closed"]
    repriced = [r for r in repriced if r[2] != "closed"]
    if closed:
        print(f"{len(closed)} postings were taken down since they were processed: "
              "marked closed, out of `top`, Home Assistant and the digest.")
        for job, *_ in closed:
            print(f"  {job.company}: {job.title}")
    priced = [r for r in repriced if r[3]]
    rescored = sum(1 for r in repriced if r[4])
    print(f"Pay index for {len(repriced)} matches already processed: {len(priced)} with a pay index, "
          f"{len(repriced) - len(priced)} without (no pay stated, or the place isn't known)."
          + (f" {rescored} re-scored by the AI to read their pay." if rescored else ""))
    for job, old, new, pay, ai in sorted(priced, key=lambda r: r[3].index, reverse=True):
        change = f"{old} -> {new}" if new != old else new
        print(f"  {pay.index:5.2f} {change:22} {job.company}: {job.title}{'  (AI)' if ai else ''}\n"
              f"        {pay.text()}")
    dropped = sum(1 for _, old, new, _, _ in repriced if new == "low_pay" and old != "low_pay")
    if dropped:
        print(f"{dropped} now low_pay (top of range under {cfg.pay.min_index:g}): out of `top` and Home Assistant.")
    print()


def print_rescored(rescored) -> None:
    scored = [r for r in rescored if r[3] is not None]
    closed = sum(1 for r in rescored if r[2] == "closed")
    print(f"Re-scored {len(scored)} of {len(rescored)} matches scored before the current stories and profile"
          + (f" ({closed} taken down since: closed)" if closed else "")
          + (f"; {len(rescored) - len(scored) - closed} couldn't be scored, run again to retry" if
             len(rescored) - len(scored) - closed else "") + ".")
    for job, old, new, fit in sorted(scored, key=lambda r: r[3].score, reverse=True):
        change = f"{old} -> {new}" if new != old else new
        print(f"  [{fit.score:>3}] {change:18} {job.company}: {job.title}")
        if fit.top_story_titles:
            print("        stories: " + "; ".join(fit.top_story_titles))
    print()


async def cmd_backfill(cfg: Config, args) -> int:
    if not Path(cfg.database).is_file():
        print(f"No database at {cfg.database}: run the service first so it records each board's baseline.")
        return 1
    days = args.days or cfg.criteria.max_age_days
    async with open_runner(cfg, dry_run=args.dry_run) as runner:
        if args.dry_run:
            # Work on an in-memory copy of the baseline so the real database is left exactly as it was.
            real = Store(cfg.database)
            try:
                rows = real.recent_candidates(utcnow() - timedelta(days=days))
            finally:
                real.close()
            runner.store.insert_jobs([job_from_row(r) for r in rows if r["status"] == "baseline"], "baseline")
            runner.scorer = make_scorer(cfg, runner.store)
        real = Store(cfg.database) if args.dry_run else None
        try:
            repriced = await runner.reprice(cfg.boards(), store=real, write=not args.dry_run, days=days)
        finally:
            if real:
                real.close()
        if repriced:
            print_repriced(repriced, cfg)
        if args.rescore and runner.scorer and not args.dry_run:
            rescored, left = await runner.rescore(cfg.boards(), days=days)
            print_rescored(rescored)
            if left:
                print(f"Stopped with {cfg.scoring.backfill_reserve} of today's AI calls kept for new jobs "
                      f"(scoring.backfill_reserve); {left} jobs left. Run it again after 00:00 UTC to continue.\n")
        elif args.rescore and args.dry_run:
            print("--rescore is skipped in a dry run (it makes AI calls and saves the new scores).\n")
        if not runner.scorer:
            print("AI scoring isn't configured: set ANTHROPIC_API_KEY in .env (and scoring.enabled: true).")
            return 0 if repriced else 1
        results = await runner.backfill(cfg.boards(), push=args.push and not args.dry_run, days=days)
        if not results:
            print(f"No unscored baseline matches posted in the last {days} days.")
            if (repriced or args.rescore) and runner.homeassistant and not args.dry_run:
                await runner.publish_dashboard()
            return 0
        print(f"\n{len(results)} baseline matches posted in the last {days} days:")
        results.sort(key=lambda r: -1 if r[2] is None else r[2].score, reverse=True)
        for job, status, score in results:
            if status == "alerted":
                status = "pushed"
            extra = " · ".join(filter(None, [job.location, job.salary]))
            print(f"  [{score.score if score else '--':>3}] {status:8} {job.company}: {job.title} — {extra}\n"
                  f"                 {job.url}")
            if score:
                print(f"                 {score.summary}" + (f"  ⚠ {'; '.join(score.red_flags)}" if score.red_flags else ""))
                if score.top_story_titles:
                    print("                 stories: " + "; ".join(score.top_story_titles))
                if score.best_resume:
                    print(f"                 apply with: {score.best_resume}")
        if args.dry_run:
            print("\nDry run: scores shown above, nothing stored or sent.")
            return 0
        held = "" if args.push else f" (and {cfg.scoring.push_threshold}+, since --push wasn't given)"
        print(f"\nSaved. Scores {cfg.scoring.digest_threshold}-{cfg.scoring.push_threshold - 1}{held} "
              "go in the next digest; `jobalerts top` and Home Assistant now show the scores.")
        unscored = sum(1 for _, status, _ in results if status == "unscored")
        if unscored:
            print(f"{unscored} couldn't be scored (see the log above); run backfill again to retry them.")
        if runner.homeassistant:
            try:
                await runner.publish_dashboard()
                print("Home Assistant sensor updated.")
            except Exception as e:
                print(f"Home Assistant (MQTT) update failed: {type(e).__name__}: {e}")
    return 0


async def cmd_map(cfg: Config, args) -> int:
    """Write the cost-of-living map page to a file (the service serves the same page on map.port)."""
    has_db = Path(cfg.database).is_file()
    async with open_runner(cfg, dry_run=True, memory_store=not has_db) as runner:
        if not runner.pay:
            print("The pay index is off (pay.enabled: false), so there's no map.")
            return 1
        html = await runner.map_page()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    jobs = "" if has_db else " (no database yet, so no jobs on it)"
    print(f"Wrote {out} ({len(html) // 1024} KB){jobs}. Open it in a browser.")
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


def _pay_text(j: dict) -> str:
    """"pay 1.22 rent (1.10-1.34) / 0.83 buy" from a top_matches row."""
    if j["pay_index"] is None:
        return ""
    spread = f" ({j['pay_low']:.2f}-{j['pay_high']:.2f})" if j["pay_low"] is not None else ""
    if j.get("buy_index") is None:
        return f"pay {j['pay_index']:.2f}{spread}"
    buy_spread = f" ({j['buy_low']:.2f}-{j['buy_high']:.2f})" if j["buy_low"] is not None else ""
    return f"pay {j['pay_index']:.2f} rent{spread} / {j['buy_index']:.2f} buy{buy_spread}"


def _print_top(jobs: list[dict], step_up: bool = False) -> None:
    for i, j in enumerate(jobs, 1):
        mark = j["step_up"] if step_up else j["score"]
        score = f"[{mark}] " if mark is not None else ""
        extra = " · ".join(filter(None, [j["location"], j["salary"], _pay_text(j), j["age"],
                                         "pushed" if j["pushed"] else ""]))
        print(f"{i:3}. {score}{j['company']}: {j['title']} — {extra}\n     {j['url']}")
        if step_up and j["step_up_note"]:
            print(f"     step up: {j['step_up_note']} (fit {j['score']})")
        elif j["summary"]:
            print(f"     {j['summary']}")
        if j["stories"]:
            print("     stories: " + "; ".join(j["stories"]))
        if j["best_resume"]:
            print(f"     apply with: {j['best_resume']}")


async def cmd_top(cfg: Config, args) -> int:
    async with open_runner(cfg, memory_store=False) as runner:
        days = args.days or cfg.homeassistant.days
        total, jobs = runner.top_matches(args.n, days)
        print(f"{total} matches posted in the last {days} days" + ("; best fits:" if jobs else "."))
        _print_top(jobs)
        _, step_ups = runner.top_matches(args.n, days, by="step_up")
        if step_ups:
            print("\nBiggest step ups (AI step-up score, a credible promotion):")
            _print_top(step_ups, step_up=True)
        if args.push:
            if not runner.homeassistant:
                print("\n--push: set homeassistant.enabled: true and MQTT_HOST (plus its login) in .env first.")
                return 1
            try:
                await runner.publish_dashboard()
            except Exception as e:  # e.g. ConnectionRefusedError, or MQTTException "Not authorized"
                print(f"\nMQTT publish to {cfg.homeassistant.mqtt_host}:{cfg.homeassistant.mqtt_port} failed: "
                      f"{type(e).__name__}: {e}")
                return 1
            cost = " and sensor.job_alerts_ai_cost_this_month" if ai_cost(runner.store, cfg.scoring) else ""
            print(f"\nPublished to {runner.homeassistant.state_topic} on {cfg.homeassistant.mqtt_host}. "
                  f"Home Assistant shows it as sensor.job_alerts{cost}.")
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
            cost = ai_cost_usd(usage, cfg.scoring.model)
            print(f"\nAI scoring, 7d: {calls} calls, ~${cost:.3f} at {cfg.scoring.model} list prices")
        if cost := ai_cost(store, cfg.scoring):
            print(f"AI cost this month: ~${cost['month']:.3f} ({cost['calls_month']} calls)")
    finally:
        store.close()
    return 0


async def cmd_pay(cfg: Config, args) -> int:
    async with open_runner(cfg, dry_run=True) as runner:
        if args.update:
            print(await runner.refresh_pay_data(force=True))
        if not runner.pay:
            print(runner.pay_problem or "Pay index is off (pay.enabled: false).")
            return 1
        pi = runner.pay
        data, home, ref = pi.data, pi.home, cfg.pay.reference_salary
        print(f"Reference: ${ref:,.0f} in {home.label} = 1.00")
        print(f"Prices built {data.get('built')} ({col.data_age_days(data)} days ago; refreshed after "
              f"{cfg.pay.refresh_days}):\n  US: {data['sources']['us']}\n  Abroad: {data['sources']['countries']}")
        if pi.housing_share:
            print(f"Housing: {pi.housing_share:.0%} of spending at {data['sources'].get('housing', 'HUD FMRs')}; "
                  f"the rest at RPP goods and other services")
        if pi.ownership:
            o = pi.ownership
            print(f"Buying: {data['sources'].get('homes')}; {data['sources'].get('mortgage')} = "
                  f"{data['mortgage_rate']:.2f}%; {o.down_payment:.0%} down, property tax {o.property_tax:.2%} "
                  f"(or by state), upkeep {o.upkeep:.2%} a year, utilities ${o.utilities:,.0f}/mo at US prices")
        elif cfg.pay.buy.enabled and pi.housing_share:
            print("Buying: no home prices in this table; run `jobalerts pay --update`")
        elif cfg.pay.housing_share:
            print("Housing: plain RPP (this price table has no HUD rents; run `jobalerts pay --update`)")
        if pi.taxes:
            t = pi.taxes
            print(f"Taxes: {t.year}, {t.data.get('source')} (built {t.data.get('built')}); {t.describe()}; "
                  "abroad approximate")
            if problem := t.problem():
                print(f"  note: {problem}")
        else:
            print("Taxes: off (pay.taxes: false), comparing gross pay")
        if not args.salary:
            return 0
        pay = col.parse_salary(args.salary)
        place = runner.pay.resolve(args.location, args.remote)
        if pay is None or place is None:
            print(f"\nCan't read {'the salary ' + repr(args.salary) if pay is None else 'the location ' + repr(args.location)}."
                  + ("" if pay is None else " Map it with pay.locations in config.yaml."))
            return 1
        result = runner.pay.index(pay, args.location, args.remote)
        if result is None:
            print(f"\nNo exchange rate for {pay.currency}.")
            return 1
        print(f"\n{args.salary!r} in {args.location!r}{' (remote)' if args.remote else ''}:")
        for label, text in pi.explain(result):
            print(f"  {label + ':':<12} " + text.replace("\n", "\n" + " " * 15))
    return 0


COMMANDS = {"run": cmd_run, "poll": cmd_poll, "discover": cmd_discover, "test-notify": cmd_test_notify,
            "digest": cmd_digest, "top": cmd_top, "stats": cmd_stats, "backfill": cmd_backfill, "pay": cmd_pay,
            "map": cmd_map, "web": cmd_web, "set-password": cmd_set_password}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobalerts", description="Watch employer job boards and push new matches to your phone.")
    parser.add_argument("-c", "--config", default=os.environ.get("JOBALERTS_CONFIG", "config/config.yaml"),
                        help="path to config.yaml (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the service: poll every board on its tier interval, forever, with the web UI")
    sub.add_parser("web", help="serve the web UI only (no polling or pushes), over the same database")
    sub.add_parser("set-password", help="set or reset the web UI password")
    p = sub.add_parser("poll", help="poll every board once")
    p.add_argument("--once", action="store_true", help="single pass (the default; accepted for clarity)")
    p.add_argument("--dry-run", action="store_true", help="fetch and filter only: store nothing, send nothing")
    p.add_argument("--board", help="only boards whose key or company name contains this text")
    p.add_argument("--score", type=int, default=0, metavar="N", help="with --dry-run: AI-score the first N matches")
    d = sub.add_parser("discover", help="find a company's ATS board by name or careers URL")
    d.add_argument("target", nargs="*", help='company names ("Cloudflare") or careers page URLs')
    d.add_argument("--file", help='text file with one "Company" or "Company | careers URL" per line')
    d.add_argument("--add", action="store_true", help="append the results to companies.yaml")
    d.add_argument("--tier", help="polling tier for --add (default: slow for Workday, else normal)")
    d.add_argument("--name", help="display name to use for the company")
    sub.add_parser("test-notify", help="send a sample alert to your ntfy topic")
    g = sub.add_parser("digest", help="send the daily digest now")
    g.add_argument("--dry-run", action="store_true", help="print it instead of sending")
    t = sub.add_parser("top", help="best recent matches (what the Home Assistant sensor shows)")
    t.add_argument("-n", type=int, default=0, help="how many (default: homeassistant.top_n)")
    t.add_argument("--days", type=int, default=0, help="posted in the last N days (default: homeassistant.days)")
    t.add_argument("--push", action="store_true", help="also update the Home Assistant sensor now")
    sub.add_parser("stats", help="board health, request rates, detection delay, AI spend")
    b = sub.add_parser("backfill", help="pay-index the matches already processed (no AI calls), then AI-score the "
                                        "matches that were already open when each board was first polled "
                                        "(once per job, posted within criteria.max_age_days)")
    b.add_argument("--dry-run", action="store_true", help="score and print only: store nothing, send nothing")
    b.add_argument("--push", action="store_true", help="push strong fits to your phone (default: next digest)")
    b.add_argument("--days", type=int, default=0, help="look back N days (default: criteria.max_age_days)")
    b.add_argument("--rescore", action="store_true",
                   help="also AI-score again the matches already scored (after editing stories/ or profile.md; "
                        "~$0.001 each, never pushed)")
    m = sub.add_parser("map", help="write the cost-of-living map (every metro's pay index, break-even salaries, "
                                   "top jobs) to an HTML file")
    m.add_argument("--out", default="data/map.html", help="where to write it (default: data/map.html)")
    y = sub.add_parser("pay", help="explain the cost-of-living pay index for a salary and location, or "
                                   "--update its data from BEA and the World Bank")
    y.add_argument("salary", nargs="?", help='e.g. "$150K-180K" or "EUR 90,000"')
    y.add_argument("location", nargs="?", default="", help='e.g. "Austin, TX" or "Munich, Germany"')
    y.add_argument("--remote", action="store_true", help="a remote job (you'd live at pay.reference_location)")
    y.add_argument("--update", action="store_true", help="download fresh price data now")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("paho").setLevel(logging.CRITICAL)  # its connect errors are re-raised and reported by us
    install_log_redaction()
    cfg = load_config(args.config)
    if args.command in ("run", "web", "poll", "discover", "backfill", "digest"):
        seed_user_files(cfg)  # a missing profile, watch list or story folder starts from the shipped example
    try:
        return asyncio.run(COMMANDS[args.command](cfg, args)) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
