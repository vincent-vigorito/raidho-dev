"""#358: sezione `## Sessions` di index.md rigenerata dai journal.

sessions_index.update (Attive/Archivio, conteggi, link, solo quella sezione, idempotente,
symlink e nomi ostili), write_session_file e compact_sessions --apply che la aggiornano,
lint_checks senza link rotti per i journal nelle cartelle dei giorni.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from _helpers import cov_env

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "scripts"
PYTHON = os.environ.get("RAIDHO_TEST_PYTHON") or sys.executable
sys.path.insert(0, str(SCRIPTS))
import sessions_index as si  # noqa: E402
from raidho import persistence  # noqa: E402

HEAD = "---\ntitle: Index\ntype: index\n---\n\n# Index\n\n## Concepts\n\n- [[only-one]]  \n\n"
TAIL = "## Altro\n\nriga finale con spazi  \n\n"


def journal(sid, day="2026-10-01"):
    return (f"---\ntitle: Session {sid}\ntype: session\ncreated: {day}\nupdated: {day}\nid: {sid}\n"
            f"date: {day}\n---\n\n# Session {sid}\n")


def put(wiki, rel, sid):
    d = wiki / "sessions" / rel
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.md"
    f.write_text(journal(sid))
    return f


def make_wiki(tmp_path, index=HEAD + "## Sessions\n\nvecchio contenuto\n[[sparito]]\n\n" + TAIL):
    wiki = tmp_path / "proj" / ".raidhowiki" / "wiki"
    (wiki / "sessions").mkdir(parents=True)
    (wiki / "concepts").mkdir()
    (wiki / "concepts" / "only-one.md").write_text("---\ntitle: Only one\ntype: concept\n---\n\n# Only one\n")
    if index is not None:
        (wiki / "index.md").write_text(index)
    return wiki


def section(text):
    lines = text.split("\n")
    start = lines.index("## Sessions")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def test_attive_archivio_conteggi_link_e_resto_identico(tmp_path):
    wiki = make_wiki(tmp_path)
    put(wiki, "2026-09-30", "090000-cli-claude-aaaa")
    put(wiki, "2026-10-01", "100000-cli-claude-bbbb")
    put(wiki, "2026-10-01", "080000-cli-codex-cccc")
    put(wiki, "archive/2026-09-01", "070000-cli-claude-dddd")
    (wiki / "sessions" / "2026-10-02").mkdir()          # giorno vuoto: nessuna riga
    (wiki / "sessions" / "2026-10-01" / "note.txt").write_text("non md")

    assert si.update(wiki) is True
    text = (wiki / "index.md").read_text()
    assert text.startswith(HEAD + "## Sessions\n")
    assert text.endswith("\n" + TAIL)
    assert "vecchio contenuto" not in text and "[[sparito]]" not in text
    assert section(text) == (
        "## Sessions\n\n### Attive\n\n"
        "- 2026-10-01 (2): [[080000-cli-codex-cccc]] · [[100000-cli-claude-bbbb]]\n"
        "- 2026-09-30 (1): [[090000-cli-claude-aaaa]]\n\n"
        "### Archivio\n\n"
        "- 2026-09-01 (1): [[070000-cli-claude-dddd]]\n")


def test_secondo_giro_non_scrive(tmp_path):
    wiki = make_wiki(tmp_path)
    put(wiki, "2026-10-01", "100000-cli-claude-bbbb")
    assert si.update(wiki) is True
    index = wiki / "index.md"
    before = index.read_text()
    os.utime(index, ns=(1_000_000_000, 1_000_000_000))
    ino = index.stat().st_ino
    assert si.update(wiki) is False
    assert index.stat().st_mtime_ns == 1_000_000_000 and index.stat().st_ino == ino
    assert index.read_text() == before


def test_sezione_assente_aggiunta_in_fondo(tmp_path):
    wiki = make_wiki(tmp_path, index=HEAD + TAIL)
    put(wiki, "archive/2026-09-01", "070000-cli-claude-dddd")
    assert si.update(wiki) is True
    text = (wiki / "index.md").read_text()
    assert text.startswith(HEAD + TAIL.rstrip("\n") + "\n\n## Sessions\n\n### Archivio\n")
    assert "### Attive" not in text
    assert text.endswith("[[070000-cli-claude-dddd]]\n")
    assert si.update(wiki) is False


def test_sezione_in_fondo_e_senza_sessioni(tmp_path):
    wiki = make_wiki(tmp_path, index=HEAD + "## Sessions\n\n- [[vecchia]]\n")
    assert si.update(wiki) is True
    assert (wiki / "index.md").read_text() == HEAD + "## Sessions\n\n" + si.EMPTY + "\n"
    assert si.update(wiki) is False


def test_journal_legacy_nella_radice(tmp_path):
    wiki = make_wiki(tmp_path)
    (wiki / "sessions" / "2026-01-01-vecchio.md").write_text(journal("x"))
    (wiki / "sessions" / "index.md").write_text("indice delle sessioni")
    si.update(wiki)
    assert "- legacy (1): [[2026-01-01-vecchio]]" in (wiki / "index.md").read_text()


def test_index_mancante_non_crea_niente(tmp_path):
    wiki = make_wiki(tmp_path, index=None)
    put(wiki, "2026-10-01", "100000-cli-claude-bbbb")
    assert si.update(wiki) is False
    assert not (wiki / "index.md").exists()


def test_index_symlink_non_toccato(tmp_path):
    wiki = make_wiki(tmp_path, index=None)
    target = tmp_path / "fuori.md"
    target.write_text(HEAD)
    (wiki / "index.md").symlink_to(target)
    put(wiki, "2026-10-01", "100000-cli-claude-bbbb")
    assert si.update(wiki) is False
    assert target.read_text() == HEAD and (wiki / "index.md").is_symlink()


def test_giorni_e_file_symlink_saltati(tmp_path):
    wiki = make_wiki(tmp_path)
    fuori = tmp_path / "fuori"
    fuori.mkdir()
    (fuori / "segreto-1.md").write_text("x")
    (wiki / "sessions" / "2026-09-15").symlink_to(fuori, target_is_directory=True)
    (wiki / "sessions" / "archive").mkdir()
    (wiki / "sessions" / "archive" / "2026-09-14").symlink_to(fuori, target_is_directory=True)
    vero = put(wiki, "2026-10-01", "100000-cli-claude-bbbb")
    (vero.parent / "link-1.md").symlink_to(vero)
    (wiki / "sessions" / "legacy-link.md").symlink_to(vero)
    si.update(wiki)
    sec = section((wiki / "index.md").read_text())
    assert "segreto-1" not in sec and "link-1" not in sec and "legacy" not in sec
    assert "2026-09-15" not in sec and "2026-09-14" not in sec and "### Archivio" not in sec
    assert "- 2026-10-01 (1): [[100000-cli-claude-bbbb]]" in sec


def test_cartella_sessions_symlink_ignorata(tmp_path):
    wiki = make_wiki(tmp_path)
    (wiki / "sessions").rmdir()
    fuori = tmp_path / "fuori"
    (fuori / "2026-10-01").mkdir(parents=True)
    (fuori / "2026-10-01" / "abc-1.md").write_text("x")
    (fuori / "legacy-1.md").write_text("x")
    (wiki / "sessions").symlink_to(fuori, target_is_directory=True)
    si.update(wiki)
    assert section((wiki / "index.md").read_text()) == "## Sessions\n\n" + si.EMPTY + "\n"


@pytest.mark.parametrize("ostile", ["A<img src=x>", "x](y)", "x](javascript:alert(1))", "a]]b", "a b",
                                    "a|b", "-abc", "ABC-1", "àccento", "a\nb", "index"])
def test_nomi_file_ostili_mai_link(tmp_path, ostile):
    wiki = make_wiki(tmp_path)
    d = wiki / "sessions" / "2026-10-01"
    d.mkdir()
    (d / f"{ostile}.md").write_text("x")
    (d / "abc-1.md").write_text("x")
    si.update(wiki)
    sec = section((wiki / "index.md").read_text())
    assert sec == "## Sessions\n\n### Attive\n\n- 2026-10-01 (1): [[abc-1]]\n"


@pytest.mark.parametrize("giorno", ["x](javascript:alert(1))", "x](y)", "<img src=x>", "a b", ".nascosto", "a\nb"])
def test_nomi_giorno_ostili_saltati(tmp_path, giorno):
    wiki = make_wiki(tmp_path)
    put(wiki, giorno, "abc-1")
    put(wiki, "archive/" + giorno, "abc-2")
    si.update(wiki)
    assert section((wiki / "index.md").read_text()) == "## Sessions\n\n" + si.EMPTY + "\n"


def test_conflitto_di_revisione_riprova(tmp_path, monkeypatch):
    wiki = make_wiki(tmp_path)
    put(wiki, "2026-10-01", "100000-cli-claude-bbbb")
    real, calls = si.write_text, []

    def flaky(path, text, rev):
        calls.append(rev)
        if len(calls) == 1:   # un altro processo ha scritto index.md nel frattempo
            path.write_text(path.read_text() + "aggiunta concorrente\n")
        return real(path, text, rev)

    monkeypatch.setattr(si, "write_text", flaky)
    assert si.update(wiki) is True
    text = (wiki / "index.md").read_text()
    assert len(calls) == 2
    assert "aggiunta concorrente" in text and "[[100000-cli-claude-bbbb]]" in text


def test_conflitti_continui_si_arrende(tmp_path, monkeypatch):
    wiki = make_wiki(tmp_path)
    put(wiki, "2026-10-01", "100000-cli-claude-bbbb")
    before = (wiki / "index.md").read_text()

    def always(*_a):
        raise persistence.PersistenceError("conflict", "conflitto")

    monkeypatch.setattr(si, "write_text", always)
    assert si.update(wiki) is False
    assert (wiki / "index.md").read_text() == before


# --- write_session_file ---------------------------------------------------------------

def load_session_end():
    spec = importlib.util.spec_from_file_location("se358", PLUGIN / "hooks" / "session_end.py")
    se = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(se)
    return se


TRANSCRIPT = {"user_messages": ["a"] * 3, "assistant_messages_count": 3, "tools_used": {},
              "started": "2026-10-01T09:00:00+00:00", "ended": "2026-10-01T10:00:00+00:00"}


def test_write_session_file_aggiorna_la_sezione(tmp_path):
    se = load_session_end()
    wiki = make_wiki(tmp_path)
    f = se.write_session_file(wiki / "sessions", "project", {"session_id": "s1", "agent": "cli-claude"}, TRANSCRIPT)
    day = f.parent.name
    text = (wiki / "index.md").read_text()
    assert f"- {day} (1): [[{f.stem}]]" in section(text)
    assert text.startswith(HEAD) and text.endswith(TAIL)
    f2 = se.write_session_file(wiki / "sessions", "project", {"session_id": "s2", "agent": "cli-codex"}, TRANSCRIPT)
    assert f"- {day} (2): " in section((wiki / "index.md").read_text())
    assert f"[[{f2.stem}]]" in (wiki / "index.md").read_text()


def test_write_session_file_hub_non_tocca_index(tmp_path):
    se = load_session_end()
    hub = tmp_path / "hub"
    (hub / "sessions").mkdir(parents=True)
    (hub / "index.md").write_text(HEAD)
    se.write_session_file(hub / "sessions", "hub", {"session_id": "s1", "agent": "cli-claude"}, TRANSCRIPT)
    assert (hub / "index.md").read_text() == HEAD


def test_errore_dell_update_non_blocca_la_sessione(tmp_path, monkeypatch, capsys):
    se = load_session_end()
    wiki = make_wiki(tmp_path)
    before = (wiki / "index.md").read_text()

    def boom(_wiki):
        raise RuntimeError("index rotto")

    monkeypatch.setattr(si, "update", boom)
    monkeypatch.setitem(sys.modules, "sessions_index", si)
    f = se.write_session_file(wiki / "sessions", "project", {"session_id": "s1", "agent": "cli-claude"}, TRANSCRIPT)
    assert f.is_file() and "type: session" in f.read_text()
    assert (wiki / "index.md").read_text() == before
    assert "sessions index update failed: index rotto" in capsys.readouterr().err


def test_index_illeggibile_non_blocca_la_sessione(tmp_path):
    se = load_session_end()
    wiki = make_wiki(tmp_path, index=None)
    (wiki / "index.md").write_bytes(b"\xff\xfe non utf-8")
    f = se.write_session_file(wiki / "sessions", "project", {"session_id": "s1", "agent": "cli-claude"}, TRANSCRIPT)
    assert f.is_file()
    assert (wiki / "index.md").read_bytes() == b"\xff\xfe non utf-8"


# --- compact_sessions --apply ---------------------------------------------------------

def old_session(sid, day):
    return ("---\n" + "\n".join([
        f"title: Session {sid}", "type: session", f"created: {day}", f"updated: {day}", f"id: {sid}",
        "transcript_path: /t/x.jsonl", "duration: 1m 10s", "scope: project", "agent: cli-claude",
        "harness: claude", f"date: {day}", "end_reason: prompt_input_exit", "messages_user: 2",
        "messages_assistant: 3"]) + f"\n---\n\n# Session {sid}\n\n## Summary\n\n- cosa\n\n"
        "## User prompts\n\n- uno\n- due\n")


def run_compact(proj, *extra):
    return subprocess.run([PYTHON, str(SCRIPTS / "compact_sessions.py"), "--root", str(proj), "--json",
                           "--older-than", "14", *extra],
                          capture_output=True, text=True, timeout=60, cwd=str(proj), env={**os.environ, **cov_env()})


def test_compact_apply_sposta_in_archivio(tmp_path):
    wiki = make_wiki(tmp_path)
    proj = wiki.parent.parent
    (proj / ".raidhowiki" / "meta.yaml").write_text("name: proj\n")
    old = (date.today() - timedelta(days=20)).isoformat()
    sid = "100003-cli-claude-s001"
    (wiki / "sessions" / old).mkdir()
    (wiki / "sessions" / old / f"{sid}.md").write_text(old_session(sid, old))
    si.update(wiki)
    assert f"### Attive\n\n- {old} (1): [[{sid}]]" in (wiki / "index.md").read_text()

    dry = run_compact(proj)
    assert dry.returncode == 0, dry.stderr
    assert f"### Attive\n\n- {old} (1): [[{sid}]]" in (wiki / "index.md").read_text()

    r = run_compact(proj, "--apply")
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)
    assert len(rep["archived"]) == 1 and not rep["errors"], rep
    text = (wiki / "index.md").read_text()
    assert section(text) == f"## Sessions\n\n### Archivio\n\n- {old} (1): [[{sid}]]\n"
    assert text.startswith(HEAD) and text.endswith(TAIL)


def test_compact_importabile_come_modulo(tmp_path):
    """steward e i test caricano compact_sessions con spec_from_file_location, senza scripts/ in sys.path."""
    code = ("import importlib.util, sys; "
            f"spec = importlib.util.spec_from_file_location('cs', {str(SCRIPTS / 'compact_sessions.py')!r}); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print('ok')")
    r = subprocess.run([PYTHON, "-c", code], capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
                       env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-400:]


# --- lint_checks ----------------------------------------------------------------------

def test_lint_nessun_link_rotto_per_le_sessioni(tmp_path):
    wiki = make_wiki(tmp_path)
    put(wiki, "2026-10-01", "100000-cli-claude-bbbb")
    put(wiki, "archive/2026-09-01", "070000-cli-claude-dddd")
    d = wiki / "sessions" / "2026-10-01"
    for ostile in ("A<img src=x>", "x](y)"):
        (d / f"{ostile}.md").write_text(journal("o"))
    (wiki / "sessions" / "legacy-1.md").write_text(journal("legacy-1"))
    si.update(wiki)
    r = subprocess.run([PYTHON, str(SCRIPTS / "lint_checks.py"), "--wiki-root", str(wiki)],
                       capture_output=True, text=True, timeout=60, env={**os.environ, **cov_env()})
    assert r.returncode == 0, r.stderr
    issues = json.loads(r.stdout)["issues"]
    assert [i for i in issues if i["type"] == "broken-link"] == []
    assert not [i for i in issues if i["type"] == "orphan" and i["page"] in
                ("100000-cli-claude-bbbb", "070000-cli-claude-dddd", "legacy-1")]
