"""AI second look at jobs the keyword filter turned down (but no hard exclude did).

Keyword lists miss good jobs with unexpected titles ("Manager, Generator Operations" at a fusion
company). Instead of scoring each of those in full, two cheap Claude calls sort them first:

  pass 1  titles, 50 at a time: name only the clear misfits; the rest go on
  pass 2  title + company + the start of the description, 8 at a time: "no" only when clearly
          not worth the candidate's attention; a "yes" goes through the normal scoring

Measured on a week of real postings (2026-11-06): of the rejected postings the full score rated 50+,
this kept two thirds, and it kept both that rated 75+; about 17% went on to full scoring.
"""
from __future__ import annotations

import json
import logging

import anthropic
from pydantic import BaseModel, ValidationError

from .config import Prescreen, Scoring
from .models import Job
from .resumes import Resume
from .scorer import resumes_block
from .store import Store

log = logging.getLogger(__name__)

SYSTEM = """You pre-screen job postings for one candidate. A cheap keyword filter has already rejected \
these postings by title; your job is to catch the ones it should not have rejected. Every posting you \
pass is then read in full and scored by a more careful reviewer, so passing a posting costs little and \
missing a good one costs a lot. Be inclusive when in doubt.

Say no to roles plainly outside the candidate's field or level: HR, finance, legal, sales, marketing, \
facilities, trades and technicians, entry-level, and software or IT work unrelated to physics, \
hardware or scientific computing. Generic engineering titles (for example "Principal Electrical \
Engineer") at companies in the candidate's industries deserve a closer look, and so do stretch roles \
a level or two above the candidate's current one (manager of managers, director, head of a function).

{resumes}

<candidate_profile>
{profile}
</candidate_profile>"""

TITLES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["no"],
    "properties": {"no": {"type": "array", "items": {"type": "integer"}}},
}
POSTINGS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "verdict", "reason"],
        "properties": {"id": {"type": "integer"}, "verdict": {"type": "string", "enum": ["yes", "no"]},
                       "reason": {"type": "string"}}}}},
}


class Verdict(BaseModel):
    id: int
    verdict: str
    reason: str = ""


def titles_prompt(jobs: list[Job]) -> str:
    lines = "\n".join(f"{i}. {j.title} | {j.company} | {j.location or 'location not stated'}"
                      for i, j in enumerate(jobs))
    return ("Job titles (number. title | company | location):\n" + lines +
            "\n\nReturn in `no` only the numbers that are clearly NOT worth a closer look for this candidate "
            "(clearly another profession or far too junior). Every number you leave out gets a closer look.")


def postings_prompt(jobs: list[Job], excerpt_chars: int) -> str:
    parts = []
    for i, j in enumerate(jobs):
        desc = (j.description or "(no description available)")[:excerpt_chars]
        parts.append(f'<posting id="{i}">\nTitle: {j.title}\nCompany: {j.company}\n'
                     f"Location: {j.location or 'not stated'}\n\n{desc}\n</posting>")
    return ("\n\n".join(parts) + "\n\nFor every posting, answer `no` only if it is clearly not worth this "
            "candidate's attention, otherwise `yes` (it will be read in full and scored), with a reason of at "
            "most 12 words.")


class Prescreener:
    def __init__(self, cfg: Prescreen, scoring: Scoring, store: Store, profile: str,
                 resumes: list[Resume] | None = None, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.scoring = scoring
        self.store = store
        self.system = SYSTEM.format(profile=profile.strip() or "(no profile provided)",
                                    resumes=resumes_block(resumes or []))
        self.client = client or anthropic.AsyncAnthropic(max_retries=3, timeout=120.0)

    async def clear_misfits(self, jobs: list[Job]) -> set[int] | None:
        """Pass 1: indexes of the jobs whose title alone rules them out. None if the call failed."""
        data = await self._ask(titles_prompt(jobs), TITLES_SCHEMA)
        if data is None:
            return None
        return {i for i in data.get("no", []) if isinstance(i, int) and 0 <= i < len(jobs)}

    async def judge(self, jobs: list[Job]) -> dict[int, Verdict] | None:
        """Pass 2: verdict per index. A job the answer leaves out counts as yes. None if the call failed."""
        data = await self._ask(postings_prompt(jobs, self.cfg.excerpt_chars), POSTINGS_SCHEMA)
        if data is None:
            return None
        out = {}
        for item in data.get("results", []):
            try:
                v = Verdict.model_validate(item)
            except ValidationError:
                continue
            if 0 <= v.id < len(jobs):
                out[v.id] = v
        for i in range(len(jobs)):
            out.setdefault(i, Verdict(id=i, verdict="yes", reason="not in the pre-screen's answer"))
        return out

    async def _ask(self, prompt: str, schema: dict) -> dict | None:
        if self.store.ai_calls_today() >= self.scoring.daily_max_calls:
            log.warning("AI daily cap (%d) reached; pre-screen waits", self.scoring.daily_max_calls)
            return None
        try:
            response = await self.client.messages.create(
                model=self.scoring.model,
                max_tokens=8000,  # Haiku thinks by default and thinking counts toward max_tokens
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.RateLimitError as e:
            log.warning("Claude rate limited during the pre-screen: %s", e)
            return None
        except anthropic.APIStatusError as e:
            log.error("Claude API error %s during the pre-screen: %s", e.status_code, e)
            return None
        except anthropic.APIConnectionError as e:
            log.error("Claude unreachable during the pre-screen: %s", e)
            return None
        usage = response.usage
        self.store.record_ai_call(usage.input_tokens or 0, usage.output_tokens or 0,
                                  usage.cache_read_input_tokens or 0, usage.cache_creation_input_tokens or 0)
        if response.stop_reason == "refusal":
            log.warning("Claude declined a pre-screen batch")
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.error("Unparseable pre-screen answer (stop_reason=%s): %s", response.stop_reason, e)
            return None
