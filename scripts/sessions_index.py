"""Sezione `## Sessions` di `wiki/index.md` rigenerata dai journal presenti.

Attive (`sessions/<data>/`) e Archivio (`sessions/archive/<data>/`), una riga per
giorno con i link a ogni sessione: index.md non entra nel contesto degli agenti
(context_loader e wiki_embed lo saltano) e il linter vuole un link entrante per
ogni journal, archivio compreso (che compact_sessions tiene sotto `archive_max`).
Tocca solo quella sezione; scrive solo se cambia, in modo atomico.
"""
from __future__ import annotations

import re
from pathlib import Path

from raidho.persistence import PersistenceError, read_text, revision, write_text

HEADING = "## Sessions"
EMPTY = "_(nessuna sessione ancora)_"
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_DAY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _ids(directory: Path) -> list:
    return sorted(f.stem for f in directory.glob("*.md")
                  if f.is_file() and not f.is_symlink() and _ID_RE.fullmatch(f.stem) and f.stem != "index")


def _days(directory: Path) -> list:
    if not directory.is_dir() or directory.is_symlink():
        return []
    days = []
    for d in sorted(directory.iterdir(), reverse=True):
        if d.name == "archive" or d.is_symlink() or not d.is_dir() or not _DAY_RE.fullmatch(d.name):
            continue
        ids = _ids(d)
        if ids:
            days.append((d.name, ids))
    return days


def _lines(days: list) -> list:
    return [f"- {day} ({len(ids)}): " + " · ".join(f"[[{i}]]" for i in ids) for day, ids in days]


def render(sessions: Path) -> str:
    active = _days(sessions)
    legacy = _ids(sessions) if sessions.is_dir() and not sessions.is_symlink() else []
    if legacy:
        active.append(("legacy", legacy))
    archived = _days(sessions / "archive")
    if not active and not archived:
        return EMPTY
    parts = []
    if active:
        parts += ["### Attive", "", *_lines(active), ""]
    if archived:
        parts += ["### Archivio", "", *_lines(archived), ""]
    return "\n".join(parts).rstrip("\n")


def replace_section(text: str, content: str) -> str:
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == HEADING), None)
    block = [HEADING, "", content]
    if start is None:
        return text.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^##\s", lines[i])), len(lines))
    tail = lines[end:]
    return "\n".join(lines[:start] + block + ([""] + tail if tail else [""]))


def update(wiki: Path) -> bool:
    """True se index.md e' cambiato. Senza index.md non crea niente."""
    wiki = Path(wiki).resolve()
    index = wiki / "index.md"
    if index.is_symlink() or not index.is_file():
        return False
    for _ in range(3):
        text = read_text(index)
        if text is None:
            return False
        new = replace_section(text, render(wiki / "sessions"))
        if new == text:
            return False
        try:
            write_text(index, new, revision(text))
            return True
        except PersistenceError:
            continue
    return False
