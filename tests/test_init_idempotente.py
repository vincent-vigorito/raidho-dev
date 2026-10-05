"""init_project.py idempotente (RAI-561): il provisioning di Raidho lo lancia a ogni
aggiornamento del plugin, quindi con la wiki gia' fatta non deve cambiare niente ('wiki:
presente', exit 0), e con un AGENTS.md / CLAUDE.md dell'utente il contenuto resta nel
file composto (AGENTS.src.md, CLAUDE.original.md). HOME finta, cartella temporanea."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from _helpers import cov_env

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
INIT = PLUGIN_ROOT / "scripts" / "init_project.py"
PYTHON = os.environ.get("RAIDHO_TEST_PYTHON") or sys.executable

AGENTS_UTENTE = "# Il mio progetto\n\nRegola 552: usa sempre i tab. Comando: make test-rapidi\n"
CLAUDE_UTENTE = "# Note mie per Claude\n\nNon toccare la cartella legacy/ (CLAUDE-552)\n"


def init(project: Path, home: Path, *extra: str):
    env = {**os.environ, "HOME": str(home), **cov_env()}
    env.pop("RAIDHO_PROJECT_ROOT", None)
    return subprocess.run([PYTHON, str(INIT), "--type", "dev", "--mode", "cold", "--target", str(project / ".raidhowiki"),
                           *extra], capture_output=True, text=True, timeout=120, env=env)


def albero(root: Path) -> dict:
    """{percorso relativo: sha1 del contenuto} di tutti i file (e dei symlink)."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = "->" + os.readlink(p)
        elif p.is_file():
            out[rel] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def prepara(tmp_path: Path):
    project = tmp_path / "progetto"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return project, home


def test_prima_volta_crea_wiki_e_triade(tmp_path):
    project, home = prepara(tmp_path)
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert "wiki: presente" not in r.stdout
    assert (project / ".raidhowiki" / "meta.yaml").is_file()
    for f in ("AGENTS.md", "AGENTS.src.md", "SOUL.md", "TOOLS.md", "CLAUDE.md"):
        assert (project / f).is_file(), f
    assert "@AGENTS.md" in (project / "CLAUDE.md").read_text()


def test_seconda_esecuzione_wiki_presente_e_albero_identico(tmp_path):
    project, home = prepara(tmp_path)
    assert init(project, home).returncode == 0
    prima = albero(project)
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "wiki: presente"
    assert albero(project) == prima


def test_terza_esecuzione_con_nome_o_tipo_diversi_non_cambia_niente(tmp_path):
    project, home = prepara(tmp_path)
    assert init(project, home).returncode == 0
    prima = albero(project)
    r = init(project, home, "--name", "altro-nome")
    assert (r.returncode, r.stdout.strip()) == (0, "wiki: presente")
    assert albero(project) == prima


def test_con_meta_yaml_basta_per_dire_presente(tmp_path):
    project, home = prepara(tmp_path)
    wiki = project / ".raidhowiki"
    wiki.mkdir()
    (wiki / "meta.yaml").write_text("token: x\n")
    (project / "AGENTS.md").write_text(AGENTS_UTENTE)
    prima = albero(project)
    r = init(project, home)
    assert (r.returncode, r.stdout.strip()) == (0, "wiki: presente")
    assert albero(project) == prima
    assert not (project / "AGENTS.src.md").exists()


def test_agents_md_dell_utente_diventa_src_e_finisce_nel_composto(tmp_path):
    project, home = prepara(tmp_path)
    (project / "AGENTS.md").write_text(AGENTS_UTENTE)
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert (project / "AGENTS.src.md").read_text() == AGENTS_UTENTE
    composto = (project / "AGENTS.md").read_text()
    assert "Regola 552: usa sempre i tab" in composto
    assert "make test-rapidi" in composto
    assert "auto_generated" in composto[:500] or "auto-generated" in composto[:500].lower()
    assert composto != AGENTS_UTENTE
    assert (project / "SOUL.md").is_file() and (project / "TOOLS.md").is_file()


def test_dopo_l_init_con_agents_utente_il_secondo_giro_non_tocca_nulla(tmp_path):
    project, home = prepara(tmp_path)
    (project / "AGENTS.md").write_text(AGENTS_UTENTE)
    assert init(project, home).returncode == 0
    prima = albero(project)
    r = init(project, home)
    assert (r.returncode, r.stdout.strip()) == (0, "wiki: presente")
    assert albero(project) == prima
    assert (project / "AGENTS.src.md").read_text() == AGENTS_UTENTE


def test_claude_md_dell_utente_conservato_in_original(tmp_path):
    project, home = prepara(tmp_path)
    (project / "CLAUDE.md").write_text(CLAUDE_UTENTE)
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert (project / "CLAUDE.original.md").read_text() == CLAUDE_UTENTE
    assert "@AGENTS.md" in (project / "CLAUDE.md").read_text()
    prima = albero(project)
    assert init(project, home).stdout.strip() == "wiki: presente"
    assert albero(project) == prima
    assert (project / "CLAUDE.original.md").read_text() == CLAUDE_UTENTE


def test_agents_e_claude_dell_utente_insieme(tmp_path):
    project, home = prepara(tmp_path)
    (project / "AGENTS.md").write_text(AGENTS_UTENTE)
    (project / "CLAUDE.md").write_text(CLAUDE_UTENTE)
    assert init(project, home).returncode == 0
    assert (project / "AGENTS.src.md").read_text() == AGENTS_UTENTE
    assert (project / "CLAUDE.original.md").read_text() == CLAUDE_UTENTE
    assert "Regola 552" in (project / "AGENTS.md").read_text()


def test_agents_gia_composto_non_viene_rinominato(tmp_path):
    project, home = prepara(tmp_path)
    assert init(project, home).returncode == 0
    sorgente = (project / "AGENTS.src.md").read_text()
    # wiki cancellata a mano, triade rimasta: AGENTS.md composto (marker) resta composto
    shutil.rmtree(project / ".raidhowiki")
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert (project / "AGENTS.src.md").read_text() == sorgente
    assert (project / ".raidhowiki" / "meta.yaml").is_file()


def test_agents_md_symlink_non_viene_sovrascritto(tmp_path):
    project, home = prepara(tmp_path)
    reale = tmp_path / "reale.md"
    reale.write_text(AGENTS_UTENTE)
    (project / "AGENTS.md").symlink_to(reale)
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert reale.read_text() == AGENTS_UTENTE
    assert not (project / "AGENTS.src.md").is_symlink()


def test_il_file_dell_utente_non_viene_toccato_se_l_init_non_parte(tmp_path):
    project, home = prepara(tmp_path)
    (project / "AGENTS.md").write_text(AGENTS_UTENTE)
    r = subprocess.run([PYTHON, str(INIT), "--type", "dev", "--mode", "cold"], capture_output=True, text=True,
                       timeout=60, env={**os.environ, "HOME": str(home)})
    assert r.returncode != 0
    assert (project / "AGENTS.md").read_text() == AGENTS_UTENTE
    assert not (project / "AGENTS.src.md").exists()


def test_agents_md_utente_con_src_gia_presente_non_va_perso(tmp_path):
    project, home = prepara(tmp_path)
    (project / "AGENTS.src.md").write_text("# sorgente gia' mio\n")
    (project / "AGENTS.md").write_text(AGENTS_UTENTE)
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    qui = {p.name: p.read_text() for p in project.glob("AGENTS*.md")}
    assert any("Regola 552" in t for t in qui.values()), qui.keys()


# --- .raidhowiki a meta': cartella presente senza meta.yaml (init interrotto) ---

INDEX_CUSTOM = "# Indice scritto a mano\n\n- [[nota-552]] non toccare\n"
FILE_WIKI = ("meta.yaml", "wiki/index.md", "wiki/log.md", "wiki/overview.md")


def wiki_a_meta(project: Path) -> Path:
    """.raidhowiki con il solo wiki/index.md dell'utente: niente meta.yaml, log e overview."""
    wiki = project / ".raidhowiki"
    (wiki / "wiki").mkdir(parents=True)
    (wiki / "wiki" / "index.md").write_text(INDEX_CUSTOM)
    return wiki


def test_wiki_a_meta_si_completa_senza_toccare_l_index_dell_utente(tmp_path):
    project, home = prepara(tmp_path)
    wiki = wiki_a_meta(project)
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert "wiki: presente" not in r.stdout
    assert "[raidho] initialized" in r.stdout
    assert (wiki / "wiki" / "index.md").read_text() == INDEX_CUSTOM
    for f in FILE_WIKI:
        assert (wiki / f).is_file(), f
    meta = (wiki / "meta.yaml").read_text()
    assert 'name: "progetto"' in meta and "{{" not in meta
    assert "{{" not in (wiki / "wiki" / "log.md").read_text()
    for f in ("AGENTS.md", "AGENTS.src.md", "SOUL.md", "TOOLS.md", "CLAUDE.md"):
        assert (project / f).is_file(), f


def test_wiki_a_meta_non_sovrascrive_nessun_file_presente(tmp_path):
    project, home = prepara(tmp_path)
    wiki = wiki_a_meta(project)
    (wiki / "wiki" / "overview.md").write_text("# Overview mia\n")
    (wiki / "wiki" / "log.md").write_text("# Log mio\n")
    extra = wiki / "wiki" / "concepts"
    extra.mkdir()
    (extra / "x.md").write_text("concetto 552\n")
    mio = {f: (wiki / f).read_text() for f in ("wiki/index.md", "wiki/overview.md", "wiki/log.md", "wiki/concepts/x.md")}
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert {f: (wiki / f).read_text() for f in mio} == mio
    assert (wiki / "meta.yaml").is_file()


def test_wiki_a_meta_secondo_giro_presente_e_albero_identico(tmp_path):
    project, home = prepara(tmp_path)
    wiki_a_meta(project)
    assert init(project, home).returncode == 0
    prima = albero(project)
    r = init(project, home)
    assert (r.returncode, r.stdout.strip()) == (0, "wiki: presente")
    assert albero(project) == prima


def test_wiki_a_meta_con_agents_dell_utente_lo_conserva(tmp_path):
    project, home = prepara(tmp_path)
    wiki_a_meta(project)
    (project / "AGENTS.md").write_text(AGENTS_UTENTE)
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert (project / "AGENTS.src.md").read_text() == AGENTS_UTENTE
    assert "Regola 552" in (project / "AGENTS.md").read_text()
    assert (project / ".raidhowiki" / "wiki" / "index.md").read_text() == INDEX_CUSTOM


def test_wiki_a_meta_cartella_vuota_come_prima_volta(tmp_path):
    project, home = prepara(tmp_path)
    (project / ".raidhowiki").mkdir()
    r = init(project, home)
    assert r.returncode == 0, r.stderr
    assert (project / ".raidhowiki" / "meta.yaml").is_file()
    assert (project / ".raidhowiki" / "wiki" / "index.md").is_file()


def test_wiki_a_meta_con_meta_yaml_ma_senza_wiki_resta_presente(tmp_path):
    """Il criterio di 'fatta' e' meta.yaml: un file mancante diverso non rilancia l'init."""
    project, home = prepara(tmp_path)
    assert init(project, home).returncode == 0
    (project / ".raidhowiki" / "wiki" / "overview.md").unlink()
    r = init(project, home)
    assert (r.returncode, r.stdout.strip()) == (0, "wiki: presente")
    assert not (project / ".raidhowiki" / "wiki" / "overview.md").exists()
