"""Top recent matches and AI cost, for `jobalerts top` and Home Assistant sensors over MQTT."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from .vault import secret
from .col import NO_PAY, parse_salary
from .config import HomeAssistant, Scoring
from .filters import JobFilter
from .models import parse_dt, utcnow
from .scorer import ai_cost_usd
from .store import Store, job_from_row, stories_from_row

log = logging.getLogger(__name__)


def _age(published) -> str:
    if published is None:
        return ""
    hours = (utcnow() - published).total_seconds() / 3600
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


def top_matches(store: Store, job_filter: JobFilter, days: int, n: int, pay=None,
                by: str = "score") -> tuple[int, list[dict]]:
    """(number of matches posted in the last `days`, the best `n` of them).

    Ranked by AI score (or with by="step_up" by the AI's step-up score, for the best promotions;
    jobs without one are left out), then newest first. Jobs that were already open when a board was
    first polled (baseline) count too if they pass the title/location filter, so the list isn't empty
    right after a deploy. `pay` (a col.PayIndex) fills in the pay index for jobs stored without one."""
    matches = []
    for r in store.recent_candidates(utcnow() - timedelta(days=days)):
        published = parse_dt(r["published_at"])
        if r["status"] == "baseline" and not job_filter.check_basic(job_from_row(r))[0]:
            continue
        pay_index, low, high = r["pay_index"], r["pay_low"], r["pay_high"]
        buy_index, buy_low, buy_high = r["buy_index"], r["buy_low"], r["buy_high"]
        pay_note = "" if r["pay_note"] == NO_PAY else r["pay_note"] or ""
        if pay_index is None and pay and (result := pay.index(parse_salary(r["salary"] or ""), r["location"] or "",
                                                              bool(r["remote"]))):
            pay_index, low, high, pay_note = result.index, result.low, result.high, result.text()
            buy_index, buy_low, buy_high = result.buy_index, result.buy_low, result.buy_high
        matches.append({
            "company": r["company"] or "",
            "title": r["title"] or "",
            "location": r["location"] or "",
            "salary": r["salary"] or "",
            "pay_index": round(pay_index, 2) if pay_index is not None else None,
            "pay_low": round(low, 2) if low is not None else None,
            "pay_high": round(high, 2) if high is not None else None,
            "buy_index": round(buy_index, 2) if buy_index is not None else None,
            "buy_low": round(buy_low, 2) if buy_low is not None else None,
            "buy_high": round(buy_high, 2) if buy_high is not None else None,
            "pay": pay_note,
            "url": r["url"] or "",
            "score": r["score"],
            "step_up": r["step_up"],
            "step_up_note": r["step_up_note"] or "",
            "summary": r["score_summary"] or "",
            "best_resume": r["best_resume"] or "",
            "stories": [title for _, title in stories_from_row(r)],   # best-fitting work stories, best first
            "story_ids": [sid for sid, _ in stories_from_row(r)],
            "posted": r["published_at"] or r["first_seen_at"],
            "age": _age(published or parse_dt(r["first_seen_at"])),
            "pushed": r["status"] == "alerted",
        })
    matches.sort(key=lambda m: m["posted"], reverse=True)
    if by == "step_up":
        ranked = sorted((m for m in matches if m["step_up"] is not None), key=lambda m: m["step_up"], reverse=True)
        return len(matches), ranked[:n]
    matches.sort(key=lambda m: -1 if m["score"] is None else m["score"], reverse=True)
    return len(matches), matches[:n]


def ai_cost(store: Store, scoring: Scoring) -> dict | None:
    """Estimated AI scoring spend at list prices: this month (UTC), today and the last 7 days.
    Only this app's own calls; the Console's Billing page is the real bill."""
    if not scoring.enabled:
        return None
    today = utcnow().date()
    month = store.ai_usage_since(today.replace(day=1).isoformat())
    week = store.ai_usage(days=6)  # today and the 6 days before it
    return {
        "month": round(ai_cost_usd(month, scoring.model), 4),
        "today": round(ai_cost_usd([r for r in month if r["day"] == today.isoformat()], scoring.model), 4),
        "last_7_days": round(ai_cost_usd(week, scoring.model), 4),
        "calls_month": sum(r["calls"] for r in month),
        "calls_7_days": sum(r["calls"] for r in week),
        "model": scoring.model,
    }


class MqttPublisher:
    """Publishes the sensor through Home Assistant's MQTT discovery. All messages are retained, so the
    sensor comes back by itself when Home Assistant or the broker restarts. Only needs an MQTT login
    allowed to write the two topic trees below, not a Home Assistant token."""

    def __init__(self, cfg: HomeAssistant, send=None):
        self.cfg = cfg
        self._send = send or _paho_send  # tests swap in a fake

    @property
    def configured(self) -> bool:
        return bool(self.cfg.mqtt_host)

    @property
    def state_topic(self) -> str:
        return f"{self.cfg.base_topic}/top/state"

    @property
    def attributes_topic(self) -> str:
        return f"{self.cfg.base_topic}/top/attributes"

    @property
    def discovery_topic(self) -> str:
        return f"{self.cfg.discovery_prefix}/sensor/{self.cfg.base_topic}/top/config"

    def _device(self) -> dict:
        return {"identifiers": [self.cfg.base_topic], "name": "Job alerts", "manufacturer": "jobalerts"}

    def discovery(self) -> dict:
        return {
            "name": None,  # the entity takes the device's name -> sensor.job_alerts
            "unique_id": f"{self.cfg.base_topic}_top",
            "state_topic": self.state_topic,
            "json_attributes_topic": self.attributes_topic,
            "unit_of_measurement": "jobs",
            "state_class": "measurement",
            "icon": "mdi:briefcase-search",
            "expire_after": 3 * self.cfg.interval,  # goes "unavailable" if the service stops updating it
            "device": self._device(),
        }

    def cost_discovery(self) -> dict:
        base = self.cfg.base_topic
        return {
            "name": "AI cost this month",  # -> sensor.job_alerts_ai_cost_this_month
            "unique_id": f"{base}_ai_cost",
            "state_topic": f"{base}/cost/state",
            "json_attributes_topic": f"{base}/cost/attributes",
            "unit_of_measurement": "USD",
            "device_class": "monetary",
            "suggested_display_precision": 2,
            "icon": "mdi:cash",
            "expire_after": 3 * self.cfg.interval,
            "device": self._device(),
        }

    def messages(self, total: int, jobs: list[dict], cost: dict | None = None,
                 step_ups: list[dict] | None = None) -> list[tuple[str, str]]:
        top = f"{jobs[0]['company']}: {jobs[0]['title']}" if jobs else "none"
        updated = utcnow().isoformat(timespec="seconds")
        # Story titles stay out of Home Assistant (ids are enough for a card) to keep the attributes small.
        lean = lambda rows: [{k: v for k, v in r.items() if k != "stories"} for r in rows or []]
        attributes = {"days": self.cfg.days, "top": top[:250], "jobs": lean(jobs), "step_ups": lean(step_ups),
                      "updated": updated}
        out = [(self.discovery_topic, json.dumps(self.discovery())),
               (self.attributes_topic, json.dumps(attributes)),
               (self.state_topic, str(total))]
        if cost:
            base = self.cfg.base_topic
            out += [(f"{self.cfg.discovery_prefix}/sensor/{base}/cost/config", json.dumps(self.cost_discovery())),
                    (f"{base}/cost/attributes", json.dumps({**cost, "updated": updated})),
                    (f"{base}/cost/state", f"{cost['month']:.4f}")]
        return out

    async def publish(self, total: int, jobs: list[dict], cost: dict | None = None,
                      step_ups: list[dict] | None = None) -> None:
        await asyncio.to_thread(self._send, self.cfg, self.messages(total, jobs, cost, step_ups))
        log.debug("MQTT %s updated: %d matches", self.state_topic, total)


def _paho_send(cfg: HomeAssistant, messages: list[tuple[str, str]]) -> None:
    from paho.mqtt import publish

    password = cfg.mqtt_password or secret("MQTT_PASSWORD")
    auth = {"username": cfg.mqtt_username, "password": password} if cfg.mqtt_username else None
    publish.multiple([{"topic": t, "payload": payload, "qos": 1, "retain": True} for t, payload in messages],
                     hostname=cfg.mqtt_host, port=cfg.mqtt_port, client_id=f"{cfg.base_topic}-dashboard",
                     auth=auth, tls={} if cfg.mqtt_tls else None)
