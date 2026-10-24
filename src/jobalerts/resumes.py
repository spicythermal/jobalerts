"""Read the candidate's resumes (PDF, Markdown or text) so the scorer can compare postings against them.

Drop every version you use (e.g. an R&D-leadership one and an HPC one) into the resume folder;
the scorer sees all of them and says which version fits each posting best.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SUFFIXES = (".pdf", ".md", ".txt")
MAX_CHARS = 20_000  # per resume; a 3-page resume is ~8K characters


@dataclass
class Resume:
    name: str   # file name, used as the resume's label in prompts and alerts
    text: str


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader  # imported here so the rest of the app works without it
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def tidy(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def load_resumes(folder: Path) -> list[Resume]:
    """Every readable resume in the folder, sorted by name. Unreadable files are logged and skipped."""
    if not folder.is_dir():
        return []
    out: list[Resume] = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in SUFFIXES or path.name.startswith((".", "~")):
            continue
        try:
            text = pdf_text(path) if path.suffix.lower() == ".pdf" else path.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Couldn't read resume %s: %s", path.name, e)
            continue
        text = tidy(text)
        if not text:
            log.warning("Resume %s has no extractable text (scanned image?); skipping", path.name)
            continue
        out.append(Resume(path.name, text[:MAX_CHARS]))
    return out
