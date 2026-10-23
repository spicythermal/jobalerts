"""AI fit scoring with Claude: one cheap call per keyword-matched job, against your profile.md.

Claude Haiku 5.5 costs $0.10/M input and $0.50/M output tokens, so a typical posting
(~3K tokens in, a few hundred out) is about $0.0005; 100 matches a day is ~5 cents.
"""
from __future__ import annotations

import json
import logging

import anthropic
from pydantic import BaseModel, ValidationError

from .config import Scoring
from .models import Job
from .store import Store

log = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 30_000

SYSTEM = """You screen newly posted jobs for one candidate and decide which ones are worth \
interrupting them with a phone notification.

Score each posting 0-100 for how strongly this candidate should apply right now:
- 90-100: exceptional fit, apply today
- 75-89: strong fit
- 50-74: plausible, worth a look later
- below 50: skip

Weigh the candidate's stated priorities, the real impact and scope of the role, and their \
deal-breakers above simple keyword overlap. Postings are often vague; judge from what is \
actually stated and don't invent requirements.

Return:
- score: integer 0-100
- summary: one sentence (at most 25 words) saying why it does or doesn't fit, written for a phone notification
- red_flags: concrete concerns such as clearance required, wrong location, pay below the candidate's floor, \
or contract-only. Use an empty list if there are none.

<candidate_profile>
{profile}
</candidate_profile>"""

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "summary": {"type": "string"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "summary", "red_flags"],
    "additionalProperties": False,
}


class FitScore(BaseModel):
    score: int
    summary: str
    red_flags: list[str] = []


def job_prompt(job: Job) -> str:
    description = job.description or "(no description available)"
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS] + "\n[description truncated]"
    fields = [
        ("Title", job.title), ("Company", job.company), ("Location", job.location),
        ("Remote", "yes" if job.remote else "not stated"), ("Salary", job.salary or "not stated"),
        ("Department", job.department), ("Posted", job.published_at.date().isoformat() if job.published_at else ""),
    ]
    header = "\n".join(f"{k}: {v}" for k, v in fields if v)
    return f"<job_posting>\n{header}\n\nDescription:\n{description}\n</job_posting>"


class Scorer:
    def __init__(self, cfg: Scoring, store: Store, profile: str, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.store = store
        self.system = SYSTEM.format(profile=profile.strip())
        self.client = client or anthropic.AsyncAnthropic(max_retries=3, timeout=90.0)

    async def score(self, job: Job) -> FitScore | None:
        """Returns None when scoring isn't possible (budget spent, API error, refusal); callers then
        fall back to keyword-only routing so a match is never silently dropped."""
        if self.store.ai_calls_today() >= self.cfg.daily_max_calls:
            log.warning("AI daily cap (%d) reached; not scoring %s", self.cfg.daily_max_calls, job.key)
            return None
        try:
            response = await self.client.messages.create(
                model=self.cfg.model,
                max_tokens=4000,  # Haiku 5.5 thinks by default and thinking counts toward max_tokens
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": job_prompt(job)}],
                output_config={"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            )
        except anthropic.RateLimitError as e:
            log.warning("Claude rate limited while scoring %s: %s", job.key, e)
            return None
        except anthropic.APIStatusError as e:
            log.error("Claude API error %s while scoring %s: %s", e.status_code, job.key, e)
            return None
        except anthropic.APIConnectionError as e:
            log.error("Claude unreachable while scoring %s: %s", job.key, e)
            return None

        usage = response.usage
        self.store.record_ai_call(
            (usage.input_tokens or 0) + (usage.cache_read_input_tokens or 0)
            + (usage.cache_creation_input_tokens or 0),
            usage.output_tokens or 0,
        )
        if response.stop_reason == "refusal":
            log.warning("Claude declined to score %s", job.key)
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            result = FitScore.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as e:
            log.error("Unparseable score for %s (stop_reason=%s): %s", job.key, response.stop_reason, e)
            return None
        result.score = max(0, min(100, result.score))
        return result
