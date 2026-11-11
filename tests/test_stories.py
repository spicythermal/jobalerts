import json
from types import SimpleNamespace

from jobalerts.config import Scoring
from jobalerts.resumes import Resume
from jobalerts.scorer import Scorer
from jobalerts.stories import load_stories
from jobalerts.store import Store, stories_from_row

from conftest import REPO, run
from test_scorer import FakeMessages, JOB, response

STORY = """---
id: gun
title: Designed an electron gun
employer: Example Lab
dates: TODO
role: lead          # TODO: confirm
skills: [verification, design]
metrics: ["+20% efficiency"]
---
**Situation:** An RF source needed a better gun.

**Action:** Designed coaxial guns. TODO: were they built? what
did the measurements show?

**Result:** TODO: efficiency of what?

**Talk track (30 s):** "I designed a gun."

**Slide idea:** gun cross-section
"""


def test_load_story_drops_todos_and_presentation_notes(tmp_path):
    (tmp_path / "gun.md").write_text(STORY)
    (tmp_path / "README.md").write_text("# not a story")
    (tmp_path / "talks").mkdir()
    (tmp_path / "talks" / "plan.md").write_text("---\ntitle: a plan\n---\n")
    (tmp_path / "broken.md").write_text("no front matter")
    (story,) = load_stories(tmp_path)
    assert (story.id, story.title) == ("gun", "Designed an electron gun")
    assert "employer: Example Lab" in story.text and "role: lead" in story.text and "+20% efficiency" in story.text
    assert "Designed coaxial guns." in story.text and "An RF source" in story.text
    for gone in ("TODO", "dates:", "built?", "**Result:**", "Talk track", "Slide idea", "confirm"):
        assert gone not in story.text, gone


def test_repo_stories_all_load():
    stories = load_stories(REPO / "stories")
    assert len(stories) >= 15 and all(s.title and "TODO" not in s.text for s in stories)
    assert len({s.id for s in stories}) == len(stories)
    assert "lanl-scorpius-beam-spill" in {s.id for s in stories}


def story_scorer(reply, tmp_path):
    (tmp_path / "gun.md").write_text(STORY)
    (tmp_path / "beam.md").write_text("---\nid: beam\ntitle: Halved beam loss\n---\n**Result:** 3x current.\n")
    messages = FakeMessages(reply)
    resumes = [Resume("cv.pdf", "Summary: physicist. " + "x" * 5000 + " LAST-PAGE-SECRET")]
    s = Scorer(Scoring(), Store(":memory:"), "profile", client=SimpleNamespace(messages=messages),
               resumes=resumes, stories=load_stories(tmp_path))
    return s, messages


def test_scorer_judges_from_stories_and_names_the_top_ones(tmp_path):
    s, messages = story_scorer(response({"score": 80, "summary": "ok", "red_flags": [], "best_resume": "cv.pdf",
                                         "top_stories": ["beam", "nope", "gun", "beam", "x"]}), tmp_path)
    fit = run(s.score(JOB))
    assert fit.top_stories == ["beam", "gun"]                       # unknown and repeated ids dropped
    assert fit.top_story_titles == ["Halved beam loss", "Designed an electron gun"]
    assert fit.best_resume == "cv.pdf"
    system = messages.calls[0]["system"][0]["text"]
    assert '<story id="gun" title="Designed an electron gun">' in system and "their work stories" in system
    assert "Summary: physicist." in system and "LAST-PAGE-SECRET" not in system  # resumes trimmed to the opening
    assert "top_stories" in messages.calls[0]["output_config"]["format"]["schema"]["required"]


def test_scorer_without_stories_uses_full_resumes():
    messages = FakeMessages(response({"score": 60, "summary": "ok", "red_flags": [], "top_stories": ["a"]}))
    s = Scorer(Scoring(), Store(":memory:"), "profile", client=SimpleNamespace(messages=messages),
               resumes=[Resume("cv.pdf", "Summary " + "x" * 5000 + " LAST-PAGE")])
    fit = run(s.score(JOB))
    assert fit.top_stories == [] and fit.top_story_titles == []
    system = messages.calls[0]["system"][0]["text"]
    assert "LAST-PAGE" in system and "<work_stories>" not in system and "their resumes" in system


def test_stories_from_row_tolerates_old_rows():
    assert stories_from_row({"top_stories": None}) == []
    assert stories_from_row({"top_stories": "not json"}) == []
    assert stories_from_row({"top_stories": json.dumps([["a", "A"]])}) == [("a", "A")]
