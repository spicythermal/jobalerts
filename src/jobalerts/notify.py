"""Push notifications via ntfy (https://docs.ntfy.sh/publish/), published as JSON."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .config import Notify
from .models import Job

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: Notify, dry_run: bool = False, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.dry_run = dry_run or not cfg.topic
        self._client = httpx.AsyncClient(timeout=15.0, transport=transport)
        self.sent: list[dict] = []  # payloads, kept for dry runs and tests

    async def aclose(self) -> None:
        await self._client.aclose()

    def build(self, title: str, message: str, *, priority: int = 3, tags: list[str] | None = None,
              click: str | None = None, actions: list[dict] | None = None) -> dict:
        payload = {"topic": self.cfg.topic, "title": title[:250], "message": message[:4000],
                   "priority": priority, "tags": tags or []}
        if click:
            payload["click"] = click
        if actions:
            payload["actions"] = actions
        return payload

    async def send(self, title: str, message: str, **kwargs) -> None:
        payload = self.build(title, message, **kwargs)
        self.sent.append(payload)
        if self.dry_run:
            log.info("[dry-run notify] %s | %s", payload["title"], payload["message"].replace("\n", " / "))
            return
        headers = {"Authorization": f"Bearer {self.cfg.token}"} if self.cfg.token else {}
        for attempt in range(3):
            try:
                r = await self._client.post(self.cfg.server.rstrip("/"), json=payload, headers=headers)
                r.raise_for_status()
                return
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise
                log.warning("ntfy send failed (%s); retrying", e)
                await asyncio.sleep(3 * (attempt + 1))

    async def job_alert(self, job: Job, score=None) -> None:
        lines = [" · ".join(filter(None, [job.location or None, "Remote" if job.remote and
                                           "remote" not in job.location.lower() else None, job.salary or None]))]
        if score is not None:
            lines.append(f"Fit {score.score}/100: {score.summary}")
            if score.red_flags:
                lines.append("Watch out: " + "; ".join(score.red_flags))
            if score.best_resume:
                lines.append(f"Apply with: {score.best_resume}")
        age = job.age_days()
        if age is not None:
            hours = age * 24
            lines.append(f"Posted {hours:.0f}h ago" if hours < 48 else f"Posted {age:.0f}d ago")
        lines.append(f"via {job.source}")
        priority = 5 if score is not None and score.score >= 90 else 4
        await self.send(
            f"{job.company}: {job.title}",
            "\n".join(line for line in lines if line),
            priority=priority,
            tags=["briefcase"],
            click=job.url,
            actions=[{"action": "view", "label": "Open posting", "url": job.url, "clear": True}],
        )
