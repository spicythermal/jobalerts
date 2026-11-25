"""Read the candidate's work stories (stories/*.md, see stories/README.md) for the AI scorer.

Each story is one project: what the candidate did, their role and the results. Scoring against
the whole library sees far more than any one resume, and the scorer names the stories that best
fit each job. Unfinished parts of a story are marked "TODO:" and left out of the prompt, as are the
talk and slide notes, which are for presentations, not for judging fit.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

MAX_CHARS = 6_000  # per story; a filled-in story is ~2-3K characters
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
TODO = re.compile(r"\bTODO\b.*?(?=\n\s*\n|\Z)", re.S)      # from TODO to the end of its paragraph
NOT_FOR_SCORING = ("**Talk track", "**Slide idea", "**Questions it answers")
FACTS = ("employer", "dates", "role", "skills", "domains", "metrics")


@dataclass
class Story:
    id: str
    title: str
    text: str   # the facts and narrative as the scorer sees them


def _fact(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return "" if value is None else str(value)


def story_text(meta: dict, body: str) -> str:
    """The story's facts and narrative, without TODOs or presentation notes."""
    facts = {key: _fact(meta.get(key)) for key in FACTS}
    lines = [f"{key}: {value}" for key, value in facts.items() if value and not value.startswith("TODO")]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", TODO.sub("", body))]
    kept = [p for p in paragraphs if p and not p.startswith(NOT_FOR_SCORING)
            and not re.fullmatch(r"\*\*[^*]+\*\*:?\s*", p)]  # a label left empty by a removed TODO
    return "\n".join(lines) + ("\n\n" + "\n\n".join(kept) if kept else "")


def load_stories(folder: Path) -> list[Story]:
    """Every story in the folder (not README.md, not subfolders such as talks/), sorted by id.
    A file without front matter or a title is logged and skipped."""
    if not folder.is_dir():
        return []
    out: list[Story] = []
    for path in sorted(folder.glob("*.md")):
        if path.name.lower() == "readme.md" or path.name.startswith((".", "~")):
            continue
        try:
            m = FRONTMATTER.match(path.read_text(encoding="utf-8"))
            meta = yaml.safe_load(m.group(1)) if m else None
        except (OSError, yaml.YAMLError) as e:
            log.warning("Couldn't read story %s: %s", path.name, e)
            continue
        if not isinstance(meta, dict) or not meta.get("title"):
            log.warning("Story %s has no front matter with a title; skipping", path.name)
            continue
        out.append(Story(str(meta.get("id") or path.stem), str(meta["title"]),
                         story_text(meta, m.group(2))[:MAX_CHARS]))
    return out


# ---------------------------------------------------------------------------- editing (the Stories page)
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
LIST_FIELDS = ("skills", "domains", "metrics", "sources")
ROLES = ("lead", "contributor", "advisor")
PUBLIC = ("yes", "partial", "no")
TEMPLATE = """**Situation:** 

**Task:** 

**Action:** 

**Result:** 

**Resume bullets**
- 

**Talk track**

**Slide idea**

**Questions it answers**
- 
"""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "story"


def story_path(folder: Path, story_id: str) -> Path | None:
    """The file for a story id, or None for an id that isn't a plain slug (no paths)."""
    return folder / f"{story_id}.md" if SLUG.match(story_id or "") else None


def read_story(path: Path) -> tuple[dict, str, str]:
    """(front matter, body, the whole file) of one story file."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER.match(text)
    meta = (yaml.safe_load(m.group(1)) if m else None) or {}
    return (meta if isinstance(meta, dict) else {}), (m.group(2) if m else text), text


def dump_story(meta: dict, body: str, original: str | None = None) -> str:
    """A story file. When the front matter's values didn't change, the original front matter text is
    kept as it was (with its comments); otherwise it's written out fresh."""
    front = None
    if original and (m := FRONTMATTER.match(original)):
        try:
            if yaml.safe_load(m.group(1)) == meta:
                front = m.group(1)
        except yaml.YAMLError:
            pass
    if front is None:
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000, default_flow_style=None).rstrip()
    return f"---\n{front}\n---\n{body.strip()}\n"


def list_stories(folder: Path) -> list[dict]:
    """Every story file for the Stories page, with its front matter (also ones the scorer skips)."""
    out = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.md")):
        if path.name.lower() == "readme.md" or path.name.startswith((".", "~")):
            continue
        try:
            meta, body, _ = read_story(path)
        except (OSError, yaml.YAMLError):
            meta, body = {}, ""
        out.append({"id": path.stem, "editable": bool(SLUG.match(path.stem)), "meta": meta,
                    "title": str(meta.get("title") or ""), "todos": len(TODO.findall(body)), "mtime": path.stat().st_mtime})
    return out
