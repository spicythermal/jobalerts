"""Edit the watch list (companies.yaml) from the Boards page without losing your comments.

Entries written by `jobalerts discover --add` and this module are one JSON object per line
(`  - {"name": ..., "ats": ...}`), which is valid YAML. Changes touch only that entry's line, so
comments and section headings stay where they are. If an entry was hand-written over several
lines, the file is rewritten: the comments above `companies:` stay, the ones between entries go.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from .config import Company

_ENTRY = re.compile(r"^\s*-\s*(\{.*\})\s*(#.*)?$")


def load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return list((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("companies") or [])


def _entry_lines(lines: list[str]) -> list[int] | None:
    """Line numbers of the entries, when every entry is a one-line mapping; None otherwise."""
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "companies:")
    except StopIteration:
        return None
    found = []
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not _ENTRY.match(lines[i]):
            return None
        found.append(i)
    return found


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _line(entry: dict) -> str:
    return f"  - {json.dumps(entry, ensure_ascii=False)}"


def save_all(path: Path, entries: list[dict]) -> None:
    """Rewrite the list, keeping the comment header above `companies:`."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    header = []
    for line in lines:
        if line.strip() == "companies:":
            break
        header.append(line)
    _write(path, "\n".join(header + ["companies:"] + [_line(e) for e in entries]) + "\n")


def change(path: Path, index: int, entry: dict | None) -> None:
    """Replace entry `index` with `entry` (validated), or remove it with None."""
    entries = load(path)
    if not 0 <= index < len(entries):
        raise IndexError(index)
    if entry is not None:
        Company.model_validate(entry)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    where = _entry_lines(lines)
    if where is None or len(where) != len(entries):
        entries = entries[:index] + ([entry] if entry is not None else []) + entries[index + 1:]
        save_all(path, entries)
        return
    if entry is None:
        del lines[where[index]]
    else:
        comment = _ENTRY.match(lines[where[index]]).group(2)
        lines[where[index]] = _line(entry) + (f"  {comment}" if comment else "")
    _write(path, "\n".join(lines) + "\n")


def add(path: Path, entry: dict) -> bool:
    """Append an entry; False if the same board is already listed."""
    Company.model_validate(entry)
    for e in load(path):
        if e.get("ats") == entry.get("ats") and (e.get("slug") or e.get("url")) == (entry.get("slug") or entry.get("url")):
            return False
    text = path.read_text(encoding="utf-8") if path.is_file() else "companies:\n"
    if "companies:" not in text:
        text += "\ncompanies:\n"
    if not text.endswith("\n"):
        text += "\n"
    _write(path, text + _line(entry) + "\n")
    return True
