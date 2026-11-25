"""AI fit scoring with Claude: one cheap call per keyword-matched job, against your work stories
(stories/*.md) and profile.md, or your resumes when there are no stories.

Claude Haiku 5.5 costs $0.10/M input and $0.50/M output tokens, so a typical posting
(~3K tokens in, a few hundred out) is about $0.0005; 100 matches a day is ~5 cents.
"""
from __future__ import annotations

import hashlib
import json
import logging

import anthropic
from pydantic import BaseModel, ValidationError

from .vault import secret
from .col import Pay
from .config import Scoring
from .models import Job, utcnow
from .resumes import Resume
from .stories import Story
from .store import Store

log = logging.getLogger(__name__)

# List prices, USD per million tokens (input, output). Cache reads cost 0.1x input and cache writes
# (5-minute TTL) 1.25x. An unknown model is priced like the dearest one, so the cost estimate errs high.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}


def ai_cost_usd(rows, model: str) -> float:
    """Estimated spend for ai_usage rows at the model's list prices."""
    inp, out = PRICES.get(model, max(PRICES.values()))
    return sum(r["input_tokens"] * inp + r["output_tokens"] * out
               + r["cache_read_tokens"] * inp * 0.1 + r["cache_write_tokens"] * inp * 1.25
               for r in rows) / 1e6

MAX_DESCRIPTION_CHARS = 30_000

SYSTEM = """You screen newly posted jobs for one candidate and decide which ones are worth \
interrupting them with a phone notification.

Score each posting 0-100 for how strongly this candidate should apply right now:
- 90-100: exceptional fit, apply today
- 75-89: strong fit
- 50-74: plausible, worth a look later
- below 50: skip

Compare the posting's must-have qualifications against the candidate's actual experience in \
{evidence}: domain, seniority, people leadership, tools, and track record. Weigh the \
candidate's stated priorities, the real impact and scope of the role, and their deal-breakers \
above simple keyword overlap. Postings are often vague; judge from what is actually stated \
and don't invent requirements.

Return:
- score: integer 0-100
- summary: one sentence (at most 25 words) naming the strongest overlap with their experience or \
the main gap, written for a phone notification
- red_flags: concrete concerns such as a must-have their experience doesn't show, clearance required, \
wrong location, or contract-only. Use an empty list if there are none.
- best_resume: the file name of the resume version to apply with, exactly as given in the resumes \
below (empty string if none are provided)
- top_stories: the ids of up to 3 work stories that best show the candidate can do this job, best \
first, exactly as given in the work stories below (empty list if none are provided or none fit)
- pay_min, pay_max: the posted base salary range as plain numbers (the same number twice if only one \
figure is given; 0 and 0 if the posting states no pay). Base pay only: no bonus, equity or benefits. \
If the range differs by location, use the one for the job's location, or for the candidate's home \
area if the job is remote.
- pay_currency: ISO code of that pay, such as USD or EUR (empty string if no pay is stated)
- pay_period: what the pay figures are per: year, month, week, day or hour
- step_up: integer 0-100, a separate judgement from score: how big a career step up this role is \
for the candidate AND how credibly they could win it. Compare it with their current or most recent \
role in title level, scope, team size, budget and decision authority:
  - below 30: lateral move or a step down
  - 30-59: a modest step (somewhat larger scope or team at about the same level)
  - 60-74: a real step up, but a long shot or only partly a promotion
  - 75-89: a clear promotion (a level up, such as group leader to manager of managers or director) \
that their record makes a credible stretch
  - 90-100: a major step up they could still realistically win
  A role so far above them that they would not be considered, or in a field where their record \
doesn't carry over, scores low. A strong fit at the same level is a high score but a low step_up.
- step_up_note: at most 15 words naming what makes it a step up (empty string if step_up is under 60)

Don't let pay move the score. The program compares base pay with the candidate's salary floor \
itself, adjusted for the local cost of living, so a lower number in a cheaper place isn't a red flag.

{experience}

<candidate_profile>
{profile}
</candidate_profile>"""

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "summary": {"type": "string"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "best_resume": {"type": "string"},
        "top_stories": {"type": "array", "items": {"type": "string"}},
        "pay_min": {"type": "number"},
        "pay_max": {"type": "number"},
        "pay_currency": {"type": "string"},
        "pay_period": {"type": "string", "enum": ["year", "month", "week", "day", "hour"]},
        "step_up": {"type": "integer"},
        "step_up_note": {"type": "string"},
    },
    "required": ["score", "summary", "red_flags", "best_resume", "top_stories", "pay_min", "pay_max", "pay_currency", "pay_period",
                 "step_up", "step_up_note"],
    "additionalProperties": False,
}


class FitScore(BaseModel):
    score: int
    summary: str
    red_flags: list[str] = []
    best_resume: str = ""
    top_stories: list[str] = []          # story ids, best first (at most 3)
    top_story_titles: list[str] = []     # filled in by the Scorer, not the model
    basis: str = ""                      # filled in by the Scorer: which prompt (stories, profile, model) it used
    pay_min: float = 0
    pay_max: float = 0
    pay_currency: str = ""
    pay_period: str = "year"
    step_up: int = 0
    step_up_note: str = ""

    def pay(self) -> Pay | None:
        """The base pay the model read from the posting, if it found any."""
        high = max(self.pay_min, self.pay_max)
        if high <= 0:
            return None
        return Pay(min(self.pay_min, self.pay_max) or high, high, self.pay_currency.upper().strip(), self.pay_period or "year")


RESUME_SUMMARY_CHARS = 1_500  # with stories, each resume is only there to pick the version to apply with


def resumes_block(resumes: list[Resume], summary_only: bool = False) -> str:
    if not resumes:
        return "<resumes>(none provided; judge from the profile only)</resumes>"
    parts = [f'<resume file="{r.name}">\n{r.text[:RESUME_SUMMARY_CHARS] if summary_only else r.text}\n</resume>'
             for r in resumes]
    note = ("Resume versions, opening section only. Use them to pick best_resume; judge fit from the "
            "work stories.\n" if summary_only else "")
    return "<resumes>\n" + note + "\n".join(parts) + "\n</resumes>"


def stories_block(stories: list[Story]) -> str:
    parts = [f'<story id="{s.id}" title="{s.title}">\n{s.text}\n</story>' for s in stories]
    return ("<work_stories>\nOne story per project: what the candidate did, their role in it and the results.\n"
            + "\n".join(parts) + "\n</work_stories>")


def experience_prompt(resumes: list[Resume], stories: list[Story]) -> tuple[str, str]:
    """(how the prompt names the candidate's experience, the block that holds it)."""
    if stories:
        return ("their work stories", stories_block(stories) + "\n\n" + resumes_block(resumes, summary_only=True))
    return "their resumes", resumes_block(resumes)


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
    def __init__(self, cfg: Scoring, store: Store, profile: str, client: anthropic.AsyncAnthropic | None = None,
                 resumes: list[Resume] | None = None, stories: list[Story] | None = None):
        self.cfg = cfg
        self.store = store
        self.resume_names = [r.name for r in resumes or []]
        self.story_titles = {s.id: s.title for s in stories or []}
        evidence, experience = experience_prompt(resumes or [], stories or [])
        self.system = SYSTEM.format(profile=profile.strip() or "(no profile provided)",
                                    evidence=evidence, experience=experience)
        self.client = client or anthropic.AsyncAnthropic(api_key=secret("ANTHROPIC_API_KEY") or None, max_retries=3, timeout=90.0)
        # Changes when the stories, resumes, profile, prompt or model change: --rescore skips jobs already
        # scored on the current basis, so an interrupted rescore picks up where it stopped.
        self.basis = hashlib.sha256(f"{cfg.model}\n{self.system}".encode()).hexdigest()[:12]
        self._cap_logged = ""

    def calls_left(self) -> int:
        """Claude calls left today (UTC) under scoring.daily_max_calls."""
        return max(0, self.cfg.daily_max_calls - self.store.ai_calls_today())

    def capped(self) -> bool:
        return self.calls_left() <= 0

    async def score(self, job: Job) -> FitScore | None:
        """Returns None when scoring isn't possible (budget spent, API error, refusal); callers then
        fall back to keyword-only routing so a match is never silently dropped."""
        if self.capped():
            today = utcnow().date().isoformat()
            if self._cap_logged != today:  # once a day, not once per job
                self._cap_logged = today
                log.warning("AI daily cap (%d) reached; no AI scores until 00:00 UTC", self.cfg.daily_max_calls)
            log.debug("AI daily cap: not scoring %s", job.key)
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
        self.store.record_ai_call(usage.input_tokens or 0, usage.output_tokens or 0,
                                  usage.cache_read_input_tokens or 0, usage.cache_creation_input_tokens or 0)
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
        result.step_up = max(0, min(100, result.step_up))
        if result.best_resume not in self.resume_names:
            result.best_resume = ""
        ids = [i.strip() for i in result.top_stories]
        result.top_stories = [i for n, i in enumerate(ids) if i in self.story_titles and i not in ids[:n]][:3]
        result.top_story_titles = [self.story_titles[i] for i in result.top_stories]
        result.basis = self.basis
        return result
