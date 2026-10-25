"""Top recent matches, for `jobalerts top` and a Home Assistant sensor published over MQTT."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from .config import HomeAssistant
from .filters import JobFilter
from .models import parse_dt, utcnow
from .store import Store, job_from_row

log = logging.getLogger(__name__)


def _age(published) -> str:
    if published is None:
        return ""
    hours = (utcnow() - published).total_seconds() / 3600
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


def top_matches(store: Store, job_filter: JobFilter, days: int, n: int) -> tuple[int, list[dict]]:
    """(number of matches posted in the last `days`, the best `n` of them).

    Ranked by AI score, then newest first. Jobs that were already open when a board was first
    polled (baseline) count too if they pass the title/location filter, so the list isn't empty
    right after a deploy."""
    matches = []
    for r in store.recent_candidates(utcnow() - timedelta(days=days)):
        published = parse_dt(r["published_at"])
        if r["status"] == "baseline" and not job_filter.check_basic(job_from_row(r))[0]:
            continue
        matches.append({
            "company": r["company"] or "",
            "title": r["title"] or "",
            "location": r["location"] or "",
            "salary": r["salary"] or "",
            "url": r["url"] or "",
            "score": r["score"],
            "summary": r["score_summary"] or "",
            "best_resume": r["best_resume"] or "",
            "posted": r["published_at"] or r["first_seen_at"],
            "age": _age(published or parse_dt(r["first_seen_at"])),
            "pushed": r["status"] == "alerted",
        })
    matches.sort(key=lambda m: m["posted"], reverse=True)
    matches.sort(key=lambda m: -1 if m["score"] is None else m["score"], reverse=True)
    return len(matches), matches[:n]


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
            "device": {"identifiers": [self.cfg.base_topic], "name": "Job alerts", "manufacturer": "jobalerts"},
        }

    def messages(self, total: int, jobs: list[dict]) -> list[tuple[str, str]]:
        top = f"{jobs[0]['company']}: {jobs[0]['title']}" if jobs else "none"
        attributes = {"days": self.cfg.days, "top": top[:250], "jobs": jobs,
                      "updated": utcnow().isoformat(timespec="seconds")}
        return [(self.discovery_topic, json.dumps(self.discovery())),
                (self.attributes_topic, json.dumps(attributes)),
                (self.state_topic, str(total))]

    async def publish(self, total: int, jobs: list[dict]) -> None:
        await asyncio.to_thread(self._send, self.cfg, self.messages(total, jobs))
        log.debug("MQTT %s updated: %d matches", self.state_topic, total)


def _paho_send(cfg: HomeAssistant, messages: list[tuple[str, str]]) -> None:
    from paho.mqtt import publish

    auth = {"username": cfg.mqtt_username, "password": cfg.mqtt_password} if cfg.mqtt_username else None
    publish.multiple([{"topic": t, "payload": payload, "qos": 1, "retain": True} for t, payload in messages],
                     hostname=cfg.mqtt_host, port=cfg.mqtt_port, client_id=f"{cfg.base_topic}-dashboard",
                     auth=auth, tls={} if cfg.mqtt_tls else None)
