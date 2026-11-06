"""Load config.yaml + companies.yaml. Secrets come from the environment / .env, referenced as ${VAR}."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, model_validator

ATS = Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workday", "ultipro",
              "personio", "workable", "bamboohr", "rippling", "breezy", "successfactors"]
AGGREGATORS = ("usajobs", "adzuna", "jsearch", "theirstack")

_ENV_REF = re.compile(r"\$\{(\w+)\}")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_ULTIPRO = re.compile(r"https?://(recruiting\d*\.ultipro\.com)/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})")


class Criteria(BaseModel):
    title_field: list[str] = []          # field words: pass the title and override title_exclude_soft
    title_include: list[str] = []        # role words: pass the title (field + include empty = everything)
    title_exclude: list[str] = []        # any phrase rejects the job
    title_exclude_soft: list[str] = []   # rejects the job unless the title also has a field word
    seniority_exclude: list[str] = []    # e.g. intern, junior, principal - matched against the title
    description_exclude: list[str] = []  # e.g. "security clearance" - matched against the description
    locations: list[str] = []            # any phrase must appear in the location (empty = anywhere)
    allow_remote: bool = True            # remote jobs pass the location check
    max_age_days: int = 7                # newly seen but older than this -> stored, never alerted


class Polling(BaseModel):
    tiers: dict[str, int] = {"priority": 180, "normal": 600, "slow": 1800}
    jitter: float = 0.2
    host_min_interval: dict[str, float] = {
        "default": 1.0,
        "boards-api.greenhouse.io": 1.0,
        "myworkdayjobs.com": 5.0,
    }
    failure_alert_threshold: int = 5
    max_backoff: int = 3600
    max_alerts_per_poll: int = 5


class Scoring(BaseModel):
    enabled: bool = False
    model: str = "claude-haiku-5-5"
    effort: Literal["low", "medium", "high"] = "low"
    profile_path: str = "profile.md"     # relative to the config directory
    resume_dir: str = "../resume_reference"  # PDF/.md/.txt resumes, relative to the config directory
    push_threshold: int = 75
    digest_threshold: int = 50
    daily_max_calls: int = 300


class PayConfig(BaseModel):
    """Cost-of-living pay index (see col.py): reference_salary at reference_location scores 1.0."""
    enabled: bool = True
    reference_salary: float = 155_000
    reference_location: str = "Portland, OR"
    min_index: float = 1.05              # top of the stated range below this -> no AI call, no alert (0 = off)
    col_data: str = "col_data.json"      # committed seed, relative to the config directory
    refresh_days: int = 30               # rebuild from BEA + World Bank when older (0 = never); kept next to the DB
    locations: dict[str, str] = {}       # location text -> US metro name, state code, ISO3 country or "home"
    taxes: bool = True                   # compare take-home pay (after income and payroll taxes) instead of gross
    tax_rules: str = "taxes.yaml"        # household, localities, local taxes, rates abroad (config dir)
    tax_data: str = "tax_data.json"      # US taxes by salary and place, built by tools/build_tax_data.py


class Notify(BaseModel):
    server: str = "https://ntfy.sh"
    topic: str = ""
    token: str = ""
    timezone: str = "America/New_York"
    digest_time: str = "18:00"
    digest_max_items: int = 20


class HomeAssistant(BaseModel):
    """Dashboard sensor, published to Home Assistant through its MQTT broker (no HA token needed)."""
    enabled: bool = False
    mqtt_host: str = ""                  # e.g. 192.168.1.10 (the Mosquitto add-on on Home Assistant)
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False               # true for a TLS listener (usually port 8883)
    discovery_prefix: str = "homeassistant"
    base_topic: str = "jobalerts"
    top_n: int = 3
    days: int = 7                        # matches posted in the last N days
    interval: int = 300                  # seconds between refreshes (also refreshed right after new matches)


class AggregatorConfig(BaseModel):
    enabled: bool = False
    interval: int = 3600
    country: str = "us"                  # adzuna only; a search can override it with its own country
    searches: list[dict] = []


class Aggregators(BaseModel):
    usajobs: AggregatorConfig = AggregatorConfig(interval=1800)
    adzuna: AggregatorConfig = AggregatorConfig(interval=3600)
    jsearch: AggregatorConfig = AggregatorConfig(interval=14400)
    theirstack: AggregatorConfig = AggregatorConfig(interval=86400)


class Company(BaseModel):
    name: str
    ats: ATS
    slug: str = ""        # board token / company id (not needed for workday)
    url: str = ""         # workday / ultipro / successfactors: the careers site URL
    search: str = ""      # workday / smartrecruiters / ultipro / successfactors: server-side search text
    region: str = ""      # lever: "eu" for jobs.eu.lever.co companies
    pages: int = 2        # workday / ultipro: pages (of 20 / 50) to read per poll
    tier: str = "normal"
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.ats == "workday":
            if ".myworkdayjobs.com" not in self.url:
                raise ValueError(f"{self.name}: workday companies need url: https://<tenant>.wdN.myworkdayjobs.com/<site>")
        elif self.ats == "ultipro":
            parse_ultipro_url(self.url)
        elif self.ats == "successfactors":
            if not urlparse(self.url).netloc:
                raise ValueError(f"{self.name}: successfactors companies need url: https://careers.<company>.com")
        elif not self.slug:
            raise ValueError(f"{self.name}: {self.ats} companies need a slug")
        return self


def parse_workday_url(url: str) -> tuple[str, str, str]:
    """https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/... -> (host, tenant, site)."""
    u = urlparse(url)
    host = u.netloc
    parts = [p for p in u.path.split("/") if p]
    if parts and _LOCALE.match(parts[0]):
        parts = parts[1:]
    if not host or not parts:
        raise ValueError(f"not a Workday careers URL: {url}")
    return host, host.split(".")[0], parts[0]


def parse_ultipro_url(url: str) -> tuple[str, str, str]:
    """https://recruiting2.ultipro.com/TEN1234/JobBoard/<guid>/... -> (host, tenant, board id)."""
    m = _ULTIPRO.match(url or "")
    if not m:
        raise ValueError(f"not a UltiPro job board URL (https://recruitingN.ultipro.com/<tenant>/JobBoard/<id>): {url}")
    return m.group(1), m.group(2), m.group(3)


@dataclass
class Board:
    """One thing the scheduler polls: a company's job board or an aggregator search."""
    source: str
    slug: str
    name: str
    interval: int
    params: dict = field(default_factory=dict)
    watched: bool = True   # False for aggregator searches (their companies feed suggestions)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.slug}"


class Config(BaseModel):
    contact_email: str = ""
    database: str = "data/jobalerts.db"
    companies_file: str = "companies.yaml"
    criteria: Criteria = Criteria()
    polling: Polling = Polling()
    scoring: Scoring = Scoring()
    pay: PayConfig = PayConfig()
    notify: Notify = Notify()
    homeassistant: HomeAssistant = HomeAssistant()
    aggregators: Aggregators = Aggregators()
    companies: list[Company] = []
    config_dir: Path = Path(".")

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.config_dir / p

    def boards(self) -> list[Board]:
        out: list[Board] = []
        for c in self.companies:
            if not c.enabled:
                continue
            if c.tier not in self.polling.tiers:
                raise ValueError(f"{c.name}: unknown tier {c.tier!r} (have {sorted(self.polling.tiers)})")
            params = {"search": c.search, "region": c.region, "pages": c.pages}
            slug = c.slug
            if c.ats == "workday":
                host, tenant, site = parse_workday_url(c.url)
                params.update(host=host, tenant=tenant, site=site)
                slug = f"{tenant}/{site}"
            elif c.ats == "ultipro":
                host, tenant, board_id = parse_ultipro_url(c.url)
                params.update(host=host, tenant=tenant, board_id=board_id)
                slug = f"{tenant}/{board_id[:8]}"
            elif c.ats == "successfactors":
                params["url"] = c.url
                slug = urlparse(c.url).netloc
            out.append(Board(c.ats, slug, c.name, self.polling.tiers[c.tier], params))
        for source in AGGREGATORS:
            agg: AggregatorConfig = getattr(self.aggregators, source)
            if not agg.enabled:
                continue
            for search in agg.searches:
                label = search.get("name") or next(
                    (str(search[k]) for k in ("keyword", "query", "what") if search.get(k)), "search")
                params = {"country": agg.country, **search}
                if params["country"] != agg.country and not search.get("name"):
                    label = f"{label} {params['country']}"  # same words in another country is another board
                out.append(Board(source, _slugify(label), f"{source} '{label}'", agg.interval, params, watched=False))
        return out

    def watched_companies(self) -> list[str]:
        return [c.name for c in self.companies]


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "search"


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=value lines; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _read_yaml(path: Path) -> dict:
    text = _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), path.read_text(encoding="utf-8"))
    return yaml.safe_load(text) or {}


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(path.parent.parent / ".env")
    cfg = Config.model_validate(_read_yaml(path))
    cfg.config_dir = path.parent
    companies_path = cfg.resolve(cfg.companies_file)
    if companies_path.is_file():
        cfg.companies = [Company.model_validate(c) for c in _read_yaml(companies_path).get("companies") or []]
    if os.environ.get("JOBALERTS_DB"):
        cfg.database = os.environ["JOBALERTS_DB"]
    if not cfg.contact_email:
        cfg.contact_email = os.environ.get("CONTACT_EMAIL", "")
    return cfg
