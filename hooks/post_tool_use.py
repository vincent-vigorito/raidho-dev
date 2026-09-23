#!/usr/bin/env python3
"""post_tool_use.py — hook PostToolUse: rieffettua embedding wiki pages
toccate via Write/Edit/MultiEdit fuori dai tool `wiki.upsert_*` (che già
triggerano inline il re-embed).

Input via stdin (JSON dal Claude Code harness):
  {
    "tool_name": "Write" | "Edit" | "MultiEdit" | ...,
    "tool_input": {"file_path": "...", ...},
    ...
  }

Match: `tool_name in {Write,Edit,MultiEdit}` AND
       path matches `<root>/.raidhowiki/wiki/**.md`.

Skip silenzioso se RAIDHO_WIKI_EMBED=0 o se non c'è wiki raidho.
Fire-and-forget: avvia `wiki_jobs.py --single <path>` detached.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_WIKI_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}


def _parse_stdin() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def _find_raidhowiki_root(file_path: Path) -> Path | None:
    """Risale dal file fino a trovare un dir con `.raidhowiki/`.
    Ritorna la project root (parent di .raidhowiki/), o None.
    """
    cur = file_path.resolve()
    for p in [cur] + list(cur.parents):
        if (p / ".raidhowiki").is_dir():
            return p
        # Se siamo dentro .raidhowiki/, il parent è la root
        if p.name == ".raidhowiki" and p.parent.exists():
            return p.parent
    return None


def _is_wiki_page(file_path: Path, project_root: Path) -> bool:
    """Vero se path è dentro <project>/.raidhowiki/wiki/ e .md."""
    if file_path.suffix.lower() != ".md":
        return False
    wiki_root = project_root / ".raidhowiki" / "wiki"
    try:
        file_path.resolve().relative_to(wiki_root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _trigger_embed_bg(project_root: Path, md_path: Path) -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "wiki_jobs.py"
    if not script.is_file():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(script), str(project_root), "--single", str(md_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def main() -> None:
    if os.environ.get("RAIDHO_WIKI_EMBED", "1") == "0":
        return

    payload = _parse_stdin()
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _WIKI_WRITE_TOOLS:
        return

    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path") or tool_input.get("path")
    if not raw_path:
        return

    file_path = Path(raw_path).expanduser()
    if not file_path.is_absolute():
        # CC dovrebbe sempre passare path assoluto, ma graceful
        file_path = (Path.cwd() / file_path).resolve()

    project_root = _find_raidhowiki_root(file_path)
    if project_root is None:
        return

    if not _is_wiki_page(file_path, project_root):
        return

    _trigger_embed_bg(project_root, file_path)


if __name__ == "__main__":
    main()
