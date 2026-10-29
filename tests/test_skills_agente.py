"""D6 (RAI-940 T3.4): il gruppo MCP `skills` manca solo agli agenti di Raidho (RAIDHO_AGENT non vuoto)."""
import json
import os
import subprocess
import sys
from pathlib import Path

from test_hardening import PLUGIN, PYTHON, call, project, rpc

SKILL_TOOLS = {"skill_list", "skill_load", "skill_read_file", "skill_save"}


def lista(tmp_path, extra_env=None):
    project(tmp_path)
    risposta = rpc(tmp_path, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], extra_env=extra_env)[0]
    return {t["name"] for t in risposta["result"]["tools"]}


def test_agente_senza_gruppo_skills_gli_altri_restano(tmp_path):
    nomi = lista(tmp_path, {"RAIDHO_AGENT": "x"})
    assert not [n for n in nomi if n.startswith("skill_")]
    assert {"wiki_read", "memory_recall"} <= nomi


def test_agente_anche_con_skills_nei_gruppi_e_chiamata_diretta_rifiutata(tmp_path):
    env = {"RAIDHO_AGENT": "x", "RAIDHO_TOOL_GROUPS": "memory,skills,wiki"}
    nomi = lista(tmp_path, env)
    assert not [n for n in nomi if n.startswith("skill_")]
    assert "wiki_read" in nomi
    for tool in ("skill.load", "skill_save", "skill.list"):
        r = rpc(tmp_path, [call(tool, {"name": "x"})], extra_env=env)[0]
        assert r["error"]["code"] == -32601, r


def test_sessione_locale_senza_agente_ha_le_skill(tmp_path):
    assert SKILL_TOOLS <= lista(tmp_path)


def test_agente_vuoto_conta_come_locale(tmp_path):
    assert SKILL_TOOLS <= lista(tmp_path, {"RAIDHO_AGENT": ""})
    assert SKILL_TOOLS <= lista(tmp_path / "b", {"RAIDHO_AGENT": "", "RAIDHO_TOOL_GROUPS": "skills"})


def session_start(tmp_path, extra_env):
    project(tmp_path)
    skill = tmp_path / ".raidhowiki" / "skills" / "prova-d6"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: prova-d6\ndescription: skill di prova D6\n---\ncorpo\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), **extra_env}
    r = subprocess.run([PYTHON, str(PLUGIN / "hooks" / "session_start.py")], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60, input="{}")
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_session_start_catalogo_solo_in_locale(tmp_path):
    locale = session_start(tmp_path / "l", {})
    assert "skill.load" in locale and "prova-d6" in locale
    vuoto = session_start(tmp_path / "v", {"RAIDHO_AGENT": ""})
    assert "skill.load" in vuoto
    agente = session_start(tmp_path / "a", {"RAIDHO_AGENT": "x"})
    assert "skill.load" not in agente and "prova-d6" not in agente
    assert "[raidho] Sessione aperta" in agente
